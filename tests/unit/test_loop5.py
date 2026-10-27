"""Loop 5 (2026-10-24): dedupe identity, thời gian, facts TTL/thứ tự,
claim provenance, dispatch recheck, UNKNOWN reconcile, handoff expiry."""
import sys, os, unittest, random, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from datetime import datetime, timezone
from app.normalize import parse_time_expression, TZ, yes_no
from app.facts import Facts, SEED_FACTS
from app.harness import make_engine
from app.engine import Clock
from app.renderer import claim_check
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

class TestTimeParser(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)  # thứ 7
    def test_next_tuesday(self):
        k, v = parse_time_expression("thứ ba tuần sau", self.NOW)
        self.assertEqual((k, v.day, v.month, v.hour), ("exact", 27, 10, 10))
    def test_explicit_date(self):
        k, v = parse_time_expression("ngày 3 tháng 11", self.NOW)
        self.assertEqual((k, v.day, v.month), ("exact", 3, 11))
    def test_weekend_range(self):
        k, v = parse_time_expression("cuối tuần này", self.NOW)
        self.assertEqual(k, "range")
        self.assertEqual([(d.day, d.month) for d in v[3]], [(24, 10), (25, 10)])
    def test_month_end_vague(self):
        self.assertEqual(parse_time_expression("cuối tháng này", self.NOW)[0], "vague")
    def test_morning_tomorrow(self):
        k, v = parse_time_expression("sáng mai", self.NOW)
        self.assertEqual((k, v.day, v.hour), ("exact", 25, 9))
    def test_past_date(self):
        dec = datetime(2026, 12, 1, 9, 0, tzinfo=TZ)
        self.assertEqual(parse_time_expression("ngày 3 tháng 11", dec)[0], "past")
    def test_time_only_without_date(self):
        self.assertEqual(parse_time_expression("chiều", self.NOW), ("time_only", 15))
    def test_explicit_clock_with_date(self):
        now = datetime(2026, 10, 26, 16, 30, tzinfo=TZ)
        for text, expected in (("ngày 27 tháng 10 lúc 18h", (27, 18, 0)),
                               ("ngày 27 tháng 10 lúc 18h30", (27, 18, 30)),
                               ("ngày 27 tháng 10 lúc 18:30", (27, 18, 30)),
                               ("ngày 27 tháng 10 lúc 18 giờ 30", (27, 18, 30)),
                               ("hôm nay lúc 18h", (26, 18, 0))):
            with self.subTest(text=text):
                kind, value = parse_time_expression(text, now)
                self.assertEqual(kind, "exact")
                self.assertEqual((value.day, value.hour, value.minute), expected)
    def test_numeric_time_only_does_not_drop_minutes(self):
        self.assertEqual(parse_time_expression("18h30", self.NOW)[0], "time_only")
    def test_explicit_clock_respects_daypart(self):
        for text, hour in (("ngày 27 tháng 10 lúc 3h chiều", 15), ("ngày 27 tháng 10 lúc 6h tối", 18)):
            kind, value = parse_time_expression(text, self.NOW)
            self.assertEqual((kind, value.hour), ("exact", hour))
    def test_today_explicit_past_clock_is_past(self):
        now = datetime(2026, 10, 26, 16, 30, tzinfo=TZ)
        self.assertEqual(parse_time_expression("hôm nay lúc 15h", now)[0], "past")

class TestYesNo(unittest.TestCase):
    def test_short_vietnamese_yes_phrases(self):
        for text in ("có", "có ạ", "dạ có", "vâng, có", "Có, xác nhận lịch đó."):
            with self.subTest(text=text):
                self.assertIs(yes_no(text), True)

