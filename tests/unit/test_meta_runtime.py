import json
import os
import tempfile
import unittest
from pathlib import Path

from meta_services.graph import MetaGraphError
from meta_services.runtime import MetaRuntime


def social_semantic(text):
    return {
        "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
        "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
        "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": [],
    }


class FakeGraph:
    page_id = "123"

    def __init__(self):
        self.replies = []
        self.comment_errors = 0

    def get_comment(self, comment_id):
        if self.comment_errors:
            self.comment_errors -= 1
            raise MetaGraphError("temporary", status=503)
        return {"id": comment_id, "message": "Cảm ơn", "from": {"id": "999"},
                "object": {"id": "123_456", "from": {"id": "123"}}}

    def get_post(self, post_id):
        return {"id": post_id, "message": "Creta bản mới", "from": {"id": "123"}}

    def reply_to_comment(self, comment_id, message):
        self.replies.append((comment_id, message))
        return {"id": f"reply_{len(self.replies)}"}


class MetaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.graph = FakeGraph()
        self.model_contexts = []

        def model_call(post, comment, parent="", context="", budget_guard=None):
            self.model_contexts.append(context)
            return {"ok": True, "semantic": social_semantic(comment), "attempts": 0}

        self.runtime = MetaRuntime(
            self.graph, app_secret="secret", verify_token="verify", service_api_key="api-key",
            outbound_enabled=True, db_path=Path(self.temp.name) / "meta.db",
            budget_path=Path(self.temp.name) / "budget.jsonl", model_call=model_call,
        )
        self.addCleanup(self.runtime.close)

    def payload(self, cid):
        return {"object": "page", "entry": [{"id": "123", "changes": [{
            "field": "feed", "value": {"item": "comment", "verb": "add",
                "comment_id": cid, "post_id": "123_456", "parent_id": "123_456",
                "from": {"id": "999"}, "message": "Cảm ơn"}
        }]}]}

    def test_webhook_comments_are_deduped_and_bot_reply_enters_followup_context(self):
        self.assertEqual(self.runtime.enqueue_webhook(self.payload("456_1")), 1)
        self.assertEqual(self.runtime.enqueue_webhook(self.payload("456_1")), 0)
        self.assertTrue(self.runtime.process_next())
        self.assertTrue(self.runtime.enqueue_webhook(self.payload("456_2")))
        self.assertTrue(self.runtime.process_next())
        self.assertEqual(len(self.graph.replies), 2)
        self.assertIn("Cảm ơn bạn đã tương tác!", self.model_contexts[1])

    def test_engine_accepts_exact_evidence_from_canonical_post_context(self):
        self.runtime.model_call = lambda *args, **kwargs: {
            "ok": True, "semantic": social_semantic("Creta bản mới"), "attempts": 0,
        }
        self.runtime.enqueue_webhook(self.payload("456_6"))
        self.assertTrue(self.runtime.process_next())
        result = self.runtime.list_inbox()[0]["result"]
        self.assertNotIn("REVIEW_SCHEMA", result["routing"])
        self.assertIn("social_ack", result["routing"])

    def test_engine_accepts_exact_evidence_from_scoped_bot_reply_context(self):
        self.runtime.enqueue_webhook(self.payload("456_7"))
        self.runtime.process_next()
        self.runtime.model_call = lambda *args, **kwargs: {
            "ok": True, "semantic": social_semantic("Cảm ơn bạn đã tương tác!"), "attempts": 0,
        }
        self.runtime.enqueue_webhook(self.payload("456_8"))
        self.runtime.process_next()
        result = self.runtime.list_inbox()[0]["result"]
        self.assertNotIn("REVIEW_SCHEMA", result["routing"])

    def test_nested_comment_can_bind_vehicle_to_canonical_post_source(self):
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
            "requests": [{"topic": "stock", "desired_outcome": "stock_availability", "polarity": "positive"}],
            "operations": [{"op": "START_REQUEST", "request_ref": "topic:stock",
                "source_message_id": "123_456", "evidence_excerpt": "Creta",
                "slot": {"kind": "vehicle", "value": "Creta", "explicit": False}}],
            "uncertainties": [],
        }
        self.runtime.model_call = lambda *args, **kwargs: {
            "ok": True, "semantic": semantic, "attempts": 0,
        }
        payload = self.payload("456_9")
        payload["entry"][0]["changes"][0]["value"]["parent_id"] = "456_8"
        self.runtime.enqueue_webhook(payload)
        self.runtime.process_next()
        result = self.runtime.list_inbox()[0]["result"]
        self.assertNotIn("REVIEW_PROVENANCE", result["routing"])
        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])

    def test_no_sample_facts_are_loaded(self):
        self.assertEqual(self.runtime.engine.facts.facts, [])

    def test_temporary_graph_read_failure_retries_without_losing_the_comment(self):
        self.graph.comment_errors = 1
        self.runtime.enqueue_webhook(self.payload("456_4"))
        self.assertTrue(self.runtime.process_next())
        job = self.runtime.list_inbox()[0]
        self.assertEqual((job["status"], job["attempts"]), ("PENDING", 1))
        with self.runtime.store.lock:
            self.runtime.store.conn.execute(
                "UPDATE meta_inbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE comment_id='456_4'")
            self.runtime.store.conn.commit()
        self.assertTrue(self.runtime.process_next())
        job = self.runtime.list_inbox()[0]
        self.assertEqual((job["status"], job["attempts"]), ("DONE", 2))

    def test_disabled_outbound_still_processes_webhooks_without_sending(self):
        self.runtime.outbound_enabled = False
        self.assertEqual(self.runtime.enqueue_webhook(self.payload("456_3")), 1)
        self.assertTrue(self.runtime.process_next())
        self.assertTrue(self.model_contexts)
        self.assertEqual(self.graph.replies, [])
        self.assertEqual(self.runtime.list_inbox()[0]["status"], "DONE")

    def test_duplicate_manual_idempotency_key_does_not_leave_sqlite_transaction_open(self):
        action = {"message": "same action"}
        self.assertTrue(self.runtime.claim_manual_write("manual-123", "publish", "123", action)["claimed"])
        self.assertFalse(self.runtime.claim_manual_write("manual-123", "publish", "123", action)["claimed"])
        self.assertFalse(self.runtime.store.conn.in_transaction)
        self.runtime.enqueue_webhook(self.payload("456_5"))
        self.assertIsNotNone(self.runtime.inbox.claim())


if __name__ == "__main__":
    unittest.main()
