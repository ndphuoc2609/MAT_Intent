"""Acceptance cases: answer the actual question with scoped evidence."""
from tests.unit.test_peer_conversation import ConversationBase
from app.persona_fixture import seed_database_demo, seed_nam_creta


class RealisticConversationTests(ConversationBase):
    def test_two_questions_in_one_turn_both_get_answers(self):
        _, r = self.turn('Bác đang đi xe gì, bác làm nghề gì?')
        self.assertTrue(r['outbound_permission'], r)
        self.assertIn('Hyundai Creta', r['response_text'])
        self.assertIn('kỹ sư cơ khí', r['response_text'])
        self.assertEqual(self.profile(), {})
        self.assertEqual(len(r['response_plan']['fact_ids']), 2)

    def test_two_properties_of_same_topic(self):
        _, r = self.turn('Bác trả trước bao nhiêu, góp mấy năm?')
        self.assertIn('35%', r['response_text'] or '')
        self.assertIn('5 năm', r['response_text'] or '')

    def test_colloquial_vehicle_question(self):
        for text in ('Bác đi con gì vậy?', 'Xe bác đang chạy là xe gì?', 'Anh đi xe gì thế?'):
            with self.subTest(text=text):
                _, r = self.turn(text)
                self.assertIn('Hyundai Creta', r['response_text'] or '')
                self.assertEqual(self.profile(), {})

    def test_opinion_about_another_model_does_not_reuse_creta_story(self):
        _, r = self.turn('Bác đánh giá Tucson thế nào?')
        self.assertTrue(r['outbound_permission'], r)
        self.assertNotIn('tiếng ồn', r['response_text'])
        self.assertNotIn('Creta khá vừa vặn', r['response_text'])
        self.assertIn('lái thử', r['response_text'])

    def test_missing_color_gives_specific_next_step(self):
        _, r = self.turn('Bác đi xe màu gì?')
        self.assertTrue(r['outbound_permission'], r)
        self.assertNotIn('không rõ', r['response_text'])
        self.assertIn('màu', r['response_text'])
        self.assertIn('ánh sáng', r['response_text'])

    def test_unknown_dealer_does_not_pretend_to_find_personal_purchase_history(self):
        _, r = self.turn('Xe đó bác mua ở đâu?')
        self.assertIn('đại lý', r['response_text'] or '')
        self.assertNotIn('không rõ', r['response_text'] or '')
        self.assertEqual(self.profile(), {})

    def test_sparse_database_persona_answers_both_known_facts_without_nams_story(self):
        repo = self.engine.persona_store
        pid, release = seed_database_demo(repo, {'id': 'acceptance-coach', 'name': 'Coach',
            'car_owned': 'Ford Ranger XLS', 'occupation': 'Huấn luyện viên'}, seed_nam_creta(repo))
        self.post['id'] = 'coach-post'
        repo.bind_post('p', 'coach-post', pid, release)
        from tests.unit.test_persona_engine import _social_semantic
        text = 'Bác đi xe gì, làm nghề gì?'
        event = dict(page_id='p', post_id='coach-post', thread_root_id='root', actor_id='a',
                     comment_id='coach-1', text=text, actor_role='user', event_type='create')
        r = self.engine.process(event, self.post, _social_semantic(text))
        self.assertTrue(r['outbound_permission'], r)
        self.assertIn('Ford Ranger XLS', r['response_text'])
        self.assertIn('Huấn luyện viên', r['response_text'])
        self.assertNotIn('Nam Định', r['response_text'])
        self.assertNotIn('Creta', r['response_text'])

    def test_multi_question_does_not_leak_with_block(self):
        self.engine.store.block('p', 'a', 'public_reply')
        self.engine.store.conn.commit()
        _, r = self.turn('Bác đi xe gì, làm nghề gì?')
        self.assertFalse(r['outbound_permission'])
        self.assertEqual(self.profile(), {})

    def test_published_persona_composes_two_approved_answers(self):
        from tests.unit.test_persona_engine import _PersonaStore, _snapshot_with_district
        self.engine.persona_store = _PersonaStore(_snapshot_with_district())
        self.engine.allow_persona_fixtures = False
        _, r = self.turn('Bạn ở tỉnh nào, bạn ở quận nào?')
        self.assertTrue(r['outbound_permission'], r)
        self.assertIn('Đà Nẵng', r['response_text'])
        self.assertIn('Hải Châu', r['response_text'])
        self.assertEqual(set(r['response_plan']['fact_ids']), {'f1', 'f2'})
        self.assertEqual(self.profile(), {})

    def test_revoked_second_fact_blocks_whole_combined_reply(self):
        from tests.unit.test_persona_engine import _PersonaStore, _snapshot_with_district
        repo = _PersonaStore(_snapshot_with_district())
        repo.revoked_fact_ids = lambda *args: ['f2']
        self.engine.persona_store = repo
        self.engine.allow_persona_fixtures = False
        _, r = self.turn('Bạn ở tỉnh nào, bạn ở quận nào?')
        self.assertFalse(r['outbound_permission'], r)
        self.assertIn('REVIEW_PERSONA_FACT_REVOKED', r['routing'])
        self.assertFalse(r['sends'])

    def test_owner_correction_plus_persona_question_stops_buy_pending(self):
        self.turn('Tôi cũng tính mua xe')
        _, r = self.turn('Tôi có xe rồi, bác đang đi xe gì?')
        self.assertIn('Hyundai Creta', r['response_text'] or '')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')
        self.assertNotEqual(self.state()['purchase_intent'], 'exploring')

    def test_decline_plus_persona_question_stops_buy_pending(self):
        self.turn('Tôi cũng tính mua xe')
        _, r = self.turn('Đừng hỏi nữa, bác làm nghề gì?')
        self.assertIn('kỹ sư cơ khí', r['response_text'] or '')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['follow_up'], 'stopped')
        self.assertEqual(self.profile()['interested_vehicle']['status'], 'DECLINED')

    def test_unavailable_private_detail_is_reviewed_without_canned_deflection(self):
        _, r = self.turn('Bạn bao nhiêu tuổi?')
        self.assertFalse(r['outbound_permission'])
        self.assertIsNone(r['response_text'])
        self.assertIn('REVIEW_PERSONA_NO_PEER_UTTERANCE', r['routing'])
        self.assertEqual(self.profile(), {})