class TestFactsOrderAndTTL(unittest.TestCase):
    def test_date_only_validity_uses_vietnamese_calendar_dates(self):
        promo = next(f for f in SEED_FACTS if f["id"] == "f_promo_creta_oct")
        promo = dict(promo, ttl_hours=1000)
        facts = Facts(facts=[promo], verified_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        now = datetime(2026, 10, 1, 0, 30, tzinfo=TZ)
        self.assertEqual(len(facts.query("promo", vehicle="Creta", now=now)[0]), 1)

        now = datetime(2026, 11, 1, 0, 30, tzinfo=TZ)
        fresh, stale, _ = facts.query("promo", vehicle="Creta", now=now)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

        now = datetime(2026, 10, 31, 23, 59, 59, 500000, tzinfo=TZ)
        self.assertEqual(len(facts.query("promo", vehicle="Creta", now=now)[0]), 1)

    def test_explicit_offset_validity_bounds_keep_their_instants(self):
        promo = dict(next(f for f in SEED_FACTS if f["id"] == "f_promo_creta_oct"),
                     valid_from="2026-10-01T00:00:00+07:00",
                     valid_to="2026-10-31T23:59:59+07:00", ttl_hours=1000)
        facts = Facts(facts=[promo], verified_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        for now in (datetime(2026, 9, 30, 17, tzinfo=timezone.utc),
                    datetime(2026, 10, 31, 16, 59, 59, tzinfo=timezone.utc)):
            with self.subTest(now=now):
                self.assertEqual(len(facts.query("promo", vehicle="Creta", now=now)[0]), 1)

    def test_shuffled_order_same_answer(self):
        rng = random.Random(7); facts = list(SEED_FACTS); rng.shuffle(facts)
        eng = make_engine(facts=Facts(facts=facts))
        s = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                      slot=dict(kind="variant", value="Cao cấp")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                      slot=dict(kind="price_kind", value="list"))])
        r = eng.process(ev("f1", "Creta bản cao niêm yết?"), PC, s)
        self.assertIn("699.000.000", r["response_text"])
    def test_stale_ttl_promo_handoff(self):
        # verified_at cách đây 40 ngày > TTL promo 720h (30 ngày) -> stale
        old = datetime(2026, 9, 14, tzinfo=timezone.utc)
        now = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
        f = Facts(verified_at=old)
        fresh, stale, conf = f.query("promo", vehicle="Creta", now=now)
        self.assertEqual(fresh, []); self.assertTrue(any(x["id"] == "f_promo_creta_oct" for x in stale))

class TestClaimProvenance(unittest.TestCase):
    USED = [dict(fact_id="f_list_creta_high", vehicle="Creta", variant="Cao cấp",
                 location=None, attribute="list_price", value=699000000, unit="VND")]
    def test_tucson_number_in_creta_reply(self):
        self.assertTrue(claim_check("Giá Tucson là 699.000.000 đồng", [699000000],
                                    self.USED, {"Creta"}))
    def test_wrong_unit(self):
        self.assertTrue(claim_check("Giá Creta là 699.000.000%", [699000000],
                                    self.USED, {"Creta"}))
    def test_db_number_not_selected(self):
        self.assertTrue(claim_check("Giá Creta là 899.000.000 đồng", [699000000],
                                    self.USED, {"Creta"}))
    def test_wrong_attribute(self):
        used = [dict(fact_id="f_spec_creta", vehicle="Creta", variant=None, location=None,
                     attribute="seats", value=5, unit=""),
                dict(fact_id="f_spec_creta", vehicle="Creta", variant=None, location=None,
                     attribute="power_hp", value=115, unit="")]
        self.assertTrue(claim_check("Thông số mẫu Creta: seats 5, power_hp 5",
                                    [5, 115], used, {"Creta"}))
    def test_correct_claim_passes(self):
        self.assertEqual(claim_check("Giá niêm yết Creta Cao cấp là 699.000.000 đồng",
                                     [699000000], self.USED, {"Creta"}), [])

