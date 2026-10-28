import sqlite3
import tempfile
import unittest
import unicodedata
from pathlib import Path
from datetime import datetime, timezone

from app.engine import Clock
from app.facts import (DealerDirectoryError, Facts, dealer_matches_location,
                       dealer_public_address, load_dealers)
from app.harness import make_engine


NOW = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
POSTCARD = {"topic": "vehicle", "vehicles": ["Creta"],
            "persona_default": "tư vấn viên"}


def directory(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE dealers (name TEXT PRIMARY KEY, mailing_address TEXT NOT NULL)")
        conn.executemany("INSERT INTO dealers(name, mailing_address) VALUES (?, ?)", rows)


def event(text="Tìm đại lý Hyundai ở Hà Nội", comment_id="dealer-1"):
    return {"page_id": "brand", "actor_id": "customer", "comment_id": comment_id,
            "post_id": "post", "parent_id": "root", "thread_root_id": "root",
            "actor_role": "user", "event_type": "create", "content_version": 1,
            "text": text}


def dealer_semantic(text="Tìm đại lý Hyundai ở Hà Nội", location="Hà Nội"):
    operations = [{"op": "START_REQUEST", "request_ref": "topic:dealer",
                   "topic": "dealer", "evidence_excerpt": "đại lý"}]
    if location:
        operations.append({"op": "UPDATE_SLOT", "request_ref": "topic:dealer",
                           "evidence_excerpt": location,
                           "slot": {"kind": "location", "value": location,
                                    "explicit": True, "evidence_excerpt": location}})
    return {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope",
            "requests": [{"topic": "dealer", "desired_outcome": "dealer_lookup",
                          "polarity": "positive"}],
            "operations": operations, "uncertainties": []}


class DealerDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "facts.sqlite3"
        directory(self.db, [
            ("Hyundai Cầu Diễn", "Ô số 02, Cụm công nghiệp Lai Xá, xã Hoài Đức, thành phố Hà Nội."),
            ("Hyundai Hà Đông", "Tổ 17, phường Yên Nghĩa, thành phố Hà Nội"),
            ("Hyundai Hồ Chí Minh", "Nguyễn Diễm Tiên - +84 849 295 929\nSố 94 Phạm Đình Hổ, Q.1, TP. HCM"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        return make_engine(clock=Clock(NOW), facts=Facts(facts=[]),
                           dealer_db_path=str(self.db))

    def test_loader_is_read_only_and_does_not_invent_verification_or_hours(self):
        before = self.db.read_bytes()
        facts = load_dealers(str(self.db))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(len(facts), 3)
        for fact in facts:
            self.assertEqual(fact["source"], "local_dealer_directory")
            self.assertNotIn("hours", fact["extra"])
            self.assertNotIn("verified_at", fact)
            self.assertNotIn("ttl_hours", fact)
        self.assertFalse(Facts(facts=[], dealer_facts=facts).is_fresh(facts[0], NOW))

    def test_harness_replaces_sample_dealers_and_answers_with_all_city_matches(self):
        engine = self.engine()
        result = engine.process(event(), POSTCARD, dealer_semantic())

        self.assertIn("answer_dealer", result["routing"])
        self.assertIn("Hyundai Cầu Diễn", result["response_text"])
        self.assertIn("Hyundai Hà Đông", result["response_text"])
        self.assertNotIn("Hyundai Hồ Chí Minh", result["response_text"])
        self.assertNotIn("SAMPLE", result["response_text"].upper())
        self.assertNotIn("+84", result["response_text"])
        self.assertNotIn("Diễm Tiên", result["response_text"])
        self.assertNotIn("gần nhất", result["response_text"].lower())
        self.assertNotIn("giờ mở cửa", result["response_text"].lower())
        self.assertIn("danh sách đại lý đã nạp", result["response_text"].lower())
        self.assertIn("kiểm tra cập nhật", result["response_text"].lower())

    def test_city_alias_and_district_match_only_addressed_dealer(self):
        engine = self.engine()
        sem = dealer_semantic("Tìm đại lý ở Hồ Chí Minh, Quận 1", "Hồ Chí Minh, Quận 1")
        result = engine.process(event("Tìm đại lý ở Hồ Chí Minh, Quận 1", "dealer-2"),
                                POSTCARD, sem)
        self.assertIn("answer_dealer", result["routing"])
        self.assertIn("Hyundai Hồ Chí Minh", result["response_text"])
        self.assertNotIn("Hyundai Hà Đông", result["response_text"])
        self.assertNotIn("Hyundai Cầu Diễn", result["response_text"])

    def test_missing_location_asks_for_region_without_claiming_nearest(self):
        text = "Bạn mua Creta ở đại lý nào?"
        engine = self.engine()
        result = engine.process(event(text, "dealer-3"), POSTCARD,
                                dealer_semantic(text, location=None))
        self.assertIn("ask_clarification", result["routing"])
        self.assertIn("tỉnh/thành phố", result["response_text"].lower())
        self.assertNotIn("gần nhất", result["response_text"].lower())

    def test_no_match_is_review_with_official_directory_link_not_staff_promise(self):
        text, location = "Tìm đại lý ở Narnia", "Narnia"
        engine = self.engine()
        result = engine.process(event(text, "dealer-4"), POSTCARD,
                                dealer_semantic(text, location=location))
        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertIn("https://hyundai.thanhcong.vn/thong-tin-dai-ly", result["response_text"])
        self.assertIn("dữ liệu chưa được xác minh trực tuyến", result["response_text"].lower())
        self.assertNotIn("nhân viên sẽ", result["response_text"].lower())
        self.assertEqual(engine.sender.sent, [])

    def test_missing_directory_is_reported_without_crashing_engine_or_using_seed(self):
        missing = str(Path(self.tmp.name) / "missing.sqlite3")
        with self.assertRaises(DealerDirectoryError):
            load_dealers(missing)
        engine = make_engine(clock=Clock(NOW), facts=Facts(facts=[]), dealer_db_path=missing)
        result = engine.process(event("Tìm đại lý ở Hà Nội", "dealer-missing"), POSTCARD,
                                dealer_semantic())
        self.assertEqual(engine.dealer_directory_error, "dealer directory file is missing")
        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertIn("chưa đọc được", result["response_text"].lower())
        self.assertIn("https://hyundai.thanhcong.vn/thong-tin-dai-ly", result["response_text"])
        self.assertNotIn("SAMPLE", result["response_text"].upper())

    def test_unrecognized_schema_is_reported_without_crashing_engine(self):
        invalid = Path(self.tmp.name) / "invalid.sqlite3"
        with sqlite3.connect(invalid) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
        engine = make_engine(clock=Clock(NOW), facts=Facts(facts=[]), dealer_db_path=str(invalid))
        result = engine.process(event("Tìm đại lý ở Hà Nội", "dealer-invalid"), POSTCARD,
                                dealer_semantic())
        self.assertEqual(engine.dealer_directory_error,
                         "dealer directory schema needs name and mailing_address")
        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertIn("chưa đọc được", result["response_text"].lower())
        self.assertNotIn("SAMPLE", result["response_text"].upper())

    def test_stale_dealer_name_cannot_override_the_address_location(self):
        fact = {"source": "local_dealer_directory", "location": None,
                "extra": {"name": "Hyundai Bình Định", "address": "Tỉnh Gia Lai"}}
        self.assertFalse(dealer_matches_location(fact, "Bình Định"))

    def test_contact_prefix_is_removed_without_losing_address_or_address_numbers(self):
        raw = unicodedata.normalize(
            "NFD", "Nguyễn A - 0901234567   Số 123, Quốc lộ 13, TP. HCM")
        expected = unicodedata.normalize("NFD", "Số 123, Quốc lộ 13, TP. HCM")
        self.assertEqual(dealer_public_address(raw), expected)

    def test_contact_suffix_is_removed_and_address_range_is_preserved(self):
        raw = "Số 94 -96 Phạm Đình Hổ, Q.1, TP. HCM - Nguyễn A 0901234567"
        self.assertEqual(dealer_public_address(raw), "Số 94 -96 Phạm Đình Hổ, Q.1, TP. HCM")

    def test_ambiguous_contact_only_line_is_omitted_and_matching_uses_sanitized_address(self):
        contact_only = "Nguyễn A - 0901234567"
        self.assertEqual(dealer_public_address(contact_only), "")
        fact = {"source": "local_dealer_directory", "location": None,
                "extra": {"name": "Hyundai HCM", "address":
                          "Nguyễn A - 0901234567 Số 123, Quốc lộ 13, TP. HCM"}}
        self.assertTrue(dealer_matches_location(fact, "TP.HCM"))
        self.assertFalse(dealer_matches_location(fact, "Nguyễn A"))

    def test_contact_name_without_phone_is_not_retained_as_an_address_line(self):
        raw = "Nguyễn A\nSố 123, Quốc lộ 13, TP. HCM"
        self.assertEqual(dealer_public_address(raw), "Số 123, Quốc lộ 13, TP. HCM")

    def test_same_line_contact_prefix_without_phone_is_removed(self):
        raw = "Nguyễn A   Số 123, Quốc lộ 13, TP. HCM"
        self.assertEqual(dealer_public_address(raw), "Số 123, Quốc lộ 13, TP. HCM")

    def test_same_line_contact_suffix_without_phone_is_removed(self):
        raw = "Số 123, Quốc lộ 13, TP. HCM - Nguyễn A"
        self.assertEqual(dealer_public_address(raw), "Số 123, Quốc lộ 13, TP. HCM")

    def test_address_without_contact_delimiter_is_fail_closed_only_when_ambiguous(self):
        self.assertEqual(dealer_public_address("Nguyễn A"), "")
        self.assertEqual(dealer_public_address("Nguyễn A 123"), "")

    def test_ward_office_number_prefix_is_preserved(self):
        self.assertEqual(dealer_public_address("Ô số 02, Lô 1, KCN Lai Xá, Hà Nội"),
                         "Ô số 02, Lô 1, KCN Lai Xá, Hà Nội")


if __name__ == "__main__":
    unittest.main()
