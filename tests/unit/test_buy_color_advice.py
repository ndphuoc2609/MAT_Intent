import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.engine import Clock
from app.harness import make_engine
from datetime import datetime, timezone


def event(comment_id, text):
    return dict(page_id="p", post_id="post", thread_root_id="root", comment_id=comment_id,
                parent_id="root", actor_id="u1", actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"{comment_id}:1")


def semantic(text, outcome, operations=None):
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                requests=[dict(topic="buy", desired_outcome=outcome, polarity="positive")],
                operations=operations or [dict(op="START_REQUEST", topic="buy",
                    request_ref="topic:buy", evidence_excerpt=text,
                    request=dict(topic="buy", desired_outcome=outcome))], uncertainties=[])


POSTCARD = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


class TestBuyColorAdvice(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(clock=Clock(NOW))

    def requests(self):
        return [self.engine.store.get_request(row[0]) for row in
                self.engine.store.conn.execute("SELECT id FROM requests ORDER BY rowid").fetchall()]

    def test_color_recommendation_gives_neutral_advice_and_closes_advice_request(self):
        text = "Thế theo bạn nên mua màu nào là đẹp"
        result = self.engine.process(event("color-1", text), POSTCARD,
                                    semantic(text, "color_recommendation"))

        self.assertIn("tông trung tính", result["response_text"].lower())
        self.assertIn("chưa được xác minh", result["response_text"].lower())
        self.assertNotIn("HANDOFF", result["routing"])
        self.assertNotIn("liên hệ", result["response_text"].lower())
        self.assertNotIn("tồn kho", result["response_text"].lower())
        self.assertIn("answer_general_advice", result["routing"])
        self.assertEqual(result["sends"][0]["status"], "sent_to_fake_ledger")
        request = self.requests()[0]
        self.assertEqual(request["desired_outcomes"], ["color_recommendation"])
        self.assertEqual(request["status"], "COMPLETED")
        self.assertEqual(self.engine.store.open_pending_for("u1", request["id"]), [])

    def test_purchase_request_still_hands_off_for_human_confirmation(self):
        text = "Tôi muốn mua xe này"
        result = self.engine.process(event("buy-1", text), POSTCARD, semantic(text, "purchase"))
        self.assertIn("HANDOFF", result["routing"])

    def test_deposit_request_still_hands_off(self):
        text = "Tôi muốn đặt cọc xe này"
        result = self.engine.process(event("deposit-1", text), POSTCARD, semantic(text, "deposit"))
        self.assertIn("HANDOFF", result["routing"])

    def test_explicit_human_request_still_hands_off(self):
        initial = "Bạn tư vấn màu xe giúp mình"
        self.engine.process(event("human-0", initial), POSTCARD,
                            semantic(initial, "color_recommendation"))
        request = self.requests()[0]
        text = "Cho gặp nhân viên tư vấn"
        sem = semantic(text, "color_recommendation", [dict(op="REQUEST_HUMAN",
            request_ref=request["id"], evidence_excerpt=text, reason="staff")])
        result = self.engine.process(event("human-1", text), POSTCARD, sem)
        self.assertTrue(any(route.startswith("HANDOFF") for route in result["routing"]))


if __name__ == "__main__":
    unittest.main()
