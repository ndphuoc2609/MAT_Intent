"""Unit tests (stdlib unittest, chạy: python3 -m unittest discover -s tests -v).
Kiểm tra behavior/contract, không mirror công thức implementation."""
import sys, os, unittest, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from datetime import datetime, timezone
from app.normalize import canon_variant, canon_location, parse_relative_datetime, TZ
from app.semantics import validate_semantic
from app.facts import Facts, calc_otr
from app.harness import make_engine
from app.engine import Clock

def sem(ops, topic="price"):
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                requests=[dict(topic=topic, desired_outcome="x", polarity="positive")],
                operations=ops, uncertainties=[])
def ev(cid, text, actor="u1"):
    return dict(page_id="p", post_id="post", thread_root_id="root", comment_id=cid,
                parent_id="root", actor_id=actor, actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"{cid}:1")
PC = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")

class TestNormalize(unittest.TestCase):
    def test_canon(self):
        self.assertEqual(canon_variant("bản cao"), "Cao cấp")
        self.assertEqual(canon_location("HN"), "Hà Nội")
    def test_relative_datetime(self):
        now = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)
        d = parse_relative_datetime("chiều mai", now)
        self.assertEqual((d.day, d.month, d.hour), (25, 10, 15))

class TestSemanticValidation(unittest.TestCase):
    def test_reject_unknown_op(self):
        s = sem([dict(op="FLY", request_ref="topic:price", evidence_excerpt="giá")])
        self.assertTrue(any("unknown op" in e for e in validate_semantic(s, "giá", [])))
    def test_reject_fake_evidence(self):
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="đã cọc")])
        self.assertTrue(validate_semantic(s, "giá bao nhiêu", []))
    def test_reject_bool_sarcasm(self):
        s = sem([dict(op="SOCIAL_ACK", request_ref="topic:social", evidence_excerpt="đẹp")])
        s["sarcasm"] = True
        self.assertTrue(validate_semantic(s, "đẹp", []))
    def test_accept_valid(self):
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta"))])
        self.assertEqual(validate_semantic(s, "Creta giá bao nhiêu", []), [])

class TestFacts(unittest.TestCase):
    def test_otr(self):
        c = calc_otr(699_000_000, 12, 20_000_000, 1_560_000)
        self.assertEqual(c["total"], 804_440_000)
    def test_expired_and_conflict(self):
        f = Facts(); now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        fresh, stale, conf = f.query("promo", vehicle="Creta", now=now)
        self.assertEqual(fresh, []); self.assertTrue(stale)

class TestEngine(unittest.TestCase):
    def test_price_otr_and_duplicate(self):
        eng = make_engine()
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Bản cao",
                      slot=dict(kind="variant", value="Cao cấp")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="lăn bánh",
                      slot=dict(kind="price_kind", value="otr")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="HN",
                      slot=dict(kind="location", value="Hà Nội"))])
        r = eng.process(ev("c1", "Creta Bản cao lăn bánh HN bao nhiêu?"), PC, s)
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("804.440.000", r["response_text"])
        r2 = eng.process(ev("c1", "Creta Bản cao lăn bánh HN bao nhiêu?"), PC, s)
        self.assertEqual(r2["routing"], ["DUPLICATE_IGNORED"]); self.assertEqual(len(r2["sends"]), 0)
    def test_version_conflict_detected(self):
        eng = make_engine()
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta"))])
        eng.process(ev("c1", "Creta giá?"), PC, s)
        req = eng.store.get_request(eng.store.active_requests("p", "u1")[0]["id"])
        stale = dict(req)  # bản stale, version DB sẽ bị đẩy lên trước
        req2 = eng.store.get_request(req["id"]); req2["status"] = "OPEN"; req2["version"] += 1
        eng.store.save_request(req2)
        with self.assertRaises(RuntimeError):
            stale["status"] = "OPEN"; stale["version"] += 1; eng.store.save_request(stale)
    def test_outbox_dedupe_no_blind_retry(self):
        eng = make_engine()
        eng.store.conn.execute("BEGIN IMMEDIATE")
        eng._outbox(ev("c9", "x"), [], "reply", "hi", "SENT", "2026-10-24T09:00:00+07:00",
                    dict(dedupe_key="c9:reply", text="hi", comment_id="c9"))
        eng.store.conn.commit()
        eng.store.conn.execute("BEGIN IMMEDIATE")
        # Ghi lại khác nội dung: row SENT không được ghi đè (chỉ đếm row thì
        # INSERT OR REPLACE vẫn ra 1 row nên phải assert cả nội dung).
        eng._outbox(ev("c9", "x"), [], "reply", "hi2", "SENT", "2026-10-24T09:00:00+07:00",
                    dict(dedupe_key="c9:reply", text="hi2", comment_id="c9"))
        eng.store.conn.commit()
        rows = eng.store.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["text"], "hi")
        # Nhánh touched: cùng key, khác nội dung cũng không ghi đè.
        req = eng.store.get_request(eng.store.active_requests("p", "u1")[0]["id"]) \
            if eng.store.active_requests("p", "u1") else None
        if req is None:
            s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                          slot=dict(kind="vehicle", value="Creta"))])
            eng.process(ev("c10", "Creta giá?"), PC, s)
            req = eng.store.get_request(eng.store.active_requests("p", "u1")[0]["id"])
        eng.store.conn.execute("BEGIN IMMEDIATE")
        eng._outbox(ev("c10", "x"), [req], "reply", "first", "SENT", "2026-10-24T09:00:00+07:00")
        eng.store.conn.commit()
        key = f"c10:{req['id']}:reply:{req['version']}"
        eng.store.conn.execute("BEGIN IMMEDIATE")
        eng._outbox(ev("c10", "x"), [req], "reply", "second", "SENT", "2026-10-24T09:00:00+07:00")
        eng.store.conn.commit()
        touched_rows = [r for r in eng.store.ledger() if r["dedupe_key"] == key]
        self.assertEqual(len(touched_rows), 1)
        self.assertEqual(touched_rows[0]["payload"]["text"], "first")
    def test_no_label_leakage_in_runtime(self):
        import pathlib
        app = pathlib.Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "app"
        for p in app.rglob("*.py"):
            txt = p.read_text()
            self.assertNotIn("expected_final", txt, f"label leakage in {p}")
            self.assertNotIn("human_gold", txt, f"label leakage in {p}")

if __name__ == "__main__":
    unittest.main()
