"""Regression cho audit session 4 (A1–A8) — mỗi test đều FAIL trên baseline
review-fix (trừ các positive control) và PASS sau sửa. Chạy:
python3 -m unittest discover -s tests -t . -k TestReviewAudit
Không gọi model thật, không gửi Facebook thật.
"""
import contextlib, io, json, os, sys, tempfile, unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from app.harness import make_engine
from app.engine import Clock

NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
PC = {"topic": "Hyundai", "vehicles": ["Creta"], "persona_default": "tư vấn viên"}


def eng(db=":memory:"):
    return make_engine(db=db, clock=Clock(NOW))


def ev(cid, text, actor="uA", page="p"):
    return dict(page_id=page, post_id="post", thread_root_id="root", comment_id=cid,
                parent_id="root", actor_id=actor, actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=cid + ":1")


def sem(ops, topic="price", uncertainties=None, sarcasm="absent"):
    return dict(schema_version="1", sentiment="NEU", sarcasm=sarcasm, scope="in_scope",
                requests=[dict(topic=topic, desired_outcome=topic, polarity="positive")],
                operations=ops, uncertainties=uncertainties or [])


def op(t, ref, ex, kind=None, value=None, **kw):
    r = dict(op=t, request_ref=ref, evidence_excerpt=ex, **kw)
    if kind:
        r["slot"] = dict(kind=kind, value=value)
    return r


def getreq(e, topic, actor="uA", vehicle=None):
    for (rid,) in e.store.conn.execute(
            "SELECT id FROM requests WHERE actor_id=? AND topic=? ORDER BY rowid DESC", (actor, topic)):
        r = e.store.get_request(rid)
        if not vehicle or (r["slots"].get("vehicle") or {}).get("value") == vehicle:
            return r
    return None


def td_propose(e, cid="td"):
    """Đề xuất lái thử Creta/Hà Nội/sáng mai; trả (request, pending)."""
    text = "Lái thử Creta Hà Nội sáng mai"
    r = e.process(ev(cid, text), PC, sem([
        op("START_REQUEST", "topic:testdrive", text, "vehicle", "Creta"),
        op("UPDATE_SLOT", "topic:testdrive", text, "location", "Hà Nội"),
        op("UPDATE_SLOT", "topic:testdrive", text, "datetime", "sáng mai")], "testdrive"))
    assert "ERROR_ROLLBACK" not in r["routing"], r
    req = getreq(e, "testdrive")
    pend = e.store.open_pending_for("uA", req["id"])[0]
    return req, pend


def price_req(e, cid="price", vehicle="Creta", kind="list", page="p"):
    text = f"{vehicle} Cao cấp giá niêm yết"
    ops = [op("START_REQUEST", "topic:price", text, "vehicle", vehicle),
           op("UPDATE_SLOT", "topic:price", text, "variant", "Cao cấp")]
    if kind:
        ops.append(op("UPDATE_SLOT", "topic:price", text, "price_kind", kind))
    r = e.process(ev(cid, text, page=page), PC, sem(ops))
    assert "ERROR_ROLLBACK" not in r["routing"], r
    return r, getreq(e, "price", vehicle=vehicle)


class TestMixedVehicleReply(unittest.TestCase):
    def test_safe_stock_reply_acknowledges_unverified_second_vehicle(self):
        e = eng()
        pc = {"topic": "Nhiều xe Hyundai", "vehicles": ["Creta", "Tucson"],
              "persona_default": "tư vấn viên"}
        text = "Creta và Tucson còn xe không?"
        r = e.process(ev("two_stock", text), pc, sem([
            op("START_REQUEST", "topic:stock", "Creta", "vehicle", "Creta"),
            op("START_REQUEST", "topic:stock", "Tucson", "vehicle", "Tucson")
        ], "stock"))
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("Creta", r["response_text"])
        self.assertIn("Tucson", r["response_text"])
        self.assertIn("chưa được xác minh", r["response_text"])


