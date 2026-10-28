import unittest

from app.engine import Clock
from app.harness import make_engine
from app.semantics import attach_context_provenance, validate_semantic
from app.semantics import normalize_model_semantic, semantic_model_json_schema
from scripts.cache_key import state_fingerprint
from scripts.run_contract import resolve


PC = {"topic": "Creta", "vehicles": ["Creta"], "persona_default": "tư vấn viên"}


def semantic(operations, topic="price", version="1"):
    return {
        "schema_version": version, "sentiment": "NEU", "sarcasm": "absent",
        "scope": "in_scope",
        "requests": [{"topic": topic, "desired_outcome": topic, "polarity": "positive"}],
        "operations": operations, "uncertainties": [],
    }


def event(cid, text, *, actor="u1", root="root", parent=None):
    return {"page_id": "p", "post_id": "post", "thread_root_id": root,
            "comment_id": cid, "parent_id": parent or root, "actor_id": actor,
            "actor_role": "user", "event_type": "create", "content_version": 1,
            "text": text, "event_dedupe_key": f"{cid}:1"}


def slot_op(kind, value, evidence, op="START_REQUEST", ref="topic:price", **fields):
    return {"op": op, "request_ref": ref, "evidence_excerpt": evidence,
            "slot": {"kind": kind, "value": value, **fields}}


