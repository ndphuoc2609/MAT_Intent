"""Local, live Luna sandbox. Business decisions and simulated sends belong to Engine."""
from __future__ import annotations

import json
import ipaddress
import hashlib
import os
import sqlite3
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlencode, parse_qs

from app.adapters.luna import MODEL as DEFAULT_MODEL, call_luna
from app.adapters.jev import JevOpenRouter, intent_questions, validate_answers
from app.engine import WallClock
from app.facts import Facts, dealer_public_address
from app.harness import make_engine
from app.persona_fixture import seed_nam_creta, seed_database_persona
from app.persona_store import PersonaStore, migrate_sqlite
from app.renderer import style_from_tone
from app.semantics import attach_context_provenance, validate_semantic
from scripts import budget_ledger
from scripts.run_multiturn_eval import CONTEXT_CHAR_LIMIT, fmt_context

STATIC = Path(__file__).with_name("static")
PROVIDER_LOCK = threading.RLock()
REPAIR_CHAR_LIMIT = 10000
# Conservative bound for the capped prompt and the adapter's current 1,200-token output/rates.
ATTEMPT_COST_RESERVE = 0.05
MODEL = os.environ.get("INTENT_LUNA_MODEL", DEFAULT_MODEL)
JEV_MODEL = JevOpenRouter.model
JEV_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandbox-jev-shadow")
JEV_SLOT = threading.BoundedSemaphore(1)
JEV_TRACE_PATH = Path(__file__).resolve().parents[1] / "runs" / "sandbox_jev_trace.jsonl"
POSTS = [
    {"id": "creta", "page_id": "sample-hyundai", "title": "Hyundai Creta", "text": "Hyundai Creta: tìm hiểu phiên bản Tiêu chuẩn và Cao cấp tại Hà Nội.", "vehicles": ["Creta"]},
    {"id": "tucson", "page_id": "sample-hyundai", "title": "Hyundai Tucson", "text": "Hyundai Tucson Cao cấp: thông tin xe và lái thử tại Hà Nội.", "vehicles": ["Tucson"]},
    {"id": "compare", "page_id": "sample-hyundai", "title": "Creta và Tucson", "text": "Cùng tìm hiểu Hyundai Creta và Tucson. Bạn quan tâm xe nào?", "vehicles": ["Creta", "Tucson"]},
]



def required_text(data, key, maximum=10000):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be non-empty text (maximum {maximum} characters)")
    return value.strip()


