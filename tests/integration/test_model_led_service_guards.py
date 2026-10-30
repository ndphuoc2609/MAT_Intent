import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.intent_api import IntentService
from app.persona_fixture import seed_database_persona
from app.persona_store import PersonaStore, migrate_sqlite
from app.state import IntentLeaseLost, Store
from tests.unit.test_intent_api import request


def plan_action(text, kind='persona_question'):
    return {'action': 'plan', 'payload': {'intents': [{'kind': kind,
        'subject': 'persona' if kind == 'persona_question' else 'customer',
        'normalized': text, 'evidence': text, 'context_source': 'current_comment',
        'certainty': 'explicit'}], 'priority_order': [0], 'unresolved': [],
        'objective': 'Xử lý nội dung khách vừa nói', 'next_action': 'answer',
        'question': None, 'handoff_reason': None}}


class ModelLedServiceGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def service(self, *, bind):
        store = Store(':memory:')
        migrate_sqlite(store.conn)
        repo = PersonaStore(store)
        if bind:
            persona_id, release_id = seed_database_persona(repo, {'id': 'synthetic-test',
                'name': 'Linh', 'occupation': 'Giáo viên', 'car_owned': 'Hyundai Tucson'})
            repo.bind_post('brand-01', 'post-01', persona_id, release_id)
        with patch.dict(os.environ, {'MAT_INTENT_MODEL_LED_ENABLED': 'true',
                                     'MAT_INTENT_MODEL_LED_ALLOWED_PAGES': 'brand-01'}):
            service = IntentService(store=store, api_key='local-test-key',
                model_call=lambda *_a, **_kw: self.fail('legacy provider must not run'),
                budget_path=str(Path(self.temp.name) / 'budget.jsonl'),
                trace_path=str(Path(self.temp.name) / 'trace.jsonl'))
        self.addCleanup(service.close)
        return service

    def test_api_missing_binding_never_calls_model_loop(self):
        service = self.service(bind=False)
        with patch('app.model_led_pipeline.run_model_led') as loop:
            code, result = service.process(request('unbound-event'))
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(result['trace']['reason'], 'BINDING_UNAVAILABLE')
        loop.assert_not_called()

    def test_api_page_outside_server_allowlist_never_calls_model(self):
        service = self.service(bind=True)
        service.model_led_allowed_pages = frozenset()
        with patch('app.model_led_pipeline.run_model_led') as loop, \
             patch('app.adapters.luna.call_luna_tool') as provider:
            code, result = service.process(request('unauthorized-page-event'))
        self.assertEqual(code, 403)
        self.assertEqual(result['status'], 'UNAUTHORIZED_SCOPE')
        self.assertIsNone(service.store.conn.execute('SELECT 1 FROM intent_api_events WHERE event_id=?',
            ('unauthorized-page-event',)).fetchone())
        loop.assert_not_called()
        provider.assert_not_called()

    def test_expired_api_lease_rolls_back_tool_mutation(self):
        service = self.service(bind=True)
        service.store.conn.execute('CREATE TABLE guarded_test_write (value TEXT)')
        service.store.conn.commit()
        seen = []
        def loop(engine, event, postcard, context, model, budget_guard, *, mutation_guard, **limits):
            self.assertTrue(postcard['persona_binding'])
            self.assertEqual(engine.persona_store.get_binding_snapshot('brand-01', 'post-01')['persona_uuid'],
                             engine.persona_store.binding('brand-01', 'post-01')['persona_id'])
            def expire_then_write():
                service.store.conn.execute("UPDATE intent_api_events SET lease_until='2000-01-01T00:00:00+00:00' WHERE event_id=?",
                                           (event['event_dedupe_key'],))
                service.store.conn.execute("INSERT INTO guarded_test_write VALUES ('must roll back')")
            try:
                mutation_guard(expire_then_write)
            except IntentLeaseLost:
                seen.append('lease blocked')
            return {'status': 'ABSTAIN', 'reason': 'LEASE_LOST'}
        with patch('app.model_led_pipeline.run_model_led', side_effect=loop):
            code, result = service.process(request('lease-event'))
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(seen, ['lease blocked'])
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM guarded_test_write').fetchone()[0], 0)

    def test_api_tool_attempt_is_reserved_recorded_and_settled(self):
        from scripts import budget_ledger
        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        def loop(engine, event, postcard, context, model, budget_guard, **kwargs):
            self.assertTrue(budget_guard())
            call = model({}, [], {}, 3)
            self.assertEqual(call['attempts'], 1)
            return {'status': 'ABSTAIN', 'reason': 'MODEL_ABSTAIN', 'attempts': 1}
        provider = {'ok': False, 'attempts': 1, 'cost_usd_est': 0.001,
                    'cost_unknown': False, 'usage': {'prompt_tokens': 42, 'completion_tokens': 5},
                    'model_effective': 'synthetic'}
        with patch('app.model_led_pipeline.run_model_led', side_effect=loop), \
             patch('app.adapters.luna.call_luna_tool', return_value=provider) as call:
            code, result = service.process(request('budget-event'))
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(call.call_count, 1)
        calls, cost, unknown, _ = budget_ledger.totals(service.budget_path)
        self.assertEqual((calls, unknown), (1, 0))
        self.assertAlmostEqual(cost, 0.001)
        trace = json.loads(Path(service.trace_path).read_text().strip())
        self.assertEqual(trace['usage'], {'prompt_tokens': 42, 'completion_tokens': 5})
        self.assertEqual(trace['attempts'], 1)

    def test_api_tool_budget_denial_does_not_call_provider(self):
        service = self.service(bind=True)
        service.budget_max_calls = 0
        service.budget_max_cost, service.budget_reserve = 5.0, 0.1
        def loop(engine, event, postcard, context, model, budget_guard, **kwargs):
            self.assertFalse(budget_guard())
            return {'status': 'ABSTAIN', 'reason': 'BUDGET_EXHAUSTED'}
        with patch('app.model_led_pipeline.run_model_led', side_effect=loop), \
             patch('app.adapters.luna.call_luna_tool') as provider:
            code, result = service.process(request('denied-budget-event'))
        self.assertEqual(code, 200)
        self.assertEqual(result['trace']['reason'], 'BUDGET_EXHAUSTED')
        provider.assert_not_called()

    def test_api_shared_pipeline_retrieves_bound_fact_and_returns_validated_draft(self):
        from scripts import budget_ledger
        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        calls = []
        customer = 'Bạn đang đi xe gì?'
        def provider(context, transcript, schema, timeout, **kwargs):
            calls.append((context, transcript))
            if not transcript:
                body = plan_action(customer)
            elif transcript[-1].get('tool_result', {}).get('status') == 'ACCEPTED' and len(transcript) == 2:
                body = {'action': 'retrieve', 'payload': {'kind': 'persona_facts',
                    'topic': 'vehicle_current', 'query': None, 'keys': ['vehicle.current.model'],
                    'entities': {'vehicle': None, 'location': None, 'people': []},
                    'time_start': None, 'time_end': None, 'variant': None}}
            else:
                fact = next(f for f in transcript[-1]['tool_result']['facts']
                            if f['key'] == 'vehicle.current.model')
                text = 'Mình đang đi Hyundai Tucson.'
                semantic = {'schema_version': '2', 'sentiment': 'NEU', 'sarcasm': 'absent',
                    'scope': 'in_scope', 'requests': [{'topic': 'social', 'desired_outcome': 'question',
                        'polarity': 'positive'}], 'operations': [{'op': 'SOCIAL_ACK',
                        'evidence_excerpt': customer, 'request_ref': None, 'source_message_id': None}],
                    'uncertainties': [], 'intent_labels': ['social'], 'category': 'social', 'tags': []}
                body = {'action': 'final', 'payload': {'schema_version': 'persona_turn_v1',
                    'semantic': semantic, 'reply': {'reply_text': text, 'fact_ids': [fact['fact_id']],
                        'memory_ids': [], 'claims': [{'text': text, 'subject': 'persona',
                            'predicate': 'vehicle.current.model', 'fact_ids': [fact['fact_id']],
                            'memory_ids': [], 'message_ids': []}], 'product_claims': [],
                        'fact_proposals': [], 'asks_question': False, 'question': None},
                    'diagnostics': None, 'peer_interpretation': {'speaker_intent': 'social',
                        'address_term': 'bạn', 'persona_topic': 'vehicle_current',
                        'asks_persona_question': True, 'claims': [], 'evidence_excerpt': customer}}}
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1', 'message': body},
                'attempts': 1, 'usage': {}, 'cost_usd_est': 0.001, 'cost_unknown': False}
        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            code, result = service.process(request('dialogue-event', text=customer))
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'DRAFT', result)
        self.assertEqual(result['decision'].get('draft_text'), 'Mình đang đi Hyundai Tucson.', result)
        self.assertFalse(result['decision']['outbound_effects_enabled'])
        self.assertEqual(len(calls), 3, result)
        self.assertEqual(calls[2][1][-1]['tool_result']['status'], 'FOUND')
        self.assertEqual(budget_ledger.totals(service.budget_path)[:3], (3, 0.003, 0))

    def test_api_product_reply_uses_retrieved_current_fact(self):
        from datetime import datetime, timedelta, timezone

        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        now = datetime.now(timezone.utc)
        service.engine.facts.facts.append({'id': 'catalog-creta-standard-price', 'type': 'list_price',
            'vehicle': 'Creta', 'variant': 'Tiêu chuẩn', 'location': None,
            'value': 599_000_000, 'unit': 'VND', 'source': 'verified_catalog',
            'valid_from': (now - timedelta(days=1)).date().isoformat(),
            'valid_to': (now + timedelta(days=30)).date().isoformat(), 'ttl_hours': 24,
            'verified_at': now, 'version': 2})
        service.engine.facts.touch()
        customer = 'Giá Creta Tiêu chuẩn bao nhiêu?'
        price = 'Giá Creta Tiêu chuẩn là 599 triệu đồng.'
        plan = {'intents': [{'kind': 'product_question', 'subject': 'customer',
            'normalized': 'hỏi giá Creta Tiêu chuẩn', 'evidence': customer,
            'context_source': 'current_comment', 'certainty': 'explicit'}],
            'priority_order': [0], 'unresolved': [], 'objective': 'Trả lời giá từ nguồn hiện hành',
            'next_action': 'answer', 'question': None, 'handoff_reason': None}
        semantic = {'schema_version': '2', 'sentiment': 'NEU', 'sarcasm': 'absent',
            'scope': 'in_scope', 'requests': [{'topic': 'price', 'desired_outcome': 'list_price',
                'polarity': 'positive'}], 'operations': [{'op': 'START_REQUEST',
                'evidence_excerpt': customer, 'request_ref': 'topic:price', 'topic': 'price',
                'request': None, 'slot': None, 'source_message_id': None}],
            'uncertainties': [], 'intent_labels': ['price'], 'category': 'sales', 'tags': []}
        final = {'schema_version': 'persona_turn_v1', 'semantic': semantic,
            'reply': {'reply_text': price, 'fact_ids': [], 'memory_ids': [], 'claims': [],
                'product_claims': [{'text': price, 'product_fact_id': 'catalog-creta-standard-price',
                    'product_type': 'list_price', 'effective_at': service.engine.facts.facts[-1]['valid_from'],
                    'vehicle': 'Creta', 'variant': 'Tiêu chuẩn', 'location': None}],
                'fact_proposals': [], 'asks_question': False, 'question': None},
            'diagnostics': None, 'peer_interpretation': {'speaker_intent': 'unknown',
                'address_term': None, 'persona_topic': None, 'asks_persona_question': False,
                'claims': [], 'evidence_excerpt': customer}}

        def tool(action, payload):
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1',
                'message': {'action': action, 'payload': payload}}, 'attempts': 1,
                'usage': {}, 'cost_usd_est': 0.001, 'cost_unknown': False}

        def provider(_context, transcript, _schema, _timeout, **_kwargs):
            if not transcript:
                return tool('plan', plan)
            if transcript[-2]['message']['action'] == 'plan':
                return tool('retrieve', {'kind': 'product_facts', 'topic': None, 'query': None,
                    'keys': ['list_price'], 'entities': {'vehicle': 'Creta', 'location': None, 'people': []},
                    'time_start': None, 'time_end': None, 'variant': 'Tiêu chuẩn'})
            self.assertEqual(transcript[-1]['tool_result']['status'], 'FOUND')
            self.assertEqual(transcript[-1]['tool_result']['facts'][0]['source'], 'verified_catalog')
            return tool('final', final)

        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            code, result = service.process(request('product-fact-event', text=customer))
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'DRAFT', result)
        self.assertEqual(result['decision']['draft_text'], price)
        self.assertEqual([step['action'] for step in result['trace']['tool_trace']],
            ['plan', 'retrieve', 'final'])

    def test_api_tool_records_customer_fact_under_fenced_actor_scope(self):
        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        def provider(context, transcript, schema, timeout, **kwargs):
            text = 'Cuối tuần mình hay đi xa'
            body = (plan_action(text, 'share_need') if not transcript else
                {'action': 'record_customer_facts', 'payload': {'facts': [{
                'predicate': 'usage_need', 'value': 'đi xa', 'evidence_excerpt': 'đi xa',
                'subject': 'customer', 'certainty': 'explicit'}]}} if len(transcript) == 2 else
                {'action': 'abstain', 'payload': {'reason': 'Chưa có lời đáp phù hợp'}})
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1', 'message': body},
                'attempts': 1, 'usage': {}, 'cost_usd_est': 0.001, 'cost_unknown': False}
        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            code, result = service.process(request('fact-record', root_id='root-1', current_id='message-1',
                text='Cuối tuần mình hay đi xa', root_text='Xin chào'))
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'REVIEW')
        rows = service.store.conn.execute('SELECT actor_id,predicate,value,evidence,certainty '
            'FROM persona_customer_facts WHERE page_id=? AND post_id=? AND thread_root_id=?',
            ('brand-01', 'post-01', 'root-1')).fetchall()
        self.assertEqual([tuple(row) for row in rows], [('A', 'usage_need', 'đi xa', 'đi xa', 'explicit')])

    def test_api_new_story_is_persisted_before_model_final_and_not_marked_told(self):
        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        customer = 'Bạn có chuyện nào đáng nhớ không?'
        story = 'Mình từng ghé thăm một khu vườn yên tĩnh.'
        calls = []
        def provider(context, transcript, schema, timeout, **kwargs):
            calls.append(transcript)
            entities = {'vehicle': None, 'location': None, 'people': []}
            last_action = transcript[-2].get('message', {}).get('action') if len(transcript) >= 2 else None
            if not transcript:
                body = plan_action(customer)
            elif last_action == 'plan':
                body = {'action': 'retrieve', 'payload': {'kind': 'persona_memory', 'topic': 'outing',
                    'query': None, 'keys': [], 'entities': entities, 'time_start': None,
                    'time_end': None, 'variant': None}}
            elif last_action == 'retrieve':
                self.assertEqual(transcript[-1]['tool_result']['status'], 'NOT_FOUND')
                body = {'action': 'propose_memory', 'payload': {'proposal_id': 'proposal-1',
                    'idempotency_key': 'event-story-1', 'persona_version': context['persona_version'],
                    'topic': 'outing', 'event': story, 'time_start': None, 'time_end': None,
                    'entities': entities, 'base_fact_ids': [], 'new_details': [], 'relation': None,
                    'vehicle_use': 'unknown'}}
            else:
                accepted = transcript[-1]['tool_result']
                self.assertEqual(accepted['status'], 'ACCEPTED')
                memory_id = accepted['memory_id']
                self.assertEqual(service.store.conn.execute('SELECT status FROM persona_stories WHERE memory_id=?',
                    (memory_id,)).fetchone()[0], 'ACTIVE')
                semantic = {'schema_version': '2', 'sentiment': 'NEU', 'sarcasm': 'absent', 'scope': 'in_scope',
                    'requests': [{'topic': 'social', 'desired_outcome': 'question', 'polarity': 'positive'}],
                    'operations': [{'op': 'SOCIAL_ACK', 'evidence_excerpt': customer,
                        'request_ref': None, 'source_message_id': None}], 'uncertainties': [],
                    'intent_labels': ['social'], 'category': 'social', 'tags': []}
                body = {'action': 'final', 'payload': {'schema_version': 'persona_turn_v1', 'semantic': semantic,
                    'reply': {'reply_text': story, 'fact_ids': [], 'memory_ids': [memory_id],
                        'claims': [{'text': story, 'subject': 'persona', 'predicate': 'gen.memory',
                            'fact_ids': [], 'memory_ids': [memory_id], 'message_ids': []}],
                        'product_claims': [], 'fact_proposals': [],
                        'asks_question': False, 'question': None}, 'diagnostics': None,
                    'peer_interpretation': {'speaker_intent': 'social', 'address_term': 'bạn',
                        'persona_topic': 'travel_story', 'asks_persona_question': True,
                        'claims': [], 'evidence_excerpt': customer}}}
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1', 'message': body},
                'attempts': 1, 'usage': {}, 'cost_usd_est': 0.001, 'cost_unknown': False}
        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            data = request('new-story', text=customer)
            code, result = service.process(data)
            replay_code, replay = service.process(data)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'DRAFT', result)
        self.assertEqual(result['decision']['draft_text'], story)
        self.assertEqual(result['trace']['conversation_plan']['intents'][0]['kind'], 'persona_question')
        self.assertEqual([step['action'] for step in result['trace']['tool_trace']],
            ['plan', 'retrieve', 'propose_memory', 'final'])
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay_code, 200)
        self.assertEqual(len(calls), 4)
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM persona_stories').fetchone()[0], 1)
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM persona_story_deliveries').fetchone()[0], 0)

    def _seed_bound_trip(self, service):
        from app.persona_stories import StoryStore
        repo = service.engine.persona_store
        snapshot = repo.get_binding_snapshot('brand-01', 'post-01')
        vehicle = next(f for f in snapshot['facts'] if f['key'] == 'vehicle.current.model')
        event = 'Mình đi Đà Lạt bằng Hyundai Tucson.'
        result = StoryStore(repo).propose(snapshot, {'proposal_id': 'seed-trip',
            'idempotency_key': 'seed-trip-v1', 'persona_version': snapshot['release_id'],
            'topic': 'travel_story', 'event': event, 'time_start': '2025-04-01', 'time_end': None,
            'entities': {'vehicle': 'Hyundai Tucson', 'location': 'Đà Lạt', 'people': []},
            'base_fact_ids': [vehicle['fact_id']], 'new_details': [], 'relation': None,
            'vehicle_use': 'owned'})
        self.assertEqual(result['status'], 'ACCEPTED', result)
        return snapshot, vehicle, result['memory_id'], event

    def _run_bound_story_turn(self, service, event_id, *, propose):
        from scripts import budget_ledger
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        customer = 'Bác kể lại chuyến đi đó nhé?'
        snapshot, vehicle, memory_id, event = self._seed_bound_trip(service)
        proposal = {'proposal_id': 'retell-proposal', 'idempotency_key': event_id + '-retell',
            'persona_version': snapshot['release_id'], 'topic': 'travel_story',
            'event': 'Mình đã lái chiếc Hyundai Tucson tới Đà Lạt trong chuyến đi.',
            'time_start': '2025-04-01', 'time_end': None,
            'entities': {'vehicle': 'Hyundai Tucson', 'location': 'Đà Lạt', 'people': []},
            'base_fact_ids': [vehicle['fact_id']], 'new_details': [], 'relation': None,
            'vehicle_use': 'owned'}
        semantic = {'schema_version': '2', 'sentiment': 'NEU', 'sarcasm': 'absent',
            'scope': 'in_scope', 'requests': [{'topic': 'social', 'desired_outcome': 'question',
                'polarity': 'positive'}], 'operations': [{'op': 'SOCIAL_ACK',
                'evidence_excerpt': customer, 'request_ref': None, 'source_message_id': None}],
            'uncertainties': [], 'intent_labels': ['social'], 'category': 'social', 'tags': []}
        final = {'schema_version': 'persona_turn_v1', 'semantic': semantic,
            'reply': {'reply_text': event, 'fact_ids': [], 'memory_ids': [memory_id],
                'claims': [{'text': event, 'subject': 'persona', 'predicate': 'gen.memory',
                    'fact_ids': [], 'memory_ids': [memory_id], 'message_ids': []}],
                'product_claims': [], 'fact_proposals': [], 'asks_question': False, 'question': None},
            'diagnostics': None, 'peer_interpretation': {'speaker_intent': 'social',
                'address_term': 'bác', 'persona_topic': 'travel_story', 'asks_persona_question': True,
                'claims': [], 'evidence_excerpt': customer}}

        def tool(action, payload):
            return {'ok': True, 'message': {'schema_version': 'conversation_tools_v1',
                'message': {'action': action, 'payload': payload}}, 'attempts': 1,
                'usage': {'prompt_tokens': 10, 'completion_tokens': 8},
                'cost_usd_est': 0.001, 'cost_unknown': False}

        calls, comparisons = [], []
        def provider(context, transcript, _schema, _timeout, **_kwargs):
            calls.append((context, transcript))
            if context.get('_task') == 'story_comparison':
                comparisons.append(context)
                return {'ok': True, 'message': {'verdict': 'SAME',
                    'evidence': 'Cùng ngày và cùng xe, địa điểm; cách diễn đạt chỉ khác câu chữ.'},
                    'attempts': 1, 'usage': {'prompt_tokens': 6, 'completion_tokens': 3},
                    'cost_usd_est': 0.001, 'cost_unknown': False}
            if not transcript:
                return tool('plan', plan_action(customer)['payload'])
            action = transcript[-2]['message']['action']
            if action == 'plan':
                return tool('retrieve', {'kind': 'persona_memory', 'topic': 'travel_story',
                    'query': 'chuyến Đà Lạt bằng Hyundai Tucson', 'keys': [],
                    'entities': {'vehicle': 'Hyundai Tucson', 'location': 'Đà Lạt', 'people': []},
                    'time_start': '2025-04-01', 'time_end': None, 'variant': None})
            if action == 'retrieve':
                self.assertEqual(transcript[-1]['tool_result']['status'], 'FOUND')
                if propose:
                    return tool('propose_memory', proposal)
                return tool('final', final)
            self.assertEqual(action, 'propose_memory')
            self.assertEqual(transcript[-1]['tool_result']['status'], 'EXISTING')
            self.assertEqual(transcript[-1]['tool_result']['memory_id'], memory_id)
            return tool('final', final)

        data = request(event_id, text=customer, root_text=customer)
        data['post'] = {'id': 'post-01', 'text': 'Linh chia sẻ chuyến đi bằng Tucson.',
                        'vehicles': ['Hyundai Tucson']}
        with patch('app.adapters.luna.call_luna_tool', side_effect=provider):
            code, result = service.process(data)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'DRAFT', result)
        self.assertEqual(result['decision']['draft_text'], event)
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM persona_stories').fetchone()[0], 1)
        self.assertEqual(budget_ledger.totals(service.budget_path)[:3],
                         (3 + 2 * int(propose), 0.003 + 0.002 * int(propose), 0))
        return result, comparisons, memory_id

    def test_api_retrieved_story_with_dialog_metadata_can_be_cited(self):
        service = self.service(bind=True)
        result, comparisons, _ = self._run_bound_story_turn(service, 'story-retrieve', propose=False)
        self.assertEqual(comparisons, [])
        self.assertEqual([step['action'] for step in result['trace']['tool_trace']],
                         ['plan', 'retrieve', 'final'])

    def test_api_story_proposal_calls_semantic_comparator_and_reuses_event(self):
        service = self.service(bind=True)
        result, comparisons, memory_id = self._run_bound_story_turn(service, 'story-compare', propose=True)
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]['_schema_name'], 'story_comparison_v1')
        self.assertEqual([step['action'] for step in result['trace']['tool_trace']],
                         ['plan', 'retrieve', 'propose_memory', 'final', 'story_comparison'])
        self.assertIn({'action': 'story_comparison', 'status': 'SAME',
            'candidate': 'retell-proposal', 'memory_id': memory_id}, result['trace']['tool_trace'])


if __name__ == '__main__':
    unittest.main()