class TestA1Ambiguity(unittest.TestCase):
    def test_iso_datetime_update_preserves_local_date_and_time(self):
        e = eng()
        req, _ = td_propose(e)
        text = "Chuyển sang 3 giờ chiều"
        value = "2026-10-25T15:00:00+07:00"
        r = e.process(ev("update_iso_datetime", text), PC, sem([
            dict(op="UPDATE_SLOT", request_ref=req["id"], evidence_excerpt=text,
                 slot=dict(kind="datetime", value=value, raw="3 giờ chiều"))], "testdrive"))
        self.assertIn("propose_test_drive_slot", r["routing"])
        updated = e.store.get_request(req["id"])
        self.assertEqual(updated["slots"]["datetime"]["value"], value)
        self.assertFalse(updated["slots"]["datetime"]["explicit"])
        self.assertEqual(updated["slots"]["datetime"]["source_message_id"], "td")
        self.assertEqual(updated["slots"]["datetime"]["source_actor_id"], "uA")
        self.assertEqual(updated["slots"]["datetime"]["source_request_id"], req["id"])
        self.assertIn("25/10 lúc 15:00", r["response_text"])

    def test_explicit_affirmation_phrase_confirms_yes_no_pending(self):
        e = eng()
        req, pend = td_propose(e)
        text = "Được, xác nhận lịch đó."
        r = e.process(ev("confirm_phrase", text), PC, sem([
            op("CONFIRM_PENDING", req["id"], text, pending_id=pend["id"])
        ], "testdrive"))
        self.assertIn("answer_confirmation", r["routing"])
        self.assertEqual(e.store.get_request(req["id"])["status"], "READY")

    def test_ambiguous_update_restores_live_slot_and_supersedes_pending(self):
        e = eng()
        req, pend = td_propose(e)
        self.assertEqual(req["slots"]["location"]["value"], "Hà Nội")
        text = "Đổi sang Hà Nội hay TP.HCM"
        r = e.process(ev("amb", text), PC, sem([
            op("UPDATE_SLOT", req["id"], text, "location", "TP.HCM")], "testdrive",
            uncertainties=[{"different_action": True, "alternatives": ["Hà Nội", "TP.HCM"]}]))
        self.assertIn("REVIEW_AMBIGUOUS", r["routing"])
        self.assertEqual(r["sends"], [])
        after = e.store.get_request(req["id"])
        self.assertEqual(after["slots"]["location"]["value"], "Hà Nội")
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "SUPERSEDED")

    def test_stale_proposition_confirm_blocked_after_ambiguous_update(self):
        e = eng()
        req, pend = td_propose(e)
        text = "Đổi sang Hà Nội hay TP.HCM"
        e.process(ev("amb", text), PC, sem([
            op("UPDATE_SLOT", req["id"], text, "location", "TP.HCM")], "testdrive",
            uncertainties=[{"different_action": True, "alternatives": ["Hà Nội", "TP.HCM"]}]))
        r = e.process(ev("confirm_old", "oke"), PC, sem([
            op("CONFIRM_PENDING", req["id"], "oke", pending_id=pend["id"])], "testdrive"))
        self.assertNotEqual(e.store.get_request(req["id"])["status"], "READY")
        self.assertEqual(r["sends"], [])
        self.assertIn(r["routing"][0], ("REVIEW_BAD_PENDING", "REVIEW_STALE_PROPOSITION"))

    def test_stale_basis_guard_direct(self):
        # Guard basis: pending vẫn OPEN nhưng slot đã đổi (VD cập nhật từ
        # kênh khác không qua plan supersede) thì CONFIRM phải bị chặn.
        e = eng()
        req, pend = td_propose(e)
        cur = e.store.get_request(req["id"])
        cur["slots"]["location"] = dict(value="TP.HCM", raw="TP.HCM",
                                        source_message_id="staff_note", explicit=True, confirmed=False)
        cur["version"] += 1
        e.store.save_request(cur)
        e.store.conn.commit()  # ghi trực tiếp ngoài process() phải commit
        r = e.process(ev("confirm_stale", "oke"), PC, sem([
            op("CONFIRM_PENDING", req["id"], "oke", pending_id=pend["id"])], "testdrive"))
        self.assertIn("REVIEW_STALE_PROPOSITION", r["routing"])
        self.assertEqual(r["sends"], [])

    def test_clear_update_then_confirm_new_proposal(self):
        # Positive: cập nhật RÕ RÀNG vẫn hoạt động — đề xuất mới được xác nhận đúng.
        e = eng()
        req, pend = td_propose(e)
        text = "Đổi sang TP.HCM"
        r = e.process(ev("upd", text), PC, sem([
            op("UPDATE_SLOT", req["id"], text, "location", "TP.HCM")], "testdrive"))
        self.assertIn("propose_test_drive_slot", r["routing"])
        req2 = e.store.get_request(req["id"])
        self.assertEqual(req2["slots"]["location"]["value"], "TP.HCM")
        pend2 = e.store.open_pending_for("uA", req["id"])[0]
        self.assertNotEqual(pend2["id"], pend["id"])
        r = e.process(ev("ok2", "oke"), PC, sem([
            op("CONFIRM_PENDING", req["id"], "oke", pending_id=pend2["id"])], "testdrive"))
        self.assertIn("answer_confirmation", r["routing"])
        self.assertEqual(e.store.get_request(req["id"])["status"], "READY")
        self.assertEqual(e.store.get_request(req["id"])["slots"]["location"]["value"], "TP.HCM")

    def test_uncertain_optin_keeps_blocks(self):
        e = eng()
        e.process(ev("block", "Đừng liên hệ"), PC, sem([
            op("SET_CONTACT_PREFERENCE", None, "Đừng liên hệ", scope="all")]))
        self.assertEqual(e.store.blocks("p", "uA"), frozenset({"all"}))
        r = e.process(ev("optin", "Cứ gọi thoải mái"), PC, sem([
            op("SET_CONTACT_PREFERENCE", None, "Cứ gọi thoải mái", scope="allow_all")],
            sarcasm="uncertain"))
        self.assertIn("REVIEW_SARCASM", r["routing"])
        self.assertEqual(e.store.blocks("p", "uA"), frozenset({"all"}))

    def test_clear_optin_while_blocked_still_works(self):
        # Positive: opt-in rõ ràng khi đang bị chặn vẫn ghi nhận được.
        e = eng()
        e.process(ev("block", "Đừng liên hệ"), PC, sem([
            op("SET_CONTACT_PREFERENCE", None, "Đừng liên hệ", scope="all")]))
        e.process(ev("optin", "Cứ gọi thoải mái"), PC, sem([
            op("SET_CONTACT_PREFERENCE", None, "Cứ gọi thoải mái", scope="allow_all")]))
        self.assertEqual(e.store.blocks("p", "uA"), frozenset())