class Sandbox:
    def __init__(self, ledger_path=None, dealer_db_path=None, persona_db_path=None, database_url=None):
        self.ledger_path = ledger_path or budget_ledger.DEFAULT_PATH
        self.dealer_db_path = dealer_db_path or os.environ.get("SANDBOX_DEALER_DB")
        self.enforce_budget = os.environ.get("SANDBOX_ENFORCE_BUDGET", "false").strip().lower() in {
            "1", "true", "yes", "on"}
        self.budget_max_calls = int(os.environ.get("INTENT_MODEL_MAX_CALLS") or 300)
        self.budget_max_cost = float(os.environ.get("INTENT_MODEL_MAX_COST_USD") or 2.0)
        self.conversations = {}
        self.lock = PROVIDER_LOCK
        self.jev_lock = threading.Lock()
        self.jev_adapter = JevOpenRouter()
        self.jev_trace_path = str(JEV_TRACE_PATH)
        self.jev_shadow_enabled = os.environ.get("SANDBOX_JEV_SHADOW_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on"}
        self.database_url = database_url or os.environ.get("SANDBOX_DATABASE_URL") or os.environ.get("DB_NEON")
        # A path is accepted only when a test injects an isolated fixture database.
        self.persona_db_path = persona_db_path
        self.persona_conn = None
        self.persona_store = None
        self._database_persona_ids = {}

    def persona_repository(self):
        if self.persona_store is None:
            if self.database_url:
                from app.pg_adapter import connect
                self.persona_conn = connect(self.database_url)
                self.persona_store = PersonaStore(self.persona_conn.conn)
            elif self.persona_db_path:
                Path(self.persona_db_path).parent.mkdir(parents=True, exist_ok=True)
                self.persona_conn = sqlite3.connect(self.persona_db_path, check_same_thread=False, timeout=10)
                migrate_sqlite(self.persona_conn)
                self.persona_store = PersonaStore(self.persona_conn)
            else:
                raise RuntimeError("Set SANDBOX_DATABASE_URL to the Neon PostgreSQL database")
            _, self.fixture_release_id = seed_nam_creta(self.persona_store)
        return self.persona_store

    def personas(self):
        repository = self.persona_repository()
        fixtures = [dict(id=item["id"], name=item["display_name"], slug=item["slug"],
                         source_kind=item["source_kind"], status=item["status"])
                    for item in repository.list_personas()
                    if not item['slug'].startswith(('db_demo_', 'db_persona_'))]
        if not os.environ.get("DB_Host"):
            return fixtures
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(host=os.environ["DB_Host"], port=os.environ.get("DB_Port", "5432"),
                                 dbname="content_ai_fac", user=os.environ.get("DB_Username"),
                                 password=os.environ.get("DB_Password"), connect_timeout=5,
                                 options="-c default_transaction_read_only=on", row_factory=dict_row) as conn:
                personas = []
                for row in conn.execute("SELECT id,name,occupation,car_owned,category,role,tone,perspective,sample_quote "
                                        "FROM public.personas ORDER BY name,id"):
                    source_id = str(row["id"])
                    alias = self._database_persona_ids.setdefault(source_id, uuid.uuid4().hex)
                    personas.append(dict(row, id=alias, _source_persona_id=source_id))
            return personas + fixtures
        except Exception:
            if fixtures:
                return fixtures
            raise

    def generate_post(self, persona, snapshot, _metrics=None, owner_id=None):
        from app.persona_fact_policy import purchase_year, select_facts, validate_proposals
        from app.persona_generation import allowed_generation_facts
        from app.persona_query import KEY_REGISTRY

        started = time.perf_counter()
        owner_id = owner_id or f"post-preview:{uuid.uuid4().hex}"
        reservation = f"sandbox-post:{uuid.uuid4().hex}"
        reservations, phase_calls, errors = [], [], []
        budget_attempt = 0

        def guard():
            nonlocal budget_attempt
            if not self.enforce_budget:
                return True
            budget_attempt += 1
            attempt_id = f"{reservation}:{budget_attempt}"
            allowed, _ = budget_ledger.reserve(
                self.ledger_path, reservation_id=attempt_id, max_calls=self.budget_max_calls,
                max_cost=self.budget_max_cost,
                reserve_cost_usd=ATTEMPT_COST_RESERVE, prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
            if allowed:
                reservations.append(attempt_id)
            return allowed

        def call_phase(phase, prompt, persona_context=None, repair_note=None):
            call_started = time.perf_counter()
            result = call_luna("", prompt, model=MODEL, max_tokens=3200,
                budget_guard=guard, persona_context=persona_context, persona_phase=phase,
                repair_note=repair_note)
            attempts = result.get("attempts", 0)
            known = result.get("cost_unknown") is False
            retry_known = result.get("retry_cost_unknown") is False
            cost = result.get("cost_usd_est") if known else None
            if cost is not None and type(result.get("retry_cost_usd_est")) in (int, float):
                cost += result["retry_cost_usd_est"]
            elif attempts > 1 and not retry_known:
                cost = None
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            trace = {"phase": phase, "attempts": attempts,
                "latency_ms": round((time.perf_counter() - call_started) * 1000, 3),
                "usage": {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                          if type(usage.get(key)) is int and usage[key] >= 0},
                "cost_usd_est": cost, "model": result.get("model_effective", MODEL),
                "error_code": result.get("error_code"), "error": result.get("error")}
            phase_calls.append(trace)
            if not result.get("ok"):
                errors.append({"phase": phase, "error": result.get("error_code") or result.get("error")})
            budget_ledger.record(self.ledger_path, run_id=reservation,
                kind="post_analysis" if phase == "analysis" else "post", attempts=attempts,
                cost_usd_est=cost, model=trace["model"], status=None if result.get("ok") else "failed")
            if self.enforce_budget:
                budget_ledger.settle_reservations(self.ledger_path, reservations)
                reservations.clear()
            return result

        def metrics_update(**fields):
            if _metrics is None:
                return
            attempts = sum(item["attempts"] for item in phase_calls)
            model_costs = [item["cost_usd_est"] for item in phase_calls]
            usage = {}
            for item in phase_calls:
                for key, value in item["usage"].items():
                    usage[key] = usage.get(key, 0) + value
            _metrics.update(fields, phase_calls=list(phase_calls), phase_errors=list(errors),
                attempts=attempts, model_latency_ms=round(sum(item["latency_ms"] for item in phase_calls), 3),
                generation_latency_ms=round((time.perf_counter() - started) * 1000, 3),
                usage=usage, cost_usd_est=sum(model_costs) if all(v is not None for v in model_costs) else None,
                cost_unknown=any(v is None for v in model_costs), model=phase_calls[-1]["model"] if phase_calls else MODEL)

        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise ValueError("Cần cấu hình OPENROUTER_API_KEY để tạo bài viết.")
        persona_id = snapshot.get("persona", {}).get("id")
        release_id = snapshot.get("release", {}).get("id")
        if not persona_id or not release_id:
            raise ValueError("Không có release persona để tạo bài viết.")
        scoped_snapshot = {**snapshot, "persona_uuid": persona_id, "release_id": release_id,
                           "source_kind": snapshot["persona"].get("source_kind")}
        repository = self.persona_repository()

        revoked = repository.revoked_fact_ids(persona_id, release_id)
        scoped_snapshot = repository.attach_generated(scoped_snapshot)
        facts = allowed_generation_facts(scoped_snapshot, revoked_ids=revoked,
            allow_fictional_fixture=True)
        prompt = "Viết một bài chia sẻ Facebook ngắn dưới vai persona đã gắn với bài viết này. " \
                 "Chọn chủ đề phù hợp với dữ kiện hiện có; không biến bài thành lời chào bán."
        analysis = call_phase("analysis", prompt)
        if analysis.get("error_code") == "BUDGET_EXHAUSTED":
            metrics_update(generation_validation={"status": "REVIEW", "reason": "BUDGET_EXHAUSTED"})
            raise ValueError("Ngân sách gọi Luna đã hết; chưa gửi yêu cầu tới OpenRouter.")
        needs = analysis.get("needs") if analysis.get("ok") else None
        selected = select_facts(facts, needs)
        if not selected:
            selected = list(facts)
        topic_keys = {key for values in KEY_REGISTRY.values() for key in values}
        supplied = {fact.get("key") or fact.get("canonical_key") for fact in selected}
        topics = set(needs.get("topics") or ()) if isinstance(needs, dict) else set()
        gaps = sorted(topic for topic in topics if not any(
            (fact.get("key") or fact.get("canonical_key", "")).startswith(f"gen.{topic}.")
            or (fact.get("key") or fact.get("canonical_key")) in topic_keys for fact in selected))
        context = {"persona_uuid": persona_id, "release_id": release_id,
            "persona_description": snapshot.get('release', {}).get('style', {}).get('persona_description', {}),
            "fixture_only": snapshot.get("fixture_only", False),
            "source_kind": snapshot["persona"].get("source_kind"), "facts": selected,
            "needs": needs, "gaps": gaps,
            "voice_guide": snapshot.get("release", {}).get("style", {}).get("voice_guide", {})}
        generated = call_phase("post", prompt, persona_context=context)
        if generated.get("error_code") == "MODEL_PERSONA_POST_SHAPE":
            generated = call_phase("post", prompt, persona_context=context,
                repair_note=f"Lần trước bị validator từ chối: {generated.get('error')}. "
                "Sửa đúng lỗi này. Mỗi claim dùng predicate đúng key của fact được trích; "
                "chỉ dùng gen.<namespace> khi fact_id là proposal_id new1/new2. "
                "fact_ids cấp bài phải bằng hợp các fact_ids trong claims.")

        def record_proposals(candidate):
            proposals = candidate.get("fact_proposals", [])
            refs = set(candidate.get("fact_ids", []))
            for claim in candidate.get("claims", []):
                refs.update(claim.get("fact_ids", []))
            live = repository.live_generated_keys(persona_id)
            created_today = repository.generated_created_today(
                persona_id, datetime.now(timezone.utc).date().isoformat())
            proposal_ids, proposal_results, seen_ids = {}, [], set()
            if not isinstance(proposals, list) or len(proposals) > 2:
                raise ValueError("Số fact proposal của bài vượt giới hạn.")
            for proposal in proposals:
                proposal_id = proposal.get("proposal_id") if isinstance(proposal, dict) else None
                if proposal_id in seen_ids:
                    proposal_results.append({"proposal_id": proposal_id, "status": "REJECTED",
                                             "reason": "GENERATED_FACT_SHAPE"})
                    continue
                seen_ids.add(proposal_id)
                accepted, reason = validate_proposals([proposal], live_keys=live,
                    created_today=created_today, anchors={"purchase_year": purchase_year(facts)},
                    source_text=[])
                if reason or proposal_id not in refs:
                    reason = reason or "GENERATED_FACT_UNREFERENCED"
                    proposal_results.append({"proposal_id": proposal_id, "status": "REJECTED",
                                             "reason": reason})
                    continue
                item = accepted[0]
                try:
                    fact_id = repository.propose_generated(persona_id, item["key"], item["value"],
                        item["precision"], f"luna_generated_post:{owner_id.removeprefix('post:')}",
                        owner_id, datetime.now(timezone.utc).isoformat())
                except Exception:
                    proposal_results.append({"proposal_id": proposal_id, "key": item["key"],
                        "status": "REJECTED", "reason": "GENERATED_FACT_EXISTS" if item["key"] in live
                        else "GENERATED_FACT_STORAGE_FAILED"})
                    continue
                live.add(item["key"]); created_today += 1; proposal_ids[proposal_id] = fact_id
                proposal_results.append({"proposal_id": proposal_id, "fact_id": fact_id,
                    "key": item["key"], "status": "PENDING", "reason": None})
            rejected_ids = {item.get("proposal_id") for item in proposal_results
                            if item["status"] == "REJECTED"}
            candidate = dict(candidate,
                fact_ids=[proposal_ids.get(fid, fid) for fid in candidate["fact_ids"]
                          if fid not in rejected_ids],
                claims=[dict(claim, fact_ids=[proposal_ids.get(fid, fid) for fid in claim["fact_ids"]
                                               if fid not in rejected_ids])
                        for claim in candidate["claims"]])
            return candidate, proposal_results, proposal_ids

        candidate = generated.get("post_candidate") if generated.get("ok") else None
        if isinstance(candidate, dict):
            candidate, proposal_results, proposal_ids = record_proposals(candidate)
        else:
            reason = generated.get("error") or generated.get("error_code") or "MODEL_ERROR"
            metrics_update(generation_validation={"status": "REVIEW", "reason": "POST_GENERATION_FAILED",
                "model_error_code": generated.get("error_code"), "model_error": reason})
            raise ValueError(f"Luna chưa tạo được bài viết có cấu trúc và đủ nguồn ({reason}).")

        metrics_update(post_candidate=candidate, fact_ids=candidate.get("fact_ids", []),
            generated_fact_ids=sorted(proposal_ids.values()), proposal_results=proposal_results,
            generation_validation={"status": "SKIPPED", "reason": "POST_CONTENT_VALIDATION_DISABLED"},
            needs=needs, gaps=gaps,
            facts_supplied=[{key: fact.get(key) for key in
                ("fact_id", "key", "value", "precision", "source_kind") if key in fact} for fact in selected])
        return candidate["post_text"].strip()

    def create(self, data):
        actor = required_text(data, "actor_id", 200)
        self.persona_repository()
        channel = data.get("channel", "public")
        if channel not in ("public", "private"):
            raise ValueError("channel must be public or private")
        persona = None
        fixture_binding = None
        persona_origin = None
        post_owner_id = None
        if data.get("persona_id"):
            persona_id = required_text(data, "persona_id", 50)
            persona = next((p for p in self.personas() if p["id"] == persona_id), None)
            if persona is None:
                raise ValueError("unknown persona_id")
            generation = {}
            if persona.get("source_kind") == "fictional_fixture":
                persona_origin = "fictional_fixture"
                persona = {"id": persona_id, "name": persona["name"], "tone": "warm",
                           "perspective": "người dùng Hyundai Creta trong kịch bản hư cấu",
                           "persona_default": "người dùng chia sẻ trải nghiệm"}
                release = self.persona_repository().get_release_snapshot(persona_id, self.fixture_release_id)
                fixture_binding = {"persona_uuid": persona_id, "release_id": release["release"]["id"]}
            else:
                persona_origin = "database_snapshot"
                repository = self.persona_repository()
                source_persona = dict(persona, id=persona.get("_source_persona_id", persona_id))
                persona_uuid, release_id = seed_database_persona(repository, source_persona)
                fixture_binding = {"persona_uuid": persona_uuid, "release_id": release_id}
                release = repository.get_release_snapshot(persona_uuid, release_id)
            vehicles = [f['value'] for f in release['facts'] if f['key'] == 'vehicle.current.model']
            post_id = uuid.uuid4().hex
            post_owner_id = f"post:{post_id}"
            post = {"id": post_id, "page_id": "sample-hyundai",
                    "title": persona['name'] + (f" · {vehicles[0]}" if vehicles else ''),
                    "text": self.generate_post(persona, release, _metrics=generation,
                                                owner_id=post_owner_id), "vehicles": vehicles,
                    "generation": generation,
                    "persona_id": fixture_binding["persona_uuid"] if fixture_binding else persona_id,
                    "persona_name": persona["name"]}
            if fixture_binding:
                post.update(release_id=fixture_binding["release_id"],
                            persona_source_kind=release['persona']['source_kind'],
                            persona_fixture_only=release['fixture_only'], sample_product_facts=False,
                            persona_origin=persona_origin)
        elif data.get("post_id"):
            post = next((dict(p) for p in POSTS if p["id"] == data["post_id"]), None)
            if post is None:
                raise ValueError("unknown seeded post_id")
        else:
            post = dict(id=uuid.uuid4().hex, page_id=required_text(data, "page_id", 200),
                        text=required_text(data, "post_text"), title="Manual post")
            vehicles = data.get("vehicles", [])
            if not isinstance(vehicles, list) or len(vehicles) > 20 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 100 for v in vehicles):
                raise ValueError("vehicles must be a list of vehicle names")
            post["vehicles"] = [v.strip() for v in vehicles]
        cid = uuid.uuid4().hex
        try:
            if self.database_url:
                from app.pg_adapter import connect
                engine = make_engine(store=connect(self.database_url), clock=WallClock(), facts=Facts(facts=[]))
            else:
                engine = make_engine(db=self.persona_db_path, clock=WallClock(), facts=Facts(facts=[]),
                                     dealer_db_path=self.dealer_db_path)
        except Exception:
            if fixture_binding and post_owner_id:
                with self.persona_repository()._atomic():
                    self.persona_repository().discard_generated(
                        post_owner_id, datetime.now(timezone.utc).isoformat())
            raise
        engine.persona_store = PersonaStore(engine.store.conn)
        engine.allow_persona_fixtures = True
        engine.auto_send_generated = bool(fixture_binding)
        engine.model_led_conversation = bool(fixture_binding) and os.environ.get(
            'SANDBOX_MODEL_LED_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
        engine.response_style = style_from_tone(persona.get("tone") if persona else None)
        conversation = dict(id=cid, page_id=post["page_id"], actor_id=actor, thread_id="root-" + cid,
                            post=post, engine=engine, history=[], counter=0, jev_shadows={},
                            channel=channel, generation_mode="generated" if fixture_binding else "legacy",
                            model_led=engine.model_led_conversation)
        conversation["postcard"] = dict(id=post["id"], page_id=post["page_id"],
                                        text=post["text"], title=post.get("title"),
                                        topic="vehicle", vehicles=post["vehicles"],
                                        persona_default=persona.get("persona_default", "tư vấn viên") if persona else "tư vấn viên")
        if fixture_binding:
            authored_fact_ids = sorted(generation.get('fact_ids', []))
            authored_post = {"text": post["text"], "fact_ids": authored_fact_ids,
                "persona_uuid": fixture_binding["persona_uuid"],
                "release_id": fixture_binding["release_id"]}
            conversation["postcard"].update(persona_authored_post=authored_post,
                persona_authored_fact_ids=authored_fact_ids)
            post["persona_authored_fact_ids"] = authored_fact_ids
            repository = self.persona_repository()
            try:
                if generation.get('generation_validation'):
                    engine.store.audit(post["id"], "persona_authored_post_generated_facts", {
                        "persona_id": fixture_binding["persona_uuid"],
                        "fact_ids": generation.get("fact_ids", []),
                        "proposals": generation.get("proposal_results", []),
                        "validation": generation.get("generation_validation")},
                        datetime.now(timezone.utc).isoformat())
                    engine.store.conn.commit()
                with repository._atomic():
                    repository.bind_post(post["page_id"], post["id"],
                        fixture_binding["persona_uuid"], fixture_binding["release_id"])
                    repository.activate_generated(post_owner_id,
                        generation.get('generated_fact_ids', []), datetime.now(timezone.utc).isoformat())
            except Exception:
                engine.store.conn.rollback()
                with repository._atomic():
                    repository.discard_generated(post_owner_id, datetime.now(timezone.utc).isoformat())
                raise
            conversation["postcard"]["persona_binding"] = fixture_binding
        self.conversations[cid] = conversation
        return self.state(conversation)

    @staticmethod
    def event(conversation, text="", comment_id=None):
        return dict(page_id=conversation["page_id"], actor_id=conversation["actor_id"],
                    post_id=conversation["post"]["id"], thread_root_id=conversation["thread_id"],
                    parent_id=conversation.get("root_comment_id") or conversation["thread_id"],
                    comment_id=comment_id or "preview",
                    actor_role="user", event_type="create", content_version=1, text=text,
                    channel=conversation.get("channel", "public"),
                    generation_mode=conversation.get("generation_mode", "legacy"))

    def context(self, conversation, event):
        engine = conversation["engine"]
        store = getattr(self, "persona_store", None)
        if store is None or not conversation.get("post", {}).get("release_id"):
            return engine.context(event, conversation["postcard"])
        root = store.root_analysis(conversation["page_id"], conversation["post"]["id"],
                                   conversation["thread_id"])
        messages = []
        if root:
            messages.append({"message_id": root["root_comment_id"],
                "page_id": conversation["page_id"], "post_id": conversation["post"]["id"],
                "thread_root_id": conversation["thread_id"], "parent_id": None,
                "actor_id": root["root_actor_id"], "role": "root", "text": root["root_text"],
                "event_time": root.get("created_at"), "semantic": root.get("semantic")})
        return engine.context(event, conversation["postcard"], messages)

    def state(self, conversation, actor_id=None):
        engine = conversation["engine"]
        event = self.event(conversation)
        if actor_id is not None:
            event["actor_id"] = required_text({"actor_id": actor_id}, "actor_id", 200)
        context = self.context(conversation, event)
        persona_release = None
        generated_facts = []
        if self.persona_store and conversation["post"].get("release_id"):
            binding = self.persona_store.get_binding_snapshot(conversation["page_id"], conversation["post"]["id"])
            if binding:
                persona_release = {"persona": binding["snapshot"]["persona"],
                    "release": binding["snapshot"]["release"], "fixture_only": binding["fixture_only"],
                    "source_kind": binding["source_kind"],
                    "facts": [{key: value for key, value in fact.items() if key != "source_ref"}
                              for fact in binding["facts"]]}
                persona_release["persona"] = {key: value for key, value in persona_release["persona"].items()
                                               if key != "slug"}
                generated_facts = [{key: value for key, value in fact.items() if key != "source_ref"}
                                   for fact in self.persona_store.list_generated(binding["persona_uuid"])]
        with engine.store.lock:
            outbox = [dict(dedupe_key=r[0], purpose=r[1], status=r[2]) for r in
                      engine.store.conn.execute("SELECT dedupe_key,purpose,status FROM outbox ORDER BY rowid")]
        return dict(id=conversation["id"], page_id=conversation["page_id"], actor_id=event["actor_id"],
                    thread_id=conversation["thread_id"], post=conversation["post"], history=conversation["history"],
                    channel=conversation.get("channel", "public"),
                    generation_mode=conversation.get("generation_mode", "legacy"),
                    persona_generation_available=callable(getattr(conversation["engine"], "persona_generation_context", None)),
                    active_requests=context["active_requests"], pending=context["pending"],
                    lead_profile=engine.store.lead_profile(event),
                    dealer_directory={"status": "unavailable" if engine.dealer_directory_error else "available",
                        "count": sum(1 for fact in engine.facts.facts
                                     if fact.get("source") == "local_dealer_directory")},
                    persona_release=persona_release,
                    generated_facts=generated_facts,
                    outbox=outbox, fake_sends=engine.sender.sent,
                    sample_facts=bool(conversation["post"].get("sample_product_facts")), simulated_send=True)

    def budget(self, inflight=0):
        if not self.enforce_budget:
            return True, "budget enforcement is disabled; provider spend is not capped"
        return budget_ledger.guard(self.ledger_path, max_calls=self.budget_max_calls,
                                   max_cost=self.budget_max_cost,
                                   prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                                   prior_cost=budget_ledger.SHARED_PRIOR_COST_USD,
                                   est_attempts=inflight + 1, est_cost=ATTEMPT_COST_RESERVE * (inflight + 1))

    def close(self):
        for conversation in self.conversations.values():
            conversation['engine'].store.conn.close()
        if self.persona_conn:
            getattr(self.persona_conn, 'conn', self.persona_conn).close()

    @staticmethod
    def prior_customer_turns(conversation, text, existing=()):
        existing = set(existing)
        return list(dict.fromkeys(turn["text"] for turn in conversation["history"]
                                  if isinstance(turn.get("text"), str) and turn["text"].strip()
                                  and turn.get("actor_id", conversation["actor_id"]) == conversation["actor_id"]
                                  and turn["text"] != text and turn["text"] not in existing))[-5:]

    def model(self, conversation, text, context, repair_note="", max_tokens=1200,
              persona_context=None, persona_phase=None, schema_version=None):
        existing = [row["text"] for row in context["recent"]] + context["parent_texts"]
        customer_turns = self.prior_customer_turns(conversation, text, existing)
        replies = [turn["response_text"] for turn in conversation["history"]
                   if turn.get("actor_id", conversation["actor_id"]) == conversation["actor_id"] and
                   turn.get("outbound_permission") is True and
                   any(send.get("status") == "sent_to_fake_ledger" for send in turn.get("sends", [])
                       if isinstance(send, dict)) and
                   isinstance(turn.get("response_text"), str)][-5:]
        if getattr(self, "persona_store", None) and conversation.get("post", {}).get("release_id"):
            root = self.persona_store.root_analysis(conversation["page_id"], conversation["post"]["id"],
                                                    conversation["thread_id"])
            if root and root.get("root_actor_id") == conversation["actor_id"] and root.get("reply_text"):
                replies = [root["reply_text"], *replies][-5:]
        model_context = fmt_context({**context, "recent_bot_replies": replies},
                                    prior_customer_turns=customer_turns)
        if persona_context is not None or persona_phase is not None:
            model_context_obj = json.loads(model_context)
            model_context_obj["channel"] = conversation.get("channel", "public")
            model_context = json.dumps(model_context_obj, ensure_ascii=False, separators=(",", ":"))
        jev_shadow_event_id = self.start_jev_shadow(conversation, f"{conversation['id']}-{conversation['counter']}",
                                                    model_context)
        reservations = []
        def guard():
            if not self.enforce_budget:
                return True
            reservation_id = f"sandbox:{conversation['id']}:{uuid.uuid4().hex}"
            allowed, _ = budget_ledger.reserve(
                self.ledger_path, reservation_id=reservation_id,
                max_calls=self.budget_max_calls, max_cost=self.budget_max_cost,
                reserve_cost_usd=ATTEMPT_COST_RESERVE,
                prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
            if allowed: reservations.append(reservation_id)
            return allowed
        model_started = time.perf_counter()
        model_args = dict(parent="", context=model_context,
                          repair_note=repair_note[-REPAIR_CHAR_LIMIT:], budget_guard=guard,
                          model=MODEL, max_tokens=max_tokens)
        if persona_context is not None:
            model_args["persona_context"] = persona_context
        if persona_phase is not None:
            model_args["persona_phase"] = persona_phase
        if schema_version is not None:
            model_args["schema_version"] = schema_version
        result = call_luna(conversation["post"]["text"], text, **model_args)
        result["model_latency_ms"] = (time.perf_counter() - model_started) * 1000
        result["jev_shadow_event_id"] = jev_shadow_event_id
        attempts = result.get("attempts", 0)
        if attempts:
            cost = result.get("cost_usd_est") if not result.get("cost_unknown", True) else None
            # Retries before a successful response have no reported usage.
            if result.get("ok") and attempts > 1:
                retry_unknown = result.get("retry_cost_unknown", True)
                budget_ledger.record(self.ledger_path, run_id=conversation["id"], kind="retry",
                                     attempts=attempts - 1,
                                     cost_usd_est=None if retry_unknown else result.get("retry_cost_usd_est"),
                                     model=MODEL, status="failed")
                attempts = 1
            budget_ledger.record(self.ledger_path, run_id=conversation["id"],
                                 kind="repair" if repair_note else "call", attempts=attempts,
                                 cost_usd_est=cost, model=result.get("model_effective", MODEL),
                                 status=None if result.get("ok") else "failed")
            budget_ledger.settle_reservations(self.ledger_path, reservations)
        return result

    @staticmethod
    def _persona_call_trace(phase, result):
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                  if type(usage.get(key)) is int and usage[key] >= 0}
        if "total_tokens" not in counts and all(key in counts for key in ("prompt_tokens", "completion_tokens")):
            counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
        trace = {"phase": phase, "latency_ms": round(result.get("model_latency_ms", 0.0) or 0.0, 3),
                 "attempts": result.get("attempts", 0), "usage": counts,
                 "cost_usd_est": result.get("cost_usd_est") if not result.get("cost_unknown", True) else None,
                 "validation": "not_run", "validation_errors": []}
        for key in ("error_code", "finish_reason", "schema_version", "prompt_version"):
            if result.get(key) is not None:
                trace[key] = result[key]
        reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens") if isinstance(
            usage.get("completion_tokens_details"), dict) else None
        if type(reasoning) is int and reasoning >= 0:
            trace["reasoning_tokens"] = reasoning
        return trace

    @staticmethod
    def _combine_persona_calls(calls, **values):
        usage, attempts, cost, cost_unknown, latency = {}, 0, 0.0, False, 0.0
        for _, result in calls:
            raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if type(raw_usage.get(key)) is int and raw_usage[key] >= 0:
                    usage[key] = usage.get(key, 0) + raw_usage[key]
            attempts += result.get("attempts", 0) if type(result.get("attempts")) is int else 0
            latency += result.get("model_latency_ms", 0.0) or 0.0
            cost_unknown |= bool(result.get("attempts", 0) and result.get("cost_unknown", True))
            if not result.get("cost_unknown", True) and type(result.get("cost_usd_est")) in (int, float):
                cost += result["cost_usd_est"]
        if "total_tokens" not in usage and all(key in usage for key in ("prompt_tokens", "completion_tokens")):
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        latest = calls[-1][1] if calls else {}
        retry_attempts = sum(max(0, (result.get("attempts", 0) if type(result.get("attempts")) is int else 0) - 1)
                              for _, result in calls)
        combined = dict(ok=True, attempts=attempts, retry_attempts=retry_attempts,
            usage=usage, cost_unknown=cost_unknown,
            cost_usd_est=None if cost_unknown else cost, model_latency_ms=latency,
            model_effective=latest.get("model_effective", MODEL),
            jev_shadow_event_id=latest.get("jev_shadow_event_id"),
            phase_calls=[Sandbox._persona_call_trace(phase, result) for phase, result in calls])
        for key in ("semantic", "reply_candidate", "diagnostics", "peer_signals", "needs", "conversation_plan",
                    "generation_context", "phase_errors", "error", "error_code", "finish_reason"):
            if key in values:
                combined[key] = values[key]
        return combined

    def _model_led_turn(self, conversation, event, context, max_tokens):
        from app.adapters.luna import call_luna_tool
        from app.model_led_pipeline import run_model_led
        reservations, calls = [], []

        def guard():
            reservation = f"model-led:{conversation['id']}:{uuid.uuid4().hex}"
            allowed, _ = budget_ledger.reserve(self.ledger_path, reservation_id=reservation,
                max_calls=self.budget_max_calls, max_cost=self.budget_max_cost,
                reserve_cost_usd=ATTEMPT_COST_RESERVE,
                prior_calls=budget_ledger.SHARED_PRIOR_CALLS, prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
            if allowed:
                reservations.append(reservation)
            return allowed

        def model(public_context, transcript, schema, timeout):
            started = time.perf_counter()
            result = call_luna_tool(public_context, transcript, schema, timeout,
                budget_guard=lambda: True, model=MODEL, max_tokens=max_tokens)
            result['model_latency_ms'] = (time.perf_counter() - started) * 1000
            calls.append(('tool', result))
            cost = result.get('cost_usd_est') if not result.get('cost_unknown', True) else None
            budget_ledger.record(self.ledger_path, run_id=conversation['id'], kind='model_led',
                attempts=result.get('attempts', 0), cost_usd_est=cost, model=MODEL,
                status=None if result.get('ok') else 'failed')
            budget_ledger.settle_reservations(self.ledger_path, reservations)
            reservations.clear()
            return result

        def mutation_guard(operation):
            with conversation['engine'].store.lock:
                with conversation['engine'].persona_store._atomic():
                    return operation()

        outcome = run_model_led(conversation['engine'], event, conversation['postcard'], context,
            model, guard, mutation_guard=mutation_guard, bot_history=conversation['history'][-5:])
        final = outcome.get('final') or {}
        values = dict(semantic=final.get('semantic'), reply_candidate=final.get('reply_candidate'),
            peer_signals=final.get('peer_signals'), diagnostics=final.get('diagnostics'),
            conversation_plan=final.get('conversation_plan') or outcome.get('plan'),
            generation_context=outcome.get('generation_context'),
            phase_errors=[] if outcome.get('status') == 'FINAL' else
                [{'phase': 'tool_loop', 'error': outcome.get('reason')}])
        combined = self._combine_persona_calls(calls, **values)
        combined['tool_trace'] = outcome.get('trace', [])
        return combined

    def _persona_two_phase(self, conversation, event, context, repair_note, max_tokens):
        calls = []
        analysis = self.model(conversation, event["text"], context, repair_note,
            max_tokens=max_tokens, persona_phase="analysis")
        calls.append(("analysis", analysis))
        if analysis.get("error_code") == "MODEL_OUTPUT_TRUNCATED":
            analysis = self.model(conversation, event["text"], context,
                "Lần trước JSON bị cắt; chỉ trả JSON đúng schema, súc tích.",
                max_tokens=max(3200, max_tokens * 2), persona_phase="analysis")
            calls.append(("analysis_retry", analysis))
        phase_errors = []
        if not analysis.get("ok"):
            phase_errors.append({"phase": "analysis", "error_code": analysis.get("error_code"),
                                 "error": analysis.get("error")})
            if analysis.get("error_code") == "BUDGET_EXHAUSTED":
                combined = self._combine_persona_calls(calls, ok=False,
                    error=analysis.get("error"), error_code="BUDGET_EXHAUSTED",
                    phase_errors=phase_errors)
                combined["ok"] = False
                return combined
            legacy = self.model(conversation, event["text"], context, repair_note,
                                max_tokens=max_tokens, schema_version="2")
            calls.append(("fallback_semantic", legacy))
            if not legacy.get("ok"):
                combined = self._combine_persona_calls(calls, ok=False, error=legacy.get("error"),
                    error_code=legacy.get("error_code"), phase_errors=phase_errors)
                combined["ok"] = False
                return combined
            semantic = legacy.get("semantic")
            context_builder = getattr(conversation["engine"], "persona_generation_context", None)
            try:
                generation_context = context_builder(event, conversation["postcard"], None) if callable(context_builder) else None
            except Exception as exc:
                generation_context = None
                phase_errors.append({"phase": "context", "error": type(exc).__name__})
            return self._combine_persona_calls(calls, semantic=semantic, reply_candidate=None,
                peer_signals=None, diagnostics=None, needs=None, generation_context=generation_context,
                phase_errors=phase_errors)

        semantic = analysis.get("semantic")
        checked_semantic = attach_context_provenance(semantic, event, context, conversation["postcard"])
        semantic_errors = validate_semantic(checked_semantic, event["text"],
            [conversation["post"]["text"]] + [row["text"] for row in context["recent"]] + context["parent_texts"] +
            self.prior_customer_turns(conversation, event["text"],
                [row["text"] for row in context["recent"]] + context["parent_texts"]))
        calls[0][1]["semantic_validation"] = semantic_errors
        if semantic_errors:
            return self._combine_persona_calls(calls, semantic=semantic, reply_candidate=None,
                diagnostics=analysis.get("diagnostics"), peer_signals=analysis.get("peer_signals"),
                needs=analysis.get("needs"), phase_errors=phase_errors)
        context_builder = getattr(conversation["engine"], "persona_generation_context", None)
        try:
            generation_context = context_builder(event, conversation["postcard"], analysis.get("needs")) \
                if callable(context_builder) else None
        except Exception as exc:
            generation_context = None
            phase_errors.append({"phase": "context", "error": type(exc).__name__})
        if not isinstance(generation_context, dict):
            phase_errors.append({"phase": "context", "error": "persona_generation_context_unavailable"})
            return self._combine_persona_calls(calls, semantic=semantic, reply_candidate=None,
                diagnostics=analysis.get("diagnostics"), peer_signals=analysis.get("peer_signals"),
                needs=analysis.get("needs"), generation_context=None, phase_errors=phase_errors)
        reply_context = dict(generation_context, phase_one_analysis={
            "semantic": semantic, "needs": analysis.get("needs")})
        reply = self.model(conversation, event["text"], context, "", max_tokens=max_tokens,
                           persona_context=reply_context, persona_phase="reply")
        calls.append(("reply", reply))
        if reply.get("error_code") == "MODEL_OUTPUT_TRUNCATED":
            reply = self.model(conversation, event["text"], context,
                "Lần trước JSON bị cắt; chỉ trả lời ngắn, giữ nguyên semantic.",
                max_tokens=max(3200, max_tokens * 2), persona_context=reply_context,
                persona_phase="reply")
            calls.append(("reply_retry", reply))
        if not reply.get("ok"):
            phase_errors.append({"phase": "reply", "error_code": reply.get("error_code"),
                                 "error": reply.get("error")})
        return self._combine_persona_calls(calls, semantic=semantic,
            reply_candidate=reply.get("reply_candidate") if reply.get("ok") else None,
            diagnostics=analysis.get("diagnostics"), peer_signals=analysis.get("peer_signals"),
            needs=analysis.get("needs"), generation_context=generation_context, phase_errors=phase_errors)

    def start_jev_shadow(self, conversation, event_id, context):
        """Start one optional sandbox-only shadow call; never affects Luna or policy."""
        if not self.jev_shadow_enabled:
            return None
        key = event_id
        event_hash = hashlib.sha256("\x1f".join((conversation["page_id"], conversation["post"]["id"],
            conversation["thread_id"], conversation["actor_id"], event_id)).encode()).hexdigest()
        with self.jev_lock:
            if key in conversation["jev_shadows"]:
                return key
            if not JEV_SLOT.acquire(blocking=False):
                record = {"status": "skipped", "reason": "shadow capacity is busy"}
                conversation["jev_shadows"][key] = record
                return key
            conversation["jev_shadows"][key] = {"status": "pending"}
            while len(conversation["jev_shadows"]) > 100:
                old_key = next((item for item, item_result in conversation["jev_shadows"].items()
                                if item != key and item_result.get("status") != "pending"), None)
                if old_key is None:
                    break
                del conversation["jev_shadows"][old_key]

        def run():
            started = time.perf_counter()
            reservations = []
            result = None
            ledger_recorded = False
            guard_granted = False
            attempts, effective, safe_usage, cost, cost_known, answers = 0, JEV_MODEL, {}, None, False, {}
            try:
                def guard():
                    nonlocal guard_granted
                    if not self.enforce_budget:
                        guard_granted = True
                        return True
                    reservation_id = f"sandbox-jev:{conversation['id']}:{event_id}:{uuid.uuid4().hex}"
                    allowed, _ = budget_ledger.reserve(
                        self.ledger_path, reservation_id=reservation_id,
                        max_calls=self.budget_max_calls, max_cost=self.budget_max_cost,
                        reserve_cost_usd=ATTEMPT_COST_RESERVE,
                        prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                        prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
                    if allowed:
                        reservations.append(reservation_id)
                        guard_granted = True
                    return allowed

                questions = intent_questions()
                result = self.jev_adapter.decide({"context": context}, questions, timeout=10,
                                                 budget_guard=guard)
                if not isinstance(result, dict):
                    result = {"ok": False, "attempts": 1, "cost_unknown": True}
                attempts = result.get("attempts") if type(result.get("attempts")) is int else 1
                if attempts < 0:
                    attempts = 1
                effective = result.get("model_effective")
                if not isinstance(effective, str) or not effective.strip():
                    effective = JEV_MODEL
                effective = effective.strip()[:200]
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                safe_usage = {name: usage[name] for name in ("input_tokens", "output_tokens", "total_tokens")
                              if type(usage.get(name)) is int and usage[name] >= 0}
                latency = result.get("latency_ms")
                if type(latency) not in (int, float) or latency < 0 or not float("inf") > latency:
                    latency = (time.perf_counter() - started) * 1000
                cost = result.get("cost_usd_est")
                cost_known = (type(cost) in (int, float) and cost >= 0 and float("inf") > cost and
                              not result.get("cost_unknown", True))
                if attempts:
                    budget_ledger.record(self.ledger_path, run_id=f"jev:{conversation['id']}:{event_id}",
                        kind="shadow", attempts=attempts, cost_usd_est=float(cost) if cost_known else None,
                        model=effective, status=None if result.get("ok") else "failed")
                    ledger_recorded = True
                answers = validate_answers(result.get("answers"), questions) if result.get("ok") else {}
                error_code = result.get("error_code")
                if error_code not in {"MODEL_BLOCKED", "HTTP_401", "HTTP_402", "HTTP_403", "HTTP_404",
                                     "HTTP_429", "PROVIDER_ERROR", "TIMEOUT", "INVALID_RESPONSE"}:
                    error_code = "PROVIDER_ERROR" if not result.get("ok") else None
                http_status = result.get("http_status")
                if type(http_status) is not int or not 100 <= http_status <= 599:
                    http_status = None
                record = {"status": "complete" if result.get("ok") else "failed",
                    "model_requested": JEV_MODEL, "model_effective": effective,
                    "question_version": "jev_intent_questions_v1",
                    "predictions": {name: float(answer["noul"]) for name, answer in answers.items()},
                    "latency_ms": round(float(latency), 3), "attempts": attempts,
                    "usage": safe_usage, "cost_usd_est": float(cost) if cost_known else None,
                    "cost_unknown": not cost_known,
                    "error_code": error_code, "http_status": http_status,
                    "error": None if result.get("ok") else "Jev shadow unavailable; check provider configuration"}
            except Exception:
                if guard_granted and not ledger_recorded:
                    try:
                        budget_ledger.record(self.ledger_path,
                            run_id=f"jev:{conversation['id']}:{event_id}", kind="shadow",
                            attempts=1, cost_usd_est=None, model=JEV_MODEL, status="failed")
                        ledger_recorded = True
                    except Exception:
                        pass
                record = {"status": "failed", "model_requested": JEV_MODEL,
                    "model_effective": effective, "attempts": attempts, "usage": safe_usage,
                    "predictions": {}, "cost_usd_est": float(cost) if cost_known else None,
                    "cost_unknown": not cost_known,
                    "question_version": "jev_intent_questions_v1",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": "Jev shadow failed validation or provider request"}
            finally:
                if reservations and (ledger_recorded or isinstance(result, dict) and
                                     result.get("attempts") == 0):
                    try:
                        budget_ledger.settle_reservations(self.ledger_path, reservations)
                    except Exception:
                        pass
                try:
                    from scripts.model_trace import append
                    append(self.jev_trace_path, {"event_hash": event_hash, "adapter": "jev_decisions",
                        "model_requested": record.get("model_requested", JEV_MODEL),
                        "model_effective": record.get("model_effective", JEV_MODEL),
                        "provider": result.get("provider") if isinstance(result, dict) else None,
                        "schema_version": "jev_decisions_v1",
                        "prompt_version": "jev_intent_questions_v1",
                        "question_version": record.get("question_version", "jev_intent_questions_v1"),
                        "question_keys": sorted(intent_questions()),
                        "decisions": {name: {"type": "noul", "probability": probability}
                                      for name, probability in record.get("predictions", {}).items()},
                        "usage": record.get("usage", {}), "cost_usd": record.get("cost_usd_est"),
                        "cost_unknown": record.get("cost_unknown", True), "latency_ms": record.get("latency_ms"),
                        "attempts": record.get("attempts", 0), "shadow": True,
                        "review_reasons": [record["error_code"]] if record.get("error_code") else [],
                        "routing": ["SHADOW_COMPLETED" if record.get("status") == "complete" else "SHADOW_FAILED"]})
                    record["trace_status"] = "saved"
                except Exception:
                    record["trace_status"] = "failed"
                    record["trace_error"] = "Could not persist Jev shadow trace"
                record["event_hash"] = event_hash
                with self.jev_lock:
                    conversation["jev_shadows"][key] = record
                JEV_SLOT.release()

        try:
            JEV_POOL.submit(run)
        except RuntimeError:
            with self.jev_lock:
                conversation["jev_shadows"][key] = {"status": "skipped", "reason": "worker unavailable"}
            JEV_SLOT.release()
        return key

    def turn(self, conversation, data):
        started = time.perf_counter()
        text = required_text(data, "text")
        if "actor_id" in data:
            conversation["actor_id"] = required_text(data, "actor_id", 200)
        conversation["counter"] += 1
        event = self.event(conversation, text, f"{conversation['id']}-{conversation['counter']}")
        engine = conversation["engine"]
        context = self.context(conversation, event)
        generation_context = None
        generation_context_error = None
        generation_context_builder = getattr(engine, "persona_generation_context", None)
        two_phase = conversation.get("generation_mode") == "generated"
        if two_phase:
            if not callable(generation_context_builder):
                generation_context_error = "persona_generation_context_unavailable"
        engine_processed = False
        turn = dict(comment_id=event["comment_id"], text=text, actor_id=event["actor_id"], ok=False, semantic=None, routing=[], response_text=None,
                    sends=[], errors=[], attempts=0, cost_usd_est=0.0, cost_unknown=False,
                    model=MODEL, model_latency_ms=0.0, usage={}, usage_complete=True, model_calls=[])
        allowed, reason = self.budget()
        if generation_context_error:
            turn.update(error="Persona generation is unavailable for this conversation.",
                        generation_validation={"status": "REVIEW", "reason": generation_context_error},
                        routing=["REVIEW_GENERATION_CONTEXT"])
            engine_processed = True
        elif not allowed:
            turn.update(error=reason, routing=["BLOCKED_BUDGET"])
        else:
            truncated = False
            for iteration in range(2):
                if iteration:
                    allowed, reason = self.budget()
                    if not allowed:
                        turn.update(error=reason, routing=["BLOCKED_BUDGET"])
                        break
                note = ("Lần trước JSON bị cắt; chỉ trả JSON đúng schema, súc tích."
                        if truncated else "; ".join(turn["errors"]) if iteration else "")
                token_cap = (3200 if two_phase else 1800) if truncated else 1200
                if two_phase:
                    api = (self._model_led_turn(conversation, event, context, token_cap)
                           if conversation.get('model_led') else
                           self._persona_two_phase(conversation, event, context, note, token_cap))
                    generation_context = api.pop("generation_context", None)
                    if 'tool_trace' in api:
                        turn['tool_trace'] = api['tool_trace']
                    if api.get("phase_errors"):
                        turn["phase_errors"] = list(api["phase_errors"])
                    if isinstance(generation_context, dict):
                        turn["persona_needs"] = generation_context.get("needs")
                        turn["persona_gaps"] = generation_context.get("gaps", [])
                        turn["persona_facts_supplied"] = [{key: fact.get(key) for key in
                            ("fact_id", "key", "value", "precision", "source_kind") if key in fact}
                            for fact in generation_context.get("facts", [])]
                else:
                    api = self.model(conversation, text, context, note,
                                     max_tokens=token_cap, persona_context=generation_context)
                turn["model_latency_ms"] += api.get("model_latency_ms", 0.0) or 0.0
                usage = api.get("usage") if isinstance(api.get("usage"), dict) else {}
                attempts = api.get("attempts", 0)
                retry_attempts = api.get("retry_attempts", max(0, attempts - 1))
                if attempts and (retry_attempts > 0 or any(type(usage.get(key)) is not int or usage[key] < 0
                                                     for key in ("prompt_tokens", "completion_tokens"))):
                    turn["usage_complete"] = False
                call_usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                              if type(usage.get(key)) is int and usage[key] >= 0}
                if "total_tokens" not in call_usage and all(
                        key in call_usage for key in ("prompt_tokens", "completion_tokens")):
                    call_usage["total_tokens"] = call_usage["prompt_tokens"] + call_usage["completion_tokens"]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if key in call_usage:
                        turn["usage"][key] = turn["usage"].get(key, 0) + call_usage[key]
                call_trace = {"phase": "repair" if iteration else "initial",
                    "latency_ms": round(api.get("model_latency_ms", 0.0) or 0.0, 3),
                    "attempts": attempts, "usage": dict(call_usage),
                    "cost_usd_est": api.get("cost_usd_est") if not api.get("cost_unknown", True) else None,
                    "validation": "not_run", "validation_errors": []}
                usage_details = usage.get("completion_tokens_details")
                reasoning = usage_details.get("reasoning_tokens") if isinstance(usage_details, dict) else None
                if type(reasoning) is int and reasoning >= 0:
                    call_trace["reasoning_tokens"] = reasoning
                if api.get("error_code") in {"MODEL_OUTPUT_TRUNCATED", "MODEL_OUTPUT_MISSING",
                                              "MODEL_OUTPUT_SHAPE", "MODEL_OUTPUT_INVALID_JSON",
                                              "MODEL_RESPONSE_SHAPE", "MODEL_REFUSAL", "MODEL_SEMANTIC_SHAPE"}:
                    call_trace["error_code"] = api["error_code"]
                    turn["model_error_code"] = api["error_code"]
                if api.get("finish_reason") in {"length", "stop", "content_filter", "refusal", "tool_calls"}:
                    call_trace["finish_reason"] = api["finish_reason"]
                if api.get("phase_calls"):
                    turn["model_calls"].extend(api["phase_calls"])
                else:
                    turn["model_calls"].append(call_trace)
                turn["model"] = api.get("model_effective", MODEL)
                turn["jev_shadow_event_id"] = api.get("jev_shadow_event_id")
                turn["diagnostics"] = api.get("diagnostics")
                turn["peer_signals"] = api.get("peer_signals")
                turn["attempts"] += attempts
                if attempts:
                    unknown = api.get("cost_unknown", True) or (api.get("ok") and retry_attempts > 0)
                    turn["cost_unknown"] |= unknown
                    turn["cost_usd_est"] = (None if turn["cost_unknown"] else
                                             turn["cost_usd_est"] + api.get("cost_usd_est", 0))
                if not api.get("ok"):
                    if (iteration == 0 and api.get("error_code") == "MODEL_OUTPUT_TRUNCATED"
                            and api.get("finish_reason") == "length"):
                        truncated = True
                        continue
                    turn.update(error=api.get("error") or "Model request failed", routing=["REVIEW_MODEL_ERROR"])
                    break
                turn.pop("model_error_code", None)
                candidate = api.get("reply_candidate")
                turn["semantic"] = attach_context_provenance(api.get("semantic"), event, context,
                                                               conversation["postcard"])
                turn["errors"] = validate_semantic(turn["semantic"], text,
                    [conversation["post"]["text"]] + [r["text"] for r in context["recent"]] + context["parent_texts"] +
                    self.prior_customer_turns(conversation, text,
                        [r["text"] for r in context["recent"]] + context["parent_texts"]))
                call_trace["validation"] = "failed" if turn["errors"] else "passed"
                call_trace["validation_errors"] = list(turn["errors"])
                if api.get("phase_calls"):
                    for phase_call in api["phase_calls"]:
                        if phase_call.get("phase") == "analysis":
                            phase_call["validation"] = call_trace["validation"]
                            phase_call["validation_errors"] = list(turn["errors"])
                if turn["errors"]:
                    turn.update(error="Model semantics failed validation", routing=["REVIEW_SCHEMA"])
                    continue
                process_args = dict(expected_state_token=context["state_token"],
                                    trusted_context_messages=context["messages"])
                if generation_context is not None:
                    process_args.update(reply_candidate=api.get("reply_candidate"),
                                        generation_context=generation_context,
                                        peer_signals=api.get("peer_signals"),
                                        conversation_plan=api.get("conversation_plan"))
                result = engine.process(event, conversation["postcard"], turn["semantic"], **process_args)
                engine_processed = not any(route.startswith("ERROR_") for route in result["routing"])
                turn.update({k: result[k] for k in ("routing", "response_text", "candidate_text", "sends", "errors",
                    "outbound_permission", "state_changes", "proposal_results", "conversation_plan") if k in result})
                turn["response_plan"] = result.get("response_plan")
                for key in ("response_kind", "generation_validation"):
                    if key in result:
                        turn[key] = result[key]
                turn["dealer_maps"] = []
                if result.get("outbound_permission") and "answer_dealer" in result["routing"]:
                    used = set((result.get("response_plan") or {}).get("facts_used") or [])
                    for fact in engine.facts.facts:
                        if fact.get("id") not in used or fact.get("source") != "local_dealer_directory":
                            continue
                        extra = fact.get("extra") or {}
                        name, address = extra.get("name"), dealer_public_address(extra.get("address"))
                        if isinstance(name, str) and name and address:
                            query = urlencode({"api": "1", "query": f"{name}, {address}"})
                            turn["dealer_maps"].append({"name": name, "url": f"https://www.google.com/maps/search/?{query}"})
                turn["ok"] = not any(route.startswith(("REVIEW", "BLOCKED", "ERROR_")) for route in turn["routing"])
                turn.pop("error", None)
                if result.get("semantic") is not None and not conversation.get("root_comment_id"):
                    conversation["root_comment_id"] = event["comment_id"]
                    if getattr(self, "persona_store", None) and conversation["post"].get("release_id"):
                        binding = self.persona_store.get_binding_snapshot(
                            conversation["page_id"], conversation["post"]["id"])
                        if binding:
                            metadata = {"source_kind": binding.get("source_kind"),
                                "release_checksum": binding.get("snapshot", {}).get("release", {}).get("checksum"),
                                "fixture_only": bool(binding.get("fixture_only"))}
                            self.persona_store.record_root_analysis(
                                conversation["page_id"], conversation["post"]["id"], conversation["thread_id"],
                                event["comment_id"], event["actor_id"], result["semantic"],
                                turn.get("response_text") if turn.get("outbound_permission") else None,
                                persona_snapshot=metadata, persona_id=binding["persona_id"],
                                release_id=binding["release_id"], root_text=event["text"],
                                tags=result.get("tags") or [], model_version=turn.get("model"),
                                schema_version=result.get("semantic_version"), source_kind="sandbox")
                break
        if not engine_processed:
            engine.process(event, conversation["postcard"], None)
        if two_phase:
            supplied = generation_context.get("facts", []) if isinstance(generation_context, dict) else []
            plan = turn.get("response_plan") or {}
            reason = turn.get("generation_validation")
            if reason in (None, "OK"):
                reason = turn.get("error") or (turn.get("errors") or [None])[0]
            with engine.store.lock:
                engine.store.audit(event["comment_id"], "persona_generated_turn", {
                    "persona_id": (generation_context or {}).get("persona_uuid") or conversation["post"].get("persona_id"),
                    "fact_ids_supplied": [fact.get("fact_id") for fact in supplied
                                          if isinstance(fact, dict) and fact.get("fact_id")],
                    "fact_ids_used": plan.get("fact_ids", []),
                    "proposals": turn.get("proposal_results", []),
                    "validation": turn.get("generation_validation") or ("SEMANTIC_REVIEW" if reason else "NOT_APPLICABLE"),
                    "reason": reason,
                    "routing": turn.get("routing", []),
                }, datetime.now(timezone.utc).isoformat())
                engine.store.conn.commit()
        turn["effects"] = [dict(purpose=row[0], status=row[1],
                                text=json.loads(row[2]).get("text") if row[0] in ("handoff_draft", "lead_draft") else None)
                           for row in engine.store.conn.execute(
            "SELECT purpose,status,payload FROM outbox WHERE json_extract(payload, '$.comment_id')=? OR dedupe_key LIKE ? ORDER BY rowid",
            (event["comment_id"], event["comment_id"] + ":%")).fetchall()]
        conversation["history"].append(turn)
        turn["model_latency_ms"] = round(turn["model_latency_ms"], 3)
        response = dict(self.state(conversation), turn=turn)
        turn["pipeline_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def local_host(self):
        try:
            host = urlsplit("//" + self.headers.get("Host", "").strip())
        except ValueError:
            return False
        name = (host.hostname or "").rstrip(".").lower()
        if not host.netloc or host.path or host.query or host.fragment or host.username or host.password:
            return False
        if name == "localhost" or name.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self.local_host():
            self.reply(403, dict(error="Local host required; open the app with localhost or 127.0.0.1"))
            return
        path = urlsplit(self.path).path
        if path == "/health":
            try:
                budget_ok, budget_reason = self.server.app.budget()
            except Exception:
                budget_ok, budget_reason = False, "budget ledger unavailable"
            self.reply(200, dict(ok=True, model=MODEL, configured=bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
                                 budget_ok=budget_ok, budget_reason=None if budget_ok else budget_reason,
                                 budget_enforced=self.server.app.enforce_budget,
                                 jev_shadow_enabled=self.server.app.jev_shadow_enabled,
                                 budget_warning=None if self.server.app.enforce_budget else
                                 "Budget enforcement is off; model calls can incur uncapped provider charges.",
                                 simulated_send=True))
        elif path == "/api/conversations":
            with self.server.app.lock:
                self.reply(200, {"conversations": [dict(id=c["id"], title=c["post"].get("title", "Hội thoại"))
                    for c in self.server.app.conversations.values()]})
        elif path == "/api/personas":
            try:
                self.reply(200, {"personas": [{"id": p["id"], "name": p["name"],
                                                "slug": p.get("slug"),
                                                "source_kind": p.get("source_kind"),
                                                "status": p.get("status")} for p in self.server.app.personas()]})
            except Exception:
                self.reply(503, {"error": "Không tải được persona từ database. Kiểm tra cấu hình và kết nối."})
        elif path == "/api/posts":
            self.reply(200, dict(posts=POSTS, sample_facts=False))
        elif path in ("/", "/static/app.js", "/static/style.css"):
            name = {"/": "index.html", "/static/app.js": "app.js", "/static/style.css": "style.css"}[path]
            mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
            self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
        elif path.startswith("/api/conversations/"):
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:2] == ["api", "conversations"] and parts[3] == "jev":
                cid, event_id = parts[2], parts[4]
                with self.server.app.lock, self.server.app.jev_lock:
                    conversation = self.server.app.conversations.get(cid)
                    record = conversation.get("jev_shadows", {}).get(event_id) if conversation else None
                    self.reply(200, dict(record) if record else
                               dict(status="missing", error="Jev shadow result not found"))
                return
            cid = path.removeprefix("/api/conversations/")
            actor_id = parse_qs(urlsplit(self.path).query).get("actor_id", [None])[0]
            with self.server.app.lock:
                conversation = self.server.app.conversations.get(cid)
                self.reply(200, self.server.app.state(conversation, actor_id)) if conversation else self.reply(404, dict(error="Conversation not found"))
        else:
            self.reply(404, dict(error="Not found"))

    def do_POST(self):
        try:
            if not self.local_host():
                self.reply(403, dict(error="Local host required; open the app with localhost or 127.0.0.1"))
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("Content-Type must be application/json")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.reply(403, dict(error="Cross-origin requests refused"))
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("JSON body must be between 1 and 65536 bytes")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            path = urlsplit(self.path).path
            with self.server.app.lock:
                if path == "/api/conversations":
                    self.reply(201, self.server.app.create(data))
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 6 and parts[:2] == ["api", "conversations"] and parts[3] == "facts" and parts[5] == "revoke":
                    conversation = self.server.app.conversations.get(parts[2])
                    if not conversation:
                        self.reply(404, dict(error="Conversation not found"))
                        return
                    binding = self.server.app.persona_store.get_binding_snapshot(
                        conversation["page_id"], conversation["post"]["id"])
                    if not binding:
                        self.reply(404, dict(error="Generated fact not found"))
                        return
                    existing = next((fact for fact in self.server.app.persona_store.list_generated(
                        binding["persona_uuid"]) if fact["gen_fact_id"] == parts[4]), None)
                    if not existing:
                        self.reply(404, dict(error="Generated fact not found"))
                        return
                    reason = required_text(data, "reason", 500)
                    now_iso = conversation["engine"].clock.now().isoformat()
                    changed = self.server.app.persona_store.revoke_generated(parts[4], reason,
                        "sandbox-operator", now_iso)
                    conversation["engine"].store.audit(existing["created_comment_id"],
                        "persona_generated_fact_revoked", {"fact_id": parts[4], "reason": reason}, now_iso)
                    conversation["engine"].store.conn.commit()
                    self.reply(200, {**self.server.app.state(conversation), "revoked": changed,
                        "fact_id": parts[4]})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "turn":
                    conversation = self.server.app.conversations.get(parts[2])
                    if conversation:
                        self.reply(200, self.server.app.turn(conversation, data))
                        return
                self.reply(404, dict(error="Conversation not found"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            self.reply(400, dict(error=str(exc)))
        except Exception:
            self.reply(500, dict(error="Sandbox request failed; no automatic retry. Inspect server setup."))


def create_server(host="127.0.0.1", port=8766, ledger_path=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = Sandbox(ledger_path)
    server.app.persona_repository()
    return server


def main():
    server = create_server(os.environ.get("SANDBOX_HOST", "127.0.0.1"), int(os.environ.get("SANDBOX_PORT", "8766")))
    print(f"Live Luna sandbox: http://{server.server_address[0]}:{server.server_address[1]} (sample facts, FakeSender only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.close()


if __name__ == "__main__":
    main()
