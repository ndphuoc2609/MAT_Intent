"""Race test bằng barrier: 20 thread cùng event (dedupe) và 10 thread comment khác
cùng actor. Kỳ vọng: đúng 1 send cho event trùng, không ERROR_ROLLBACK."""
import sys, os, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.harness import make_engine
PC = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")
def sem():
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                requests=[dict(topic="price", desired_outcome="price_list", polarity="positive")],
                operations=[dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                                 slot=dict(kind="vehicle", value="Creta")),
                            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                                 slot=dict(kind="variant", value="Cao cấp")),
                            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                                 slot=dict(kind="price_kind", value="list"))])
def ev(cid, text):
    return dict(page_id="p", post_id="post", thread_root_id="root", comment_id=cid,
                parent_id="root", actor_id="u1", actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"{cid}:1")

class TestRace(unittest.TestCase):
    def test_same_event_20_threads(self):
        eng = make_engine(); bar = threading.Barrier(20); out = []
        def run():
            bar.wait(); out.append(eng.process(ev("dup", "Creta bản cao niêm yết bao nhiêu?"), PC, sem()))
        ts = [threading.Thread(target=run) for _ in range(20)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(len(r["sends"]) for r in out), 1)
        self.assertEqual(sum(1 for r in out if r["routing"] == ["DUPLICATE_IGNORED"]), 19)
        self.assertFalse(any("ERROR_ROLLBACK" in r["routing"] for r in out))
    def test_different_comments_same_actor(self):
        eng = make_engine(); bar = threading.Barrier(10); out = []
        def run(i):
            bar.wait(); out.append(eng.process(ev(f"c{i}", "Creta bản cao niêm yết bao nhiêu?"), PC, sem()))
        ts = [threading.Thread(target=lambda i=i: run(i)) for i in range(10)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertFalse(any("ERROR_ROLLBACK" in r["routing"] for r in out))
        # mỗi comment là một câu hỏi độc lập -> 1 request COMPLETED mỗi comment,
        # không mất update, không version conflict
        self.assertEqual(sum(len(r["sends"]) for r in out), 10)
        self.assertEqual(len(eng.store.conn.execute(
            "SELECT id FROM requests WHERE status='COMPLETED'").fetchall()), 10)

if __name__ == "__main__":
    unittest.main()
