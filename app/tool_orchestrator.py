"""Bounded structured model/tool loop. Authority and I/O are supplied by the caller."""
from __future__ import annotations

import copy
import json
import math
import re
import time
from datetime import date, datetime

from .persona_turn import PERSONA_PREDICATES, persona_turn_model_schema, parse_persona_turn
from .persona_fact_policy import NAMESPACES

VERSION = 'conversation_tools_v1'
SCOPE = ('page_id', 'post_id', 'thread_root_id', 'actor_id')
FINAL_PREDICATES = PERSONA_PREDICATES | {f'gen.{namespace}' for namespace in NAMESPACES}


def _obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def _text(maximum=200):
    return {'type': 'string', 'minLength': 1, 'maxLength': maximum}


def _nullable(value):
    return {'anyOf': [value, {'type': 'null'}]}


def _array(item, limit=20):
    return {'type': 'array', 'items': item, 'maxItems': limit}


def conversation_tool_schema():
    entity = _obj({'vehicle': _nullable(_text()), 'location': _nullable(_text()),
                   'people': _array(_text(), 8)})
    retrieve = _obj({'kind': {'enum': ['persona_memory', 'persona_facts', 'product_facts']},
        'topic': _nullable(_text()), 'query': _nullable(_text(500)), 'keys': _array(_text()),
        'entities': entity, 'time_start': _nullable(_text(10)), 'time_end': _nullable(_text(10)),
        'variant': _nullable(_text())})
    relation = _obj({'type': {'enum': ['retell', 'addendum']}, 'memory_id': _text(),
        'base_version': _nullable({'type': 'integer', 'minimum': 0})})
    proposal = _obj({'proposal_id': _text(), 'idempotency_key': _text(), 'persona_version': _text(),
        'topic': _text(), 'event': _text(1000), 'time_start': _nullable(_text(10)),
        'time_end': _nullable(_text(10)), 'entities': entity, 'base_fact_ids': _array(_text()),
        'new_details': _array(_text(500)), 'relation': _nullable(relation),
        'vehicle_use': {'enum': ['owned', 'borrowed', 'test_drive', 'unknown']}})
    customer = _obj({'predicate': _text(), 'value': _text(500), 'evidence_excerpt': _text(500),
        'subject': {'enum': ['customer']}, 'certainty': {'enum': ['explicit', 'inferred', 'unknown']}})
    intent = _obj({'kind': {'enum': ['persona_question', 'product_question', 'share_need',
            'pending_answer', 'social', 'buying_interest', 'transaction', 'correction',
            'decline', 'third_party', 'handoff', 'other']},
        'subject': {'enum': ['customer', 'persona', 'third_party', 'unclear']},
        'normalized': _text(240), 'evidence': _text(500),
        'context_source': {'enum': ['current_comment', 'post', 'history', 'unclear']},
        'certainty': {'enum': ['explicit', 'inferred', 'unknown']}})
    plan = _obj({'intents': _array(intent, 12),
        'priority_order': _array({'type': 'integer', 'minimum': 0, 'maximum': 11}, 12),
        'unresolved': _array(_text(200), 8), 'objective': _text(240),
        'next_action': {'enum': ['answer', 'ask', 'clarify', 'continue', 'stop', 'handoff']},
        'question': _nullable(_text(240)), 'handoff_reason': _nullable(_text(160))})
    variants = []
    for action, payload in (
            ('plan', plan),
            ('retrieve', retrieve), ('propose_memory', proposal),
            ('record_customer_facts', _obj({'facts': _array(customer, 8)})),
            ('final', persona_turn_model_schema(persona_predicates=FINAL_PREDICATES, proposals=True)),
            ('abstain', _obj({'reason': _text(500)}))):
        variants.append(_obj({'action': {'enum': [action]}, 'payload': payload}))
    return _obj({'schema_version': {'enum': [VERSION]}, 'message': {'anyOf': variants}})


def _valid_plan(value, current_text):
    if not isinstance(value, dict) or set(value) != {'intents', 'priority_order', 'unresolved',
            'objective', 'next_action', 'question', 'handoff_reason'}:
        return False
    intents, order = value['intents'], value['priority_order']
    if (not isinstance(intents, list) or len(intents) > 12 or not isinstance(order, list)
            or len(order) != len(set(order)) or any(type(i) is not int or i < 0 or i >= len(intents) for i in order)
            or not isinstance(value['unresolved'], list) or len(value['unresolved']) > 8
            or any(not isinstance(item, str) or not item.strip() for item in value['unresolved'])
            or not isinstance(value['objective'], str) or not value['objective'].strip()):
        return False
    for item in intents:
        if (not isinstance(item, dict) or set(item) != {'kind', 'subject', 'normalized', 'evidence',
                'context_source', 'certainty'} or not isinstance(item['evidence'], str)
                or not item['evidence'] or item['evidence'] not in current_text):
            return False
    action, question = value['next_action'], value['question']
    if action in ('ask', 'clarify'):
        if not isinstance(question, str) or not question.strip() or len(question) > 240 or question.count('?') > 1:
            return False
    elif question is not None:
        return False
    if action == 'handoff':
        if (not isinstance(value['handoff_reason'], str) or not value['handoff_reason'].strip()
                or not any(item['kind'] == 'handoff' and item['subject'] == 'customer'
                    and item['certainty'] == 'explicit' for item in intents)):
            return False
    elif value['handoff_reason'] is not None:
        return False
    return True


