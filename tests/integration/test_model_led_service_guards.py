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
from meta_services.runtime import MetaRuntime
from tests.unit.test_intent_api import request
from tests.unit.test_meta_runtime import FakeGraph


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

    def test_meta_flag_fails_closed_without_inbox_lease_fencing(self):
        graph = FakeGraph()
        with patch.dict(os.environ, {'MAT_INTENT_MODEL_LED_ENABLED': 'true'}):
            runtime = MetaRuntime(graph, app_secret='secret', verify_token='verify', service_api_key='key',
                outbound_enabled=True, db_path=Path(self.temp.name) / 'meta.db',
                budget_path=Path(self.temp.name) / 'meta-budget.jsonl',
                trace_path=Path(self.temp.name) / 'meta-trace.jsonl',
                model_call=lambda *_a, **_kw: self.fail('provider must not run'))
        self.addCleanup(runtime.close)
        payload = {'object': 'page', 'entry': [{'id': '123', 'changes': [{'field': 'feed', 'value': {
            'item': 'comment', 'verb': 'add', 'comment_id': '456_1', 'post_id': '123_456',
            'parent_id': '123_456', 'from': {'id': '999'}, 'message': 'Cảm ơn'}}]}]}
        self.assertEqual(runtime.enqueue_webhook(payload), 1)
        self.assertTrue(runtime.process_next())
        self.assertEqual(graph.replies, [])
        row = runtime.inbox.list()[0]
        self.assertIn('REVIEW_MODEL_LED_FENCING_UNAVAILABLE', row['result']['routing'])

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
                            'memory_ids': [], 'message_ids': []}], 'asks_question': False, 'question': None},
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
        self.assertEqual(len(calls), 2, result)
        self.assertEqual(calls[1][1][-1]['tool_result']['status'], 'FOUND')
        self.assertEqual(budget_ledger.totals(service.budget_path)[:3], (2, 0.002, 0))

    def test_api_tool_records_customer_fact_under_fenced_actor_scope(self):
        service = self.service(bind=True)
        service.budget_max_calls, service.budget_max_cost, service.budget_reserve = 1000, 5.0, 0.1
        def provider(context, transcript, schema, timeout, **kwargs):
            body = ({'action': 'record_customer_facts', 'payload': {'facts': [{
                'predicate': 'usage_need', 'value': 'đi xa', 'evidence_excerpt': 'đi xa',
                'subject': 'customer', 'certainty': 'explicit'}]}} if not transcript
                else {'action': 'abstain', 'payload': {'reason': 'Chưa có lời đáp phù hợp'}})
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
            if not transcript:
                body = {'action': 'retrieve', 'payload': {'kind': 'persona_memory', 'topic': 'outing',
                    'query': None, 'keys': [], 'entities': entities, 'time_start': None,
                    'time_end': None, 'variant': None}}
            elif len(transcript) == 2:
                self.assertEqual(transcript[-1]['tool_result']['status'], 'NOT_FOUND')
                body = {'action': 'propose_memory', 'payload': {'proposal_id': 'proposal-1',
                    'idempotency_key': 'event-story-1', 'persona_version': context['persona_version'],
                    'topic': 'outing', 'event': story, 'time_start': None, 'time_end': None,
                    'entities': entities, 'base_fact_ids': [], 'new_details': [], 'relation': None}}
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
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay_code, 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM persona_stories').fetchone()[0], 1)
        self.assertEqual(service.store.conn.execute('SELECT COUNT(*) FROM persona_story_deliveries').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
