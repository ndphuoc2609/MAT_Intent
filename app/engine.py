"""Flow/state engine + gate. Python sở hữu state, policy, facts, outbox.
Model chỉ đề xuất operations; mọi commit đều qua validate + gate ở đây."""
from __future__ import annotations
import hashlib, json, uuid, re
from datetime import datetime, timedelta, timezone
from .normalize import VEHICLES, canon_slot, norm, parse_time_expression, fmt_money, yes_no
from .semantics import attach_context_provenance, validate_semantic
from .facts import (DEALER_DIRECTORY_URL, Facts, calc_otr,
                    dealer_public_address)
from .renderer import render, claim_check, claim_check_typed
from .state import EventPayloadConflict, Store, FakeSender
from .policy import AUTO_ACTIONS, HANDOFF_SLA_HOURS, PENDING_TTL_HOURS
from .persona_query import parse_persona_questions
from .persona_reply import render_persona_reply
from .persona_runtime import PersonaGenerationRuntime, validated_peer_signals, peer_plan_from_signals
from .lead_flow import (address as call_term, extract_answer, is_decline, peer_cue, plan_peer_turn, purchase_subject_correction, proxy_clarification,
                        states_ownership, social_reaction)


def _delivery_status(result):
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("sent_to_fake_ledger", "sent"):
        return "SENT"
    if status in ("failed", "FAILED"):
        return "FAILED"
    return "UNKNOWN"

def _social_reply(text):
    if is_decline(text):
        return 'Ừ, mình không hỏi thêm nhé.'
    reaction = social_reaction(text)
    if reaction:
        return {'praise': 'Ừ, nhìn cũng thích mắt nhỉ.', 'laughter': 'Haha, vui mà.',
                'tag_friend': 'Cứ rủ bạn vào cùng trao đổi nhé.'}[reaction]
    message = norm(text or "")
    words = set(message.split())
    if words & {"chao", "hello", "hi"}:
        return "Chào bạn! Mình có thể giúp gì?"
    if "cam on" in message:
        before_thanks = message.split("cam on", 1)[0].split()
        after_thanks = message.split("cam on", 1)[1].split()
        if set(before_thanks + after_thanks) & {"khong", "ko", "chua", "dau"}:
            return "Mình hiểu rồi."
        return "Không có gì, bạn nhé!"
    if words & {"ok", "oke", "okay", "oki"}:
        return "Oke bạn nhé."
    if message and not message.strip('👍👌😊🙂❤️❤👏🙏😍🎉! .'):
        return 'Mình nhận được rồi nhé.'
    return None

def _color_advice(vehicle=None):
    available_for = f" cho {vehicle}" if vehicle else ""
    return ("Mình nghiêng về tông trung tính: dễ phối và nhìn lâu không chán. "
            f"Màu nào đang có sẵn{available_for} thì hiện chưa được xác minh.")

class Clock:
    """FrozenClock cho test/contract: now() luôn trả cùng một mốc."""
    def __init__(self, now: datetime): self._now = now
    def now(self): return self._now
    def advance(self, **kw): self._now = self._now + timedelta(**kw)

class WallClock:
    """Server mode (R15): thời gian thật mỗi lần gọi. Test giữ Clock."""
    def now(self): return datetime.now(timezone.utc)
    def advance(self, **kw): raise RuntimeError("WallClock cannot advance")

def _slot(req, name):
    s = req["slots"].get(name)
    return s["value"] if isinstance(s, dict) else s

def _set_slot(req, name, value, raw, source, explicit=True, confirmed=False,
              source_request_id=None, source_actor_id=None, source_state_token=None, bump=True):
    req["slots"][name] = dict(value=value, raw=raw, source_message_id=source,
                              explicit=explicit, confirmed=confirmed)
    if source_request_id:
        req["slots"][name]["source_request_id"] = source_request_id
    if source_actor_id:
        req["slots"][name]["source_actor_id"] = source_actor_id
    if source_state_token:
        req["slots"][name]["source_state_token"] = source_state_token
    if bump: req["version"] += 1

def _op_identity(op):
    """Identity của op: (vehicle, variant) từ slot hoặc slot.extra."""
    slot = op.get("slot") or {}
    veh = var = None
    if slot.get("kind") == "vehicle": veh = canon_slot("vehicle", slot.get("value"))
    if slot.get("kind") == "variant": var = canon_slot("variant", slot.get("value"))
    ex = slot.get("extra") or {}
    if veh is None and ex.get("vehicle"): veh = canon_slot("vehicle", ex["vehicle"])
    if var is None and ex.get("variant"): var = canon_slot("variant", ex["variant"])
    return veh, var
def _turn_identity(ops, ref):
    """Identity hiệu lực của một request_ref trong cả lượt (R04): START có
    thể không nêu xe/phiên bản nhưng UPDATE sau đó trong cùng lượt nêu rõ
    (VD: START giá + UPDATE xe Tucson). Kiểm tra handoff scope phải dùng
    identity này, không wildcard match chỉ vì START slot-less."""
    veh = var = None
    for o in ops or []:
        if not isinstance(o, dict) or o.get("request_ref") != ref: continue
        v, vv = _op_identity(o)
        if v is not None: veh = v
        if vv is not None: var = vv
    return veh, var

def _start_identity(ops, ref, this_op):
    """Identity để kiểm handoff scope cho MỘT op START (A4): nếu trong lượt
    chỉ có đúng một START dùng ref này thì identity cả lượt (turn identity)
    là đúng — UPDATE sau đó thuộc về START đó. Nếu có NHIỀU START cùng ref
    thì mỗi START chỉ được dùng identity của CHÍNH nó (slot/extra của nó);
    lấy identity cuối lượt áp cho mọi START là last-write-wins và cho phép
    START Creta vượt handoff nhờ identity Tucson."""
    starts = [o for o in (ops or [])
              if isinstance(o, dict) and o.get("op") == "START_REQUEST"
              and (o.get("request_ref") or "") == ref]
    if len(starts) == 1:
        return _turn_identity(ops, ref)
    return _op_identity(this_op)

def _canon_slot_values(slots):
    """Slot values dạng so sánh được (JSON-canonical): dùng cho proposition
    basis (A1) và rejected-proposal fingerprint (A2)."""
    out = {}
    for k, v in (slots or {}).items():
        out[k] = v.get("value") if isinstance(v, dict) else v
    return json.dumps(out, ensure_ascii=False, sort_keys=True, default=str)

# Lease cho outbox CLAIMED (A5): worker khác chỉ được reclaim khi claim cũ
# đã quá hạn này (theo engine clock) — không reset mù khi worker còn gửi.
CLAIM_LEASE_SECONDS = 300

def _ident_match(req, topic, veh, var):
    if req["topic"] != topic: return False
    tv, tvar = _slot(req, "vehicle"), _slot(req, "variant")
    if veh is not None and tv is not None and veh != tv: return False
    if var is not None and tvar is not None and var != tvar: return False
    return True

def _slot_score(req, op):
    # Điểm identity để resolve op sau vào đúng request trong lượt có nhiều
    # request cùng topic (A4): chấm cả slot chính lẫn slot.extra (START
    # multi-xe thường mang vehicle/variant trong extra).
    slot = op.get("slot") or {}; score = 0
    if slot.get("kind") == "vehicle" and _slot(req, "vehicle") == canon_slot("vehicle", slot.get("value")): score += 2
    if slot.get("kind") == "variant" and _slot(req, "variant") == canon_slot("variant", slot.get("value")): score += 1
    ex = slot.get("extra") or {}
    if ex.get("vehicle") and _slot(req, "vehicle") == canon_slot("vehicle", ex["vehicle"]): score += 2
    if ex.get("variant") and _slot(req, "variant") == canon_slot("variant", ex["variant"]): score += 1
    for kind, value in [(slot.get("kind"), slot.get("value")), *ex.items()]:
        if kind not in ("vehicle", "variant") and _slot(req, kind) == canon_slot(kind, value):
            score += 3
    if slot.get("kind") == "price_kind":
        outcome = {"list": "price_list", "otr": "price_otr"}.get(canon_slot("price_kind", slot.get("value")))
        if outcome in req.get("desired_outcomes", []) and _slot(req, "price_kind") is None:
            score += 2
    if slot.get("kind") == "location" and req.get("topic") == "price" and \
            "price_otr" in req.get("desired_outcomes", []) and _slot(req, "location") is None:
        score += 1
    return score

def _new_req(page_id, post_id, thread_root_id, actor_id, topic, outcome, comment_id, now):
    return dict(id=str(uuid.uuid4())[:12], page_id=page_id, post_id=post_id,
                thread_root_id=thread_root_id, actor_id=actor_id,
                topic=topic, status="OPEN", version=1, slots={},
                source_events=[comment_id], desired_outcomes=[outcome],
                created_at=now, updated_at=now)

def _pending_id(): return "q_" + str(uuid.uuid4())[:8]

