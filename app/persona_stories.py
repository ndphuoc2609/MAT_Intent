"""Durable fictional stories, scoped by a server-provided persona binding.

Uncertain matches fail closed. Semantic comparisons are advisory and only used
alongside structured temporal/entity checks, never as proof of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

from .normalize import canon_slot, norm
from .persona_fact_policy import validate_proposals, purchase_year
from .persona_generation import allowed_generation_facts, _specific_entities

_FIELDS = {'proposal_id', 'idempotency_key', 'persona_version', 'topic', 'event',
           'time_start', 'time_end', 'entities', 'base_fact_ids', 'new_details', 'relation'}
_ENTITIES = {'vehicle', 'location', 'people'}
_COLUMNS = ('memory_id', 'persona_id', 'release_id', 'topic', 'event_text', 'time_start',
            'time_end', 'entities_json', 'base_fact_ids_json', 'new_details_json',
            'relation_json', 'label', 'status')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _date_bounds(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('date must be text or null')
    if re.fullmatch(r'\d{4}', value):
        y = int(value)
        return date(y, 1, 1), date(y, 12, 31)
    parsed = date.fromisoformat(value)
    return parsed, parsed


def _story(row):
    item = dict(zip(_COLUMNS, row))
    return {'memory_id': item['memory_id'], 'persona_uuid': item['persona_id'],
            'release_id': item['release_id'], 'topic': item['topic'], 'event': item['event_text'],
            'time_start': item['time_start'], 'time_end': item['time_end'],
            'entities': json.loads(item['entities_json']), 'base_fact_ids': json.loads(item['base_fact_ids_json']),
            'new_details': json.loads(item['new_details_json']),
            'relation': json.loads(item['relation_json']) if item['relation_json'] else None,
            'label': item['label'], 'status': item['status']}


def _nonempty(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and '\x00' not in value


def _overlap(a_start, a_end, b_start, b_end):
    a = _date_bounds(a_start); b = _date_bounds(b_start)
    if not a or not b:
        return None
    ae = _date_bounds(a_end) if a_end else a
    be = _date_bounds(b_end) if b_end else b
    return a[0] <= be[1] and b[0] <= ae[1]


def _entity_conflict(a, b):
    def canonical(key, value):
        value = canon_slot(key, value)
        value = norm(str(value))
        return value.removeprefix('hyundai ' ) if key == 'vehicle' else value
    return any(a.get(k) and b.get(k) and canonical(k, a[k]) != canonical(k, b[k])
               for k in ('vehicle', 'location'))


class StoryStore:
    """Server invokes this repository with a verified post binding snapshot."""

    def __init__(self, repository):
        self.repo = repository
        self.conn = repository.conn

    def _version(self, persona_id):
        row = self.conn.execute('SELECT version FROM persona_story_versions WHERE persona_id=?', (persona_id,)).fetchone()
        return row[0] if row else 0

    def _lock(self, persona_id):
        query = 'SELECT persona_id FROM personas WHERE persona_id=?'
        if not self.repo.sqlite:
            query += ' FOR UPDATE'
        if not self.conn.execute(query, (persona_id,)).fetchone():
            raise ValueError('persona not found')
        self.conn.execute('INSERT OR IGNORE INTO persona_story_versions(persona_id,version) VALUES (?,0)', (persona_id,))

    def _binding(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError('server binding missing')
        persona, release = snapshot.get('persona_uuid'), snapshot.get('release_id')
        if not _nonempty(persona, 200) or not _nonempty(release, 200):
            raise ValueError('server binding invalid')
        row = self.conn.execute('SELECT 1 FROM persona_releases WHERE persona_id=? AND release_id=? '
                                "AND status IN ('FIXTURE','PUBLISHED')", (persona, release)).fetchone()
        if not row:
            raise ValueError('server binding stale')
        return persona, release

    def _rows(self, persona_id):
        return [_story(row) for row in self.conn.execute(
            'SELECT ' + ','.join(_COLUMNS) + ' FROM persona_stories WHERE persona_id=? AND status=\'ACTIVE\' '
            'ORDER BY created_at,memory_id', (persona_id,)).fetchall()]

    def get(self, snapshot, memory_id):
        try:
            persona, _ = self._binding(snapshot)
            row = self.conn.execute('SELECT ' + ','.join(_COLUMNS) +
                " FROM persona_stories WHERE persona_id=? AND memory_id=? AND status='ACTIVE'",
                (persona, memory_id)).fetchone()
            return _story(row) if row else None
        except Exception:
            return None

    def retrieve(self, snapshot, *, topic=None, entities=None, time_start=None, time_end=None, query=None):
        try:
            persona, _ = self._binding(snapshot)
            if topic is not None and not _nonempty(topic, 100):
                raise ValueError('invalid topic')
            if entities is not None and (not isinstance(entities, dict) or set(entities) - _ENTITIES):
                raise ValueError('invalid entities')
            if query is not None and not _nonempty(query, 600):
                raise ValueError('invalid query')
            _date_bounds(time_start); _date_bounds(time_end)
            rows = self._rows(persona)
            scored = []
            for item in rows:
                score = 0
                if topic and norm(item['topic']) == norm(topic): score += 4
                for key in ('vehicle', 'location'):
                    value = (entities or {}).get(key)
                    if isinstance(value, str) and item['entities'].get(key) and norm(value) == norm(item['entities'][key]): score += 3
                overlap = _overlap(time_start, time_end, item['time_start'], item['time_end'])
                if overlap is True: score += 2
                if query:
                    a, b = norm(query), norm(item['event'])
                    score += SequenceMatcher(None, a, b).ratio() * 3
                    score += min(2, len(set(a.split()) & set(b.split())) / 3)
                if not any((topic, entities, time_start, query)) or score >= 1:
                    scored.append((score, item))
            scored.sort(key=lambda pair: (-pair[0], pair[1]['memory_id']))
            return {'status': 'FOUND' if scored else 'NOT_FOUND', 'memories': [item for _, item in scored[:20]],
                    'version': self._version(persona)}
        except Exception:
            return {'status': 'UNKNOWN', 'memories': [], 'version': None}

    def _validate(self, snapshot, p, customer_texts):
        persona, release = self._binding(snapshot)
        if not isinstance(p, dict) or set(p) != _FIELDS:
            raise ValueError('proposal shape')
        if (not _nonempty(p['proposal_id'], 100) or not _nonempty(p['idempotency_key'], 200)
                or not _nonempty(p['topic'], 100) or not _nonempty(p['event'], 160)
                or p['persona_version'] != release):
            raise ValueError('proposal version or fields')
        entities, details, ids = p['entities'], p['new_details'], p['base_fact_ids']
        if (not isinstance(entities, dict) or set(entities) != _ENTITIES or entities['people'] != []
                or any(v is not None and not _nonempty(v, 100) for v in (entities['vehicle'], entities['location']))
                or not isinstance(details, list) or len(details) > 5
                or any(not _nonempty(v, 160) for v in details)
                or not isinstance(ids, list) or len(ids) > 20 or len(set(ids)) != len(ids)
                or any(not _nonempty(v, 200) for v in ids)):
            raise ValueError('proposal entities or details')
        start, end = _date_bounds(p['time_start']), _date_bounds(p['time_end'])
        if (start and start[0] > date.today()) or (end and end[0] > date.today()) or (end and not start) or (start and end and start[0] > end[1]):
            raise ValueError('invalid timeline')
        relation = p['relation']
        if relation is not None and (not isinstance(relation, dict) or set(relation) != {'type', 'memory_id'}
                or relation['type'] not in ('retell', 'addendum') or not _nonempty(relation['memory_id'], 200)):
            raise ValueError('invalid relation')
        allowed = allowed_generation_facts(snapshot,
            revoked_ids=self.repo.revoked_fact_ids(persona, release), allow_fictional_fixture=True)
        facts = {x['fact_id']: x for x in allowed if not x.get('generated')}
        if not set(ids) <= facts.keys():
            raise ValueError('base fact unavailable')
        vehicle = entities['vehicle']
        if vehicle:
            vehicle_facts = [f for f in facts.values() if f.get('key') in
                ('vehicle.current.model', 'vehicle.history.model', 'vehicle.experience.model') and
                (norm(vehicle) in norm(str(f.get('value'))) or norm(str(f.get('value'))) in norm(vehicle))]
            if not vehicle_facts:
                return persona, release, 'AMBIGUOUS'
            if not any(f['fact_id'] in ids for f in vehicle_facts):
                raise ValueError('vehicle needs a base fact')
            year = purchase_year(list(facts.values()))
            if year and start and start[1].year < year and any(f.get('key') == 'vehicle.current.model' for f in vehicle_facts) and not any(
                    f.get('key') == 'vehicle.experience.model' and norm(vehicle) in norm(str(f.get('value'))) for f in facts.values()):
                return persona, release, 'CONFLICT'
        for i, value in enumerate([p['event'], *details]):
            if re.search(r'\b(?:vo|chong|bo|me|con trai|con gai|con minh|anh trai|chi gai|gia dinh|nguoi than|ban be)\b', norm(value)):
                return persona, release, 'REJECTED'
            permitted_entities = [str(v) for v in (entities['vehicle'], entities['location']) if v]
            permitted_entities += [str(f.get('value')) for f in facts.values() if f['fact_id'] in ids]
            if any(not any(entity in norm(allowed) for allowed in permitted_entities)
                   for entity in _specific_entities(value)):
                return persona, release, 'REJECTED'
            _, error = validate_proposals([dict(proposal_id='new1', namespace='memory', slug=f'check_{i}',
                value=value, precision='none')], source_text=list(customer_texts), today=date.today())
            if error:
                return persona, release, 'CONFLICT' if error == 'GENERATED_FACT_FROM_CUSTOMER' else 'REJECTED'
        return persona, release, None

    def _compare(self, proposal, old, judge):
        overlap = _overlap(proposal['time_start'], proposal['time_end'], old['time_start'], old['time_end'])
        relation = proposal['relation']
        explicitly_same = bool(relation and relation['memory_id'] == old['memory_id'])
        if explicitly_same and (_entity_conflict(proposal['entities'], old['entities']) or overlap is False):
            return 'CONFLICT'
        if explicitly_same and relation['type'] == 'retell':
            known_entities = [key for key in ('vehicle', 'location')
                              if proposal['entities'].get(key) and old['entities'].get(key)]
            if (overlap is True and known_entities
                    and all(proposal['entities'].get(key) and old['entities'].get(key)
                            and not _entity_conflict(
                                {key: proposal['entities'][key]}, {key: old['entities'][key]})
                            for key in ('vehicle', 'location') if proposal['entities'].get(key) or old['entities'].get(key))
                    and set(proposal['base_fact_ids']) == set(old['base_fact_ids'])
                    and proposal['new_details'] == old['new_details']):
                return 'SAME'
            return 'AMBIGUOUS'
        if overlap is False and not explicitly_same:
            return 'DIFFERENT'
        if _entity_conflict(proposal['entities'], old['entities']):
            return 'AMBIGUOUS'
        if proposal['new_details'] and proposal['new_details'] != old['new_details']:
            return 'AMBIGUOUS'
        if norm(proposal['event']) == norm(old['event']) and proposal['new_details'] == old['new_details']:
            return 'SAME'
        if relation and relation['type'] == 'addendum':
            return 'AMBIGUOUS'
        if judge is None:
            return 'AMBIGUOUS'
        verdict = judge(proposal, old)
        if verdict not in ('SAME', 'DIFFERENT', 'CONFLICT', 'AMBIGUOUS'):
            raise ValueError('semantic judge unavailable')
        if verdict == 'DIFFERENT' and (overlap is not False):
            return 'AMBIGUOUS'
        if verdict == 'SAME':
            # shortcut: distinct wording requires matching known time and entities, upgrade with structured event identities.
            if overlap is not True or not any(proposal['entities'].get(k) and old['entities'].get(k)
                                               for k in ('vehicle', 'location')):
                return 'AMBIGUOUS'
        return verdict

    def propose(self, snapshot, proposal, *, customer_texts=(), judge=None):
        try:
            persona, release, reject = self._validate(snapshot, proposal, customer_texts)
            request_hash = hashlib.sha256(_json(proposal).encode()).hexdigest()
            key = proposal['idempotency_key']
            for _ in range(3):
                existing = self.conn.execute('SELECT request_hash,result_json FROM persona_story_requests WHERE persona_id=? AND idempotency_key=?', (persona, key)).fetchone()
                if existing:
                    return json.loads(existing[1]) if existing[0] == request_hash else {'status': 'CONFLICT', 'reason': 'IDEMPOTENCY_KEY_REUSED'}
                version = self._version(persona)
                rows = self._rows(persona)
                decision, target = reject, None
                if not decision:
                    relation = proposal['relation']
                    if relation and not any(s['memory_id'] == relation['memory_id'] for s in rows):
                        decision = 'CONFLICT'
                    for old in rows:
                        if decision: break
                        comparison = self._compare(proposal, old, judge)
                        if comparison == 'SAME': decision, target = 'EXISTING', old
                        elif comparison in ('AMBIGUOUS', 'CONFLICT'): decision = comparison
                with self.repo._atomic():
                    self._lock(persona)
                    if not self.repo.sqlite:
                        for fact_id in sorted(proposal['base_fact_ids']):
                            self.conn.execute('SELECT fact_id FROM persona_facts WHERE fact_id=? FOR UPDATE', (fact_id,)).fetchone()
                    existing = self.conn.execute('SELECT request_hash,result_json FROM persona_story_requests WHERE persona_id=? AND idempotency_key=?', (persona, key)).fetchone()
                    if existing:
                        return json.loads(existing[1]) if existing[0] == request_hash else {'status': 'CONFLICT', 'reason': 'IDEMPOTENCY_KEY_REUSED'}
                    if self._version(persona) != version:
                        continue
                    _, _, latest_reject = self._validate(snapshot, proposal, customer_texts)
                    if latest_reject:
                        decision, target = latest_reject, None
                    if decision is None:
                        mid, now = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
                        self.conn.execute('INSERT INTO persona_stories(memory_id,persona_id,release_id,topic,event_text,time_start,time_end,entities_json,base_fact_ids_json,new_details_json,relation_json,label,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,\'fictional_generated\',\'ACTIVE\',?)',
                            (mid, persona, release, proposal['topic'], proposal['event'], proposal['time_start'], proposal['time_end'],
                             _json(proposal['entities']), _json(proposal['base_fact_ids']), _json(proposal['new_details']),
                             _json(proposal['relation']) if proposal['relation'] else None, now))
                        self.conn.execute('UPDATE persona_story_versions SET version=version+1 WHERE persona_id=?', (persona,))
                        target = self.get(snapshot, mid)
                        decision = 'ACCEPTED'
                    result = {'status': decision, 'memory_id': target['memory_id'] if target else None,
                              'memory': target, 'version': self._version(persona)}
                    self.conn.execute('INSERT INTO persona_story_requests(persona_id,idempotency_key,request_hash,result_json) VALUES (?,?,?,?)',
                        (persona, key, request_hash, _json(result)))
                    return result
            return {'status': 'AMBIGUOUS', 'reason': 'VERSION_CHANGED'}
        except ValueError as exc:
            return {'status': 'REJECTED', 'reason': str(exc)}
        except Exception:
            return {'status': 'UNKNOWN', 'reason': 'STORE_OR_JUDGE_ERROR'}

    def revoke(self, snapshot, memory_id, reason):
        try:
            persona, _ = self._binding(snapshot)
            if not _nonempty(reason, 500): raise ValueError('reason required')
            with self.repo._atomic():
                self._lock(persona)
                row = self.conn.execute("UPDATE persona_stories SET status='REVOKED',revoked_at=?,revoke_reason=? WHERE persona_id=? AND memory_id=? AND status='ACTIVE'",
                    (datetime.now(timezone.utc).isoformat(), reason.strip(), persona, memory_id))
                if row.rowcount:
                    self.conn.execute("DELETE FROM persona_story_reservations WHERE persona_id=? AND memory_id=? AND status='RESERVED'", (persona, memory_id))
                    self.conn.execute('UPDATE persona_story_versions SET version=version+1 WHERE persona_id=?', (persona,))
                return bool(row.rowcount)
        except Exception:
            return False

    def record_delivery(self, snapshot, scope, dedupe_key, memory_ids, *, status):
        if status != 'SENT': return False
        try:
            persona, _ = self._binding(snapshot)
            if (not _nonempty(dedupe_key, 200) or not isinstance(memory_ids, list) or len(memory_ids) != 1
                    or not all(_nonempty(memory_id, 200) for memory_id in memory_ids)):
                return False
            keys = ('page_id', 'post_id', 'thread_root_id', 'actor_id')
            if not isinstance(scope, dict) or any(not _nonempty(scope.get(k), 200) for k in keys): return False
            with self.repo._atomic():
                self._lock(persona)
                for memory_id in memory_ids:
                    reserved = self.conn.execute("SELECT page_id,post_id,thread_root_id,actor_id FROM persona_story_reservations WHERE persona_id=? AND dedupe_key=? AND memory_id=? AND status='SENDING'", (persona, dedupe_key, memory_id)).fetchone()
                    # A prior matching receipt makes SENT reconciliation idempotent.
                    prior = self.conn.execute('SELECT persona_id,scope_json,memory_ids_json FROM persona_story_delivery_receipts WHERE dedupe_key=?', (dedupe_key,)).fetchone()
                    expected_receipt = (persona, _json({k: scope[k] for k in keys}), _json(sorted(set(memory_ids))))
                    if (not reserved or tuple(reserved) != tuple(scope[k] for k in keys)) and (not prior or tuple(prior) != expected_receipt):
                        return False
                scope_json = _json({k: scope[k] for k in keys})
                ids_json = _json(sorted(set(memory_ids)))
                receipt = self.conn.execute('SELECT persona_id,scope_json,memory_ids_json FROM persona_story_delivery_receipts WHERE dedupe_key=?', (dedupe_key,)).fetchone()
                if receipt and tuple(receipt) != (persona, scope_json, ids_json): return False
                if not receipt:
                    self.conn.execute('INSERT INTO persona_story_delivery_receipts(dedupe_key,persona_id,scope_json,memory_ids_json) VALUES (?,?,?,?)',
                                      (dedupe_key, persona, scope_json, ids_json))
                now = datetime.now(timezone.utc).isoformat()
                for memory_id in sorted(set(memory_ids)):
                    self.conn.execute('INSERT OR IGNORE INTO persona_story_deliveries(dedupe_key,memory_id,page_id,post_id,thread_root_id,actor_id,delivered_at) VALUES (?,?,?,?,?,?,?)',
                        (dedupe_key, memory_id, *(scope[k] for k in keys), now))
                self.conn.execute('DELETE FROM persona_story_reservations WHERE persona_id=? AND dedupe_key=?', (persona, dedupe_key))
            return True
        except Exception:
            return False

    def reserve(self, snapshot, scope, dedupe_key, memory_id):
        try:
            persona, release = self._binding(snapshot)
            keys = ('page_id', 'post_id', 'thread_root_id', 'actor_id')
            if (not isinstance(scope, dict) or any(not _nonempty(scope.get(k), 200) for k in keys)
                    or not _nonempty(dedupe_key, 200) or not _nonempty(memory_id, 200)):
                return False
            bound = self.repo.get_binding_snapshot(scope['page_id'], scope['post_id'])
            if not bound or (bound['persona_uuid'], bound['release_id']) != (persona, release): return False
            with self.repo._atomic():
                self._lock(persona)
                if self.get(snapshot, memory_id) is None: return False
                row = self.conn.execute('SELECT persona_id,page_id,post_id,thread_root_id,actor_id,memory_id FROM persona_story_reservations WHERE dedupe_key=?', (dedupe_key,)).fetchone()
                expected = (persona, *(scope[k] for k in keys), memory_id)
                if row: return tuple(row) == expected
                if self.conn.execute('SELECT 1 FROM persona_story_deliveries WHERE memory_id=? AND page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?',
                                     (memory_id, *(scope[k] for k in keys))).fetchone(): return False
                self.conn.execute('INSERT INTO persona_story_reservations(dedupe_key,persona_id,page_id,post_id,thread_root_id,actor_id,memory_id,status,reserved_at) VALUES (?,?,?,?,?,?,?,\'RESERVED\',?)',
                    (dedupe_key, *expected, datetime.now(timezone.utc).isoformat()))
            return True
        except Exception:
            return False

    def told(self, snapshot, scope):
        try:
            persona, release = self._binding(snapshot)
            keys = ('page_id', 'post_id', 'thread_root_id', 'actor_id')
            if not isinstance(scope, dict) or any(not _nonempty(scope.get(k), 200) for k in keys):
                raise ValueError('scope invalid')
            bound = self.repo.get_binding_snapshot(scope['page_id'], scope['post_id'])
            if not bound or (bound['persona_uuid'], bound['release_id']) != (persona, release):
                raise ValueError('binding invalid')
            rows = self.conn.execute('SELECT d.memory_id,d.delivered_at FROM persona_story_deliveries d '
                'JOIN persona_stories s ON s.memory_id=d.memory_id WHERE s.persona_id=? AND d.page_id=? '
                'AND d.post_id=? AND d.thread_root_id=? AND d.actor_id=? ORDER BY d.delivered_at,d.memory_id',
                (persona, *(scope[k] for k in keys))).fetchall()
            return {'status': 'FOUND' if rows else 'NOT_FOUND',
                    'deliveries': [{'memory_id': row[0], 'delivered_at': row[1]} for row in rows]}
        except Exception:
            return {'status': 'UNKNOWN', 'deliveries': []}

    def start_delivery(self, snapshot, dedupe_key):
        try:
            persona, _ = self._binding(snapshot)
            with self.repo._atomic():
                self._lock(persona)
                row = self.conn.execute('SELECT memory_id,status FROM persona_story_reservations WHERE persona_id=? AND dedupe_key=?', (persona, dedupe_key)).fetchone()
                if not row or self.get(snapshot, row[0]) is None: return False
                if row[1] == 'SENDING': return True
                return self.conn.execute("UPDATE persona_story_reservations SET status='SENDING' WHERE persona_id=? AND dedupe_key=? AND status='RESERVED'", (persona, dedupe_key)).rowcount == 1
        except Exception:
            return False

    def release_reservation(self, snapshot, dedupe_key, *, delivery_status=None):
        try:
            persona, _ = self._binding(snapshot)
            with self.repo._atomic():
                self._lock(persona)
                sql = 'DELETE FROM persona_story_reservations WHERE persona_id=? AND dedupe_key=?'
                if delivery_status != 'FAILED':
                    sql += " AND status='RESERVED'"
                return bool(self.conn.execute(sql, (persona, dedupe_key)).rowcount)
        except Exception:
            return False