class TestSlotEvidence(unittest.TestCase):
    def test_strict_provider_schema_uses_required_nullable_fields(self):
        schema = semantic_model_json_schema("1")
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertEqual(set(node.get("required", [])), set(node.get("properties", {})))
                    self.assertIs(node.get("additionalProperties"), False)
                self.assertFalse(set(node) & {"oneOf", "allOf", "if", "then", "else"})
                for value in node.values(): check(value)
            elif isinstance(node, list):
                for value in node: check(value)
        check(schema)
        shaped = normalize_model_semantic({"schema_version": "1", "operations": [{
            "op": "SOCIAL_ACK", "evidence_excerpt": "oke", "request_ref": None}]})
        self.assertEqual(shaped["operations"], [{"op": "SOCIAL_ACK", "evidence_excerpt": "oke"}])

    def test_provider_slot_without_server_owned_provenance_normalizes(self):
        value = {"requests": [{"topic": "price", "desired_outcome": None}], "operations": [{
            "op": "START_REQUEST", "request": {"topic": "price", "desired_outcome": None},
            "slot": {"kind": "vehicle", "value": "Creta", "raw": None,
                     "evidence_excerpt": "Creta", "explicit": True, "confirmed": None, "extra": None}}]}
        shaped = normalize_model_semantic(value)
        self.assertEqual(shaped["requests"], [{"topic": "price"}])
        self.assertEqual(shaped["operations"][0]["request"], {"topic": "price"})
        self.assertEqual(shaped["operations"][0]["slot"], {
            "kind": "vehicle", "value": "Creta", "evidence_excerpt": "Creta", "explicit": True})
        minimal = normalize_model_semantic({"requests": [{"topic": "price"}], "operations": [{
            "op": "START_REQUEST", "request": {"topic": "price"},
            "slot": {"kind": "vehicle", "value": "Creta"}}]})
        self.assertEqual(minimal["operations"][0]["slot"], {"kind": "vehicle", "value": "Creta"})

    def test_post_vehicle_context_is_inferred_with_post_provenance(self):
        engine = make_engine()
        sem = semantic([{"op": "START_REQUEST", "request_ref": "topic:price",
                         "evidence_excerpt": "Giá", "slot": {
                             "kind": "vehicle", "value": "Creta", "explicit": False,
                             "evidence_excerpt": "Creta"}}])
        result = engine.process(event("postcar", "Giá thế nào?"), PC, sem)
        self.assertFalse(any(route.startswith("REVIEW") for route in result["routing"]), result)
        slot = next(iter(result["state_after"]["requests"].values()))["slots"]["vehicle"]
        self.assertEqual((slot["value"], slot["source_message_id"], slot["source_actor_id"], slot["explicit"]),
                         ("Creta", "post", "p", False))

    def test_rejects_vehicle_not_entailed_by_excerpt(self):
        sem = semantic([slot_op("vehicle", "Tucson", "giá")])
        errors = validate_semantic(sem, "Cho mình hỏi giá Creta", [])
        self.assertTrue(any("slot evidence" in error for error in errors), errors)

    def test_raw_slot_text_cannot_fake_evidence_for_another_vehicle(self):
        sem = semantic([slot_op("vehicle", "Tucson", "giá", raw="Creta")])
        self.assertTrue(validate_semantic(sem, "Cho mình hỏi giá Creta", []))

    def test_accepts_controlled_variant_alias(self):
        sem = semantic([slot_op("variant", "Cao cấp", "bản cao")])
        self.assertEqual(validate_semantic(sem, "Creta bản cao giá?", []), [])

    def test_rejects_negated_vehicle_but_accepts_replacement(self):
        text = "Không phải Tucson, Creta"
        rejected = semantic([slot_op("vehicle", "Tucson", "Tucson")])
        accepted = semantic([slot_op("vehicle", "Creta", "Creta")])
        self.assertTrue(validate_semantic(rejected, text, []))
        self.assertEqual(validate_semantic(accepted, text, []), [])

    def test_canonical_location_and_amount_need_their_own_evidence(self):
        location = semantic([slot_op("location", "Hà Nội", "HN")])
        amount = semantic([slot_op("down_payment", {"amount": 200, "unit": "triệu"}, "200 triệu")])
        wrong_amount = semantic([slot_op("down_payment", {"amount": 300, "unit": "triệu"}, "200 triệu")])
        self.assertEqual(validate_semantic(location, "Tucson lăn bánh HN", []), [])
        self.assertEqual(validate_semantic(amount, "Đặt cọc 200 triệu", []), [])
        self.assertTrue(validate_semantic(wrong_amount, "Đặt cọc 200 triệu", []))

    def test_negated_amount_is_rejected_but_corrected_amount_is_supported(self):
        text = "Không phải 50 triệu, lấy 100 triệu"
        rejected = semantic([slot_op("down_payment", {"amount": 50, "unit": "triệu"},
                                     "50 triệu", ref="topic:loan")], topic="loan")
        accepted = semantic([slot_op("down_payment", {"amount": 100, "unit": "triệu"},
                                     "100 triệu", ref="topic:loan")], topic="loan")
        self.assertTrue(validate_semantic(rejected, text, []))
        self.assertEqual(validate_semantic(accepted, text, []), [])

    def test_accepts_datetime_quoted_from_current_message(self):
        sem = semantic([slot_op("datetime", "sáng mai", "sáng mai", ref="topic:testdrive")], topic="testdrive")
        self.assertEqual(validate_semantic(sem, "Lái thử sáng mai", []), [])

    def test_inherited_slot_keeps_same_actor_source_even_if_post_lists_many_cars(self):
        sem = semantic([slot_op("vehicle", "Creta", "Creta", explicit=False)])
        ctx = {"state_token": "state-a", "recent": [{"message_id": "prior", "actor_id": "u1",
                "role": "recent", "text": "Giá Creta"}], "messages": []}
        enriched = attach_context_provenance(sem, event("current", "giá bao nhiêu"), ctx,
                                               {"vehicles": ["Creta", "Tucson"], "text": "Xe Hyundai"})
        slot = enriched["operations"][0]["slot"]
        self.assertFalse(slot["explicit"])
        self.assertEqual((slot["source_message_id"], slot["source_actor_id"], slot["source_state_token"]),
                         ("prior", "u1", "state-a"))

    def test_v2_inherited_provenance_is_attached_by_server_not_model(self):
        sem = semantic([slot_op("vehicle", "Creta", "Creta", explicit=False)], version="2")
        sem.update(intent_labels=["price"], category="sales", tags=[])
        sem["operations"][0]["slot"].update(source_message_id="prior", source_actor_id="attacker",
                                              source_state_token="model-invented")
        ctx = {"state_token": "server-snapshot", "recent": [{"message_id": "prior", "actor_id": "u1",
                "role": "recent", "text": "Giá Creta"}], "messages": []}
        enriched = attach_context_provenance(sem, event("current", "giá bao nhiêu"), ctx,
                                               {"vehicles": ["Creta"], "text": "Xe Hyundai"})
        slot = enriched["operations"][0]["slot"]
        self.assertEqual(slot["source_message_id"], "prior")
        self.assertEqual(slot["source_actor_id"], "u1")
        self.assertEqual(slot["source_state_token"], "server-snapshot")

    def test_operation_source_id_is_server_mapped_from_current_evidence(self):
        sem = semantic([{"op": "START_REQUEST", "request_ref": "topic:spec",
                         "source_message_id": "model-guessed-id", "evidence_excerpt": "Xe như nào"}],
                       topic="spec")
        enriched = attach_context_provenance(sem, event("current-1", "Xe như nào"),
            {"messages": [{"message_id": "current-1", "actor_id": "u1", "role": "current",
                           "text": "Xe như nào"}]}, PC)
        self.assertEqual(enriched["operations"][0]["source_message_id"], "current-1")

    def test_operation_source_id_maps_to_unique_parent_evidence(self):
        sem = semantic([{"op": "START_REQUEST", "request_ref": "topic:spec",
                         "source_message_id": "model-guessed-id", "evidence_excerpt": "Thông tin gốc"}],
                       topic="spec")
        context = {"messages": [
            {"message_id": "parent-1", "actor_id": "u2", "role": "parent", "text": "Thông tin gốc"},
            {"message_id": "current-1", "actor_id": "u1", "role": "current", "text": "đúng vậy"}]}
        enriched = attach_context_provenance(sem, event("current-1", "đúng vậy", parent="parent-1"),
                                               context, PC)
        self.assertEqual(enriched["operations"][0]["source_message_id"], "parent-1")

    def test_ambiguous_context_excerpt_does_not_trust_model_source_id(self):
        engine = make_engine()
        postcard = {"vehicles": [], "text": "Bài viết"}
        root = event("root", "cụm trích dẫn", actor="root-author", root="root")
        parent = event("parent", "cụm trích dẫn", actor="parent-author", root="root", parent="root")
        current = event("current", "đúng như trên", actor="u1", root="root", parent="parent")
        engine.process(root, postcard, None)
        engine.process(parent, postcard, None)
        sem = semantic([{"op": "START_REQUEST", "request_ref": "topic:spec",
                         "source_message_id": "model-guessed-id", "evidence_excerpt": "cụm trích dẫn"}],
                       topic="spec")
        result = engine.process(current, postcard, sem)
        self.assertIn("REVIEW_PROVENANCE", result["routing"])
        self.assertEqual(result["state_changes"], [])

    def test_explicit_slot_source_must_be_the_current_message(self):
        engine = make_engine()
        postcard = {"vehicles": ["Creta"], "text": "Hyundai Creta"}
        prior = event("prior", "Creta", root="root")
        engine.process(prior, postcard, None)
        current = event("current", "Creta", root="root")
        op = slot_op("vehicle", "Creta", "Creta", explicit=True)
        op["source_message_id"] = "prior"
        op["slot"]["source_message_id"] = "prior"
        result = engine.process(current, postcard, semantic([op]))
        self.assertIn("REVIEW_PROVENANCE", result["routing"])
        self.assertEqual(result["state_changes"], [])

    def test_color_and_down_payment_unit_are_grounded_and_negation_is_respected(self):
        color = semantic([slot_op("color", "Trắng", "Trắng", ref="topic:stock")], topic="stock")
        self.assertEqual(validate_semantic(color, "Còn màu trắng không?", []), [])
        wrong_color = semantic([slot_op("color", "Trắng", "đen", ref="topic:stock")], topic="stock")
        self.assertTrue(validate_semantic(wrong_color, "Không lấy màu trắng, lấy màu đen", []))
        unit = semantic([{"op": "ANSWER_PENDING", "request_ref": "topic:loan", "pending_id": "q_1",
                          "evidence_excerpt": "triệu", "slot": {"kind": "down_payment_unit",
                          "value": "triệu"}}], topic="loan")
        self.assertEqual(validate_semantic(unit, "triệu", []), [])

    def test_bad_slot_shape_and_unknown_top_level_field_return_validation_errors(self):
        bad_slot = semantic([{"op": "START_REQUEST", "request_ref": "topic:price",
                              "evidence_excerpt": "giá", "slot": "Tucson"}])
        extra_field = semantic([{"op": "SOCIAL_ACK", "evidence_excerpt": "oke"}])
        extra_field["model_guess"] = "Tucson"
        self.assertTrue(validate_semantic(bad_slot, "giá Creta", []))
        self.assertTrue(validate_semantic(extra_field, "oke", []))

    def test_unhashable_slot_kind_is_a_schema_error_not_an_exception(self):
        malformed = semantic([{"op": "START_REQUEST", "request_ref": "topic:price",
                               "evidence_excerpt": "giá", "slot": {"kind": [], "value": "Creta"}}])
        self.assertTrue(validate_semantic(malformed, "giá Creta", []))

    def test_v2_multilabel_contract_requires_labels_category_and_tags(self):
        sem = semantic([{"op": "SOCIAL_ACK", "evidence_excerpt": "cảm ơn"}], version="2")
        sem["requests"].append({"topic": "social", "desired_outcome": "ack", "polarity": "positive"})
        sem.update(intent_labels=["price", "social"], category=None, tags=["multi_intent"])
        self.assertEqual(validate_semantic(sem, "cảm ơn hỏi giá", []), [])
        del sem["tags"]
        self.assertTrue(any("requires tags" in error for error in
                            validate_semantic(sem, "cảm ơn hỏi giá", [])))


