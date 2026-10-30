import io
import json
import os
import unittest
from unittest.mock import patch

from app.adapters.luna import call_luna_tool
from app.tool_orchestrator import VERSION, conversation_tool_schema, orchestrate


def context():
    return {'scope': {'page_id': 'page', 'post_id': 'post', 'thread_root_id': 'root', 'actor_id': 'customer'},
            'persona_uuid': 'persona-a', 'release_id': 'release-a',
            'current_text': 'Cuối tuần bạn hay đi đâu?',
            'model_context': {'facts': [], 'source_ref': 'private-source'}}


def msg(action, payload):
    return {'schema_version': VERSION, 'message': {'action': action, 'payload': payload}}


def retrieval():
    return {'kind': 'persona_memory', 'topic': 'trip', 'query': 'cuối tuần', 'keys': [],
        'entities': {'vehicle': None, 'location': None, 'people': []},
        'time_start': None, 'time_end': None, 'variant': None}


def proposal():
    return {'proposal_id': 'p1', 'idempotency_key': 'key1', 'persona_version': 'release-a',
        'topic': 'trip', 'event': 'Một chuyến đi cuối tuần.', 'time_start': None, 'time_end': None,
        'entities': {'vehicle': None, 'location': None, 'people': []}, 'base_fact_ids': [],
        'new_details': [], 'relation': None}


def final():
    return {'schema_version': 'persona_turn_v1', 'semantic': {'schema_version': '2', 'sentiment': 'NEU',
        'sarcasm': 'absent', 'scope': 'in_scope', 'requests': [], 'operations': [],
        'uncertainties': [], 'intent_labels': [], 'category': None, 'tags': []},
        'reply': {'reply_text': 'Ừ, mình hiểu rồi.', 'fact_ids': [], 'memory_ids': [], 'claims': [],
                  'asks_question': False, 'question': None},
        'diagnostics': None, 'peer_interpretation': None}


def model_stream(messages, seen):
    def model(ctx, history, schema, timeout):
        seen.append((ctx, history, timeout))
        return {'ok': True, 'message': next(messages), 'attempts': 1,
                'cost_usd_est': 0.01, 'cost_unknown': False, 'usage': {'prompt_tokens': 10}}
    return model