class TestA2PendingEvidence(unittest.TestCase):
    def test_choice_answer_wrong_slot_kind_rejected(self):
        e = eng()
        _, req = price_req(e, kind=None)
        pend = e.store.open_pending_for("uA", req["id"])[0]
        self.assertEqual(pend["expected_slot"], "price_kind")
        r = e.process(ev("ans", "niêm yết Hà Nội"), PC, sem([
            op("ANSWER_PENDING", req["id"], "Hà Nội", "location", "Hà Nội", pending_id=pend["id"])]))
        self.assertIn("REVIEW_BAD_PENDING", r["routing"])
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "OPEN")
        self.assertNotIn("location", e.store.get_request(req["id"])["slots"])

    def test_choice_answer_right_slot_works(self):
        e = eng()
        _, req = price_req(e, kind=None)
        pend = e.store.open_pending_for("uA", req["id"])[0]
        r = e.process(ev("ans", "niêm yết"), PC, sem([
            op("ANSWER_PENDING", req["id"], "niêm yết", "price_kind", "list", pending_id=pend["id"])]))
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "ANSWERED")
        self.assertEqual(e.store.get_request(req["id"])["slots"]["price_kind"]["value"], "list")

    def test_reject_with_thanks_not_rejected(self):
        e = eng()
        req, pend = td_propose(e, "rej")
        r = e.process(ev("thanks", "Cảm ơn"), PC, sem([
            op("REJECT_PENDING", req["id"], "Cảm ơn", pending_id=pend["id"])], "testdrive"))
        self.assertIn("REVIEW_AMBIGUOUS", r["routing"])
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "OPEN")

    def test_reject_then_no_reproposal_loop(self):
        e = eng()
        req, pend = td_propose(e, "rej2")
        r = e.process(ev("no", "không"), PC, sem([
            op("REJECT_PENDING", req["id"], "không", pending_id=pend["id"])], "testdrive"))
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "REJECTED")
        # Lượt sau chạm lại request: không được đề xuất lại đúng combo cũ.
        text = "Lái thử Creta Hà Nội sáng mai"
        r = e.process(ev("again", text), PC, sem([
            op("UPDATE_SLOT", req["id"], text, "location", "Hà Nội")], "testdrive"))
        self.assertNotIn("propose_test_drive_slot", r["routing"])
        self.assertIn("ask_clarification", r["routing"])
        # Trả lời giờ khác -> đề xuất mới hợp lệ.
        pend2 = e.store.open_pending_for("uA", req["id"])[0]
        r = e.process(ev("newtime", "chiều mai"), PC, sem([
            op("ANSWER_PENDING", req["id"], "chiều mai", "datetime", "chiều mai",
               pending_id=pend2["id"])], "testdrive"))
        self.assertIn("propose_test_drive_slot", r["routing"])


