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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlencode

from app.adapters.luna import MODEL as DEFAULT_MODEL, call_luna, NoRedirect
from app.adapters.jev import JevOpenRouter, intent_questions, validate_answers
from app.engine import WallClock
from app.facts import Facts, dealer_public_address
from app.harness import make_engine
from app.persona_fixture import seed_nam_creta
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
VEHICLES = ("Creta", "Tucson")


def required_text(data, key, maximum=10000):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be non-empty text (maximum {maximum} characters)")
    return value.strip()


class Sandbox:
    def __init__(self, ledger_path=None, dealer_db_path=None, persona_db_path=None):
        self.ledger_path = ledger_path or budget_ledger.DEFAULT_PATH
        self.dealer_db_path = dealer_db_path or os.environ.get("SANDBOX_DEALER_DB") or str(
            Path(__file__).resolve().parents[1] / "fact_db.sqlite3")
        self.enforce_budget = os.environ.get("SANDBOX_ENFORCE_BUDGET", "false").strip().lower() in {
            "1", "true", "yes", "on"}
        self.conversations = {}
        self.lock = PROVIDER_LOCK
        self.jev_lock = threading.Lock()
        self.jev_adapter = JevOpenRouter()
        self.jev_trace_path = str(JEV_TRACE_PATH)
        self.persona_db_path = persona_db_path or os.environ.get("SANDBOX_PERSONA_DB") or str(
            Path(__file__).resolve().parents[1] / "runs" / "persona_sandbox.sqlite3")
        self.persona_conn = None
        self.persona_store = None

    def persona_repository(self):
        if self.persona_store is None:
            Path(self.persona_db_path).parent.mkdir(parents=True, exist_ok=True)
            self.persona_conn = sqlite3.connect(self.persona_db_path, check_same_thread=False, timeout=10)
            migrate_sqlite(self.persona_conn)
            self.persona_store = PersonaStore(self.persona_conn)
            seed_nam_creta(self.persona_store)
        return self.persona_store

    def personas(self):
        repository = self.persona_repository()
        fixtures = [dict(id=item["id"], name=item["display_name"], slug=item["slug"],
                         source_kind=item["source_kind"], status=item["status"])
                    for item in repository.list_personas()]
        if not os.environ.get("DB_Host"):
            return fixtures
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(host=os.environ["DB_Host"], port=os.environ.get("DB_Port", "5432"),
                                 dbname="content_ai_fac", user=os.environ.get("DB_Username"),
                                 password=os.environ.get("DB_Password"), connect_timeout=5,
                                 options="-c default_transaction_read_only=on", row_factory=dict_row) as conn:
                personas = [dict(row, id=str(row["id"])) for row in conn.execute(
                    "SELECT id,name,occupation,car_owned,category,role,tone,perspective,sample_quote "
                    "FROM public.personas ORDER BY name,id")]
            return personas + fixtures
        except Exception:
            if fixtures:
                return fixtures
            raise

    def generate_post(self, persona, vehicle, _metrics=None):
        started = time.perf_counter()
        model_started = None
        effective_model = MODEL
        usage = {}
        reported_cost = None
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("Cần cấu hình OPENROUTER_API_KEY để tạo bài viết.")
        reservation = f"sandbox-post:{uuid.uuid4().hex}"
        if self.enforce_budget:
            allowed, reason = budget_ledger.reserve(
                self.ledger_path, reservation_id=reservation, max_calls=300, max_cost=2.00,
                reserve_cost_usd=ATTEMPT_COST_RESERVE, prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
            if not allowed:
                raise ValueError(f"Không thể tạo bài: {reason}")
        payload = {"model": MODEL, "max_tokens": 600, "messages": [
            {"role": "system", "content": "Viết một bài Facebook tiếng Việt khoảng 100-180 từ, có mở đầu, thông tin hữu ích và câu hỏi mở. Thể hiện góc nhìn và giọng điệu persona, nêu tiêu chí thực tế khi cân nhắc xe. Không bịa giá, thông số, ưu đãi, trải nghiệm cá nhân hay cam kết chưa được cung cấp. Chỉ trả nội dung bài viết."},
            {"role": "user", "content": json.dumps({"persona": persona, "vehicle": vehicle}, ensure_ascii=False)}]}
        request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        cost = None
        try:
            model_started = time.perf_counter()
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                result = json.load(response)
            if isinstance(result, dict) and isinstance(result.get("model"), str) and result["model"].strip():
                effective_model = result["model"].strip()[:200]
            usage = result.get("usage") or {}
            reported_cost = usage.get("cost")
            if type(reported_cost) in (int, float) and 0 <= reported_cost < float("inf"):
                cost = reported_cost
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str) or len(text.strip()) < 200:
                raise ValueError("Bài viết tạo ra quá ngắn; hãy thử lại.")
            return text.strip()
        finally:
            if _metrics is not None:
                _metrics.update({"model": effective_model, "attempts": 1,
                    "model_latency_ms": round((time.perf_counter() - model_started) * 1000, 3)
                        if model_started is not None else None,
                    "generation_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "usage": {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                              if type(usage.get(key)) is int and usage[key] >= 0},
                    "cost_usd_est": cost})
            budget_ledger.record(self.ledger_path, run_id=reservation, kind="post", attempts=1,
                                 cost_usd_est=cost, model=effective_model)
            if self.enforce_budget:
                budget_ledger.settle_reservations(self.ledger_path, [reservation])

    def create(self, data):
        actor = required_text(data, "actor_id", 200)
        persona = None
        fixture_binding = None
        if data.get("persona_id"):
            persona_id = required_text(data, "persona_id", 50)
            persona = next((p for p in self.personas() if p["id"] == persona_id), None)
            if persona is None:
                raise ValueError("unknown persona_id")
            vehicle = required_text(data, "vehicle", 30)
            if vehicle not in VEHICLES:
                raise ValueError("vehicle must be one of the available sample vehicles")
            generation = {}
            if persona.get("source_kind") == "fictional_fixture":
                persona = {"id": persona_id, "name": persona["name"], "tone": "warm",
                           "perspective": "người dùng Hyundai Creta trong kịch bản hư cấu"}
                release = self.persona_repository().get_release_snapshot(persona_id)
                fixture_binding = {"persona_uuid": persona_id, "release_id": release["release"]["id"]}
            post = {"id": uuid.uuid4().hex, "page_id": "sample-hyundai",
                    "title": f"Hyundai {vehicle} · {persona['name']}",
                    "text": self.generate_post(persona, vehicle, _metrics=generation), "vehicles": [vehicle],
                    "generation": generation,
                    "persona_id": persona_id, "persona_name": persona["name"]}
            if fixture_binding:
                post.update(release_id=fixture_binding["release_id"], persona_source_kind="fictional_fixture",
                            persona_fixture_only=True)
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
        engine = make_engine(clock=WallClock(), facts=Facts(facts=[]), dealer_db_path=self.dealer_db_path)
        engine.persona_store = self.persona_store
        engine.allow_persona_fixtures = True
        engine.response_style = style_from_tone(persona.get("tone") if persona else None)
        conversation = dict(id=cid, page_id=post["page_id"], actor_id=actor, thread_id="root-" + cid,
                            post=post, engine=engine, history=[], counter=0, jev_shadows={})
        conversation["postcard"] = dict(id=post["id"], page_id=post["page_id"],
                                        text=post["text"], title=post.get("title"),
                                        topic="vehicle", vehicles=post["vehicles"],
                                        persona_default=persona.get("persona_default", "tư vấn viên") if persona else "tư vấn viên")
        if fixture_binding:
            self.persona_repository().bind_post(post["page_id"], post["id"],
                                         fixture_binding["persona_uuid"], fixture_binding["release_id"])
            conversation["postcard"]["persona_binding"] = fixture_binding
        self.conversations[cid] = conversation
        return self.state(conversation)

    @staticmethod
    def event(conversation, text="", comment_id=None):
        return dict(page_id=conversation["page_id"], actor_id=conversation["actor_id"],
                    post_id=conversation["post"]["id"], thread_root_id=conversation["thread_id"],
                    parent_id=conversation.get("root_comment_id") or conversation["thread_id"],
                    comment_id=comment_id or "preview",
                    actor_role="user", event_type="create", content_version=1, text=text)

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

    def state(self, conversation):
        engine = conversation["engine"]
        context = self.context(conversation, self.event(conversation))
        persona_release = None
        if self.persona_store and conversation["post"].get("release_id"):
            binding = self.persona_store.get_binding_snapshot(conversation["page_id"], conversation["post"]["id"])
            if binding:
                persona_release = {"persona": binding["snapshot"]["persona"],
                    "release": binding["snapshot"]["release"], "fixture_only": binding["fixture_only"],
                    "facts": binding["facts"], "utterances": binding["utterances"]}
        with engine.store.lock:
            outbox = [dict(dedupe_key=r[0], purpose=r[1], status=r[2]) for r in
                      engine.store.conn.execute("SELECT dedupe_key,purpose,status FROM outbox ORDER BY rowid")]
        return dict(id=conversation["id"], page_id=conversation["page_id"], actor_id=conversation["actor_id"],
                    thread_id=conversation["thread_id"], post=conversation["post"], history=conversation["history"],
                    active_requests=context["active_requests"], pending=context["pending"],
                    dealer_directory={"status": "unavailable" if engine.dealer_directory_error else "available",
                        "count": sum(1 for fact in engine.facts.facts
                                     if fact.get("source") == "local_dealer_directory")},
                    persona_release=persona_release,
                    outbox=outbox, fake_sends=engine.sender.sent, sample_facts=False, simulated_send=True)

    def budget(self, inflight=0):
        if not self.enforce_budget:
            return True, "budget enforcement is disabled; provider spend is not capped"
        return budget_ledger.guard(self.ledger_path, max_calls=300, max_cost=2.00,
                                   prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                                   prior_cost=budget_ledger.SHARED_PRIOR_COST_USD,
                                   est_attempts=inflight + 1, est_cost=ATTEMPT_COST_RESERVE * (inflight + 1))

    @staticmethod
    def prior_customer_turns(conversation, text, existing=()):
        existing = set(existing)
        return list(dict.fromkeys(turn["text"] for turn in conversation["history"]
                                  if isinstance(turn.get("text"), str) and turn["text"].strip()
                                  and turn["text"] != text and turn["text"] not in existing))[-5:]

    def model(self, conversation, text, context, repair_note="", max_tokens=1200):
        existing = [row["text"] for row in context["recent"]] + context["parent_texts"]
        customer_turns = self.prior_customer_turns(conversation, text, existing)
        replies = [turn["response_text"] for turn in conversation["history"]
                   if turn.get("outbound_permission") is True and
                   any(send.get("status") == "sent_to_fake_ledger" for send in turn.get("sends", [])
                       if isinstance(send, dict)) and
                   isinstance(turn.get("response_text"), str)][-5:]
        if getattr(self, "persona_store", None) and conversation.get("post", {}).get("release_id"):
            root = self.persona_store.root_analysis(conversation["page_id"], conversation["post"]["id"],
                                                    conversation["thread_id"])
            if root and root.get("reply_text"):
                replies = [root["reply_text"], *replies][-5:]
        model_context = fmt_context({**context, "recent_bot_replies": replies},
                                    prior_customer_turns=customer_turns)
        jev_shadow_event_id = self.start_jev_shadow(conversation, f"{conversation['id']}-{conversation['counter']}",
                                                    model_context)
        reservations = []
        def guard():
            if not self.enforce_budget:
                return True
            reservation_id = f"sandbox:{conversation['id']}:{uuid.uuid4().hex}"
            allowed, _ = budget_ledger.reserve(
                self.ledger_path, reservation_id=reservation_id, max_calls=300,
                max_cost=2.00, reserve_cost_usd=ATTEMPT_COST_RESERVE,
                prior_calls=budget_ledger.SHARED_PRIOR_CALLS,
                prior_cost=budget_ledger.SHARED_PRIOR_COST_USD)
            if allowed: reservations.append(reservation_id)
            return allowed
        model_started = time.perf_counter()
        result = call_luna(conversation["post"]["text"], text,
                           parent="", context=model_context,
                           repair_note=repair_note[-REPAIR_CHAR_LIMIT:], budget_guard=guard,
                           model=MODEL, max_tokens=max_tokens)
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

    def start_jev_shadow(self, conversation, event_id, context):
        """Start one optional sandbox-only shadow call; never affects Luna or policy."""
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
                        self.ledger_path, reservation_id=reservation_id, max_calls=300, max_cost=2.00,
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
        conversation["counter"] += 1
        event = self.event(conversation, text, f"{conversation['id']}-{conversation['counter']}")
        engine = conversation["engine"]
        context = self.context(conversation, event)
        engine_processed = False
        turn = dict(text=text, ok=False, semantic=None, routing=[], response_text=None,
                    sends=[], errors=[], attempts=0, cost_usd_est=0.0, cost_unknown=False,
                    model=MODEL, model_latency_ms=0.0, usage={}, usage_complete=True, model_calls=[])
        allowed, reason = self.budget()
        if not allowed:
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
                api = self.model(conversation, text, context, note, max_tokens=1800 if truncated else 1200)
                turn["model_latency_ms"] += api.get("model_latency_ms", 0.0) or 0.0
                usage = api.get("usage") if isinstance(api.get("usage"), dict) else {}
                attempts = api.get("attempts", 0)
                if attempts and (attempts > 1 or any(type(usage.get(key)) is not int or usage[key] < 0
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
                turn["model_calls"].append(call_trace)
                turn["model"] = api.get("model_effective", MODEL)
                turn["jev_shadow_event_id"] = api.get("jev_shadow_event_id")
                turn["attempts"] += attempts
                if attempts:
                    unknown = api.get("cost_unknown", True) or (api.get("ok") and attempts > 1)
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
                turn["semantic"] = attach_context_provenance(api.get("semantic"), event, context,
                                                               conversation["postcard"])
                turn["errors"] = validate_semantic(turn["semantic"], text,
                    [conversation["post"]["text"]] + [r["text"] for r in context["recent"]] + context["parent_texts"] +
                    self.prior_customer_turns(conversation, text,
                        [r["text"] for r in context["recent"]] + context["parent_texts"]))
                call_trace["validation"] = "failed" if turn["errors"] else "passed"
                call_trace["validation_errors"] = list(turn["errors"])
                if turn["errors"]:
                    turn.update(error="Model semantics failed validation", routing=["REVIEW_SCHEMA"])
                    continue
                result = engine.process(event, conversation["postcard"], turn["semantic"],
                                        expected_state_token=context["state_token"],
                                        trusted_context_messages=context["messages"])
                engine_processed = not any(route.startswith("ERROR_") for route in result["routing"])
                turn.update({k: result[k] for k in ("routing", "response_text", "sends", "errors",
                    "outbound_permission", "state_changes") if k in result})
                turn["response_plan"] = result.get("response_plan")
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
                                 budget_warning=None if self.server.app.enforce_budget else
                                 "Budget enforcement is off; model calls can incur uncapped provider charges.",
                                 simulated_send=True))
        elif path == "/api/personas":
            try:
                self.reply(200, {"personas": [{"id": p["id"], "name": p["name"],
                                                "slug": p.get("slug"),
                                                "source_kind": p.get("source_kind"),
                                                "status": p.get("status")} for p in self.server.app.personas()],
                                 "vehicles": list(VEHICLES)})
            except Exception:
                self.reply(503, {"error": "Không tải được persona từ database. Kiểm tra cấu hình và kết nối."})
        elif path == "/api/posts":
            self.reply(200, dict(posts=POSTS, sample_facts=True))
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
            with self.server.app.lock:
                conversation = self.server.app.conversations.get(cid)
                self.reply(200, self.server.app.state(conversation)) if conversation else self.reply(404, dict(error="Conversation not found"))
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


if __name__ == "__main__":
    main()
