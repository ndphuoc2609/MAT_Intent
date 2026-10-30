"""Regression tests for malformed or truncated OpenRouter model responses.

All provider responses are local fixtures; this module never makes a live call.
"""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.adapters import luna
from app.semantics import normalize_model_semantic, validate_semantic
from app.harness import make_engine


def _semantic(text="đại lý ở đâu"):
    return {
        "schema_version": "1",
        "sentiment": "NEU",
        "sarcasm": "absent",
        "scope": "in_scope",
        "requests": [{"topic": "dealer", "desired_outcome": "dealer_location",
                      "polarity": "positive"}],
        "operations": [{"op": "START_REQUEST", "request_ref": "topic:dealer",
                         "evidence_excerpt": text}],
        "uncertainties": [],
    }


class LunaOutputShapeTests(unittest.TestCase):
    def _call_with_response(self, response):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(luna.urllib.request, "build_opener") as build_opener, \
             patch.object(luna.time, "sleep"):
            build_opener.return_value.open.return_value = io.BytesIO(
                json.dumps(response).encode("utf-8"))
            return luna.call_luna("post fixture", "comment fixture")

    def test_token_limit_with_null_content_is_reported_as_truncation(self):
        result = self._call_with_response({
            "choices": [{"finish_reason": "length", "message": {"content": None}}],
            "usage": {"prompt_tokens": 450, "completion_tokens": 1200, "cost": 0.00123},
            "model": "fixture/model",
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(result["error"], "model output truncated at max_tokens")
        self.assertEqual(result["usage"]["completion_tokens"], 1200)
        self.assertEqual(result["cost_usd_est"], 0.00123)
        self.assertFalse(result["cost_unknown"])
        self.assertNotIn("raw_text", result)

    def test_null_content_without_length_reason_is_a_missing_output(self):
        result = self._call_with_response({
            "choices": [{"finish_reason": "stop", "message": {"content": None}}],
            "usage": {"cost": 0.0005},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "MODEL_OUTPUT_MISSING")
        self.assertEqual(result["cost_usd_est"], 0.0005)

    def test_non_string_structured_content_is_rejected_without_stringifying(self):
        semantic = _semantic()
        result = self._call_with_response({
            "choices": [{"finish_reason": "stop", "message": {"content": [
                {"type": "text", "text": json.dumps(semantic)}
            ]}}],
            "usage": {"cost": 0.0005},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "MODEL_OUTPUT_SHAPE")
        self.assertEqual(result["cost_usd_est"], 0.0005)
        self.assertNotIn("raw_text", result)

    def test_invalid_json_has_a_distinct_safe_error_code(self):
        result = self._call_with_response({
            "choices": [{"finish_reason": "stop", "message": {"content": "{"}}],
            "usage": {"cost": 0.0005},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "MODEL_OUTPUT_INVALID_JSON")

    def test_provider_response_shape_refusal_and_semantic_shape_have_safe_codes(self):
        cases = (
            ({"usage": {"cost": 0.0005}}, "MODEL_RESPONSE_SHAPE"),
            ({"choices": [{"finish_reason": "stop", "message": {
                "content": None, "refusal": "fixture refusal"}}], "usage": {"cost": 0.0005}},
             "MODEL_REFUSAL"),
            ({"choices": [{"finish_reason": "stop", "message": {"content": "[]"}}],
              "usage": {"cost": 0.0005}}, "MODEL_SEMANTIC_SHAPE"),
        )
        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                result = self._call_with_response(response)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], expected_code)
                self.assertEqual(result["cost_usd_est"], 0.0005)

    def test_refusal_and_content_filter_take_precedence_over_length_or_json(self):
        refusal_detail = "fixture-only refusal detail"
        cases = (
            ({"choices": [{"finish_reason": "length", "message": {
                "content": None, "refusal": refusal_detail}}],
              "usage": {"completion_tokens": 1200, "cost": 0.001}}, "length"),
            ({"choices": [{"finish_reason": "content_filter", "message": {
                "content": json.dumps(_semantic())}}], "usage": {"cost": 0.002}}, "content_filter"),
        )
        for response, finish_reason in cases:
            with self.subTest(finish_reason=finish_reason), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                 patch.object(luna.urllib.request, "build_opener") as build_opener, \
                 patch.object(luna.time, "sleep"):
                build_opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
                result = luna.call_luna("post fixture", "comment fixture")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "MODEL_REFUSAL")
                self.assertEqual(result["finish_reason"], finish_reason)
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(build_opener.return_value.open.call_count, 1)
                self.assertNotIn(refusal_detail, json.dumps(result))
                self.assertNotIn("semantic", result)
                self.assertIsNotNone(result["cost_usd_est"])

    def test_engine_routes_malformed_operation_container_to_review_without_mutation(self):
        def event(comment_id):
            return {"page_id": "page", "post_id": "post", "thread_root_id": "root",
                    "comment_id": comment_id, "parent_id": "root", "actor_id": "actor",
                    "actor_role": "user", "event_type": "create", "content_version": 1,
                    "text": "đại lý ở đâu", "event_dedupe_key": f"{comment_id}:1"}

        def business_rows(engine):
            return {table: engine.store.conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("requests", "pending", "handoffs", "contact_prefs",
                                  "contact_blocks", "outbox")}

        for index, operations in enumerate((42, {"op": "START_REQUEST"}, None)):
            with self.subTest(operations_type=type(operations).__name__):
                engine = make_engine()
                original = _semantic()
                original["operations"] = operations
                before_semantic = json.loads(json.dumps(original))
                before_rows = business_rows(engine)
                result = engine.process(event(f"bad-shape-{index}"),
                    {"topic": "Creta", "vehicles": ["Creta"]}, original)
                self.assertIn("REVIEW_SCHEMA", result["routing"], result)
                self.assertTrue(result["errors"], result)
                self.assertEqual(business_rows(engine), before_rows)
                self.assertEqual(original, before_semantic)
                engine.store.conn.close()

    def test_sandbox_turn_routes_malformed_operations_to_review_without_state_mutation(self):
        from sandbox.server import Sandbox

        with tempfile.TemporaryDirectory() as temp_dir:
            app = Sandbox(ledger_path=os.path.join(temp_dir, "budget.jsonl"),
                          dealer_db_path=os.path.join(temp_dir, "dealers.sqlite3"),
                          persona_db_path=os.path.join(temp_dir, "personas.sqlite3"))
            state = app.create({"post_id": "creta", "actor_id": "actor-1"})
            conversation = app.conversations[state["id"]]
            engine = conversation["engine"]
            def business_rows():
                return {table: engine.store.conn.execute(f"SELECT * FROM {table}").fetchall()
                        for table in ("requests", "pending", "handoffs", "contact_prefs",
                                      "contact_blocks", "outbox")}

            try:
                for index, operations in enumerate((42, {"op": "START_REQUEST"}, None)):
                    with self.subTest(operations_type=type(operations).__name__):
                        before_rows = business_rows()
                        sem = _semantic(f"comment {index}")
                        sem["operations"] = operations
                        before_semantic = json.loads(json.dumps(sem))
                        model_result = {"ok": True, "semantic": sem, "attempts": 1,
                                        "cost_usd_est": 0.0, "cost_unknown": False,
                                        "model_effective": "fixture/model"}
                        with patch("sandbox.server.call_luna", return_value=model_result):
                            result = app.turn(conversation, {"text": f"comment {index}"})
                        self.assertIn("REVIEW_SCHEMA", result["turn"]["routing"], result["turn"])
                        self.assertFalse(result["turn"]["ok"])
                        self.assertTrue(result["turn"]["errors"])
                        self.assertEqual(business_rows(), before_rows)
                        self.assertEqual(sem, before_semantic)
            finally:
                engine.store.conn.close()
                engine.sender.receiver.conn.close()

    def test_normalizer_does_not_typeerror_on_malformed_nested_shapes(self):
        malformed = {
            "requests": None,
            "operations": [{"op": ["START_REQUEST"], "slot": {"kind": [], "value": {}}}],
        }
        normalized = normalize_model_semantic(malformed)
        self.assertEqual(normalized, malformed)
        errors = validate_semantic(normalized, "dealer ở đâu", [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