class TestA3PageIsolation(unittest.TestCase):
    def test_context_does_not_read_other_page(self):
        e = eng()
        price_req(e, "page_A_secret", page="page_A")
        ctx = e.context(ev("page_B_context", "oke", page="page_B"), PC)
        self.assertFalse(any(x["comment_id"] == "page_A_secret" for x in ctx["recent"]))
        ctx2 = e.context(dict(ev("page_B_context2", "oke", page="page_B"),
                               parent_id="page_A_secret"), PC)
        self.assertEqual(ctx2["parent_texts"], [])

    def test_sweep_does_not_expire_other_page_pending(self):
        e = eng()
        _, req = price_req(e, "page_A_secret", kind=None, page="page_A")
        pend = e.store.open_pending_for("uA", req["id"])[0]
        e.clock.advance(hours=25)
        e.process(ev("page_B_sweep", "chào", page="page_B"), PC, sem([
            op("SOCIAL_ACK", None, "chào")], "social"))
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "OPEN")
        e.process(ev("page_A_sweep", "chào", page="page_A"), PC, sem([
            op("SOCIAL_ACK", None, "chào")], "social"))
        self.assertEqual(e.store.get_pending(pend["id"])["status"], "EXPIRED")


class TestA4MultiRequestHandoff(unittest.TestCase):
    def _handoff_creta_price(self, e):
        _, req = price_req(e, "handoff_price", kind=None)
        e.process(ev("handoff", "Gặp nhân viên"), PC, sem([
            op("REQUEST_HUMAN", req["id"], "Gặp nhân viên", human_source="customer_explicit")]))
        self.assertTrue(e.store.handoff(req["id"]))
        return req

    def _mixed_turn(self, e, order):
        text = "Giá niêm yết Creta Cao cấp và Tucson Cao cấp"
        def start(veh):
            s = op("START_REQUEST", "topic:price", text, "vehicle", veh)
            s["slot"]["extra"] = {"variant": "Cao cấp", "price_kind": "list"}
            return s
        ops = [start(v) for v in order]
        s = sem(ops)
        s["requests"] = [dict(topic="price", desired_outcome="price_" + v, polarity="positive")
                         for v in order]
        return e.process(ev("mixed", text), PC, s)

    def test_mixed_same_ref_creta_first(self):
        e = eng()
        old = self._handoff_creta_price(e)
        r = self._mixed_turn(e, ["Creta", "Tucson"])
        self.assertIn("BLOCKED_HANDOFF", r["routing"])
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("Tucson", r["response_text"])
        self.assertNotIn("Creta", r["response_text"])
        self.assertTrue(e.store.handoff(old["id"]))
        n_price = len([x for x in e.store.conn.execute(
            "SELECT id FROM requests WHERE actor_id='uA' AND topic='price'").fetchall()])
        self.assertEqual(n_price, 2)  # cũ (handoff) + Tucson mới; không tạo Creta mới

    def test_mixed_same_ref_tucson_first(self):
        e = eng()
        old = self._handoff_creta_price(e)
        r = self._mixed_turn(e, ["Tucson", "Creta"])
        self.assertIn("BLOCKED_HANDOFF", r["routing"])
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("Tucson", r["response_text"])
        self.assertNotIn("Creta", r["response_text"])
        self.assertTrue(e.store.handoff(old["id"]))

    def test_two_cars_same_topic_no_handoff_both_answered(self):
        e = eng()
        r = self._mixed_turn(e, ["Creta", "Tucson"])
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("Creta", r["response_text"])
        self.assertIn("Tucson", r["response_text"])


