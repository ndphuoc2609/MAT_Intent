"""Shared engine regressions required by the live conversation sandbox."""
import json
import unittest
from datetime import datetime, timezone

from app.engine import Clock
from app.facts import Facts, SEED_FACTS
from app.harness import make_engine
from app.semantics import validate_semantic


PC = dict(topic="Creta", vehicles=["Creta"], persona_default="tư vấn viên")
T0 = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)


def event(cid, text, **changes):
    value = dict(page_id="p", actor_id="u", comment_id=cid, post_id="post",
                 parent_id="root", thread_root_id="root", actor_role="user",
                 event_type="create", content_version=1, text=text)
    value.update(changes)
    return value


def semantic(*operations, scope="in_scope"):
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope=scope,
                requests=[dict(topic="price", desired_outcome="giá", polarity="positive")],
                operations=list(operations), uncertainties=[])


def operation(kind="START_REQUEST", **changes):
    value = dict(op=kind, request_ref="topic:price", evidence_excerpt="Creta",
                 slot=dict(kind="vehicle", value="Creta"))
    value.update(changes)
    return value


def business_state(engine):
    return {table: engine.store.conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("requests", "pending", "handoffs", "contact_prefs",
                          "contact_blocks", "outbox")}


class TestScopeRegressions(unittest.TestCase):
    def test_unknown_scope_cannot_change_requests_preferences_or_expired_pending(self):
        operations = [operation(), operation("UPDATE_SLOT", slot=dict(kind="vehicle", value="Tucson")),
                      dict(op="CANCEL_REQUEST", request_ref="topic:price", evidence_excerpt="Creta"),
                      dict(op="SET_CONTACT_PREFERENCE", scope="all", evidence_excerpt="Creta"),
                      dict(op="SOCIAL_ACK", evidence_excerpt="Creta")]
        for op in operations:
            with self.subTest(op=op["op"]):
                engine = make_engine(clock=Clock(T0))
                engine.process(event("initial", "Creta"), PC, semantic(operation()))
                engine.clock.advance(days=7)
                before = business_state(engine)
                sent_before = len(engine.sender.sent)
                result = engine.process(event("unknown", "Creta"), PC, semantic(op, scope="unknown"))
                self.assertEqual(business_state(engine), before)
                self.assertEqual(len(engine.sender.sent), sent_before)
                self.assertEqual(result["sends"], [])
                self.assertTrue(any(route.startswith("REVIEW") for route in result["routing"]))


