import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.engine import Clock
from app.facts import Facts
from app.harness import make_engine


NOW = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
POSTCARD = {"page_id": "brand", "topic": "Hyundai Creta", "text": "Hyundai Creta",
            "vehicles": ["Creta"], "persona_default": "tư vấn viên"}


def event(comment_id, text, *, root="thread-1", parent=None):
    return {"page_id": "brand", "post_id": "post-1", "thread_root_id": root,
            "comment_id": comment_id, "parent_id": parent or root,
            "actor_id": "customer-1", "actor_role": "user", "event_type": "create",
            "content_version": 1, "text": text, "event_dedupe_key": f"{comment_id}:1"}


def semantic(text, topic, outcome, operations, *, requests=None, sarcasm="absent"):
    return {"schema_version": "1", "sentiment": "NEU", "sarcasm": sarcasm,
            "scope": "in_scope",
            "requests": requests or [{"topic": topic, "desired_outcome": outcome,
                                       "polarity": "positive"}],
            "operations": operations, "uncertainties": []}


def start(topic, outcome, excerpt, slot=None):
    operation = {"op": "START_REQUEST", "topic": topic, "request_ref": f"topic:{topic}",
                 "request": {"topic": topic, "desired_outcome": outcome},
                 "evidence_excerpt": excerpt}
    if slot is not None:
        operation["slot"] = slot
    return operation