class ToolOrchestrationTests(unittest.TestCase):
    def test_retrieve_propose_then_validate_final(self):
        calls, seen = [], []
        tools = {'retrieve': lambda q: {'status': 'NOT_FOUND'},
            'propose_memory': lambda p: calls.append(p) or
                {'status': 'ACCEPTED', 'memory_id': 'm1', 'source_ref': 'private'}}
        model = model_stream(iter([msg('retrieve', retrieval()), msg('propose_memory', proposal()),
                                   msg('final', final())]), seen)
        result = orchestrate(context(), model, tools, lambda turn: {'status': 'OK'}, budget_guard=lambda: True)
        self.assertEqual(result['status'], 'FINAL')
        self.assertEqual(result['attempts'], 3)
        self.assertAlmostEqual(result['cost_usd_est'], 0.03)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('source_ref', seen[0][0])
        self.assertNotIn('source_ref', seen[2][1][-1]['tool_result'])
        self.assertEqual(seen[0][0]['persona_version'], 'release-a')

    def test_database_error_cannot_authorize_creating_memory(self):
        calls = []
        model = model_stream(iter([msg('retrieve', retrieval()), msg('propose_memory', proposal()),
                                   msg('abstain', {'reason': 'chưa đủ dữ liệu'})]), [])
        tools = {'retrieve': lambda _: (_ for _ in ()).throw(RuntimeError('db down')),
                 'propose_memory': lambda p: calls.append(p)}
        result = orchestrate(context(), model, tools, lambda _: {'status': 'OK'}, budget_guard=lambda: True)
        self.assertEqual(calls, [])
        self.assertEqual(result['trace'][0]['status'], 'UNKNOWN')
        self.assertEqual(result['trace'][1]['status'], 'UNKNOWN')

    def test_other_persona_id_is_not_a_tool_parameter(self):
        illegal = retrieval(); illegal['persona_id'] = 'persona-b'
        result = orchestrate(context(), model_stream(iter([msg('retrieve', illegal)]), []),
            {'retrieve': lambda _: self.fail('must reject')}, lambda _: {'status': 'OK'}, budget_guard=lambda: True)
        self.assertEqual(result['reason'], 'INVALID_MODEL_MESSAGE')

    def test_version_and_evidence_are_checked_before_backend(self):
        wrong = proposal(); wrong['persona_version'] = 'release-b'
        customer = {'facts': [{'predicate': 'budget', 'value': '700 triệu',
            'evidence_excerpt': '700 triệu', 'subject': 'customer', 'certainty': 'explicit'}]}
        called = []
        model = model_stream(iter([msg('retrieve', retrieval()), msg('propose_memory', wrong),
            msg('record_customer_facts', customer), msg('abstain', {'reason': 'chưa rõ'})]), [])
        result = orchestrate(context(), model, {'retrieve': lambda _: {'status': 'NOT_FOUND'},
            'propose_memory': lambda _: called.append('memory'),
            'record_customer_facts': lambda _: called.append('customer')}, lambda _: {'status': 'OK'},
            budget_guard=lambda: True)
        self.assertEqual(called, [])
        self.assertEqual([item.get('status') for item in result['trace'][:3]], ['NOT_FOUND', 'CONFLICT', 'REJECTED'])

    def test_rejected_final_can_be_repaired_within_budget(self):
        seen = []
        model = model_stream(iter([msg('final', final()), msg('final', final())]), seen)
        checks = iter([{'status': 'REVIEW', 'reason': 'UNSUPPORTED'}, {'status': 'OK'}])
        result = orchestrate(context(), model, {}, lambda _: next(checks), budget_guard=lambda: True)
        self.assertEqual(result['status'], 'FINAL')
        self.assertEqual(seen[1][1][-1]['tool_result']['reason'], 'UNSUPPORTED')

    def test_limit_and_budget_are_fail_closed(self):
        called = []
        result = orchestrate(context(), lambda *a: called.append(a), {}, lambda _: {'status': 'OK'},
                             budget_guard=lambda: False)
        self.assertEqual(result['reason'], 'BUDGET_EXHAUSTED')
        self.assertEqual(called, [])
        result = orchestrate({}, lambda *a: called.append(a), {}, lambda _: {'status': 'OK'},
                             budget_guard=lambda: True)
        self.assertEqual(result['reason'], 'BINDING_UNAVAILABLE')
        self.assertEqual(called, [])

    def test_adapter_one_http_attempt_and_strict_schema(self):
        content = msg('abstain', {'reason': 'chưa rõ'})
        response = {'usage': {'cost': 0.001}, 'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(content, ensure_ascii=False)}}]}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('app.adapters.luna.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = call_luna_tool({}, [], conversation_tool_schema(), 5, budget_guard=lambda: True)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result['message'], content)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(payload['response_format']['json_schema']['name'], VERSION)

    def test_model_retry_attempts_cannot_bypass_per_call_budget(self):
        def model(*_):
            return {'ok': True, 'message': msg('abstain', {'reason': 'chưa rõ'}),
                    'attempts': 2, 'cost_usd_est': 0.01, 'cost_unknown': False}
        result = orchestrate(context(), model, {}, lambda _: {'status': 'OK'}, budget_guard=lambda: True)
        self.assertEqual(result['reason'], 'MODEL_ATTEMPTS_UNGUARDED')
        self.assertTrue(result['cost_unknown'])

    def test_adapter_does_not_call_provider_when_budget_denied(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}), \
             patch('app.adapters.luna.urllib.request.build_opener') as opener:
            result = call_luna_tool({}, [], conversation_tool_schema(), 5, budget_guard=lambda: False)
        opener.assert_not_called()
        self.assertEqual(result['error_code'], 'BUDGET_EXHAUSTED')

    def test_uncertain_budget_or_model_exception_never_counts_cost_as_zero(self):
        def model(*_): raise TimeoutError()
        result = orchestrate(context(), model, {}, lambda _: {'status': 'OK'}, budget_guard=lambda: True)
        self.assertEqual(result['reason'], 'MODEL_ERROR')
        self.assertIsNone(result['cost_usd_est'])
        self.assertIsNone(result['attempts'])


if __name__ == '__main__':
    unittest.main()