class TestThreadActorIsolation(unittest.TestCase):
    def test_contract_placeholders_resolve_only_inside_current_thread_scope(self):
        engine = make_engine()
        for rid, actor, root in (
            ("a-u1", "u1", "thread-a"), ("a-u2", "u2", "thread-a"),
            ("b-u1", "u1", "thread-b"), ("b-u2", "u2", "thread-b"),
        ):
            engine.store.insert_request({
                "id": rid, "page_id": "p", "actor_id": actor, "post_id": "post",
                "thread_root_id": root, "topic": "price", "status": "OPEN", "version": 1,
                "slots": {}, "source_events": [], "desired_outcomes": [],
            })
        for pid, rid, actor in (("pending-a", "a-u1", "u1"), ("pending-b", "b-u1", "u1")):
            engine.store.insert_pending({
                "id": pid, "request_id": rid, "actor_id": actor, "qtype": "yes_no",
                "expected_slot": None, "options": [], "proposition": "proposal",
                "issued_at": "", "expires_at": "", "version": 1, "status": "OPEN",
            })

        resolved = resolve({"pending_id": "$PENDING", "other": "$OTHER_REQUEST"},
                           engine, event("current", "oke", root="thread-b"))
        self.assertEqual(resolved, {"pending_id": "pending-b", "other": "b-u2"})

    def test_reusing_event_id_with_different_payload_is_conflict(self):
        engine = make_engine()
        original = event("same-id", "hello")
        engine.process(original, PC, None)
        changed = {**original, "text": "different content"}
        result = engine.process(changed, PC, None)
        self.assertIn("EVENT_ID_CONFLICT", result["routing"])

    def test_same_actor_followup_in_another_thread_does_not_update_old_request(self):
        engine = make_engine()
        engine.process(event("a1", "Giá Creta", root="thread-a"), PC,
                       semantic([slot_op("vehicle", "Creta", "Creta")]))
        result = engine.process(event("b1", "Bản cao nhé", root="thread-b"), PC,
                                semantic([slot_op("variant", "Cao cấp", "Bản cao",
                                                  op="UPDATE_SLOT")]))
        requests = engine.store.conn.execute("SELECT slots FROM requests").fetchall()
        self.assertEqual(len(requests), 1)
        self.assertNotIn("variant", __import__("json").loads(requests[0][0]))
        self.assertTrue(any(route.startswith("REVIEW") for route in result["routing"]), result)

    def test_other_actor_cannot_confirm_pending_in_same_thread(self):
        engine = make_engine(clock=Clock(__import__("datetime").datetime(
            2026, 10, 24, 9, tzinfo=__import__("datetime").timezone.utc)))
        text = "Lái thử Creta Hà Nội sáng mai"
        engine.process(event("a1", text, actor="A"), PC, semantic([
            {"op": "START_REQUEST", "request_ref": "topic:testdrive", "evidence_excerpt": "Creta",
             "slot": {"kind": "vehicle", "value": "Creta"}},
            {"op": "UPDATE_SLOT", "request_ref": "topic:testdrive", "evidence_excerpt": "Hà Nội",
             "slot": {"kind": "location", "value": "Hà Nội"}},
            {"op": "UPDATE_SLOT", "request_ref": "topic:testdrive", "evidence_excerpt": "sáng mai",
             "slot": {"kind": "datetime", "value": "sáng mai"}},
        ], topic="testdrive"))
        request = engine.store.conn.execute("SELECT id FROM requests WHERE actor_id='A'").fetchone()[0]
        pending = engine.store.open_pending_for("A", request)[0]
        result = engine.process(event("b1", "oke", actor="B"), PC, semantic([
            {"op": "CONFIRM_PENDING", "request_ref": request, "pending_id": pending["id"],
             "evidence_excerpt": "oke"},
        ], topic="testdrive"))
        self.assertIn("REVIEW_OWNER", result["routing"])
        self.assertEqual(engine.store.get_pending(pending["id"])["status"], "OPEN")

    def test_state_change_during_inference_rejects_stale_semantic(self):
        engine = make_engine()
        first = event("a1", "Giá Creta", root="thread-a")
        engine.process(first, PC, semantic([slot_op("vehicle", "Creta", "Creta")]))
        current = event("a2", "Bản cao", root="thread-a")
        token = engine.context(current, PC)["state_token"]
        engine.process(current, PC, semantic([slot_op("variant", "Cao cấp", "Bản cao", op="UPDATE_SLOT")]))
        stale = event("a3", "Bản tiêu chuẩn", root="thread-a")
        result = engine.process(stale, PC, semantic([slot_op("variant", "Tiêu chuẩn", "Bản tiêu chuẩn",
                                                              op="UPDATE_SLOT")]),
                                expected_state_token=token)
        self.assertIn("REVIEW_STALE_CONTEXT", result["routing"])
        request = engine.store.conn.execute("SELECT slots FROM requests").fetchone()[0]
        self.assertEqual(__import__("json").loads(request)["variant"]["value"], "Cao cấp")
        fresh_token = engine.context(stale, PC)["state_token"]
        retried = engine.process(stale, PC, semantic([slot_op("variant", "Tiêu chuẩn",
            "Bản tiêu chuẩn", op="UPDATE_SLOT")]), expected_state_token=fresh_token)
        self.assertNotIn("DUPLICATE_IGNORED", retried["routing"])
        request = engine.store.conn.execute("SELECT slots FROM requests").fetchone()[0]
        self.assertEqual(__import__("json").loads(request)["variant"]["value"], "Tiêu chuẩn")