class TestIdentityAndDispatch(unittest.TestCase):
    def price_sem(self, vehicle):
        # Kèm variant: Creta không variant sẽ conflict 2 giá -> HANDOFF theo
        # thiết kế (không phải lỗi identity), nên test identity phải có variant.
        return sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt=vehicle,
                         slot=dict(kind="vehicle", value=vehicle)),
                    dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                         slot=dict(kind="variant", value="Cao cấp")),
                    dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                         slot=dict(kind="price_kind", value="list"))], topic="price")
    def test_price_two_vehicles_kept_separate(self):
        # Request đầu còn đang mở (thiếu price_kind -> WAITING_USER) thì hỏi
        # giá xe khác phải tạo request mới, không được tái dùng request cũ
        # (trước Loop 5, reuse chỉ theo topic sẽ ghi đè Creta thành Tucson).
        eng = make_engine()
        ask = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                        slot=dict(kind="vehicle", value="Creta"))], topic="price")
        eng.process(ev("i1", "Creta giá bao nhiêu?"), PCM, ask)
        eng.process(ev("i2", "Tucson bản cao niêm yết?"), PCM, self.price_sem("Tucson"))
        reqs = eng.store.conn.execute("SELECT topic,status,slots FROM requests").fetchall()
        self.assertEqual(len(reqs), 2)
        vehicles = sorted(json.loads(r[2])["vehicle"]["value"] for r in reqs)
        self.assertEqual(vehicles, ["Creta", "Tucson"])
        self.assertEqual(sorted(r[1] for r in reqs), ["COMPLETED", "WAITING_USER"])
    def test_dispatch_blocked_by_pref_change(self):
        eng = make_engine()
        s = self.price_sem("Creta")
        def hook(): eng.store.block("p", "u1", "public_reply")
        eng.dispatch_hook = hook
        r = eng.process(ev("d1", "Creta bản cao niêm yết?"), PC, s)
        self.assertIn("BLOCKED_AT_DISPATCH", r["routing"]); self.assertEqual(len(r["sends"]), 0)
    def test_dispatch_blocked_stale_facts(self):
        eng = make_engine()
        s = self.price_sem("Creta")
        def hook():
            for f in eng.facts.facts:
                if f["id"] == "f_list_creta_high": f["valid_to"] = "2020-01-01"
            eng.facts.touch()
        eng.dispatch_hook = hook
        r = eng.process(ev("d2", "Creta bản cao niêm yết?"), PC, s)
        self.assertIn("BLOCKED_STALE_FACTS", r["routing"]); self.assertEqual(len(r["sends"]), 0)
    def test_dispatch_blocked_stale_state(self):
        eng = make_engine()
        s = self.price_sem("Creta")
        def hook():
            # price request đã COMPLETED nên không nằm trong active_requests;
            # lấy trực tiếp từ bảng requests để mô phỏng thay đổi đồng thời.
            rid = eng.store.conn.execute("SELECT id FROM requests").fetchone()[0]
            cur = eng.store.get_request(rid); cur["version"] += 1
            eng.store.save_request(cur)
        eng.dispatch_hook = hook
        r = eng.process(ev("d3", "Creta bản cao niêm yết?"), PC, s)
        self.assertIn("BLOCKED_STALE_STATE", r["routing"]); self.assertEqual(len(r["sends"]), 0)