class TestFactConflicts(unittest.TestCase):
    def test_query_conflicts_on_spec_and_dealer_details_but_not_stock_colors(self):
        now = T0
        facts = list(SEED_FACTS)
        for base, duplicate in (("f_spec_creta", "spec_copy"), ("f_dealer_hn", "dealer_copy")):
            row = next(f for f in facts if f["id"] == base).copy()
            row["id"] = duplicate
            row["extra"] = dict(row["extra"], conflict="different")
            facts.append(row)
        db = Facts(facts)
        for ftype, kwargs in (("spec", {"vehicle": "Creta"}), ("dealer", {"location": "Hà Nội"})):
            with self.subTest(ftype=ftype):
                self.assertTrue(db.query(ftype, now=now, **kwargs)[2])
        self.assertFalse(db.query("stock", vehicle="Creta", now=now)[2])

    def test_query_handles_nested_spec_metadata(self):
        facts = list(SEED_FACTS)
        row = next(f for f in facts if f["id"] == "f_spec_creta").copy()
        row["id"] = "spec_nested_copy"
        row["extra"] = dict(row["extra"], features=["ABS"], details={"safety": ["airbag"]})
        facts.append(row)
        self.assertTrue(Facts(facts).query("spec", vehicle="Creta", now=T0)[2])

    def test_conflicting_spec_dealer_warranty_and_registration_facts_handoff(self):
        cases = (
            ("spec", "spec", {"vehicle": "Creta"}, "spec_copy", {"extra": {"engine": "1.6L", "seats": 5, "power_hp": 115}}),
            ("dealer", "dealer", {"location": "Hà Nội"}, "dealer_copy", {"extra": {"name": "Other dealer", "address": "Other address", "hours": "9-5"}}),
            ("service", "warranty", {}, "warranty_copy", {"value": 3}),
            ("registration", "registration_fee", {"location": "Hà Nội"}, "reg_fee_copy", {"value": 3_000_000}),
        )
        for topic, ftype, query, copy_id, changes in cases:
            with self.subTest(topic=topic):
                facts = list(SEED_FACTS)
                row = next(f for f in facts if f["type"] == ftype and
                           all(f.get(k) == v for k, v in query.items())).copy()
                row["id"] = copy_id
                row.update(changes)
                facts.append(row)
                engine = make_engine(clock=Clock(T0), facts=Facts(facts))
                slots = []
                for name, value in query.items():
                    kind = "vehicle" if name == "vehicle" else "location"
                    slots.append(dict(op="UPDATE_SLOT", request_ref=f"topic:{topic}",
                                      evidence_excerpt=value, slot=dict(kind=kind, value=value)))
                ops = [dict(op="START_REQUEST", request_ref=f"topic:{topic}",
                            evidence_excerpt="request")]
                ops.extend(slots)
                if topic == "service":
                    ops.append(dict(op="UPDATE_SLOT", request_ref=f"topic:{topic}",
                                    evidence_excerpt="warranty", slot=dict(kind="service_topic", value="warranty")))
                sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                           requests=[dict(topic=topic, desired_outcome=topic, polarity="positive")],
                           operations=ops, uncertainties=[])
                result = engine.process(event(f"conflict_{topic}", "request Creta Hà Nội warranty"), PC, sem)
                self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
                handoffs = [row for row in engine.store.ledger()
                            if row["purpose"] == "handoff_draft" and row["dedupe_key"].startswith(f"conflict_{topic}:")]
                self.assertTrue(handoffs)
                handoff_text = " ".join(row["payload"].get("text", "") for row in handoffs)
                sent_text = " ".join((result["response_text"], handoff_text,
                                      *(item.get("text", "") for item in engine.sender.sent)))
                for forbidden in ("5 năm", "3 năm", "2.000.000", "3.000.000", "Other dealer", "1.5L", "1.6L"):
                    self.assertNotIn(forbidden, sent_text)

    def test_identical_duplicates_remain_answerable(self):
        facts = list(SEED_FACTS)
        for base, copy_id in (("f_warranty", "same_warranty"), ("f_reg_fee_hn", "same_fee")):
            row = next(f for f in facts if f["id"] == base).copy()
            row["id"] = copy_id
            facts.append(row)
        self.assertFalse(Facts(facts).query("warranty", now=T0)[2])
        self.assertFalse(Facts(facts).query("registration_fee", location="Hà Nội", now=T0)[2])
        for topic, text, slot_ops in (
            ("service", "warranty", [dict(op="UPDATE_SLOT", request_ref="topic:service",
                evidence_excerpt="warranty", slot=dict(kind="service_topic", value="warranty"))]),
            ("registration", "Hà Nội", [dict(op="UPDATE_SLOT", request_ref="topic:registration",
                evidence_excerpt="Hà Nội", slot=dict(kind="location", value="Hà Nội"))]),
        ):
            with self.subTest(identical_topic=topic):
                engine = make_engine(clock=Clock(T0), facts=Facts(facts))
                ref = f"topic:{topic}"
                ops = [dict(op="START_REQUEST", request_ref=ref, evidence_excerpt="request"),
                       *[dict(op=o["op"], request_ref=ref, evidence_excerpt=o["evidence_excerpt"], slot=o["slot"])
                         for o in slot_ops]]
                sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                           requests=[dict(topic=topic, desired_outcome=topic, polarity="positive")],
                           operations=ops, uncertainties=[])
                result = engine.process(event(f"same_{topic}", f"request {text}"), PC, sem)
                self.assertIn("answer_", " ".join(result["routing"]))
                self.assertEqual(len(engine.sender.sent), 1)

    def test_validator_rejects_state_changes_outside_scope(self):
        for scope in ("unknown", "out_of_scope"):
            for op in (operation(), dict(op="SET_CONTACT_PREFERENCE", scope="all", evidence_excerpt="Creta")):
                with self.subTest(scope=scope, op=op["op"]):
                    self.assertTrue(validate_semantic(semantic(op, scope=scope), "Creta", []))


