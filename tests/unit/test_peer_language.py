import unittest

from app.lead_flow import (address, address_explicit, asks_usage_duration, extract_answer, is_decline,
                           shallow_interest, states_ownership, wants_switch)
from app.persona_query import parse_persona_query


class PendingEvidenceTests(unittest.TestCase):
    def test_unrelated_usage_does_not_answer_budget_question(self):
        from app.persona_runtime import peer_plan_from_signals
        pending = [{'pending_id': 'budget-question', 'expected_slot': 'budget'}]
        signals = {'speaker_intent': 'social', 'address_term': None, 'asks_persona_question': False,
                   'claims': [{'predicate': 'usage_need', 'value': 'đi xa', 'evidence_excerpt': 'đi xa'}]}
        plan = peer_plan_from_signals(signals, 'Cuối tuần hay đi xa', {}, {}, pending)
        self.assertEqual(plan['supersede'], [])
        self.assertIn(('usage_need', 'đi xa', 'ANSWERED'), plan['lead_updates'])

    def test_only_question_matching_answer_is_marked_answered(self):
        from app.persona_runtime import peer_plan_from_signals
        pending = [{'pending_id': 'budget-question', 'expected_slot': 'budget'},
                   {'pending_id': 'usage-question', 'expected_slot': 'usage_need'}]
        signals = {'speaker_intent': 'social', 'address_term': None, 'asks_persona_question': False,
                   'claims': [{'predicate': 'usage_need', 'value': 'đi xa', 'evidence_excerpt': 'đi xa'}]}
        plan = peer_plan_from_signals(signals, 'Cuối tuần hay đi xa', {}, {}, pending)
        self.assertEqual(plan['supersede'], [(pending[1], 'ANSWERED')])


class AddressTests(unittest.TestCase):
    def test_vocative_at_end_of_answer(self):
        self.assertEqual(address('Chắc năm nay bác ạ', 'bạn'), 'bác')
        self.assertEqual(address('tháng sau anh nhé', 'bác'), 'anh')
    def test_co_is_not_co_with_accent(self):
        self.assertEqual(address('tôi có xe rồi', 'bác'), 'bác')
        self.assertEqual(address('có rồi'), 'bạn')
        self.assertEqual(address('co xe roi'), 'bạn')
        self.assertEqual(address('có xe rồi nhé', 'chú'), 'chú')

    def test_real_terms_and_unaccented_terms(self):
        self.assertEqual(address('Cô đi xe gì'), 'cô')
        self.assertEqual(address('Bac di mau gi'), 'bác')
        self.assertEqual(address('Bác chạy bao lâu rồi'), 'bác')
        self.assertEqual(address('chào anh'), 'anh')
        self.assertEqual(address('Creta giống bác'), 'bác')

    def test_relatives_are_not_the_addressee(self):
        self.assertEqual(address('anh trai tôi có xe', 'bác'), 'bác')
        self.assertIsNone(address_explicit('em tính mua cho anh trai'))

    def test_new_explicit_term_replaces_the_old_one(self):
        self.assertEqual(address('anh đi xe gì', 'bác'), 'anh')


class MeaningTests(unittest.TestCase):
    def test_implicit_first_person_purchase_interest(self):
        for text in ('cũng tính mua creta nè chị', 'tính mua nè, bạn đi thấy sao'):
            self.assertTrue(shallow_interest(text), text)
        for text in ('chị tôi cũng tính mua Creta', 'không tính mua nữa', 'tính mua cho bố tôi'):
            self.assertFalse(shallow_interest(text), text)
    def test_changing_color_or_parts_is_not_buying_a_car(self):
        for text in ('tôi muốn đổi màu xe', 'tôi muốn thay lốp xe', 'tôi muốn nâng cấp âm thanh',
                     'tôi định thay dầu xe', 'tôi muốn đổi màu xe rồi mới tính mua'):
            self.assertFalse(wants_switch(text), text)
            self.assertFalse(shallow_interest(text), text)
        for text in ('tôi muốn đổi xe', 'tôi định thay xe', 'có xe rồi nhưng muốn đổi'):
            self.assertTrue(wants_switch(text), text)

    def test_proxy_clarification_is_not_a_repeated_question(self):
        from app.lead_flow import plan_peer_turn
        plan = plan_peer_turn('Tôi chỉ hỏi hộ thôi', {}, {}, [], 'm1')
        self.assertEqual(plan['topic'], 'proxy_clarification')
    def test_buying_interest_is_first_person_only(self):
        self.assertTrue(shallow_interest('Toi cũng tính mua'))
        for text in ('em tính mua cho anh trai', 'bạn tôi tính mua', 'mình tính mua rồi',
                     'tôi không tính mua', 'tôi đặt cọc', 'mua cho bố tôi'):
            self.assertFalse(shallow_interest(text), text)

    def test_owning_a_car_is_not_wanting_one(self):
        self.assertTrue(states_ownership('tôi có xe rồi'))
        self.assertFalse(wants_switch('tôi có xe rồi'))
        self.assertTrue(states_ownership('có xe rồi nhưng muốn đổi'))
        self.assertTrue(wants_switch('có xe rồi nhưng muốn đổi'))
        for text in ('chưa có xe', 'anh tôi có Creta', 'Creta giống bác'):
            self.assertFalse(states_ownership(text), text)

    def test_usage_duration_question_is_about_nam(self):
        self.assertTrue(asks_usage_duration('Bác chạy bao lâu rồi'))
        query = parse_persona_query('Bác chạy bao lâu rồi', 'm1', 'a')
        self.assertEqual(query['canonical_keys'], ['vehicle.current.usage_started_at'])
        self.assertFalse(asks_usage_duration('tôi chạy bao lâu rồi'))

    def test_answers_are_field_bound(self):
        self.assertEqual(extract_answer('interested_vehicle', 'Creta'), 'Creta')
        self.assertIsNone(extract_answer('interested_vehicle', 'Creta giống bác'))
        self.assertIsNone(extract_answer('budget', '5'))
        self.assertEqual(extract_answer('budget', '800 triệu'), '800 triệu')
        self.assertIsNone(extract_answer('usage_need', 'đi làm hàng ngày'))

    def test_decline_words(self):
        for text in ('thôi', 'thôi nhé', 'đừng hỏi', 'không cần đâu'):
            self.assertTrue(is_decline(text), text)
        self.assertFalse(is_decline('bác dùng thời gian bao lâu'))


if __name__ == '__main__':
    unittest.main()
