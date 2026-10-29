import json
import http.client
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from unittest.mock import patch

from app.intent_api import IntentService, create_server
from app.state import Store


def request(event_id, *, actor="A", root_id="root-1", current_id=None, text="Giá lăn bánh Creta bao nhiêu?",
            parent=None, root_actor="A", root_text="Giá lăn bánh Creta bao nhiêu?"):
    current_id = current_id or root_id
    root = {"id": root_id, "actor_id": root_actor, "text": root_text}
    current = {"id": current_id, "actor_id": actor, "text": text}
    if current_id == root_id:
        root = current.copy()
    return {"event_id": event_id, "page_id": "brand-01",
            "post": {"id": "post-01", "text": "Thông tin về Hyundai Creta", "vehicles": ["Creta"]},
            "thread_id": root_id, "root": root, "parent": parent, "current": current}


def semantic_for(comment):
    base = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
            "requests": [{"topic": "price", "desired_outcome": "price_otr", "polarity": "positive"}],
            "operations": [], "uncertainties": []}
    if "giá" in comment.casefold():
        base["operations"] = [{"op": "START_REQUEST", "request_ref": "topic:price",
            "evidence_excerpt": "giá", "slot": {"kind": "vehicle", "value": "Creta",
                "raw": "Creta", "explicit": True, "evidence_excerpt": "Creta"}}]
    elif "bản cao" in comment.casefold():
        base["operations"] = [{"op": "UPDATE_SLOT", "request_ref": "topic:price",
            "evidence_excerpt": "Bản cao", "slot": {"kind": "variant", "value": "Cao cấp",
                "raw": "bản cao", "explicit": True, "evidence_excerpt": "Bản cao"}}]
    else:
        base["operations"] = [{"op": "UNRESOLVED", "evidence_excerpt": comment[:100]}]
    return base


class IntentApiServiceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def model_call(post, comment, **kwargs):
            self.calls.append((post, comment, kwargs["context"]))
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0,
                    "model_effective": "fixture-only"}
        self.store = Store(":memory:")
        self.service = IntentService(store=self.store, api_key="local-test-key", model_call=model_call)
        self.addCleanup(self.service.close)

    def post(self, data, key="local-test-key"):
        if key != self.service.api_key:
            return 401, {"status": "UNAUTHORIZED"}
        return self.service.process(data)

    def test_root_reply_scope_idempotency_and_model_context(self):
        code, root = self.post(request("event-root"))
        self.assertEqual(code, 200, root)
        self.assertEqual(root["semantic_version"], "2")
        self.assertFalse(root["decision"]["outbound_effects_enabled"])

        reply = request("event-reply", current_id="reply-a", text="Bản cao nhé",
                        parent={"id": "root-1", "actor_id": "A", "text": "Giá lăn bánh Creta bao nhiêu?"})
        code, updated = self.post(reply)
        self.assertEqual(code, 200, updated)
        slots = next(iter(updated["state_after"]["requests"].values()))["slots"]
        self.assertEqual(slots["variant"]["value"], "Cao cấp")

        other_actor = request("event-b", actor="B", current_id="reply-b", text="Bản cao nhé",
            parent={"id": "root-1", "actor_id": "A", "text": "Giá lăn bánh Creta bao nhiêu?"})
        code, blocked = self.post(other_actor)
        self.assertEqual(code, 200, blocked)
        self.assertTrue(any(x.startswith("REVIEW") for x in blocked["decision"]["routing"]))
        self.assertEqual(len(blocked["state_after"]["requests"]), 0)

        another_thread = request("event-other-thread", root_id="root-2", current_id="reply-c",
            text="Bản cao nhé", root_text="Thông tin Creta", parent=None)
        code, isolated = self.post(another_thread)
        self.assertEqual(code, 200, isolated)
        self.assertTrue(any(x.startswith("REVIEW") for x in isolated["decision"]["routing"]))

        code, replay = self.post(request("event-root"))
        self.assertEqual(code, 200)
        self.assertTrue(replay["idempotent_replay"])
        changed = request("event-root", text="Giá lăn bánh Tucson bao nhiêu?")
        changed["post"]["vehicles"] = ["Tucson"]
        changed["root"]["text"] = changed["current"]["text"]
        self.assertEqual(self.post(changed)[0], 409)

        context = json.loads(self.calls[1][2])
        self.assertEqual(context["postcard"]["vehicles"], ["Creta"])
        root_message = next(m for m in context["messages"] if m["role"] in ("root", "root_parent"))
        self.assertEqual(root_message["actor_id"], "A")
        self.assertEqual(root_message["semantic"]["intent_labels"], ["price"])
        current = [m for m in context["messages"] if m["role"] == "current"]
        self.assertEqual(current[0]["actor_id"], "A")
        parent = [m for m in context["messages"] if m["role"] in ("parent", "root_parent")]
        # When the direct parent is the root, context keeps one canonical message.
        self.assertTrue(parent or root_message["message_id"] == "root-1")
        self.assertEqual((parent or [root_message])[0]["actor_id"], "A")
        self.assertNotIn("state_token", self.calls[1][2])
        self.assertNotIn('"state_token"', self.calls[1][2])

    def test_auth_validation_health_and_readiness(self):
        self.assertEqual(self.post(request("unauth"), key="wrong")[0], 401)
        malformed = request("bad")
        malformed["unknown"] = True
        self.assertEqual(self.post(malformed)[0], 422)
        self.assertTrue(self.service.health()["ok"])
        self.assertTrue(self.service.readiness()["ok"])

    def test_concurrent_duplicate_is_claimed_once_and_model_failure_is_explicit(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_model(post, comment, **kwargs):
            calls.append(comment)
            entered.set()
            release.wait(2)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        self.service.model_call = slow_model
        self.service.live_model = False
        payload = request("event-concurrent")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.post, payload)
            self.assertTrue(entered.wait(1))
            second = self.post(payload)
            release.set()
            first_result = first.result(timeout=3)
        self.assertEqual(second[0], 202)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first_result[0], 200)

        def failed_model(*args, **kwargs):
            return {"ok": False, "attempts": 0, "error": "private provider detail"}
        self.service.model_call = failed_model
        code, failed = self.post(request("event-model-fails"))
        self.assertEqual(code, 503)
        self.assertEqual(failed["status"], "MODEL_UNAVAILABLE")
        self.assertNotIn("private provider detail", json.dumps(failed))
        stored = self.service.get_event("event-model-fails")
        self.assertEqual(stored["status"], "PENDING")

    def test_state_change_during_model_call_retries_same_event_with_fresh_context(self):
        root = {"id": "root-1", "actor_id": "A", "text": "Giá lăn bánh Creta bao nhiêu?"}
        payload = request("event-stale", current_id="reply-stale", text="Bản cao nhé", parent=root)
        calls = []
        def mutating_model(post, comment, **kwargs):
            calls.append(comment)
            if len(calls) == 1:
                external_event = {"page_id": "brand-01", "post_id": "post-01",
                    "thread_root_id": "root-1", "comment_id": "external-state", "parent_id": "root-1",
                    "actor_id": "A", "actor_role": "user", "event_type": "create",
                    "content_version": 1, "text": root["text"], "event_dedupe_key": "external-state"}
                postcard = {"id": "post-01", "page_id": "brand-01", "text": "Thông tin về Hyundai Creta",
                            "vehicles": ["Creta"]}
                self.service.engine.process(external_event, postcard, semantic_for(root["text"]))
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        self.service.model_call = mutating_model
        code, result = self.post(payload)
        self.assertEqual(code, 200, result)
        self.assertEqual(calls, ["Bản cao nhé", "Bản cao nhé"])
        self.assertNotIn("REVIEW_STALE_CONTEXT", result["decision"]["routing"])
        requests = result["state_after"]["requests"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[next(iter(requests))]["slots"]["variant"]["value"], "Cao cấp")

    def test_engine_rollback_is_not_reported_or_persisted_as_success(self):
        with patch.object(self.service.engine, "_apply_ops", side_effect=RuntimeError("rollback probe")):
            code, result = self.post(request("event-engine-rollback"))
        self.assertEqual(code, 503, result)
        self.assertEqual(result["status"], "PROCESSING_ERROR")
        self.assertEqual(self.service.get_event("event-engine-rollback")["status"], "PENDING")
        self.assertEqual(self.service.engine.context(
            {"page_id": "brand-01", "post_id": "post-01", "thread_root_id": "root-1", "actor_id": "A"},
            {"id": "post-01", "page_id": "brand-01", "text": "Thông tin Creta", "vehicles": ["Creta"]}
        )["active_requests"], [])

    def test_context_conflict_replay_returns_same_structured_conflict_without_model_call(self):
        self.post(request("event-context-root"))
        before = len(self.calls)
        conflict = request("event-context-conflict", actor="B", current_id="reply-conflict",
            text="Oke", root_actor="A", root_text="Giá lăn bánh Tucson bao nhiêu?")
        first_code, first = self.post(conflict)
        second_code, second = self.post(conflict)
        self.assertEqual(first_code, 409)
        self.assertEqual(second_code, 409)
        self.assertEqual({k: v for k, v in first.items() if k != "idempotent_replay"},
                         {k: v for k, v in second.items() if k != "idempotent_replay"})
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["status"], "CONTEXT_CONFLICT")
        self.assertEqual(len(self.calls), before)

    def test_root_and_parent_same_id_must_match_actor_and_text(self):
        conflict = request("event-root-parent-conflict", current_id="reply", text="Oke",
            parent={"id": "root-1", "actor_id": "B", "text": "different"})
        self.assertEqual(self.post(conflict)[0], 422)

    def test_explicit_slot_from_model_must_not_write_unproven_value(self):
        forged = semantic_for("Giá lăn bánh Creta bao nhiêu?")
        forged["operations"][0]["slot"] = {"kind": "vehicle", "value": "Tucson",
            "raw": "Tucson", "explicit": True, "evidence_excerpt": "Tucson"}
        self.service.model_call = lambda *args, **kwargs: {"ok": True, "semantic": forged, "attempts": 0}
        code, result = self.post(request("event-slot-forgery"))
        self.assertEqual(code, 200, result)
        self.assertTrue(any(r in ("REVIEW_SCHEMA", "REVIEW_PROVENANCE")
                            for r in result["decision"]["routing"]))
        self.assertEqual(result["state_after"]["requests"], {})

    def test_scope_review_returns_the_validated_semantic(self):
        out_of_scope = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "out_of_scope", "requests": [], "uncertainties": [],
            "operations": [{"op": "UNRESOLVED", "reason": "other topic", "evidence_excerpt": "xin chào"}]}
        self.service.model_call = lambda *args, **kwargs: {"ok": True, "semantic": out_of_scope, "attempts": 0}
        code, result = self.post(request("event-scope-review", text="xin chào"))
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["semantic"]["scope"], "out_of_scope")
        self.assertIn("REVIEW_SCOPE", result["decision"]["routing"])

    def test_expired_lease_fences_worker_before_state_commit(self):
        entered, release = threading.Event(), threading.Event()
        self.service.lease_seconds = 1
        def slow_model(post, comment, **kwargs):
            entered.set()
            release.wait(3)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        self.service.model_call = slow_model
        second = IntentService(store=self.store, api_key="local-test-key", model_call=slow_model)
        second.lease_seconds = 1
        payload = request("event-expired-lease")
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.service.process, payload)
            self.assertTrue(entered.wait(2))
            self.store.conn.execute("UPDATE intent_api_events SET lease_until=? WHERE event_id=?",
                                    ("2000-01-01T00:00:00+00:00", payload["event_id"]))
            self.store.conn.commit()
            from app.intent_api import validate_request
            second_claim = second._claim(validate_request(payload))
            release.set()
            first_result = first.result(timeout=5)
        self.assertEqual(second_claim[0], "replay")
        self.assertNotEqual(first_result[1].get("status"), "PROCESSED")
        self.assertEqual(first_result[1].get("status"), "WORKER_LEASE_LOST")
        self.assertEqual(self.service.get_event(payload["event_id"])["status"], "REVIEW")
        self.assertEqual(self.store.active_requests("brand-01", "A", "post-01", "root-1"), [])

    def test_missing_live_provider_and_budget_is_reported_without_calling_model(self):
        from app.adapters.luna import call_luna
        store = Store(":memory:")
        service = IntentService(store=store, api_key="local-test-key", model_call=call_luna)
        try:
            with patch.dict(os.environ, {}, clear=True):
                code, result = service.process(request("event-not-ready"))
            self.assertEqual(code, 503)
            self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
            self.assertTrue(any("OPENROUTER_API_KEY" in reason for reason in result["reasons"]))
            self.assertTrue(any("INTENT_MODEL_MAX_CALLS" in reason for reason in result["reasons"]))
            self.assertEqual(service.get_event("event-not-ready")["status"], "PENDING")
        finally:
            service.close()

    def test_budget_ledger_write_failure_is_terminal_for_the_event(self):
        calls = []
        def model_call(post, comment, **kwargs):
            calls.append(comment)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 1,
                    "cost_usd_est": None, "cost_unknown": True}
        self.service.model_call = model_call
        with patch("scripts.budget_ledger.record", side_effect=OSError("ledger unavailable")):
            status, result = self.post(request("event-ledger-fails"))
        self.assertEqual(status, 503)
        self.assertEqual(result["status"], "REVIEW")
        self.assertIn("REVIEW_BUDGET_LEDGER", result["decision"]["routing"])
        replay_status, replay = self.post(request("event-ledger-fails"))
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(calls), 1)

    def test_api_budget_stays_enforced_while_sandbox_defaults_open(self):
        from app.adapters.luna import call_luna
        from sandbox.server import Sandbox
        from scripts import budget_ledger
        with tempfile.TemporaryDirectory() as directory:
            ledger = os.path.join(directory, "budget.jsonl")
            budget_ledger.record(ledger, run_id="fixture", kind="call", attempts=24,
                                 cost_usd_est=0.01)
            service = IntentService(store=Store(":memory:"), api_key="local-test-key",
                model_call=call_luna, budget_path=ledger)
            self.addCleanup(service.close)
            service.budget_max_calls = 300
            service.budget_max_cost = 2.0
            service.budget_reserve = 0.00032
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "offline-placeholder"}):
                status, result = service.process(request("event-budget-cap"))
            self.assertEqual(status, 503)
            self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
            self.assertTrue(any("exhausted" in reason for reason in result["reasons"]))
            self.assertTrue(Sandbox(ledger).budget()[0])
            with patch.dict(os.environ, {"SANDBOX_ENFORCE_BUDGET": "true"}):
                self.assertFalse(Sandbox(ledger).budget()[0])
            self.assertEqual(budget_ledger.totals(ledger)[0], 24)

    def test_http_api_auth_health_readiness_post_and_event_read(self):
        server = create_server(self.service, host="127.0.0.1", port=0)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(worker.join, 2)
        port = server.server_address[1]

        def get(path, key=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            return response.status, body

        code, health = get("/health")
        self.assertEqual((code, health["service"]), (200, "mat-intent"))
        self.assertEqual(get("/ready")[0], 200)
        self.assertEqual(get("/v1/events/event-http")[0], 401)

        payload = request("event-http")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/intent", body=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer local-test-key", "Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 200, result)
        self.assertEqual(result["semantic_version"], "2")
        self.assertEqual(get("/v1/events/event-http", "local-test-key")[0], 200)

        failed_payload = request("event-http-engine-failure", root_id="http-error-root")
        with patch.object(self.service.engine, "_apply_ops", side_effect=RuntimeError("rollback probe")):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/intent", body=json.dumps(failed_payload).encode(), headers={
                "Authorization": "Bearer local-test-key", "Content-Type": "application/json"})
            failed_response = conn.getresponse()
            failed = json.loads(failed_response.read())
            conn.close()
        self.assertEqual(failed_response.status, 503, failed)
        self.assertEqual(failed["status"], "PROCESSING_ERROR")
        self.assertEqual(self.service.get_event("event-http-engine-failure")["status"], "PENDING")

    def test_jev_shadow_is_bounded_and_trace_contains_no_comment_text(self):
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        counts = {"active": 0, "started": 0, "max_active": 0}
        class BlockingJev:
            def decide(self, state, questions, **kwargs):
                with lock:
                    counts["active"] += 1
                    counts["started"] += 1
                    counts["max_active"] = max(counts["max_active"], counts["active"])
                    if counts["started"] == 2:
                        started.set()
                release.wait(2)
                with lock:
                    counts["active"] -= 1
                    if counts["active"] == 0:
                        completed.set()
                return {"ok": True, "attempts": 0, "model_effective": "typesafe/jev-1.14",
                        "answers": {key: {"type": "noul", "noul": 0.2} for key in questions}}
        with tempfile.TemporaryDirectory() as directory:
            trace_path = os.path.join(directory, "trace.jsonl")
            self.service.jev_shadow_enabled = True
            self.service.jev_adapter = BlockingJev()
            self.service.trace_path = trace_path
            data = request("shadow-event", text="private comment text")
            event = {"page_id": data["page_id"], "post_id": data["post"]["id"],
                     "thread_root_id": data["thread_id"], "comment_id": data["current"]["id"],
                     "actor_id": data["current"]["actor_id"]}
            self.assertTrue(self.service._start_jev_shadow(data, event, "private comment text"))
            self.assertTrue(self.service._start_jev_shadow(data, event, "private comment text"))
            self.assertTrue(started.wait(1))
            self.assertFalse(self.service._start_jev_shadow(data, event, "private comment text"))
            release.set()
            self.assertTrue(completed.wait(2))
            rows = []
            for _ in range(100):
                if os.path.exists(trace_path):
                    with open(trace_path, encoding="utf-8") as stream:
                        rows = [json.loads(line) for line in stream if line.strip()]
                    if len(rows) == 2:
                        break
                threading.Event().wait(0.01)
            self.assertEqual(len(rows), 2)
            self.assertEqual(counts["max_active"], 2)
            self.assertNotIn("private comment text", json.dumps(rows))
            self.assertTrue(all(row["question_keys"] and row["decisions"] for row in rows))
            self.assertEqual(rows[0]["decisions"]["price"], {"type": "noul", "probability": 0.2})


if __name__ == "__main__":
    unittest.main()