class Engine(PersonaGenerationRuntime):
    HANDOFF_TTL_HOURS = HANDOFF_SLA_HOURS  # alias tương thích cũ (xem policy.py)
    def __init__(self, store: Store, facts: Facts, sender: FakeSender, clock: Clock,
                 kill_switch: bool = False, auto_send_generated: bool = False,
                 model_led_conversation: bool = False):
        self.store, self.facts, self.sender, self.clock = store, facts, sender, clock
        self.kill_switch = kill_switch
        self.dispatch_hook = None  # test hook: chạy giữa plan và dispatch
        # Worker id riêng cho mỗi Engine instance: claim lease (A5) phân biệt
        # worker nào đang giữ một intent CLAIMED.
        self.worker_id = uuid.uuid4().hex[:12]
        self.persona_store = None
        self.allow_persona_fixtures = False
        self.auto_send_generated = bool(auto_send_generated)
        self.model_led_conversation = bool(model_led_conversation)

    def _peer_plan(self, event, semantic):
        """Read-only: what this turn means given what the person already told us."""
        text = event.get('text', '')
        state = self.store.peer_state(event)
        pending = [p for p in self.store.open_pending_for(event['actor_id'], page_id=event['page_id'],
                   post_id=event['post_id'], thread_root_id=event['thread_root_id']) if p['qtype'] == 'open']
        query = parse_persona_questions(text, event.get('comment_id'), event.get('actor_id'), semantic=semantic)[0]
        social = bool(_social_reply(text)) and not peer_cue(text) and not is_decline(text)
        plan = plan_peer_turn(text, state, self.store.lead_profile(event), pending, event['comment_id'],
                              social=social, persona_question=query['topic'] != 'none')
        plan['recent'] = list(state.get('last_utterances') or [])
        return plan

    def _peer_reply(self, event, snapshot, query, plan=None):
        """Pick approved utterances that answer what was just said. Nothing fits -> REVIEW, not a generic line."""
        if plan is None:
            plan = dict(call=call_term(event.get('text', ''), self.store.peer_state(event).get('address_term')),
                        topic=None, hook_field=None, reason='', recent=[])
        call = plan['call']
        utterances = snapshot['utterances']
        facts = {f['fact_id']: f for f in snapshot['facts']}
        hook_field = None
        selected = []
        if query['topic'] != 'none':
            for question in query.get('questions', [query]):
                keys = set(question.get('canonical_keys') or [])
                if keys == {'purchase.year'}: keys = {'purchase.date'}
                if keys == {'purchase.finance_method'}: keys = {'purchase.down_payment_percent', 'purchase.loan_term_years'}
                candidates = [u for u in utterances if u.get('topic') == question['topic'] and keys
                              and {facts.get(f, {}).get('key') for f in u.get('required_fact_ids', [])} == keys]
                if question['topic'] == 'opinion':
                    mentioned = {name for word, name in VEHICLES.items() if re.search(r'\b' + re.escape(word) + r'\b', norm(question.get('evidence_excerpt') or event['text']))}
                    own = norm(next((f['value'] for f in facts.values() if f.get('key') == 'vehicle.current.model'), ''))
                    if mentioned and any(norm(name) not in own for name in mentioned):
                        candidates = [u for u in utterances if u.get('topic') == 'opinion_other_vehicle' and u.get('style') == call]
                        if not candidates:
                            return dict(status='REVIEW', reason='NO_MATCHING_VEHICLE_EXPERIENCE', query=query)
                if not candidates:
                    fallback = ('unknown_purchase_dealer' if keys == {'purchase.dealer'} else
                                'unknown_vehicle_color' if keys == {'vehicle.current.color'} else
                                'unknown_vehicle_opinion' if question['topic'] == 'opinion' else
                                'unknown_' + question['topic'])
                    candidates = [u for u in utterances if u.get('topic') == fallback and u.get('style') == call]
                if not candidates:
                    return dict(status='REVIEW', reason='NO_PEER_UTTERANCE', query=query)
                chosen = min(candidates, key=lambda u: u['utterance_id'])['utterance_id']
                if chosen not in selected:
                    selected.append(chosen)
            candidates = []
        else:
            topic = plan.get('topic')
            if not topic and plan.get('reason') == 'social':
                said = norm(event.get('text', ''))
                topic = social_reaction(event.get('text', '')) or (
                    'thanks' if 'cam on' in said else
                    'greeting' if any(w in said.split() for w in ('chao', 'hello', 'hi')) else 'ack')
            candidates = [u for u in utterances if topic and u.get('topic') == topic and u.get('style') == call]
            if topic == 'ack_interested_vehicle':
                vehicle = (self.store.lead_profile(event).get('interested_vehicle') or {}).get('value') or extract_answer('interested_vehicle', event.get('text', ''))
                vehicle = extract_answer('interested_vehicle', vehicle or '')
                stories = [u for u in utterances if vehicle and u.get('topic') == 'share_vehicle' and any(
                    facts.get(fid, {}).get('key') == 'vehicle.current.model'
                    and extract_answer('interested_vehicle', facts[fid]['value']) == vehicle
                    for fid in u.get('required_fact_ids', []))]
                candidates = stories or candidates
            hook_field = plan.get('hook_field')
        if not candidates and not selected:
            return dict(status='REVIEW', reason='NO_PEER_UTTERANCE', query=query)
        recent = set((plan.get('recent') or [])[-2:])  # what was said last turn: do not repeat it verbatim
        fresh = [u for u in candidates if u['utterance_id'] not in recent]
        pool = sorted(fresh or candidates, key=lambda u: u['utterance_id'])
        if not selected:
            selected = [pool[0]['utterance_id']]
        asked = None
        hook = next((u for u in utterances if hook_field and u.get('topic') == f'lead:{hook_field}:{call}'), None)
        if hook:
            selected.append(hook['utterance_id'])
            asked = hook_field
        result = render_persona_reply(snapshot, plan=dict(schema_version='persona_reply_plan_v1', utterance_ids=selected, hook_id=None),
            revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
            allow_fictional_fixture=self.allow_persona_fixtures)
        return {**result, 'query': query, 'persona_uuid': snapshot['persona_uuid'], 'release_id': snapshot['release_id'],
                'asked_field': asked if result.get('status') == 'OK' else None}

    def _apply_peer_plan(self, event, plan, persona, now_iso):
        """Commit what the person said (state, lead answers, closed questions) and any new question."""
        store = self.store
        ok = bool(persona) and persona.get('status') == 'OK'
        for pending, status in plan['supersede']:
            current = store.get_pending(pending['id']) or pending
            current['status'] = status; current['version'] += 1; store.update_pending(current)
            req = store.get_request(current['request_id'])
            if req and req['status'] not in ('COMPLETED', 'CANCELLED', 'HANDED_OFF'):
                req['status'] = 'COMPLETED'; req['version'] += 1; store.save_request(req)
        for field, value, status in plan['lead_updates']:
            if purchase_subject_correction(event['text']) or plan.get('reason') == 'semantic_other_person':
                old = store.lead_profile(event).get(field) or {}
                source = store.conn.execute('SELECT payload FROM events WHERE comment_id=? AND actor_id=?',
                    (old.get('source_message_id'), event['actor_id'])).fetchone()
                source_event = json.loads(source[0]) if source else {}
                if any(source_event.get(k) != event.get(k) for k in ('page_id','post_id','thread_root_id')):
                    continue
                store.audit(event['comment_id'], 'lead_subject_corrected', {'field': field, 'previous': old}, now_iso)
            store.save_lead_field(event, field, value, status, now_iso)
        state = dict(plan['next_state'])
        asked = persona.get('asked_field') if ok else None
        if asked:
            req = _new_req(event['page_id'], event['post_id'], event['thread_root_id'], event['actor_id'],
                           'social', 'lead_draft', event['comment_id'], now_iso)
            store.insert_request(req)
            self._ask(req, event, 'open', asked, [], asked, now_iso)
            store.save_lead_field(event, asked, None, 'ASKED', now_iso)
            state['asked_total'] = state.get('asked_total', 0) + 1
        if ok:
            state['last_utterances'] = (list(state.get('last_utterances') or []) + list(persona['utterance_ids']))[-6:]
        if state != store.peer_state(event):
            store.save_peer_state(event, state, now_iso)
        if plan['lead_updates'] or asked:
            self._outbox(event, [], 'lead_draft', '', 'NEEDS_REVIEW', now_iso,
                         payload=dict(profile=store.lead_profile(event), peer_state=state))

    def _persona_reply(self, event, postcard, semantic, plan=None):
        """Resolve a social question only against the release pinned to this post."""
        store = self.persona_store
        if not postcard.get("persona_binding") or store is None:
            return None
        questions = parse_persona_questions(event.get("text", ""), event.get("comment_id"),
                                             event.get("actor_id"), semantic=semantic)
        query = dict(questions[0], questions=questions) if len(questions) > 1 else questions[0]
        snapshot = store.get_binding_snapshot(event['page_id'], event.get('post_id'))
        if snapshot and (len(questions) > 1 or (snapshot.get('snapshot', {}).get('release', {}).get('style', {}).get('fixture_revision', 0) >= 3)):
            return self._peer_reply(event, snapshot, query, plan or self._peer_plan(event, semantic))
        if query["topic"] == "none":
            if "?" in event.get("text", "") or "？" in event.get("text", ""):
                return {"status": "REVIEW", "reason": "UNRESOLVED_QUERY", "query": query}
            return None
        snapshot = store.get_binding_snapshot(event["page_id"], event.get("post_id"))
        if not isinstance(snapshot, dict):
            return {"status": "REVIEW", "reason": "BINDING_NOT_FOUND", "query": query}
        required_keys = set(query.get("canonical_keys") or ())
        facts_by_id = {f.get("fact_id"): f for f in snapshot.get("facts", []) if isinstance(f, dict)}
        utterance_keys = {frozenset(facts_by_id.get(fid, {}).get("key")
                            for fid in u.get("required_fact_ids", []))
                          for u in snapshot.get("utterances", []) if isinstance(u, dict)}
        legacy_aliases = {
            frozenset({"purchase.year"}): frozenset({"purchase.date"}),
            frozenset({"purchase.date"}): frozenset({"purchase.year"}),
            frozenset({"purchase.finance_method"}): frozenset({
                "purchase.down_payment_percent", "purchase.loan_term_years"}),
        }
        legacy = frozenset(required_keys)
        current = legacy_aliases.get(legacy)
        month_question = "thang may" in norm(event.get("text", ""))
        if (current and legacy not in utterance_keys and current in utterance_keys
                and not (current == frozenset({"purchase.year"}) and month_question)):
            required_keys = set(current)
            query = {**query, "canonical_keys": sorted(required_keys)}
        filtered = dict(snapshot)
        filtered["utterances"] = [u for u in snapshot.get("utterances", []) if isinstance(u, dict) and
            required_keys and {facts_by_id.get(fid, {}).get("key") for fid in u.get("required_fact_ids", [])} == required_keys]
        result = render_persona_reply(filtered, topic=query["topic"],
            revoked_ids=store.revoked_fact_ids(snapshot["persona_uuid"], snapshot["release_id"]),
            allow_fictional_fixture=self.allow_persona_fixtures)
        if result["status"] == "UNKNOWN" and query["topic"] != "unknown":
            result = render_persona_reply(snapshot, topic="unknown",
                revoked_ids=store.revoked_fact_ids(snapshot["persona_uuid"], snapshot["release_id"]),
                allow_fictional_fixture=self.allow_persona_fixtures)
        return {**result, "query": query, "persona_uuid": snapshot.get("persona_uuid"),
                "release_id": snapshot.get("release_id")}

    def _persona_dispatch_precheck(self, payload):
        if "answer_persona" not in AUTO_ACTIONS:
            return "BLOCKED_POLICY"
        scope = payload.get("persona_scope")
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in ("persona_uuid", "release_id")):
            return "BLOCKED_PERSONA_SCOPE"
        if not isinstance(scope, dict) or any(not isinstance(scope.get(k), str) or not scope[k]
                for k in ("page_id", "post_id", "thread_root_id", "actor_id")):
            return "BLOCKED_PERSONA_SCOPE"
        row = self.store.conn.execute(
            "SELECT payload,retracted FROM events WHERE comment_id=? ORDER BY rowid DESC LIMIT 1",
            (payload.get("comment_id"),)).fetchone()
        try:
            source = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            return "BLOCKED_PERSONA_SCOPE"
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if (not row or not isinstance(source, dict) or row[1]
                or source.get("comment_id") != payload.get("comment_id")
                or source.get("event_type") not in ("create", "edit")
                or digest != payload.get("persona_event_token")
                or any(source.get(key) != value for key, value in scope.items())):
            return "BLOCKED_PERSONA_SCOPE"
        if self.kill_switch or self.store.blocks(scope["page_id"], scope["actor_id"]) & {"all", "public_reply"}:
            return "BLOCKED_AT_DISPATCH"
        handoff = self.store.conn.execute(
            "SELECT 1 FROM handoffs h JOIN requests r ON r.id=h.request_id WHERE h.released_at IS NULL "
            "AND r.page_id=? AND r.post_id=? AND r.thread_root_id=? AND r.actor_id=? LIMIT 1",
            (scope["page_id"], scope["post_id"], scope["thread_root_id"], scope["actor_id"])).fetchone()
        if handoff:
            return "BLOCKED_HANDOFF_AT_DISPATCH"
        state = self._trace_state_snapshot(scope)
        token = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if token != payload.get("persona_state_token"):
            return "BLOCKED_STALE_STATE"
        try:
            store = self.persona_store
            snapshot = store.get_binding_snapshot(scope["page_id"], scope["post_id"]) if store else None
            if not snapshot or any(snapshot.get(k) != payload.get(k) for k in ("persona_uuid", "release_id")):
                return "BLOCKED_STALE_PERSONA"
            if payload.get('reviewed_fallback') and payload.get('generation_state_token') != self._generation_state_digest(source):
                return 'BLOCKED_STALE_GENERATION_STATE'
            if payload.get('generated_candidate') is not None:
                return self._generated_dispatch_validation(payload, snapshot, source)
            result = render_persona_reply(snapshot, plan={"schema_version": "persona_reply_plan_v1",
                "utterance_ids": payload.get("utterance_ids"), "hook_id": None},
                revoked_ids=store.revoked_fact_ids(payload["persona_uuid"], payload["release_id"]),
                allow_fictional_fixture=self.allow_persona_fixtures)
            if payload.get('reviewed_fallback') and self._uses_story_facts(snapshot, result.get('fact_ids')):
                return 'BLOCKED_MEMORY_CITATION_REQUIRED'
            if (result.get("status") != "OK" or result.get("text") != payload.get("text") or
                    result.get("fact_ids") != payload.get("fact_ids")):
                return "BLOCKED_STALE_PERSONA"
        except Exception:
            return "BLOCKED_STALE_PERSONA"
        return None

    def _upgrade_legacy_persona_event_token(self, key, payload):
        """Bind a pre-token intent only when its comment has exactly one event row."""
        scope = payload.get("persona_scope")
        if not isinstance(scope, dict):
            return False
        rows = self.store.conn.execute(
            "SELECT payload,retracted FROM events WHERE comment_id=?",
            (payload.get("comment_id"),)).fetchall()
        if len(rows) != 1 or rows[0][1]:
            return False
        try:
            source = json.loads(rows[0][0])
        except (TypeError, ValueError):
            return False
        if (not isinstance(source, dict) or source.get("comment_id") != payload.get("comment_id")
                or source.get("event_type") != "create"
                or any(source.get(name) != value for name, value in scope.items())):
            return False
        payload["persona_event_token"] = hashlib.sha256(json.dumps(
            source, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        updated = self.store.conn.execute(
            "UPDATE outbox SET payload=? WHERE dedupe_key=? AND status='CLAIMED'",
            (json.dumps(payload, ensure_ascii=False), key))
        return updated.rowcount == 1
    def _sweep_expired(self, event, now_iso):
        """Hết hạn: pending quá expires_at -> EXPIRED (request về OPEN nếu
        không còn pending mở); handoff quá SLA -> OVERDUE (escalation nội bộ),
        KHÔNG tự release cho bot tiếp quản (R04). A3: sweep chỉ quét scope
        (page, actor) của event hiện tại — event Page B không được làm hết
        hạn pending của Page A. (Housekeeping toàn cục nếu cần phải là đường
        riêng, không nằm trong quyền của event hiện tại.)"""
        actor = event["actor_id"]; page = event["page_id"]
        now = datetime.fromisoformat(now_iso)
        for p in self.store.open_pending_for(actor, page_id=page,
                                              post_id=event.get("post_id"),
                                              thread_root_id=event.get("thread_root_id")):
            if p["expires_at"] and now_iso > p["expires_at"]:
                p["status"] = "EXPIRED"; p["version"] += 1; self.store.update_pending(p)
                req = self.store.get_request(p["request_id"])
                if req and req["status"] == "WAITING_USER" and not self.store.open_pending_for(
                        actor, req["id"], page_id=page, post_id=event.get("post_id"),
                        thread_root_id=event.get("thread_root_id")):
                    req["status"] = "OPEN"; req["version"] += 1; self.store.save_request(req)
        for active in self.store.active_requests(page, actor, event.get("post_id"),
                                                event.get("thread_root_id")):
            req = self.store.get_request(active["id"])
            if self._clear_past_testdrive_datetime(req, now):
                req["version"] += 1; self.store.save_request(req)
        rows = self.store.conn.execute(
            "SELECT h.request_id, h.acquired_at FROM handoffs h JOIN requests r ON r.id=h.request_id "
            "WHERE r.actor_id=? AND r.page_id=? AND r.post_id=? AND r.thread_root_id=? "
            "AND h.released_at IS NULL", (actor, page, event.get("post_id"),
                                             event.get("thread_root_id"))).fetchall()
        for rid, acq in rows:
            try: acq_dt = datetime.fromisoformat(acq)
            except Exception: continue
            if (now - acq_dt).total_seconds() > HANDOFF_SLA_HOURS * 3600:
                self.store.mark_handoff_overdue(rid)
                self.store.audit(event["comment_id"], "handoff_overdue", {"request_id": rid}, now_iso)
    def release_handoff(self, request_id, reason, now_iso=None):
        """Release handoff CHỈ khi có điều kiện rõ + audit lý do (R04).
        Hết SLA không tự gọi hàm này."""
        now_iso = now_iso or self.clock.now().isoformat()
        self.store.release_handoff(request_id, now_iso)
        self.store.audit(request_id, "handoff_released", {"request_id": request_id, "reason": reason}, now_iso)
        self.store.conn.commit()  # release là thao tác độc lập, phải durable ngay
    def reconcile_unknown(self):
        """Chốt các outbox UNKNOWN/CLAIMED bằng receiver ledger (R03/A5):
        - receiver có effect -> SENT (bằng chứng delivery).
        - CLAIMED, receiver không có effect, lease còn hiệu lực -> GIỮ
          CLAIMED (worker khác có thể còn đang gửi; không reset mù).
        - CLAIMED, receiver không có effect, lease đã hết -> INTENDED
          (send chưa từng bắt đầu hoặc worker đã chết, drain claim lại an toàn).
        - UNKNOWN nhưng receiver không có effect -> giữ UNKNOWN, chờ staff
          (không retry mù, không tự kết luận SENT)."""
        done = []
        now = self.clock.now()
        for row in self.store.conn.execute(
                "SELECT dedupe_key, status, claimed_at FROM outbox WHERE status IN ('UNKNOWN','CLAIMED')").fetchall():
            key, status, claimed_at = row[0], row[1], row[2]
            if self.sender.receiver.has_effect(key):
                saved = self.store.get_outbox(key)
                with self.store.lock, self._generation_delivery_transaction(saved['payload'] if saved else {}):
                    self.store.conn.execute("UPDATE outbox SET status='SENT' WHERE dedupe_key=?", (key,))
                    if saved:
                        self._record_generated_delivery(saved['payload'], now.isoformat())
                done.append((key, "SENT"))
            elif status == "CLAIMED":
                expired = True
                if claimed_at:
                    try:
                        expired = (now - datetime.fromisoformat(claimed_at)).total_seconds() > CLAIM_LEASE_SECONDS
                    except Exception:
                        expired = True
                if expired:
                    next_status = ("INTENDED" if getattr(self.sender, "retry_claimed_after_restart", True)
                                   else "UNKNOWN")
                    self.store.conn.execute("UPDATE outbox SET status=? WHERE dedupe_key=?",
                                            (next_status, key))
                    done.append((key, next_status))
                else:
                    done.append((key, "CLAIMED"))
            else:
                done.append((key, "UNKNOWN"))
        self.store.conn.commit()
        return done
    def drain_intended(self, now_iso=None):
        """Recovery worker (A5): xử lý lại các intent INTENDED còn tồn đọng
        và CLAIMED đã hết lease — tức là các intent đã commit nhưng dispatcher
        chưa gửi xong do crash trước/sau commit. Chạy khi: engine restart
        (server gọi rõ ràng), hoặc khi một event trùng lặp (redelivery) tới —
        vì redelivery KHÔNG được làm mất vĩnh viễn intent chỉ vì
        DUPLICATE_IGNORED. Mỗi intent: claim nguyên tử (kèm lease) -> kiểm
        version state (state đã đổi thì BLOCK, không gửi text cũ) -> gửi qua
        sender (receiver dedupe theo key nên không tạo effect thứ hai) ->
        SENT/UNKNOWN. UNKNOWN giữ nguyên (chờ reconcile bằng receiver
        evidence), không retry mù. Trả [(dedupe_key, outcome)]."""
        if not getattr(self.sender, "retry_intended", True):
            return []
        now_iso = now_iso or self.clock.now().isoformat()
        now = datetime.fromisoformat(now_iso)
        drained = []
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT dedupe_key, request_id, purpose, version, status, payload, claimed_at "
                "FROM outbox WHERE status IN ('INTENDED','CLAIMED') ORDER BY created_at").fetchall()
        for key, rid, purpose, ver, status, payload_json, claimed_at in rows:
            if status == "CLAIMED":
                if claimed_at:
                    try:
                        if (now - datetime.fromisoformat(claimed_at)).total_seconds() <= CLAIM_LEASE_SECONDS:
                            continue  # worker khác có thể còn đang gửi
                    except Exception:
                        pass
            with self.store.lock:
                if status == "INTENDED":
                    ok = self.store.claim_outbox(key, self.worker_id, now_iso)
                else:
                    ok = self.store.reclaim_outbox(key, self.worker_id, now_iso,
                                                   expected_claimed_at=claimed_at)
                self.store.conn.commit()
                if not ok:
                    continue
                req = self.store.get_request(rid) if rid else None
                if req is not None and req["version"] != ver:
                    self.store.set_outbox_status(key, "BLOCKED")
                    self.store.audit(key, "drain_blocked_stale_state",
                                     {"key": key, "row_version": ver,
                                      "current_version": req["version"]}, now_iso)
                    self.store.conn.commit()
                    drained.append((key, "BLOCKED_STALE_STATE")); continue
                try:
                    payload = json.loads(payload_json)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid outbox payload")
                except (TypeError, ValueError):
                    self.store.set_outbox_status(key, "BLOCKED")
                    self.store.audit(key, "drain_blocked", {"reason": "INVALID_PAYLOAD"}, now_iso)
                    self.store.conn.commit()
                    drained.append((key, "BLOCKED")); continue
                is_persona = purpose == "persona_reply" or any(k in payload for k in ("persona_uuid", "release_id", "persona_scope", "utterance_ids", "fact_ids"))
                if is_persona:
                    if not payload.get("persona_event_token"):
                        if not self._upgrade_legacy_persona_event_token(key, payload):
                            self.store.set_outbox_status(key, "NEEDS_REVIEW")
                            self.store.audit(key, "legacy_persona_intent_needs_review",
                                             {"key": key, "reason": "UNVERIFIABLE_EVENT_TOKEN"}, now_iso)
                            self.store.conn.commit()
                            drained.append((key, "NEEDS_REVIEW")); continue
                    err = self._persona_dispatch_precheck(payload)
                    if err is None:
                        err = self._reserve_memory_delivery(payload, now_iso)
                    if err is None:
                        err = self._start_memory_delivery(payload)
                    if err:
                        self.store.set_outbox_status(key, "BLOCKED")
                        self._discard_generated_delivery(payload, now_iso)
                        self.store.audit(key, "drain_blocked", {"reason": err}, now_iso)
                        self.store.conn.commit()
                        drained.append((key, "BLOCKED")); continue
                elif self.kill_switch:
                    self.store.set_outbox_status(key, "INTENDED")
                    self.store.conn.commit()
                    drained.append((key, "SKIPPED_KILL_SWITCH")); continue
            res = self.sender.send(payload)
            st = _delivery_status(res)
            with self.store.lock, self._generation_delivery_transaction(payload):
                cur = self.store.get_outbox(key)
                if cur and cur["status"] == "CLAIMED":
                    self.store.set_outbox_status(key, st)
                    if st == 'SENT':
                        self._record_generated_delivery(payload, now_iso)
                    elif st == 'FAILED':
                        self._discard_generated_delivery(payload, now_iso, delivery_status='FAILED')
                self.store.conn.commit()
            drained.append((key, st))
        return drained
    def _dispatch(self, event, touched, purpose, text, now_iso, payload, recheck=None):
        """Dispatcher (R03): claim INTENDED -> recheck -> send -> ghi kết quả.
        recheck: callable -> routing lỗi hoặc None (gọi SAU claim, TRƯỚC send).
        Trả (status, result): SENT / UNKNOWN / BLOCKED / SKIPPED.
        Send chạy NGOÀI transaction: crash tại đây để lại row CLAIMED +
        receiver effect (nếu có) -> reconcile_unknown() chốt bằng receiver."""
        key = payload["dedupe_key"]
        with self.store.lock:
            if self.store.get_outbox(key) is None:
                self.store.insert_outbox_intended(
                    key, touched[0]["id"] if touched else None, purpose, 1, payload, now_iso)
                self.store.conn.commit()
            claimed = self.store.claim_outbox(key, self.worker_id, now_iso)
            self.store.conn.commit()
            if not claimed:
                cur = self.store.get_outbox(key)
                if cur and cur["status"] == "SENT":
                    return "SENT", {"status": "sent_to_fake_ledger", "id": key, "duplicate_delivery": True}
                return "SKIPPED", None
            is_persona = purpose == "persona_reply" or any(k in payload for k in ("persona_uuid", "release_id", "persona_scope", "utterance_ids", "fact_ids"))
            if recheck is not None or is_persona:
                err = self._persona_dispatch_precheck(payload) if is_persona else None
                err = err or (recheck() if recheck is not None else None)
                if err is None and is_persona:
                    err = self._reserve_memory_delivery(payload, now_iso)
                if err is None and is_persona:
                    err = self._start_memory_delivery(payload)
                if err:
                    self.store.set_outbox_status(key, "BLOCKED")
                    self._discard_generated_delivery(payload, now_iso)
                    self.store.audit(event["comment_id"], "dispatch_blocked", {"key": key, "reason": err}, now_iso)
                    self.store.conn.commit()
                    return "BLOCKED", None
        res = self.sender.send(payload)
        status = _delivery_status(res)
        with self.store.lock, self._generation_delivery_transaction(payload):
            cur = self.store.get_outbox(key)
            if cur and cur["status"] == "CLAIMED":
                self.store.set_outbox_status(key, status)
                if status == 'SENT':
                    self._record_generated_delivery(payload, now_iso)
                elif status == 'FAILED':
                    self._discard_generated_delivery(payload, now_iso, delivery_status='FAILED')
            self.store.conn.commit()
        return status, res
    def _send_reply(self, event, touched, purpose, text, now_iso, payload):
        """Tương thích cũ: gửi qua dispatcher (claim -> recheck nhẹ -> send)."""
        return self._dispatch(event, touched, purpose, text, now_iso, payload)
    def _dispatch_precheck(self, page, actor, safe, plan_versions, plan_facts_version, plan_used, now_iso):
        """Recheck trước dispatch (dùng cả trước commit và ở dispatcher):
        trả routing lỗi hoặc None. Freshness kiểm tra lại bằng clock tại
        dispatch, không chỉ facts.version (R07)."""
        if self.kill_switch or (self.store.blocks(page, actor) & {"all", "public_reply"}):
            return "BLOCKED_AT_DISPATCH"
        if self.facts.version != plan_facts_version:
            return "BLOCKED_STALE_FACTS"
        for rid, v in plan_versions.items():
            cur = self.store.get_request(rid)
            if (cur or {}).get("version") != v:
                return "BLOCKED_STALE_STATE"
        if any(self.store.handoff(r["id"]) for (r, _, _) in safe):
            return "BLOCKED_HANDOFF_AT_DISPATCH"
        now = datetime.fromisoformat(now_iso)
        is_fresh = getattr(self.facts, "is_fresh", None)
        if is_fresh is not None:
            for u in plan_used:
                fid = u.get("fact_id")
                if not fid or fid.startswith("calc:"): continue
                f = next((x for x in self.facts.facts if x.get("id") == fid), None)
                if f is None:
                    return "BLOCKED_STALE_FACTS"
                if f.get("source") == "local_dealer_directory":
                    # This directory has no verification timestamp; dispatch
                    # checks the immutable facts snapshot/version above only.
                    continue
                if not is_fresh(f, now): return "BLOCKED_STALE_FACTS"
        return None
    # ---------- context ----------
    def context(self, event, postcard, external_messages=None):
        # Requests/pending/recent are private to page + post + thread + actor.
        # Root and direct parent are shared context, with their authors kept.
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT text, comment_id, actor_id, payload FROM events WHERE retracted=0 AND actor_id=? AND "
                "json_extract(payload, '$.page_id')=? AND json_extract(payload, '$.post_id')=? "
                "AND json_extract(payload, '$.thread_root_id')=? AND comment_id<>? ORDER BY rowid DESC LIMIT 5",
                (event["actor_id"], event["page_id"], event.get("post_id"),
                 event.get("thread_root_id"), event.get("comment_id"))).fetchall()
            recent = []
            for row in rows:
                if row[1] in {event.get("comment_id"), event.get("thread_root_id"), event.get("parent_id")}:
                    continue
                try: payload = json.loads(row[3] or "{}")
                except (TypeError, ValueError): payload = {}
                recent.append({"comment_id": row[1], "message_id": row[1], "actor_id": row[2],
                    "role": "recent", "text": row[0] or "", "parent_id": payload.get("parent_id"),
                    "event_time": payload.get("event_time")})
            by_id = {}
            for key, role in (("thread_root_id", "root"), ("parent_id", "parent")):
                mid = event.get(key)
                if mid in by_id and role == "parent" and by_id[mid].get("role") == "root":
                    by_id[mid]["role"] = "root_parent"
                    continue
                if not mid or mid in (event.get("post_id"), event.get("comment_id")) or mid in by_id:
                    continue
                pr = self.store.conn.execute(
                    "SELECT text, actor_id, payload FROM events WHERE comment_id=? AND retracted=0 ORDER BY rowid DESC LIMIT 1",
                    (mid,)).fetchone()
                if not pr: continue
                try: payload = json.loads(pr[2] or "{}")
                except (TypeError, ValueError): payload = {}
                if (payload.get("page_id") == event["page_id"] and
                        payload.get("post_id") == event.get("post_id") and
                        payload.get("thread_root_id") == event.get("thread_root_id")):
                    by_id[mid] = {"message_id": mid, "actor_id": pr[1], "role": role,
                                  "text": pr[0] or "", "parent_id": payload.get("parent_id"),
                                  "event_time": payload.get("event_time")}
            allowed_shared = {event.get("thread_root_id"), event.get("parent_id")}
            for message in external_messages or []:
                if (not isinstance(message, dict) or message.get("message_id") not in allowed_shared or
                        message.get("message_id") in (event.get("post_id"), event.get("comment_id")) or
                        message.get("page_id", event.get("page_id")) != event.get("page_id") or
                        message.get("post_id", event.get("post_id")) != event.get("post_id") or
                        message.get("thread_root_id", event.get("thread_root_id")) != event.get("thread_root_id") or
                        not isinstance(message.get("text"), str)):
                    continue
                if message["message_id"] in by_id:
                    if (message.get("role") == "root" and
                            message["message_id"] == event.get("parent_id")):
                        by_id[message["message_id"]]["role"] = "root"
                    semantic = message.get("semantic")
                    if (isinstance(semantic, dict) and
                            not validate_semantic(semantic, message["text"], [])):
                        by_id[message["message_id"]]["semantic"] = semantic
                    continue
                item = {"message_id": message["message_id"],
                    "actor_id": message.get("actor_id"), "role": message.get("role") or
                    ("root" if message["message_id"] == event.get("thread_root_id") else "parent"),
                    "text": message["text"], "parent_id": message.get("parent_id"),
                    "event_time": message.get("event_time")}
                if isinstance(message.get("semantic"), dict):
                    item["semantic"] = message["semantic"]
                by_id[message["message_id"]] = item
            shared = [dict(message_id=event.get("post_id"), actor_id=postcard.get("page_id"),
                           role="post", text=postcard.get("text") or "")]
            shared.extend(by_id.values())
            if all(x.get("message_id") != event.get("comment_id") for x in shared):
                shared.append(dict(message_id=event.get("comment_id"), actor_id=event.get("actor_id"),
                                   role="current", text=event.get("text") or "",
                                   parent_id=event.get("parent_id"), event_time=event.get("event_time")))
            active = self.store.active_requests(event["page_id"], event["actor_id"],
                                                event.get("post_id"), event.get("thread_root_id"))
            state_sources = []
            seen_sources = set()
            for request in active:
                for name, slot in (request.get("slots") or {}).items():
                    source_id = slot.get("source_message_id") if isinstance(slot, dict) else None
                    if not source_id or source_id in seen_sources:
                        continue
                    seen_sources.add(source_id)
                    row = self.store.conn.execute(
                        "SELECT text,actor_id,payload FROM events WHERE comment_id=? AND actor_id=? "
                        "AND retracted=0 ORDER BY rowid DESC LIMIT 1", (source_id, event["actor_id"])).fetchone()
                    if not row:
                        continue
                    try: payload = json.loads(row[2] or "{}")
                    except (TypeError, ValueError): payload = {}
                    if (payload.get("page_id") == event["page_id"] and
                            payload.get("post_id") == event.get("post_id") and
                            payload.get("thread_root_id") == event.get("thread_root_id")):
                        state_sources.append({"message_id": source_id, "actor_id": row[1],
                            "role": "state_source", "text": row[0] or "",
                            "request_id": request["id"], "slot_kind": name,
                            "slot_value": slot.get("value")})
            pend = []
            for r in active:
                pend += self.store.open_pending_for(
                    event["actor_id"], r["id"], page_id=event["page_id"],
                    post_id=event.get("post_id"), thread_root_id=event.get("thread_root_id"))
            contact_blocks = sorted(self.store.blocks(event["page_id"], event["actor_id"]))
        parent_texts = [x["text"] for x in shared if x["role"] in ("root", "parent", "root_parent")]
        snapshot = {"scope": [event.get(k) for k in
                              ("page_id", "post_id", "thread_root_id", "actor_id")],
                    "postcard": postcard, "shared": shared, "recent": recent,
                    "state_sources": state_sources,
                    "requests": [{k: r.get(k) for k in
                                  ("id", "version", "topic", "status", "slots", "desired_outcomes")}
                                 for r in active],
                    "contact_blocks": contact_blocks,
                    "pending": [{k: p.get(k) for k in
                                 ("id", "request_id", "version", "status", "qtype", "expected_slot",
                                  "options", "proposition", "basis_version", "basis_slots")}
                                for p in pend]}
        state_token = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                                default=str, separators=(",", ":")).encode()).hexdigest()
        return dict(event=event, postcard=postcard, messages=shared, recent=recent,
                    parent_texts=parent_texts, active_requests=active, pending=pend,
                    contact_blocks=contact_blocks,
                    state_sources=state_sources, state_token=state_token)

    def _trace_state_snapshot(self, event):
        scope = (event["page_id"], event.get("post_id"), event.get("thread_root_id"), event["actor_id"])
        rows = self.store.conn.execute(
            "SELECT id,topic,status,version,slots,desired_outcomes FROM requests "
            "WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?",
            scope).fetchall()
        requests = {r[0]: {"topic": r[1], "status": r[2], "version": r[3],
                           "slots": json.loads(r[4] or "{}"),
                           "desired_outcomes": json.loads(r[5] or "[]")} for r in rows}
        rows = self.store.conn.execute(
            "SELECT p.id,p.request_id,p.status,p.version,p.proposition,p.expected_slot,p.options "
            "FROM pending p JOIN requests r ON r.id=p.request_id "
            "WHERE r.page_id=? AND r.post_id=? AND r.thread_root_id=? AND p.actor_id=?",
            scope).fetchall()
        pending = {r[0]: {"request_id": r[1], "status": r[2], "version": r[3],
                          "proposition": r[4], "expected_slot": r[5],
                          "options": json.loads(r[6] or "[]")} for r in rows}
        return {"requests": requests, "pending": pending,
                "contact_blocks": sorted(self.store.blocks(event["page_id"], event["actor_id"]))}

    @staticmethod
    def _trace_state_diff(before, after):
        if not before or not after:
            return []
        changes = []
        old_requests, new_requests = before["requests"], after["requests"]
        for rid in sorted(new_requests.keys() - old_requests.keys()):
            req = new_requests[rid]
            changes.append({"kind": "request_created", "request_id": rid,
                            "topic": req["topic"], "status": req["status"], "slots": req["slots"],
                            "desired_outcomes": req["desired_outcomes"]})
        for rid in sorted(old_requests.keys() & new_requests.keys()):
            old, new = old_requests[rid], new_requests[rid]
            if old["status"] != new["status"]:
                changes.append({"kind": "request_status", "request_id": rid,
                                "from": old["status"], "to": new["status"]})
            if old["desired_outcomes"] != new["desired_outcomes"]:
                changes.append({"kind": "request_outcomes", "request_id": rid,
                                "from": old["desired_outcomes"], "to": new["desired_outcomes"]})
            for name in sorted(old["slots"].keys() | new["slots"].keys()):
                if old["slots"].get(name) != new["slots"].get(name):
                    changes.append({"kind": "slot_changed", "request_id": rid, "slot": name,
                                    "from": old["slots"].get(name), "to": new["slots"].get(name)})
        for rid in sorted(old_requests.keys() - new_requests.keys()):
            changes.append({"kind": "request_removed", "request_id": rid,
                            "topic": old_requests[rid]["topic"]})
        old_pending, new_pending = before["pending"], after["pending"]
        if before.get("contact_blocks") != after.get("contact_blocks"):
            changes.append({"kind": "contact_blocks_changed", "from": before.get("contact_blocks", []),
                            "to": after.get("contact_blocks", [])})
        for pid in sorted(new_pending.keys() - old_pending.keys()):
            changes.append({"kind": "pending_created", "pending_id": pid,
                            **new_pending[pid]})
        for pid in sorted(old_pending.keys() & new_pending.keys()):
            if old_pending[pid] != new_pending[pid]:
                changes.append({"kind": "pending_changed", "pending_id": pid,
                                "from": old_pending[pid], "to": new_pending[pid]})
        for pid in sorted(old_pending.keys() - new_pending.keys()):
            changes.append({"kind": "pending_removed", "pending_id": pid})
        return changes

    # ---------- main ----------
    def process(self, event, postcard, semantic, now_dt=None, trusted_context_texts=None,
                trusted_source_ids=None, expected_state_token=None,
                trusted_context_messages=None, attempt_token=None, attempt_result_callback=None,
                reply_candidate=None, generation_context=None, peer_signals=None):
        peer_signals = validated_peer_signals(peer_signals, event.get('text', ''))
        if (postcard.get('persona_binding') and isinstance(semantic, dict)
                and semantic.get('scope') == 'in_scope' and semantic.get('sarcasm') == 'absent'
                and peer_signals is None and peer_cue(event.get('text', ''))
                and all(r.get('topic') in ('social', 'buy') for r in semantic.get('requests', []))
                and all(o.get('op') in ('START_REQUEST', 'UPDATE_SLOT', 'SOCIAL_ACK') for o in semantic.get('operations', []))):
            semantic = dict(semantic, requests=[dict(topic='social', desired_outcome='ack', polarity='positive')],
                operations=[dict(op='SOCIAL_ACK', evidence_excerpt=event['text'])])
            if 'intent_labels' in semantic: semantic.update(intent_labels=['social'], category='social')
        trusted_source_ids = dict(trusted_source_ids or {})
        if event.get("post_id") and (postcard.get("text") or postcard.get("vehicles")):
            post_source = trusted_source_ids.get(event["post_id"])
            if not isinstance(post_source, dict):
                post_source = {"text": post_source if isinstance(post_source, str) else ""}
                trusted_source_ids[event["post_id"]] = post_source
            post_source.update(text=postcard.get("text", ""),
                               actor_id=postcard.get("page_id") or event["page_id"],
                               role="post", vehicles=list(postcard.get("vehicles") or []))
        now = now_dt or self.clock.now()
        now_iso = now.isoformat()
        page, actor = event["page_id"], event["actor_id"]
        result = dict(comment_id=event["comment_id"], routing=[], outbound_permission=False,
                      sends=[], state_changes=[], response_plan=None, response_text=None,
                      errors=[], audit=[], semantic=None, semantic_version=None,
                      intent_labels=[], category=None, tags=[], state_after=None)
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            trace_before = None
            state_trace_committed = False
            try:
                self.store.lock_scope(event)
                if attempt_token:
                    self.store.assert_intent_lease(event["event_dedupe_key"], attempt_token)
                def commit_attempt():
                    nonlocal state_trace_committed
                    if attempt_token:
                        self.store.assert_intent_lease(event["event_dedupe_key"], attempt_token)
                    if attempt_result_callback:
                        after = self._trace_state_snapshot(event)
                        result["state_after"] = after
                        result["state_changes"] = (result["state_changes"] +
                                                   self._trace_state_diff(trace_before, after))
                        attempt_result_callback(result)
                    self.store.conn.commit()
                    state_trace_committed = bool(attempt_result_callback)
                trace_before = self._trace_state_snapshot(event)
                dedupe = event.get("event_dedupe_key")
                if not dedupe:
                    if event.get("event_type") in ("edit", "delete"):
                        version = event.get("content_version")
                        if version is None:
                            version = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                        dedupe = f"{event['comment_id']}:{event['event_type']}:{version}"
                    else:
                        dedupe = f"{event['comment_id']}:{event.get('content_version', 1)}"
                inserted = self.store.insert_event(dedupe, event, actor)
                if inserted.rowcount == 0:
                    result["routing"].append("DUPLICATE_IGNORED"); commit_attempt()
                    # A5: redelivery KHÔNG được làm mất intent đã commit nhưng
                    # chưa dispatch (crash trước dispatcher). Drain các intent
                    # tồn đọng ngay tại đây (claim nguyên tử nên an toàn dù
                    # nhiều worker cùng thấy event trùng). Kết quả drain ghi
                    # riêng, không đổi routing DUPLICATE_IGNORED.
                    try:
                        result["drained"] = self.drain_intended(now_iso)
                    except Exception as e:
                        result["drain_error"] = str(e)
                    return result
                # edit/delete sau khi đã reply -> REVIEW, không tự gửi lại
                if event.get("event_type") in ("edit", "delete"):
                    if event["event_type"] == "delete":
                        self.store.conn.execute(
                            "UPDATE events SET retracted=1 WHERE comment_id=? AND json_extract(payload, '$.page_id')=?",
                            (event["comment_id"], page))
                    sent_before = self.store.conn.execute(
                        "SELECT 1 FROM outbox WHERE json_extract(payload, '$.comment_id')=? "
                        "AND status IN ('SENT','CLAIMED','UNKNOWN','NEEDS_REVIEW')",
                        (event["comment_id"],)).fetchone()
                    if sent_before:
                        result["routing"].append("REVIEW_EDIT_AFTER_REPLY" if event["event_type"] == "edit" else "RETRACTED")
                        self.store.audit(event["comment_id"], "edit_delete_after_reply", result, now_iso)
                        commit_attempt(); return result
                    if event["event_type"] == "delete":
                        result["routing"].append("RETRACTED"); commit_attempt(); return result
                if event.get("actor_role") == "page_self":
                    result["routing"].append("PAGE_SELF_IGNORED"); commit_attempt(); return result
                # A narrowly matched persona question can be misclassified as out of scope.
                if (postcard.get('persona_binding') and self.allow_persona_fixtures and self.persona_store
                        and isinstance(semantic, dict) and semantic.get('scope') == 'out_of_scope'
                        and semantic.get('sarcasm') == 'absent' and not semantic.get('uncertainties')
                        and not semantic.get('requests') and len(semantic.get('operations', [])) == 1
                        and semantic['operations'][0].get('op') == 'UNRESOLVED'
                        and semantic['operations'][0].get('evidence_excerpt') == event.get('text')
                        and semantic['operations'][0].get('source_message_id') in (None, event.get('comment_id'))
                        and re.fullmatch(r'(?:ban|bac|chu|co|chi|anh|ad) lam nghe gi[?!. ]*', norm(event.get('text', '')))
                        and not validate_semantic(semantic, event.get('text', ''), [])):
                    binding = self.persona_store.get_binding_snapshot(page, event.get('post_id'))
                    if binding:
                        self.store.audit(event['comment_id'], 'persona_scope_override', {'original_scope': 'out_of_scope'}, now_iso)
                        semantic = dict(semantic, scope='in_scope', requests=[dict(topic='social', desired_outcome='persona_question', polarity='unknown')],
                                        operations=[dict(op='SOCIAL_ACK', evidence_excerpt=event['text'])])
                        if 'intent_labels' in semantic: semantic.update(intent_labels=['social'], category='social')
                if isinstance(semantic, dict) and semantic.get("scope") in ("out_of_scope", "unknown"):
                    scope_errors = validate_semantic(semantic, event.get("text", ""), [])
                    if scope_errors:
                        result["errors"] = scope_errors
                        result["routing"].append("REVIEW_SCHEMA")
                        self.store.audit(event["comment_id"], "schema_error", result, now_iso)
                    else:
                        result["semantic"] = semantic
                        result["semantic_version"] = semantic["schema_version"]
                        result["intent_labels"] = list(dict.fromkeys(
                            semantic.get("intent_labels") or [r["topic"] for r in semantic.get("requests", [])]))
                        result["category"] = semantic.get("category")
                        result["tags"] = list(semantic.get("tags") or [])
                        result["routing"].append("REVIEW_SCOPE")
                        self.store.audit(event["comment_id"], "scope_review", result, now_iso)
                    commit_attempt(); return result
                self._sweep_expired(event, now_iso)
                ctx = self.context(event, postcard, trusted_context_messages)
                if event.get("post_id") in trusted_source_ids:
                    trusted_source_ids[event["post_id"]]["state_token"] = ctx["state_token"]
                if semantic is None:
                    result["routing"].append("REVIEW_NO_SEMANTIC")
                    self.store.audit(event["comment_id"], "no_semantic", result, now_iso)
                    commit_attempt(); return result
                if expected_state_token is not None and expected_state_token != ctx["state_token"]:
                    result["routing"].append("REVIEW_STALE_CONTEXT")
                    self.store.audit(event["comment_id"], "stale_context", result, now_iso)
                    # Keep the API inbox event durable, but let this engine event
                    # be retried with a fresh snapshot and the same idempotency key.
                    self.store.conn.execute("DELETE FROM events WHERE dedupe_key=?", (dedupe,))
                    commit_attempt(); return result
                semantic = attach_context_provenance(semantic, event, ctx, postcard)
                for message in [*ctx["messages"], *ctx["recent"], *ctx.get("state_sources", [])]:
                    mid = message.get("message_id")
                    if mid:
                        source = trusted_source_ids.get(mid)
                        if isinstance(source, str):
                            source = {"text": source}
                            trusted_source_ids[mid] = source
                        if isinstance(source, dict):
                            source.setdefault("text", message.get("text", ""))
                            if message.get("actor_id") is not None:
                                source["actor_id"] = message["actor_id"]
                            source.update(state_token=ctx["state_token"])
                            if message.get("role"):
                                source["role"] = message["role"]
                        else:
                            trusted_source_ids[mid] = {"text": message.get("text", ""),
                                "actor_id": message.get("actor_id"),
                                "state_token": ctx["state_token"], "role": message.get("role")}
                ctx_texts = [postcard.get("text", "")] + [r["text"] for r in ctx["recent"]] + ctx.get("parent_texts", [])
                ctx_texts += [m["text"] for m in ctx.get("state_sources", [])]
                if isinstance(trusted_context_texts, (list, tuple)):
                    ctx_texts += [text for text in trusted_context_texts
                                  if isinstance(text, str) and text.strip() and len(text) <= 20000]
                errs = validate_semantic(semantic, event.get("text", ""), ctx_texts)
                if errs:
                    result["errors"] = errs; result["routing"].append("REVIEW_SCHEMA")
                    self.store.audit(event["comment_id"], "schema_error", result, now_iso)
                    commit_attempt(); return result
                result["semantic"] = semantic
                result["semantic_version"] = semantic["schema_version"]
                result["intent_labels"] = list(dict.fromkeys(
                    semantic.get("intent_labels") or [r["topic"] for r in semantic.get("requests", [])]))
                result["category"] = semantic.get("category")
                result["tags"] = list(semantic.get("tags") or [])
                # Gate kill-switch (khẩn cấp: chặn cả pref phase).
                if self.kill_switch:
                    result["routing"].append("BLOCKED_KILL_SWITCH")
                    self.store.audit(event["comment_id"], "blocked", result, now_iso)
                    commit_attempt(); return result
                # Pref pre-phase (R08/A1b): CHỈ block (protective opt-out)
                # được áp trước opt-out gate; unblock/allow_all là MỞ RỘNG
                # quyền nên bị hoãn lại tới sau risk gates (xem allow phase
                # dưới). Xung đột pref trong cùng lượt -> REVIEW, không áp
                # pref nào.
                main_ops, pref_err, deferred_allows = self._apply_pref_phase(event, semantic, result, now_iso)
                if pref_err:
                    result["routing"].append(pref_err)
                    self.store.audit(event["comment_id"], "review", result, now_iso)
                    commit_attempt(); return result
                # Opt-out gates trên blocks HIỆU LỰC: blocks hiện tại trừ các
                # kênh mà lượt này xin mở lại. Nhờ vậy "allow_all khi đang bị
                # chặn" vẫn qua gate được, nhưng "allow_call khi đang bị chặn
                # all" vẫn bị chặn (allow_call không gỡ được block all).
                blocks = self.store.blocks(page, actor)
                eff_blocks = set(blocks)
                for ch, act in deferred_allows:
                    if act == "clear":
                        eff_blocks = set()
                    elif act == "unblock":
                        eff_blocks.discard(ch)
                if "all" in eff_blocks:
                    result["routing"].append("BLOCKED_OPT_OUT_ALL")
                    self.store.audit(event["comment_id"], "blocked", result, now_iso)
                    commit_attempt(); return result
                if "public_reply" in eff_blocks:
                    result["routing"].append("BLOCKED_OPT_OUT_PUBLIC")
                    self.store.audit(event["comment_id"], "blocked", result, now_iso)
                    commit_attempt(); return result
                # Validation phase (R01/R09/A4/A6): pure reads, CHƯA ghi
                # business state. Trả thêm: indices của ops bị HANDOFF-block
                # (được tách khỏi main_ops để phần còn lại vẫn xử lý được)
                # và canonical source cho từng op.
                if postcard.get('persona_binding') and self.persona_store and len(main_ops) == 1:
                    binding = self.persona_store.get_binding_snapshot(page, event.get('post_id'))
                    fixture = (binding or {}).get('source_kind') == 'fictional_fixture' and self.allow_persona_fixtures
                    op = main_ops[0]
                    excerpt = op.get('evidence_excerpt') or ''
                    source = op.get('source_message_id')
                    pending = [p for p in self.store.open_pending_for(actor, page_id=page,
                        post_id=event.get('post_id'), thread_root_id=event.get('thread_root_id')) if p['qtype'] == 'open']
                    correction = op.get('op') == 'UNRESOLVED' and (purchase_subject_correction(event.get('text', '')) or proxy_clarification(event.get('text', '')))
                    decline = (op.get('op') == 'CANCEL_REQUEST' and op.get('request_ref') == 'topic:social'
                        and len(pending) == 1 and is_decline(event.get('text', '')))
                    unresolved_decline = op.get('op') == 'UNRESOLVED' and len(pending) <= 1 and is_decline(event.get('text', ''))
                    unresolved_ownership = (op.get('op') == 'UNRESOLVED' and len(pending) <= 1
                        and states_ownership(event.get('text', '')) and peer_cue(event.get('text', '')))
                    volunteered = (op.get('op') == 'UPDATE_SLOT' and op.get('request_ref') == 'topic:social'
                        and len(pending) == 1 and pending[0]['expected_slot'] != 'purchase_timeline'
                        and (op.get('slot') or {}).get('kind') == 'purchase_timeline'
                        and extract_answer('purchase_timeline', event.get('text', '')) is not None)
                    if (fixture and (correction or decline or unresolved_decline or unresolved_ownership or volunteered) and excerpt and excerpt in event.get('text', '')
                            and source in (None, event.get('comment_id'))
                            and all(r.get('topic') == 'social' for r in semantic.get('requests', []))):
                        main_ops = [dict(op='SOCIAL_ACK', evidence_excerpt=excerpt, source_message_id=source)]
                verr, ambig, blocked_idx, canon = self._validate_ops(
                    event, postcard, semantic, main_ops, now_iso, trusted_source_ids)
                if ambig is not None:
                    return self._disambiguate(event, postcard, ambig, result, now_iso)
                if verr:
                    result["routing"].append(verr)
                    self.store.audit(event["comment_id"], "review", result, now_iso)
                    commit_attempt(); return result
                main_ops_eff = [o for i, o in enumerate(main_ops) if i not in blocked_idx]
                if blocked_idx:
                    result["routing"].append("BLOCKED_HANDOFF")
                if blocked_idx and not main_ops_eff:
                    # Mọi op business đều bị HANDOFF-block: không gửi, không
                    # tạo state mới (ngoài ingest + sweep + pref đã ghi trong
                    # transaction này); giữ routing BLOCKED để staff thấy.
                    # (Lượt chỉ có pref op thì main_ops rỗng nhưng blocked_idx
                    # cũng rỗng nên không vào nhánh này — vẫn đi tiếp tới
                    # allow phase và processed_no_reply.)
                    self.store.audit(event["comment_id"], "blocked", result, now_iso)
                    commit_attempt(); return result
                # Risk gate TRƯỚC apply (R02): sarcasm = meaning không tin cậy
                # -> không commit business gì từ lượt này.
                if semantic.get("sarcasm") in ("present", "uncertain"):
                    result["routing"].append("REVIEW_SARCASM")
                    self.store.audit(event["comment_id"], "sarcasm_review", result, now_iso)
                    commit_attempt(); return result
                # Business savepoint (R02): mọi ghi business state nằm trong
                # này; REVIEW/BLOCKED sau apply/plan -> rollback, chỉ giữ
                # ingest + sweep + pref + audit.
                # A1: snapshot request ĐÃ TỒN TẠI trước lượt này (slots +
                # status) để ambiguity gate khôi phục lại nếu lượt mơ hồ đã
                # chạm vào chúng — request MỚI tạo trong lượt mơ hồ thì giữ
                # làm placeholder OPEN cho câu hỏi làm rõ tham chiếu tới.
                pre_snapshot = {}
                for _r in self.store.active_requests(page, actor, event.get("post_id"),
                                                     event.get("thread_root_id")):
                    pre_snapshot[_r["id"]] = (json.loads(json.dumps(_r["slots"], ensure_ascii=False)),
                                              _r["status"])
                self.store.conn.execute("SAVEPOINT business")
                canon_eff = [c for i, c in enumerate(canon) if i not in blocked_idx]
                touched, per_req = self._apply_ops(event, postcard, semantic, main_ops_eff, ctx, result, now_iso, canon_eff)
                # Lỗi fatal (bad ref/owner/schema/pending...) vẫn rollback toàn
                # bộ như R02. Riêng BLOCKED_HANDOFF là skip từng phần (A4):
                # phần bị khóa bỏ qua, phần còn lại vẫn xử lý được.
                fatal = [r for r in result["routing"]
                         if r.startswith(("REVIEW", "BLOCKED")) and r != "BLOCKED_HANDOFF"]
                if fatal:
                    self.store.conn.execute("ROLLBACK TO SAVEPOINT business")
                    self.store.conn.execute("RELEASE SAVEPOINT business")
                    self.store.audit(event["comment_id"], "review", result, now_iso)
                    commit_attempt(); return result
                # Ambiguity gate SAU apply (A1, thay quy tắc session 3):
                # lượt mơ hồ KHÔNG gửi gì. Request MỚI trong lượt vẫn giữ làm
                # placeholder OPEN (VD conditional_stock_then_book). Nhưng
                # slot/status của request ĐÃ TỒN TẠI mà lượt mơ hồ đã đổi thì
                # KHÔI PHỤC về snapshot trước lượt (tentative, không phải
                # accepted) và SUPERSEDE pending yes/no gắn với proposition cũ
                # — nếu không, khách sẽ xác nhận proposition Hà Nội trong khi
                # request thực tế đã mang TP.HCM. Lượt có op phá hoại
                # (CANCEL/CONFIRM/REJECT) thì rollback toàn bộ như trước.
                if any(isinstance(u, dict) and u.get("different_action")
                       for u in semantic.get("uncertainties") or []):
                    result["routing"].append("REVIEW_AMBIGUOUS")
                    destructive = any(isinstance(o, dict) and o.get("op") in
                                      ("CANCEL_REQUEST", "CONFIRM_PENDING", "REJECT_PENDING")
                                      for o in main_ops)
                    if destructive:
                        self.store.conn.execute("ROLLBACK TO SAVEPOINT business")
                    else:
                        for _req in touched:
                            if _req["id"] in pre_snapshot:
                                _old_slots, _old_status = pre_snapshot[_req["id"]]
                                _cur = self.store.get_request(_req["id"])
                                if _cur and (json.dumps(_cur["slots"], ensure_ascii=False, sort_keys=True)
                                            != json.dumps(_old_slots, ensure_ascii=False, sort_keys=True)
                                            or _cur["status"] != _old_status):
                                    _cur["slots"] = _old_slots; _cur["status"] = _old_status
                                    _cur["version"] += 1; self.store.save_request(_cur)
                                    for _p in self.store.open_pending_for(
                                            actor, _req["id"], page_id=page,
                                            post_id=event.get("post_id"),
                                            thread_root_id=event.get("thread_root_id")):
                                        if _p["qtype"] == "yes_no":
                                            _p["status"] = "SUPERSEDED"; _p["version"] += 1
                                            self.store.update_pending(_p)
                                    self.store.audit(event["comment_id"], "ambiguous_restore",
                                                     {"request_id": _req["id"]}, now_iso)
                        self.store.conn.execute("RELEASE SAVEPOINT business")
                    self.store.audit(event["comment_id"], "ambiguous_review", result, now_iso)
                    commit_attempt(); return result
                # Allow phase (A1b): chỉ tới đây (đã qua validation + sarcasm
                # gate + ambiguity gate) thì unblock/allow mới được áp. Lượt
                # REVIEW_SARCASM / REVIEW_AMBIGUOUS / REVIEW_SCHEMA / BLOCKED
                # đều return trước điểm này nên không âm thầm xóa block.
                # Allow nằm trong business savepoint: claim/dispatch fail sau
                # đó vẫn rollback được nó.
                for ch, act in deferred_allows:
                    if act == "unblock": self.store.unblock(page, actor, ch)
                    elif act == "clear": self.store.clear_blocks(page, actor)
                    result["state_changes"].append(f"contact_pref={ch}:{act}")
                if "BLOCKED_HANDOFF" in result["routing"] and not touched:
                    # Mọi phần đều bị khóa (không còn touched sau apply):
                    # không gửi gì, chỉ audit + commit.
                    self.store.audit(event["comment_id"], "blocked", result, now_iso)
                    commit_attempt(); return result
                if isinstance(generation_context, dict) and postcard.get('persona_binding'):
                    self._generation_turn_ordinal(event, record=True)
                if touched and all(r.get('desired_outcomes') == ['lead_draft'] for r in touched):
                    for req in touched:
                        for field, slot in req['slots'].items():
                            if field in ('current_vehicle','interested_vehicle','purchase_timeline','budget','usage_need','finance_intent'):
                                value = slot.get('value') if isinstance(slot, dict) else slot
                                if (field in ('interested_vehicle', 'current_vehicle')
                                        and extract_answer(field, event.get('text', '')) is None):
                                    continue
                                self.store.save_lead_field(event, field, str(value), 'ANSWERED', now_iso)
                        req['status'] = 'COMPLETED'; req['version'] += 1; self.store.save_request(req)
                    touched = []
                    result['routing'].append('SOCIAL_ACK')
                if not touched:
                    self.store.conn.execute("RELEASE SAVEPOINT business")
                    if "SOCIAL_ACK" in result["routing"]:
                        peer_plan = None
                        snapshot = None
                        if postcard.get('persona_binding') and self.persona_store:
                            snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
                            peer = (snapshot or {}).get('snapshot', {}).get('release', {}).get('style', {}).get('fixture_revision', 0) >= 3
                            handoff = self.store.conn.execute("SELECT 1 FROM handoffs h JOIN requests r ON r.id=h.request_id WHERE h.released_at IS NULL AND r.page_id=? AND r.post_id=? AND r.actor_id=?", (page, event['post_id'], actor)).fetchone()
                            if handoff:
                                result['routing'].append('BLOCKED_HANDOFF'); commit_attempt(); return result
                            if peer:
                                peer_plan = self._peer_plan(event, semantic)
                        if isinstance(generation_context, dict) and snapshot and peer_signals is not None:
                            peer_plan = peer_plan_from_signals(peer_signals, event['text'], self.store.peer_state(event),
                                self.store.lead_profile(event), self.store.open_pending_for(actor, page_id=page,
                                post_id=event['post_id'], thread_root_id=event['thread_root_id']),
                                model_led=self.model_led_conversation)
                        if isinstance(generation_context, dict) and snapshot:
                            self._generation_turn_ordinal(event, record=True)
                            peer_plan = peer_plan or self._peer_plan(event, semantic)
                            persona = self._generated_reply(event, postcard, semantic, peer_plan,
                                                            reply_candidate, generation_context, peer_signals)
                            # Customer corrections take effect now; an unsent question creates no pending.
                            self._apply_peer_plan(event, peer_plan, None, now_iso)
                            if persona.get('status') == 'OK' and self.auto_send_generated:
                                candidate = persona.get('candidate', reply_candidate)
                                draft_id = self._queue_generated_draft(event, candidate, generation_context,
                                    persona, peer_plan, now_iso, peer_signals=peer_signals)
                                payload = self._generated_outbox_payload(event, draft_id, candidate,
                                    generation_context, persona, peer_plan)
                                self._outbox_intended(event, [], 'persona_reply', payload, now_iso)
                                self.store.conn.execute("UPDATE generated_reply_drafts SET status='APPROVED',reviewer='sandbox-auto',updated_at=? WHERE draft_id=?",
                                    (now_iso, draft_id))
                                result.update(draft_id=draft_id, response_text=None,
                                    candidate_text=persona['text'], response_kind='generated_reply',
                                    generation_validation='OK', proposal_results=persona.get('proposal_results', []))
                                result['response_plan'] = dict(actions=['generated_persona'], fact_ids=persona.get('fact_ids', []),
                                    memory_ids=persona.get('memory_ids', []), validator_version='persona_generation_v1')
                                result['routing'].append('GENERATED_REPLY_READY')
                                self.store.audit(event['comment_id'], 'generated_reply_staged', dict(draft_id=draft_id,
                                    fact_ids=persona.get('fact_ids', []), proposals=persona.get('proposal_results', [])), now_iso)
                                commit_attempt()
                                status, send = self._dispatch(event, [], 'persona_reply', persona['text'], now_iso, payload)
                                result['outbound_permission'] = status == 'SENT'
                                if status == 'SENT':
                                    result['response_text'] = persona['text']
                                    result['sends'].append(send)
                                    result['routing'].append('AUTO_REPLY')
                                    result['proposal_results'] = [dict(item, status='ACTIVE')
                                        if item.get('status') == 'PENDING' else item
                                        for item in result.get('proposal_results', [])]
                                elif status == 'BLOCKED':
                                    self._discard_generated_delivery(payload, now_iso)
                                    result['proposal_results'] = [dict(item, status='DISCARDED',
                                        reason='BLOCKED_AT_DISPATCH') if item.get('status') == 'PENDING' else item
                                        for item in result.get('proposal_results', [])]
                                    result['routing'].append('BLOCKED_AT_DISPATCH')
                                elif status == 'FAILED':
                                    self._discard_generated_delivery(payload, now_iso, delivery_status='FAILED')
                                    result['proposal_results'] = [dict(item, status='DISCARDED',
                                        reason='SEND_FAILED') if item.get('status') == 'PENDING' else item
                                        for item in result.get('proposal_results', [])]
                                    result['routing'].append('SEND_FAILED')
                                else:
                                    result['routing'].append('SEND_UNKNOWN')
                                self.store.conn.execute('UPDATE generated_reply_drafts SET status=?,updated_at=? WHERE draft_id=?',
                                    (status, now_iso, draft_id))
                                self.store.conn.commit()
                                return result
                            if persona.get('status') != 'OK' and self.auto_send_generated:
                                self.persona_store.discard_generated(event['comment_id'], now_iso)
                                if self.model_led_conversation:
                                    result.update(generation_validation=persona.get('reason') or 'NO_VALID_GENERATED_REPLY',
                                        proposal_results=persona.get('proposal_results', []))
                                    result['routing'].append('REVIEW_GENERATED_REPLY')
                                    self.store.audit(event['comment_id'], 'generated_reply_blocked', dict(
                                        reason=persona.get('reason'), proposals=persona.get('proposal_results', [])), now_iso)
                                    commit_attempt(); return result
                                if persona.get('reason') == 'GENERATED_FACT_EXISTS':
                                    result.update(generation_validation=persona['reason'],
                                        proposal_results=persona.get('proposal_results', []))
                                    result['routing'].append('REVIEW_GENERATED_FACT_RACE')
                                    self.store.audit(event['comment_id'], 'generated_reply_blocked', dict(
                                        reason=persona['reason'], proposals=persona.get('proposal_results', [])), now_iso)
                                    commit_attempt(); return result
                                fallback = self._persona_reply(event, postcard, semantic, plan=peer_plan)
                                if fallback and fallback.get('status') == 'OK':
                                    if self._uses_story_facts(snapshot, fallback.get('fact_ids')):
                                        fallback.update(status='REVIEW', reason='MEMORY_CITATION_REQUIRED')
                                    else:
                                        fallback['reply_kind'] = 'approved_fallback'
                                if fallback and fallback.get('status') == 'OK':
                                    fallback_candidate = dict(reply_text=fallback['text'])
                                    draft_id = self._queue_generated_draft(event, fallback_candidate, generation_context,
                                        fallback, peer_plan, now_iso, peer_signals=peer_signals)
                                    payload = self._generated_outbox_payload(event, draft_id, fallback_candidate,
                                        generation_context, fallback, peer_plan, approved_fallback=True)
                                    self._outbox_intended(event, [], 'persona_reply', payload, now_iso)
                                    self.store.conn.execute("UPDATE generated_reply_drafts SET status='APPROVED',reviewer='sandbox-fallback',updated_at=? WHERE draft_id=?",
                                        (now_iso, draft_id))
                                    result.update(draft_id=draft_id, response_text=None,
                                        candidate_text=fallback['text'],
                                        response_kind='approved_fallback', generation_validation=persona.get('reason'))
                                    result['routing'].append('APPROVED_FALLBACK_READY')
                                    commit_attempt()
                                    status, send = self._dispatch(event, [], 'persona_reply', fallback['text'], now_iso, payload)
                                    result['outbound_permission'] = status == 'SENT'
                                    if status == 'SENT':
                                        result['response_text'] = fallback['text']
                                        result['sends'].append(send)
                                        result['routing'].append('AUTO_REPLY')
                                    elif status in ('BLOCKED', 'FAILED'):
                                        self._discard_generated_delivery(payload, now_iso)
                                        result['routing'].append('BLOCKED_AT_DISPATCH' if status == 'BLOCKED' else 'SEND_FAILED')
                                    else:
                                        result['routing'].append('SEND_UNKNOWN')
                                    self.store.conn.execute('UPDATE generated_reply_drafts SET status=?,updated_at=? WHERE draft_id=?',
                                        (status, now_iso, draft_id))
                                    self.store.conn.commit()
                                    return result
                                result.update(generation_validation=persona.get('reason') or 'NO_VALID_GENERATED_REPLY',
                                    proposal_results=persona.get('proposal_results', []))
                                result['routing'].append('REVIEW_GENERATED_REPLY')
                                self.store.audit(event['comment_id'], 'generated_reply_blocked', dict(
                                    reason=persona.get('reason'), fact_ids=persona.get('fact_ids', []),
                                    proposals=persona.get('proposal_results', [])), now_iso)
                                commit_attempt(); return result
                            if not isinstance(persona.get('candidate', reply_candidate), dict):
                                result.update(generation_validation=persona.get('reason') or 'INVALID_CANDIDATE',
                                    proposal_results=persona.get('proposal_results', []))
                                result['routing'].append('REVIEW_GENERATED_REPLY')
                                self.store.audit(event['comment_id'], 'generated_reply_blocked', dict(
                                    reason=persona.get('reason') or 'INVALID_CANDIDATE',
                                    proposals=persona.get('proposal_results', [])), now_iso)
                                commit_attempt(); return result
                            draft_candidate = persona.get('candidate', reply_candidate)
                            draft_id = self._queue_generated_draft(event, draft_candidate, generation_context,
                                persona, peer_plan, now_iso, peer_signals=peer_signals)
                            result.update(draft_id=draft_id, response_text=None,
                                draft_text=draft_candidate.get('reply_text'), response_kind='generated_draft',
                                generation_validation=persona.get('status'),
                                proposal_results=persona.get('proposal_results', []))
                            result['response_plan'] = dict(actions=['draft_persona'], fact_ids=persona.get('fact_ids', []),
                                memory_ids=persona.get('memory_ids', []), reason=persona.get('reason'), validator_version='persona_generation_v1')
                            result['routing'].append('GENERATED_DRAFT_READY' if persona.get('status') == 'OK' else 'REVIEW_GENERATED_REPLY')
                            self.store.audit(event['comment_id'], 'generated_reply_processed', dict(draft_id=draft_id,
                                fact_ids=persona.get('fact_ids', []), proposals=persona.get('proposal_results', [])), now_iso)
                            commit_attempt(); return result
                        persona = self._persona_reply(event, postcard, semantic, plan=peer_plan)
                        if (isinstance(generation_context, dict) and event.get('generation_mode') in ('generated', 'draft')):
                            self._generation_turn_ordinal(event, record=True)
                            peer_plan = peer_plan or self._peer_plan(event, semantic)
                            self._apply_peer_plan(event, peer_plan, None, now_iso)
                            fallback = dict(persona or dict(status='REVIEW', reason='NO_VALID_GENERATED_REPLY'), reply_kind='approved_fallback')
                            snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
                            if snapshot and self._uses_story_facts(snapshot, fallback.get('fact_ids')):
                                fallback.update(status='REVIEW', reason='MEMORY_CITATION_REQUIRED')
                            draft_id = self._queue_generated_draft(event, dict(reply_text=fallback.get('text') or ''),
                                generation_context, fallback, peer_plan, now_iso, peer_signals=peer_signals)
                            result.update(draft_id=draft_id, draft_text=fallback.get('text'), response_kind='fallback_draft')
                            result['routing'].append('FALLBACK_DRAFT_READY' if fallback.get('status') == 'OK' else 'REVIEW_GENERATED_REPLY')
                            result['response_plan'] = dict(reason=fallback.get('reason'), fact_ids=fallback.get('fact_ids', []))
                            commit_attempt(); return result
                        if peer_plan is not None:
                            self._apply_peer_plan(event, peer_plan, persona, now_iso)
                        if persona and persona.get("status") != "OK":
                            reason = persona.get("reason") or "UNKNOWN"
                            result["routing"].append("REVIEW_PERSONA_" + reason)
                            result["response_plan"] = {"persona_query": persona.get("query"),
                                "release_id": persona.get("release_id"), "reason": reason}
                            self.store.audit(event["comment_id"], "persona_reply_review", result, now_iso)
                            commit_attempt(); return result
                        action = "answer_persona" if persona else "social_ack"
                        if action not in AUTO_ACTIONS:
                            result["routing"].append("BLOCKED_POLICY")
                            self.store.audit(event["comment_id"], "blocked_policy", result, now_iso)
                            commit_attempt(); return result
                        result["response_text"] = persona["text"] if persona else _social_reply(event.get("text"))
                        if not result['response_text']:
                            result['routing'].append('REVIEW_UNRESOLVED'); commit_attempt(); return result
                        result["response_plan"] = dict(parts=[result["response_text"]], actions=[action])
                        if persona:
                            result["response_plan"].update(persona_query=persona["query"],
                                persona_uuid=persona["persona_uuid"], release_id=persona["release_id"],
                                utterance_ids=persona["utterance_ids"], fact_ids=persona["fact_ids"],
                                fictional_fixture=bool(persona.get("fictional_fixture")))
                        result["routing"].append(action)
                        payload = dict(dedupe_key=f"{event['comment_id']}:reply", text=result["response_text"], comment_id=event["comment_id"])
                        if persona:
                            payload.update(persona_uuid=persona["persona_uuid"],
                                           release_id=persona["release_id"],
                                           utterance_ids=persona["utterance_ids"],
                                           fact_ids=persona["fact_ids"])
                            payload["persona_scope"] = {k: event.get(k) for k in
                                ("page_id", "post_id", "thread_root_id", "actor_id")}
                            payload["persona_event_token"] = hashlib.sha256(json.dumps(
                                event, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
                            state = self._trace_state_snapshot(event)
                            payload["persona_state_token"] = hashlib.sha256(json.dumps(
                                state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
                            blocked = self._persona_dispatch_precheck(payload)
                            if blocked:
                                result["routing"].append(blocked)
                                self.store.audit(event["comment_id"], "persona_reply_blocked", result, now_iso)
                                commit_attempt(); return result
                        # A5b: ghi intent TRƯỚC commit (trong transaction này)
                        # để crash trước dispatcher vẫn còn INTENDED mà drain
                        # được; trước đây intent chỉ xuất hiện trong _dispatch
                        # nên crash trước _dispatch làm mất reply vĩnh viễn
                        # (redelivery bị dedupe chặn).
                        purpose = "persona_reply" if persona else "reply"
                        self._outbox_intended(event, [], purpose, payload, now_iso)
                        self.store.audit(event["comment_id"], "processed", result, now_iso)
                        commit_attempt()
                        def persona_recheck():
                            current = self._persona_reply(event, postcard, semantic, plan=peer_plan)
                            if not persona:
                                return None
                            if not current or current.get("status") != "OK" or any(
                                    current.get(key) != persona.get(key)
                                    for key in ("text", "persona_uuid", "release_id", "utterance_ids", "fact_ids")):
                                return "BLOCKED_STALE_PERSONA"
                            return None
                        status, sres = self._dispatch(event, [], purpose, result["response_text"], now_iso, payload,
                                                      recheck=persona_recheck if persona else None)
                        if status == "SENT":
                            result["outbound_permission"] = True; result["sends"].append(sres)
                        elif status == "FAILED":
                            result["routing"].append("SEND_FAILED")
                        elif status == "BLOCKED":
                            result["routing"].append("BLOCKED_AT_DISPATCH")
                        else:
                            result["routing"].append("SEND_UNKNOWN")
                        return result
                    self.store.audit(event["comment_id"], "processed_no_reply", result, now_iso)
                    commit_attempt(); return result
                # Flow + plan cho từng request được chạm. Tách phần auto-safe khỏi
                # phần HANDOFF: gửi chỉ phần safe, phần handoff chỉ vào draft.
                per, actions = [], []
                for req in touched:
                    p, nums, act = self._plan(req, postcard, event, now, now_iso)
                    if act == "HANDOFF" and req["status"] == "WAITING_USER":
                        req["status"] = "OPEN"; req["version"] += 1; self.store.save_request(req)
                    per.append((req, p, nums, act)); actions.append(act)
                    result["routing"].append(act)
                safe = [(r, p, n) for (r, p, n, a) in per if a in AUTO_ACTIONS]
                ho = [(r, p, n) for (r, p, n, a) in per if a == "HANDOFF"]
                # Mixed split: request có human-draft do model đề xuất (không phải
                # khách yêu cầu người rõ ràng) nhưng plan được phần factual an
                # toàn -> gửi phần safe + ghi draft cho phần cần người, kèm câu
                # báo trong cùng reply. Khách yêu cầu người rõ ràng vẫn HANDOFF
                # toàn phần (không nằm trong safe).
                draft_safe = [(r, p, n) for (r, p, n, a) in per
                              if r.get("_human_draft") and a in AUTO_ACTIONS]
                safe_parts = [x for (_, p, _) in safe for x in p]
                stock_handoff = [item for item in ho if item[0]["topic"] == "stock"]
                if safe:
                    for r, _, _ in stock_handoff:
                        vehicle = self._vehicle(r, postcard)
                        if vehicle:
                            safe_parts.append(f"Tồn kho {vehicle} hiện chưa được xác minh.")
                if "BLOCKED_HANDOFF" in result["routing"] and safe:
                    safe_parts.append("Một phần yêu cầu cần người xem xét trước khi có thể xác nhận.")
                for (r, _, _) in draft_safe:
                    safe_parts.append(f"Phần này cần chuyển cho nhân viên để kiểm tra ({r['_human_draft']}); hiện chưa thể xác nhận tự động.")
                safe_text = render(safe_parts, postcard.get("persona_default", "tư vấn viên"),
                                   style=getattr(self, "response_style", "neutral"))
                ho_text = render([x for (_, p, _) in ho for x in p],
                                 postcard.get("persona_default", "tư vấn viên"),
                                 style=getattr(self, "response_style", "neutral"))
                safe_nums = [n for (_, _, ns) in safe for n in ns]
                ho_nums = [n for (_, _, ns) in ho for n in ns]
                def _veh_set(group):
                    vs = set()
                    for (r, _, _) in group:
                        v = self._vehicle(r, postcard)
                        if v: vs.add(v)
                        cv = _slot(r, "compare_vehicle")
                        if cv: vs.add(cv)
                    return vs
                safe_used = [u for (r, _, _) in safe for u in r.get("_used_facts", [])]
                ho_used = [u for (r, _, _) in ho for u in r.get("_used_facts", [])]
                cerr = []
                if safe or ho:
                    # Union check (số lạ) + typed check (số đúng xe/phiên bản).
                    cerr = claim_check(safe_text, safe_nums, safe_used,
                                       _veh_set(safe + (stock_handoff if safe else []))) \
                        + claim_check(ho_text, ho_nums, ho_used, _veh_set(ho)) \
                        + claim_check_typed(safe_text, safe_used) \
                        + claim_check_typed(ho_text, ho_used)
                if cerr:
                    result["errors"] = cerr; result["routing"].append("REVIEW_CLAIM")
                    self.store.conn.execute("ROLLBACK TO SAVEPOINT business")
                    self.store.conn.execute("RELEASE SAVEPOINT business")
                    self.store.audit(event["comment_id"], "claim_review", result, now_iso)
                    commit_attempt(); return result
                result["response_text"] = safe_text if safe else ho_text
                result["response_plan"] = dict(parts=[x for (_, p, _, _) in per for x in p],
                                               facts_allowed=safe_nums + ho_nums, actions=actions,
                                               facts_used=[u["fact_id"] for u in safe_used + ho_used])
                if ho and not safe:
                    result["outbound_permission"] = False
                    result["routing"].append("HANDOFF_DRAFT_ONLY")
                    self._outbox(event, [r for (r, _, _) in ho], "handoff_draft", ho_text, "NEEDS_REVIEW", now_iso)
                    self.store.conn.execute("RELEASE SAVEPOINT business")
                    self.store.audit(event["comment_id"], "processed", result, now_iso)
                    commit_attempt(); return result
                if safe:
                    plan_versions = {r["id"]: r["version"] for (r, _, _) in safe}
                    plan_facts_version = self.facts.version
                    plan_used = list(safe_used)
                    if self.dispatch_hook: self.dispatch_hook()
                    pre = self._dispatch_precheck(page, actor, safe, plan_versions, plan_facts_version, plan_used, now_iso)
                    if pre:
                        result["routing"].append(pre)
                        self.store.conn.execute("ROLLBACK TO SAVEPOINT business")
                        self.store.conn.execute("RELEASE SAVEPOINT business")
                        self.store.audit(event["comment_id"], "blocked_dispatch", result, now_iso)
                        commit_attempt(); return result
                    payload = dict(dedupe_key=f"{event['comment_id']}:reply",
                                   text=safe_text, comment_id=event["comment_id"])
                    self._outbox_intended(event, [r for (r, _, _) in safe], "reply", payload, now_iso)
                    draft_reqs = [r for (r, _, _) in ho] + [r for (r, _, _) in draft_safe]
                    if draft_reqs:
                        self._outbox(event, draft_reqs, "handoff_draft", ho_text or safe_text, "NEEDS_REVIEW", now_iso)
                    self.store.conn.execute("RELEASE SAVEPOINT business")
                    self.store.audit(event["comment_id"], "processed", result, now_iso)
                    commit_attempt()
                    # --- dispatcher phase (R03): claim -> recheck -> send.
                    def recheck():
                        return self._dispatch_precheck(page, actor, safe, plan_versions, plan_facts_version, plan_used, self.clock.now().isoformat())
                    status, sres = self._dispatch(event, [r for (r, _, _) in safe], "reply", safe_text, now_iso, payload, recheck=recheck)
                    if status == "SENT":
                        result["outbound_permission"] = True; result["sends"].append(sres)
                    elif status == "BLOCKED":
                        result["routing"].append("BLOCKED_AT_DISPATCH")
                    elif status == "SKIPPED":
                        result["routing"].append("SEND_SKIPPED_CLAIMED")
                    elif status == "FAILED":
                        result["routing"].append("SEND_FAILED")
                    else:
                        result["outbound_permission"] = False; result["routing"].append("SEND_UNKNOWN")
                    if draft_reqs:
                        result["routing"].append("HANDOFF_DRAFT_ONLY")
                    return result
                # touched nhưng không có safe lẫn ho (gần như không xảy ra):
                result["routing"].append("REVIEW_NO_AUTO_ACTION")
                self.store.conn.execute("RELEASE SAVEPOINT business")
                self.store.audit(event["comment_id"], "processed", result, now_iso)
                commit_attempt(); return result
            except EventPayloadConflict as e:
                self.store.conn.rollback()
                result["errors"].append(str(e)); result["routing"].append("EVENT_ID_CONFLICT")
                return result
            except Exception as e:
                self.store.conn.rollback()
                result["errors"].append(str(e)); result["routing"].append("ERROR_ROLLBACK")
                return result
            finally:
                try:
                    after = self._trace_state_snapshot(event)
                    result["state_after"] = after
                    if not state_trace_committed:
                        result["state_changes"] = (result["state_changes"] +
                                                   self._trace_state_diff(trace_before, after))
                except Exception:
                    pass
    def _apply_pref_phase(self, event, semantic, result, now_iso):
        """Pref pre-phase (R08/A1b). Trả (main_ops, err|None, deferred_allows).
        CHỈ block (protective opt-out) được áp ngay trước opt-out gate.
        Unblock/allow_all là MỞ RỘNG quyền nên chỉ được TRẢ VỀ để process()
        áp ở allow phase — sau validation + sarcasm gate + ambiguity gate.
        Nhờ vậy lượt REVIEW_SARCASM / REVIEW_AMBIGUOUS / semantic không hợp
        lệ không thể âm thầm xóa block. Xung đột trong cùng lượt (clear-all
        kèm op khác, hoặc cùng kênh vừa block vừa unblock) -> err, không áp
        pref nào (kể cả block)."""
        page, actor = event["page_id"], event["actor_id"]
        pref_ops = [o for o in semantic["operations"] if o.get("op") == "SET_CONTACT_PREFERENCE"]
        main_ops = [o for o in semantic["operations"] if o.get("op") != "SET_CONTACT_PREFERENCE"]
        if not pref_ops: return main_ops, None, []
        actions = []
        for o in pref_ops:
            s = o.get("scope")
            if s in ("call", "public_reply", "inbox"): actions.append((s, "block"))
            elif s == "all": actions.append(("all", "block"))
            elif s == "allow_call": actions.append(("call", "unblock"))
            elif s == "allow_public_reply": actions.append(("public_reply", "unblock"))
            elif s == "allow_inbox": actions.append(("inbox", "unblock"))
            elif s == "allow_all": actions.append((None, "clear"))
            else:
                result["errors"].append(f"bad contact scope {s!r}")
                return main_ops, "REVIEW_PREF_CONFLICT", []
        if any(a == (None, "clear") for a in actions) and len(actions) > 1:
            return main_ops, "REVIEW_PREF_CONFLICT", []
        seen = {}
        for ch, act in actions:
            if ch in seen and seen[ch] != act: return main_ops, "REVIEW_PREF_CONFLICT", []
            seen[ch] = act
        deferred = []
        for ch, act in actions:
            if act == "block":
                self.store.block(page, actor, ch)
                result["state_changes"].append(f"contact_pref={ch}:{act}")
            else:
                deferred.append((ch, act))
        return main_ops, None, deferred
    def _start_topic(self, op, semantic):
        topic = (op.get("request") or {}).get("topic") if isinstance(op.get("request"), dict) else None
        topic = topic or op.get("topic")
        if not topic and isinstance(op.get("request_ref"), str) and op["request_ref"].startswith("topic:"):
            topic = op["request_ref"][6:]
        if not topic and semantic.get("requests"):
            topic = semantic["requests"][0].get("topic")
        return topic
    def _resolve_ref(self, ref, op, page, actor, touched, by_ref,
                     post_id=None, thread_root_id=None):
        """Pure reads. Trả (req, err, candidates); err ∈ {None, 'not_found',
        'owner', 'page', 'ambiguous'}. Tie rule: by_ref khớp trước; rồi active
        khớp identity; rồi completed/cancelled khớp identity (reopen); >1 ứng
        viên sau lọc mà không phân biệt được -> ambiguous, KHÔNG chọn newest."""
        if ref in by_ref: return by_ref[ref], None, None
        if isinstance(ref, str) and not ref.startswith("topic:"):
            r = self.store.get_request(ref)
            if r:
                if r["actor_id"] != actor: return None, "owner", None
                if r["page_id"] != page: return None, "page", None
                if post_id is not None and (r.get("post_id") != post_id or
                                            r.get("thread_root_id") != thread_root_id):
                    return None, "thread", None
                return r, None, None
            pend = self.store.get_pending(ref)
            if pend:
                pr = self.store.get_request(pend["request_id"])
                if pr is None: return None, "not_found", None
                if pend["actor_id"] != actor or pr["actor_id"] != actor: return None, "owner", None
                if pr["page_id"] != page: return None, "page", None
                if post_id is not None and (pr.get("post_id") != post_id or
                                            pr.get("thread_root_id") != thread_root_id):
                    return None, "thread", None
                return pr, None, None
            ref = "topic:" + ref
        if isinstance(ref, str) and ref.startswith("topic:"):
            topic = ref[6:]
            cands = [t for t in touched if t["topic"] == topic]
            if cands:
                if op is not None:
                    scored = [(_slot_score(t, op), t) for t in cands]
                    best = max(s for s, _ in scored)
                    winners = [t for s, t in scored if s == best and best > 0]
                    if len(winners) == 1: return winners[0], None, None
                    if best > 0: return None, "ambiguous", winners
                if len(cands) == 1: return cands[0], None, None
                return None, "ambiguous", cands
            sql = "SELECT id FROM requests WHERE page_id=? AND actor_id=? AND topic=?"
            args = [page, actor, topic]
            if post_id is not None and thread_root_id is not None:
                sql += " AND post_id=? AND thread_root_id=?"
                args.extend((post_id, thread_root_id))
            rows = self.store.conn.execute(sql + " ORDER BY rowid", args).fetchall()
            all_reqs = [r for r in [self.store.get_request(rid) for (rid,) in rows] if r]
            veh, var = _op_identity(op) if op else (None, None)
            active = [r for r in all_reqs if r["status"] in ("OPEN", "WAITING_USER", "READY")]
            rest = [r for r in all_reqs if r["status"] not in ("OPEN", "WAITING_USER", "READY")]
            for pool in ([r for r in active if _ident_match(r, topic, veh, var)],
                         [r for r in rest if _ident_match(r, topic, veh, var)]):
                if len(pool) == 1: return pool[0], None, None
                if len(pool) > 1: return None, "ambiguous", pool
            # Op không nêu identity: chỉ 1 active -> follow-up đó; nhiều active
            # mà không phân biệt được -> ambiguous (không chọn newest).
            if len(active) == 1: return active[0], None, None
            if len(active) > 1: return None, "ambiguous", active
            if len(rest) == 1: return rest[0], None, None
            if len(rest) > 1: return None, "ambiguous", rest
            return None, "not_found", None
        return None, "not_found", None
    def _validate_pending(self, op, req, event, now_iso):
        """Kiểm tra pending cho ANSWER/CONFIRM/REJECT (R01). Trả routing lỗi
        hoặc None. Thứ tự: tồn tại -> actor -> link request -> hết hạn ->
        trạng thái -> qtype -> slot/options -> nội dung xác nhận."""
        t = op["op"]
        pend = self.store.get_pending(op.get("pending_id") or "")
        if not pend: return "REVIEW_BAD_PENDING"
        if pend["actor_id"] != event["actor_id"]: return "REVIEW_OWNER"
        if pend["request_id"] != req["id"]: return "REVIEW_BAD_PENDING"
        if pend["status"] == "EXPIRED" or (pend["expires_at"] and now_iso > pend["expires_at"]):
            return "REVIEW_PENDING_EXPIRED"
        if pend["status"] != "OPEN": return "REVIEW_BAD_PENDING"
        if pend['qtype'] == 'open' and pend.get('basis_version') != req['version']:
            return 'REVIEW_STALE_PROPOSITION'
        if t in ("CONFIRM_PENDING", "REJECT_PENDING"):
            if pend["qtype"] != "yes_no": return "REVIEW_BAD_PENDING"
            yn = yes_no(event.get("text", ""))
            # A2: CONFIRM và REJECT đối xứng về bằng chứng. Không xác định
            # được khẳng định/phủ định thì KHÔNG commit quyết định nào:
            # CONFIRM với "không" -> DENIED, với text mơ hồ -> AMBIGUOUS;
            # REJECT với "oke" -> DENIED (đó là đồng ý), với "Cảm ơn" (không
            # phải phủ định) -> AMBIGUOUS. Trước đây REJECT chấp nhận cả
            # yn=None nên "Cảm ơn" cũng thành REJECTED.
            if t == "CONFIRM_PENDING":
                if yn is False: return "REVIEW_CONFIRM_DENIED"
                if yn is None: return "REVIEW_AMBIGUOUS"
            else:
                if yn is True: return "REVIEW_CONFIRM_DENIED"
                if yn is None: return "REVIEW_AMBIGUOUS"
            # A1: proposition binding — pending yes/no gắn với slot values
            # lúc nó được đưa ra (basis). Nếu request đã đổi slot từ đó
            # (VD lượt mơ hồ đã sửa location) thì proposition này đã cũ:
            # không được xác nhận/từ chối nó như thể còn đúng nữa.
            if pend.get("basis_slots"):
                cur_values = {k: (v.get("value") if isinstance(v, dict) else v)
                              for k, v in (req.get("slots") or {}).items()}
                if json.dumps(cur_values, ensure_ascii=False, sort_keys=True, default=str) != \
                   json.dumps(pend["basis_slots"], ensure_ascii=False, sort_keys=True, default=str):
                    return "REVIEW_STALE_PROPOSITION"
        elif t == "ANSWER_PENDING":
            if pend["qtype"] not in ("slot", "choice", "open"): return "REVIEW_BAD_PENDING"
            slot = op.get("slot") or {}
            if pend["qtype"] in ("slot", "open"):
                if slot.get("kind") != pend["expected_slot"]: return "REVIEW_BAD_PENDING"
            else:
                # A2: choice cũng phải đúng expected_slot, không chỉ đúng
                # một giá trị trong options. Trước đây ANSWER cho pending hỏi
                # price_kind nhưng slot location="list" vẫn qua vì "list" nằm
                # trong options — và ghi location=list vào state.
                if slot.get("kind") != pend["expected_slot"]: return "REVIEW_BAD_PENDING"
                val = canon_slot(slot.get("kind"), slot.get("value")) if slot.get("kind") else None
                opts = pend["options"] or []
                if val not in opts and slot.get("value") not in opts: return "REVIEW_BAD_PENDING"
        return None
    def _validate_ops(self, event, postcard, semantic, ops, now_iso, trusted_source_ids=None):
        """Validation phase (R01/R09/A4/A6): pure reads, KHÔNG ghi business
        state. Trả (error_routing|None, ambig|None, blocked_idx:set,
        canon:list). blocked_idx = indices của ops bị HANDOFF-block (được
        tách khỏi lượt để phần còn lại vẫn xử lý được, thay vì abort cả
        lượt). canon[i] = canonical source_message_id của op i (None nếu
        op không nêu nguồn)."""
        page, actor = event["page_id"], event["actor_id"]
        touched, by_ref = [], {}
        blocked_idx, canon = set(), []
        start_counts, multi_refs, topic_start_counts = {}, set(), {}
        trusted_source_ids = trusted_source_ids if isinstance(trusted_source_ids, dict) else {}
        for i, op in enumerate(ops):
            t = op["op"]
            canon.append(None)
            if t == "SOCIAL_ACK": continue
            if t == "UNRESOLVED": return "REVIEW_UNRESOLVED", None, blocked_idx, canon
            if t == "SET_CONTACT_PREFERENCE": continue  # đã áp ở pre-phase
            # Operation evidence may describe the user's intent while the slot
            # value comes from a different, explicitly identified message.
            op_src = op.get("source_message_id")
            slot = op.get("slot") if isinstance(op.get("slot"), dict) else {}
            slot_src = slot.get("source_message_id")
            slot_source_id = slot_src or op_src
            canon[i] = slot_source_id
            op_excerpt = op.get("evidence_excerpt") or ""
            slot_excerpt = slot.get("evidence_excerpt") or op_excerpt
            if (op_excerpt and norm(op_excerpt) not in norm(event.get("text", "")) and
                    not op_src and t not in ("SOCIAL_ACK", "UNRESOLVED")):
                return "REVIEW_PROVENANCE", None, blocked_idx, canon
            if (slot and slot.get("explicit", True) and not op_src and
                    norm(op.get("evidence_excerpt", "")) not in norm(event.get("text", ""))):
                return "REVIEW_PROVENANCE", None, blocked_idx, canon
            if op_src:
                trusted_source = trusted_source_ids.get(op_src)
                if trusted_source is not None:
                    source_text = trusted_source.get("text") if isinstance(trusted_source, dict) else trusted_source
                    source_actor = (trusted_source.get("actor_id") if isinstance(trusted_source, dict)
                                    else event.get("page_id") if op_src == event.get("post_id") else None)
                    op_source_owner_ok = (source_actor == actor or
                                          op_src == event.get("post_id") and source_actor == page or
                                          op_src in (event.get("parent_id"), event.get("thread_root_id")))
                    if (not isinstance(source_text, str) or
                            op_excerpt and norm(op_excerpt) not in norm(source_text) or
                            not op_source_owner_ok):
                        return "REVIEW_PROVENANCE", None, blocked_idx, canon
                else:
                    row = self.store.conn.execute(
                        "SELECT text, payload FROM events WHERE comment_id=?", (op_src,)).fetchone()
                    if not row: return "REVIEW_PROVENANCE", None, blocked_idx, canon
                    try: payload = json.loads(row[1] or "{}")
                    except Exception: payload = {}
                    if payload.get("page_id") != page: return "REVIEW_PROVENANCE", None, blocked_idx, canon
                    if (payload.get("post_id") != event.get("post_id") or
                            payload.get("thread_root_id") != event.get("thread_root_id")):
                        return "REVIEW_PROVENANCE", None, blocked_idx, canon
                    if (payload.get("actor_id") != actor and
                            op_src not in (event.get("parent_id"), event.get("thread_root_id"))):
                        return "REVIEW_PROVENANCE", None, blocked_idx, canon
                    if op_excerpt and norm(op_excerpt) not in norm(row[0] or ""):
                        return "REVIEW_PROVENANCE", None, blocked_idx, canon
            if slot and slot_source_id:
                source = trusted_source_ids.get(slot_source_id)
                if not isinstance(source, dict):
                    return "REVIEW_PROVENANCE", None, blocked_idx, canon
                source_text = source.get("text") or ""
                source_actor = source.get("actor_id")
                if slot.get("explicit", True) and slot_source_id != event.get("comment_id"):
                    return "REVIEW_PROVENANCE", None, blocked_idx, canon
                post_inference = (slot_source_id == event.get("post_id") and
                                  source.get("role") == "post" and slot.get("explicit", True) is False and
                                  slot.get("kind") == "vehicle" and
                                  canon_slot("vehicle", slot.get("value")) in
                                  {canon_slot("vehicle", v) for v in source.get("vehicles", [])})
                inferred = slot.get("explicit", True) is False
                if (inferred and (slot_source_id == event.get("comment_id") or
                                  source.get("state_token") != slot.get("source_state_token") or
                                  source_actor != slot.get("source_actor_id")) or
                        source_actor != actor and not post_inference or
                        slot.get("explicit", True) and source_actor != actor or
                        slot_excerpt and norm(slot_excerpt) not in norm(source_text) and
                        not (post_inference and any(
                            norm(slot_excerpt) == norm(vehicle) and
                            canon_slot("vehicle", vehicle) == canon_slot("vehicle", slot.get("value"))
                            for vehicle in source.get("vehicles", [])))):
                    return "REVIEW_PROVENANCE", None, blocked_idx, canon
            if t == "START_REQUEST":
                topic = self._start_topic(op, semantic)
                topic_start_counts[topic] = topic_start_counts.get(topic, 0) + 1
                outcomes = [r.get("desired_outcome") for r in semantic.get("requests", [])
                            if r.get("topic") == topic]
                outcome = outcomes[topic_start_counts[topic] - 1] if topic_start_counts[topic] <= len(outcomes) else topic
                ref = op.get("request_ref") or f"topic:{topic}"
                # Identity riêng từng START (A4): nhiều START cùng ref thì
                # mỗi START dùng identity của chính nó, không dùng identity
                # cuối lượt cho mọi check.
                veh, var = _start_identity(ops, ref, op)
                if topic and self.store.handoff_for_scope(page, actor, topic, veh, var,
                                                          event.get("post_id"), event.get("thread_root_id")):
                    blocked_idx.add(i)
                    continue
                # Virtual request cho op sau cùng turn resolve (chưa tạo thật).
                vreq = dict(id=None, page_id=page, actor_id=actor, topic=topic,
                            status="OPEN", version=1, slots={}, desired_outcomes=[outcome])
                if op.get("slot"):
                    for k, value in [(op["slot"].get("kind"), op["slot"].get("value")),
                                     *((op["slot"].get("extra") or {}).items())]:
                        if k: vreq["slots"][k] = dict(value=canon_slot(k, value) if k in ("vehicle", "variant", "color", "location", "price_kind") else value)
                duplicate = next((r for r in touched if _ident_match(r, topic, veh, var)
                                  and r.get("desired_outcomes") == [outcome]), None)
                if duplicate is None: touched.append(vreq)
                else: vreq = duplicate
                start_counts[ref] = start_counts.get(ref, 0) + 1
                if start_counts[ref] > 1:
                    # Nhiều START cùng ref: không đặt by_ref (để op sau
                    # resolve bằng identity scoring trên touched, không bị
                    # last-write-wins kéo về START cuối).
                    multi_refs.add(ref)
                    by_ref.pop(ref, None)
                else:
                    by_ref[ref] = vreq
                continue
            req, err, cands = self._resolve_ref(op.get("request_ref"), op, page, actor, touched, by_ref,
                                                event.get("post_id"), event.get("thread_root_id"))
            if err == "ambiguous":
                return None, dict(topic=op.get("request_ref"), candidates=cands), blocked_idx, canon
            if err == "owner": return "REVIEW_OWNER", None, blocked_idx, canon
            if err == "page": return "REVIEW_PAGE_SCOPE", None, blocked_idx, canon
            if err == "thread": return "REVIEW_THREAD_SCOPE", None, blocked_idx, canon
            if err == "not_found":
                if t in ("REQUEST_HUMAN", "REPORT_COMPLAINT"): continue  # apply tạo rồi handoff
                return "REVIEW_BAD_REF", None, blocked_idx, canon
            if req.get("id") and self.store.handoff(req["id"]):
                # Op nhắm vào request đang handoff: skip riêng op này (A4),
                # không abort cả lượt như trước.
                blocked_idx.add(i)
                continue
            if t in ("ANSWER_PENDING", "CONFIRM_PENDING", "REJECT_PENDING"):
                perr = self._validate_pending(op, req, event, now_iso)
                if perr: return perr, None, blocked_idx, canon
            if t == "UPDATE_SLOT" and req.get("id") is None:
                slot = op.get("slot") or {}
                for kind, value in [(slot.get("kind"), slot.get("value")), *((slot.get("extra") or {}).items())]:
                    if kind: req["slots"][kind] = dict(value=canon_slot(kind, value) if kind in ("vehicle", "variant", "color", "location", "price_kind") else value)
            if req not in touched: touched.append(req)
            if op.get("request_ref") and op.get("request_ref") not in multi_refs:
                by_ref[op["request_ref"]] = req
        return None, None, blocked_idx, canon
    def _disambiguate(self, event, postcard, ambig, result, now_iso):
        """Tie topic ref: hỏi rõ các ứng viên, KHÔNG mutate business state."""
        descs = []
        for r in ambig["candidates"]:
            v = _slot(r, "vehicle"); var = _slot(r, "variant")
            descs.append(f"{r['topic']} {v or ''} {var or ''}".strip())
        text = f"Bạn đang nói tới yêu cầu nào ạ: {' hay '.join(descs)}?"
        result["routing"].append("REVIEW_REF_AMBIGUOUS")
        result["response_text"] = text
        result["response_plan"] = dict(parts=[text], actions=["ask_clarification"])
        result["ambiguous_refs"] = dict(topic=ambig["topic"], candidates=[r.get("id") for r in ambig["candidates"]])
        payload = dict(dedupe_key=f"{event['comment_id']}:reply", text=text, comment_id=event["comment_id"])
        # A5b: ghi intent TRƯỚC commit như nhánh social — crash trước
        # dispatcher vẫn còn INTENDED để drain (trước đây commit rồi mới
        # dispatch nên crash trước _dispatch làm mất câu hỏi làm rõ).
        self._outbox_intended(event, [], "reply", payload, now_iso)
        self.store.audit(event["comment_id"], "ref_ambiguous", result, now_iso)
        self.store.conn.commit()
        status, sres = self._dispatch(event, [], "reply", text, now_iso, payload)
        if status == "SENT":
            result["outbound_permission"] = True; result["sends"].append(sres)
        else:
            result["routing"].append("SEND_UNKNOWN")
        return result
    def _outbox(self, event, touched, purpose, text, status, now_iso, payload=None):
        if not touched:
            key = f"{event['comment_id']}:lead_draft" if purpose == 'lead_draft' else f"{event['comment_id']}:reply"
            row = self.store.conn.execute("SELECT status FROM outbox WHERE dedupe_key=?", (key,)).fetchone()
            if row and row[0] == "SENT": return  # không ghi đè row đã SENT
            self.store.conn.execute(
                "INSERT INTO outbox(dedupe_key,request_id,purpose,version,status,payload,created_at,claimed_at,claimed_by) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET "
                "request_id=excluded.request_id,purpose=excluded.purpose,version=excluded.version,"
                "status=excluded.status,payload=excluded.payload,created_at=excluded.created_at,"
                "claimed_at=NULL,claimed_by=NULL",
                (key, None, purpose, 1, status,
                 json.dumps(payload or dict(text=text), ensure_ascii=False), now_iso, None, None))
            return
        for req in touched:
            key = f"{event['comment_id']}:{req['id']}:{purpose}:{req['version']}"
            row = self.store.conn.execute("SELECT status FROM outbox WHERE dedupe_key=?", (key,)).fetchone()
            if row and row[0] == "SENT": continue
            self.store.conn.execute(
                "INSERT INTO outbox(dedupe_key,request_id,purpose,version,status,payload,created_at,claimed_at,claimed_by) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET "
                "request_id=excluded.request_id,purpose=excluded.purpose,version=excluded.version,"
                "status=excluded.status,payload=excluded.payload,created_at=excluded.created_at,"
                "claimed_at=NULL,claimed_by=NULL",
                (key, req["id"], purpose, req["version"], status,
                 json.dumps(payload or dict(text=text), ensure_ascii=False), now_iso, None, None))
    def _outbox_intended(self, event, touched, purpose, payload, now_iso):
        """Ghi outbox intent (INTENDED) trong business transaction, TRƯỚC khi
        commit durable point; dispatcher claim rồi mới gửi (R03)."""
        key = payload["dedupe_key"]
        req = touched[0] if touched else None
        row = self.store.conn.execute("SELECT status FROM outbox WHERE dedupe_key=?", (key,)).fetchone()
        if (event.get("event_type") == "edit" and purpose == "persona_reply"
                and payload.get("persona_event_token") and row and row[0] == "INTENDED"):
            self.store.conn.execute(
                "UPDATE outbox SET request_id=?,purpose=?,version=?,status='INTENDED',payload=?,"
                "created_at=?,claimed_at=NULL,claimed_by=NULL WHERE dedupe_key=? AND status='INTENDED'",
                (req["id"] if req else None, purpose, req["version"] if req else 1,
                 json.dumps(payload, ensure_ascii=False), now_iso, key))
            return
        self.store.insert_outbox_intended(key, req["id"] if req else None, purpose,
                                          req["version"] if req else 1, payload, now_iso)
    # ---------- ops ----------
    def _apply_ops(self, event, postcard, semantic, ops, ctx, result, now_iso, canon=None):
        """Áp operations đã qua validation phase. Các check ở đây là phòng thủ
        (validation đã chạy trước, trong cùng transaction nên trạng thái không
        đổi); mọi REVIEW/BLOCKED (trừ BLOCKED_HANDOFF) đều được process()
        rollback qua savepoint. BLOCKED_HANDOFF thì SKIP riêng op đó (A4).
        canon: canonical source song song với ops (từ _validate_ops)."""
        page, actor = event["page_id"], event["actor_id"]
        touched, by_ref = [], {}
        handed_off_now = set()
        start_counts, multi_refs, topic_start_counts = {}, set(), {}
        def _block_handoff():
            if "BLOCKED_HANDOFF" not in result["routing"]:
                result["routing"].append("BLOCKED_HANDOFF")
        for oi, op in enumerate(ops):
            _canon = (canon or [None] * len(ops))[oi] if canon is not None else None
            t = op["op"]
            if t == "SOCIAL_ACK":
                result["routing"].append("SOCIAL_ACK")
                continue
            if t == "UNRESOLVED":
                result["routing"].append("REVIEW_UNRESOLVED"); return touched, None
            if t == "SET_CONTACT_PREFERENCE":
                continue  # đã áp ở pref pre-phase (defensive: không áp lại)
            if t == "START_REQUEST":
                # Handoff scope (R04/A4): cùng nghiệp vụ/đối tượng đang có
                # handoff hiệu lực thì KHÔNG tạo id mới để vượt handoff.
                # Identity riêng từng START (nhiều START cùng ref thì mỗi
                # START dùng identity của chính nó, không lấy identity cuối
                # lượt áp cho mọi START).
                topic = self._start_topic(op, semantic)
                topic_start_counts[topic] = topic_start_counts.get(topic, 0) + 1
                topic_outcomes = [r.get("desired_outcome") for r in semantic.get("requests", [])
                                  if r.get("topic") == topic]
                _outcome = (topic_outcomes[topic_start_counts[topic] - 1]
                            if topic_start_counts[topic] <= len(topic_outcomes) else topic)
                _ref = op.get("request_ref") or f"topic:{topic}"
                _veh, _var = _start_identity(ops, _ref, op)
                if topic and self.store.handoff_for_scope(page, actor, topic, _veh, _var,
                                                          event.get("post_id"), event.get("thread_root_id")):
                    _block_handoff(); continue  # A4: skip riêng START này
                start_counts[_ref] = start_counts.get(_ref, 0) + 1
                if start_counts[_ref] > 1:
                    multi_refs.add(_ref)
                    by_ref.pop(_ref, None)
                # Coalesce only the same identity and desired outcome; distinct
                # outcomes (list vs OTR) need independent slots and planning.
                _dup = None
                for _t in touched:
                    if _ident_match(_t, topic, _veh, _var) and _t.get("desired_outcomes") == [_outcome]: _dup = _t; break
                if _dup is not None:
                    if op.get("slot"):
                        self._apply_slot(_dup, op, event, _canon, ctx.get("state_token")); self.store.save_request(_dup)
                    if _ref not in multi_refs: by_ref[_ref] = _dup
                    continue
                # Reuse request cùng identity đang mở; khác identity -> tạo mới
                # (VD: đang mở giá Creta, hỏi giá Tucson thì Creta vẫn còn đó).
                # Yêu cầu cũ đã COMPLETED/CANCELLED không nằm trong active nên
                # khách quay lại mở mới hợp lệ không bị dedupe nhầm.
                # QUAN TRỌNG: chỉ reuse khi START tự nêu identity (vehicle/
                # variant); START không nêu identity thì KHÔNG được wildcard
                # match request đang mở rồi để UPDATE sau đó ghi đè identity
                # của nó (lỗi: hỏi giá Tucson lại sửa request giá Creta).
                existing = None
                if _veh is not None or _var is not None:
                    for a in self.store.active_requests(page, actor, event.get("post_id"),
                                                        event.get("thread_root_id")):
                        if _ident_match(self.store.get_request(a["id"]), topic, _veh, _var) and \
                                self.store.get_request(a["id"]).get("desired_outcomes") == [_outcome]:
                            existing = self.store.get_request(a["id"]); break
                if existing:
                    req = existing
                    if op.get("slot"):
                        self._apply_slot(req, op, event, _canon, ctx.get("state_token")); self.store.save_request(req)
                else:
                    req = _new_req(page, event.get("post_id"), event.get("thread_root_id"), actor, topic,
                                   _outcome,
                                   event["comment_id"], now_iso)
                    self.store.insert_request(req)
                    if op.get("slot"):
                        self._apply_slot(req, op, event, _canon, ctx.get("state_token")); self.store.save_request(req)
                if _ref not in multi_refs: by_ref[_ref] = req
                if req not in touched: touched.append(req)
                continue
            req, err, _cands = self._resolve_ref(op.get("request_ref"), op, page, actor, touched, by_ref,
                                                 event.get("post_id"), event.get("thread_root_id"))
            if err == "ambiguous":
                result["errors"].append(f"ambiguous request_ref: {op.get('request_ref')}")
                result["routing"].append("REVIEW_REF_AMBIGUOUS"); return touched, None
            if err == "owner":
                result["errors"].append("owner mismatch"); result["routing"].append("REVIEW_OWNER")
                return touched, None
            if err == "page":
                result["errors"].append("page scope mismatch"); result["routing"].append("REVIEW_PAGE_SCOPE")
                return touched, None
            if err == "thread":
                result["errors"].append("post/thread scope mismatch"); result["routing"].append("REVIEW_THREAD_SCOPE")
                return touched, None
            if req is None and t in ("REQUEST_HUMAN", "REPORT_COMPLAINT"):
                # Khiếu nại có thể đến khi chưa có request mở: tạo rồi handoff.
                topic = op["request_ref"][6:] if str(op.get("request_ref", "")).startswith("topic:") else "complaint"
                req = _new_req(page, event.get("post_id"), event.get("thread_root_id"),
                               actor, topic, topic, event["comment_id"], now_iso)
                self.store.insert_request(req)
            if req is None:
                result["errors"].append(f"request_ref not found: {op.get('request_ref')}")
                result["routing"].append("REVIEW_BAD_REF"); return touched, None
            if req["actor_id"] != actor:
                result["errors"].append("owner mismatch"); result["routing"].append("REVIEW_OWNER")
                return touched, None
            if req["page_id"] != page:
                result["errors"].append("page scope mismatch"); result["routing"].append("REVIEW_PAGE_SCOPE")
                return touched, None
            if req["status"] == "COMPLETED" and t in ("UPDATE_SLOT", "ANSWER_PENDING"):
                req["status"] = "OPEN"; req["version"] += 1; self.store.save_request(req)
            if req["id"] in handed_off_now:
                # Cùng turn đã handoff rồi: op human thứ hai là idempotent, bỏ qua.
                if req not in touched: touched.append(req)
                continue
            if req["id"] and self.store.handoff(req["id"]):
                _block_handoff(); continue  # A4: skip riêng op này
            if t == "UPDATE_SLOT":
                self._apply_slot(req, op, event, _canon, ctx.get("state_token")); self.store.save_request(req)
            elif t in ("ANSWER_PENDING", "CONFIRM_PENDING", "REJECT_PENDING"):
                perr = self._validate_pending(op, req, event, now_iso)
                if perr:
                    result["routing"].append(perr); return touched, None
                pend = self.store.get_pending(op.get("pending_id") or "")
                if t == "ANSWER_PENDING":
                    self._apply_slot(req, op, event, _canon, ctx.get("state_token"))
                    pend["status"] = "ANSWERED"; pend["version"] += 1; self.store.update_pending(pend)
                    self.store.save_request(req)
                elif t == "CONFIRM_PENDING":
                    pend["status"] = "CONFIRMED"; pend["version"] += 1; self.store.update_pending(pend)
                    req["status"] = "READY"; req["version"] += 1; self.store.save_request(req)
                else:  # REJECT_PENDING
                    pend["status"] = "REJECTED"; pend["version"] += 1; self.store.update_pending(pend)
                    req["status"] = "OPEN"; req["version"] += 1; self.store.save_request(req)
                    # A2: ghi dấu đề xuất này đã bị từ chối để _plan không
                    # đề xuất lại y hệt (vòng lặp hỏi — từ chối — hỏi lại).
                    self.store.add_proposal_rejection(
                        req["id"], _canon_slot_values(req["slots"]), now_iso)
            elif t == "CANCEL_REQUEST":
                for p in self.store.open_pending_for(actor, req["id"], page_id=page,
                                                      post_id=event.get("post_id"),
                                                      thread_root_id=event.get("thread_root_id")):
                    p["status"] = "SUPERSEDED"; p["version"] += 1; self.store.update_pending(p)
                req["status"] = "CANCELLED"; req["version"] += 1; self.store.save_request(req)
            elif t in ("REQUEST_HUMAN", "REPORT_COMPLAINT"):
                if t == "REQUEST_HUMAN" and op.get("human_source") == "model_proposed":
                    # Model đề xuất người vì thiếu thông tin (VD: xin số tư vấn
                    # nhưng không có fact số điện thoại), KHÔNG phải khách yêu
                    # cầu người rõ ràng. Giữ request mở để plan phần factual
                    # được (nếu có fact); phần cần người chỉ vào draft ở bước
                    # plan/dispatch. Khách yêu cầu người rõ ràng (mặc định khi
                    # không ghi source) và khiếu nại vẫn HANDOFF toàn phần.
                    req["_human_draft"] = op.get("reason", t)
                else:
                    req["status"] = "HANDED_OFF"; req["version"] += 1; self.store.save_request(req)
                    self.store.add_handoff(req["id"], "staff", op.get("reason", t), req["version"], now_iso)
                    handed_off_now.add(req["id"])
            if req not in touched: touched.append(req)
            if op.get("request_ref") and op.get("request_ref") not in multi_refs:
                by_ref[op["request_ref"]] = req
        return touched, None
    def _apply_slot(self, req, op, event, canonical=None, state_token=None):
        """Normalize primary and extra values, bumping the version once per op."""
        slot = op.get("slot") or {}
        slots = [slot]
        for kind, raw in (slot.get("extra") or {}).items():
            if kind == slot.get("kind"): continue
            slots.append(dict(kind=kind, value=raw, explicit=slot.get("explicit", True),
                              source_message_id=slot.get("source_message_id"),
                              source_request_id=slot.get("source_request_id"),
                              source_actor_id=slot.get("source_actor_id"),
                              source_state_token=slot.get("source_state_token")))
        # Clear dependencies from the old vehicle before applying this turn's variant/color.
        for value_slot in sorted(slots, key=lambda s: s.get("kind") != "vehicle"):
            self._apply_slot_value(req, value_slot, event, canonical, state_token)
        req["version"] += 1

    def _supersede_testdrive_proposals(self, req):
        for pend in self.store.open_pending_for(req["actor_id"], req["id"],
                                                 page_id=req.get("page_id"),
                                                 post_id=req.get("post_id"),
                                                 thread_root_id=req.get("thread_root_id")):
            if pend["qtype"] == "yes_no":
                pend["status"] = "SUPERSEDED"; pend["version"] += 1; self.store.update_pending(pend)

    def _clear_past_testdrive_datetime(self, req, now):
        if req.get("topic") != "testdrive": return False
        value = _slot(req, "datetime")
        if not isinstance(value, str): return False
        try: past = datetime.fromisoformat(value) < now
        except ValueError: return False
        if not past: return False
        req["slots"].pop("datetime", None)
        _set_slot(req, "datetime_unresolved", "past", value, None, bump=False)
        self._supersede_testdrive_proposals(req)
        return True

    def _apply_slot_value(self, req, slot, event, canonical=None, state_token=None):
        """Apply one value with validated provenance; preserve unchanged inherited slots."""
        kind, slot_value = slot.get("kind"), slot.get("value")
        raw = slot.get("raw") if isinstance(slot.get("raw"), str) else slot_value
        if kind == "down_payment_unit":
            dp = _slot(req, "down_payment")
            if isinstance(dp, dict):
                dp = dict(dp); dp["unit"] = slot_value
                _set_slot(req, "down_payment", dp, raw,
                          canonical if canonical is not None else event["comment_id"],
                          explicit=True, source_actor_id=event["actor_id"], bump=False)
                return
        value = canon_slot(kind, slot_value)
        inherited_datetime = None
        if kind == "datetime" and isinstance(raw, str):
            tk, tv = parse_time_expression(raw, self.clock.now())
            if tk == "exact":
                value = tv.isoformat()
            elif tk == "range":
                start, end, label, opts = tv
                value = {"range": [start.isoformat(), end.isoformat()], "label": label,
                         "options": [o.isoformat() for o in opts]}
            elif tk == "time_only":
                # Chỉ có buổi, không có ngày: kết hợp với ngày của đề xuất hiện
                # có nếu có; ngược lại ghi marker unresolved để plan hỏi lại.
                cur = _slot(req, "datetime")
                if isinstance(cur, str):
                    try:
                        current_slot = req["slots"].get("datetime")
                        inherited_datetime = current_slot if isinstance(current_slot, dict) else None
                        hour, minute = tv if isinstance(tv, tuple) else (tv, 0)
                        value = datetime.fromisoformat(cur).replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
                        if datetime.fromisoformat(value) < self.clock.now():
                            req["slots"].pop("datetime", None)
                            self._supersede_testdrive_proposals(req)
                            _set_slot(req, "datetime_unresolved", "time_only_past", raw, event["comment_id"], bump=False)
                            return
                    except Exception:
                        req["slots"].pop("datetime", None)
                        self._supersede_testdrive_proposals(req)
                        _set_slot(req, "datetime_unresolved", "time_only_without_date", raw, event["comment_id"], bump=False)
                        return
                else:
                    req["slots"].pop("datetime", None)
                    self._supersede_testdrive_proposals(req)
                    _set_slot(req, "datetime_unresolved", "time_only_without_date", raw, event["comment_id"], bump=False)
                    return
            else:  # past / vague: không đặt slot sai, ghi marker để plan hỏi lại
                req["slots"].pop("datetime", None)
                self._supersede_testdrive_proposals(req)
                _set_slot(req, "datetime_unresolved", tk, raw, event["comment_id"], bump=False)
                return
        # đổi vehicle -> vô hiệu hoá variant/color phụ thuộc xe cũ
        if kind == "vehicle" and _slot(req, "vehicle") and _slot(req, "vehicle") != value:
            req["slots"].pop("variant", None); req["slots"].pop("color", None)
        old = req["slots"].get(kind)
        new_explicit = slot.get("explicit", True)
        if inherited_datetime and inherited_datetime.get("source_message_id"):
            source_id = inherited_datetime["source_message_id"]
            _set_slot(req, kind, value, raw, source_id, explicit=False,
                      source_request_id=req["id"],
                      source_actor_id=inherited_datetime.get("source_actor_id") or req["actor_id"],
                      source_state_token=state_token, bump=False)
            return
        # A6: canonical (nếu có) thắng mọi nguồn khác và đã được validate ở
        # _validate_ops; chỉ khi không có canonical mới dùng logic kế thừa.
        if canonical is not None:
            _set_slot(req, kind, value, raw, canonical,
                      explicit=new_explicit, confirmed=slot.get("confirmed", False),
                      source_request_id=slot.get("source_request_id"),
                      source_actor_id=slot.get("source_actor_id") or event["actor_id"],
                      source_state_token=slot.get("source_state_token"), bump=False)
        elif old is not None and old.get("value") == value and not slot.get("source_message_id"):
            pass  # kế thừa y hệt: giữ nguyên nguồn + explicit gốc
        else:
            src_id = slot.get("source_message_id")
            if src_id is None:
                src_id = event["comment_id"] if new_explicit else None
            _set_slot(req, kind, value, raw, src_id,
                      explicit=new_explicit, confirmed=slot.get("confirmed", False),
                      source_request_id=slot.get("source_request_id"),
                      source_actor_id=slot.get("source_actor_id") or event["actor_id"],
                      source_state_token=slot.get("source_state_token"), bump=False)
    # ---------- flow / plan ----------
    def _ask(self, req, event, qtype, slot, options, proposition, now_iso):
        exp = (now_iso[:10] and (datetime.fromisoformat(now_iso) + timedelta(hours=PENDING_TTL_HOURS)).isoformat())
        # A1: basis của proposition = slot values lúc hỏi + version mà request
        # sẽ có sau khi _ask save (+1). CONFIRM/REJECT sau này đối chiếu với
        # basis này: slot đã đổi -> proposition cũ, không được quyết định nữa.
        basis_slots = {k: (v.get("value") if isinstance(v, dict) else v)
                       for k, v in (req.get("slots") or {}).items()}
        p = dict(id=_pending_id(), request_id=req["id"], actor_id=req["actor_id"], qtype=qtype,
                 expected_slot=slot, options=options, proposition=proposition,
                 issued_at=now_iso, expires_at=exp, version=1, status="OPEN",
                 basis_version=req["version"] + 1, basis_slots=basis_slots)
        self.store.insert_pending(p)
        req["status"] = "WAITING_USER"; req["version"] += 1; self.store.save_request(req)
        return p
    def _vehicle(self, req, postcard):
        v = _slot(req, "vehicle")
        if v: return v
        vs = postcard.get("vehicles") or []
        return vs[0] if len(vs) == 1 else None
    def _plan(self, req, postcard, event, now, now_iso):
        topic, parts, nums = req["topic"], [], []
        # Provenance cho claim check: fact ID -> đối tượng -> thuộc tính ->
        # giá trị/đơn vị. Claim check ở process() dùng danh sách này, không
        # chỉ kiểm tra "số có trong DB".
        req["_used_facts"] = []
        def _use(fact, attribute, value=None, unit=None):
            req["_used_facts"].append(dict(
                fact_id=fact.get("id", "calc"), vehicle=fact.get("vehicle"),
                variant=fact.get("variant"), location=fact.get("location"),
                attribute=attribute, value=fact["value"] if value is None else value,
                unit=fact.get("unit") if unit is None else unit))
        if req["status"] == "CANCELLED":
            return ([f"Đã hủy yêu cầu {topic} của bạn."], nums, "social_ack")
        if req["status"] == "HANDED_OFF":
            return (["Yêu cầu này cần người xem xét trước khi mình có thể xác nhận thêm."], nums, "HANDOFF")
        if self._clear_past_testdrive_datetime(req, now):
            if req["status"] == "READY": req["status"] = "OPEN"
            req["version"] += 1; self.store.save_request(req)
        if req["status"] == "READY" and topic == "testdrive":
            return (["Mình đã ghi nhận bạn đồng ý với đề xuất lái thử. Đây chưa phải lịch đã đặt."],
                    nums, "answer_confirmation")
        fresh = lambda *a, **k: self.facts.query(*a, now=now, **k)
        if topic == "complaint":
            req["status"] = "HANDED_OFF"; req["version"] += 1; self.store.save_request(req)
            self.store.add_handoff(req["id"], "staff", "complaint", req["version"], now_iso)
            return (["Mình đã ghi nhận khiếu nại; nội dung này cần người kiểm tra trước khi có câu trả lời."],
                    nums, "HANDOFF")
        if topic == "price":
            vehicle = self._vehicle(req, postcard)
            if not vehicle:
                self._ask(req, event, "slot", "vehicle", postcard.get("vehicles") or [],
                          "Bạn đang hỏi giá xe nào?", now_iso)
                return (["Bạn đang hỏi giá xe nào ạ?"], nums, "ask_clarification")
            kind = _slot(req, "price_kind")
            if not kind:
                self._ask(req, event, "choice", "price_kind", ["list", "otr"],
                          "Bạn muốn giá niêm yết hay giá lăn bánh?", now_iso)
                return (["Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?"], nums, "ask_clarification")
            variant = _slot(req, "variant")
            fl, st, conf = fresh("list_price", vehicle=vehicle, variant=variant)
            if conf and not variant:
                variants = sorted({f["variant"] for f in self.facts.facts if f["type"] == "list_price" and f.get("vehicle") == vehicle and f["variant"]})
                self._ask(req, event, "choice", "variant", variants, "Bạn muốn xem phiên bản nào?", now_iso)
                return (["Bạn muốn xem phiên bản nào ạ?"], nums, "ask_clarification")
            if conf or not fl:
                return (["Giá phiên bản này chưa có dữ liệu xác nhận nên mình chưa thể báo chính xác."],
                        nums, "HANDOFF")
            price = fl[0]["value"]; nums.append(price); _use(fl[0], "list_price")
            if kind == "list":
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                return ([f"Giá niêm yết {vehicle} {variant or ''} là {fmt_money(price)} (giá mẫu SAMPLE).".strip()],
                        nums, "answer_price")
            loc = _slot(req, "location")
            if not loc:
                self._ask(req, event, "slot", "location", ["Hà Nội", "TP.HCM"],
                          "Bạn tính lăn bánh ở tỉnh/thành nào?", now_iso)
                return (["Bạn tính lăn bánh ở tỉnh/thành nào ạ?"], nums, "ask_clarification")
            fr, _, fconf = fresh("reg_rate", location=loc); fp, _, pconf = fresh("plate_fee", location=loc)
            fd, _, dconf = fresh("road_fee")
            if fconf or pconf or dconf:
                # R07: không bỏ cờ conflict ở phí lăn bánh (dùng fact đầu tiên
                # trong khi có 2 mức phí mâu thuẫn là trả lời sai).
                return (["Dữ liệu phí lăn bánh hiện chưa thống nhất, nên mình chưa thể đưa ra con số chính xác."],
                        nums, "HANDOFF")
            if not fr or not fp or not fd:
                return ([f"Mình chưa có đủ dữ liệu phí lăn bánh ở {loc} để tính chính xác."], nums, "HANDOFF")
            calc = calc_otr(price, fr[0]["value"], fp[0]["value"], fd[0]["value"])
            nums += [fr[0]["value"], fp[0]["value"], fd[0]["value"], calc["total"]]
            _use(fr[0], "reg_rate"); _use(fp[0], "plate_fee"); _use(fd[0], "road_fee")
            req["_used_facts"].append(dict(fact_id="calc:otr_v1", vehicle=vehicle, variant=variant,
                                           location=loc, attribute="otr_total",
                                           value=calc["total"], unit="VND"))
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Giá lăn bánh {vehicle} {variant or ''} tại {loc} là {fmt_money(calc['total'])} "
                     f"(niêm yết {fmt_money(price)} + trước bạ {fr[0]['value']}% + biển {fmt_money(fp[0]['value'])} "
                     f"+ phí đường bộ {fmt_money(fd[0]['value'])}; số mẫu SAMPLE)."],
                    nums, "answer_price")
        if topic == "promo":
            vehicle = self._vehicle(req, postcard)
            if not vehicle:
                # R07: postcard nhiều xe mà chưa rõ xe nào thì hỏi, không tự
                # khớp promo của xe đầu tiên.
                self._ask(req, event, "slot", "vehicle", postcard.get("vehicles") or [],
                          "Bạn hỏi ưu đãi xe nào?", now_iso)
                return (["Bạn hỏi ưu đãi xe nào ạ?"], nums, "ask_clarification")
            fl, st, conf = fresh("promo", vehicle=vehicle)
            if conf:
                return (["Dữ liệu ưu đãi hiện chưa thống nhất, nên mình chưa thể xác nhận mức áp dụng."],
                        nums, "HANDOFF")
            if not fl:
                if st: return (["Thông tin ưu đãi đã cũ nên mình chưa thể xác nhận mức hiện tại."], nums, "HANDOFF")
                return (["Hiện chưa có ưu đãi được xác nhận cho xe này."], nums, "answer_promo")
            nums.append(fl[0]["value"]); _use(fl[0], "promo")
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Ưu đãi mẫu cho {vehicle or 'xe này'} là {fmt_money(fl[0]['value'])} (SAMPLE)."], nums, "answer_promo")
        if topic == "loan":
            vehicle = self._vehicle(req, postcard)
            dp = _slot(req, "down_payment")
            if isinstance(dp, dict) and dp.get("amount") and not dp.get("unit"):
                self._ask(req, event, "slot", "down_payment_unit", ["triệu", "tỷ"],
                          "Số trả trước bạn nói là đơn vị nào?", now_iso)
                return (["Số trả trước bạn nói là đơn vị triệu hay tỷ ạ?"], nums, "ask_clarification")
            fl, _, lconf = fresh("loan_rate")
            if lconf:
                return (["Dữ liệu lãi suất hiện chưa thống nhất, nên mình chưa thể đưa ra con số chính xác."],
                        nums, "HANDOFF")
            if not fl:
                return (["Mình chưa có lãi suất được xác nhận nên chưa thể ước tính khoản trả góp."],
                        nums, "HANDOFF")
            nums.append(fl[0]["value"]); _use(fl[0], "loan_rate")
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Lãi suất tham khảo mẫu là {fl[0]['value']}%/năm (SAMPLE). "
                     "Để ước tính khoản trả góp chính xác còn cần giá xe, số tiền trả trước và kỳ hạn vay."],
                    nums, "answer_loan_info")
        if topic == "stock":
            vehicle, variant, color = self._vehicle(req, postcard), _slot(req, "variant"), _slot(req, "color")
            if not vehicle:
                self._ask(req, event, "slot", "vehicle", postcard.get("vehicles") or [],
                          "Bạn hỏi tồn kho xe nào?", now_iso)
                return (["Bạn hỏi tồn kho xe nào ạ?"], nums, "ask_clarification")
            fl, _, sconf = fresh("stock", vehicle=vehicle, variant=variant,
                             location=_slot(req, "location"),
                             extra={"color": color} if color else None)
            if sconf:
                # R07: tồn kho mâu thuẫn (VD vừa 1 vừa 0) thì handoff, không
                # chọn fact đầu tiên để trả "Còn".
                return (["Dữ liệu tồn kho hiện chưa thống nhất nên mình chưa thể xác nhận xe còn hay hết."],
                        nums, "HANDOFF")
            if not fl:
                item = " ".join(part for part in (vehicle, variant, color) if part)
                return ([f"Mình chưa xác nhận được xe {item} còn hay hết vì chưa có dữ liệu "
                         "để kiểm tra tồn kho thực tế."],
                        nums, "HANDOFF")
            # Không hỏi variant khi user không nêu: câu trả lời tồn kho chung
            # không nêu tên phiên bản (variant or ''), chỉ nêu màu khi mọi fact
            # khớp cùng một màu và location của fact đầu tiên — hành vi này giữ
            # nguyên theo contract (không tự chọn phiên bản để khẳng định).
            if all((f["value"] or 0) <= 0 for f in fl):
                # R07: stock bằng 0 không được trả "Còn".
                _use(fl[0], "stock")
                nums.append(0)
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                return ([f"Hiện đã hết {vehicle} {variant or ''} {color or ''} (dữ liệu mẫu SAMPLE).".strip()],
                        nums, "answer_stock")
            _use(fl[0], "stock")
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            # Màu: nếu mọi fact khớp cùng một màu thì nêu màu đó (sự thật từ
            # fact, không suy đoán); khác màu thì giữ "theo kho".
            shared_colors = {f.get("extra", {}).get("color") for f in fl} - {None}
            shown_color = color or (shared_colors.pop() if len(shared_colors) == 1 else "theo kho")
            return ([f"Còn {vehicle} {variant or ''} màu {shown_color} tại "
                     f"{fl[0].get('location') or 'đại lý'} (dữ liệu mẫu SAMPLE).".strip()],
                    nums, "answer_stock")
        if topic == "spec":
            vehicle = self._vehicle(req, postcard)
            if req.get("desired_outcomes") == ["color_recommendation"]:
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                return ([_color_advice(vehicle)], nums, "answer_general_advice")
            if not vehicle:
                self._ask(req, event, "slot", "vehicle", postcard.get("vehicles") or [],
                          "Bạn hỏi thông số xe nào?", now_iso)
                return (["Bạn hỏi thông số xe nào ạ?"], nums, "ask_clarification")
            fl, _, conflict = fresh("spec", vehicle=vehicle)
            if not fl or conflict:
                if "city_driving_suitability" in req.get("desired_outcomes", []):
                    return ([f"Với nhu cầu đi phố, bạn có thể cân nhắc kích thước xe, tầm nhìn và mức tiêu hao. "
                             f"Mình chưa có thông số được xác nhận để kết luận riêng về {vehicle}."],
                            nums, "HANDOFF")
                return ([f"Mình chưa có thông số được xác nhận cho {vehicle} để trả lời chính xác."],
                        nums, "HANDOFF")
            ex = fl[0].get("extra", {})
            nums += [v for v in ex.values() if isinstance(v, (int, float))]
            import re as _re
            for k, v in ex.items():
                if isinstance(v, (int, float)):
                    req["_used_facts"].append(dict(fact_id=fl[0]["id"], vehicle=vehicle, variant=None,
                                                   location=None, attribute=k, value=v, unit=""))
                elif isinstance(v, str):
                    for x in _re.findall(r"\d+(?:[.,]\d+)?", v):
                        num = float(x.replace(",", ".")) if "." in x or "," in x else int(x)
                        nums.append(num)
                        mu = _re.search(r"\d+(?:[.,]\d+)?\s*([A-Za-z]+)\s*$", v)
                        unit = mu.group(1) if mu else ""
                        req["_used_facts"].append(dict(fact_id=fl[0]["id"], vehicle=vehicle, variant=None,
                                                       location=None, attribute=k, value=num, unit=unit))
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Thông số mẫu {vehicle}: " + ", ".join(f"{k} {v}" for k, v in ex.items()) + " (SAMPLE)."],
                    nums, "answer_spec")
        if topic == "dealer":
            loc = _slot(req, "location")
            if not loc:
                self._ask(req, event, "slot", "location", [],
                          "Bạn đang ở khu vực nào (tỉnh/thành phố hoặc quận/huyện)?", now_iso)
                return (["Bạn đang ở khu vực nào (tỉnh/thành phố hoặc quận/huyện) ạ?"], nums, "ask_clarification")
            if getattr(self.facts, "dealer_directory_configured", False):
                matched = self.facts.directory_dealers(loc)
                by_name = {}
                for fact in matched:
                    extra = fact.get("extra") or {}
                    address = dealer_public_address(extra.get("address"))
                    if not extra.get("name") or not address:
                        continue
                    by_name.setdefault(norm(extra["name"]), []).append((fact, address))
                if any(len({norm(address) for _, address in entries}) > 1
                       for entries in by_name.values()):
                    message = ("Danh sách đã nạp có địa chỉ không nhất quán cho cùng một đại lý. "
                               "Hãy kiểm tra tại danh sách Hyundai chính thức: "
                               f"{DEALER_DIRECTORY_URL}. Dữ liệu chưa được xác minh trực tuyến.")
                    return ([message], nums, "HANDOFF")
                entries = [items[0] for _, items in sorted(by_name.items())]
                if not entries:
                    if getattr(self, "dealer_directory_error", None):
                        reason = "Danh sách đại lý trên máy hiện chưa đọc được."
                    else:
                        reason = "Chưa tìm thấy đại lý phù hợp trong danh sách địa chỉ đã nạp."
                    message = (f"{reason} Tra cứu danh sách Hyundai chính thức: "
                               f"{DEALER_DIRECTORY_URL}. Dữ liệu chưa được xác minh trực tuyến.")
                    return ([message], nums, "HANDOFF")
                parts_out = []
                import re as _re
                for fact, address in entries:
                    extra = fact["extra"]
                    _use(fact, "dealer_directory_entry")
                    nums += [int(value) for value in _re.findall(r"\d+", address)]
                    parts_out.append(f"{extra['name']} — {address}")
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                text = ("Mình tìm được các địa chỉ này trong danh sách đại lý đã nạp. "
                        "Dữ liệu chưa được xác minh trực tuyến; bạn kiểm tra cập nhật trước khi ghé nhé:\n" +
                        "\n".join(f"• {part}" for part in parts_out))
                return ([text], nums, "answer_dealer")
            fl, _, conflict = fresh("dealer", location=loc)
            if not fl or conflict:
                return ([f"Chưa tìm thấy đại lý phù hợp trong danh sách đã nạp. "
                         f"Tra cứu danh sách Hyundai chính thức: {DEALER_DIRECTORY_URL}. "
                         "Chưa có kết quả trực tuyến được xác minh."], nums, "HANDOFF")
            _use(fl[0], "dealer")
            ex = fl[0].get("extra", {})
            import re as _re
            for v in ex.values():
                if isinstance(v, str):
                    nums += [float(x.replace(",", ".")) if "." in x or "," in x else int(x)
                             for x in _re.findall(r"\d+(?:[.,]\d+)?", v)]
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Đại lý mẫu: {ex.get('name')} — {ex.get('address')}, giờ mở cửa {ex.get('hours')} (SAMPLE)."],
                    nums, "answer_dealer")
        if topic == "testdrive":
            vehicle, loc, dt = self._vehicle(req, postcard), _slot(req, "location"), _slot(req, "datetime")
            if isinstance(dt, dict) and "range" in dt:
                # Khoảng thời gian (VD cuối tuần này): hỏi chọn ngày cụ thể,
                # không tự chọn một ngày trong khoảng.
                vn = ["thứ 2", "thứ 3", "thứ 4", "thứ 5", "thứ 6", "thứ 7", "chủ nhật"]
                labels = []
                for o in dt["options"]:
                    d = datetime.fromisoformat(o)
                    labels.append(f"{vn[d.weekday()]} {d.day:02d}/{d.month:02d}")
                    # Số trong câu hỏi chọn ngày (ngày/tháng/thứ) đều có nguồn
                    # từ khoảng do khách đưa -> đưa vào allowed để claim check
                    # không chặn câu hỏi do chính engine sinh từ input đó.
                    nums += [d.day, d.month]
                    if d.weekday() < 6: nums.append(d.weekday() + 2)
                self._ask(req, event, "choice", "datetime", labels,
                          f"Cuối tuần này bạn muốn lái thử vào { ' hay '.join(labels)}?", now_iso)
                return ([f"Cuối tuần này bạn muốn lái thử vào {' hay '.join(labels)} ạ?"], nums, "ask_clarification")
            if dt:
                try: datetime.fromisoformat(dt)
                except Exception: dt = None
            # Đề xuất cũ bị thay bằng đề xuất mới khi slot đổi: supersede pending mở.
            if vehicle and loc and dt:
                for pend in self.store.open_pending_for(req["actor_id"], req["id"],
                                                         page_id=req.get("page_id"),
                                                         post_id=req.get("post_id"),
                                                         thread_root_id=req.get("thread_root_id")):
                    if pend["qtype"] == "yes_no":
                        pend["status"] = "SUPERSEDED"; pend["version"] += 1; self.store.update_pending(pend)
            if not vehicle or not loc or not dt:
                missing = "vehicle" if not vehicle else "location" if not loc else "datetime"
                q = {"vehicle": "Bạn muốn lái thử xe nào?", "location": "Bạn muốn lái thử ở khu vực nào?",
                     "datetime": "Bạn muốn lái thử vào thời gian nào?"}[missing]
                self._ask(req, event, "slot", missing, [], q, now_iso)
                return ([q], nums, "ask_clarification")
            # A2: combo (xe, địa điểm, thời gian) này đã từng bị từ chối
            # thì KHÔNG đề xuất lại y hệt — hỏi thời gian khác thay vì lặp
            # vòng hỏi -> từ chối -> hỏi lại đúng đề xuất cũ.
            if _canon_slot_values(req["slots"]) in self.store.rejected_fingerprints(req["id"]):
                q = "Thời gian đó chưa phù hợp, bạn muốn lái thử vào thời gian nào khác?"
                self._ask(req, event, "slot", "datetime", [], q, now_iso)
                return ([q], nums, "ask_clarification")
            try:
                dtf = datetime.fromisoformat(dt); dt_text = dtf.strftime("%d/%m lúc %H:%M")
                nums += [dtf.day, dtf.month, dtf.hour, dtf.minute]
            except Exception:
                dt_text = str(dt)
            prop = f"Xác nhận đề xuất lái thử {vehicle} tại {loc} lúc {dt_text}?"
            p = self._ask(req, event, "yes_no", None, ["yes", "no"], prop, now_iso)
            return ([f"Đã ghi nhận đề xuất lái thử {vehicle} tại {loc} lúc {dt_text}. "
                     "Bạn xác nhận đề xuất này nhé? (Đây là đề xuất, chưa phải lịch đã đặt.)"],
                    nums, "propose_test_drive_slot")
        if topic == "buy":
            if req.get("desired_outcomes") == ["color_recommendation"]:
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                return ([_color_advice(self._vehicle(req, postcard))],
                        nums, "answer_general_advice")
            # Mua/đặt cọc cần người xác nhận cuối — không auto confirm.
            req["status"] = "HANDED_OFF"; req["version"] += 1; self.store.save_request(req)
            self.store.add_handoff(req["id"], "staff", "buy_confirmation", req["version"], now_iso)
            if postcard.get('persona_binding') and self.persona_store:
                snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
                if (snapshot or {}).get('snapshot', {}).get('release', {}).get('style', {}).get('fixture_revision', 0) >= 3:
                    reply = self._peer_reply(event, snapshot, dict(topic='none', canonical_keys=[]), dict(
                        call=call_term(event.get('text', ''), self.store.peer_state(event).get('address_term')),
                        topic='purchase_boundary', hook_field=None, reason='boundary', recent=[]))
                    if reply.get('status') == 'OK': return ([reply['text']], nums, 'HANDOFF')
            return (["Mình chưa thể xác nhận việc mua hoặc đặt cọc qua cuộc trò chuyện này."],
                    nums, "HANDOFF")
        if topic == "service":
            fl, _, conflict = fresh("warranty")
            if _slot(req, "service_topic") == "warranty" and fl and not conflict:
                nums.append(fl[0]["value"]); _use(fl[0], "warranty")
                req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
                return ([f"Thời gian bảo hành mẫu là {fl[0]['value']} năm (SAMPLE)."], nums, "answer_spec")
            return (["Mình chưa có thông tin đã xác nhận cho câu hỏi dịch vụ này."], nums, "HANDOFF")
        if topic == "registration":
            loc = _slot(req, "location")
            if not loc:
                self._ask(req, event, "slot", "location", ["Hà Nội", "TP.HCM"],
                          "Bạn đăng ký xe ở tỉnh/thành nào?", now_iso)
                return (["Bạn đăng ký xe ở tỉnh/thành nào ạ?"], nums, "ask_clarification")
            fl, _, conflict = fresh("registration_fee", location=loc)
            if not fl or conflict:
                return ([f"Phí đăng ký tại {loc} chưa có dữ liệu xác nhận nên mình chưa thể báo chính xác."],
                        nums, "HANDOFF")
            nums.append(fl[0]["value"]); _use(fl[0], "registration_fee")
            req["status"] = "COMPLETED"; req["version"] += 1; self.store.save_request(req)
            return ([f"Phí đăng ký mẫu tại {loc} là {fmt_money(fl[0]['value'])} (SAMPLE, theo tài liệu mẫu)."],
                    nums, "answer_price")
        if topic == "social":
            text = _social_reply(event.get('text'))
            return ([text], nums, 'social_ack') if text else ([], nums, 'REVIEW_UNRESOLVED')
        return (["Mình chưa đủ thông tin để trả lời câu này chính xác."], nums, "HANDOFF")