class TestExtraSlots(unittest.TestCase):
    def test_vague_update_supersedes_old_testdrive_confirmation(self):
        engine = make_engine(clock=Clock(T0))
        td_semantic = lambda op: dict(schema_version="1", sentiment="NEU", sarcasm="absent",
            scope="in_scope", requests=[dict(topic="testdrive", desired_outcome="testdrive_propose",
            polarity="positive")], operations=[op], uncertainties=[])
        engine.process(event("start_confirm", "Creta Hà Nội sáng mai"), PC, td_semantic(dict(
            op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
            slot=dict(kind="vehicle", value="Creta", extra={"location": "Hà Nội", "datetime": "sáng mai"}))))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        pending_id = engine.store.open_pending_for("u", request_id)[0]["id"]
        engine.process(event("change_confirm", "cuối tháng này"), PC, td_semantic(dict(
            op="UPDATE_SLOT", request_ref=request_id, evidence_excerpt="cuối tháng này",
            slot=dict(kind="datetime", value="cuối tháng này"))))
        self.assertEqual(engine.store.get_pending(pending_id)["status"], "SUPERSEDED")

    def test_time_only_past_of_same_day_clears_old_datetime(self):
        engine = make_engine(clock=Clock(datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)))
        td_semantic = lambda op: dict(schema_version="1", sentiment="NEU", sarcasm="absent",
            scope="in_scope", requests=[dict(topic="testdrive", desired_outcome="testdrive_propose",
            polarity="positive")], operations=[op], uncertainties=[])
        engine.process(event("start_past_time", "Creta Hà Nội hôm nay lúc 18h"), PC, td_semantic(dict(
            op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
            slot=dict(kind="vehicle", value="Creta", extra={"location": "Hà Nội", "datetime": "hôm nay lúc 18h"}))))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        result = engine.process(event("change_past_time", "15h"), PC, td_semantic(dict(
            op="UPDATE_SLOT", request_ref=request_id, evidence_excerpt="15h",
            slot=dict(kind="datetime", value="15h"))))
        slots = engine.store.get_request(request_id)["slots"]
        self.assertNotIn("datetime", slots)
        self.assertEqual(slots["datetime_unresolved"]["value"], "time_only_past")
        self.assertIn("vào thời gian nào", result["response_text"])

    def test_clock_advance_invalidates_testdrive_before_location_update(self):
        engine = make_engine(clock=Clock(T0))
        td_semantic = lambda op: dict(schema_version="1", sentiment="NEU", sarcasm="absent",
            scope="in_scope", requests=[dict(topic="testdrive", desired_outcome="testdrive_propose",
            polarity="positive")], operations=[op], uncertainties=[])
        engine.process(event("start_clock", "Creta Hà Nội hôm nay lúc 18h"), PC, td_semantic(dict(
            op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
            slot=dict(kind="vehicle", value="Creta", extra={"location": "Hà Nội", "datetime": "hôm nay lúc 18h"}))))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        pending_id = engine.store.open_pending_for("u", request_id)[0]["id"]
        engine.clock.advance(hours=4)
        result = engine.process(event("location_after_time", "địa điểm Hà Nội"), PC, td_semantic(dict(
            op="UPDATE_SLOT", request_ref=request_id, evidence_excerpt="Hà Nội",
            slot=dict(kind="location", value="Hà Nội"))))
        req = engine.store.get_request(request_id)
        self.assertNotIn("datetime", req["slots"])
        self.assertEqual(engine.store.get_pending(pending_id)["status"], "SUPERSEDED")
        self.assertNotEqual(req["status"], "READY")
        self.assertIn("vào thời gian nào", result["response_text"])
        engine.process(event("stale_confirmation", "oke"), PC, td_semantic(dict(
            op="CONFIRM_PENDING", request_ref=request_id, pending_id=pending_id,
            evidence_excerpt="oke")))
        self.assertNotEqual(engine.store.get_request(request_id)["status"], "READY")

    def test_pending_validation_is_read_only_but_process_expires_elapsed_datetime(self):
        engine = make_engine(clock=Clock(T0))
        td_semantic = lambda op: dict(schema_version="1", sentiment="NEU", sarcasm="absent",
            scope="in_scope", requests=[dict(topic="testdrive", desired_outcome="testdrive_propose",
            polarity="positive")], operations=[op], uncertainties=[])
        engine.process(event("start_readonly", "Creta Hà Nội hôm nay lúc 18h"), PC, td_semantic(dict(
            op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
            slot=dict(kind="vehicle", value="Creta", extra={"location": "Hà Nội", "datetime": "hôm nay lúc 18h"}))))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        pending_id = engine.store.open_pending_for("u", request_id)[0]["id"]
        engine.clock.advance(hours=4)
        now_iso = engine.clock.now().isoformat()
        req_before, pending_before = engine.store.get_request(request_id), engine.store.get_pending(pending_id)
        op = dict(op="CONFIRM_PENDING", pending_id=pending_id)
        engine._validate_pending(op, req_before, event("direct_validate", "oke"), now_iso)
        self.assertEqual(engine.store.get_request(request_id), req_before)
        self.assertEqual(engine.store.get_pending(pending_id), pending_before)
        engine.process(event("process_confirm", "oke"), PC, td_semantic(dict(
            op="CONFIRM_PENDING", request_ref=request_id, pending_id=pending_id, evidence_excerpt="oke")))
        self.assertNotIn("datetime", engine.store.get_request(request_id)["slots"])
        self.assertEqual(engine.store.get_pending(pending_id)["status"], "SUPERSEDED")
        self.assertNotEqual(engine.store.get_request(request_id)["status"], "READY")

    def test_vague_datetime_update_clears_old_proposal(self):
        engine = make_engine(clock=Clock(T0))
        td_semantic = lambda op: dict(schema_version="1", sentiment="NEU", sarcasm="absent",
            scope="in_scope", requests=[dict(topic="testdrive", desired_outcome="testdrive_propose",
            polarity="positive")], operations=[op], uncertainties=[])
        engine.process(event("td_start", "Creta Hà Nội sáng mai"), PC, td_semantic(dict(
            op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
            slot=dict(kind="vehicle", value="Creta", extra={"location": "Hà Nội", "datetime": "sáng mai"}))))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        result = engine.process(event("td_change", "cuối tháng này"), PC, td_semantic(dict(
            op="UPDATE_SLOT", request_ref=request_id, evidence_excerpt="cuối tháng này",
            slot=dict(kind="datetime", value="cuối tháng này"))))
        slots = engine.store.get_request(request_id)["slots"]
        self.assertNotIn("datetime", slots)
        self.assertEqual(slots["datetime_unresolved"]["value"], "vague")


