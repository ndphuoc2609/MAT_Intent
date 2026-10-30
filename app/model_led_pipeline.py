"""Shared server-bound model/tool pipeline; callers own delivery and budget."""
from __future__ import annotations

import copy
from .customer_facts import list_customer_facts, record_customer_facts
from .persona_fact_policy import select_facts
from .persona_runtime import digest, peer_plan_from_signals, validated_peer_signals
from .persona_stories import StoryStore
from .semantics import attach_context_provenance, validate_semantic
from .tool_orchestrator import orchestrate


def run_model_led(engine, event, postcard, engine_context, model, budget_guard, *,
                  mutation_guard=None, bot_history=(), **limits):
    """Execute tools against the server's binding; never accept model scope IDs."""
    repo = engine.persona_store
    scope = {k: event.get(k) for k in ('page_id', 'post_id', 'thread_root_id', 'actor_id')}
    try:
        with engine.store.lock:
            if not repo or not postcard.get('persona_binding'):
                return {'status': 'ABSTAIN', 'reason': 'BINDING_UNAVAILABLE'}
            if engine.kill_switch or engine.store.blocks(scope['page_id'], scope['actor_id']) & {'all', 'public_reply'}:
                return {'status': 'ABSTAIN', 'reason': 'BLOCKED'}
            handoff = engine.store.conn.execute('SELECT 1 FROM handoffs h JOIN requests r ON r.id=h.request_id '
                'WHERE h.released_at IS NULL AND r.page_id=? AND r.post_id=? AND r.actor_id=?',
                (scope['page_id'], scope['post_id'], scope['actor_id'])).fetchone()
            if handoff:
                return {'status': 'ABSTAIN', 'reason': 'HANDOFF'}
            snapshot = repo.get_binding_snapshot(scope['page_id'], scope['post_id'])
            generation = engine.persona_generation_context(event, postcard, None)
            if not snapshot or not generation:
                return {'status': 'ABSTAIN', 'reason': 'BINDING_UNAVAILABLE'}
            customer_facts = list_customer_facts(repo, event)
    except Exception:
        return {'status': 'ABSTAIN', 'reason': 'CONTEXT_UNAVAILABLE'}
    stories = StoryStore(repo)
    with engine.store.lock:
        told = stories.told(snapshot, scope)
    if told.get('status') == 'UNKNOWN':
        return {'status': 'ABSTAIN', 'reason': 'STORY_CONTEXT_UNAVAILABLE'}
    told_ids = {item['memory_id'] for item in told.get('deliveries', [])}
    supplied_stories = {}
    customer_texts = [event['text']] + [m['text'] for m in generation.get('messages', [])
                     if m.get('actor_id') == scope['actor_id'] and isinstance(m.get('text'), str)]

    def retrieve(args):
        if args['kind'] == 'persona_memory':
            with engine.store.lock:
                result = stories.retrieve(snapshot, topic=args['topic'], query=args['query'], entities=args['entities'],
                    time_start=args['time_start'], time_end=args['time_end'])
                for memory in result.get('memories', []):
                    memory['already_told_to_customer'] = memory['memory_id'] in told_ids
            for story in result.get('memories', []): supplied_stories[story['memory_id']] = story
            return result
        if args['kind'] == 'persona_facts':
            needs = {'keys': args['keys'], 'topics': [args['topic']] if args['topic'] else []}
            rows = select_facts([f for f in generation['facts'] if not f.get('generated')], needs)
            return {'status': 'FOUND' if rows else 'NOT_FOUND', 'facts': rows}
        if args['kind'] == 'product_facts':
            kinds = {'list_price', 'promo', 'stock', 'spec', 'warranty', 'dealer'}
            if len(args['keys']) != 1 or args['keys'][0] not in kinds:
                return {'status': 'UNKNOWN', 'reason': 'UNSUPPORTED_PRODUCT_QUERY'}
            from .facts import Facts
            rows = [fact for fact in engine.facts.facts
                if fact.get('source') != 'SAMPLE' and fact.get('type') == args['keys'][0]]
            if not rows:
                return {'status': 'UNKNOWN', 'reason': 'PRODUCT_SOURCE_UNAVAILABLE', 'facts': []}
            product_facts = Facts(rows, verified_at=engine.facts.verified_at)
            fresh, stale, conflict = product_facts.query(args['keys'][0],
                vehicle=args['entities'].get('vehicle'), variant=args['variant'],
                location=args['entities'].get('location'), now=engine.clock.now())
            if conflict: return {'status': 'CONFLICT', 'facts': []}
            return {'status': 'FOUND' if fresh else 'NOT_FOUND', 'facts': fresh}
        return {'status': 'UNKNOWN'}

    def mutate(operation):
        if not callable(mutation_guard):
            return {'status': 'UNKNOWN', 'reason': 'MUTATION_GUARD_REQUIRED'}
        return mutation_guard(operation)

    def propose(args):
        result = mutate(lambda: stories.propose(snapshot, args, customer_texts=customer_texts))
        if not isinstance(result, dict):
            return {'status': 'UNKNOWN', 'reason': 'MUTATION_RESULT_INVALID'}
        story = result.get('memory')
        if isinstance(story, dict): supplied_stories[story['memory_id']] = story
        return result

    def record(args):
        return mutate(lambda: record_customer_facts(repo, event, args['facts'], engine.clock.now().isoformat()))

    latest_generation = [generation]
    def validate_bound(turn):
        candidate, semantic = turn.get('reply_candidate'), turn.get('semantic')
        signals = validated_peer_signals(turn.get('peer_signals'), event['text'])
        enriched = attach_context_provenance(semantic, event, engine_context, postcard)
        evidence = [postcard.get('text', '')] + [m.get('text', '') for m in engine_context.get('recent', [])]
        evidence += list(engine_context.get('parent_texts') or [])
        if validate_semantic(enriched, event['text'], evidence):
            return {'status': 'REVIEW', 'reason': 'INVALID_SEMANTIC'}
        ctx = copy.deepcopy(generation)
        ctx['stories'] = list(supplied_stories.values())
        ctx['context_token'] = digest({k: v for k, v in ctx.items() if k != 'context_token'})
        pending = engine.store.open_pending_for(scope['actor_id'], page_id=scope['page_id'],
            post_id=scope['post_id'], thread_root_id=scope['thread_root_id'])
        if signals:
            plan = peer_plan_from_signals(signals, event['text'], engine.store.peer_state(event),
                engine.store.lead_profile(event), pending, model_led=True)
        else:
            state = engine.store.peer_state(event)
            plan = dict(call=state.get('address_term') or 'bạn', topic=None, hook_field=None,
                supersede=[], lead_updates=[], next_state=state, reason='model_interpretation_missing',
                recent=list(state.get('last_utterances') or []))
        checked = engine._generated_reply(event, postcard, semantic, plan, candidate, ctx, signals)
        if checked.get('status') == 'OK': latest_generation[0] = ctx
        return checked

    def validate(turn):
        if not callable(mutation_guard):
            return {'status': 'REVIEW', 'reason': 'MUTATION_GUARD_REQUIRED'}
        try:
            return mutation_guard(lambda: validate_bound(turn))
        except Exception:
            return {'status': 'REVIEW', 'reason': 'VALIDATION_CONTEXT_UNAVAILABLE'}

    recent = list(reversed(engine_context.get('recent', [])))
    thread_messages = [message for message in engine_context.get('messages', [])
        if message.get('role') in ('root', 'parent', 'root_parent') and isinstance(message.get('text'), str)]
    assistants = {turn.get('comment_id'): turn.get('response_text') for turn in bot_history
        if isinstance(turn, dict) and turn.get('actor_id') == scope['actor_id']
        and turn.get('outbound_permission') and isinstance(turn.get('response_text'), str)}
    prior_messages, seen_messages = [], set()
    for message in recent + thread_messages:
        if message.get('actor_id') != scope['actor_id'] or not isinstance(message.get('text'), str):
            continue
        message_id = message.get('message_id', message.get('comment_id'))
        if message_id in seen_messages:
            continue
        seen_messages.add(message_id)
        prior_messages.append(message)
    prior_messages.sort(key=lambda message: str(message.get('event_time') or ''))
    history = []
    for message in prior_messages:
        message_id = message.get('message_id', message.get('comment_id'))
        history.append({'role': 'customer', 'text': message['text'], 'message_id': message_id})
        if message_id in assistants:
            history.append({'role': 'assistant', 'text': assistants[message_id], 'message_id': message_id})
    shared = [dict(role=message.get('role'), actor_id=message.get('actor_id'), text=message.get('text'))
        for message in thread_messages if message.get('actor_id') != scope['actor_id']]
    model_context = {'persona_description': generation.get('persona_description', {}),
        'voice_guide': generation.get('voice_guide', {}), 'post': postcard.get('text', ''),
        'shared_context': shared, 'history': history,
        'customer_facts': customer_facts, 'pending': engine_context.get('pending', []),
        'customer_profile': engine.store.lead_profile(event),
        'persona_stories_already_told': sorted(told_ids),
        'peer_state': generation.get('peer_state', {}), 'channel': generation.get('channel', 'public')}
    result = orchestrate({'scope': scope, 'persona_uuid': snapshot['persona_uuid'],
        'release_id': snapshot['release_id'], 'current_text': event['text'], 'model_context': model_context},
        model, {'retrieve': retrieve, 'propose_memory': propose, 'record_customer_facts': record},
        validate, budget_guard=budget_guard, **limits)
    result['generation_context'] = latest_generation[0]
    return result
