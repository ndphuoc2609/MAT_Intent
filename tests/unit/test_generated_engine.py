import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from tests.unit.test_peer_conversation import ConversationBase
from tests.unit.test_persona_engine import _social_semantic


class GeneratedEngineTests(ConversationBase):
    def _stored_story_turn(self):
        from app.persona_runtime import digest
        from app.persona_stories import StoryStore
        self.engine.model_led_conversation = True
        self.engine.auto_send_generated = True
        event, context, _ = self.proposal('Bạn kể về chuyến đi của bạn?')
        snapshot = self.engine.persona_store.get_binding_snapshot('p', 'post')
        vehicle = next(f['fact_id'] for f in snapshot['facts'] if f['key'] == 'vehicle.current.model')
        repository = StoryStore(self.engine.persona_store)
        proposal = dict(proposal_id='new1', idempotency_key=event['comment_id'],
            persona_version=snapshot['release_id'], topic='chuyến đi', event='Mình đi Đà Lạt bằng Creta',
            time_start='2025-04-01', time_end=None,
            entities=dict(vehicle='Creta', location='Đà Lạt', people=[]), base_fact_ids=[vehicle],
            new_details=[], relation=None)
        accepted = repository.propose(snapshot, proposal)
        self.assertEqual(accepted['status'], 'ACCEPTED', accepted)
        story = accepted['memory']
        context['stories'] = [story]
        context['context_token'] = digest({k: v for k, v in context.items() if k != 'context_token'})
        candidate = dict(reply_text=story['event'], fact_ids=[], memory_ids=[story['memory_id']],
            claims=[dict(text=story['event'], subject='persona', predicate='gen.memory', fact_ids=[],
                memory_ids=[story['memory_id']], message_ids=[])], asks_question=False, question=None)
        signals = dict(speaker_intent='social', address_term=None, asks_persona_question=True,
            persona_topic='travel_story', evidence_excerpt=event['text'], claims=[])
        return event, context, candidate, signals, repository, snapshot, story

    def test_stored_story_is_recorded_only_after_successful_send(self):
        event, context, candidate, signals, repository, snapshot, story = self._stored_story_turn()
        self.assertEqual(self.engine.store.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 0)
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context, peer_signals=signals)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(self.engine.sender.sent[-1]['text'], story['event'])
        self.assertEqual(self.engine.store.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 1)

    def test_failed_send_keeps_story_but_never_marks_told(self):
        event, context, candidate, signals, repository, snapshot, story = self._stored_story_turn()
        with patch.object(self.engine.sender, 'send', return_value={'status': 'failed'}):
            result = self.engine.process(event, self.post, _social_semantic(event['text']),
                reply_candidate=candidate, generation_context=context, peer_signals=signals)
        self.assertFalse(result['outbound_permission'], result)
        self.assertIsNotNone(repository.get(snapshot, story['memory_id']))
        self.assertEqual(self.engine.store.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 0)
        self.assertEqual(self.engine.store.conn.execute('SELECT count(*) FROM persona_story_reservations').fetchone()[0], 0)

    def test_revoked_stored_story_cannot_be_dispatched(self):
        event, context, candidate, signals, repository, snapshot, story = self._stored_story_turn()
        self.engine.auto_send_generated = False
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context, peer_signals=signals)
        row = self.engine.store.conn.execute('SELECT payload FROM generated_reply_drafts WHERE draft_id=?',
                                             (result['draft_id'],)).fetchone()
        data = __import__('json').loads(row[0])
        payload = self.engine._generated_outbox_payload(event, result['draft_id'], data['candidate'],
            context, data['validation'], data['peer_plan'])
        self.assertTrue(repository.revoke(snapshot, story['memory_id'], 'thu hồi'))
        status, _ = self.engine._dispatch(event, [], 'persona_reply', candidate['reply_text'],
                                          self.engine.clock.now().isoformat(), payload)
        self.assertEqual(status, 'BLOCKED')
        self.assertFalse(self.engine.sender.sent)
        self.assertEqual(self.engine.store.conn.execute('SELECT count(*) FROM persona_story_deliveries').fetchone()[0], 0)

    def test_model_led_story_must_match_active_bound_store_at_validation(self):
        from app.persona_runtime import digest
        self.engine.model_led_conversation = True
        event, context, _ = self.proposal('Bạn kể về chuyến đi của bạn?')
        story = dict(memory_id='story-1', persona_uuid=context['persona_uuid'],
            release_id=context['release_id'], topic='trip', event='Mình đi Đà Lạt bằng Creta.',
            time_start='2025-04-01', time_end=None,
            entities={'vehicle': 'Creta', 'location': 'Đà Lạt', 'people': []},
            base_fact_ids=[], new_details=[], relation=None, label='fictional_generated', status='ACTIVE')
        context['stories'] = [story]
        context['context_token'] = digest({k: v for k, v in context.items() if k != 'context_token'})
        text = story['event']
        candidate = dict(reply_text=text, fact_ids=[], memory_ids=['story-1'], claims=[
            dict(text=text, subject='persona', predicate='gen.memory', fact_ids=[],
                 memory_ids=['story-1'], message_ids=[])], asks_question=False, question=None)
        with patch('app.persona_stories.StoryStore.get', return_value=story):
            result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']),
                                                  {}, candidate, context, None)
        self.assertEqual(result['status'], 'OK', result)
        self.assertEqual(result['memory_ids'], ['story-1'])
        altered = dict(candidate, reply_text=text + ' Xe bị hỏng.', claims=[
            dict(candidate['claims'][0], text=text + ' Xe bị hỏng.')])
        with patch('app.persona_stories.StoryStore.get', return_value=story):
            result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']),
                                                  {}, altered, context, None)
        self.assertEqual(result['reason'], 'STORY_CLAIM_UNVERIFIED')
        with patch('app.persona_stories.StoryStore.get', return_value=None):
            result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']),
                                                  {}, candidate, context, None)
        self.assertEqual(result['reason'], 'STORY_UNAVAILABLE_OR_CHANGED')

    def test_model_led_plan_does_not_choose_fixed_sales_question(self):
        from app.persona_runtime import peer_plan_from_signals
        signals = dict(speaker_intent='buying_interest', address_term=None,
            asks_persona_question=False, claims=[])
        plan = peer_plan_from_signals(signals, 'Mình đang cân nhắc', {}, {}, [], model_led=True)
        self.assertIsNone(plan['hook_field'])

    def test_model_led_reply_accepts_relevant_grounded_additional_fact(self):
        self.engine.model_led_conversation = True
        event, _, _ = self.proposal('Bạn đang đi xe gì?')
        context = self.engine.persona_generation_context(event, self.post,
            {'topics': ['vehicle_current'], 'keys': ['vehicle.current.model', 'vehicle.current.trim']})
        by_key = {f['key']: f['fact_id'] for f in context['facts']}
        text = 'Mình đang đi Hyundai Creta, bản 1.5 Cao cấp.'
        ids = [by_key['vehicle.current.model'], by_key['vehicle.current.trim']]
        candidate = dict(reply_text=text, fact_ids=ids, memory_ids=[], claims=[
            dict(text=text, subject='persona', predicate=key, fact_ids=ids, memory_ids=[], message_ids=[])
            for key in ('vehicle.current.model', 'vehicle.current.trim')], asks_question=False, question=None)
        result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']), {}, candidate, context, None)
        self.assertEqual(result['status'], 'OK', result)

    def test_model_led_accepts_contextual_question_without_funnel(self):
        self.engine.model_led_conversation = True
        event, context, _ = self.proposal('Nhà mình năm người, cuối tuần hay đi xa.')
        text = 'Những chuyến đi đó thường mang nhiều hành lý không?'
        candidate = dict(reply_text=text, fact_ids=[], memory_ids=[], claims=[], asks_question=True,
            question=dict(kind='conversation', field=None, text=text))
        plan = dict(hook_field=None, next_state={'follow_up': None})
        result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']), plan, candidate, context, None)
        self.assertEqual(result['status'], 'OK', result)

    def test_model_led_does_not_send_canned_fallback_when_model_fails(self):
        self.engine.model_led_conversation = True
        self.engine.auto_send_generated = True
        event, context, _ = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=None, generation_context=context)
        self.assertFalse(result['outbound_permission'], result)
        self.assertEqual(self.engine.sender.sent, [])

    def test_purging_expired_generated_fact_leaves_connection_ready_for_next_turn(self):
        event, _, _ = self.proposal()
        now = self.engine.clock.now()
        persona_id = self.engine.persona_store.get_binding_snapshot('p', 'post')['persona_uuid']
        self.engine.persona_store.propose_generated(persona_id, 'gen.trip.old_route',
            'Cuối tuần mình hay đi Đà Lạt', 'approximate', 'test', 'expired-fact',
            (now - timedelta(minutes=31)).isoformat())

        context = self.engine.persona_generation_context(event, self.post)

        self.assertIsInstance(context, dict)
        self.assertFalse(self.engine.store.conn.in_transaction)
        result = self.engine.process(event, self.post, _social_semantic(event['text']))
        self.assertFalse(any(route.startswith('ERROR_') for route in result['routing']))

    def proposal(self, text='Bác đi xe gì?'):
        event = dict(page_id='p', post_id='post', thread_root_id='root', actor_id='a',
                     comment_id=f'g{self.counter}', text=text, actor_role='user', event_type='create')
        self.counter += 1
        context = self.engine.persona_generation_context(event, self.post)
        fact = next(f for f in context['facts'] if f['key'] == 'vehicle.current.model')
        candidate = dict(reply_text='Mình đang đi Hyundai Creta.', fact_ids=[fact['fact_id']], memory_ids=[],
            claims=[dict(text='Mình đang đi Hyundai Creta.', subject='persona', predicate='vehicle.current.model',
                         fact_ids=[fact['fact_id']], memory_ids=[], message_ids=[])],
            asks_question=False, question=None)
        return event, context, candidate

    def test_generation_context_only_supplies_keys_requested_for_a_topic(self):
        event, _, _ = self.proposal('Bạn đang đi xe gì?')

        context = self.engine.persona_generation_context(event, self.post,
            {'topics': ['vehicle_current'], 'keys': ['vehicle.current.model']})

        self.assertEqual({fact['key'] for fact in context['facts']}, {'vehicle.current.model'})

    def generated_proposal(self, event, context, *, extra_sentence=''):
        text = 'Cuối tuần mình hay đi Đà Lạt.' + extra_sentence
        return dict(reply_text=text, fact_ids=['new1'], memory_ids=[],
            claims=[dict(text='Cuối tuần mình hay đi Đà Lạt.', subject='persona', predicate='gen.trip',
                fact_ids=['new1'], memory_ids=[], message_ids=[])],
            asks_question=False, question=None, fact_proposals=[dict(proposal_id='new1', namespace='trip',
                slug='weekend_getaway', value='Cuối tuần mình hay đi Đà Lạt', precision='approximate')])

    def test_generated_proposal_is_pending_until_successful_delivery(self):
        event, context, _ = self.proposal('Cuối tuần bạn đi đâu?')
        candidate = self.generated_proposal(event, context)
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.assertEqual(result['generation_validation'], 'OK', result)
        rows = self.engine.persona_store.list_generated(context['persona_uuid'])
        self.assertEqual(rows[0]['status'], 'PENDING')
        self.assertEqual(result['proposal_results'][0]['status'], 'PENDING')
        self.assertEqual(self.engine.sender.sent, [])

    def test_rejected_unused_proposal_does_not_block_a_grounded_reply(self):
        event, context, _ = self.proposal('Cuối tuần bạn đi đâu?')
        candidate = self.generated_proposal(event, context)
        candidate['fact_proposals'].append(dict(proposal_id='new2', namespace='hobby',
            slug='contact_detail', value='Số điện thoại 0987654321', precision='exact'))
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.assertEqual(result['generation_validation'], 'OK', result)
        self.assertEqual([proposal['status'] for proposal in result['proposal_results']], ['PENDING', 'REJECTED'])
        self.assertEqual(result['proposal_results'][1]['reason'], 'GENERATED_FACT_CONTACT')

    def test_memory_need_accepts_relevant_trip_proposal(self):
        event, _, _ = self.proposal('Bạn có kỷ niệm nào đáng nhớ với Creta không?')
        context = self.engine.persona_generation_context(event, self.post,
            {'topics': ['memory'], 'keys': []})
        candidate = self.generated_proposal(event, context)

        result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']), {},
            candidate, context, None)

        self.assertEqual(result['status'], 'OK', result)
        self.assertEqual(result['proposal_results'][0]['status'], 'PENDING')

    def test_vehicle_experience_need_accepts_memory_proposal(self):
        event, _, _ = self.proposal('Bạn có kỷ niệm nào đáng nhớ với Creta không?')
        needs = {'topics': ['vehicle_experience'], 'keys': ['vehicle.experience.model']}
        context = self.engine.persona_generation_context(event, self.post, needs)
        value = 'Có lần mình đi Creta trong một chuyến đi cuối tuần.'
        candidate = dict(reply_text=value, fact_ids=['new1'], memory_ids=[],
            claims=[dict(text=value, subject='persona', predicate='gen.memory', fact_ids=['new1'],
                         memory_ids=[], message_ids=[])], asks_question=False, question=None,
            fact_proposals=[dict(proposal_id='new1', namespace='memory', slug='creta_weekend_memory',
                value=value, precision='none')])

        result = self.engine._generated_reply(event, self.post, _social_semantic(event['text']), {},
            candidate, context, None)

        self.assertEqual(result['status'], 'OK', result)

    def test_real_persona_can_propose_and_use_its_own_generated_fact(self):
        from app.persona_fixture import seed_database_persona
        repository = self.engine.persona_store
        persona_id, release_id = seed_database_persona(repository, {
            'id': 'real-person-1', 'name': 'Linh', 'occupation': 'Giáo viên',
            'car_owned': 'Hyundai Tucson', 'tone': 'warm'})
        repository.bind_post('p', 'real-post', persona_id, release_id)
        postcard = dict(id='real-post', page_id='p', text='Bài do Linh chia sẻ.',
                        vehicles=['Hyundai Tucson'], persona_binding=True)
        event = dict(page_id='p', post_id='real-post', thread_root_id='root', actor_id='customer',
                     comment_id='real-fact-1', text='Cuối tuần chị hay đi đâu?', actor_role='user',
                     event_type='create')
        context = self.engine.persona_generation_context(event, postcard, {'topics': ['trip'], 'keys': []})
        candidate = dict(reply_text='Cuối tuần mình hay đi Đà Lạt.', fact_ids=['new1'], memory_ids=[],
            claims=[dict(text='Cuối tuần mình hay đi Đà Lạt.', subject='persona', predicate='gen.trip',
                         fact_ids=['new1'], memory_ids=[], message_ids=[])], asks_question=False, question=None,
            fact_proposals=[dict(proposal_id='new1', namespace='trip', slug='weekend_getaway',
                value='Cuối tuần mình hay đi Đà Lạt', precision='approximate')])
        result = self.engine._generated_reply(event, postcard, _social_semantic(event['text']), {},
                                              candidate, context, None)
        self.assertEqual(result['status'], 'OK', result)
        self.assertEqual(result['proposal_results'][0]['status'], 'PENDING')
        fact = next(item for item in repository.attach_generated(
            repository.get_binding_snapshot('p', 'real-post'), event['comment_id'])['facts']
            if item.get('generated'))
        self.assertEqual(fact['persona_uuid'], persona_id)
        self.assertEqual(fact['source_kind'], 'generated')

    def test_fact_repeated_from_customer_question_is_not_learned(self):
        event, context, _ = self.proposal('Bạn mua xe năm 2020 à?')
        candidate = dict(reply_text='Mình mua xe năm 2020.', fact_ids=['new1'], memory_ids=[],
            claims=[dict(text='Mình mua xe năm 2020.', subject='persona', predicate='gen.car',
                fact_ids=['new1'], memory_ids=[], message_ids=[])], asks_question=False, question=None,
            fact_proposals=[dict(proposal_id='new1', namespace='car', slug='purchase_year',
                value='Mình mua xe năm 2020', precision='year')])
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.assertEqual(result['proposal_results'][0]['reason'], 'GENERATED_FACT_FROM_CUSTOMER')
        self.assertIsNone(self.engine.store.conn.execute(
            'SELECT 1 FROM persona_generated_facts WHERE created_comment_id=?', (event['comment_id'],)).fetchone())
        self.assertNotEqual(result['generation_validation'], 'OK')

    def test_same_key_race_sends_only_one_winner_and_does_not_crash(self):
        self.engine.auto_send_generated = True
        events = [dict(page_id='p', post_id='post', thread_root_id='root', actor_id=f'a{i}',
            comment_id=f'race-{i}', text='Cuối tuần bạn đi đâu?', actor_role='user', event_type='create')
            for i in range(2)]
        contexts = [self.engine.persona_generation_context(event, self.post) for event in events]
        def run(pair):
            event, context = pair
            return self.engine.process(event, self.post, _social_semantic(event['text']),
                reply_candidate=self.generated_proposal(event, context), generation_context=context)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, zip(events, contexts)))
        rows = self.engine.store.conn.execute('SELECT status FROM persona_generated_facts WHERE fact_key=?',
            ('gen.trip.weekend_getaway',)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual([row[0] for row in rows], ['ACTIVE'])
        self.assertEqual(sum(result['generation_validation'] == 'OK' for result in results), 1)
        self.assertEqual(sum(result['outbound_permission'] for result in results), 1)
        self.assertEqual(len(self.engine.sender.sent), 1)

    def test_unsupported_sentence_discards_a_new_proposal(self):
        event, context, _ = self.proposal('Cuối tuần bạn đi đâu?')
        candidate = self.generated_proposal(event, context, extra_sentence=' Vợ mình cũng thích.')
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.assertNotEqual(result['generation_validation'], 'OK')
        row = self.engine.store.conn.execute('SELECT status FROM persona_generated_facts WHERE created_comment_id=?',
            (event['comment_id'],)).fetchone()
        self.assertEqual(row[0], 'DISCARDED')
        self.assertEqual(self.engine.sender.sent, [])

    def test_auto_send_activates_fact_for_the_next_customer(self):
        self.engine.auto_send_generated = True
        event, context, _ = self.proposal('Cuối tuần bạn đi đâu?')
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=self.generated_proposal(event, context), generation_context=context)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(len(self.engine.sender.sent), 1)
        fact = self.engine.persona_store.list_generated(context['persona_uuid'])[0]
        self.assertEqual(fact['status'], 'ACTIVE')

        event2 = dict(event, actor_id='customer-2', comment_id='another-customer', text='Bạn kể chuyện cuối tuần đi')
        context2 = self.engine.persona_generation_context(event2, self.post, {'topics': ['trip'], 'keys': []})
        generated = next(item for item in context2['facts'] if item.get('generated'))
        self.assertEqual(generated['value'], 'Cuối tuần mình hay đi Đà Lạt')
        candidate = dict(reply_text='Cuối tuần mình hay đi Đà Lạt.', fact_ids=[generated['fact_id']], memory_ids=[],
            claims=[dict(text='Cuối tuần mình hay đi Đà Lạt.', subject='persona', predicate='gen.trip',
                fact_ids=[generated['fact_id']], memory_ids=[], message_ids=[])], asks_question=False, question=None)
        second = self.engine.process(event2, self.post, _social_semantic(event2['text']),
            reply_candidate=candidate, generation_context=context2)
        self.assertTrue(second['outbound_permission'], second)
        self.assertEqual(len(self.engine.sender.sent), 2)

    def test_revoked_generated_fact_blocks_a_waiting_outbox(self):
        event, context, _ = self.proposal('Cuối tuần bạn đi đâu?')
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=self.generated_proposal(event, context), generation_context=context)
        with self.engine.store.lock:
            row = self.engine.store.conn.execute('SELECT payload FROM generated_reply_drafts WHERE draft_id=?',
                (result['draft_id'],)).fetchone()
            data = __import__('json').loads(row[0])
            payload = self.engine._generated_outbox_payload(event, result['draft_id'], data['candidate'],
                context, data['validation'], data['peer_plan'])
            self.engine._outbox_intended(event, [], 'persona_reply', payload, self.engine.clock.now().isoformat())
            self.engine.store.conn.commit()
        generated_id = data['validation']['generated_fact_ids'][0]
        self.engine.persona_store.revoke_generated(generated_id, 'fixture test', 'operator')
        self.assertEqual(self.engine.drain_intended(), [(payload['dedupe_key'], 'BLOCKED')])
        self.assertEqual(self.engine.sender.sent, [])

    def test_generation_is_draft_until_operator_approves(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.assertFalse(result['outbound_permission'])
        self.assertEqual(self.engine.sender.sent, [])
        self.assertIn('draft_id', result)
        reviewed = self.engine.review_generated(event, result['draft_id'], 'approve')
        self.assertEqual(reviewed['status'], 'SENT')
        self.assertEqual(len(self.engine.sender.sent), 1)
        again = self.engine.review_generated(event, result['draft_id'], 'approve')
        self.assertEqual(again['status'], 'SENT')
        self.assertEqual(len(self.engine.sender.sent), 1)

    def test_draft_cannot_be_reviewed_from_other_actor(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
                                     generation_context=context)
        with self.assertRaises(ValueError):
            self.engine.review_generated(dict(event, actor_id='someone-else'), result['draft_id'], 'approve')

    def test_customer_optout_after_draft_prevents_late_approval(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
                                     generation_context=context)
        self.engine.store.block('p', 'a', 'public_reply')
        self.engine.store.conn.commit()
        reviewed = self.engine.review_generated(event, result['draft_id'], 'approve')
        self.assertEqual(reviewed['status'], 'BLOCKED')
        self.assertEqual(self.engine.sender.sent, [])

    def test_reject_keeps_draft_but_sends_nothing(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
                                     generation_context=context)
        reviewed = self.engine.review_generated(event, result['draft_id'], 'reject')
        self.assertEqual(reviewed['status'], 'REJECTED')
        self.assertEqual(self.engine.sender.sent, [])
        self.assertEqual(self.engine.generated_drafts(event)[0]['status'], 'REJECTED')

    def test_edit_only_saves_and_requires_separate_approval(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        edited = self.engine.review_generated(event, result['draft_id'], 'edit', text=candidate['reply_text'])
        self.assertEqual(edited['status'], 'PENDING')
        self.assertEqual(self.engine.sender.sent, [])
        self.assertEqual(self.engine.review_generated(event, result['draft_id'], 'approve')['status'], 'SENT')

    def test_semantic_slang_ownership_overrides_legacy_funnel(self):
        self.turn('Tôi cũng tính mua xe')
        event, context, candidate = self.proposal('Tui có xế sẵn rùi nha, bác đi xe gì?')
        signals = dict(speaker_intent='owns_vehicle', address_term='bác', persona_topic='vehicle_current',
            asks_persona_question=True, evidence_excerpt=event['text'], claims=[])
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
            generation_context=context, peer_signals=signals)
        self.assertIn('draft_id', result)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')
        self.assertNotEqual(self.state()['purchase_intent'], 'exploring')

    def test_edited_source_before_review_blocks_delivery(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
            generation_context=context)
        self.engine.store.conn.execute('UPDATE events SET retracted=1 WHERE comment_id=?', (event['comment_id'],))
        self.engine.store.conn.commit()
        reviewed = self.engine.review_generated(event, result['draft_id'], 'approve')
        self.assertEqual(reviewed['status'], 'BLOCKED')
        self.assertEqual(self.engine.sender.sent, [])

    def test_later_semantic_correction_blocks_old_draft_even_without_pending(self):
        event, context, candidate = self.proposal()
        first = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
            generation_context=context)
        later, new_context, new_candidate = self.proposal('Tui có xế sẵn rồi, bác đi xe gì?')
        signals = dict(speaker_intent='owns_vehicle', address_term='bác', persona_topic='vehicle_current',
            asks_persona_question=True, evidence_excerpt=later['text'], claims=[])
        self.engine.process(later, self.post, _social_semantic(later['text']), reply_candidate=new_candidate,
            generation_context=new_context, peer_signals=signals)
        reviewed = self.engine.review_generated(event, first['draft_id'], 'approve')
        self.assertEqual(reviewed['status'], 'BLOCKED')
        self.assertEqual(self.engine.sender.sent, [])

    def test_story_waits_four_intervening_valid_customer_turns(self):
        event, context, candidate = self.proposal('Cuối tuần bạn về đâu?')
        self.assertTrue(context['memories'])
        memory = context['memories'][0]
        scope = tuple(event[k] for k in ('page_id','post_id','thread_root_id','actor_id'))
        self.engine.store.conn.execute('INSERT INTO persona_memory_deliveries VALUES (?,?,?,?,?,?,?,?)',
            ('story-send', *scope, 1, '["' + memory['memory_id'] + '"]', '2026-10-30'))
        self.engine.store.conn.commit()
        snapshot = self.engine.persona_store.get_binding_snapshot('p', 'post')
        self.assertEqual(self.engine._generation_memories(event, snapshot, 5), [])
        self.assertTrue(self.engine._generation_memories(event, snapshot, 6))

    def test_story_fact_cannot_bypass_memory_citation_or_cooldown(self):
        event, context, _ = self.proposal('Cuối tuần bạn về đâu?')
        memory = context['memories'][0]
        candidate = dict(reply_text='Cuối tuần mình về Nam Định.', fact_ids=memory['fact_ids'], memory_ids=[],
            claims=[dict(text='Cuối tuần mình về Nam Định.', subject='persona', predicate='travel.weekend.destination',
                         fact_ids=memory['fact_ids'], memory_ids=[], message_ids=[])], asks_question=False, question=None)
        signals = dict(speaker_intent='social', address_term=None, persona_topic='travel_story',
                       asks_persona_question=True, evidence_excerpt=event['text'], claims=[])
        checked = self.engine._generated_reply(event, self.post, {}, self.engine._peer_plan(event, None), candidate, context, signals)
        self.assertEqual(checked['status'], 'REVIEW')
        self.assertEqual(checked['reason'], 'MEMORY_CITATION_REQUIRED')
        scope = tuple(event[k] for k in ('page_id', 'post_id', 'thread_root_id', 'actor_id'))
        self.engine.store.conn.execute('INSERT INTO persona_memory_deliveries VALUES (?,?,?,?,?,?,?,?)',
            ('story-cooldown', *scope, 1, '["' + memory['memory_id'] + '"]', '2026-10-30'))
        self.engine.store.conn.commit()
        during = self.engine.persona_generation_context(event, self.post)
        self.assertFalse(during['memories'])
        self.assertFalse(set(memory['fact_ids']) & {f['fact_id'] for f in during['facts']})

    def test_inflight_story_reservation_blocks_a_competing_dispatch(self):
        event, context, _ = self.proposal('Cuối tuần bạn về đâu?')
        memory = context['memories'][0]
        fact_id = memory['fact_ids'][0]
        fact = next(item for item in context['facts'] if item['fact_id'] == fact_id)
        text = memory['approved_summary']
        candidate = dict(reply_text=text, fact_ids=[fact_id], memory_ids=[memory['memory_id']],
            claims=[dict(text=text, subject='persona', predicate=fact['key'], fact_ids=[fact_id],
                         memory_ids=[memory['memory_id']], message_ids=[])],
            asks_question=False, question=None)
        signals = dict(speaker_intent='social', address_term='bạn', persona_topic='travel_story',
            asks_persona_question=True, evidence_excerpt=event['text'], claims=[])
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context, peer_signals=signals)
        self.assertEqual(result['generation_validation'], 'OK', result)
        self.assertTrue(self.engine.persona_store.reserve_memory_delivery(event, [memory['memory_id']],
            'other-worker:reply', 1, self.engine.clock.now().isoformat()))
        self.assertFalse(self.engine.persona_store.reserve_memory_delivery(event, [memory['memory_id']],
            'competing-worker:reply', 1, self.engine.clock.now().isoformat()))
        row = self.engine.store.conn.execute('SELECT payload FROM generated_reply_drafts WHERE draft_id=?',
            (result['draft_id'],)).fetchone()
        data = __import__('json').loads(row[0])
        payload = self.engine._generated_outbox_payload(event, result['draft_id'], data['candidate'],
            context, data['validation'], data['peer_plan'])
        status, _ = self.engine._dispatch(event, [], 'persona_reply', data['validation']['text'],
            self.engine.clock.now().isoformat(), payload)
        self.assertEqual(status, 'BLOCKED')
        self.assertEqual(self.engine.sender.sent, [])
        self.assertEqual(self.engine.persona_store.reserved_memory_ids(event), [memory['memory_id']])

    def test_automatic_fallback_cannot_bypass_story_cooldown(self):
        event, _, _ = self.proposal('Cuối tuần bạn về đâu?')
        memory = self.engine.persona_store.get_binding_snapshot('p', 'post')['memories'][0]
        scope = tuple(event[k] for k in ('page_id', 'post_id', 'thread_root_id', 'actor_id'))
        self.engine.store.conn.execute('INSERT INTO persona_memory_deliveries VALUES (?,?,?,?,?,?,?,?)',
            ('story-fallback-cooldown', *scope, 1, '["' + memory['memory_id'] + '"]', '2026-10-30'))
        self.engine.store.conn.commit()
        context = self.engine.persona_generation_context(event, self.post)
        self.assertFalse(context['memories'])

        self.engine.auto_send_generated = True
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=None, generation_context=context)
        self.assertFalse(result['outbound_permission'])
        self.assertEqual(self.engine.sender.sent, [])

    def test_revoked_story_sources_remain_blocked_for_facts_and_fallback(self):
        event, context, _ = self.proposal('Cuối tuần bạn về đâu?')
        memory = context['memories'][0]
        self.engine.persona_store.revoke_memory(memory['memory_id'], reviewer='reviewer', reason='consent withdrawn')
        context = self.engine.persona_generation_context(event, self.post)
        self.assertFalse(context['memories'])
        self.assertFalse(set(memory['fact_ids']) & {f['fact_id'] for f in context['facts']})
        self.engine.auto_send_generated = True
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
                                    reply_candidate=None, generation_context=context)
        self.assertFalse(result['outbound_permission'])
        self.assertEqual(self.engine.sender.sent, [])

    def test_null_generated_reply_automatically_sends_approved_fallback(self):
        event, context, _ = self.proposal()
        self.engine.auto_send_generated = True
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=None, generation_context=context)
        self.assertTrue(result['outbound_permission'], result)
        self.assertEqual(self.engine.sender.sent[-1]['text'], result['response_text'])

    def test_null_reply_story_fallback_cannot_bypass_memory_policy(self):
        event, context, _ = self.proposal('Cuối tuần bạn về đâu?')
        self.engine.auto_send_generated = True
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
                                    reply_candidate=None, generation_context=context)
        self.assertFalse(result['outbound_permission'])
        self.assertEqual(self.engine.sender.sent, [])

    def test_operator_can_rephrase_grounded_claim_then_approve(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        text = 'Mình đi chiếc Hyundai Creta.'
        edited = self.engine.review_generated(event, result['draft_id'], 'edit', text=text)
        self.assertEqual(edited['status'], 'PENDING')
        self.assertEqual(edited['validation_status'], 'OK', edited)
        self.assertFalse(self.engine.sender.sent)
        self.assertEqual(self.engine.review_generated(event, result['draft_id'], 'approve')['status'], 'SENT')
        self.assertEqual(self.engine.sender.sent[-1]['text'], text)

    def test_interested_vehicle_answer_does_not_unlock_travel_story(self):
        self.turn('Tôi cũng tính mua xe')
        event, context, _ = self.proposal('Creta')
        snapshot = self.engine.persona_store.get_binding_snapshot('p', 'post')
        memory = snapshot['memories'][0]
        candidate = dict(reply_text='Cuối tuần mình về Nam Định.', fact_ids=memory['fact_ids'], memory_ids=[memory['memory_id']],
            claims=[dict(text='Cuối tuần mình về Nam Định.', subject='persona', predicate='travel.weekend.destination',
                         fact_ids=memory['fact_ids'], memory_ids=[memory['memory_id']], message_ids=[])], asks_question=False, question=None)
        signals = dict(speaker_intent='social', asks_persona_question=False, persona_topic=None)
        checked = self.engine._generated_reply(event, self.post, {}, self.engine._peer_plan(event, None), candidate, context, signals)
        self.assertEqual(checked['status'], 'REVIEW')
        self.assertEqual(checked['reason'], 'MEMORY_NOT_ALLOWED')

    def test_unproven_vehicle_slot_does_not_erase_semantic_ownership(self):
        event, context, candidate = self.proposal('Tui có xế rồi nha')
        signals = dict(speaker_intent='owns_vehicle', address_term=None, persona_topic=None,
            asks_persona_question=False, evidence_excerpt=event['text'],
            claims=[dict(predicate='current_vehicle', value='Creta', evidence_excerpt=event['text'])])
        self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
            generation_context=context, peer_signals=signals)
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')
        self.assertNotIn('current_vehicle', self.profile())

    def test_late_ack_reconciles_review_without_resending(self):
        event, context, candidate = self.proposal()
        result = self.engine.process(event, self.post, _social_semantic(event['text']),
            reply_candidate=candidate, generation_context=context)
        self.engine.sender.fail_mode = 'lost_ack'
        self.assertEqual(self.engine.review_generated(event, result['draft_id'], 'approve')['status'], 'UNKNOWN')
        key = result['draft_id'] + ':reply'
        self.assertEqual(self.engine.sender.receiver.effect_count(key), 0)
        self.assertEqual(self.engine.review_generated(event, result['draft_id'], 'approve')['status'], 'UNKNOWN')
        self.engine.sender.confirm_delivery(key)
        self.assertEqual(self.engine.review_generated(event, result['draft_id'], 'approve')['status'], 'SENT')
        self.assertEqual(self.engine.sender.receiver.effect_count(key), 1)

    def test_generation_context_contains_verified_prior_correction(self):
        self.turn('Tôi có xe rồi')
        _, context, _ = self.proposal('Bác đi xe gì?')
        self.assertEqual(context['peer_state']['ownership_status'], 'owns_vehicle')

    def test_null_reply_keeps_evidence_checked_decline_and_ownership(self):
        event, context, _ = self.proposal('Tui có xế rồi, không tìm mua đâu')
        event['generation_mode'] = 'draft'
        signals = dict(speaker_intent='declines', address_term=None, persona_topic=None,
            asks_persona_question=False, evidence_excerpt=event['text'], claims=[
                dict(predicate='ownership_status', value='owns_vehicle', evidence_excerpt='Tui có xế rồi')])
        result = self.engine.process(event, self.post, _social_semantic(event['text']), generation_context=context,
            reply_candidate=None, peer_signals=signals)
        self.assertEqual(self.state()['ownership_status'], 'owns_vehicle')
        self.assertEqual(self.state()['follow_up'], 'stopped')
        self.assertEqual(self.state()['purchase_intent'], 'not_interested')
        self.assertFalse(result['outbound_permission'])

    def test_delivered_lead_question_is_recorded_despite_intervening_social_turn(self):
        event, context, candidate = self.proposal('Tôi cũng tính mua xe')
        question = 'Bạn đang nghiêng về mẫu xe nào?'
        candidate = dict(reply_text=question, fact_ids=[], memory_ids=[], claims=[], asks_question=True,
            question=dict(kind='lead', field='interested_vehicle', text=question))
        signals = dict(speaker_intent='buying_interest', address_term=None, persona_topic=None,
            asks_persona_question=False, evidence_excerpt=event['text'], claims=[])
        result = self.engine.process(event, self.post, _social_semantic(event['text']), reply_candidate=candidate,
            generation_context=context, peer_signals=signals)
        original = self.engine.sender.send
        def intervening(payload):
            later = dict(event, comment_id='later-social', text='cảm ơn')
            self.engine._generation_turn_ordinal(later, record=True)
            self.engine.store.conn.commit()
            return original(payload)
        self.engine.sender.send = intervening
        reviewed = self.engine.review_generated(event, result['draft_id'], 'approve')
        self.assertEqual(reviewed['status'], 'SENT', reviewed)
        self.assertEqual(self.pending(), ['interested_vehicle'])
        self.assertEqual(self.state()['asked_total'], 1)