class TestTopicOutcomeRegressions(unittest.TestCase):
    def test_missing_stock_fact_creates_handoff_draft_without_auto_send(self):
        engine = make_engine(clock=Clock(T0), facts=Facts([]))
        sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                   requests=[dict(topic="stock", desired_outcome="stock_check", polarity="positive")],
                   operations=[dict(op="START_REQUEST", request_ref="topic:stock", evidence_excerpt="Creta",
                                    slot=dict(kind="vehicle", value="Creta"))], uncertainties=[])

        result = engine.process(event("missing_stock", "còn xe Creta không"), PC, sem)

        self.assertIn("HANDOFF_DRAFT_ONLY", result["routing"])
        self.assertFalse(result["outbound_permission"])
        self.assertEqual(result["sends"], [])
        drafts = [row for row in engine.store.ledger() if row["purpose"] == "handoff_draft"]
        self.assertTrue(drafts)
        self.assertIn("kiểm tra tồn kho thực tế", drafts[0]["payload"]["text"])

    def test_each_start_request_uses_its_topic_desired_outcome(self):
        engine = make_engine(clock=Clock(T0))
        sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                   requests=[dict(topic="price", desired_outcome="price_list", polarity="positive"),
                             dict(topic="testdrive", desired_outcome="testdrive_propose", polarity="positive")],
                   operations=[dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                                    slot=dict(kind="vehicle", value="Creta")),
                               dict(op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
                                    slot=dict(kind="vehicle", value="Creta"))], uncertainties=[])

        engine.process(event("two_topics", "giá Creta và lái thử Creta"), PC, sem)

        outcomes = {row["topic"]: row["desired_outcomes"][0]
                    for row in engine.store.active_requests("p", "u")}
        self.assertEqual(outcomes, {"price": "price_list", "testdrive": "testdrive_propose"})

    def test_repeated_same_topic_starts_map_outcomes_in_order(self):
        engine = make_engine(clock=Clock(T0))
        sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                   requests=[dict(topic="price", desired_outcome="price_list", polarity="positive"),
                             dict(topic="price", desired_outcome="price_otr", polarity="positive")],
                   operations=[dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                                    slot=dict(kind="vehicle", value="Creta")),
                               dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Tucson",
                                    slot=dict(kind="vehicle", value="Tucson"))], uncertainties=[])

        engine.process(event("same_topic_two_outcomes", "giá Creta và giá Tucson"), PC, sem)

        outcomes = {row["slots"]["vehicle"]["value"]: row["desired_outcomes"][0]
                    for row in engine.store.active_requests("p", "u")}
        self.assertEqual(outcomes, {"Creta": "price_list", "Tucson": "price_otr"})

    def test_same_vehicle_list_and_otr_intents_are_routed_and_planned_separately(self):
        engine = make_engine(clock=Clock(T0))
        sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                   requests=[dict(topic="price", desired_outcome=outcome, polarity="positive")
                             for outcome in ("price_list", "price_otr", "price_list")],
                   operations=[dict(op="START_REQUEST", request_ref="topic:price", evidence_excerpt="Creta",
                                    slot=dict(kind="vehicle", value="Creta", extra={"variant": "Cao cấp"}))
                               for _ in range(3)] + [
                               dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="niêm yết",
                                    slot=dict(kind="price_kind", value="niêm yết")),
                               dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="lăn bánh",
                                    slot=dict(kind="price_kind", value="lăn bánh")),
                               dict(op="UPDATE_SLOT", request_ref="topic:price", evidence_excerpt="Hà Nội",
                                    slot=dict(kind="location", value="Hà Nội"))],
                   uncertainties=[])

        result = engine.process(event("same_identity_outcomes", "giá niêm yết và lăn bánh Creta bản cao cấp Hà Nội"), PC, sem)

        requests = [engine.store.get_request(row[0])
                    for row in engine.store.conn.execute("SELECT id FROM requests").fetchall()]
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(requests), 2)
        by_kind = {row["slots"]["price_kind"]["value"]: row for row in requests}
        self.assertEqual(set(by_kind), {"list", "otr"})
        self.assertEqual(by_kind["list"]["desired_outcomes"], ["price_list"])
        self.assertEqual(by_kind["otr"]["desired_outcomes"], ["price_otr"])
        self.assertEqual(by_kind["otr"]["slots"]["location"]["value"], "Hà Nội")
        self.assertEqual(result["routing"].count("answer_price"), 2)
        self.assertIn("Giá niêm yết Creta", result["response_text"])
        self.assertIn("Giá lăn bánh Creta", result["response_text"])

    def test_inconsistent_start_topics_are_rejected_without_state_or_send(self):
        engine = make_engine(clock=Clock(T0))
        sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                   requests=[dict(topic="price", desired_outcome="price_list", polarity="positive")],
                   operations=[dict(op="START_REQUEST", request_ref="topic:price", topic="stock",
                                    evidence_excerpt="Creta", slot=dict(kind="vehicle", value="Creta"))],
                   uncertainties=[])

        self.assertTrue(validate_semantic(sem, "Creta", []))
        result = engine.process(event("topic_mismatch", "Creta"), PC, sem)

        self.assertIn("REVIEW_SCHEMA", result["routing"])
        self.assertEqual(engine.store.active_requests("p", "u"), [])
        self.assertEqual(engine.sender.sent, [])

    def test_extra_cannot_override_primary_identity(self):
        engine = make_engine(clock=Clock(T0))
        op = operation(slot=dict(kind="vehicle", value="Creta", extra={"vehicle": "Tucson"}))
        result = engine.process(event("duplicate", "Creta"), PC, semantic(op))
        self.assertIn("REVIEW_SCHEMA", result["routing"])
        self.assertEqual(engine.store.active_requests("p", "u"), [])
        self.assertEqual(engine.sender.sent, [])

    def test_every_extra_uses_primary_slot_normalization(self):
        engine = make_engine(clock=Clock(T0))
        op = operation(slot=dict(kind="vehicle", value="Creta", extra={
            "variant": "bản cao", "color": "màu trắng", "location": "HN",
            "price_kind": "niêm yết", "datetime": "sáng mai"}))
        result = engine.process(event("canonical", "Creta bản cao màu trắng HN niêm yết sáng mai"), PC, semantic(op))
        self.assertEqual(result["errors"], [])
        rows = engine.store.conn.execute("SELECT slots FROM requests").fetchall()
        self.assertEqual(len(rows), 1)
        slots = json.loads(rows[0][0])
        self.assertEqual({kind: slot["value"] for kind, slot in slots.items()}, {
            "vehicle": "Creta", "variant": "Cao cấp", "color": "Trắng",
            "location": "Hà Nội", "price_kind": "list", "datetime": "2026-10-25T09:00:00+07:00"})
        self.assertTrue(slots["vehicle"]["explicit"])
        self.assertTrue(slots["variant"]["explicit"])

    def test_invalid_extra_datetime_is_not_persisted_as_a_date(self):
        engine = make_engine(clock=Clock(T0))
        op = operation(slot=dict(kind="vehicle", value="Creta", extra={"datetime": "không rõ ngày"}))
        engine.process(event("invalid_date", "Creta không rõ ngày"), PC, semantic(op))
        slots = engine.store.active_requests("p", "u")[0]["slots"]
        self.assertNotIn("datetime", slots)
        self.assertEqual(slots["datetime_unresolved"]["value"], "vague")

    def test_extra_vehicle_change_keeps_the_new_primary_variant(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("initial", "Creta"), PC, semantic(operation()))
        request_id = engine.store.active_requests("p", "u")[0]["id"]
        op = operation("UPDATE_SLOT", request_ref=request_id, evidence_excerpt="Tucson bản cao",
                       slot=dict(kind="variant", value="bản cao", extra={"vehicle": "tucson"}))
        engine.process(event("change", "Tucson bản cao"), PC, semantic(op))
        slots = engine.store.get_request(request_id)["slots"]
        self.assertEqual(slots["vehicle"]["value"], "Tucson")
        self.assertIn("variant", slots)
        self.assertEqual(slots["variant"]["value"], "Cao cấp")

    def test_malformed_extra_values_remain_rejected(self):
        for extra in ({"variant": ["Cao cấp"]}, {"variant": True}, {"invented": "value"}):
            with self.subTest(extra=extra):
                op = operation(slot=dict(kind="vehicle", value="Creta", extra=extra))
                self.assertTrue(validate_semantic(semantic(op), "Creta", []))


