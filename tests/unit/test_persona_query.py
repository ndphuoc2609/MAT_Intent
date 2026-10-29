import unittest

from app.persona_query import KEY_REGISTRY, parse_persona_query


class TestPersonaQuery(unittest.TestCase):
    def query(self, text, **kwargs):
        return parse_persona_query(text, "message-1", "actor-1", **kwargs)

    def test_age_question_uses_registered_identity_key_and_exact_current_evidence(self):
        result = self.query("Bạn bao nhiêu tuổi rồi?")
        self.assertEqual(result, {
            "schema_version": "persona_query_v1", "topic": "identity",
            "canonical_keys": ["identity.age_years"],
            "evidence_message_id": "message-1", "evidence_actor_id": "actor-1",
            "evidence_excerpt": "Bạn bao nhiêu tuổi rồi?", "needs_clarification": False,
        })

    def test_residence_question_only_selects_location_keys(self):
        result = self.query("Bạn đang sống ở quận nào?")
        self.assertEqual(result["topic"], "residence")
        self.assertEqual(result["canonical_keys"], ["location.base.district"])

    def test_current_and_past_vehicle_questions_are_distinct(self):
        current = self.query("Hiện bạn đi xe gì?")
        past = self.query("Trước đây bạn từng đi xe gì?")
        self.assertEqual((current["topic"], current["canonical_keys"]),
                         ("vehicle_current", ["vehicle.current.model"]))
        self.assertEqual((past["topic"], past["canonical_keys"]),
                         ("vehicle_past", ["vehicle.history.model"]))

    def test_purchase_question_is_not_a_dealer_fact_or_product_slot(self):
        result = self.query("Bạn mua xe ở đại lý nào?")
        self.assertEqual(result["topic"], "purchase_finance")
        self.assertEqual(result["canonical_keys"], ["purchase.dealer"])

    def test_customer_question_and_product_question_do_not_become_persona_queries(self):
        for text in ("Tôi mua Creta ở đại lý nào?", "Creta có mấy túi khí?"):
            with self.subTest(text=text):
                self.assertEqual(self.query(text)["topic"], "none")
                self.assertEqual(self.query(text)["canonical_keys"], [])

    def test_negative_or_corrected_question_does_not_reveal_persona_fact(self):
        for text in ("Bạn không ở Hà Nội à?", "Không hỏi tuổi bạn nữa"):
            with self.subTest(text=text):
                self.assertNotEqual(self.query(text)["topic"], "identity")

    def test_unknown_text_has_no_keys_and_no_invented_evidence(self):
        result = self.query("Bạn kể thêm đi")
        self.assertEqual(result["topic"], "none")
        self.assertEqual(result["canonical_keys"], [])
        self.assertIsNone(result["evidence_excerpt"])

    def test_semantic_and_policy_gates_prevent_parser_bypass(self):
        for kwargs in ({"semantic": {"scope": "unknown", "requests": []}},
                       {"policy_blocked": True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.query("Bạn bao nhiêu tuổi?", **kwargs)["topic"], "none")

    def test_in_scope_social_ack_allows_persona_query_without_request_labels(self):
        semantic = {"scope": "in_scope", "operations": [{"op": "SOCIAL_ACK"}]}
        result = self.query("Bạn bao nhiêu tuổi?", semantic=semantic)
        self.assertEqual(result["topic"], "identity")

    def test_other_operations_and_out_of_scope_still_block_persona_query(self):
        for semantic in (
            {"scope": "in_scope", "operations": [{"op": "START_REQUEST"}]},
            {"scope": "out_of_scope", "operations": [{"op": "SOCIAL_ACK"}]},
        ):
            with self.subTest(semantic=semantic):
                self.assertEqual(self.query("Bạn bao nhiêu tuổi?", semantic=semantic)["topic"], "none")

    def test_injected_parser_is_strict_and_cannot_choose_unknown_keys_or_fake_evidence(self):
        def parser(_text):
            return {"schema_version": "persona_query_v1", "topic": "identity",
                    "canonical_keys": ["identity.secret"], "evidence_excerpt": "Tuổi của bạn?",
                    "needs_clarification": False}

        result = self.query("Bạn muốn kể thêm điều gì?", parser=parser)
        self.assertEqual(result["topic"], "unknown")
        self.assertEqual(result["canonical_keys"], [])
        self.assertTrue(result["needs_clarification"])

    def test_injected_parser_evidence_is_bound_to_the_current_actor_and_message(self):
        def parser(_text):
            return {"schema_version": "persona_query_v1", "topic": "identity",
                    "canonical_keys": ["identity.age_years"],
                    "evidence_excerpt": "Tuổi của bạn là bao nhiêu?",
                    "needs_clarification": False}

        result = self.query("Tuổi của bạn là bao nhiêu?", parser=parser)
        self.assertEqual(result["evidence_message_id"], "message-1")
        self.assertEqual(result["evidence_actor_id"], "actor-1")
        self.assertEqual(result["evidence_excerpt"], "Tuổi của bạn là bao nhiêu?")

    def test_injected_parser_exception_fails_to_unknown_without_fabricating_evidence(self):
        def parser(_text):
            raise TimeoutError("provider timeout")

        result = self.query("Bạn muốn kể thêm điều gì?", parser=parser)
        self.assertEqual(result["topic"], "unknown")
        self.assertIsNone(result["evidence_excerpt"])
        self.assertTrue(result["needs_clarification"])

    def test_registry_is_closed_and_covers_all_persona_topics(self):
        expected = {"identity", "family", "occupation", "residence", "vehicle_current",
                    "vehicle_past", "vehicle_experience", "purchase_finance", "maintenance",
                    "travel_story", "preference", "opinion", "location_compare", "none", "unknown"}
        self.assertEqual(set(KEY_REGISTRY), expected)
        self.assertTrue(all(isinstance(keys, frozenset) for keys in KEY_REGISTRY.values()))


if __name__ == "__main__":
    unittest.main()
