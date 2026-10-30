"""HTTP sandbox contracts; only the external Luna boundary is mocked."""
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def meaning(text="giá bao nhiêu", evidence="giá bao nhiêu"):
    return {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope", "requests": [{"topic": "price", "desired_outcome": "price", "polarity": "positive"}],
            "operations": [{"op": "START_REQUEST", "request_ref": "topic:price", "evidence_excerpt": evidence,
                            "slot": {"kind": "vehicle", "value": "Creta", "explicit": False}}], "uncertainties": []}


def model_result(sem=None, attempts=1, cost=0.0001, **extra):
    return dict(ok=True, semantic=meaning() if sem is None else sem, raw_text="{}", usage={"prompt_tokens": 500, "completion_tokens": 100},
                cost_usd_est=cost, cost_unknown=cost is None, model_effective="openai/gpt-6-luna", attempts=attempts, **extra)


class SandboxHTTPTests(unittest.TestCase):
    def test_persona_post_uses_bound_facts_without_vehicle_selection(self):
        personas = [{'id': 'tram-post', 'name': 'Bảo Trâm Tiktoker',
                     'car_owned': 'VinFast VF3 vàng nóc trắng', 'occupation': 'Content Creator'}]
        with patch('sandbox.server.Sandbox.personas', return_value=personas), \
             patch('sandbox.server.Sandbox.generate_post', return_value='Chuyện đi làm bằng VF3') as generate:
            state = self.conversation(post_id=None, persona_id='tram-post')
            self.assertEqual(state['post']['vehicles'], ['VinFast VF3 vàng nóc trắng'])
            self.assertNotIn('Hyundai', state['post']['title'])
            snapshot = generate.call_args.args[1]
            self.assertIn('VinFast VF3 vàng nóc trắng', [f['value'] for f in snapshot['facts']])
            self.assertEqual(snapshot['release']['id'], state['post']['release_id'])
            self.assertEqual(state['post']['text'], 'Chuyện đi làm bằng VF3')

    def test_persona_without_car_has_no_default_vehicle_in_post(self):
        with patch('sandbox.server.Sandbox.personas', return_value=[{'id':'no-car','name':'An'}]), \
             patch('sandbox.server.Sandbox.generate_post', return_value='Chia sẻ của An'):
            state = self.conversation(post_id=None, persona_id='no-car')
            self.assertEqual(state['post']['vehicles'], [])

    def setUp(self):
        from sandbox.server import Sandbox
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.tmp.name) / "budget.jsonl")
        self.http = SimpleNamespace(app=Sandbox(ledger_path=self.ledger,
            persona_db_path=str(Path(self.tmp.name) / "personas.sqlite3")))
        if "jev" in self._testMethodName:
            self.http.app.jev_shadow_enabled = True
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        for conversation in self.http.app.conversations.values():
            conversation["engine"].store.conn.close()
            conversation["engine"].sender.receiver.conn.close()
        if self.http.app.persona_conn:
            self.http.app.persona_conn.close()
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
        if payload.get("persona_id"):
            # All non-fixture personas in these contracts are synthetic test rows.
            # This marker is intentionally not evidence of real-person consent.
            payload.setdefault("persona_use_consent", True)
            payload.setdefault("persona_consent_ref", "sandbox-evaluation-test-only")
        status, state = self.request("POST", "/api/conversations", payload)
        self.assertEqual(status, 201, state)
        # These contract tests exercise the pre-generation semantic path; the dedicated
        # generated-sandbox integration tests cover the default draft workflow.
        if payload.get("persona_id"):
            self.http.app.conversations[state["id"]]["generation_mode"] = "legacy"
            state["generation_mode"] = "legacy"
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

    def test_sandbox_does_not_reply_with_sample_price_when_no_verified_facts(self):
        state = self.conversation()
        comment = "Giá niêm yết Creta bản cao bao nhiêu?"
        sem = meaning(evidence=comment)
        sem["operations"].append({"op": "UPDATE_SLOT", "request_ref": "topic:price",
                                  "evidence_excerpt": "Giá niêm yết",
                                  "slot": {"kind": "price_kind", "value": "list",
                                           "evidence_excerpt": "Giá niêm yết", "explicit": True}})
        sem["operations"].append({"op": "UPDATE_SLOT", "request_ref": "topic:price",
                                  "evidence_excerpt": "bản cao",
                                  "slot": {"kind": "variant", "value": "Cao cấp",
                                           "evidence_excerpt": "bản cao", "explicit": True}})
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, result = self.turn(state, comment)
        self.assertNotIn("SAMPLE", (result["turn"].get("response_text") or "").upper())
        self.assertFalse(any("SAMPLE" in json.dumps(send, ensure_ascii=False).upper()
                             for send in result["turn"]["sends"]))

    def test_sandbox_reads_dealers_from_configured_directory(self):
        path = str(Path(self.tmp.name) / "dealers.sqlite3")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dealers(name TEXT, mailing_address TEXT)")
            db.execute("INSERT INTO dealers VALUES (?, ?)", ("Hyundai Hoàng Mai", "987 Tam Trinh, Hoàng Mai, Hà Nội"))
        self.http.app.dealer_db_path = path
        state = self.conversation()
        self.assertEqual(state["dealer_directory"], {"status": "available", "count": 1})
        comment = "Tìm đại lý ở Hà Nội"
        sem = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
               "requests": [{"topic": "dealer", "desired_outcome": "dealer", "polarity": "positive"}],
               "operations": [{"op": "START_REQUEST", "request_ref": "topic:dealer", "topic": "dealer",
                    "evidence_excerpt": comment, "slot": {"kind": "location", "value": "Hà Nội",
                    "evidence_excerpt": "Hà Nội", "explicit": True}}], "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, result = self.turn(state, comment)
        reply = result["turn"].get("response_text") or ""
        self.assertIn("Hyundai Hoàng Mai", reply)
        self.assertIn("987 Tam Trinh", reply)
        self.assertNotIn("SAMPLE", reply.upper())
        maps = result["turn"]["dealer_maps"]
        self.assertEqual(len(maps), 1)
        self.assertEqual(maps[0]["name"], "Hyundai Hoàng Mai")
        self.assertTrue(maps[0]["url"].startswith("https://www.google.com/maps/search/?api=1&query="))

    def test_dealer_follow_up_location_reads_directory(self):
        path = str(Path(self.tmp.name) / "dealers-followup.sqlite3")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dealers(name TEXT, mailing_address TEXT)")
            db.execute("INSERT INTO dealers VALUES (?, ?)", ("Hyundai Hoàng Mai", "987 Tam Trinh, Hoàng Mai, Hà Nội"))
        self.http.app.dealer_db_path = path
        state = self.conversation()
        question = "Tìm đại lý Hyundai"
        sem = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
               "requests": [{"topic": "dealer", "desired_outcome": "dealer", "polarity": "positive"}],
               "operations": [{"op": "START_REQUEST", "request_ref": "topic:dealer", "topic": "dealer",
                               "evidence_excerpt": question}], "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, first = self.turn(state, question)
        self.assertIn("ask_clarification", first["turn"]["routing"])
        request_id, pending_id = first["active_requests"][0]["id"], first["pending"][0]["id"]
        sem["operations"] = [{"op": "ANSWER_PENDING", "request_ref": request_id, "pending_id": pending_id,
                              "evidence_excerpt": "Hà Nội", "slot": {"kind": "location", "value": "Hà Nội",
                              "evidence_excerpt": "Hà Nội", "explicit": True}}]
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, second = self.turn(state, "Hà Nội")
        self.assertIn("Hyundai Hoàng Mai", second["turn"].get("response_text") or "")
        self.assertEqual(len(second["turn"]["dealer_maps"]), 1)

    def test_dealer_reply_and_map_do_not_expose_embedded_personal_contact(self):
        from urllib.parse import parse_qs, urlsplit
        path = str(Path(self.tmp.name) / "dealers-contact.sqlite3")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dealers(name TEXT, mailing_address TEXT)")
            db.execute("INSERT INTO dealers VALUES (?, ?)", ("Hyundai Hiệp Bình",
                "Nguyễn A - 0901234567    Số 123, Quốc lộ 13, Phường Hiệp Bình, TP. HCM"))
            db.execute("INSERT INTO dealers VALUES (?, ?)", ("Hyundai Thủ Đức",
                "Nguyễn B Số 456, Quốc lộ 13, Phường Hiệp Bình, TP. HCM"))
        self.http.app.dealer_db_path = path
        state = self.conversation()
        text = "Tìm đại lý ở Sài Gòn"
        sem = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
               "requests": [{"topic": "dealer", "desired_outcome": "dealer", "polarity": "positive"}],
               "operations": [{"op": "START_REQUEST", "request_ref": "topic:dealer", "topic": "dealer",
                   "evidence_excerpt": text, "slot": {"kind": "location", "value": "TP.HCM",
                   "evidence_excerpt": "Sài Gòn", "explicit": True}}], "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(sem)):
            _, result = self.turn(state, text)
        turn = result["turn"]
        self.assertIn("Số 123", turn["response_text"])
        self.assertNotIn("Nguyễn A", turn["response_text"])
        self.assertNotIn("Nguyễn B", turn["response_text"])
        self.assertNotIn("0901234567", turn["response_text"])
        queries = [parse_qs(urlsplit(link["url"]).query)["query"][0] for link in turn["dealer_maps"]]
        self.assertTrue(any("Số 123" in query for query in queries))
        self.assertTrue(any("Số 456" in query for query in queries))
        self.assertTrue(all("Nguyễn A" not in query and "Nguyễn B" not in query and "0901234567" not in query
                            for query in queries))

    def test_sandbox_jev_shadow_is_observable_without_blocking_luna(self):
        finished = threading.Event()
        class FakeJev:
            def decide(self, state, questions, **kwargs):
                finished.set()
                self_state.append(state)
                return {"ok": True, "attempts": 1, "model_effective": "typesafe/jev-1.14",
                        "latency_ms": 12.5, "usage": {"input_tokens": 40, "output_tokens": 3},
                        "cost_usd_est": 0.00002, "cost_unknown": False,
                        "answers": {key: {"type": "noul", "noul": 0.7 if key == "price" else 0.01}
                                    for key in questions}}
        self_state = []
        self.http.app.jev_adapter = FakeJev()
        self.http.app.jev_trace_path = str(Path(self.tmp.name) / "jev_trace.jsonl")
        state = self.conversation()
        comment = "Giá lăn bánh Creta bao nhiêu?"
        with patch("sandbox.server.call_luna", return_value=model_result(meaning(evidence=comment))):
            status, result = self.turn(state, comment)
        self.assertEqual(status, 200, result)
        shadow_id = result["turn"]["jev_shadow_event_id"]
        self.assertTrue(finished.wait(1))
        for _ in range(50):
            status, shadow = self.request("GET", f"/api/conversations/{state['id']}/jev/{shadow_id}")
            if shadow["status"] != "pending": break
            time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertEqual(shadow["status"], "complete")
        self.assertEqual(shadow["predictions"]["price"], 0.7)
        self.assertEqual(shadow["model_effective"], "typesafe/jev-1.14")
        self.assertNotIn(comment, json.dumps(shadow))
        self.assertEqual(len(self_state), 1)
        self.assertIn(comment, self_state[0]["context"])
        trace = json.loads(Path(self.http.app.jev_trace_path).read_text().splitlines()[0])
        self.assertEqual(trace["decisions"]["price"]["probability"], 0.7)
        self.assertEqual(trace["question_version"], "jev_intent_questions_v1")
        self.assertEqual(trace["model_effective"], "typesafe/jev-1.14")
        self.assertEqual(len(trace["event_hash"]), 64)
        self.assertNotIn(comment, json.dumps(trace))

    def test_sandbox_jev_shadow_is_bounded_and_invalid_answers_are_hidden(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        class BlockingJev:
            def decide(self, state, questions, **kwargs):
                calls.append(state)
                started.set()
                release.wait(2)
                return {"ok": True, "attempts": 1, "answers": {key: {"type": "noul", "noul": 1.2}
                        for key in questions}}
        self.http.app.jev_adapter = BlockingJev()
        state = self.conversation()
        first = self.http.app.start_jev_shadow(self.http.app.conversations[state["id"]], "event-1", "{}")
        self.assertTrue(started.wait(1))
        duplicate = self.http.app.start_jev_shadow(self.http.app.conversations[state["id"]], "event-1", "different")
        self.assertEqual(duplicate, first)
        self.assertEqual(len(calls), 1)
        second = self.http.app.start_jev_shadow(self.http.app.conversations[state["id"]], "event-2", "{}")
        self.assertEqual(self.http.app.conversations[state["id"]]["jev_shadows"][second]["status"], "skipped")
        release.set()
        for _ in range(100):
            record = self.http.app.conversations[state["id"]]["jev_shadows"].get(first, {})
            if record.get("status") != "pending": break
            time.sleep(0.01)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["predictions"], {})
        self.assertNotIn("answers", record)
        shadow_ledger = [json.loads(line) for line in Path(self.ledger).read_text().splitlines()
                         if line.strip() and json.loads(line).get("kind") == "shadow"]
        self.assertEqual(shadow_ledger[-1]["attempts"], 1)
        self.assertIsNone(shadow_ledger[-1]["cost_usd_est"])

    def test_jev_blocked_model_error_is_visible_without_provider_payload(self):
        class BlockedJev:
            def decide(self, state, questions, **kwargs):
                return {"ok": False, "attempts": 1, "cost_unknown": True,
                        "error_code": "MODEL_BLOCKED", "http_status": 404,
                        "error": "private provider payload"}
        self.http.app.jev_adapter = BlockedJev()
        self.http.app.jev_trace_path = str(Path(self.tmp.name) / "jev_trace.jsonl")
        state = self.conversation()
        event_id = self.http.app.start_jev_shadow(self.http.app.conversations[state["id"]], "blocked-1", "{}")
        for _ in range(100):
            _, result = self.request("GET", f"/api/conversations/{state['id']}/jev/{event_id}")
            if result["status"] != "pending": break
            time.sleep(0.01)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "MODEL_BLOCKED")
        self.assertEqual(result["http_status"], 404)
        self.assertNotIn("private provider payload", json.dumps(result))

    def test_jev_trace_write_failure_does_not_change_luna_result(self):
        class FakeJev:
            def decide(self, state, questions, **kwargs):
                return {"ok": True, "attempts": 1,
                    "answers": {key: {"type": "noul", "noul": 0.1} for key in questions}}
        self.http.app.jev_adapter = FakeJev()
        state = self.conversation()
        comment = "Giá lăn bánh Creta bao nhiêu?"
        with patch("scripts.model_trace.append", side_effect=OSError("private path")), \
             patch("sandbox.server.call_luna", return_value=model_result(meaning(evidence=comment))):
            status, result = self.turn(state, comment)
            event_id = result["turn"]["jev_shadow_event_id"]
            for _ in range(100):
                shadow = self.http.app.conversations[state["id"]]["jev_shadows"].get(event_id, {})
                if shadow.get("status") != "pending": break
                time.sleep(0.01)
        self.assertEqual(status, 200)
        self.assertTrue(result["turn"]["ok"])
        self.assertEqual(shadow["status"], "complete")
        self.assertEqual(shadow["trace_status"], "failed")
        self.assertNotIn("private path", json.dumps(shadow))

        class RaiseAfterRequest:
            def decide(self, state, questions, **kwargs):
                self.assert_guard = kwargs["budget_guard"]()
                raise TimeoutError("raw private context must not reach result")
        self.http.app.jev_adapter = RaiseAfterRequest()
        event_id = self.http.app.start_jev_shadow(self.http.app.conversations[state["id"]], "event-3", "{}")
        for _ in range(100):
            record = self.http.app.conversations[state["id"]]["jev_shadows"].get(event_id, {})
            if record.get("status") != "pending": break
            time.sleep(0.01)
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("private context", json.dumps(record))
        shadow_ledger = [json.loads(line) for line in Path(self.ledger).read_text().splitlines()
                         if line.strip() and json.loads(line).get("kind") == "shadow"]
        self.assertEqual(shadow_ledger[-1]["attempts"], 1)
        self.assertIsNone(shadow_ledger[-1]["cost_usd_est"])

    def test_health_keeps_sandbox_open_by_default_with_uncapped_warning(self):
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="unknown-test", kind="call", attempts=1,
                             cost_usd_est=None)
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["budget_ok"])
        self.assertFalse(health["budget_enforced"])
        self.assertIn("uncapped", health["budget_warning"].lower())

    def test_persona_selection_generates_post_for_chat_context(self):
        personas = [{"id": "advisor", "name": "Tư vấn viên", "car_owned": "Creta"}]
        with patch("sandbox.server.Sandbox.personas", return_value=personas), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Bài viết về Creta"):
            status, result = self.request("GET", "/api/personas")
            self.assertEqual(status, 200)
            self.assertTrue(any(item["id"] == "advisor" for item in result["personas"]))
            self.assertNotIn("vehicles", result)
            state = self.conversation(post_id=None, persona_id="advisor", vehicle="Creta")
        binding = self.http.app.persona_store.get_binding_snapshot(state['page_id'], state['post']['id'])
        self.assertEqual(state["post"]["persona_id"], binding['persona_uuid'])
        self.assertEqual(state["post"]["vehicles"], ["Creta"])
        self.assertIn("Creta", state["post"]["text"])

        def luna(post, text, **kwargs):
            self.assertEqual(post, state["post"]["text"])
            self.assertIn("Creta", kwargs["context"])
            self.assertTrue(kwargs["budget_guard"]())
            return model_result(meaning(evidence="Giá lăn bánh Creta"))
        with patch("sandbox.server.call_luna", side_effect=luna):
            status, result = self.turn(state, "Giá lăn bánh Creta bao nhiêu?")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["turn"]["ok"], result["turn"])
        self.assertGreaterEqual(result["turn"]["pipeline_latency_ms"], result["turn"]["model_latency_ms"])
        self.assertEqual(result["turn"]["model"], "openai/gpt-6-luna")
        self.assertEqual(result["turn"]["usage"], {"prompt_tokens": 500, "completion_tokens": 100,
                                                     "total_tokens": 600})
        self.assertTrue(result["turn"]["usage_complete"])

    def test_fictional_persona_release_answers_and_persists_root_semantic(self):
        status, personas = self.request("GET", "/api/personas")
        self.assertEqual(status, 200)
        fixture = next(item for item in personas["personas"] if item["slug"] == "nam_creta")
        with patch("sandbox.server.Sandbox.generate_post", return_value="Cùng trao đổi về Creta."):
            state = self.conversation(post_id=None, persona_id=fixture["id"], vehicle="Creta")
        self.assertTrue(state["persona_release"]["fixture_only"])
        self.assertFalse(state["persona_release"]["release"]["approved"])

        first_text = "Bạn ở đâu vậy?"
        first_semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope", "requests": [{"topic": "social", "desired_outcome": "ack",
            "polarity": "positive"}], "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": first_text}],
            "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(first_semantic)):
            _, first = self.turn(state, first_text)
        self.assertTrue(first["turn"]["response_text"].startswith("Mình ở Hà Nội."))
        # A question about Nam is answered; it is not turned into a sales question.
        self.assertEqual(first["turn"]["response_text"].count('?'), 0)
        self.assertTrue(first["turn"]["response_plan"]["fictional_fixture"])
        root_record = self.http.app.persona_store.root_analysis(
            state["page_id"], state["post"]["id"], state["thread_id"])
        self.assertEqual(root_record["root_text"], first_text)
        self.assertEqual(root_record["root_actor_id"], "customer-1")
        self.assertEqual(root_record["semantic"], first["turn"]["semantic"])
        self.assertEqual(root_record["reply_text"], first["turn"]["response_text"])

        second_text = "Bạn đang đi xe gì?"
        captured = {}
        def luna(_post, _text, **kwargs):
            captured["context"] = json.loads(kwargs["context"])
            semantic = dict(first_semantic)
            semantic["operations"] = [{"op": "SOCIAL_ACK", "evidence_excerpt": second_text}]
            return model_result(semantic)
        with patch("sandbox.server.call_luna", side_effect=luna):
            _, second = self.turn(first, second_text)
        root = next(message for message in captured["context"]["messages"]
                    if message.get("role") == "root")
        self.assertEqual(root["text"], first_text)
        self.assertEqual(root["actor_id"], "customer-1")
        self.assertEqual(root["semantic"], first["turn"]["semantic"])
        self.assertTrue(second["turn"]["response_text"].startswith("Mình đang đi Hyundai Creta."), second["turn"])

    def test_fixture_demo_two_actors_persona_sample_price_and_handoff(self):
        _, personas = self.request("GET", "/api/personas")
        fixture = next(p for p in personas["personas"] if p["slug"] == "nam_creta")
        with patch('sandbox.server.Sandbox.generate_post', return_value='Cùng trao đổi về Hyundai Creta.'):
            state = self.conversation(post_id=None, persona_id=fixture["id"])
        captured = []
        def model(_post, text, **kwargs):
            captured.append(json.loads(kwargs["context"]))
            base = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
                    "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
                    "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}
            if text.startswith("Giá niêm yết"):
                base["requests"] = [{"topic": "price", "desired_outcome": "price", "polarity": "positive"}]
                base["operations"] = [
                    {"op": "START_REQUEST", "request_ref": "topic:price", "evidence_excerpt": "Giá",
                     "slot": {"kind": "vehicle", "value": "Creta", "explicit": True, "evidence_excerpt": "Creta"}},
                    {"op": "UPDATE_SLOT", "request_ref": "topic:price", "evidence_excerpt": "bản cao",
                     "slot": {"kind": "variant", "value": "Cao cấp", "explicit": True, "evidence_excerpt": "bản cao"}},
                    {"op": "UPDATE_SLOT", "request_ref": "topic:price", "evidence_excerpt": "Giá niêm yết",
                     "slot": {"kind": "price_kind", "value": "list", "explicit": True, "evidence_excerpt": "Giá niêm yết"}}]
            elif text.startswith("Tôi muốn mua"):
                base["requests"] = [{"topic": "buy", "desired_outcome": "buy_intent", "polarity": "positive"}]
                base["operations"] = [{"op": "START_REQUEST", "request_ref": "topic:buy", "evidence_excerpt": "mua",
                    "slot": {"kind": "vehicle", "value": "Creta", "explicit": True, "evidence_excerpt": "Creta"}}]
            elif text.startswith("Xe bị hỏng"):
                base["requests"] = [{"topic": "complaint", "desired_outcome": "complaint", "polarity": "negative"}]
                base["operations"] = [{"op": "REPORT_COMPLAINT", "request_ref": "topic:complaint", "evidence_excerpt": "hỏng điều hòa"}]
            return model_result(base, attempts=0)
        turns = [
            ("customer-1", "Bạn ở đâu vậy?", "Hà Nội"),
            ("customer-1", "Bạn đang đi xe gì?", "Hyundai Creta"),
            ("customer-1", "Bạn mua xe khi nào?", "3/2024"),
            ("customer-1", "Bạn mua xe trả góp không?", "35%"),
            ("customer-1", "Xe của bạn mua bao giờ?", "3/2024"),
            ("customer-2", "Bạn ở quận nào?", "Cầu Giấy"),
            ("customer-2", "Bạn mua xe ở đại lý nào?", "chưa có thông tin xác nhận"),
            ("customer-2", "Giá niêm yết Creta bản cao bao nhiêu?", "chưa có dữ liệu xác nhận"),
            ("customer-1", "Tôi muốn mua Creta", None),
            ("customer-2", "Xe bị hỏng điều hòa", None),
        ]
        results = []
        with patch("sandbox.server.call_luna", side_effect=model), patch.object(self.http.app, "start_jev_shadow", return_value=None):
            for actor, text, expected in turns:
                status, state = self.request("POST", f"/api/conversations/{state['id']}/turn", {"text": text, "actor_id": actor})
                self.assertEqual(status, 200, state)
                turn = state["turn"]
                results.append(turn)
                if expected:
                    self.assertIn(expected, turn["response_text"] or "", turn)
        self.assertNotIn("Bạn mua xe khi nào?", json.dumps(captured[5], ensure_ascii=False))
        self.assertTrue(results[0]["response_plan"]["fact_ids"])
        self.assertFalse(state["sample_facts"])
        self.assertNotIn("SAMPLE", results[7]["response_text"] or "")
        self.assertFalse(results[7]["response_plan"].get("facts_used"))
        self.assertIn("HANDOFF_DRAFT_ONLY", results[9]["routing"])
        self.assertFalse(results[9]["sends"])
        self.assertTrue(any(e["purpose"] == "handoff_draft" for e in results[9]["effects"]))

    def test_persona_tone_changes_wording_without_changing_intent_or_pending(self):
        personas = [{"id": "warm", "name": "An", "tone": "thân thiện"},
                    {"id": "neutral", "name": "Bình", "tone": "trung tính"}]
        question = "Tìm đại lý Hyundai"
        sem = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
               "requests": [{"topic": "dealer", "desired_outcome": "dealer", "polarity": "positive"}],
               "operations": [{"op": "START_REQUEST", "request_ref": "topic:dealer", "topic": "dealer",
                               "evidence_excerpt": question}], "uncertainties": []}
        with patch("sandbox.server.Sandbox.personas", return_value=personas), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Bài viết về Creta"), \
             patch("sandbox.server.call_luna", return_value=model_result(sem)):
            warm = self.conversation(post_id=None, persona_id="warm", vehicle="Creta")
            neutral = self.conversation(post_id=None, persona_id="neutral", vehicle="Creta")
            _, warm_result = self.turn(warm, question)
            _, neutral_result = self.turn(neutral, question)
        self.assertNotEqual(warm_result["turn"]["response_text"], neutral_result["turn"]["response_text"])
        self.assertEqual(warm_result["turn"]["routing"], neutral_result["turn"]["routing"])
        self.assertEqual(warm_result["pending"][0]["expected_slot"], neutral_result["pending"][0]["expected_slot"])

    def test_chat_usage_is_incomplete_when_provider_retries_are_unreported(self):
        state = self.conversation()
        with patch("sandbox.server.call_luna", return_value=model_result(attempts=2)):
            status, result = self.turn(state)
        self.assertEqual(status, 200)
        self.assertEqual(result["turn"]["attempts"], 2)
        self.assertFalse(result["turn"]["usage_complete"])

    def test_reasoning_token_count_is_available_when_provider_reports_it(self):
        state = self.conversation()
        response = model_result()
        response["usage"]["completion_tokens_details"] = {"reasoning_tokens": 60}
        with patch("sandbox.server.call_luna", return_value=response):
            _, result = self.turn(state)
        self.assertEqual(result["turn"]["model_calls"][0]["reasoning_tokens"], 60)

    def test_total_tokens_are_summed_per_call_when_provider_omits_total(self):
        state = self.conversation()
        first = model_result(sem={}, cost=.0001)
        first["usage"]["total_tokens"] = 123
        second = model_result(cost=.0001)
        with patch("sandbox.server.call_luna", side_effect=[first, second]):
            status, result = self.turn(state)
        self.assertEqual(status, 200)
        self.assertEqual(result["turn"]["usage"]["total_tokens"], 123 + 500 + 100)
        self.assertTrue(result["turn"]["usage_complete"])

    def test_pipeline_latency_includes_response_state_build(self):
        state = self.conversation()
        original_state = self.http.app.state
        def delayed_state(conversation):
            time.sleep(.025)
            return original_state(conversation)
        with patch("sandbox.server.call_luna", return_value=model_result()), \
             patch.object(self.http.app, "state", side_effect=delayed_state):
            status, result = self.turn(state)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["turn"]["pipeline_latency_ms"], 20)

    def test_database_persona_is_available_for_post_creation(self):
        personas = [{"id": "201", "name": "Người đi làm", "tone": "chân thành"}]
        with patch("sandbox.server.Sandbox.personas", return_value=personas), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Bài viết dài về Tucson") as generate:
            status, result = self.request("GET", "/api/personas")
            self.assertEqual(status, 200)
            self.assertEqual(result["personas"][0]["id"], "201")
            state = self.conversation(post_id=None, persona_id="201", vehicle="Tucson")
            self.assertEqual(state["post"]["text"], "Bài viết dài về Tucson")
            self.assertEqual(generate.call_args.args[0], personas[0])
            self.assertEqual(generate.call_args.args[1]['release']['id'], state['post']['release_id'])
            self.assertEqual(state['post']['vehicles'], [])
            self.assertEqual(state['post']['persona_origin'], 'database_snapshot')
            self.assertEqual(set(generate.call_args.kwargs), {"_metrics", "owner_id"})

    def test_database_persona_can_start_without_consent_fields(self):
        personas = [{"id": "db-direct", "name": "Synthetic DB person"}]
        with patch("sandbox.server.Sandbox.personas", return_value=personas), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Bài viết"):
            status, result = self.request("POST", "/api/conversations",
                {"actor_id": "customer-1", "persona_id": "db-direct"})
        self.assertEqual(status, 201, result)
        self.assertEqual(result['post']['persona_origin'], 'database_snapshot')

    def test_database_persona_source_id_stays_server_side_and_snapshot_is_not_reimported(self):
        import psycopg
        source_id = 'upstream-private-person-731'
        source = {"id": source_id, "name": "Synthetic DB person", "occupation": "Giáo viên",
                  "car_owned": "Hyundai Tucson", "tone": "warm", "category": "user"}
        with patch.dict(os.environ, {"DB_Host": "db.example", "DB_Username": "sandbox",
                                    "DB_Password": "unused"}), \
             patch("psycopg.connect") as connect, \
             patch.object(self.http.app, "generate_post", return_value="Bài viết demo"):
            connect.return_value.__enter__.return_value.execute.return_value = [source]
            status, listed = self.request("GET", "/api/personas")
            self.assertEqual(status, 200, listed)
            selected = next(persona for persona in listed["personas"]
                            if persona["name"] == source["name"])
            self.assertNotEqual(selected["id"], source_id)
            self.assertNotIn(source_id, json.dumps(listed))
            state = self.http.app.create({"actor_id": "customer-1", "persona_id": selected["id"]})
            self.assertNotIn(source_id, json.dumps(state))
            binding = self.http.app.persona_store.get_binding_snapshot(
                state["page_id"], state["post"]["id"])
            self.assertEqual({fact["key"] for fact in binding["facts"]},
                             {"identity.name", "occupation.title", "vehicle.current.model"})
            options = self.http.app.personas()
            self.assertEqual(sum(persona["name"] == source["name"] for persona in options), 1)

    def test_database_persona_consent_attestation_is_not_added_to_model_prompt(self):
        personas = [{"id": "db-prompt-scope", "name": "Synthetic DB person"}]
        captured = []

        def luna(*args, **kwargs):
            captured.append(kwargs)
            return model_result()

        with patch("sandbox.server.Sandbox.personas", return_value=personas), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Synthetic database persona post."), \
             patch("sandbox.server.call_luna", side_effect=luna):
            state = self.conversation(post_id=None, persona_id="db-prompt-scope")
            status, result = self.turn(state, "giá bao nhiêu")

        self.assertEqual(status, 200, result)
        self.assertTrue(captured)
        self.assertNotIn("sandbox-evaluation-test-only", captured[0]["context"])
        self.assertNotIn("sandbox-evaluation-test-only", str(captured[0].get("persona_context", "")))

    def test_database_persona_uses_own_demo_facts_and_peer_flow(self):
        persona = {"id": "21", "name": "Coach Tuấn Gym", "car_owned": "Ford Ranger XLS",
                   "occupation": "Huấn luyện viên thể hình", "tone": "thân thiện"}
        def luna(_post, text, **kwargs):
            social = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
                "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
                "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}
            if kwargs.get('persona_phase') == 'analysis':
                topic, key, intent = None, None, 'social'
                asks = False
                if 'đi bằng xe gì' in text:
                    topic, key, asks = 'vehicle_current', 'vehicle.current.model', True
                elif 'mua ở đâu' in text:
                    topic, key, asks = 'purchase_finance', 'purchase.dealer', True
                elif 'cũng tính mua' in text:
                    intent = 'buying_interest'
                peer = {"speaker_intent": intent, "address_term": None,
                    "persona_topic": topic, "asks_persona_question": asks,
                    "claims": [], "evidence_excerpt": text}
                return model_result(social, peer_signals=peer,
                    needs={"topics": [topic] if topic else [], "keys": [key] if key else []})
            if text.startswith('chào'):
                candidate = {"reply_text": "Chào bạn!", "fact_ids": [], "memory_ids": [],
                    "claims": [], "asks_question": False, "question": None, "fact_proposals": []}
            elif 'đi bằng xe gì' in text:
                fact = next(fact for fact in kwargs['persona_context']['facts']
                            if fact['key'] == 'vehicle.current.model')
                reply = 'Mình đang đi Ford Ranger XLS.'
                candidate = {"reply_text": reply, "fact_ids": [fact['fact_id']], "memory_ids": [],
                    "claims": [{"text": reply, "subject": "persona", "predicate": fact['key'],
                        "fact_ids": [fact['fact_id']], "memory_ids": [], "message_ids": []}],
                    "asks_question": False, "question": None, "fact_proposals": []}
            elif 'cũng tính mua' in text:
                question = 'Bạn đang nghiêng về mẫu xe nào?'
                candidate = {"reply_text": question, "fact_ids": [], "memory_ids": [], "claims": [],
                    "asks_question": True, "question": {"kind": "lead", "field": "interested_vehicle",
                        "text": question}, "fact_proposals": []}
            else:
                return model_result(social, reply_candidate=None)
            return model_result(social, reply_candidate=candidate)
        with patch("sandbox.server.Sandbox.personas", return_value=[persona]), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Mình tập gym và chia sẻ chuyện xe."), \
             patch("sandbox.server.call_luna", side_effect=luna):
            state = self.conversation(post_id=None, persona_id="21", vehicle="Creta")
            self.http.app.conversations[state['id']]['generation_mode'] = 'generated'
            self.assertFalse(state['post']['persona_fixture_only'])
            self.assertFalse(state['sample_facts'])
            _, response = self.turn(state, 'chào bác')
            self.assertTrue(response['turn']['outbound_permission'], response['turn'])
            self.assertNotIn('Nam', response['turn']['response_text'] or '')
            _, response = self.turn(state, 'tập gym bác đi bằng xe gì')
            self.assertIn('Ford Ranger XLS', response['turn']['response_text'] or '')
            self.assertNotIn('Hyundai Creta', response['turn']['response_text'] or '')
            _, response = self.turn(state, 'xe đó bác mua ở đâu')
            self.assertFalse(response['turn']['outbound_permission'])
            self.assertNotIn('đại lý', response['turn']['response_text'] or '')
            self.assertEqual(response['pending'], [])
            _, response = self.turn(state, 'tôi cũng tính mua')
            self.assertTrue(response['turn']['outbound_permission'], response['turn'])
            self.assertEqual(response['pending'][0]['expected_slot'], 'interested_vehicle')

    def test_database_persona_mixed_purchase_and_opinion_uses_own_facts(self):
        persona = {'id': 'tram-test', 'name': 'Bảo Trâm Tiktoker',
                   'car_owned': 'VinFast VF3 vàng nóc trắng', 'occupation': 'Content Creator'}
        def luna(_post, text, **kwargs):
            social = {'schema_version': '1', 'sentiment': 'NEU', 'sarcasm': 'absent', 'scope': 'in_scope',
                'requests': [{'topic': 'social', 'desired_outcome': 'ack', 'polarity': 'positive'}],
                'operations': [{'op': 'SOCIAL_ACK', 'evidence_excerpt': text}], 'uncertainties': []}
            if kwargs.get('persona_phase') == 'analysis':
                claims = []
                topic, asks = None, False
                if text.startswith('cũng tính mua'):
                    claims = [{'predicate': 'interested_vehicle', 'value': 'Creta',
                               'evidence_excerpt': 'cũng tính mua creta'}]
                    intent = 'buying_interest'
                else:
                    intent, topic, asks = 'buying_interest', 'vehicle_current', True
                peer = {'speaker_intent': intent, 'address_term': None, 'persona_topic': topic,
                    'asks_persona_question': asks, 'claims': claims, 'evidence_excerpt': text}
                needs = {'topics': ['vehicle_current'], 'keys': ['vehicle.current.model']} if asks else {'topics': [], 'keys': []}
                return model_result(social, peer_signals=peer, needs=needs)
            if text.startswith('cũng tính mua'):
                reply = 'Ừ, bạn cứ cân nhắc thêm nhé.'
                return model_result(social, reply_candidate={'reply_text': reply, 'fact_ids': [],
                    'memory_ids': [], 'claims': [], 'asks_question': False, 'question': None,
                    'fact_proposals': []})
            fact = next(fact for fact in kwargs['persona_context']['facts']
                        if fact['key'] == 'vehicle.current.model')
            reply = 'Mình đang đi VinFast VF3 vàng nóc trắng.'
            return model_result(social, reply_candidate={'reply_text': reply, 'fact_ids': [fact['fact_id']],
                'memory_ids': [], 'claims': [{'text': reply, 'subject': 'persona', 'predicate': fact['key'],
                    'fact_ids': [fact['fact_id']], 'memory_ids': [], 'message_ids': []}],
                'asks_question': False, 'question': None, 'fact_proposals': []})
        with patch('sandbox.server.Sandbox.personas', return_value=[persona]), \
             patch('sandbox.server.Sandbox.generate_post', return_value='Bài viết demo'), \
             patch('sandbox.server.call_luna', side_effect=luna):
            state = self.conversation(post_id=None, persona_id='tram-test', vehicle='Creta')
            self.http.app.conversations[state['id']]['generation_mode'] = 'generated'
            _, result = self.turn(state, 'cũng tính mua creta nè chị')
            self.assertNotIn('đang đi xe đó rồi hay', result['turn']['response_text'] or '')
            self.assertEqual(result['lead_profile']['interested_vehicle']['value'], 'Creta')
            _, result = self.turn(state, 'tính mua nè, bạn đi thấy sao')
            reply = result['turn']['response_text'] or ''
            self.assertTrue(result['turn']['outbound_permission'], result['turn'])
            self.assertIn('VinFast VF3', reply)
            self.assertNotIn('Mình đang đi Creta', reply)
            self.assertTrue(result['turn']['response_plan']['fact_ids'])
            self.assertFalse(result['pending'])

    def test_database_persona_occupation_question_can_survive_model_scope_misclassification(self):
        persona = {"id": "8", "name": "Thầy Tuấn", "occupation": "Giảng viên Đại học Bách Khoa"}
        text = 'anh làm nghề gì'
        semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "out_of_scope",
                    "requests": [], "operations": [{"op": "UNRESOLVED", "reason": "off topic", "evidence_excerpt": text}],
                    "uncertainties": []}
        analyses = 0
        def luna(_post, comment, **kwargs):
            nonlocal analyses
            if kwargs.get('persona_phase') == 'analysis':
                analyses += 1
                current = dict(semantic)
                if analyses > 1:
                    current['sarcasm'] = 'uncertain'
                return model_result(current, needs={'topics': ['occupation'], 'keys': ['occupation.title']})
            fact = next(fact for fact in kwargs['persona_context']['facts']
                        if fact['key'] == 'occupation.title')
            reply = 'Mình là Giảng viên Đại học Bách Khoa.'
            return model_result(reply_candidate={'reply_text': reply, 'fact_ids': [fact['fact_id']],
                'memory_ids': [], 'claims': [{'text': reply, 'subject': 'persona',
                    'predicate': 'occupation.title', 'fact_ids': [fact['fact_id']],
                    'memory_ids': [], 'message_ids': []}], 'asks_question': False,
                'question': None, 'fact_proposals': []})
        with patch("sandbox.server.Sandbox.personas", return_value=[persona]), \
             patch("sandbox.server.Sandbox.generate_post", return_value="Bài viết demo"), \
             patch("sandbox.server.call_luna", side_effect=luna):
            state = self.conversation(post_id=None, persona_id="8", vehicle="Creta")
            self.http.app.conversations[state['id']]['generation_mode'] = 'generated'
            _, result = self.turn(state, text)
            self.assertIn('Giảng viên Đại học Bách Khoa', result['turn']['response_text'] or '', result['turn'])
            _, result = self.turn(state, text)
            self.assertFalse(result['turn'].get('outbound_permission'))

    def test_database_persona_without_vehicle_does_not_invent_one(self):
        persona = {'id': '99', 'name': 'Nhân vật mẫu', 'car_owned': 'chưa có xe'}
        text = 'bác đi xe gì'
        semantic = {'schema_version': '1', 'sentiment': 'NEU', 'sarcasm': 'absent', 'scope': 'in_scope',
                    'requests': [{'topic': 'social', 'desired_outcome': 'question', 'polarity': 'unknown'}],
                    'operations': [{'op': 'SOCIAL_ACK', 'evidence_excerpt': text}], 'uncertainties': []}
        with patch('sandbox.server.Sandbox.personas', return_value=[persona]), \
             patch('sandbox.server.Sandbox.generate_post', return_value='Bài viết demo'), \
             patch('sandbox.server.call_luna', return_value=model_result(semantic)):
            state = self.conversation(post_id=None, persona_id='99', vehicle='Creta')
            _, result = self.turn(state, text)
            self.assertNotIn('Mình đang đi', result['turn']['response_text'] or '')
            self.assertNotIn('Creta', result['turn']['response_text'] or '')

    def test_generated_post_uses_persona_and_records_model_cost(self):
        repository = self.http.app.persona_repository()
        from app.persona_fixture import seed_nam_creta
        persona_id, release_id = seed_nam_creta(repository)
        snapshot = repository.get_release_snapshot(persona_id, release_id)
        vehicle = next(fact for fact in snapshot['facts'] if fact['key'] == 'vehicle.current.model')
        post_text = "Mình đang đi Hyundai Creta."
        candidate = {'post_text': post_text, 'fact_ids': [vehicle['fact_id']], 'claims': [
            {'text': post_text, 'subject': 'persona', 'predicate': vehicle['key'],
             'fact_ids': [vehicle['fact_id']], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': []}
        calls = []
        def call(_post, _text, **kwargs):
            calls.append(kwargs)
            return {'ok': True,
                'needs': {'topics': ['vehicle_current'], 'keys': ['vehicle.current.model']}
                    if kwargs.get('persona_phase') == 'analysis' else None,
                'post_candidate': candidate if kwargs.get('persona_phase') == 'post' else None,
                'attempts': 1, 'usage': {'prompt_tokens': 15, 'completion_tokens': 30, 'total_tokens': 45},
                'cost_usd_est': 0.01, 'cost_unknown': False, 'model_effective': 'provider/effective-model'}
        metrics = {}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch("sandbox.server.call_luna", side_effect=call):
            text = self.http.app.generate_post({"name": "Nam", "tone": "warm"}, snapshot, _metrics=metrics)
        self.assertEqual(text, post_text)
        self.assertEqual([call['persona_phase'] for call in calls], ['analysis', 'post'])
        self.assertIsNone(calls[0]['persona_context'])
        self.assertIn(vehicle['fact_id'], {fact['fact_id'] for fact in calls[1]['persona_context']['facts']})
        self.assertEqual(metrics["attempts"], 2)
        self.assertEqual(metrics["usage"], {"prompt_tokens": 30, "completion_tokens": 60, "total_tokens": 90})
        self.assertEqual(metrics["model"], "provider/effective-model")
        self.assertEqual(metrics["cost_usd_est"], 0.02)
        self.assertGreaterEqual(metrics["generation_latency_ms"], metrics["model_latency_ms"])
        records = [json.loads(line) for line in Path(self.ledger).read_text().splitlines()]
        self.assertEqual({row.get('kind') for row in records}, {'post_analysis', 'post'})
        self.assertTrue(all(row.get("cost_usd_est") == 0.01 and
                            row.get("model") == "provider/effective-model" for row in records))

    def test_generated_post_budget_reservations_are_unique_across_phases(self):
        from app.persona_fixture import seed_nam_creta
        repository = self.http.app.persona_repository()
        persona_id, release_id = seed_nam_creta(repository)
        snapshot = repository.get_release_snapshot(persona_id, release_id)
        vehicle = next(fact for fact in snapshot['facts'] if fact['key'] == 'vehicle.current.model')
        post_text = "Mình đang đi Hyundai Creta."
        candidate = {'post_text': post_text, 'fact_ids': [vehicle['fact_id']], 'claims': [
            {'text': post_text, 'subject': 'persona', 'predicate': vehicle['key'],
             'fact_ids': [vehicle['fact_id']], 'memory_ids': [], 'message_ids': []}],
            'fact_proposals': []}
        calls = []
        def call(_post, _text, **kwargs):
            phase = kwargs['persona_phase']
            allowed = kwargs['budget_guard']()
            calls.append((phase, allowed))
            if not allowed:
                return {'ok': False, 'error_code': 'BUDGET_EXHAUSTED',
                        'error': 'budget guard denied before HTTP attempt', 'attempts': 0,
                        'usage': {}, 'cost_usd_est': None, 'cost_unknown': True}
            return {'ok': True,
                'needs': {'topics': ['vehicle_current'], 'keys': ['vehicle.current.model']}
                    if phase == 'analysis' else None,
                'post_candidate': candidate if phase == 'post' else None,
                'attempts': 1, 'usage': {'prompt_tokens': 10, 'completion_tokens': 20},
                'cost_usd_est': 0.001, 'cost_unknown': False}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key',
                                     'INTENT_MODEL_MAX_CALLS': '900',
                                     'INTENT_MODEL_MAX_COST_USD': '5'}), \
             patch.object(self.http.app, 'enforce_budget', True), \
             patch('sandbox.server.call_luna', side_effect=call):
            result = self.http.app.generate_post({'name': 'Nam'}, snapshot)
        self.assertEqual(result, post_text)
        self.assertEqual(calls, [('analysis', True), ('post', True)])

    def test_generated_post_reports_budget_exhaustion_without_writing_phase(self):
        from app.persona_fixture import seed_nam_creta
        repository = self.http.app.persona_repository()
        persona_id, release_id = seed_nam_creta(repository)
        snapshot = repository.get_release_snapshot(persona_id, release_id)
        failure = {"ok": False, "error_code": "BUDGET_EXHAUSTED",
                   "error": "budget guard denied before HTTP attempt", "attempts": 0,
                   "usage": {}, "cost_usd_est": None, "cost_unknown": True}
        metrics = {}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(self.http.app, "enforce_budget", True), \
             patch("sandbox.server.call_luna", return_value=failure) as luna:
            with self.assertRaisesRegex(ValueError, "chưa gửi yêu cầu tới OpenRouter"):
                self.http.app.generate_post({"name": "Nam"}, snapshot, _metrics=metrics)
        self.assertEqual(luna.call_count, 1)
        self.assertEqual(luna.call_args.kwargs["persona_phase"], "analysis")
        self.assertEqual(metrics["generation_validation"],
                         {"status": "REVIEW", "reason": "BUDGET_EXHAUSTED"})

    def test_persona_turn_stops_on_budget_exhaustion_without_legacy_fallback(self):
        failure = {"ok": False, "error_code": "BUDGET_EXHAUSTED",
                   "error": "budget guard denied before HTTP attempt", "attempts": 0,
                   "usage": {}, "cost_usd_est": None, "cost_unknown": True}
        conversation = {"id": "budget", "counter": 1, "actor_id": "customer",
                        "post": {"text": "Bài thử"}, "history": [], "engine": SimpleNamespace()}
        event = {"text": "Chào bạn"}
        with patch.object(self.http.app, "model", return_value=failure) as model:
            result = self.http.app._persona_two_phase(conversation, event,
                {"recent": [], "parent_texts": []}, "", 1200)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "BUDGET_EXHAUSTED")

    def test_sandbox_budget_uses_shared_intent_limits(self):
        from sandbox.server import Sandbox
        with patch.dict(os.environ, {"SANDBOX_ENFORCE_BUDGET": "true",
                                     "INTENT_MODEL_MAX_CALLS": "900",
                                     "INTENT_MODEL_MAX_COST_USD": "5.00"}):
            app = Sandbox(ledger_path=self.ledger)
            with patch("sandbox.server.budget_ledger.guard", return_value=(False, "blocked")) as guard:
                app.budget()
        self.assertEqual(guard.call_args.kwargs["max_calls"], 900)
        self.assertEqual(guard.call_args.kwargs["max_cost"], 5.0)

    def test_post_generation_model_falls_back_to_requested_model(self):
        from sandbox.server import MODEL
        repository = self.http.app.persona_repository()
        from app.persona_fixture import seed_nam_creta
        persona_id, release_id = seed_nam_creta(repository)
        snapshot = repository.get_release_snapshot(persona_id, release_id)
        vehicle = next(fact for fact in snapshot['facts'] if fact['key'] == 'vehicle.current.model')
        text = 'Mình đang đi Hyundai Creta.'
        candidate = {'post_text': text, 'fact_ids': [vehicle['fact_id']], 'claims': [
            {'text': text, 'subject': 'persona', 'predicate': vehicle['key'],
             'fact_ids': [vehicle['fact_id']], 'memory_ids': [], 'message_ids': []}], 'fact_proposals': []}
        def call(_post, _text, **kwargs):
            return {'ok': True, 'needs': {'topics': [], 'keys': []} if kwargs.get('persona_phase') == 'analysis' else None,
                    'post_candidate': candidate if kwargs.get('persona_phase') == 'post' else None,
                    'attempts': 1, 'usage': {}, 'cost_usd_est': 0.0, 'cost_unknown': False}
        metrics = {}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
             patch("sandbox.server.call_luna", side_effect=call):
            self.http.app.generate_post({"name": "Nam"}, snapshot, _metrics=metrics)
        self.assertEqual(metrics["model"], MODEL)

    def test_sandbox_page_starts_with_persona_and_comment_inputs(self):
        page = (Path(__file__).resolve().parents[2] / "sandbox/static/index.html").read_text()
        self.assertIn('id="persona-select"', page)
        self.assertNotIn('id="persona-use-consent"', page)
        self.assertNotIn('id="review-drafts"', page)
        self.assertNotIn('id="vehicle-select"', page)
        self.assertIn('id="turn-text"', page)
        self.assertNotIn('id="post-text"', page)
        script = (Path(__file__).resolve().parents[2] / "sandbox/static/app.js").read_text()
        self.assertIn('Fact fixture không phải xác nhận của người thật.', script)
        self.assertNotIn('fact.source_ref', script)

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

    def test_truncated_luna_output_gets_one_bounded_retry_and_no_wrong_state(self):
        state = self.conversation()
        failure = {"ok": False, "error": "model output truncated", "error_code": "MODEL_OUTPUT_TRUNCATED",
                   "finish_reason": "length", "attempts": 1, "usage": {"prompt_tokens": 40,
                   "completion_tokens": 1200}, "cost_usd_est": .001, "cost_unknown": False}
        with patch("sandbox.server.call_luna", return_value=failure) as luna:
            _, result = self.turn(state, "Đại lý ở Sài Gòn?")
        turn = result["turn"]
        self.assertEqual(turn["routing"], ["REVIEW_MODEL_ERROR"])
        self.assertEqual(turn["model_calls"][0]["error_code"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(turn["model_calls"][0]["finish_reason"], "length")
        self.assertEqual(turn["cost_usd_est"], .002)
        self.assertEqual(result["active_requests"], [])
        self.assertEqual(result["fake_sends"], [])
        self.assertEqual(luna.call_count, 2)
        self.assertEqual(luna.call_args_list[0].kwargs["max_tokens"], 1200)
        self.assertEqual(luna.call_args_list[1].kwargs["max_tokens"], 1800)

    def test_truncated_luna_output_can_recover_with_one_valid_reply(self):
        state = self.conversation()
        failure = {"ok": False, "error": "model output truncated", "error_code": "MODEL_OUTPUT_TRUNCATED",
                   "finish_reason": "length", "attempts": 1, "usage": {"prompt_tokens": 40,
                   "completion_tokens": 1200}, "cost_usd_est": .001, "cost_unknown": False}
        with patch("sandbox.server.call_luna", side_effect=[failure, model_result(cost=.0001)]) as luna:
            _, result = self.turn(state)
        turn = result["turn"]
        self.assertTrue(turn["ok"], turn)
        self.assertEqual(turn["attempts"], 2)
        self.assertAlmostEqual(turn["cost_usd_est"], .0011)
        self.assertEqual(turn["model_calls"][0]["error_code"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(turn["model_calls"][1]["validation"], "passed")
        self.assertEqual(len(result["active_requests"]), 1)
        self.assertEqual(luna.call_count, 2)

    def test_missing_luna_content_is_not_assumed_to_be_truncation(self):
        state = self.conversation()
        failure = {"ok": False, "error": "missing content", "error_code": "MODEL_OUTPUT_MISSING",
                   "attempts": 1, "cost_usd_est": None, "cost_unknown": True}
        with patch("sandbox.server.call_luna", return_value=failure) as luna:
            _, result = self.turn(state)
        self.assertEqual(luna.call_count, 1)
        self.assertEqual(result["turn"]["routing"], ["REVIEW_MODEL_ERROR"])
        self.assertEqual(result["active_requests"], [])

    def test_provider_refusal_does_not_trigger_truncation_retry(self):
        state = self.conversation()
        failure = {"ok": False, "error": "model refused", "error_code": "MODEL_REFUSAL",
                   "finish_reason": "length", "attempts": 1, "cost_usd_est": .001, "cost_unknown": False}
        with patch("sandbox.server.call_luna", return_value=failure) as luna:
            _, result = self.turn(state)
        self.assertEqual(luna.call_count, 1)
        self.assertEqual(result["turn"]["model_error_code"], "MODEL_REFUSAL")
        self.assertEqual(result["turn"]["routing"], ["REVIEW_MODEL_ERROR"])
        self.assertEqual(result["active_requests"], [])

    def test_truncation_retry_stops_when_budget_denied(self):
        state = self.conversation()
        failure = {"ok": False, "error": "truncated", "error_code": "MODEL_OUTPUT_TRUNCATED",
                   "finish_reason": "length", "attempts": 1, "cost_usd_est": .001, "cost_unknown": False}
        with patch.object(self.http.app, "budget", side_effect=[(True, ""), (False, "budget denied")]), \
             patch("sandbox.server.call_luna", return_value=failure) as luna:
            _, result = self.turn(state)
        self.assertEqual(luna.call_count, 1)
        self.assertEqual(result["turn"]["routing"], ["BLOCKED_BUDGET"])
        self.assertEqual(result["active_requests"], [])

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
        calls = result["turn"]["model_calls"]
        self.assertEqual([call["phase"] for call in calls], ["initial", "repair"])
        self.assertEqual([call["validation"] for call in calls], ["failed", "passed"])
        self.assertTrue(calls[0]["validation_errors"])
        self.assertEqual(calls[1]["validation_errors"], [])
        self.assertEqual(calls[0]["usage"]["prompt_tokens"], 500)
        self.assertGreaterEqual(calls[0]["latency_ms"], 0)

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
        with patch("sandbox.server.call_luna", return_value=model_result(meaning(evidence=comment))), patch.object(
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

    def test_sandbox_default_allows_retries_after_unknown_cost(self):
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="prior", kind="call", attempts=22, cost_usd_est=.002)
        state = self.conversation()
        calls = iter([dict(ok=False, error="retry failed", attempts=2, cost_usd_est=None,
                           cost_unknown=True), model_result()])
        first = True
        def retries(*args, **kwargs):
            nonlocal first
            guard = kwargs["budget_guard"]
            if first:
                self.assertTrue(guard())
                self.assertTrue(guard())
                self.assertTrue(guard())
                first = False
            return next(calls)
        with patch("sandbox.server.call_luna", side_effect=retries) as luna:
            _, result = self.turn(state, "Giá Creta?")
            _, next_result = self.turn(state, "giá bao nhiêu")
        self.assertFalse(result["turn"]["ok"])
        self.assertTrue(next_result["turn"]["ok"], next_result)
        self.assertEqual(luna.call_count, 2)
        self.assertEqual(self.rows()[-2]["attempts"], 2)
        self.assertTrue(self.rows()[-2]["cost_unknown"])

    def test_unknown_cost_does_not_block_sandbox_by_default_and_stays_logged(self):
        state = self.conversation()
        with patch("sandbox.server.call_luna", side_effect=[model_result(cost=None), model_result()]) as luna:
            self.turn(state, "giá bao nhiêu")
            _, result = self.turn(state, "giá bao nhiêu nữa")
        self.assertTrue(result["turn"]["ok"], result)
        self.assertEqual(luna.call_count, 2)
        self.assertEqual(self.rows()[0]["status"], "unknown_cost")
        self.assertEqual(len(self.rows()), 2)

    def test_model_inferred_vehicle_uses_verified_single_vehicle_post_provenance(self):
        state = self.conversation()
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope",
            "requests": [{"topic": "spec", "desired_outcome": "general_vehicle_information",
                          "polarity": "positive"}],
            "operations": [{"op": "START_REQUEST", "request": {
                "topic": "spec", "desired_outcome": "general_vehicle_information"},
                "request_ref": "topic:spec", "topic": "spec", "evidence_excerpt": "Xe như nào",
                # Luna returned the conversation ID, not the event/comment ID.
                "source_message_id": state["id"],
                "slot": {"kind": "vehicle", "value": "Creta", "explicit": False}}],
            "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(semantic)) as luna:
            status, result = self.turn(state, "Xe như nào")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["turn"]["ok"], result["turn"])
        self.assertEqual(result["turn"]["attempts"], 1)
        luna.assert_called_once()
        self.assertEqual(result["turn"]["semantic"]["operations"][0]["source_message_id"],
                         state["id"] + "-1")
        created = next(change for change in result["turn"]["state_changes"]
                       if change["kind"] == "request_created")
        slot = created["slots"]["vehicle"]
        self.assertEqual((slot["value"], slot["source_message_id"], slot["source_actor_id"],
                          slot["explicit"]), ("Creta", "creta", "sample-hyundai", False))

    def test_color_recommendation_turn_reaches_chat_policy_with_server_resolved_source(self):
        state = self.conversation()
        text = "Thế theo bạn nên mua màu nào là đẹp"
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope",
            "requests": [{"topic": "buy", "desired_outcome": "color_recommendation",
                          "polarity": "positive"}],
            "operations": [{"op": "START_REQUEST", "request": {
                "topic": "buy", "desired_outcome": "color_recommendation"},
                "request_ref": "topic:buy", "topic": "buy", "evidence_excerpt": text,
                "source_message_id": state["id"],
                "slot": {"kind": "vehicle", "value": "Creta", "explicit": False}}],
            "uncertainties": []}
        with patch("sandbox.server.call_luna", return_value=model_result(semantic)) as luna:
            status, result = self.turn(state, text)
        self.assertEqual(status, 200, result)
        self.assertNotIn("REVIEW_PROVENANCE", result["turn"]["routing"])
        self.assertTrue(result["turn"]["response_text"], result["turn"])
        self.assertEqual(result["turn"]["semantic"]["operations"][0]["source_message_id"],
                         state["id"] + "-1")
        luna.assert_called_once()

    def test_budget_can_be_explicitly_enforced(self):
        from sandbox.server import Sandbox
        from scripts import budget_ledger
        budget_ledger.record(self.ledger, run_id="unknown-test", kind="call", attempts=1,
                             cost_usd_est=None)
        with patch.dict(os.environ, {"SANDBOX_ENFORCE_BUDGET": "true"}):
            app = Sandbox(ledger_path=self.ledger)
        self.assertTrue(app.enforce_budget)
        allowed, reason = app.budget()
        self.assertFalse(allowed)
        self.assertIn("unknown", reason.lower())

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
        self.assertEqual(context["parent_texts"], [])
        self.assertEqual([message["role"] for message in context["messages"]], ["post", "current"])

    def test_post_is_not_inserted_as_a_fake_root_comment(self):
        state = self.conversation()
        conversation = self.http.app.conversations[state["id"]]
        rows = conversation["engine"].store.conn.execute(
            "SELECT actor_id, comment_id, payload FROM events ORDER BY rowid").fetchall()
        self.assertEqual(rows, [])

    def test_concurrent_sandbox_turns_are_not_blocked_by_spend_ledger(self):
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
        self.assertEqual(sum(result[1]["turn"]["ok"] for result in results), 2)
        self.assertEqual(budget_ledger.totals(self.ledger)[0], 25)

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

    def test_sandbox_default_opens_even_when_spend_ledger_is_at_cap(self):
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
        self.assertTrue(result["turn"]["ok"], result)
        luna.assert_called_once()
        self.assertEqual(budget_ledger.totals(self.ledger)[0], 2)

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
