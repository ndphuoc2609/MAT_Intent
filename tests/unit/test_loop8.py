"""Loop 8: model điền pending id vào request_ref (thay vì topic:...) thì
engine vẫn map pending -> request và xử lý đúng; ref hoàn toàn lạ vẫn
REVIEW_BAD_REF. Pending id là uuid runtime nên test động, không fixture tĩnh.
"""
import unittest, sys
sys.path.insert(0, "scripts")
from generate_datasets import SEM, SL, OP
from app.harness import make_engine
from app.engine import Clock
from datetime import datetime, timezone

PC = {"topic": "Creta 2026", "vehicles": ["Creta"], "persona_default": "tư vấn viên"}

def ev(i, text):
    return dict(page_id="p", post_id="post_1", thread_root_id="root", comment_id=f"c{i}",
                parent_id="root", actor_id="u1", actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"c{i}:1")

class TestPendingIdRef(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine(clock=Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)))

    def _price_ask(self):
        sem = SEM([("price", "answer", "positive")],
                  [OP("START_REQUEST", "topic:price", "Creta", SL("vehicle", "Creta")),
                   OP("UPDATE_SLOT", "topic:price", "bản cao", SL("variant", "Cao cấp"))])
        r = self.eng.process(ev("t0", "Creta bản cao giá bao nhiêu?"), PC, sem)
        self.assertIn("ask_clarification", r["routing"])
        pend = self.eng.store.open_pending_for("u1")
        self.assertEqual(len(pend), 1)
        return pend[0]["id"]

    def test_answer_pending_with_pending_id_as_ref(self):
        pid = self._price_ask()
        sem = SEM([("price", "answer", "positive")],
                  [dict(op="ANSWER_PENDING", request_ref=pid, pending_id=pid,
                        slot=SL("price_kind", "otr"), evidence_excerpt="lăn bánh")])
        r = self.eng.process(ev("t1", "lăn bánh"), PC, sem)
        # Đáp kind xong vẫn thiếu location -> hỏi tiếp, KHÔNG REVIEW_BAD_REF.
        self.assertIn("ask_clarification", r["routing"])
        self.assertNotIn("REVIEW_BAD_REF", r["routing"])
        self.assertEqual(len(r["sends"]), 1)

    def test_confirm_pending_with_pending_id_as_ref(self):
        sem0 = SEM([("testdrive", "book", "positive")],
                   [OP("START_REQUEST", "topic:testdrive", "Creta", SL("vehicle", "Creta")),
                    OP("UPDATE_SLOT", "topic:testdrive", "Hà Nội", SL("location", "Hà Nội")),
                    OP("UPDATE_SLOT", "topic:testdrive", "sáng mai", SL("datetime", "sáng mai"))])
        r0 = self.eng.process(ev("d0", "Đăng ký lái thử Creta ở Hà Nội sáng mai"), PC, sem0)
        self.assertIn("propose_test_drive_slot", r0["routing"])
        pid = self.eng.store.open_pending_for("u1")[0]["id"]
        sem = SEM([("testdrive", "book", "positive")],
                  [dict(op="CONFIRM_PENDING", request_ref=pid, pending_id=pid,
                        evidence_excerpt="oke")])
        r = self.eng.process(ev("d1", "oke"), PC, sem)
        self.assertIn("answer_confirmation", r["routing"])
        self.assertEqual(len(r["sends"]), 1)

    def test_unknown_ref_still_bad_ref(self):
        sem = SEM([("price", "answer", "positive")],
                  [dict(op="ANSWER_PENDING", request_ref="q_khong_ton_tai",
                        pending_id="q_khong_ton_tai",
                        slot=SL("price_kind", "otr"), evidence_excerpt="lăn bánh")])
        r = self.eng.process(ev("x0", "lăn bánh"), PC, sem)
        self.assertIn("REVIEW_BAD_REF", r["routing"])

if __name__ == "__main__":
    unittest.main()
