import unittest

from app.adapters.luna import SYSTEM
from app.semantics import validate_semantic


def semantic(uncertainties_marker=...):
    value = {
        "schema_version": "1",
        "sentiment": "NEU",
        "sarcasm": "absent",
        "scope": "in_scope",
        "requests": [],
        "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": "oke"}],
    }
    if uncertainties_marker is not ...:
        value["uncertainties"] = uncertainties_marker
    return value


class TestUncertaintyValidation(unittest.TestCase):
    def test_valid_and_omitted_uncertainties_are_accepted(self):
        valid = semantic([{"alternatives": ["price", "buy"], "different_action": True}])
        self.assertEqual(validate_semantic(valid, "oke", []), [])
        self.assertEqual(validate_semantic(semantic(), "oke", []), [])

    def test_uncertainties_must_be_a_list(self):
        self.assertTrue(validate_semantic(semantic("bad"), "oke", []))

    def test_uncertainty_entry_must_be_object_with_known_fields(self):
        for value in ("bad", {"alternatives": ["a", "b"], "different_action": True,
                               "confidence": 0.5}):
            with self.subTest(value=value):
                self.assertTrue(validate_semantic(semantic([value]), "oke", []))

    def test_different_action_must_be_boolean(self):
        for value in ("false", 0, 1, None):
            with self.subTest(value=value):
                uncertainty = {"alternatives": ["price", "buy"], "different_action": value}
                self.assertTrue(validate_semantic(semantic([uncertainty]), "oke", []))

    def test_prompt_flags_only_different_engine_effects_as_ambiguous(self):
        prompt = SYSTEM.lower()
        self.assertIn("different_action=true chỉ khi", prompt)
        self.assertIn("cùng dẫn tới một trạng thái/hiệu ứng", prompt)
        self.assertIn("different_action=false", prompt)

    def test_alternatives_must_contain_two_nonempty_strings(self):
        for alternatives in (None, "price, buy", [], ["price"], ["price", 2], ["", "buy"]):
            with self.subTest(alternatives=alternatives):
                uncertainty = {"alternatives": alternatives, "different_action": True}
                self.assertTrue(validate_semantic(semantic([uncertainty]), "oke", []))

    @staticmethod
    def slot_semantic(text, value, excerpt, kind="vehicle"):
        return {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope", "requests": [{"topic": "price", "desired_outcome": "price_otr",
                "polarity": "positive"}], "uncertainties": [],
            "operations": [{"op": "START_REQUEST", "request_ref": "topic:price",
                "evidence_excerpt": "Giá", "slot": {"kind": kind, "value": value,
                    "raw": excerpt, "explicit": True, "evidence_excerpt": excerpt}}],
        }

    def test_explicit_slot_value_and_its_evidence_must_be_in_current_comment(self):
        forged = self.slot_semantic("Giá lăn bánh Creta bao nhiêu?", "Tucson", "Tucson")
        self.assertTrue(validate_semantic(forged, "Giá lăn bánh Creta bao nhiêu?", []))

        fabricated_excerpt = self.slot_semantic("Giá lăn bánh Creta bao nhiêu?", "Creta", "Tucson")
        self.assertTrue(validate_semantic(fabricated_excerpt, "Giá lăn bánh Creta bao nhiêu?", []))

    def test_slot_alias_is_supported_but_negated_value_is_not(self):
        alias = self.slot_semantic("Giá xe bản cao nhé", "Cao cấp", "bản cao", "variant")
        self.assertEqual(validate_semantic(alias, "Giá xe bản cao nhé", []), [])
        negated = self.slot_semantic("Không lấy Tucson, cho hỏi giá Creta", "Tucson", "Tucson")
        self.assertTrue(validate_semantic(negated, "Không lấy Tucson, cho hỏi giá Creta", []))


if __name__ == "__main__":
    unittest.main()