class TestCacheStateCompleteness(unittest.TestCase):
    def test_fingerprint_changes_for_non_identity_slot_and_outcome(self):
        base = [{"id": "one", "topic": "testdrive", "status": "OPEN",
                 "slots": {"vehicle": {"value": "Creta"}, "location": {"value": "HN"}},
                 "desired_outcomes": ["testdrive_propose"]}]
        changed_location = [{**base[0], "slots": {**base[0]["slots"], "location": {"value": "TP.HCM"}}}]
        changed_outcome = [{**base[0], "desired_outcomes": ["testdrive_cancel"]}]
        self.assertNotEqual(state_fingerprint(base, [], []), state_fingerprint(changed_location, [], []))
        self.assertNotEqual(state_fingerprint(base, [], []), state_fingerprint(changed_outcome, [], []))

    def test_color_datetime_and_pending_proposition_change_cache_fingerprint(self):
        base_req = {"id": "r1", "topic": "testdrive", "status": "WAITING_USER", "version": 2,
                    "slots": {"color": {"value": "Trắng"}, "datetime": {"value": "2026-11-01T09:00"}},
                    "desired_outcomes": ["testdrive_propose"]}
        pending = {"id": "p1", "request_id": "r1", "actor_id": "u1", "status": "OPEN",
                   "qtype": "yes_no", "proposition": "Đề xuất lái thử Trắng lúc 9 giờ?",
                   "options": ["yes", "no"], "basis_version": 2, "basis_slots": {"color": "Trắng"}}
        baseline = state_fingerprint([base_req], [pending], ["lái thử"])
        for changed in (
            {**base_req, "slots": {**base_req["slots"], "color": {"value": "Đen"}}},
            {**base_req, "slots": {**base_req["slots"], "datetime": {"value": "2026-11-01T15:00"}}},
        ):
            self.assertNotEqual(baseline, state_fingerprint([changed], [pending], ["lái thử"]))
        changed_pending = {**pending, "proposition": "Đề xuất lái thử Trắng lúc 15 giờ?"}
        self.assertNotEqual(baseline, state_fingerprint([base_req], [changed_pending], ["lái thử"]))

    def test_state_change_result_contains_only_persisted_changes_and_structured_semantic(self):
        engine = make_engine()
        result = engine.process(event("state1", "Giá Creta"), PC,
                                semantic([slot_op("vehicle", "Creta", "Creta")]))
        self.assertEqual(result["intent_labels"], ["price"])
        self.assertEqual(result["semantic_version"], "1")
        self.assertTrue(any(change["kind"] == "request_created" for change in result["state_changes"]))
        self.assertTrue(result["state_after"]["requests"])

    def test_model_context_does_not_expose_server_state_token(self):
        from scripts.run_multiturn_eval import fmt_context
        serialized = fmt_context({"state_token": "server-secret-token", "messages": [
            {"message_id": "m1", "actor_id": "u1", "role": "current", "text": "giá Creta"}]})
        self.assertNotIn("server-secret-token", serialized)
        self.assertNotIn('"state_token"', serialized)


if __name__ == "__main__":
    unittest.main()
