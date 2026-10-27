"""Regression tests cho các bản sửa review R01–R15 (2026-10-24).
Mỗi test tiêu cực bắt đúng lỗi cũ; test tích cực đi kèm chứng minh hành vi
hợp lệ vẫn hoạt động. Các test này đồng thời là check cho mutation suite.
"""
import sys, os, unittest, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from datetime import datetime, timezone
from app.facts import Facts, SEED_FACTS
from app.harness import make_engine
from app.engine import Clock
from app.renderer import claim_check_typed
from app.state import FakeSender

def sem(ops, topic="price"):
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                requests=[dict(topic=topic, desired_outcome="x", polarity="positive")],
                operations=ops, uncertainties=[])
def ev(cid, text, actor="u1"):
    return dict(page_id="p", post_id="post", thread_root_id="root", comment_id=cid,
                parent_id="root", actor_id=actor, actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"{cid}:1")
PC = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")
PCM = dict(topic="Nhiều xe", vehicles=["Creta", "Tucson"], persona_default="tư vấn viên")
T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

def _reqs(eng, actor="u1"):
    rows = eng.store.conn.execute(
        "SELECT id, topic, status, slots FROM requests ORDER BY rowid").fetchall()
    return [dict(id=r[0], topic=r[1], status=r[2], slots=json.loads(r[3])) for r in rows
            if eng.store.get_request(r[0])["actor_id"] == actor]

def _price_turn(eng, cid, text, vehicle="Creta", price_kind="list", variant="Cao cấp"):
    if price_kind == "list" and "niêm yết" not in text.casefold():
        text += " niêm yết"
    return eng.process(ev(cid, text), PC, sem([
        dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt=text,
             slot=dict(kind="vehicle", value=vehicle)),
        dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt=text,
             slot=dict(kind="variant", value=variant)),
        dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt=text,
             slot=dict(kind="price_kind", value=price_kind))]))

class TestReviewPendingLink(unittest.TestCase):
    def test_pending_of_other_request_rejected(self):
        eng = make_engine(clock=Clock(T0))
        _price_turn(eng, "c1", "giá Creta bản cao")
        # Hai request price: tạo thêm request testdrive để lấy pending khác.
        eng.process(ev("c2", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC, sem([
            dict(op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="lái thử"),
            dict(op="UPDATE_SLOT", request_ref="topic:testdrive", evidence_excerpt="Hà Nội",
                 slot=dict(kind="location", value="Hà Nội")),
            dict(op="UPDATE_SLOT", request_ref="topic:testdrive", evidence_excerpt="sáng thứ hai tuần sau",
                 slot=dict(kind="datetime", value="sáng thứ hai tuần sau"))], topic="testdrive"))
        td = [r for r in _reqs(eng) if r["topic"] == "testdrive"][0]
        pend = eng.store.open_pending_for("u1", td["id"])[0]
        # Dùng pending của testdrive để CONFIRM cho price -> phải reject.
        r = eng.process(ev("c3", "oke"), PC, sem([
            dict(op="CONFIRM_PENDING", request_ref="topic:price", evidence_excerpt="oke",
                 pending_id=pend["id"])], topic="price"))
        self.assertIn("REVIEW_BAD_PENDING", r["routing"])
        price = [x for x in _reqs(eng) if x["topic"] == "price"][0]
        self.assertNotEqual(price["status"], "READY")
    def test_confirm_slot_question_rejected(self):
        eng = make_engine(clock=Clock(T0))
        # Chỉ nêu xe -> plan hỏi price_kind, tạo pending slot question.
        eng.process(ev("c1", "giá Creta"), PC, sem([
            dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="giá Creta",
                 slot=dict(kind="vehicle", value="Creta"))]))
        price = [x for x in _reqs(eng) if x["topic"] == "price"][0]
        pend = eng.store.open_pending_for("u1", price["id"])[0]
        # Hỏi price_kind là pending choice, không phải yes_no -> CONFIRM reject.
        self.assertNotEqual(pend["qtype"], "yes_no")
        r = eng.process(ev("c2", "oke"), PC, sem([
            dict(op="CONFIRM_PENDING", request_ref="topic:price", evidence_excerpt="oke",
                 pending_id=pend["id"])], topic="price"))
        self.assertIn("REVIEW_BAD_PENDING", r["routing"])

class TestReviewHandoffScope(unittest.TestCase):
    def _handoff_creta_price(self, eng):
        _price_turn(eng, "h1", "giá Creta bản cao")
        eng.process(ev("h2", "gặp nhân viên xử lý giá"), PC, sem([
            dict(op="REQUEST_HUMAN", request_ref="topic:price", evidence_excerpt="gặp nhân viên",
                 reason="staff")], topic="price"))
    def test_same_vehicle_blocked_other_vehicle_allowed(self):
        eng = make_engine(clock=Clock(T0))
        self._handoff_creta_price(eng)
        blocked = _price_turn(eng, "h3", "giá Creta bản cao")
        self.assertIn("BLOCKED_HANDOFF", blocked["routing"])
        self.assertEqual(len(blocked.get("sends", [])), 0)
        allowed = eng.process(ev("h4", "giá Tucson bản cao niêm yết"), PCM, sem([
            dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="giá Tucson"),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Tucson",
                 slot=dict(kind="vehicle", value="Tucson")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                 slot=dict(kind="variant", value="Cao cấp")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                 slot=dict(kind="price_kind", value="list"))]))
        self.assertTrue(allowed.get("sends"))

