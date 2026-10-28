import json
import unittest

from app.context import fmt_context


class ContextFormattingTests(unittest.TestCase):
    def test_drops_prior_copy_of_recent_actor_message_but_keeps_provenance(self):
        serialized = fmt_context({
            "postcard": {"id": "post-1", "text": "Hyundai Creta", "vehicles": ["Creta"]},
            "messages": [
                {"message_id": "root-1", "actor_id": "u2", "role": "root", "text": "Giá xe?"},
                {"message_id": "current-1", "actor_id": "u1", "role": "current", "text": "Bản cao"},
            ],
            "recent": [{"message_id": "old-1", "actor_id": "u1", "role": "recent", "text": "Giá Creta"}],
            "active_requests": [{"id": "request-1", "actor_id": "u1", "slots": {"vehicle": "Creta"}}],
        }, prior_customer_turns=["Giá Creta", "Tôi ở Hà Nội"])
        context = json.loads(serialized)

        self.assertEqual(context["postcard"]["vehicles"], ["Creta"])
        self.assertEqual([m["actor_id"] for m in context["messages"]], ["u2", "u1"])
        self.assertEqual(context["prior_customer_turns"], ["Tôi ở Hà Nội"])
        self.assertEqual(context["recent"][0]["message_id"], "old-1")
        self.assertEqual(context["active_requests"][0]["id"], "request-1")

    def test_does_not_drop_same_text_from_another_actors_root(self):
        context = json.loads(fmt_context({
            "scope": ["page", "post", "thread", "u1"],
            "messages": [{"message_id": "root-1", "actor_id": "u2", "role": "root", "text": "Giá Creta"}],
            "recent": [{"message_id": "other-1", "actor_id": "u2", "role": "recent", "text": "Giá Creta"}],
        }, prior_customer_turns=["Giá Creta"]))

        self.assertEqual(context["prior_customer_turns"], ["Giá Creta"])

    def test_keeps_prior_when_current_actor_cannot_be_determined(self):
        context = json.loads(fmt_context({
            "recent": [{"message_id": "recent-1", "actor_id": "u2", "role": "recent", "text": "Giá Creta"}],
        }, prior_customer_turns=["Giá Creta"]))

        self.assertEqual(context["prior_customer_turns"], ["Giá Creta"])


if __name__ == "__main__":
    unittest.main()