class TestUnknownAndHandoff(unittest.TestCase):
    def price_sem(self):
        return sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                         slot=dict(kind="vehicle", value="Creta")),
                    dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                         slot=dict(kind="price_kind", value="list"))])
    def test_unknown_reconcile(self):
        from app.state import Store
        from app.engine import Engine
        store = Store(); sender = FakeSender(fail_mode="lost_ack")
        eng = Engine(store, Facts(), sender, Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)))
        r = eng.process(ev("u1x", "Creta niêm yết?"), PC, self.price_sem())
        self.assertIn("SEND_UNKNOWN", r["routing"]); self.assertEqual(len(r["sends"]), 0)
        # redelivery cùng event: DUPLICATE_IGNORED, không retry mù
        r2 = eng.process(ev("u1x", "Creta niêm yết?"), PC, self.price_sem())
        self.assertEqual(r2["routing"], ["DUPLICATE_IGNORED"])
        # R03: lost_ack = kết quả không xác định, sender KHÔNG ghi effect nên
        # reconcile giữ UNKNOWN (không retry mù, không tự kết luận SENT).
        # ACK muộn (confirm_delivery) ghi effect -> reconcile chốt SENT.
        self.assertEqual(eng.reconcile_unknown()[0][1], "UNKNOWN")
        sender.confirm_delivery("u1x:reply")
        self.assertEqual(eng.reconcile_unknown()[0][1], "SENT")
        # UNKNOWN mà receiver KHÔNG có effect thì giữ UNKNOWN; CLAIMED mà
        # không có effect -> INTENDED.
        from app.state import Store as _Store
        st2 = _Store(); eng2 = Engine(st2, Facts(), FakeSender(), Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)))
        st2.insert_outbox_intended("k-unk", None, "reply", 1, {"dedupe_key": "k-unk"}, "2026-10-24T09:00:00+00:00")
        st2.set_outbox_status("k-unk", "UNKNOWN")
        st2.insert_outbox_intended("k-clm", None, "reply", 1, {"dedupe_key": "k-clm"}, "2026-10-24T09:00:00+00:00")
        st2.claim_outbox("k-clm")
        got = dict(eng2.reconcile_unknown())
        self.assertEqual(got["k-unk"], "UNKNOWN"); self.assertEqual(got["k-clm"], "INTENDED")
    def test_handoff_expiry_and_release(self):
        eng = make_engine()
        s = sem([dict(op="REQUEST_HUMAN", request_ref="topic:complaint",
                      evidence_excerpt="khiếu nại", reason="complaint")], topic="complaint")
        eng.process(ev("h1", "Khiếu nại!"), PC, s)
        # comment mới khi handoff còn tươi -> BLOCKED_HANDOFF
        s2 = sem([dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                       slot=dict(kind="vehicle", value="Creta"))])
        # complaint và price khác request: handoff chỉ chặn request complaint,
        # nhưng op trên request mới không bị chặn -> kiểm tra release thủ công:
        eng.store.release_handoff(eng.store.conn.execute("SELECT request_id FROM handoffs").fetchone()[0],
                                  "2026-10-24T10:00:00+07:00")
        self.assertIsNone(eng.store.handoff(eng.store.conn.execute("SELECT id FROM requests").fetchone()[0]))
        # R04 (contract mới): handoff quá SLA KHÔNG tự release — sweep chỉ
        # đánh dấu OVERDUE, bot vẫn bị chặn cho tới khi staff release rõ ràng.
        eng2 = make_engine()
        eng2.process(ev("h2", "Khiếu nại!"), PC, s)
        rid = eng2.store.conn.execute("SELECT request_id FROM handoffs").fetchone()[0]
        eng2.store.conn.execute("UPDATE handoffs SET acquired_at='2026-10-20T09:00:00+07:00' WHERE request_id=?", (rid,))
        eng2.store.conn.commit()
        eng2.clock.advance(hours=1)
        r = eng2.process(ev("h3", "Khiếu nại tiếp!"), PC, s)
        self.assertIn("BLOCKED_HANDOFF", r["routing"])
        self.assertTrue(eng2.store.handoff(rid)["overdue"])
        # staff release rõ ràng -> bot tiếp tục được (event mới, vì h3 đã ingest)
        eng2.release_handoff(rid, "staff_reviewed")
        r = eng2.process(ev("h4", "Khiếu nại tiếp!"), PC, s)
        self.assertNotIn("BLOCKED_HANDOFF", r["routing"])

class TestMixedPrefComplaint(unittest.TestCase):
    def test_pref_call_kept_price_sent_complaint_draft(self):
        eng = make_engine()
        s = sem([dict(op="SET_CONTACT_PREFERENCE", request_ref="topic:price", evidence_excerpt="Đừng gọi", scope="call"),
                 dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                      slot=dict(kind="vehicle", value="Creta")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="bản cao",
                      slot=dict(kind="variant", value="Cao cấp")),
                 dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                      slot=dict(kind="price_kind", value="list")),
                 dict(op="REPORT_COMPLAINT", request_ref="topic:complaint", evidence_excerpt="vụ tiền cọc",
                      reason="deposit_dispute")], topic="price")
        r = eng.process(ev("m1", "Đừng gọi nữa, gửi giá niêm yết Creta bản cao tại đây và chuyển quản lý xử lý vụ tiền cọc."), PC, s)
        self.assertEqual(eng.store.blocks("p", "u1"), frozenset({"call"}))
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("699.000.000", r["response_text"])
        purposes = [(x["purpose"], x["status"]) for x in eng.store.ledger()]
        self.assertIn(("reply", "SENT"), purposes)
        self.assertIn(("handoff_draft", "NEEDS_REVIEW"), purposes)

if __name__ == "__main__":
    unittest.main()