class TestReviewFacts(unittest.TestCase):
    def test_unknown_price_kind_is_unresolved(self):
        eng = make_engine(clock=Clock(T0), facts=Facts())
        raw_kind = "giá niêm yết hay lăn bánh tại Hà Nội"
        r = eng.process(ev("pk1", "Creta Cao cấp giá niêm yết hay lăn bánh tại Hà Nội"), PC, sem([
            dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                 slot=dict(kind="vehicle", value="Creta")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Cao cấp",
                 slot=dict(kind="variant", value="Cao cấp")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Hà Nội",
                 slot=dict(kind="location", value="Hà Nội")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt=raw_kind,
                 slot=dict(kind="price_kind", value=raw_kind))]))
        self.assertEqual(r["response_text"], "Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?")
        req = _reqs(eng)[0]
        self.assertEqual(req["slots"]["price_kind"]["value"], None)
        self.assertEqual(req["slots"]["price_kind"]["raw"], raw_kind)

    def test_stock_zero_not_available(self):
        facts = [dict(f, value=0) if f["type"] == "stock" else f for f in SEED_FACTS]
        eng = make_engine(clock=Clock(T0), facts=Facts(facts=facts))
        r = eng.process(ev("c1", "Creta Cao cấp Trắng còn không"), PC, sem([
            dict(op="START_REQUEST", request_ref="topic:stock", evidence_excerpt="Creta",
                 slot=dict(kind="vehicle", value="Creta")),
            dict(op="UPDATE_SLOT", request_ref="topic:stock", evidence_excerpt="Cao cấp",
                 slot=dict(kind="variant", value="Cao cấp")),
            dict(op="UPDATE_SLOT", request_ref="topic:stock", evidence_excerpt="Trắng",
                 slot=dict(kind="color", value="Trắng"))], topic="stock"))
        self.assertIn("hết", r["response_text"])
        self.assertNotIn("Còn Creta", r["response_text"])
    def test_otr_fee_conflict_handoff(self):
        facts = list(SEED_FACTS) + [dict(id="f_plate_hn_dup", type="plate_fee",
            vehicle=None, variant=None, location="Hà Nội", value=25_000_000, unit="VND",
            valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=720)]
        eng = make_engine(clock=Clock(T0), facts=Facts(facts=facts))
        r = eng.process(ev("c1", "Creta Cao cấp lăn bánh Hà Nội"), PC, sem([
            dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                 slot=dict(kind="vehicle", value="Creta")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Cao cấp",
                 slot=dict(kind="variant", value="Cao cấp")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Hà Nội",
                 slot=dict(kind="location", value="Hà Nội")),
            dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="lăn bánh",
                 slot=dict(kind="price_kind", value="otr"))]))
        self.assertIn("HANDOFF", r["routing"])
        self.assertEqual(len(r.get("sends", [])), 0)

class TestReviewTypedClaim(unittest.TestCase):
    def test_swapped_prices_rejected(self):
        used = [dict(fact_id="a", vehicle="Creta", variant="Cao cấp", attribute="list_price",
                     value=699_000_000, unit="VND"),
                dict(fact_id="b", vehicle="Tucson", variant="Cao cấp", attribute="list_price",
                     value=899_000_000, unit="VND")]
        errs = claim_check_typed(
            "Giá niêm yết Creta Cao cấp là 899.000.000 đồng, Tucson Cao cấp là 699.000.000 đồng.",
            used)
        self.assertTrue(errs)
    def test_correct_mixed_passes(self):
        used = [dict(fact_id="a", vehicle="Creta", variant="Cao cấp", attribute="list_price",
                     value=699_000_000, unit="VND"),
                dict(fact_id="b", vehicle="Tucson", variant="Cao cấp", attribute="list_price",
                     value=899_000_000, unit="VND")]
        errs = claim_check_typed(
            "Giá niêm yết Creta Cao cấp là 699.000.000 đồng, Tucson Cao cấp là 899.000.000 đồng.",
            used)
        self.assertEqual(errs, [])

class TestReviewFactsVersionRecheck(unittest.TestCase):
    def test_dispatch_blocked_on_facts_version_bump_only(self):
        # Chỉ bump facts.version (fact vẫn fresh, giá trị không đổi): precheck
        # version vẫn phải chặn dispatch — version là tín hiệu "facts đã đổi
        # giữa plan và dispatch", không chờ tới khi fact hết hạn mới chặn.
        eng = make_engine(clock=Clock(T0))
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                      slot=dict(kind="variant", value="Cao cấp")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                      slot=dict(kind="price_kind", value="list"))])
        eng.dispatch_hook = lambda: eng.facts.touch()
        r = eng.process(ev("d9", "Creta bản cao niêm yết?"), PC, s)
        self.assertIn("BLOCKED_STALE_FACTS", r["routing"])
        self.assertEqual(len(r.get("sends", [])), 0)

class TestReviewReceiverDedupe(unittest.TestCase):
    def test_redelivery_same_key_one_effect(self):
        eng = make_engine(clock=Clock(T0))
        payload = dict(dedupe_key="k1", text="hi", comment_id="c1")
        first = eng.sender.send(payload)
        second = eng.sender.send(payload)
        self.assertEqual(eng.sender.receiver.effect_count("k1"), 1)
        # Redelivery không tạo send mới: trả cờ duplicate, sent list không tăng.
        self.assertTrue(second.get("duplicate_delivery"))
        self.assertEqual(len(eng.sender.sent), 1)

if __name__ == "__main__":
    unittest.main()