if __name__ == "__main__":
    unittest.main()


def _crash_fn(*args, **kwargs):
    raise RuntimeError("injected process death")


class TestA5OutboxRestart(unittest.TestCase):
    def test_committed_intent_drained_on_redelivery(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "app.db")
            e = eng(db)
            e._dispatch = _crash_fn
            r, req, event, s = None, None, None, None
            text = "Creta Cao cấp giá niêm yết"
            event = ev("crash1", text)
            s = sem([op("START_REQUEST", "topic:price", text, "vehicle", "Creta"),
                     op("UPDATE_SLOT", "topic:price", text, "variant", "Cao cấp"),
                     op("UPDATE_SLOT", "topic:price", text, "price_kind", "list")])
            r = e.process(event, PC, s)
            self.assertEqual([x["status"] for x in e.store.ledger()], ["INTENDED"])
            e2 = eng(db)
            again = e2.process(event, PC, s)
            self.assertEqual(again["routing"], ["DUPLICATE_IGNORED"])
            self.assertEqual([x["status"] for x in e2.store.ledger()], ["SENT"])
            self.assertEqual(e2.sender.receiver.effect_count(), 1)
            self.assertEqual(again.get("drained"), [("crash1:reply", "SENT")])

    def test_social_intent_survives_crash_before_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "social.db")
            e = eng(db)
            e._dispatch = _crash_fn
            event = ev("social_lost", "chào")
            s = sem([op("SOCIAL_ACK", None, "chào")], "social")
            e.process(event, PC, s)
            self.assertEqual(len(e.store.ledger()), 1)
            self.assertEqual(e.store.ledger()[0]["status"], "INTENDED")
            e2 = eng(db)
            again = e2.process(event, PC, s)
            self.assertEqual(again["routing"], ["DUPLICATE_IGNORED"])
            self.assertEqual([x["status"] for x in e2.store.ledger()], ["SENT"])
            self.assertEqual(e2.sender.receiver.effect_count(), 1)

    def test_disambiguation_intent_survives_crash_before_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "dis.db")
            e = eng(db)
            # Hai đề xuất lái thử -> CANCEL topic mơ hồ -> câu hỏi làm rõ.
            for cid, veh in (("td1", "Creta"), ("td2", "Tucson")):
                text = f"Lái thử {veh} Hà Nội sáng mai"
                e.process(ev(cid, text), PC, sem([
                    op("START_REQUEST", "topic:testdrive", text, "vehicle", veh),
                    op("UPDATE_SLOT", "topic:testdrive", text, "location", "Hà Nội"),
                    op("UPDATE_SLOT", "topic:testdrive", text, "datetime", "sáng mai")], "testdrive"))
            e._dispatch = _crash_fn
            event = ev("amb1", "Hủy lịch này")
            s = sem([op("CANCEL_REQUEST", "topic:testdrive", "Hủy lịch này")], "testdrive")
            r = e.process(event, PC, s)
            self.assertIn("REVIEW_REF_AMBIGUOUS", r["routing"])
            # Hai lượt đề xuất trước đó đã SENT câu hỏi của chúng; chỉ row
            # của câu hỏi làm rõ này mới phải là INTENDED.
            row = e.store.get_outbox("amb1:reply")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "INTENDED")
            e2 = eng(db)
            again = e2.process(event, PC, s)
            self.assertEqual(again["routing"], ["DUPLICATE_IGNORED"])
            self.assertEqual(e2.store.get_outbox("amb1:reply")["status"], "SENT")

    def test_receiver_file_backed_restart_reconciles_delivered_ack_lost(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "recv.db")
            e = eng(db)
            real_send = e.sender.send

            def delivered_but_ack_lost(payload):
                real_send(payload)
                return {"status": "UNKNOWN", "id": payload["dedupe_key"]}
            e.sender.send = delivered_but_ack_lost
            text = "Creta Cao cấp giá niêm yết"
            event = ev("acklost", text)
            s = sem([op("START_REQUEST", "topic:price", text, "vehicle", "Creta"),
                     op("UPDATE_SLOT", "topic:price", text, "variant", "Cao cấp"),
                     op("UPDATE_SLOT", "topic:price", text, "price_kind", "list")])
            e.process(event, PC, s)
            self.assertEqual([x["status"] for x in e.store.ledger()], ["UNKNOWN"])
            self.assertEqual(e.sender.receiver.effect_count(), 1)
            # Restart: receiver file vẫn thấy effect -> SENT, không effect thứ hai.
            e2 = eng(db)
            self.assertEqual(e2.sender.receiver.effect_count(), 1)
            rec = e2.reconcile_unknown()
            self.assertIn(("acklost:reply", "SENT"), rec)
            self.assertEqual(e2.sender.receiver.effect_count(), 1)
            self.assertEqual([x["status"] for x in e2.store.ledger()], ["SENT"])

    def test_undelivered_unknown_stays_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "unk.db")
            e = eng(db)
            e.sender.fail_mode = "lost_ack"
            text = "Creta Cao cấp giá niêm yết"
            event = ev("unk1", text)
            s = sem([op("START_REQUEST", "topic:price", text, "vehicle", "Creta"),
                     op("UPDATE_SLOT", "topic:price", text, "variant", "Cao cấp"),
                     op("UPDATE_SLOT", "topic:price", text, "price_kind", "list")])
            e.process(event, PC, s)
            self.assertEqual([x["status"] for x in e.store.ledger()], ["UNKNOWN"])
            self.assertEqual(e.sender.receiver.effect_count(), 0)
            e2 = eng(db)
            rec = e2.reconcile_unknown()
            self.assertIn(("unk1:reply", "UNKNOWN"), rec)
            self.assertEqual([x["status"] for x in e2.store.ledger()], ["UNKNOWN"])

    def test_claim_lease_not_reset_blindly_then_expires(self):
        e = eng()
        e.store.insert_outbox_intended("k1", None, "reply", 1,
                                       dict(dedupe_key="k1", text="x", comment_id="c1"),
                                       NOW.isoformat())
        e.store.conn.commit()
        self.assertTrue(e.store.claim_outbox("k1", "worker-A", NOW.isoformat()))
        e.store.conn.commit()
        rec = e.reconcile_unknown()
        self.assertIn(("k1", "CLAIMED"), rec)  # lease còn hiệu lực: giữ CLAIMED
        e.clock.advance(minutes=6)
        rec = e.reconcile_unknown()
        self.assertIn(("k1", "INTENDED"), rec)  # hết lease: mới được reclaim

    def test_drain_skips_stale_state(self):
        e = eng()
        _, req = price_req(e, "stale1", kind=None)
        # Intent ghi với version cũ, sau đó request đổi version.
        e.store.insert_outbox_intended("stale:reply", req["id"], "reply", 1,
                                       dict(dedupe_key="stale:reply", text="giá cũ",
                                            comment_id="stale1"),
                                       NOW.isoformat())
        e.store.conn.commit()
        cur = e.store.get_request(req["id"])
        cur["version"] += 1
        e.store.save_request(cur)
        e.store.conn.commit()
        before = e.sender.receiver.effect_count()  # setup đã gửi câu hỏi choice
        drained = e.drain_intended(NOW.isoformat())
        self.assertIn(("stale:reply", "BLOCKED_STALE_STATE"), drained)
        self.assertEqual(e.sender.receiver.effect_count(), before)  # không gửi text cũ


