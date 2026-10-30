"""Evidence-bound customer facts scoped by the server event."""
from __future__ import annotations

import re
from .lead_flow import _OF_OTHER
from .normalize import canon_slot, norm

SCOPE = ('page_id', 'post_id', 'thread_root_id', 'actor_id')


def list_customer_facts(repository, event):
    rows = repository.conn.execute(
        'SELECT predicate,value,evidence,certainty,comment_id,created_at FROM persona_customer_facts '
        'WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=? '
        'ORDER BY created_at,comment_id,predicate', tuple(event[k] for k in SCOPE)).fetchall()
    return [dict(zip(('predicate','value','evidence','certainty','comment_id','created_at'), row)) for row in rows]


def record_customer_facts(repository, event, facts, now_iso):
    if not isinstance(facts, list) or len(facts) > 8:
        return {'status': 'REJECTED', 'reason': 'INVALID_FACTS'}
    text = event.get('text', '')
    if not isinstance(text, str) or _OF_OTHER.search(norm(text)):
        return {'status': 'REJECTED', 'reason': 'SUBJECT_UNCLEAR'}
    seen = set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {'predicate','value','evidence_excerpt','subject','certainty'}:
            return {'status': 'REJECTED', 'reason': 'INVALID_FACTS'}
        key, value, excerpt = fact['predicate'], fact['value'], fact['evidence_excerpt']
        if (fact['subject'] != 'customer' or fact['certainty'] not in ('explicit','inferred','unknown')
                or not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key)
                or key in seen or not isinstance(value, str) or not value.strip() or len(value) > 500
                or not isinstance(excerpt, str) or not excerpt.strip() or excerpt not in text):
            return {'status': 'REJECTED', 'reason': 'INVALID_EVIDENCE'}
        if fact['certainty'] == 'explicit' and norm(value) not in norm(excerpt):
            if key not in ('current_vehicle', 'interested_vehicle') or \
                    norm(str(canon_slot('vehicle', value))) != norm(str(canon_slot('vehicle', excerpt))):
                return {'status': 'REJECTED', 'reason': 'NORMALIZATION_UNVERIFIED'}
        seen.add(key)
    with repository._atomic():
        for fact in facts:
            key = tuple(event[k] for k in SCOPE) + (event['comment_id'], fact['predicate'])
            old = repository.conn.execute('SELECT value,evidence,certainty FROM persona_customer_facts '
                'WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=? AND comment_id=? AND predicate=?', key).fetchone()
            values = (fact['value'], fact['evidence_excerpt'], fact['certainty'])
            if old and tuple(old) != values:
                raise ValueError('customer fact idempotency conflict')
            if not old:
                repository.conn.execute('INSERT INTO persona_customer_facts VALUES (?,?,?,?,?,?,?,?,?,?)', key + values + (now_iso,))
    return {'status': 'RECORDED', 'facts': facts}
