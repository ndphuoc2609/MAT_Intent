"""Recovery: crash/restart với DB file — state + outbox bền vững, redelivery
sau restart không gửi lại (dedupe theo event + outbox)."""
import sys, os, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.harness import make_engine
PC = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")
SEM = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
           requests=[dict(topic="price", desired_outcome="price_list", polarity="positive")],
           operations=[dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                            slot=dict(kind="vehicle", value="Creta")),
                       dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                            slot=dict(kind="variant", value="Cao cấp")),
                       dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                            slot=dict(kind="price_kind", value="list"))])
EV = dict(page_id="p", post_id="post", thread_root_id="root", comment_id="r1",
          parent_id="root", actor_id="u1", actor_role="user", event_type="create",
          content_version=1, text="Creta bản cao niêm yết bao nhiêu?", event_dedupe_key="r1:1")

class TestRecovery(unittest.TestCase):
    def test_crash_restart_no_resend(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "s.db")
            eng = make_engine(db=db)
            r1 = eng.process(EV, PC, SEM); self.assertEqual(len(r1["sends"]), 1)
            # "crash": bỏ engine, mở lại cùng DB file
            eng2 = make_engine(db=db)
            r2 = eng2.process(EV, PC, SEM)
            self.assertEqual(r2["routing"], ["DUPLICATE_IGNORED"])
            self.assertEqual(len(r2["sends"]), 0)
            self.assertEqual(len(eng2.store.ledger()), 1)
            self.assertEqual(eng2.store.conn.execute(
                "SELECT status FROM requests").fetchone()[0], "COMPLETED")

if __name__ == "__main__":
    unittest.main()