class TestEventRegressions(unittest.TestCase):
    def test_unversioned_edit_does_not_dedupe_as_create_or_another_edit(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("c1", "original"), PC, None)
        for text in ("edited", "edited again"):
            edit = event("c1", text, event_type="edit")
            del edit["content_version"]
            result = engine.process(edit, PC, None)
            self.assertEqual(result["routing"], ["REVIEW_NO_SEMANTIC"])
            duplicate = engine.process(edit, PC, None)
            self.assertEqual(duplicate["routing"], ["DUPLICATE_IGNORED"])

    def test_unversioned_delete_does_not_dedupe_as_create(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("c1", "original"), PC, None)
        delete = event("c1", "", event_type="delete")
        del delete["content_version"]
        result = engine.process(delete, PC, None)
        self.assertEqual(result["routing"], ["RETRACTED"])
        self.assertEqual(engine.store.conn.execute("SELECT retracted FROM events").fetchall(), [(1,), (1,)])

    def test_deleted_text_is_absent_from_recent_and_parent_context(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("c1", "private withdrawn text"), PC, None)
        engine.process(event("c1", "", event_type="delete", content_version=2), PC, None)
        context = engine.context(event("next", "hello", parent_id="c1", thread_root_id="c1"), PC)
        self.assertEqual(context["recent"], [])
        self.assertEqual(context["parent_texts"], [])

    def test_retracted_text_cannot_supply_evidence_for_a_new_request(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("c1", "Creta"), PC, None)
        engine.process(event("c1", "", event_type="delete", content_version=2), PC, None)
        result = engine.process(event("next", "hello"), PC, semantic(operation()))
        self.assertIn("REVIEW_SCHEMA", result["routing"])
        self.assertEqual(engine.store.active_requests("p", "u"), [])

    def test_sent_comment_match_uses_exact_comment_id(self):
        engine = make_engine(clock=Clock(T0))
        sent = engine.process(event("c10", "hello"), PC,
                              semantic(dict(op="SOCIAL_ACK", evidence_excerpt="hello")))
        self.assertEqual(len(sent["sends"]), 1)
        engine.process(event("c1", "original"), PC, None)
        result = engine.process(event("c1", "edited", event_type="edit", content_version=2), PC, None)
        self.assertEqual(result["routing"], ["REVIEW_NO_SEMANTIC"])

    def test_delete_after_reply_still_retracts_every_stored_version(self):
        engine = make_engine(clock=Clock(T0))
        engine.process(event("c1", "hello"), PC,
                       semantic(dict(op="SOCIAL_ACK", evidence_excerpt="hello")))
        result = engine.process(event("c1", "", event_type="delete", content_version=2), PC, None)
        self.assertEqual(result["routing"], ["RETRACTED"])
        self.assertEqual(engine.store.conn.execute("SELECT retracted FROM events").fetchall(), [(1,), (1,)])
        self.assertEqual(len(engine.sender.sent), 1)


if __name__ == "__main__":
    unittest.main()
