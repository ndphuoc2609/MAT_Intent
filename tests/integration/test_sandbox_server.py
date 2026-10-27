"""HTTP sandbox contracts; only the external Luna boundary is mocked."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def meaning(text="giá bao nhiêu", evidence="Creta"):
    return {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope", "requests": [{"topic": "price", "desired_outcome": "price", "polarity": "positive"}],
            "operations": [{"op": "START_REQUEST", "request_ref": "topic:price", "evidence_excerpt": evidence,
                            "slot": {"kind": "vehicle", "value": "Creta", "explicit": False}}], "uncertainties": []}


def model_result(sem=None, attempts=1, cost=0.0001, **extra):
    return dict(ok=True, semantic=meaning() if sem is None else sem, raw_text="{}", usage={"prompt_tokens": 500, "completion_tokens": 100},
                cost_usd_est=cost, cost_unknown=cost is None, model_effective="openai/gpt-6-luna", attempts=attempts, **extra)


class SandboxHTTPTests(unittest.TestCase):
    def setUp(self):
        from sandbox.server import Sandbox
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.tmp.name) / "budget.jsonl")
        self.http = SimpleNamespace(app=Sandbox(ledger_path=self.ledger))
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        for conversation in self.http.app.conversations.values():
            conversation["engine"].store.conn.close()
            conversation["engine"].sender.receiver.conn.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, host="127.0.0.1:8766", origin=None):
        from sandbox.server import Handler
        body = json.dumps(payload).encode() if payload is not None else b""
        extra = f"Origin: {origin}\r\n" if origin else ""
        request = (f"{method} {path} HTTP/1.0\r\nHost: {host}\r\n{extra}Content-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        class Connection:
            def __init__(self):
                self.output = io.BytesIO()
            def makefile(self, *_):
                return io.BytesIO(request)
            def sendall(self, value):
                self.output.write(value)
        connection = Connection()
        Handler(connection, ("127.0.0.1", 12345), self.http)
        headers, response = connection.output.getvalue().split(b"\r\n\r\n", 1)
        status = int(headers.split(b" ")[1])
        return status, json.loads(response)

    def conversation(self, **fields):
        payload = dict(post_id="creta", actor_id="customer-1")
        payload.update(fields)
        status, state = self.request("POST", "/api/conversations", payload)
        self.assertEqual(status, 201, state)
        return state

    def turn(self, state, text="giá bao nhiêu"):
        return self.request("POST", f"/api/conversations/{state['id']}/turn", {"text": text})

    def rows(self):
        if not Path(self.ledger).exists():
            return []
        return [row for line in Path(self.ledger).read_text().splitlines()
                if (row := json.loads(line)).get("kind") in ("call", "repair", "retry")]

    def test_health_seeded_and_manual_posts(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        status, posts = self.request("GET", "/api/posts")
        self.assertEqual(status, 200)
        self.assertTrue(any(post["id"] == "creta" for post in posts["posts"]))
        seeded = self.conversation()
        self.assertEqual(seeded["post"]["vehicles"], ["Creta"])
        manual = self.conversation(post_id=None, page_id="manual-page", post_text="Tucson tại Page của tôi", vehicles=["Tucson"])
        self.assertEqual(manual["page_id"], "manual-page")
        self.assertEqual(manual["post"]["text"], "Tucson tại Page của tôi")
        self.assertEqual(manual["active_requests"], [])

    def test_valid_model_flow_uses_post_evidence_and_retains_pending(self):
        state = self.conversation()
        def luna(post, text, **kwargs):
            self.assertIn("Creta", post)
            self.assertIn("Creta", kwargs["context"])
            self.assertEqual(kwargs["parent"], "")
            self.assertTrue(kwargs["budget_guard"]())
            return model_result()
        with patch("sandbox.server.call_luna", side_effect=luna):
            status, result = self.turn(state)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["turn"]["ok"], result)
        self.assertEqual(result["active_requests"][0]["slots"]["vehicle"]["value"], "Creta")
        self.assertTrue(result["pending"])
        self.assertTrue(result["fake_sends"])
        self.assertEqual(result["turn"]["semantic"]["operations"][0]["op"], "START_REQUEST")
        self.assertEqual(self.rows()[0]["attempts"], 1)
        self.assertEqual(self.rows()[0]["status"], "completed")

    def test_successful_retry_keeps_uncertain_failed_attempt_reconcilable(self):
        from scripts import budget_ledger
        state = self.conversation()
        result_with_retry = model_result(attempts=2, retry_cost_usd_est=None,
                                         retry_cost_unknown=True)
        with patch("sandbox.server.call_luna", return_value=result_with_retry):
            _, result = self.turn(state)
        self.assertTrue(result["turn"]["ok"], result)
        retry, success = self.rows()
        self.assertEqual(retry["kind"], "retry")
        self.assertEqual(retry["status"], "failed")
        self.assertEqual(success["status"], "completed")
        budget_ledger.reconcile_unknown_cost(
            self.ledger, original_ts=retry["ts"], run_id=retry["run_id"],
            cost_usd_est=0.0, reason="verified zero charge in provider activity")
        self.assertEqual(budget_ledger.totals(self.ledger), (2, 0.0001, 0, 3))

    def test_same_actor_conversations_are_isolated(self):
        first = self.conversation()
        second = self.conversation()
        third = self.conversation(post_id=None, page_id="different-page", post_text="Tucson", vehicles=["Tucson"])
        with patch("sandbox.server.call_luna", return_value=model_result()):
            self.turn(first)
        for other in (second, third):
            _, current = self.request("GET", f"/api/conversations/{other['id']}")
            self.assertEqual(current["active_requests"], [])
            self.assertEqual(current["pending"], [])
            self.assertEqual(current["fake_sends"], [])
            self.assertEqual(current["history"], [])

    def test_missing_key_creates_no_state_outbox_or_unknown_cost_row(self):
        state = self.conversation()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            status, result = self.turn(state)
        self.assertEqual(status, 200)
        self.assertFalse(result["turn"]["ok"])
        self.assertIn("OPENROUTER_API_KEY", result["turn"]["error"])
        self.assertEqual(result["active_requests"], [])
        self.assertEqual(result["outbox"], [])
        self.assertEqual(result["fake_sends"], [])
        self.assertEqual(self.rows(), [])

    def test_invalid_semantic_gets_at_most_one_repair(self):
        state = self.conversation()
        with patch("sandbox.server.call_luna", return_value=model_result(sem={})) as luna:
            _, result = self.turn(state)
        self.assertEqual(luna.call_count, 2)
        self.assertFalse(result["turn"]["ok"])
        self.assertEqual(result["turn"]["semantic"], {})
        self.assertEqual(result["active_requests"], [])
        self.assertEqual(result["outbox"], [])
        self.assertEqual([row["kind"] for row in self.rows()], ["call", "repair"])

    def test_one_repair_can_recover(self):
        state = self.conversation()
        with patch("sandbox.server.call_luna", side_effect=[model_result(sem={}), model_result()]):
            _, result = self.turn(state)
        self.assertTrue(result["turn"]["ok"], result)
        self.assertEqual(result["turn"]["attempts"], 2)
        self.assertAlmostEqual(result["turn"]["cost_usd_est"], 0.0002)

    def test_review_preserves_engine_clarification_without_fake_send(self):
        state = self.conversation()
        sem = meaning()
        sem["uncertainties"] = [{"alternatives": ["price", "buy"], "different_action": True}]
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, result = self.turn(state)
        self.assertIn("REVIEW_AMBIGUOUS", result["turn"]["routing"])
        self.assertEqual(result["active_requests"][0]["status"], "OPEN")
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["fake_sends"], [])
        self.assertEqual(result["outbox"], [])

    def test_error_route_makes_turn_not_ok(self):
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        with patch("sandbox.server.call_luna", return_value=model_result()), patch.object(
                conversation["engine"], "process",
                return_value={"routing": ["ERROR_ROLLBACK"], "response_text": None,
                              "sends": [], "errors": ["rollback"], "outbound_permission": False,
                              "state_changes": []}):
            _, result = self.turn(state)
        self.assertFalse(result["turn"]["ok"])
        self.assertEqual(result["turn"]["routing"], ["ERROR_ROLLBACK"])

    def test_rollback_route_persists_customer_event_without_changing_error(self):
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        comment = "customer-1 failed planning comment"
        with patch("sandbox.server.call_luna", return_value=model_result()), patch.object(
                conversation["engine"], "_plan", side_effect=RuntimeError("plan failed")):
            _, result = self.turn(state, comment)
        self.assertFalse(result["turn"]["ok"])
        self.assertIn("ERROR_ROLLBACK", result["turn"]["routing"])
        event = self.http.app.event(conversation, comment, f"{conversation['id']}-1")
        saved = conversation["engine"].store.conn.execute(
            "SELECT text FROM events WHERE comment_id=?", (event["comment_id"],)).fetchone()
        self.assertEqual(saved[0], comment)

    def test_prior_failed_customer_turn_reaches_luna_and_evidence_validation(self):
        state = self.conversation()
        prior = "Mình đã hỏi về Tucson tuần trước"
        inherited = meaning(text="giá bao nhiêu", evidence="Tucson tuần trước")
        inherited["operations"][0]["slot"]["value"] = "Tucson"
        with patch("sandbox.server.call_luna", side_effect=[
                {"ok": False, "error": "temporary failure", "attempts": 0},
                model_result(inherited)]) as luna:
            self.turn(state, prior)
            _, result = self.turn(state, "giá bao nhiêu")
        self.assertEqual(luna.call_args_list[1].kwargs["context"].count(prior), 1)
        self.assertNotIn("REVIEW_SCHEMA", result["turn"]["routing"], result["turn"])

    def test_model_customer_history_deduplicates_current_text_without_bot_evidence(self):
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        conversation["history"] = [
            {"text": "Earlier customer question"},
            {"text": "Earlier customer question"},
            {"text": "current text"},
            {"text": "not evidence", "response_text": "Bot reply"},
        ]
        context = {"recent": [], "active_requests": [], "pending": [], "parent_texts": []}
        with patch("sandbox.server.call_luna", return_value={"ok": False, "attempts": 0}) as luna:
            self.http.app.model(conversation, "current text", context)
        formatted = luna.call_args.kwargs["context"]
        self.assertEqual(formatted.count("Earlier customer question"), 1)
        self.assertNotIn("current text", formatted)
        self.assertNotIn("Bot reply", formatted)

    def test_uncertainty_without_different_action_keeps_engine_behavior(self):
        state = self.conversation()
        sem = meaning()
        sem["uncertainties"] = [{"alternatives": ["a", "b"], "different_action": False}]
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, result = self.turn(state)
        self.assertTrue(result["turn"]["ok"], result)
        self.assertTrue(result["active_requests"])

    def test_budget_accounts_each_retry_and_denies_next_attempt(self):
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="prior", kind="call", attempts=22, cost_usd_est=.002)
        state = self.conversation()
        def retries(*args, **kwargs):
            guard = kwargs["budget_guard"]
            self.assertTrue(guard())
            self.assertTrue(guard())
            self.assertFalse(guard())
            return dict(ok=False, error="retry failed", attempts=2, cost_usd_est=None, cost_unknown=True)
        with patch("sandbox.server.call_luna", side_effect=retries):
            _, result = self.turn(state)
        self.assertFalse(result["turn"]["ok"])
        self.assertEqual(self.rows()[-1]["attempts"], 2)
        self.assertTrue(self.rows()[-1]["cost_unknown"])
        with patch("sandbox.server.call_luna", side_effect=AssertionError("budget must deny before model")):
            _, result = self.turn(state)
        self.assertIn("BUDGET", result["turn"]["routing"][0])
        self.assertEqual(result["outbox"], [])

    def test_unknown_cost_blocks_future_spend(self):
        state = self.conversation()
        with patch("sandbox.server.call_luna", return_value=model_result(cost=None)):
            self.turn(state)
        self.assertEqual(self.rows()[0]["status"], "unknown_cost")
        with patch("sandbox.server.call_luna", side_effect=AssertionError("unknown cost must block")):
            _, result = self.turn(state)
        self.assertIn("BUDGET", result["turn"]["routing"][0])
        self.assertEqual(len(self.rows()), 1)

    def test_unauthorized_http_failure_with_known_zero_cost_keeps_budget_open(self):
        from scripts import budget_ledger
        state = self.conversation()
        failure = dict(ok=False, error="HTTP 401: missing authentication header", attempts=1,
                       cost_usd_est=0.0, cost_unknown=False, model_effective="openai/gpt-6-luna")
        with patch("sandbox.server.call_luna", return_value=failure):
            _, result = self.turn(state)
        self.assertEqual(result["turn"]["routing"], ["REVIEW_MODEL_ERROR"])
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(budget_ledger.totals(self.ledger), (1, 0.0, 0, 1))
        self.assertTrue(budget_ledger.guard(self.ledger, prior_calls=276, prior_cost=.0854)[0])

    def test_page_actor_id_cannot_enter_customer_recent_context(self):
        state = self.conversation(actor_id="sample-hyundai")
        conversation = self.http.app.conversations[state["id"]]
        context = conversation["engine"].context(self.http.app.event(conversation), conversation["postcard"])
        self.assertEqual(context["recent"], [])
        self.assertIn("Creta", context["parent_texts"][0])

    def test_concurrent_turns_share_one_remaining_attempt(self):
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="prior", kind="call", attempts=23, cost_usd_est=.002)
        first, second = self.conversation(), self.conversation()
        def luna(*args, **kwargs):
            self.assertTrue(kwargs["budget_guard"]())
            time.sleep(.02)
            return model_result()
        barrier = threading.Barrier(2)
        results = []
        def submit(state):
            barrier.wait(timeout=2)
            results.append(self.turn(state))
        with patch("sandbox.server.call_luna", side_effect=luna):
            threads = [threading.Thread(target=submit, args=(state,)) for state in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result[1]["turn"]["ok"] for result in results), 1)
        self.assertEqual(budget_ledger.totals(self.ledger)[0], 24)

    def test_invalid_input_rejected_before_model(self):
        for payload in ({}, {"page_id": "page", "post_text": "x", "actor_id": "a", "vehicles": "Creta"}):
            status, _ = self.request("POST", "/api/conversations", payload)
            self.assertEqual(status, 400)
        state = self.conversation()
        self.assertEqual(self.turn(state, " ")[0], 400)
        self.assertEqual(self.request("GET", "/api/conversations/missing")[0], 404)

    def test_nonlocal_host_rejected_for_get_and_post_even_with_matching_origin(self):
        status, _ = self.request("GET", "/health", host="attacker.example:8766",
                                 origin="http://attacker.example:8766")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/conversations",
                                 {"post_id": "creta", "actor_id": "customer-1"},
                                 host="attacker.example:8766", origin="http://attacker.example:8766")
        self.assertEqual(status, 403)
        self.assertEqual(self.http.app.conversations, {})

    def test_loopback_host_accepts_forwarded_ports(self):
        for host in ("localhost:9876", "127.0.0.1:54321", "[::1]:443"):
            with self.subTest(host=host):
                status, _ = self.request("GET", "/health", host=host)
                self.assertEqual(status, 200)

    def test_cost_guard_reserves_maximum_completion_before_model_call(self):
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="near-cap", kind="call", attempts=1,
                             cost_usd_est=1.9141)
        state = self.conversation()
        def expensive_call(*args, **kwargs):
            self.assertTrue(kwargs["budget_guard"]())
            return model_result(cost=.0006)
        with patch("sandbox.server.call_luna", side_effect=expensive_call) as luna:
            status, result = self.turn(state)
        self.assertEqual(status, 200)
        self.assertFalse(result["turn"]["ok"])
        self.assertIn("BUDGET", result["turn"]["routing"][0])
        luna.assert_not_called()
        self.assertLessEqual(.0854 + budget_ledger.totals(self.ledger)[1], 2.0)

    def test_model_bounds_context_and_repair_note_before_openrouter(self):
        from sandbox.server import CONTEXT_CHAR_LIMIT, REPAIR_CHAR_LIMIT
        import json
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        context = {"recent": [{"text": "comment " + "x" * 10000} for _ in range(5)],
                   "active_requests": [], "pending": [], "parent_texts": ["parent"]}
        with patch("sandbox.server.call_luna", return_value={"ok": False, "attempts": 0}) as luna:
            self.http.app.model(conversation, "current", context, "repair " + "e" * 20000)
        args, kwargs = luna.call_args
        self.assertLessEqual(len(kwargs["context"]), CONTEXT_CHAR_LIMIT + 64)
        encoded = json.loads(kwargs["context"])
        self.assertEqual(encoded["format"], "mat_intent_context_v1")
        self.assertLess(len(encoded["recent"]), 5)
        self.assertEqual(kwargs["parent"], "")
        self.assertEqual(len(kwargs["repair_note"]), REPAIR_CHAR_LIMIT)

        context["parent_texts"] = ["parent " + "p" * 50000]
        with patch("sandbox.server.call_luna") as luna:
            with self.assertRaisesRegex(ValueError, "exceeds budget"):
                self.http.app.model(conversation, "current", context)
        luna.assert_not_called()

    def test_model_includes_only_recent_successfully_sent_bot_replies(self):
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        conversation["history"] = [
            {"outbound_permission": True, "sends": [{"status": "sent_to_fake_ledger"}], "response_text": "Earlier reply"},
            {"outbound_permission": False, "sends": [{"status": "SENT"}], "response_text": "Blocked reply"},
            {"outbound_permission": True, "sends": [{"status": "UNKNOWN"}], "response_text": "Unknown reply"},
            {"outbound_permission": True, "sends": [{"status": "sent_to_fake_ledger"}], "response_text": "Latest reply"},
        ]
        context = {"recent": [], "active_requests": [], "pending": [], "parent_texts": []}
        with patch("sandbox.server.call_luna", return_value={"ok": False, "attempts": 0}) as luna:
            self.http.app.model(conversation, "current", context)
        formatted = luna.call_args.kwargs["context"]
        self.assertLess(formatted.index("Earlier reply"), formatted.index("Latest reply"))
        import json
        replies = json.loads(formatted)["recent_bot_replies"]
        self.assertEqual(replies, [{"text": "Earlier reply", "verified_fact": False},
                                   {"text": "Latest reply", "verified_fact": False}])
        self.assertNotIn("Blocked reply", formatted)
        self.assertNotIn("Unknown reply", formatted)


if __name__ == "__main__":
    unittest.main()
