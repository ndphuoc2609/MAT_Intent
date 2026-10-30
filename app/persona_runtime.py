"""Generated persona drafts and evidence-bound operator delivery.

The model proposes words. This module never grants permission from those words.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import copy
from contextlib import contextmanager

from .lead_flow import address, FIELDS
from .normalize import norm

SCOPE = ('page_id', 'post_id', 'thread_root_id', 'actor_id')


def _story_claim_compatible(text, story):
    """Accept a conservative paraphrase without changing stored story facts."""
    if not isinstance(text, str) or not isinstance(story.get('event'), str):
        return False
    source, reply = norm(story['event']), norm(text)
    if source == reply:
        return True
    if re.search(r'[.!?;,]\s+\S', text.strip()[:-1]):
        return False
    # shortcut: lexical checks cannot prove entailment; reject added emotion, cause or excess detail.
    from .persona_generation import _CAUSE_RE, _EMOTION_RE, _FAMILY_RE, _SPOUSE_OPINION_RE
    for pattern in (_CAUSE_RE, _EMOTION_RE, _FAMILY_RE, _SPOUSE_OPINION_RE):
        if pattern.search(reply) and not pattern.search(source):
            return False
    entity_tokens = set()
    for key in ('vehicle', 'location'):
        value = story.get('entities', {}).get(key)
        if not value:
            continue
        aliases = {norm(value)}
        if key == 'vehicle' and norm(value).startswith('hyundai '):
            aliases.add(norm(value).removeprefix('hyundai '))
        if not any(re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', reply) for alias in aliases):
            return False
        entity_tokens.update(token for alias in aliases for token in alias.split())
    number = re.compile(r'(?<!\w)\d+(?:[.,/]\d+)*(?!\w)')
    if set(number.findall(reply)) - set(number.findall(source)):
        return False
    negation = re.compile(r'\b(?:khong|chua|chang|bao gio khong|never)\b')
    if bool(negation.search(source)) != bool(negation.search(reply)):
        return False
    stop = {'minh', 'toi', 'ban', 'anh', 'chi', 'em', 'bac', 'co', 'chu', 'la', 'cua',
            'voi', 'va', 'nhung', 'thi', 'ma', 'da', 'se', 'rat', 'mot', 'nhieu',
            'nay', 'do', 'kia', 'nhe', 'a', 'ha', 'bang', 'tren', 'duoi', 'o', 'den', 'tu'}
    words = lambda value: {word for word in re.findall(r'\w+', value)
                           if len(word) > 2 and word not in stop and word not in entity_tokens}
    source_words, reply_words = words(source), words(reply)
    overlap = source_words & reply_words
    travel_verbs = {'di', 'den', 'toi', 'ghe', 'tham', 'lai', 'chay', 'du lich'}
    source_verbs = {verb for verb in travel_verbs if re.search(r'(?<!\w)' + re.escape(verb) + r'(?!\w)', source)}
    reply_verbs = {verb for verb in travel_verbs if re.search(r'(?<!\w)' + re.escape(verb) + r'(?!\w)', reply)}
    verb_overlap = bool(source_verbs & reply_verbs or
                        source_verbs & {'di', 'den', 'toi', 'ghe', 'tham', 'lai', 'chay', 'du lich'}
                        and reply_verbs & {'di', 'den', 'toi', 'ghe', 'tham', 'lai', 'chay', 'du lich'})
    extra = reply_words - source_words
    if verb_overlap:
        extra -= {'di', 'den', 'toi', 'ghe', 'tham', 'lai', 'chay', 'du', 'lich'}
    return (bool(overlap or verb_overlap) and len(extra) <= 1 and
            (not source_words or len(overlap) / len(source_words) >= 0.4 or verb_overlap))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def validated_peer_signals(signals, text):
    if not isinstance(signals, dict) or not isinstance(signals.get('speaker_intent'), str) or signals.get('speaker_intent') not in {
        'social', 'owns_vehicle', 'buying_interest', 'switching_vehicle', 'declines',
        'transaction', 'correction', 'other_person', 'ai_identity', 'unknown'}:
        return None
    from .persona_query import KEY_REGISTRY
    if type(signals.get('asks_persona_question')) is not bool:
        return None
    topic = signals.get('persona_topic')
    if topic is not None and (not isinstance(topic, str) or topic not in KEY_REGISTRY):
        return None
    evidence = signals.get('evidence_excerpt')
    if not isinstance(evidence, str) or not evidence.strip() or evidence not in text:
        return None
    claims = signals.get('claims')
    if not isinstance(claims, list) or len(claims) > 8:
        return None
    supported = []
    for claim in claims:
        if not isinstance(claim, dict) or claim.get('predicate') not in (*FIELDS, 'ownership_status', 'purchase_intent'):
            return None
        excerpt, value = claim.get('evidence_excerpt'), claim.get('value')
        if not isinstance(excerpt, str) or not excerpt.strip() or excerpt not in text:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > 180:
            return None
        if claim['predicate'] in FIELDS and norm(value) not in norm(excerpt):
            continue
        supported.append(claim)
    return dict(signals, claims=supported)


def peer_plan_from_signals(signals, text, state, profile, pending, *, model_led=False):
    """Use evidence-checked semantic interpretation; no inference from reply prose."""
    ns = dict(state)
    term = signals.get('address_term')
    if term not in ('bác', 'anh', 'chị', 'chú', 'cô', 'bạn') or norm(term) not in norm(text).split():
        term = address('', state.get('address_term'))
    ns['address_term'] = term
    plan = dict(call=term, topic=None, hook_field=None, supersede=[], lead_updates=[],
                next_state=ns, reason='semantic_peer', recent=list(state.get('last_utterances') or []))
    intent = signals['speaker_intent']
    for claim in signals['claims']:
        if claim['predicate'] == 'ownership_status' and claim['value'] in ('owns_vehicle', 'does_not_own_vehicle', 'unknown'):
            ns['ownership_status'] = claim['value']
    updates = {c['predicate']: c['value'] for c in signals['claims'] if c['predicate'] in FIELDS}
    def close(status):
        plan['supersede'] = [(p, status) for p in pending]
    if intent == 'ai_identity':
        plan['topic'] = 'ai_disclosure'; ns['follow_up'] = 'stopped'; close('SUPERSEDED')
    elif intent == 'declines':
        plan['topic'] = 'decline_ack'; ns['follow_up'] = 'stopped'; ns['purchase_intent'] = 'not_interested'; close('REJECTED')
        plan['lead_updates'] = [(p['expected_slot'], None, 'DECLINED') for p in pending]
    elif intent == 'other_person':
        plan['topic'] = 'proxy_clarification'; plan['reason'] = 'semantic_other_person'; ns['purchase_intent'] = 'unknown'
        ns['follow_up'] = 'stopped'; close('SUPERSEDED')
        plan['lead_updates'] = [(f, None, 'DECLINED') for f, v in profile.items()
                                if v.get('post_id') in (None, '') and v.get('status') in ('ASKED', 'ANSWERED')]
    elif intent == 'correction':
        plan['topic'] = 'repair'; ns['follow_up'] = 'stopped'; close('SUPERSEDED')
        for f, value in updates.items():
            plan['lead_updates'].append((f, value, 'ANSWERED'))
    elif intent == 'owns_vehicle':
        plan['topic'] = 'ownership_repair' if pending or ns.get('purchase_intent') == 'exploring' else 'ownership_ack'
        ns.update(ownership_status='owns_vehicle', purchase_intent='unknown',
                  follow_up=state.get('follow_up') if model_led else 'stopped')
        close('SUPERSEDED')
        if 'current_vehicle' in updates:
            plan['lead_updates'].append(('current_vehicle', updates['current_vehicle'], 'ANSWERED'))
    elif intent in ('buying_interest', 'switching_vehicle'):
        plan['topic'] = 'switch' if intent == 'switching_vehicle' else 'interest'
        ns.update(purchase_intent='exploring', follow_up=None)
        if intent == 'switching_vehicle':
            ns['ownership_status'] = 'owns_vehicle'
        close('SUPERSEDED')
        for f, value in updates.items():
            plan['lead_updates'].append((f, value, 'ANSWERED'))
        after = dict(profile, **{f: {'status': 'ANSWERED'} for f in updates})
        if not model_led and not signals.get('asks_persona_question') and ns.get('asked_total', 0) < 3:
            field = next((f for f in ('interested_vehicle', 'purchase_timeline') if f not in after), None)
            plan['hook_field'] = field
    else:
        for f, value in updates.items():
            if f == 'current_vehicle' or pending or ns.get('purchase_intent') == 'exploring':
                plan['lead_updates'].append((f, value, 'ANSWERED'))
        matched = [p for p in pending if p.get('expected_slot') in updates]
        if matched:
            plan['supersede'] = [(p, 'ANSWERED') for p in matched]
            field = matched[0]['expected_slot']
            plan['topic'] = 'ack_' + field
            if not model_led and field == 'interested_vehicle' and ns.get('purchase_intent') == 'exploring' and 'purchase_timeline' not in profile:
                plan['hook_field'] = 'purchase_timeline'
        elif not signals.get('asks_persona_question'):
            plan['reason'] = 'social'
    if signals.get('asks_persona_question') or intent in ('declines', 'correction', 'other_person', 'owns_vehicle', 'ai_identity'):
        plan['hook_field'] = None
    return plan


class PersonaGenerationRuntime:
    def _attach_cited_stories(self, snapshot, context, candidate):
        """Rehydrate cited stories from the bound store; never trust model-provided IDs."""
        if not getattr(self, 'model_led_conversation', False):
            return snapshot, set(), None
        supplied = context.get('stories', [])
        if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
            return snapshot, set(), 'STORY_CONTEXT_INVALID'
        by_id = {item.get('memory_id'): item for item in supplied if isinstance(item.get('memory_id'), str)}
        if len(by_id) != len(supplied):
            return snapshot, set(), 'STORY_CONTEXT_INVALID'
        requested = candidate.get('memory_ids', []) if isinstance(candidate, dict) else []
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            return snapshot, set(), 'STORY_CONTEXT_INVALID'
        ids = set(requested) & set(by_id)
        if not ids:
            return snapshot, set(), None
        from .persona_stories import StoryStore
        from .persona_generation import _memory_checksum
        repository = getattr(self, 'story_store', None) or StoryStore(self.persona_store)
        facts, memories = list(snapshot.get('facts', [])), list(snapshot.get('memories', []))
        for memory_id in sorted(ids):
            story = repository.get(snapshot, memory_id)
            if (not isinstance(story, dict) or story != by_id[memory_id]
                    or story.get('status') != 'ACTIVE' or story.get('label') != 'fictional_generated'
                    or story.get('persona_uuid') != snapshot.get('persona_uuid')
                    or story.get('release_id') != snapshot.get('release_id')
                    or not isinstance(story.get('event'), str) or not story['event'].strip()):
                return snapshot, set(), 'STORY_UNAVAILABLE_OR_CHANGED'
            paraphrases = []
            for claim in candidate.get('claims', []):
                if isinstance(claim, dict) and memory_id in claim.get('memory_ids', []):
                    if (claim.get('predicate') != 'gen.memory'
                            or not _story_claim_compatible(claim.get('text'), story)):
                        return snapshot, set(), 'STORY_CLAIM_UNVERIFIED'
                    if norm(claim['text']) != norm(story['event']):
                        paraphrases.append(claim['text'])
            fact_id = 'story:' + memory_id
            key = 'gen.memory.s' + hashlib.sha256(memory_id.encode()).hexdigest()[:24]
            source = 'fictional_story:' + memory_id
            facts.append(dict(fact_id=fact_id, key=key, value=story['event'], value_type='text',
                source_kind='generated', source_ref=source, approval='generated', generated=True,
                aliases=paraphrases, tellable='public', persona_uuid=snapshot['persona_uuid'],
                release_id=snapshot['release_id']))
            memory = dict(memory_id=memory_id, persona_uuid=snapshot['persona_uuid'],
                release_id=snapshot['release_id'], topic=story['topic'], approval='generated',
                source_kind='generated', source_ref=source, label='fictional_generated', privacy='public',
                tellable='public', fact_ids=[fact_id], approved_summary=story['event'])
            memory['checksum'] = _memory_checksum(memory)
            memories.append(memory)
        return dict(snapshot, facts=facts, memories=memories), ids, None

    def _generation_story_sources(self, snapshot):
        # Retain revoked source links internally; filtered snapshots omit those memories.
        sources = self.persona_store._release_content(snapshot['release_id']).get('memories', [])
        return [m for m in sources
                if m.get('privacy') == 'public'
                and m.get('tellable') in (True, 'public')
                and m.get('approval') == ('fixture' if snapshot.get('fixture_only') else 'approved')
                and (snapshot.get('fixture_only') or m.get('consent_ref'))]

    def _uses_story_facts(self, snapshot, fact_ids):
        return any(set(fact_ids or []) & set(m.get('fact_ids', []))
                   for m in self._generation_story_sources(snapshot))

    def _story_delivery_binding(self, payload):
        context = payload.get('generation_context') or {}
        stories = context.get('stories') or []
        known_ids = {item.get('memory_id') for item in stories if isinstance(item, dict)}
        ids = set(payload.get('memory_ids') or ()) & known_ids
        if not ids:
            return None, set(), None
        scope = payload.get('persona_scope') or {}
        snapshot = self.persona_store.get_binding_snapshot(scope.get('page_id'), scope.get('post_id'))
        if (not snapshot or snapshot.get('persona_uuid') != payload.get('persona_uuid')
                or snapshot.get('release_id') != payload.get('release_id')):
            return None, ids, 'STORY_BINDING_CHANGED'
        from .persona_stories import StoryStore
        return snapshot, ids, getattr(self, 'story_store', None) or StoryStore(self.persona_store)

    def _reserve_memory_delivery(self, payload, now_iso):
        memory_ids = payload.get('memory_ids') or []
        if not memory_ids:
            return None
        snapshot, story_ids, repository = self._story_delivery_binding(payload)
        if story_ids:
            if not snapshot or len(story_ids) != 1 or len(memory_ids) != 1:
                return 'STORY_RESERVATION_UNAVAILABLE'
            if not repository.reserve(snapshot, payload.get('persona_scope') or {},
                                      payload.get('dedupe_key'), next(iter(story_ids))):
                return 'STORY_ALREADY_RESERVED_OR_REVOKED'
            return None
        reserve = getattr(self.persona_store, 'reserve_memory_delivery', None)
        if not callable(reserve):
            return 'MEMORY_RESERVATION_UNAVAILABLE'
        if not reserve(payload.get('persona_scope') or {}, memory_ids, payload.get('dedupe_key'),
                       payload.get('turn_ordinal', 0), now_iso):
            return 'MEMORY_ALREADY_RESERVED'
        return None

    def _start_memory_delivery(self, payload):
        if not payload.get('memory_ids'):
            return None
        snapshot, story_ids, repository = self._story_delivery_binding(payload)
        if story_ids:
            if not snapshot or not repository.start_delivery(snapshot, payload.get('dedupe_key')):
                return 'STORY_REVOKED_OR_RESERVATION_CANCELLED'
            return None
        start = getattr(self.persona_store, 'start_memory_delivery', None)
        if not callable(start):
            return 'MEMORY_RESERVATION_UNAVAILABLE'
        if not start(payload.get('dedupe_key')):
            return 'MEMORY_REVOKED_OR_RESERVATION_CANCELLED'
        return None

    def _generation_state_snapshot(self, event):
        key = tuple(event[k] for k in SCOPE)
        latest = self.store.conn.execute('SELECT COALESCE(MAX(ordinal),0) FROM persona_generation_turns WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?', key).fetchone()[0]
        return dict(engine=self._trace_state_snapshot(event), peer=self.store.peer_state(event),
                    lead=self.store.lead_profile(event), latest_turn=latest)

    def _generation_state_digest(self, event):
        return digest(self._generation_state_snapshot(event))

    def _generation_turn_ordinal(self, event, *, record=False):
        key = tuple(event[k] for k in SCOPE)
        existing = self.store.conn.execute('SELECT ordinal FROM persona_generation_turns WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=? AND comment_id=?', (*key, event['comment_id'])).fetchone()
        if existing:
            return existing[0]
        ordinal = self.store.conn.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM persona_generation_turns WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?', key).fetchone()[0]
        if record:
            self.store.conn.execute('INSERT INTO persona_generation_turns VALUES (?,?,?,?,?,?)', (*key, event['comment_id'], ordinal))
        return ordinal

    def _generation_memories(self, event, snapshot, ordinal):
        key = tuple(event[k] for k in SCOPE)
        last = self.store.conn.execute("SELECT MAX(turn_ordinal) FROM persona_memory_deliveries WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=? AND memory_ids<>'[]'", key).fetchone()[0]
        if last is not None and ordinal - last < 5:
            return []
        revoked = set(self.persona_store.revoked_memory_ids(snapshot['persona_uuid'], snapshot['release_id'])) if hasattr(self.persona_store, 'revoked_memory_ids') else set()
        reserved = set(self.persona_store.reserved_memory_ids(event)) if hasattr(self.persona_store, 'reserved_memory_ids') else set()
        source = snapshot.get('memories', [])
        topic_text = norm(event.get('text', ''))
        from .persona_query import parse_persona_questions
        query_topic = parse_persona_questions(event.get('text', ''), event.get('comment_id'), event.get('actor_id'))[0]['topic']
        # Retrieval is conservative before semantic classification; final policy runs again after it.
        if any(s in topic_text for s in ('dung hoi', 'khong mua', 'khong phai', 'hieu nham')):
            return []
        if reserved:
            return []
        allowed = []
        for memory in source:
            if (memory.get('memory_id') in revoked or memory.get('privacy') != 'public'
                    or memory.get('tellable') not in (True, 'public')):
                continue
            if memory.get('approval') != ('fixture' if snapshot.get('fixture_only') else 'approved'):
                continue
            if not snapshot.get('fixture_only') and not memory.get('consent_ref'):
                continue
            # A broad post topic alone does not trigger unsolicited life stories.
            if query_topic == 'none' or memory.get('topic') == query_topic:
                allowed.append(memory)
        return allowed[:2]

    def persona_generation_context(self, event, postcard, needs=None):
        if not postcard.get('persona_binding') or not self.persona_store:
            return None
        from .persona_generation import allowed_generation_facts
        from .persona_fact_policy import NAMESPACES, select_facts
        from .persona_query import KEY_REGISTRY
        snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
        if not snapshot:
            return None
        with self.store.lock:
            purge = getattr(self.persona_store, 'purge_generated', None)
            if callable(purge):
                purge(snapshot['persona_uuid'], self.clock.now().isoformat(), 30)
            attach = getattr(self.persona_store, 'attach_generated', None)
            if callable(attach):
                snapshot = attach(snapshot)
            facts = allowed_generation_facts(snapshot,
                revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
                allow_fictional_fixture=self.allow_persona_fixtures)
            memories = self._generation_memories(event, snapshot, self._generation_turn_ordinal(event))
            valid_ids = {f['fact_id'] for f in facts}
            memories = [m for m in memories if set(m.get('fact_ids', [])) <= valid_ids]
            story_facts = {fid for m in self._generation_story_sources(snapshot) for fid in m.get('fact_ids', [])}
            eligible_story_facts = {fid for m in memories for fid in m.get('fact_ids', [])}
            facts = [f for f in facts if f['fact_id'] not in story_facts or f['fact_id'] in eligible_story_facts]
            facts = select_facts(facts, needs)
            authored_post = postcard.get('persona_authored_post')
            if isinstance(authored_post, str):
                authored_post = {'text': authored_post,
                    'fact_ids': list(postcard.get('persona_authored_fact_ids') or ()),
                    'persona_uuid': snapshot['persona_uuid'], 'release_id': snapshot['release_id']}
            authored_ids = set((authored_post or {}).get('fact_ids') or postcard.get('persona_authored_fact_ids') or ())
            if isinstance(authored_post, dict) and authored_ids:
                authored_post = dict(authored_post, fact_ids=sorted(authored_ids))
                facts_by_id = {fact['fact_id']: fact for fact in allowed_generation_facts(snapshot,
                    revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
                    allow_fictional_fixture=self.allow_persona_fixtures)}
                facts.extend(facts_by_id[fid] for fid in authored_ids & facts_by_id.keys()
                             if fid not in {fact['fact_id'] for fact in facts})
            topic_keys = {topic: set(keys) for topic, keys in KEY_REGISTRY.items()}
            supplied_keys = {f.get('key') or f.get('canonical_key') for f in facts}
            topics = set(needs.get('topics') or ()) if isinstance(needs, dict) else set()
            keys = set(needs.get('keys') or ()) if isinstance(needs, dict) else set()
            gaps = sorted(topic for topic in topics if not (
                topic in NAMESPACES and any(
                    (f.get('key') or f.get('canonical_key', '')).startswith(f'gen.{topic}.') for f in facts)
                or topic in topic_keys and bool(topic_keys[topic] & supplied_keys)))
            for key in keys - supplied_keys:
                topic = next((name for name, values in topic_keys.items() if key in values), None)
                if topic and topic not in gaps:
                    gaps.append(topic)
            gaps.sort()
            valid_ids = {f['fact_id'] for f in facts}
            raw_voice = snapshot.get('snapshot', {}).get('release', {}).get('style', {}).get('voice_guide') or {}
            voice = {}
            guide_allowed = (isinstance(raw_voice, dict) and raw_voice.get('approval') == ('fixture' if snapshot.get('fixture_only') else 'approved')
                and raw_voice.get('privacy', 'public' if snapshot.get('fixture_only') else None) == 'public'
                and (snapshot.get('fixture_only') or bool(raw_voice.get('consent_ref'))))
            if guide_allowed:
                voice = {k: raw_voice[k] for k in ('tone', 'habits', 'speaker_habits', 'reactions', 'interests', 'boundaries') if k in raw_voice}
                examples = [e for e in raw_voice.get('few_shots', raw_voice.get('examples', [])) if isinstance(e, dict)
                    and e.get('approval') == ('fixture' if snapshot.get('fixture_only') else 'approved')
                    and e.get('privacy', 'public' if snapshot.get('fixture_only') else None) == 'public'
                    and (snapshot.get('fixture_only') or e.get('consent_ref'))
                    and set(e.get('fact_ids', [])) <= valid_ids][:12]
                voice['few_shots'] = [{k: e[k] for k in ('topic', 'customer_text', 'reply_text', 'fact_ids') if k in e}
                                      for e in examples]
            voice['tone'] = snapshot.get('snapshot', {}).get('release', {}).get('style', {}).get('tone', 'neutral')
            engine_context = self.context(event, postcard)
            messages = list(engine_context.get('messages', [])) + list(engine_context.get('recent', []))
            messages = [dict(m, role='user', source_role=m.get('role')) for m in messages if m.get('actor_id') == event['actor_id']
                        and m.get('role') not in ('post', 'bot')]
            if not any(m.get('message_id') == event['comment_id'] for m in messages):
                messages.append(dict(message_id=event['comment_id'], actor_id=event['actor_id'], text=event['text'], role='user', source_role='current'))
            peer_state = {k: v for k, v in self.store.peer_state(event).items()
                          if k in ('address_term', 'ownership_status', 'purchase_intent', 'follow_up', 'asked_total')}
            result = dict(peer_state=peer_state, persona_uuid=snapshot['persona_uuid'], release_id=snapshot['release_id'],
                fixture_only=snapshot.get('fixture_only', False), source_kind=snapshot.get('source_kind'),
                facts=facts, memories=memories, voice_guide=voice, messages=messages,
                persona_description=copy.deepcopy(snapshot.get('snapshot', {}).get('release', {}).get('style', {}).get('persona_description', {})),
                persona_authored_post=authored_post if isinstance(authored_post, dict) else None,
                persona_authored_fact_ids=sorted(authored_ids & {fact['fact_id'] for fact in facts}),
                needs=copy.deepcopy(needs) if isinstance(needs, dict) else None, gaps=gaps,
                channel=event.get('channel', postcard.get('channel', 'public')),
                scope={k: event[k] for k in SCOPE}, comment_id=event['comment_id'])
            result['context_token'] = digest(result)
            return result

    def _generated_reply(self, event, postcard, semantic, plan, candidate, context, signals):
        from .persona_generation import validate_generated_reply
        if not isinstance(context, dict) or context.get('scope') != {k: event[k] for k in SCOPE} or context.get('comment_id') != event['comment_id']:
            return dict(status='REVIEW', reason='GENERATION_CONTEXT_MISMATCH')
        check = {k: v for k, v in context.items() if k != 'context_token'}
        if context.get('context_token') != digest(check):
            return dict(status='REVIEW', reason='GENERATION_CONTEXT_MISMATCH')
        snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
        if not snapshot or any(snapshot.get(k) != context.get(k) for k in ('persona_uuid', 'release_id')):
            return dict(status='REVIEW', reason='GENERATION_BINDING_MISMATCH')
        if (context.get('persona_authored_post') != postcard.get('persona_authored_post')
                or context.get('persona_authored_fact_ids', []) != postcard.get('persona_authored_fact_ids', [])):
            return dict(status='REVIEW', reason='GENERATION_POST_CONTEXT_MISMATCH')
        if not isinstance(candidate, dict):
            return dict(status='REVIEW', reason='INVALID_CANDIDATE')
        candidate = copy.deepcopy(candidate)
        proposals = candidate.pop('fact_proposals', [])
        proposal_results, proposal_ids = [], {}
        customer_sources = [event.get('text', '')]
        customer_sources.extend(message['text'] for message in context.get('messages', [])
            if isinstance(message, dict) and isinstance(message.get('text'), str)
            and message.get('actor_id') == event['actor_id']
            and message.get('role') not in ('post', 'bot'))
        proposal_refs = set(candidate.get('fact_ids') or ())
        for claim in candidate.get('claims') or ():
            if isinstance(claim, dict):
                proposal_refs.update(claim.get('fact_ids') or ())
        if proposals:
            from .persona_fact_policy import purchase_year, validate_proposals
            now = self.clock.now()
            rejected_reason = None
            initial_live = self.persona_store.live_generated_keys(snapshot['persona_uuid'])
            created_today = self.persona_store.generated_created_today(
                snapshot['persona_uuid'], now.date().isoformat())
            if not isinstance(proposals, list) or len(proposals) > 2:
                rejected_reason = 'GENERATED_FACT_LIMIT'
            seen_proposal_ids = set()
            if rejected_reason:
                proposal_results.extend(dict(proposal_id=item.get('proposal_id'), status='REJECTED', reason=rejected_reason)
                                        for item in proposals if isinstance(item, dict))
            else:
                live = set(initial_live)
                for proposal in proposals:
                    pid = proposal.get('proposal_id') if isinstance(proposal, dict) else None
                    if pid in seen_proposal_ids:
                        proposal_results.append(dict(proposal_id=pid, status='REJECTED', reason='GENERATED_FACT_SHAPE'))
                        continue
                    if pid is not None:
                        seen_proposal_ids.add(pid)
                    checked, reason = validate_proposals([proposal], live_keys=live,
                        created_today=created_today, anchors={'purchase_year': purchase_year(snapshot.get('facts', []))},
                        today=now.date(), source_text=customer_sources)
                    if reason:
                        proposal_results.append(dict(proposal_id=pid, status='REJECTED', reason=reason))
                        continue
                    item = checked[0]
                    pid, key = item['proposal_id'], item['key']
                    if pid not in proposal_refs:
                        proposal_results.append(dict(proposal_id=pid, key=key, status='REJECTED',
                                                     reason='GENERATED_FACT_UNREFERENCED'))
                        continue
                    try:
                        gen_id = self.persona_store.propose_generated(snapshot['persona_uuid'], key,
                            item['value'], item['precision'], f"luna_generated:{event['comment_id']}",
                            event['comment_id'], now.isoformat())
                    except Exception:
                        why = ('GENERATED_FACT_EXISTS' if key in self.persona_store.live_generated_keys(
                            snapshot['persona_uuid']) else 'GENERATED_FACT_STORAGE_FAILED')
                        proposal_results.append(dict(proposal_id=pid, key=key, status='REJECTED', reason=why))
                        continue
                    live.add(key)
                    created_today += 1
                    proposal_ids[pid] = gen_id
                    proposal_results.append(dict(proposal_id=pid, fact_id=gen_id, key=key,
                                                 status='PENDING', reason=None))
        candidate['fact_ids'] = [proposal_ids.get(value, value) for value in candidate.get('fact_ids', [])]
        for claim in candidate.get('claims') or ():
            claim['fact_ids'] = [proposal_ids.get(value, value) for value in claim.get('fact_ids', [])]

        def rejected(reason, validation=None):
            if proposal_ids:
                self.persona_store.discard_generated(event['comment_id'], self.clock.now().isoformat())
            proposals_out = [dict(item, status='DISCARDED', reason=f'REPLY_{reason}')
                if item.get('status') == 'PENDING' else item for item in proposal_results]
            return {**(validation or {}), 'status': 'REVIEW', 'reason': reason,
                'persona_uuid': snapshot['persona_uuid'], 'release_id': snapshot['release_id'],
                'utterance_ids': [], 'query': dict(topic='generated', canonical_keys=[]),
                'fictional_fixture': snapshot.get('fixture_only', False),
                'candidate': candidate, 'generated_fact_ids': sorted(proposal_ids.values()),
                'proposal_results': proposals_out}

        if any(item.get('reason') == 'GENERATED_FACT_EXISTS' for item in proposal_results):
            return rejected('GENERATED_FACT_EXISTS')

        from .persona_query import parse_persona_questions
        requested_keys = set()
        requested_topics = set()
        for query in parse_persona_questions(event.get('text', ''), event['comment_id'],
                event['actor_id'], semantic=semantic):
            keys = set(query.get('canonical_keys') or ())
            if query.get('topic') not in ('none', 'unknown'):
                requested_topics.add(query['topic'])
            if keys == {'purchase.year'}:
                keys = {'purchase.date'}
            elif keys == {'purchase.finance_method'}:
                keys = {'purchase.down_payment_percent', 'purchase.loan_term_years'}
            if keys & {'opinion.vehicle.topic', 'opinion.vehicle.stance'}:
                keys.add('vehicle.current.model')
            requested_keys.update(keys)
        relevant_namespaces = {'travel_story': {'trip', 'memory'}, 'memory': {'memory', 'trip'},
            'vehicle_experience': {'memory', 'trip'},
            'opinion': {'taste', 'memory'}, 'preference': {'taste', 'hobby'},
            'maintenance': {'habit'}}
        proposal_namespaces = set().union(*(relevant_namespaces.get(topic, set())
                                            for topic in requested_topics))
        if not requested_keys and not requested_topics:
            needs = context.get('needs') or {}
            requested_keys.update(key for key in needs.get('keys', []) if isinstance(key, str))
            requested_topics.update(topic for topic in needs.get('topics', [])
                                    if topic in relevant_namespaces)
            from .persona_fact_policy import NAMESPACES
            proposal_namespaces.update(topic.removeprefix('gen.') for topic in
                needs.get('topics', []) if isinstance(topic, str) and
                topic.removeprefix('gen.') in NAMESPACES)
            proposal_namespaces.update(set().union(*(relevant_namespaces.get(topic, set())
                                                     for topic in requested_topics)))
        if requested_keys or requested_topics or proposal_namespaces:
            fact_keys = {fact['fact_id']: fact.get('key') for fact in context.get('facts', [])}
            fact_keys.update({fact_id: proposal['key'] for proposal in proposal_results
                              if proposal.get('status') == 'PENDING' and
                              (fact_id := proposal.get('fact_id'))})
            cited_ids = set(candidate.get('fact_ids') or ())
            for claim in candidate.get('claims') or ():
                if isinstance(claim, dict) and claim.get('subject') == 'persona':
                    cited_ids.update(claim.get('fact_ids') or ())
            cited_keys = {fact_keys[fact_id] for fact_id in cited_ids if fact_id in fact_keys}
            def relevant(key):
                return (key.startswith('gen.') and key.split('.', 2)[1] in proposal_namespaces
                        or not key.startswith('gen.') and key in requested_keys)
            model_led = getattr(self, 'model_led_conversation', False)
            if ((not cited_keys and not model_led) or (cited_keys and
                    (not any(map(relevant, cited_keys)) if model_led else not all(map(relevant, cited_keys))))):
                return rejected('REPLY_TOPIC_MISMATCH')

        snapshot = self.persona_store.attach_generated(snapshot, event['comment_id'])
        snapshot, story_ids, story_error = self._attach_cited_stories(snapshot, context, candidate)
        if story_error:
            return rejected(story_error)
        snapshot['persona_authored_post'] = context.get('persona_authored_post')
        snapshot['persona_authored_fact_ids'] = context.get('persona_authored_fact_ids', [])
        current_pending_ids = {fact['fact_id'] for fact in snapshot.get('facts', [])
            if fact.get('generated') and fact.get('source_ref') == f"luna_generated:{event['comment_id']}"}
        question = None
        if getattr(self, 'model_led_conversation', False):
            model_question = candidate.get('question')
            intent = (signals or {}).get('speaker_intent')
            if (candidate.get('asks_question') and isinstance(model_question, dict)
                    and intent not in ('declines', 'correction', 'other_person', 'ai_identity', 'transaction')
                    and plan.get('next_state', {}).get('follow_up') != 'stopped'):
                field, kind = model_question.get('field'), model_question.get('kind')
                profile = self.store.lead_profile(event)
                pending = self.store.open_pending_for(event['actor_id'], page_id=event['page_id'],
                    post_id=event['post_id'], thread_root_id=event['thread_root_id'])
                if ((kind in ('conversation', 'clarification') and field is None)
                        or (kind == 'lead' and field in FIELDS
                            and profile.get(field, {}).get('status') not in ('ASKED', 'ANSWERED', 'DECLINED')
                            and not any(p.get('expected_slot') == field for p in pending))):
                    question = dict(kind=kind, field=field)
        elif plan.get('hook_field'):
            question = dict(kind='lead', field=plan['hook_field'])
        elif (signals or {}).get('speaker_intent') not in ('declines', 'correction', 'other_person', 'owns_vehicle', 'ai_identity', 'transaction') and plan.get('next_state', {}).get('follow_up') != 'stopped':
            question = dict(kind='conversation', field=None)
        memories = {m['memory_id'] for m in self._generation_memories(event, snapshot, self._generation_turn_ordinal(event))}
        memories &= {m['memory_id'] for m in context.get('memories', [])}
        if (signals or {}).get('speaker_intent') in ('declines', 'correction', 'other_person', 'transaction', 'ai_identity'):
            memories = set()
        if signals is not None:
            if not signals.get('asks_persona_question') or signals.get('persona_topic') in (None, 'none', 'unknown'):
                memories = set()
            else:
                memories &= {m['memory_id'] for m in snapshot.get('memories', []) if m.get('topic') == signals['persona_topic']}
        else:
            from .persona_query import parse_persona_questions
            topic = parse_persona_questions(event['text'], event['comment_id'], event['actor_id'])[0]['topic']
            memories &= {m['memory_id'] for m in snapshot.get('memories', []) if m.get('topic') == topic}
        if (signals or {}).get('speaker_intent') not in ('declines', 'correction', 'other_person', 'transaction', 'ai_identity'):
            memories.update(story_ids)
        permitted_facts = {f['fact_id'] for f in context.get('facts', [])}
        permitted_facts.update(row['fact_id'] for row in snapshot.get('facts', [])
                               if row.get('generated') and row.get('source_ref') ==
                               f"luna_generated:{event['comment_id']}")
        raw_ids = candidate.get('fact_ids') if isinstance(candidate, dict) else None
        if not isinstance(raw_ids, list) or any(not isinstance(fid, str) for fid in raw_ids) or not isinstance(candidate.get('reply_text'), str):
            return rejected('INVALID_CANDIDATE')
        used_new_fact_ids = sorted((set(raw_ids) & current_pending_ids) | set(proposal_ids.values()))
        if not set(raw_ids) <= permitted_facts:
            rejected_refs = {item.get('proposal_id'): item.get('reason') for item in proposal_results
                if item.get('status') == 'REJECTED'}
            reason = next((rejected_refs.get(ref) for ref in proposal_refs if ref in rejected_refs), None)
            return rejected(reason or 'FACT_NOT_PROVIDED')
        if (not isinstance(candidate.get('memory_ids'), list) or not isinstance(candidate.get('claims'), list)
                or any(not isinstance(c, dict) or not isinstance(c.get('fact_ids'), list)
                       or not isinstance(c.get('memory_ids'), list)
                       or any(not isinstance(fid, str) for fid in c['fact_ids']) for c in candidate['claims'])):
            return rejected('INVALID_CANDIDATE')
        # A story's source facts carry the same tell/cooldown rules as its memory ID.
        for memory in self._generation_story_sources(snapshot):
            used = set(raw_ids) & set(memory.get('fact_ids', []))
            if not used:
                continue
            if memory['memory_id'] not in memories:
                return rejected('MEMORY_NOT_ALLOWED')
            if memory['memory_id'] not in candidate.get('memory_ids', []):
                return rejected('MEMORY_CITATION_REQUIRED')
            for claim in candidate.get('claims', []):
                if used & set(claim.get('fact_ids', [])) and memory['memory_id'] not in claim.get('memory_ids', []):
                    return rejected('MEMORY_CITATION_REQUIRED')
        if context.get('channel') == 'public' and re.search(r'\b(?:so dien thoai|sdt|dia chi nha|cccd|cmnd)\b', norm(candidate.get('reply_text', ''))):
            return rejected('PUBLIC_PERSONAL_DATA')
        result = validate_generated_reply(candidate, snapshot, messages=context.get('messages', []),
            allowed_memory_ids=memories, allowed_question=question, channel=context.get('channel', 'public'),
            revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
            allow_fictional_fixture=self.allow_persona_fixtures)
        if result.get('status') != 'OK':
            return rejected(result.get('reason') or 'REPLY_REJECTED', result)
        return {**result, 'persona_uuid': snapshot['persona_uuid'], 'release_id': snapshot['release_id'],
                'utterance_ids': [], 'query': dict(topic='generated', canonical_keys=[]),
                'fictional_fixture': snapshot.get('fixture_only', False),
                'asked_field': (question.get('field') if question and question.get('kind') == 'lead' else None)
                    if result.get('status') == 'OK' and candidate.get('asks_question') else None,
                'allowed_question': question, 'candidate': candidate,
                'generated_fact_ids': used_new_fact_ids, 'proposal_results': proposal_results}

    def _queue_generated_draft(self, event, candidate, context, validated, plan, now_iso, peer_signals=None):
        draft_id = 'draft_' + uuid.uuid4().hex
        payload = dict(event=event, candidate=candidate, generation_context=context,
            validation=validated, peer_plan=plan, peer_signals=peer_signals, peer_state_token=digest(self._generation_state_snapshot(event)),
            turn_ordinal=self._generation_turn_ordinal(event), validator_version='persona_generation_v1',
            proposal_results=validated.get('proposal_results', []),
            generated_fact_ids=validated.get('generated_fact_ids', []))
        self.store.conn.execute('INSERT INTO generated_reply_drafts(draft_id,page_id,post_id,thread_root_id,actor_id,comment_id,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (draft_id, *(event[k] for k in SCOPE), event['comment_id'], json.dumps(payload, ensure_ascii=False), 'PENDING', now_iso, now_iso))
        self.store.audit(event['comment_id'], 'generated_reply_drafted', dict(draft_id=draft_id,
            validation_status=validated.get('status'), reason=validated.get('reason'),
            fact_ids=validated.get('fact_ids', []), proposals=validated.get('proposal_results', [])), now_iso)
        return draft_id

    def _generated_outbox_payload(self, event, draft_id, candidate, context, validated, plan,
                                  approved_fallback=False):
        payload = dict(draft_id=draft_id, dedupe_key=f'{draft_id}:reply', text=validated['text'],
            comment_id=event['comment_id'], persona_uuid=validated['persona_uuid'],
            release_id=validated['release_id'], utterance_ids=validated.get('utterance_ids', []),
            fact_ids=validated['fact_ids'], memory_ids=validated.get('memory_ids', []),
            persona_scope={key: event[key] for key in SCOPE}, persona_event_token=digest(event),
            persona_state_token=digest(self._trace_state_snapshot(event)),
            generation_state_token=digest(self._generation_state_snapshot(event)),
            generation_context=context,
            turn_ordinal=self._generation_turn_ordinal(event), asked_field=validated.get('asked_field'),
            generated_fact_ids=validated.get('generated_fact_ids', []), proposal_results=validated.get('proposal_results', []))
        if approved_fallback:
            payload['reviewed_fallback'] = True
        else:
            payload.update(generated_candidate=candidate, generated_digest=digest(candidate),
                generation_manifest=validated['manifest'], allowed_question=validated.get('allowed_question'))
        return payload

    def capture_generated_review(self, event, postcard, candidate, context, reason):
        if not isinstance(candidate, dict) or not isinstance(context, dict):
            return None
        now_iso = self.clock.now().isoformat()
        with self.store.lock:
            existing = self.store.conn.execute('SELECT draft_id FROM generated_reply_drafts WHERE page_id=? AND comment_id=?', (event['page_id'], event['comment_id'])).fetchone()
            if existing:
                return existing[0]
            plan = self._peer_plan(event, None)
            draft_id = self._queue_generated_draft(event, candidate, context,
                dict(status='REVIEW', reason=reason, fact_ids=[], memory_ids=[]), plan, now_iso)
            row = self.store.conn.execute('SELECT payload FROM generated_reply_drafts WHERE draft_id=?', (draft_id,)).fetchone()
            data = dict(json.loads(row[0]), blocked_reason=reason)
            self.store.conn.execute('UPDATE generated_reply_drafts SET payload=? WHERE draft_id=?', (json.dumps(data, ensure_ascii=False), draft_id))
            self.store.conn.commit()
            return draft_id

    def generated_drafts(self, event_scope):
        with self.store.lock:
            rows = self.store.conn.execute('SELECT draft_id,status,payload,reviewer,updated_at FROM generated_reply_drafts WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=? ORDER BY created_at', tuple(event_scope[k] for k in SCOPE)).fetchall()
            result = []
            for row in rows:
                data = json.loads(row[2]); validation = data['validation']
                result.append(dict(draft_id=row[0], status=row[1], text=data['candidate'].get('reply_text', ''),
                    validation_status=validation.get('status'), reason=validation.get('reason'),
                    fact_ids=validation.get('fact_ids', []), memory_ids=validation.get('memory_ids', []),
                    proposed_claims=data['candidate'].get('claims', []),
                    comment_id=data['event']['comment_id'], actor_id=data['event']['actor_id'],
                    reviewer=row[3], updated_at=row[4]))
            return result

    def _generated_dispatch_validation(self, payload, snapshot, source):
        if payload.get('generation_state_token') != digest(self._generation_state_snapshot(source)):
            return 'BLOCKED_STALE_GENERATION_STATE'
        from .persona_generation import validate_generated_reply
        original = payload.get('generation_context') or {}
        if original.get('context_token') != digest({k: v for k, v in original.items() if k != 'context_token'}):
            return 'BLOCKED_GENERATION_CONTEXT'
        for message in original.get('messages', []):
            mid = message.get('message_id')
            if mid == source['comment_id']:
                if message.get('text') != source['text'] or message.get('actor_id') != source['actor_id']:
                    return 'BLOCKED_CUSTOMER_EVIDENCE'
            else:
                rows = self.store.conn.execute('SELECT payload,retracted FROM events WHERE comment_id=? AND actor_id=?', (mid, source['actor_id'])).fetchall()
                if not any(not r[1] and all(json.loads(r[0]).get(k) == source.get(k) for k in SCOPE)
                           and json.loads(r[0]).get('text') == message.get('text') for r in rows):
                    return 'BLOCKED_CUSTOMER_EVIDENCE'
        candidate = payload.get('generated_candidate')
        if payload.get('generated_digest') != digest(candidate):
            return 'BLOCKED_GENERATED_CONTENT'
        now_iso = self.clock.now().isoformat()
        self.persona_store.purge_generated(snapshot['persona_uuid'], now_iso, 30)
        snapshot = self.persona_store.attach_generated(snapshot, source.get('comment_id'))
        snapshot, story_ids, story_error = self._attach_cited_stories(snapshot, original, candidate)
        if story_error:
            return 'BLOCKED_STORY_UNAVAILABLE_OR_CHANGED'
        snapshot['persona_authored_post'] = original.get('persona_authored_post')
        snapshot['persona_authored_fact_ids'] = original.get('persona_authored_fact_ids', [])
        available_generated = {fact['fact_id'] for fact in snapshot.get('facts', []) if fact.get('generated')}
        context_generated = {fact['fact_id'] for fact in original.get('facts', []) if fact.get('generated')}
        used_generated = (set(payload.get('generated_fact_ids', [])) | context_generated) & set(candidate.get('fact_ids') or [])
        if used_generated - available_generated:
            return 'BLOCKED_GENERATED_FACT_REVOKED_OR_DISCARDED'
        revoked_memories = set(self.persona_store.revoked_memory_ids(snapshot['persona_uuid'], snapshot['release_id'])) if hasattr(self.persona_store, 'revoked_memory_ids') else set()
        if revoked_memories & set(payload.get('memory_ids', [])):
            return 'BLOCKED_REVOKED_MEMORY'
        result = validate_generated_reply(candidate, snapshot, messages=original.get('messages', []),
            allowed_memory_ids=payload.get('memory_ids', []), allowed_question=payload.get('allowed_question'), channel=original.get('channel', 'public'),
            revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
            allow_fictional_fixture=self.allow_persona_fixtures)
        if result.get('status') != 'OK' or result.get('text') != payload.get('text') or result.get('manifest') != payload.get('generation_manifest'):
            return 'BLOCKED_STALE_GENERATED_REPLY'
        return None

    def _record_generated_delivery(self, payload, now_iso):
        if not payload.get('generated_candidate') and not payload.get('reviewed_fallback'):
            return
        scope = payload['persona_scope']
        snapshot, story_ids, repository = self._story_delivery_binding(payload)
        if story_ids:
            if not snapshot or not repository.record_delivery(snapshot, scope, payload['dedupe_key'],
                                                               sorted(story_ids), status='SENT'):
                raise RuntimeError('story delivery could not be recorded')
        self.persona_store.activate_generated(payload.get('comment_id'),
            payload.get('generated_fact_ids', []), now_iso)
        if payload.get('generated_fact_ids'):
            self.store.audit(payload['comment_id'], 'persona_generated_facts_activated',
                {'fact_ids': payload['generated_fact_ids'], 'proposals': payload.get('proposal_results', [])}, now_iso)
        inserted = self.store.conn.execute('INSERT OR IGNORE INTO persona_memory_deliveries(dedupe_key,page_id,post_id,thread_root_id,actor_id,turn_ordinal,memory_ids,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (payload['dedupe_key'], *(scope[k] for k in SCOPE), payload['turn_ordinal'], json.dumps(payload.get('memory_ids', [])), now_iso))
        release_memory = getattr(self.persona_store, 'release_memory_reservation', None)
        if callable(release_memory):
            release_memory(payload['dedupe_key'])
        self.store.conn.execute("UPDATE generated_reply_drafts SET status='SENT',updated_at=? WHERE draft_id=?", (now_iso, payload.get('draft_id')))
        if inserted.rowcount and payload.get('asked_field'):
            state = self.store.peer_state(scope)
            field = payload['asked_field']
            profile = self.store.lead_profile(scope)
            pending = self.store.open_pending_for(scope['actor_id'], page_id=scope['page_id'],
                post_id=scope['post_id'], thread_root_id=scope['thread_root_id'])
            current = payload.get('generation_state_token') == digest(self._generation_state_snapshot(scope))
            still_needed = (state.get('purchase_intent') == 'exploring' and state.get('follow_up') != 'stopped'
                and profile.get(field, {}).get('status') not in ('ANSWERED', 'DECLINED')
                and not any(p['expected_slot'] == field for p in pending))
            if current or still_needed:
                plan = dict(supersede=[], lead_updates=[], next_state=state)
                self._apply_peer_plan(dict(scope, comment_id=payload['comment_id'], text=''), plan,
                    dict(status='OK', asked_field=field, utterance_ids=[]), now_iso)
            else:
                # Count the actual ask, without reviving a question corrected or declined meanwhile.
                state['asked_total'] = state.get('asked_total', 0) + 1
                self.store.save_peer_state(scope, state, now_iso)

    def _discard_generated_delivery(self, payload, now_iso, *, delivery_status=None):
        comment_id = payload.get('comment_id')
        if comment_id and self.persona_store and hasattr(self.persona_store, 'discard_generated'):
            self.persona_store.discard_generated(comment_id, now_iso)
        snapshot, story_ids, repository = self._story_delivery_binding(payload)
        if snapshot and story_ids and payload.get('dedupe_key'):
            repository.release_reservation(snapshot, payload['dedupe_key'], delivery_status=delivery_status)
        release_memory = getattr(self.persona_store, 'release_memory_reservation', None)
        if callable(release_memory) and payload.get('dedupe_key'):
            release_memory(payload['dedupe_key'])
            if payload.get('generated_fact_ids'):
                self.store.audit(comment_id, 'persona_generated_facts_discarded',
                    {'fact_ids': payload['generated_fact_ids'], 'reason': payload.get('blocked_reason', 'delivery_failed')}, now_iso)

    def _edited_generation_candidate(self, candidate, text, event):
        result = dict(candidate, reply_text=text)
        snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
        facts = {f['fact_id']: f for f in snapshot.get('facts', [])}
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', text) if s.strip()]
        claims = []
        for original in candidate.get('claims', []):
            claim = dict(original)
            referenced = [facts[fid] for fid in claim.get('fact_ids', []) if fid in facts]
            if claim.get('subject') == 'persona' and len(referenced) == 1:
                claim['predicate'] = referenced[0].get('key') or referenced[0].get('canonical_key')
            if claim['text'] not in text:
                values = [str(facts[f].get('value', '')) for f in claim.get('fact_ids', []) if f in facts]
                matches = [sentence for sentence in sentences if '?' not in sentence and values
                           and all(norm(value) in norm(sentence) for value in values)]
                if matches:
                    claim['text'] = matches[0]
                elif len(candidate.get('claims', [])) == 1 and not values:
                    claim['text'] = next((sentence for sentence in sentences if '?' not in sentence), '')
                else:
                    continue
            claims.append(claim)
        if 'claims' in candidate:
            result['claims'] = claims
            result['fact_ids'] = sorted({fid for c in claims for fid in c.get('fact_ids', [])})
            result['memory_ids'] = sorted({mid for c in claims for mid in c.get('memory_ids', [])})
        question = candidate.get('question')
        if question and question['text'] not in text:
            found = [sentence for sentence in sentences if '?' in sentence]
            result['question'] = dict(question, text=found[0]) if len(found) == 1 else None
            result['asks_question'] = result['question'] is not None
        return result

    @contextmanager
    def _generation_delivery_transaction(self, payload):
        if not payload.get('generated_candidate') and not payload.get('reviewed_fallback'):
            yield
            return
        self.store.conn.execute('BEGIN IMMEDIATE')
        try:
            self.store.lock_scope(payload['persona_scope'])
            yield
            self.store.conn.commit()
        except Exception:
            self.store.conn.rollback()
            raise

    @contextmanager
    def _generation_review_transaction(self):
        self.store.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.store.conn.commit()
        except Exception:
            self.store.conn.rollback()
            raise

    def review_generated(self, event_scope, draft_id, action, *, text=None, reviewer='sandbox-operator'):
        if action not in ('approve', 'edit', 'reject') or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('invalid review action')
        with self.store.lock, self._generation_review_transaction():
            self.store.lock_scope(event_scope)
            row = self.store.conn.execute('SELECT status,payload FROM generated_reply_drafts WHERE draft_id=? AND page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?', (draft_id, *(event_scope[k] for k in SCOPE))).fetchone()
            if not row:
                raise ValueError('Draft not found in this actor conversation')
            if row[0] != 'PENDING':
                if row[0] in ('APPROVED', 'UNKNOWN'):
                    self.store.conn.commit()
                    self.reconcile_unknown()
                    self.drain_intended()
                    current = self.store.conn.execute('SELECT status FROM generated_reply_drafts WHERE draft_id=?', (draft_id,)).fetchone()
                    return dict(draft_id=draft_id, status=current[0])
                return dict(draft_id=draft_id, status=row[0])
            data = json.loads(row[1]); event = data['event']; candidate = data['candidate']
            now_iso = self.clock.now().isoformat()
            if action == 'reject':
                self.persona_store.discard_generated(event['comment_id'], now_iso)
                self.store.conn.execute("UPDATE generated_reply_drafts SET status='REJECTED',reviewer=?,updated_at=? WHERE draft_id=?", (reviewer, now_iso, draft_id))
                self.store.audit(event['comment_id'], 'generated_reply_rejected', dict(draft_id=draft_id, reviewer=reviewer), now_iso)
                self.store.conn.commit()
                return dict(draft_id=draft_id, status='REJECTED')
            if data.get('blocked_reason'):
                self.persona_store.discard_generated(event['comment_id'], now_iso)
                return dict(draft_id=draft_id, status='NEEDS_REVIEW', reason=data['blocked_reason'])
            if action == 'edit':
                if not isinstance(text, str) or not text.strip() or len(text) > 600:
                    raise ValueError('Edited reply must contain 1–600 characters')
                candidate = self._edited_generation_candidate(candidate, text.strip(), event)
            if data['peer_state_token'] != digest(self._generation_state_snapshot(event)):
                self.persona_store.discard_generated(event['comment_id'], now_iso)
                self.store.conn.execute("UPDATE generated_reply_drafts SET status='BLOCKED',reviewer=?,updated_at=? WHERE draft_id=?", (reviewer, now_iso, draft_id))
                self.store.conn.commit()
                return dict(draft_id=draft_id, status='BLOCKED', reason='STALE_STATE')
            # Rebuild permission from the same event and current source release, not from operator prose.
            postcard = dict(persona_binding=True, id=event['post_id'], page_id=event['page_id'], channel=data['generation_context'].get('channel', 'public'))
            fallback = data['validation'].get('reply_kind') == 'approved_fallback'
            if fallback:
                from .persona_reply import render_persona_reply
                snapshot = self.persona_store.get_binding_snapshot(event['page_id'], event['post_id'])
                checked = render_persona_reply(snapshot, plan=dict(schema_version='persona_reply_plan_v1',
                    utterance_ids=data['validation'].get('utterance_ids', []), hook_id=None),
                    revoked_ids=self.persona_store.revoked_fact_ids(snapshot['persona_uuid'], snapshot['release_id']),
                    allow_fictional_fixture=self.allow_persona_fixtures)
                checked.update(persona_uuid=snapshot['persona_uuid'], release_id=snapshot['release_id'],
                               manifest=None, memory_ids=[], asked_field=data['validation'].get('asked_field'))
                if checked.get('text') != candidate.get('reply_text'):
                    checked.update(status='REVIEW', reason='FALLBACK_EDIT_NEEDS_GROUNDED_CANDIDATE')
                checked['reply_kind'] = 'approved_fallback'
                if self._uses_story_facts(snapshot, checked.get('fact_ids')):
                    checked.update(status='REVIEW', reason='MEMORY_CITATION_REQUIRED')
            else:
                checked = self._generated_reply(event, postcard, {}, data['peer_plan'], candidate, data['generation_context'], data.get('peer_signals'))
            if action == 'edit':
                updated = dict(data, candidate=candidate, validation=checked)
                self.store.conn.execute('UPDATE generated_reply_drafts SET payload=?,reviewer=?,updated_at=? WHERE draft_id=?',
                    (json.dumps(updated, ensure_ascii=False), reviewer, now_iso, draft_id))
                self.store.audit(event['comment_id'], 'generated_reply_edited', dict(draft_id=draft_id, reviewer=reviewer), now_iso)
                self.store.conn.commit()
                return dict(draft_id=draft_id, status='PENDING', validation_status=checked.get('status'), reason=checked.get('reason'))
            if checked.get('status') != 'OK':
                self.persona_store.discard_generated(event['comment_id'], now_iso)
                self.store.conn.commit()
                return dict(draft_id=draft_id, status='NEEDS_REVIEW', reason=checked.get('reason'))
            payload = dict(draft_id=draft_id, dedupe_key=f'{draft_id}:reply', text=checked['text'], comment_id=event['comment_id'],
                persona_uuid=checked['persona_uuid'], release_id=checked['release_id'],
                utterance_ids=checked.get('utterance_ids', []), fact_ids=checked['fact_ids'], memory_ids=checked.get('memory_ids', []),
                persona_scope={k: event[k] for k in SCOPE}, persona_event_token=digest(event),
                persona_state_token=digest(self._trace_state_snapshot(event)), generation_state_token=digest(self._generation_state_snapshot(event)),
                generated_candidate=candidate, generated_digest=digest(candidate), generation_context=data['generation_context'],
                generation_manifest=checked['manifest'], allowed_question=checked.get('allowed_question'),
                turn_ordinal=data['turn_ordinal'], asked_field=checked.get('asked_field'), reviewer=reviewer,
                generated_fact_ids=checked.get('generated_fact_ids', []),
                proposal_results=checked.get('proposal_results', []))
            if fallback:
                payload.pop('generated_candidate', None)
                payload['reviewed_fallback'] = True
            reason = self._persona_dispatch_precheck(payload)
            if reason:
                self.persona_store.discard_generated(event['comment_id'], now_iso)
                self.store.conn.execute("UPDATE generated_reply_drafts SET status='BLOCKED',reviewer=?,updated_at=? WHERE draft_id=?", (reviewer, now_iso, draft_id))
                self.store.conn.commit()
                return dict(draft_id=draft_id, status='BLOCKED', reason=reason)
            self.store.insert_outbox_intended(payload['dedupe_key'], None, 'persona_reply', 1, payload, now_iso)
            self.store.conn.execute("UPDATE generated_reply_drafts SET status='APPROVED',payload=?,reviewer=?,updated_at=? WHERE draft_id=?", (json.dumps(dict(data, candidate=candidate, validation=checked), ensure_ascii=False), reviewer, now_iso, draft_id))
            self.store.audit(event['comment_id'], 'generated_reply_approved', dict(draft_id=draft_id, reviewer=reviewer, edited=action == 'edit'), now_iso)
            self.store.conn.commit()
        status, send = self._dispatch(event, [], 'persona_reply', payload['text'], now_iso, payload)
        with self.store.lock:
            self.store.conn.execute('UPDATE generated_reply_drafts SET status=?,updated_at=? WHERE draft_id=?', (status, now_iso, draft_id))
            self.store.conn.commit()
        return dict(draft_id=draft_id, status=status, text=payload['text'], send=send)