class TestConversationCopy(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(clock=Clock(NOW), facts=Facts([]))

    def test_city_suitability_without_specs_is_honest_and_relevant(self):
        text = "Creta đi phố có hợp không?"
        result = self.engine.process(event("city-1", text, root="city-1"), POSTCARD,
            semantic(text, "spec", "city_driving_suitability", [start(
                "spec", "city_driving_suitability", "Creta",
                {"kind": "vehicle", "value": "Creta", "explicit": True,
                 "evidence_excerpt": "Creta"})]))

        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertIn("nhu cầu đi phố", result["response_text"])
        self.assertIn("kích thước xe, tầm nhìn và mức tiêu hao", result["response_text"])
        self.assertNotIn("?", result["response_text"])
        self.assertNotIn("nhân viên", result["response_text"].lower())
        request_id, request = next(iter(result["state_after"]["requests"].items()))
        self.assertEqual(request["status"], "OPEN")
        self.assertEqual(self.engine.store.open_pending_for("customer-1", request_id), [])

    def test_color_followup_after_city_question_uses_validated_context(self):
        first_text = "Creta đi phố có hợp không?"
        first = event("city-1", first_text, root="city-1")
        self.engine.process(first, POSTCARD, semantic(first_text, "spec", "city_driving_suitability", [start(
            "spec", "city_driving_suitability", "Creta",
            {"kind": "vehicle", "value": "Creta", "explicit": True,
             "evidence_excerpt": "Creta"})]))

        text = "Thế màu nào đẹp bạn?"
        inherited_vehicle = {"kind": "vehicle", "value": "Creta", "explicit": False,
                             "source_message_id": "city-1", "source_actor_id": "customer-1",
                             "source_state_token": "model-supplied-token", "evidence_excerpt": "Creta"}
        result = self.engine.process(event("color-2", text, root="city-1", parent="city-1"), POSTCARD,
            semantic(text, "spec", "color_recommendation", [start(
                "spec", "color_recommendation", text, inherited_vehicle)]))

        self.assertNotIn("REVIEW_PROVENANCE", result["routing"], result)
        self.assertIn("answer_general_advice", result["routing"])
        self.assertIn("tông trung tính", result["response_text"].lower())
        self.assertIn("Creta", result["response_text"])
        self.assertIn("chưa được xác minh", result["response_text"].lower())
        self.assertNotIn("trắng", result["response_text"].lower())
        self.assertEqual(result["sends"][0]["status"], "sent_to_fake_ledger")
        color_request = next((req for req in result["state_after"]["requests"].values()
                              if req["desired_outcomes"] == ["color_recommendation"]), None)
        self.assertIsNotNone(color_request)
        slot = color_request["slots"]["vehicle"]
        self.assertEqual((slot["source_message_id"], slot["source_actor_id"], slot["explicit"]),
                         ("city-1", "customer-1", False))
        self.assertNotEqual(slot["source_state_token"], "model-supplied-token")

    def test_missing_price_uses_plain_language_without_claiming_staff_followup(self):
        text = "Giá niêm yết Creta bao nhiêu?"
        operations = [start("price", "price_list", "Creta", {
            "kind": "vehicle", "value": "Creta", "explicit": True, "evidence_excerpt": "Creta"}),
            {"op": "UPDATE_SLOT", "request_ref": "topic:price", "evidence_excerpt": "niêm yết",
             "slot": {"kind": "price_kind", "value": "list", "explicit": True,
                      "evidence_excerpt": "niêm yết"}}]
        result = self.engine.process(event("price-1", text), POSTCARD,
                                     semantic(text, "price", "price_list", operations))

        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertIn("chưa có dữ liệu xác nhận", result["response_text"].lower())
        self.assertNotIn("nhân viên", result["response_text"].lower())

    def test_social_greeting_and_thanks_get_contextual_short_replies(self):
        def social(cid, text):
            return self.engine.process(event(cid, text), POSTCARD, semantic(text, "social", "ack", [
                {"op": "SOCIAL_ACK", "evidence_excerpt": text}]))

        greeting = social("hello-1", "Chào bạn")
        thanks = social("thanks-1", "Cảm ơn nhé")

        self.assertEqual(greeting["response_text"], "Chào bạn! Mình có thể giúp gì?")
        self.assertEqual(thanks["response_text"], "Không có gì, bạn nhé!")

    def test_social_negation_does_not_turn_into_a_thank_you_reply(self):
        text = "Không cảm ơn nhé"
        result = self.engine.process(event("no-thanks", text), POSTCARD,
            semantic(text, "social", "ack", [
                {"op": "SOCIAL_ACK", "evidence_excerpt": text}]))

        self.assertEqual(result["response_text"], "Mình hiểu rồi.")

    def test_sarcastic_thanks_stays_in_review(self):
        text = "Cảm ơn nhé, tuyệt quá"
        result = self.engine.process(event("sarcastic-thanks", text), POSTCARD,
            semantic(text, "social", "ack", [
                {"op": "SOCIAL_ACK", "evidence_excerpt": text}], sarcasm="present"))

        self.assertIn("REVIEW_SARCASM", result["routing"])
        self.assertIsNone(result["response_text"])
        self.assertEqual(result["sends"], [])

    def test_stock_negation_is_not_routed_to_color_recommendation(self):
        text = "Không lấy màu vàng, Creta còn màu nào?"
        result = self.engine.process(event("stock-not-color-advice", text), POSTCARD,
            semantic(text, "stock", "stock_lookup", [start(
                "stock", "stock_lookup", "Creta",
                {"kind": "vehicle", "value": "Creta", "explicit": True,
                 "evidence_excerpt": "Creta"})]))

        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertNotIn("answer_general_advice", result["routing"])
        self.assertNotIn("tông trung tính", result["response_text"].lower())
        self.assertNotIn("review", [route.lower() for route in result["routing"]])

    def test_negative_color_intent_is_reviewed_without_creating_or_completing_state(self):
        text = "Không cần tư vấn màu cho Creta"
        sem = semantic(text, "spec", "color_recommendation", [start(
            "spec", "color_recommendation", text,
            {"kind": "vehicle", "value": "Creta", "explicit": True,
             "evidence_excerpt": "Creta"})])
        sem["requests"][0]["polarity"] = "negative"
        result = self.engine.process(event("negative-color", text), POSTCARD, sem)

        self.assertTrue(any(route.startswith("REVIEW") for route in result["routing"]))
        self.assertNotIn("answer_general_advice", result["routing"])
        self.assertIsNone(result["response_text"])
        self.assertEqual(result["state_changes"], [])
        self.assertEqual(result["state_after"]["requests"], {})

    def test_cancel_operation_wins_over_color_advice_in_the_same_turn(self):
        text = "Hỏi màu Creta rồi thôi, hủy yêu cầu này"
        result = self.engine.process(event("cancel-color", text), POSTCARD,
            semantic(text, "spec", "color_recommendation", [
                start("spec", "color_recommendation", "Creta", {
                    "kind": "vehicle", "value": "Creta", "explicit": True,
                    "evidence_excerpt": "Creta"}),
                {"op": "CANCEL_REQUEST", "request_ref": "topic:spec",
                 "evidence_excerpt": "hủy yêu cầu này"}]))

        self.assertNotIn("answer_general_advice", result["routing"])
        self.assertIn("hủy", result["response_text"].lower())
        request = next(iter(result["state_after"]["requests"].values()))
        self.assertEqual(request["status"], "CANCELLED")

    def test_reject_pending_conflict_stays_in_review_instead_of_color_advice(self):
        text = "Không, thôi hỏi màu Creta"
        result = self.engine.process(event("reject-color", text), POSTCARD,
            semantic(text, "spec", "color_recommendation", [
                start("spec", "color_recommendation", "Creta", {
                    "kind": "vehicle", "value": "Creta", "explicit": True,
                    "evidence_excerpt": "Creta"}),
                {"op": "REJECT_PENDING", "request_ref": "topic:spec",
                 "pending_id": "missing-pending", "evidence_excerpt": "Không"}]))

        self.assertIn("REVIEW_BAD_PENDING", result["routing"])
        self.assertNotIn("answer_general_advice", result["routing"])
        self.assertIsNone(result["response_text"])
        self.assertEqual(result["state_after"]["requests"], {})

    def test_multi_intent_social_ack_keeps_the_color_answer(self):
        text = "Cảm ơn, Creta màu nào đẹp?"
        result = self.engine.process(event("multi-social-color", text), POSTCARD,
            semantic(text, "spec", "color_recommendation", [
                {"op": "SOCIAL_ACK", "evidence_excerpt": "Cảm ơn"},
                start("spec", "color_recommendation", "Creta", {
                    "kind": "vehicle", "value": "Creta", "explicit": True,
                    "evidence_excerpt": "Creta"})], requests=[
                {"topic": "social", "desired_outcome": "ack", "polarity": "positive"},
                {"topic": "spec", "desired_outcome": "color_recommendation", "polarity": "positive"}]))

        self.assertIn("SOCIAL_ACK", result["routing"])
        self.assertIn("answer_general_advice", result["routing"])
        self.assertIn("tông trung tính", result["response_text"].lower())
        self.assertNotIn("Cảm ơn bạn nhé", result["response_text"])

    def test_dealer_directory_is_readable_as_a_list_and_keeps_verification_caveat(self):
        names = ["Hyundai Cầu Giấy", "Hyundai Hà Đông", "Hyundai Long Biên",
                 "Hyundai Hoàng Mai", "Hyundai Mỹ Đình"]
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "dealers.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE dealers (name TEXT PRIMARY KEY, mailing_address TEXT NOT NULL)")
                conn.executemany("INSERT INTO dealers(name, mailing_address) VALUES (?, ?)", [
                    (name, f"{101 + i} Đường Mẫu, Hà Nội") for i, name in enumerate(names)])
            engine = make_engine(clock=Clock(NOW), facts=Facts([]), dealer_db_path=db_path)
            text = "Tìm đại lý Hyundai ở Hà Nội"
            operations = [start("dealer", "dealer_lookup", "đại lý"),
                {"op": "UPDATE_SLOT", "request_ref": "topic:dealer", "evidence_excerpt": "Hà Nội",
                 "slot": {"kind": "location", "value": "Hà Nội", "explicit": True,
                          "evidence_excerpt": "Hà Nội"}}]
            result = engine.process(event("dealer-1", text), POSTCARD,
                                    semantic(text, "dealer", "dealer_lookup", operations))

        lines = result["response_text"].splitlines()
        entries = [line for line in lines if line.startswith("• ")]
        self.assertEqual(len(entries), 5, result["response_text"])
        self.assertTrue(all(any(name in line for line in entries) for name in names))
        self.assertIn("chưa được xác minh trực tuyến", result["response_text"].lower())
        self.assertNotIn("gần nhất", result["response_text"].lower())
        self.assertNotIn("SAMPLE", result["response_text"].upper())


if __name__ == "__main__":
    unittest.main()