def _matches(value, schema):
    if 'anyOf' in schema:
        return any(_matches(value, item) for item in schema['anyOf'])
    if 'enum' in schema and value not in schema['enum']:
        return False
    kind = schema.get('type')
    if kind == 'null': return value is None
    if kind == 'boolean': return type(value) is bool
    if kind == 'number':
        if type(value) not in (int, float): return False
        try:
            if not math.isfinite(value): return False
        except OverflowError:
            return False
        return value >= schema.get('minimum', value) and value <= schema.get('maximum', value)
    if kind == 'integer': return type(value) is int
    if kind == 'string':
        return (isinstance(value, str) and len(value) >= schema.get('minLength', 0)
                and len(value) <= schema.get('maxLength', 10000)
                and ('pattern' not in schema or re.search(schema['pattern'], value) is not None))
    if kind == 'array':
        return (isinstance(value, list) and len(value) >= schema.get('minItems', 0)
                and len(value) <= schema.get('maxItems', 100) and
                all(_matches(item, schema.get('items', {})) for item in value))
    if kind == 'object':
        if not isinstance(value, dict): return False
        props = schema.get('properties', {})
        if not set(schema.get('required', ())) <= value.keys(): return False
        if schema.get('additionalProperties') is False and not value.keys() <= props.keys(): return False
        return all(key not in props or _matches(item, props[key]) for key, item in value.items())
    return kind is None


def _public(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()
                if k not in ('source_ref', 'consent_ref', 'persona_uuid', 'release_id', 'scope')}
    if isinstance(value, list): return [_public(item) for item in value]
    return value


