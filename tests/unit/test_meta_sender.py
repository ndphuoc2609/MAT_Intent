import unittest
from datetime import datetime, timezone

from app.engine import Clock, Engine
from app.facts import Facts
from app.state import Store
from meta_services.graph import MetaGraphError
from meta_services.sender import MetaSender


def event(comment_id="789"):
    return {
        "page_id": "123", "actor_id": "999", "post_id": "123_456",
        "thread_root_id": "123_456", "parent_id": "123_456",
        "comment_id": comment_id, "actor_role": "user", "event_type": "create",
        "content_version": 1, "event_dedupe_key": f"{comment_id}:1", "text": "Cảm ơn",
    }


class FakeGraph:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def reply_to_comment(self, comment_id, message):
        self.calls.append((comment_id, message))
        if self.error:
            raise self.error
        return {"id": "123_456_999"}


class MetaSenderTests(unittest.TestCase):
    def test_engine_records_real_sender_success_and_dedupes_redelivery(self):
        store = Store()
        graph = FakeGraph()
        sender = MetaSender(graph, store, outbound_enabled=lambda: True)
        engine = Engine(store, Facts([]), sender, Clock(datetime.now(timezone.utc)))
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
            "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
            "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": "Cảm ơn"}], "uncertainties": [],
        }
        result = engine.process(event(), {"topic": "Creta", "vehicles": []}, semantic)
        self.assertTrue(result["outbound_permission"])
        self.assertEqual(result["sends"][0]["status"], "sent")
        self.assertEqual(store.get_outbox("789:reply")["status"], "SENT")
        duplicate = engine.process(event(), {"topic": "Creta", "vehicles": []}, semantic)
        self.assertIn("DUPLICATE_IGNORED", duplicate["routing"])
        self.assertEqual(len(graph.calls), 1)

    def test_success_is_durable_and_duplicate_send_is_not_repeated(self):
        store = Store()
        graph = FakeGraph()
        sender = MetaSender(graph, store, outbound_enabled=lambda: True)
        payload = {"dedupe_key": "789:reply", "comment_id": "789", "text": "Cảm ơn bạn"}
        first = sender.send(payload)
        second = sender.send(payload)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "sent")
        self.assertEqual(len(graph.calls), 1)
        self.assertTrue(sender.receiver.has_effect("789:reply"))

    def test_uncertain_write_is_never_retried(self):
        store = Store()
        graph = FakeGraph(MetaGraphError("transport unknown", outcome_unknown=True))
        sender = MetaSender(graph, store, outbound_enabled=lambda: True)
        payload = {"dedupe_key": "789:reply", "comment_id": "789", "text": "Cảm ơn bạn"}
        self.assertEqual(sender.send(payload)["status"], "UNKNOWN")
        self.assertEqual(sender.send(payload)["status"], "UNKNOWN")
        self.assertEqual(len(graph.calls), 1)
        self.assertFalse(sender.receiver.has_effect("789:reply"))

    def test_definite_rejection_is_failed_without_retry(self):
        store = Store()
        graph = FakeGraph(MetaGraphError("rejected", status=400, code=100))
        sender = MetaSender(graph, store, outbound_enabled=lambda: True)
        payload = {"dedupe_key": "789:reply", "comment_id": "789", "text": "Cảm ơn bạn"}
        self.assertEqual(sender.send(payload)["status"], "failed")
        self.assertEqual(sender.send(payload)["status"], "failed")
        self.assertEqual(len(graph.calls), 1)

    def test_meta_sender_disables_engine_replay_of_intents(self):
        store = Store()
        sender = MetaSender(FakeGraph(), store, outbound_enabled=lambda: True)
        engine = Engine(store, Facts([]), sender, Clock(datetime.now(timezone.utc)))
        self.assertFalse(sender.retry_intended)
        self.assertEqual(engine.drain_intended(), [])


if __name__ == "__main__":
    unittest.main()
