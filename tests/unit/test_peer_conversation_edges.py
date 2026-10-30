import unittest

from tests.unit.test_peer_conversation import ConversationBase
from tests.unit.test_persona_engine import _social_semantic


class EdgeCaseTests(ConversationBase):
    def test_user_transcript_shares_story_then_stops_sales_questions(self):
        self.turn('Bạn đi xe gì đi làm')
        self.turn('tôi cũng tính mua')
        _, result = self.turn('Creta luôn á')
        self.assertIn('Cầu Giấy', result['response_text'] or '')
        self.assertIn('Nam Định', result['response_text'] or '')
        _, result = self.turn('Chắc năm nay bác ạ')
        self.assertIn('bác', result['response_text'] or '')
        self.assertNotIn('?', result['response_text'] or '')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.profile()['purchase_timeline']['value'], 'Chắc năm nay bác ạ')
        _, result = self.turn('Bạn nhắn như AI á')
        self.assertTrue(result['outbound_permission'], result)
        self.assertIn('mô phỏng', result['response_text'] or '')
        self.assertEqual(self.pending(), [])
        _, result = self.turn('bác đánh gía xe đang đi như nào')
        self.assertTrue(result['outbound_permission'], result)
        self.assertIn('tiếng ồn', result['response_text'] or '')
        self.assertTrue(result['response_plan']['fact_ids'])

    def test_color_change_does_not_start_purchase_funnel(self):
        _, result = self.turn('tôi muốn đổi màu xe')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.profile(), {})
        self.assertNotEqual(self.state().get('purchase_intent'), 'exploring')
        self.assertNotIn('mẫu xe nào', result['response_text'] or '')

    def test_explicit_social_reactions_do_not_start_leads(self):
        for text in ('Xe đẹp quá!', 'haha', 'Tag bạn @Lan xem nè', 'Không mua nữa đâu',
                     'Xe đẹp nhưng chưa có nhu cầu', 'Để sau mua'):
            _, result = self.turn(text)
            self.assertTrue(result['outbound_permission'], result)
            self.assertEqual(self.pending(), [])
            self.assertEqual(self.profile(), {})
            self.assertNotIn('?', result['response_text'] or '')

    def test_live_correction_unresolved_retracts_only_this_thread_lead(self):
        self.turn('Mình cũng tính mua Creta')
        text = 'Không phải tôi mua, tôi hỏi hộ anh tôi'
        sem = dict(_social_semantic(text), requests=[], operations=[dict(op='UNRESOLVED', reason='clarification', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'DECLINED')
        self.assertEqual(self.profile()['interested_vehicle']['evidence'], text)
        self.assertEqual(self.profile()['purchase_timeline']['status'], 'DECLINED')
        self.assertNotEqual(self.state().get('purchase_intent'), 'exploring')
        self.assertNotIn('?', result['response_text'] or '')

    def test_correction_never_bypasses_sarcasm_or_unrelated_evidence(self):
        self.turn('Mình cũng tính mua Creta')
        text = 'Không phải tôi mua, tôi hỏi hộ anh tôi'
        sem = dict(_social_semantic(text), sarcasm='uncertain', requests=[],
                   operations=[dict(op='UNRESOLVED', reason='clarification', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertIn('REVIEW_SARCASM', result['routing'])
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'ANSWERED')
        self.assertEqual(self.pending(), ['purchase_timeline'])
        sem['sarcasm'] = 'absent'
        sem['operations'][0]['evidence_excerpt'] = 'not present'
        _, result = self.turn(text, semantic=sem)
        self.assertIn('REVIEW_SCHEMA', result['routing'])
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'ANSWERED')

    def test_live_cancel_decline_records_evidence(self):
        self.turn('Tôi cũng tính mua xe')
        text = 'không cần đâu, đừng hỏi nữa'
        sem = dict(_social_semantic(text), operations=[dict(op='CANCEL_REQUEST', request_ref='topic:social', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'DECLINED')
        self.assertEqual(self.profile()['interested_vehicle']['evidence'], text)
        self.assertNotIn('?', result['response_text'] or '')

    def test_live_unresolved_decline_closes_pending(self):
        self.turn('Tôi cũng tính mua xe')
        text = 'đừng hỏi nữa'
        sem = dict(_social_semantic(text), requests=[], operations=[dict(op='UNRESOLVED', reason='stop', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'DECLINED')
        self.assertNotIn('?', result['response_text'] or '')

    def test_live_unresolved_ownership_closes_purchase_pending(self):
        self.turn('Tôi cũng tính mua xe')
        self.turn('Creta')
        text = 'tôi có xe rồi'
        sem = dict(_social_semantic(text), requests=[], operations=[dict(
            op='UNRESOLVED', reason='no purchase timing', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(self.pending(), [])
        self.assertNotIn('?', result['response_text'] or '')
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')

    def test_unresolved_decline_without_pending_gets_acknowledged(self):
        text = 'đừng hỏi nữa'
        sem = dict(_social_semantic(text), requests=[], operations=[dict(op='UNRESOLVED', reason='stop', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertTrue(result['outbound_permission'], result)
        self.assertNotIn('?', result['response_text'] or '')

    def test_volunteered_timeline_does_not_answer_vehicle_question(self):
        self.turn('Tôi cũng tính mua xe')
        text = 'tháng sau'
        sem = dict(_social_semantic(text), operations=[dict(op='UPDATE_SLOT', request_ref='topic:social',
            evidence_excerpt=text, slot=dict(kind='purchase_timeline',value=text,explicit=True))])
        _, result = self.turn(text, semantic=sem)
        self.assertNotIn('finance_intent', self.pending())
        self.assertNotIn('trả góp', result['response_text'] or '')
        self.assertNotEqual(self.profile().get('interested_vehicle', {}).get('status'), 'ANSWERED')

    def test_inquiry_for_other_is_not_decline(self):
        _, first = self.turn('Anh tôi tính mua Creta')
        text = 'Tôi chỉ hỏi hộ thôi'
        sem = dict(_social_semantic(text), requests=[], operations=[dict(op='UNRESOLVED', reason='other person', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=sem)
        self.assertTrue(result['outbound_permission'], result)
        self.assertNotIn('dừng', result['response_text'] or '')
        self.assertNotEqual(first['response_text'], result['response_text'])
        self.assertFalse(self.profile())

    def test_address_with_comma_and_ack_for_answer(self):
        self.turn('Chào bác, tôi có xe rồi')
        self.assertEqual(self.state().get('address_term'), 'bác')
        self.turn('Tôi cũng tính mua xe')
        _, result = self.turn('Creta')
        self.assertNotIn('mình nghe rồi', result['response_text'] or '')
        self.assertNotIn('mình hiểu rồi', result['response_text'] or '')
    def test_decline_is_remembered_and_not_asked_again(self):
        self.turn('Toi cũng tính mua')
        _, result = self.turn('không cần đâu')
        self.assertNotIn('?', self.say(result))
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'DECLINED')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['follow_up'], 'stopped')
        self.turn('mình cũng tính mua')
        # The customer raising buying again is a new signal, so asking may resume.
        self.assertEqual(self.state()['purchase_intent'], 'exploring')

    def test_ignored_question_is_not_repeated(self):
        self.turn('Toi cũng tính mua')
        _, result = self.turn('hôm nay trời đẹp quá')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['follow_up'], 'stopped')
        self.assertNotIn('?', result['response_text'] or '')

    def test_someone_elses_plan_is_not_the_speakers(self):
        self.turn('anh tôi tính mua Creta')
        self.assertNotEqual(self.state().get('purchase_intent'), 'exploring')
        self.assertEqual((self.profile(), self.pending()), ({}, []))

    def test_two_people_in_one_thread_do_not_share_state(self):
        self.turn('Bác đi màu gì', actor='a')
        self.turn('Toi cũng tính mua', actor='b')
        self.turn('tôi có xe rồi', actor='a')
        self.assertEqual(self.state('a')['ownership_status'], 'owns_vehicle')
        self.assertEqual(self.pending('b'), ['interested_vehicle'])
        self.assertNotIn('ownership_status', self.state('b'))
        self.assertNotEqual(self.state('a').get('address_term'), self.state('b').get('address_term'))

    def test_lead_questions_are_at_most_one_per_turn_and_three_total(self):
        self.turn('Toi cũng tính mua')
        for text in ('Creta', 'tháng sau', 'trả góp', '800 triệu', 'đi làm'):
            _, result = self.turn(text)
            self.assertLessEqual((result['response_text'] or '').count('?'), 1)
        self.assertLessEqual(self.state().get('asked_total', 0), 3)

    def test_blocked_customer_gets_no_question(self):
        self.engine.store.block('p', 'a', 'public_reply')
        self.engine.store.conn.commit()
        _, result = self.turn('Toi cũng tính mua')
        self.assertFalse(result['outbound_permission'])
        self.assertEqual(self.pending(), [])

    def test_deposit_is_handed_off_in_a_human_voice(self):
        text = 'Cho mình đặt cọc Creta'
        semantic = dict(_social_semantic(text), requests=[dict(topic='buy', desired_outcome='buy', polarity='positive')],
                        operations=[dict(op='START_REQUEST', request_ref='topic:buy', evidence_excerpt=text)])
        _, result = self.turn(text, semantic=semantic)
        self.assertIn('HANDOFF_DRAFT_ONLY', result['routing'])
        self.assertIn('đại lý', self.say(result))

    def test_plain_greeting_thanks_and_ok_still_work(self):
        for text in ('Chào ad', 'Cảm ơn bạn nhé', 'ok'):
            _, result = self.turn(text)
            self.assertTrue(result['outbound_permission'], text)
            self.assertNotIn('?', result['response_text'])

    def test_unrelated_chatter_is_never_answered_with_a_canned_line(self):
        _, result = self.turn('hôm nay mưa to ghê')
        self.assertFalse(result['outbound_permission'])
        self.assertIsNone(result['response_text'])


if __name__ == '__main__':
    unittest.main()
