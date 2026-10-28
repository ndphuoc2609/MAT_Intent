import unittest

from app.renderer import render, style_from_tone


class TestRendererStyle(unittest.TestCase):
    def test_default_render_is_byte_for_byte_compatible(self):
        parts = ["Giá niêm yết Creta là 700.000.000 đồng.", "Bạn muốn xem thêm không ạ?"]
        self.assertEqual(render(parts), "Giá niêm yết Creta là 700.000.000 đồng. Bạn muốn xem thêm không ạ?")
        self.assertEqual(render(parts, "tư vấn viên"), render(parts))

    def test_tone_maps_only_known_values_to_allowlisted_style(self):
        self.assertEqual(style_from_tone("ấm áp"), "warm")
        self.assertEqual(style_from_tone("professional"), "professional")
        self.assertEqual(style_from_tone("ngắn gọn"), "concise")
        self.assertEqual(style_from_tone("neutral"), "neutral")
        self.assertEqual(style_from_tone("rất thân thiện và chuyên nghiệp"), "neutral")
        self.assertEqual(style_from_tone(None), "neutral")

    def test_embedded_unambiguous_vietnamese_phrases_map_to_one_style(self):
        self.assertEqual(style_from_tone("Giọng tư vấn nhẹ nhàng, gần khách hàng."), "warm")
        self.assertEqual(style_from_tone("Cách trao đổi chuyên nghiệp với khách."), "professional")

    def test_mixed_or_unmapped_persona_descriptions_fall_back_to_neutral(self):
        self.assertEqual(style_from_tone("nhẹ nhàng nhưng chuyên nghiệp"), "neutral")
        self.assertEqual(style_from_tone("ấm áp but professional"), "neutral")
        self.assertEqual(style_from_tone("thẳng thắn, năng động, tình cảm, trẻ trung, vui vẻ"), "neutral")
        self.assertEqual(style_from_tone("nhẹ nhàng " + "x" * 240), "neutral")

    def test_negated_style_phrases_do_not_select_a_style(self):
        self.assertEqual(style_from_tone("không nhẹ nhàng"), "neutral")
        self.assertEqual(style_from_tone("không thân thiện"), "neutral")
        self.assertEqual(style_from_tone("không quá chuyên nghiệp"), "neutral")
        self.assertEqual(style_from_tone("không thân thiện nhưng chuyên nghiệp"), "professional")
        self.assertEqual(style_from_tone("không thân thiện và chuyên nghiệp"), "professional")

    def test_style_variants_apply_only_to_known_engine_prompts(self):
        prompt = "Bạn muốn xem giá niêm yết hay giá lăn bánh ạ?"
        self.assertEqual(render([prompt], style="warm"), "Bạn muốn xem giá niêm yết hay giá lăn bánh nhỉ?")
        self.assertEqual(render([prompt], style="professional"),
                         "Bạn muốn tham khảo giá niêm yết hay giá lăn bánh?")
        self.assertEqual(render([prompt], style="concise"), "Bạn muốn giá niêm yết hay giá lăn bánh?")

    def test_unknown_style_and_unlisted_fact_text_fall_back_without_rewriting(self):
        fact = "Hyundai Cầu Diễn — 123 Đường Mẫu, Hà Nội."
        self.assertEqual(render([fact], style="storyteller"), fact)
        self.assertEqual(render([fact], style=[]), fact)
        self.assertEqual(render([fact], style="warm"), fact)


if __name__ == "__main__":
    unittest.main()