def orchestrate(context, model, tools, validate_final, *, budget_guard, max_rounds=6,
                timeout=15, total_timeout=90, max_tool_calls=5):
    """Run model/tool exchanges. Callbacks are scoped by the server, never by model IDs.

    model receives (public_context, messages, json_schema, timeout) and returns a
    provider result with ok/message/attempts/usage/cost_usd_est/cost_unknown.
    Tool callbacks receive only validated business payloads and return status objects.
    """
    result = {'status': 'ABSTAIN', 'reason': None, 'final': None, 'plan': None, 'trace': [], 'attempts': 0,
              'usage': {}, 'cost_usd_est': 0.0, 'cost_unknown': False}
    if (not isinstance(context, dict) or not isinstance(context.get('scope'), dict)
            or any(not isinstance(context['scope'].get(k), str) or not context['scope'][k] for k in SCOPE)
            or not isinstance(context.get('persona_uuid'), str) or not context['persona_uuid']
            or not isinstance(context.get('release_id'), str) or not context['release_id']
            or not isinstance(context.get('current_text'), str) or not context['current_text'].strip()
            or not callable(model) or not callable(validate_final) or not callable(budget_guard)
            or not isinstance(tools, dict)):
        result['reason'] = 'BINDING_UNAVAILABLE'; return result
    if (type(max_rounds) is not int or not 1 <= max_rounds <= 20 or type(max_tool_calls) is not int
            or not 0 <= max_tool_calls <= 20 or type(timeout) not in (int, float) or timeout <= 0
            or not math.isfinite(timeout) or type(total_timeout) not in (int, float)
            or not math.isfinite(total_timeout) or total_timeout <= 0):
        result['reason'] = 'LIMITS_INVALID'; return result
    schema = conversation_tool_schema()
    public_context = _public(copy.deepcopy(context.get('model_context') or {}))
    if not isinstance(public_context, dict):
        result['reason'] = 'CONTEXT_INVALID'; return result
    public_context['persona_version'] = context['release_id']
    public_context['current_comment'] = context['current_text']
    public_context['limits'] = {'max_rounds': max_rounds, 'max_tool_calls': max_tool_calls}
    messages, searches = [], {}
    deadline = time.monotonic() + total_timeout
    tool_count = 0
    conversation_plan = None
    for _ in range(max_rounds):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result['reason'] = 'TIMEOUT'; break
        try:
            if budget_guard() is not True:
                result['reason'] = 'BUDGET_EXHAUSTED'; break
            call = model(copy.deepcopy(public_context), copy.deepcopy(messages), schema, min(timeout, remaining))
        except Exception:
            result.update(reason='MODEL_ERROR', attempts=None, cost_unknown=True, cost_usd_est=None)
            break
        if not isinstance(call, dict):
            result['reason'] = 'MODEL_ERROR'; break
        attempts = call.get('attempts')
        if type(attempts) is int and attempts > 1:
            result.update(reason='MODEL_ATTEMPTS_UNGUARDED', attempts=None, cost_unknown=True, cost_usd_est=None)
            break
        if type(attempts) is not int or attempts < 0:
            result.update(reason='MODEL_ATTEMPTS_UNKNOWN', attempts=None, cost_unknown=True, cost_usd_est=None)
            break
        elif result['attempts'] is not None:
            result['attempts'] += attempts
        usage = call.get('usage') or {}
        if isinstance(usage, dict):
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if type(usage.get(key)) is int and usage[key] >= 0:
                    result['usage'][key] = result['usage'].get(key, 0) + usage[key]
        cost = call.get('cost_usd_est')
        if call.get('cost_unknown', cost is None) or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            result['cost_unknown'] = True; result['cost_usd_est'] = None
        elif not result['cost_unknown']:
            result['cost_usd_est'] += cost
        if time.monotonic() > deadline:
            result['reason'] = 'TIMEOUT'; break
        if not call.get('ok'):
            result['reason'] = call.get('error_code') or 'MODEL_ERROR'; break
        envelope = call.get('message')
        if not _matches(envelope, schema):
            result['reason'] = 'INVALID_MODEL_MESSAGE'; break
        message = envelope['message']; action = message['action']; payload = message['payload']
        result['trace'].append({'action': action})
        if conversation_plan is None and action not in ('plan', 'abstain'):
            result['trace'][-1]['validation'] = 'PLAN_REQUIRED'
            messages.extend([envelope, {'tool_result': {'status': 'REJECTED', 'reason': 'PLAN_REQUIRED'}}])
            continue
        if action == 'abstain':
            result['reason'] = 'MODEL_ABSTAIN'; break
        if action == 'final':
            try:
                turn = parse_persona_turn(payload, current_text=context['current_text'], proposals=True,
                                          persona_predicates=FINAL_PREDICATES)
                if turn['reply_candidate'] is None:
                    raise ValueError('invalid reply')
                turn['conversation_plan'] = copy.deepcopy(conversation_plan)
                validation = validate_final(turn)
            except Exception:
                validation = {'status': 'REVIEW', 'reason': 'VALIDATION_ERROR'}
            if isinstance(validation, dict) and validation.get('status') == 'OK':
                result.update(status='FINAL', reason=None, final=turn, plan=copy.deepcopy(conversation_plan)); return result
            reason = validation.get('reason', 'INVALID_FINAL') if isinstance(validation, dict) else 'INVALID_FINAL'
            result['trace'][-1]['validation'] = reason
            messages.extend([envelope, {'tool_result': {'status': 'REJECTED', 'reason': reason}}])
            continue
        if tool_count >= max_tool_calls:
            result['reason'] = 'TOOL_LIMIT'; break
        tool_count += 1
        if action == 'plan':
            if conversation_plan is not None or not _valid_plan(payload, context['current_text']):
                answer = {'status': 'REJECTED', 'reason': 'INVALID_CONVERSATION_PLAN'}
            else:
                conversation_plan = copy.deepcopy(payload)
                public_context['conversation_plan'] = copy.deepcopy(payload)
                result['plan'] = copy.deepcopy(payload)
                answer = {'status': 'ACCEPTED', 'intent_count': len(payload['intents'])}
        elif action == 'propose_memory':
            if payload['persona_version'] != context['release_id']:
                answer = {'status': 'CONFLICT', 'reason': 'PERSONA_VERSION_MISMATCH'}
            elif searches.get(payload['topic']) not in ('NOT_FOUND', 'FOUND'):
                answer = {'status': 'UNKNOWN', 'reason': 'MEMORY_SEARCH_REQUIRED'}
            else:
                answer = None
        elif action == 'record_customer_facts' and any(
                not item['evidence_excerpt'] or item['evidence_excerpt'] not in context['current_text']
                for item in payload['facts']):
            answer = {'status': 'REJECTED', 'reason': 'EVIDENCE_MISMATCH'}
        else:
            answer = None
        if answer is None:
            try:
                handler = tools.get(action)
                answer = handler(copy.deepcopy(payload)) if callable(handler) else {'status': 'UNKNOWN'}
                if not isinstance(answer, dict) or not isinstance(answer.get('status'), str):
                    answer = {'status': 'UNKNOWN'}
                answer = _public(answer)
                if len(json.dumps(answer, ensure_ascii=False, allow_nan=False)) > 40000:
                    answer = {'status': 'UNKNOWN', 'reason': 'RESULT_TOO_LARGE'}
            except Exception:
                answer = {'status': 'UNKNOWN', 'reason': 'TOOL_ERROR'}
        if action == 'retrieve' and payload['kind'] == 'persona_memory' and payload['topic']:
            searches[payload['topic']] = answer['status'] if answer['status'] in ('FOUND', 'NOT_FOUND', 'CONFLICT') else 'UNKNOWN'
        if action == 'propose_memory' and answer['status'] in ('ACCEPTED', 'EXISTING'):
            searches[payload['topic']] = 'FOUND'
        result['trace'][-1]['status'] = answer['status']
        messages.extend([envelope, {'tool_result': answer}])
    if result['reason'] is None:
        result['reason'] = 'ROUND_LIMIT'
    return result