class TestA6Provenance(unittest.TestCase):
    def test_slot_source_fake_rejected(self):
        e = eng()
        o = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o["slot"]["source_message_id"] = "NONEXISTENT_SOURCE"
        r = e.process(ev("fake_source", "Tucson"), PC, sem([o], "spec"))
        self.assertIn("REVIEW_PROVENANCE", r["routing"])
        self.assertIsNone(getreq(e, "spec"))

    def test_slot_source_other_actor_rejected(self):
        e = eng()
        # Event của actor khác (uB) với text "Tucson".
        e.process(ev("other_actor", "Tucson", actor="uB"), PC, sem([
            op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")], "spec"))
        o = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o["slot"]["source_message_id"] = "other_actor"
        r = e.process(ev("fake2", "Tucson"), PC, sem([o], "spec"))
        self.assertIn("REVIEW_PROVENANCE", r["routing"])

    def test_slot_source_other_page_rejected(self):
        e = eng()
        price_req(e, "page_A_ev", page="page_A")
        o = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o["slot"]["source_message_id"] = "page_A_ev"
        r = e.process(ev("fake3", "Tucson", page="page_B"), PC, sem([o], "spec"))
        self.assertIn("REVIEW_PROVENANCE", r["routing"])

    def test_evidence_not_in_claimed_source_rejected(self):
        e = eng()
        # Event cũ cùng actor/page, text không chứa "Tucson".
        e.process(ev("old_ev", "Creta Cao cấp giá niêm yết"), PC, sem([
            op("START_REQUEST", "topic:price", "Creta", "vehicle", "Creta")]))
        o = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o["slot"]["source_message_id"] = "old_ev"
        r = e.process(ev("cur_ev", "Tucson bản cao"), PC, sem([o], "spec"))
        self.assertIn("REVIEW_PROVENANCE", r["routing"])
        self.assertIsNone(getreq(e, "spec"))

    def test_valid_sources_accepted(self):
        e = eng()
        # Op source = event hiện tại, evidence nằm trong nó.
        o = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o["source_message_id"] = "cur_ok"
        r = e.process(ev("cur_ok", "Tucson bản cao"), PC, sem([o], "spec"))
        self.assertNotIn("REVIEW_PROVENANCE", r["routing"])
        req = getreq(e, "spec")
        self.assertIsNotNone(req)
        self.assertEqual(req["slots"]["vehicle"]["source_message_id"], "cur_ok")
        # Slot source riêng (không có op source) cũng được validate + ghi.
        o2 = op("START_REQUEST", "topic:spec", "Tucson", "vehicle", "Tucson")
        o2["slot"]["source_message_id"] = "cur_ok2"
        r = e.process(ev("cur_ok2", "Tucson bản cao"), PC, sem([o2], "spec"))
        self.assertNotIn("REVIEW_PROVENANCE", r["routing"])


class TestPositiveControls(unittest.TestCase):
    def test_price_full_slots_sends(self):
        e = eng()
        r, _ = price_req(e, kind="list")
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("699.000.000", r["response_text"])

    def test_stock_variant_choice_no_buy(self):
        e = eng()
        text = "Creta Tiêu chuẩn còn hàng không"
        r = e.process(ev("stock1", text), PC, sem([
            op("START_REQUEST", "topic:stock", text, "vehicle", "Creta"),
            op("UPDATE_SLOT", "topic:stock", text, "variant", "Tiêu chuẩn")], "stock"))
        self.assertIn("answer_stock", r["routing"])
        self.assertIsNone(getreq(e, "buy"))

    def test_block_call_keeps_price_reply(self):
        # "Đừng gọi, gửi giá tại đây": block call nhưng phần giá vẫn gửi.
        e = eng()
        text = "Đừng gọi, gửi giá Creta Cao cấp niêm yết tại đây"
        r = e.process(ev("pref1", text), PC, sem([
            op("SET_CONTACT_PREFERENCE", None, "Đừng gọi", scope="call"),
            op("START_REQUEST", "topic:price", text, "vehicle", "Creta"),
            op("UPDATE_SLOT", "topic:price", text, "variant", "Cao cấp"),
            op("UPDATE_SLOT", "topic:price", text, "price_kind", "list")]))
        self.assertEqual(e.store.blocks("p", "uA"), frozenset({"call"}))
        self.assertEqual(len(r["sends"]), 1)
        self.assertIn("699.000.000", r["response_text"])

    def test_explicit_human_request_still_handoff(self):
        e = eng()
        _, req = price_req(e, kind=None)
        r = e.process(ev("human1", "Gặp nhân viên"), PC, sem([
            op("REQUEST_HUMAN", req["id"], "Gặp nhân viên", human_source="customer_explicit")]))
        self.assertIn("HANDOFF", r["routing"])
        self.assertEqual(r["sends"], [])

    def test_confirm_is_proposal_not_booking(self):
        e = eng()
        req, pend = td_propose(e)
        r = e.process(ev("ok", "oke"), PC, sem([
            op("CONFIRM_PENDING", req["id"], "oke", pending_id=pend["id"])], "testdrive"))
        self.assertIn("answer_confirmation", r["routing"])
        self.assertIn("chưa phải lịch đã đặt", r["response_text"])
