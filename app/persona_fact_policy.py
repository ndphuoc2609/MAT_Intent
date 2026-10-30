"""Rules for persona details that Luna proposes in the sandbox.

Generated facts are never mixed into approved releases. They live in their own
table, carry the ``gen.`` key prefix and are checked here before they are stored.
The checks guarantee shape, scope, limits and internal consistency; they cannot
prove a detail is true, so these facts are always labelled as generated.
"""
from __future__ import annotations

import re
from datetime import date

from .normalize import norm

GENERATED_PREFIX = 'gen.'
# Open "by story" inside these namespaces; everything else (price, promo, stock, spec,
# warranty, dealer data, contact details) has no namespace and therefore cannot be created.
NAMESPACES = ('life', 'hobby', 'trip', 'habit', 'family', 'memory', 'taste', 'work', 'car')
GENERATED_PREDICATES = frozenset(f'gen.{ns}' for ns in NAMESPACES)
PROPOSAL_IDS = ('new1', 'new2')
PRECISIONS = ('exact', 'approximate', 'month', 'year', 'none')
MAX_PER_TURN = 2
MAX_PER_DAY = 20
MAX_SHOWN = 40
MAX_VALUE = 160
PENDING_TTL_MINUTES = 30

_SLUG = re.compile(r'^[a-z][a-z0-9_]{1,39}$')
_PROPOSAL_FIELDS = {'proposal_id', 'namespace', 'slug', 'value', 'precision'}
_FORBIDDEN = {
    'GENERATED_FACT_PRODUCT': re.compile(
        r'\b(?:bao gia|gia xe|gia ban|gia niem yet|lan banh|khuyen mai|uu dai|giam gia|chiet khau|'
        r'ton kho|con hang|con xe|bao hanh|lai suat|dat coc|nhan coc)\b'),
    'GENERATED_FACT_CONTACT': re.compile(
        r'\b\d{8,}\b|@|https?|www\.|\.com\b|\.vn\b|\b(?:zalo|sdt|so dien thoai|email|inbox)\b'),
    'GENERATED_FACT_SENSITIVE': re.compile(
        r'\b(?:benh|ung thu|phau thuat|tram cam|dieu tri|chinh tri|bau cu|dang phai|ton giao|'
        r'phat giao|cong giao|thien chua|hoi giao|ly hon|vay no|no nan)\b'),
}
_MONEY = re.compile(r'\d\s*(?:trieu|ty|tr|nghin|dong|k|vnd)\b')
_YEAR = re.compile(r'\b(19\d{2}|20\d{2})\b')
_SOURCE_STOPWORDS = frozenset((
    'minh', 'ban', 'toi', 'tao', 'anh', 'chi', 'em', 'bac', 'co', 'chu', 'nguoi',
    'la', 'co', 'dang', 'da', 'se', 'hay', 'thuong', 'rat', 'cung', 'nha', 'nhe',
    'a', 'ha', 'khong', 'chua', 'tung', 'mot', 'nhung', 'va', 'voi', 'cho', 'cua',
    've', 'di', 'o', 'den', 'tu', 'trong', 'ngoai', 'tren', 'duoi', 'khi', 'thi',
    'neu', 'gi', 'nao', 'sao', 'bao', 'nhieu', 'khong', 'phai', 'muon', 'thich',
    'can', 'biet', 'hoi', 'nhe', 'do', 'kia', 'nay', 'ay', 'qua', 'lam', 'roi',
    'moi', 'gia', 'dinh', 'nha', 'nho', 'gio', 'cung',
))


def is_generated_key(key) -> bool:
    return isinstance(key, str) and key.startswith(GENERATED_PREFIX) and key.count('.') == 2 \
        and key.split('.')[1] in NAMESPACES and bool(_SLUG.match(key.split('.')[2]))


def namespace_of(key) -> str | None:
    return key.split('.')[1] if is_generated_key(key) else None


def predicate_for(key) -> str | None:
    namespace = namespace_of(key)
    return f'gen.{namespace}' if namespace else None


def as_snapshot_fact(row, persona_uuid, release_id) -> dict:
    """Immutable view of a generated fact, separate from release approval facts.

    Status and timestamps are deliberately left out: the manifest digest of a reply must
    not change when the fact moves from PENDING to ACTIVE.
    """
    key = row['fact_key']
    return {'fact_id': row['gen_fact_id'], 'key': key, 'canonical_key': key, 'category': 'generated',
            'entity_id': None, 'value': row['value_text'], 'value_type': 'text', 'unit': None,
            'valid_from': None, 'valid_to': None, 'as_of': None, 'precision': row['precision'],
            'tellable': 'public', 'source_ref': row['source_ref'], 'source_kind': 'generated',
            'approval': 'generated', 'generated': True, 'persona_uuid': persona_uuid, 'release_id': release_id}


def _forbidden_reason(value):
    text = norm(value)
    for reason, pattern in _FORBIDDEN.items():
        if pattern.search(text):
            return reason
    if _MONEY.search(text):
        return 'GENERATED_FACT_PRODUCT'
    return None


def _content_tokens(value):
    return {token for token in re.findall(r'[a-z0-9]+', norm(value))
            if len(token) > 1 and token not in _SOURCE_STOPWORDS}


def _echoed_from_customer(value, sources):
    target = _content_tokens(value)
    for source in sources:
        if not isinstance(source, str):
            continue
        normalized_value, normalized_source = norm(value), norm(source)
        if normalized_value and normalized_value in normalized_source:
            return True
        if any(year in set(_YEAR.findall(normalized_source)) for year in _YEAR.findall(normalized_value)):
            return True
        source_tokens = _content_tokens(source)
        shared = target & source_tokens
        if not target or not shared:
            continue
        ratio = len(shared) / len(target)
        distinctive = {token for token in shared if len(token) >= 3}
        if ratio >= 0.75 and len(distinctive) >= min(2, len(target)):
            return True
    return False


def validate_proposals(proposals, *, live_keys=(), created_today=0, anchors=None, today=None, source_text=''):
    """Return (accepted, None) or (None, reason). Pure: no database access."""
    today = today or date.today()
    anchors = anchors or {}
    if proposals is None:
        return [], None
    if not isinstance(proposals, list) or len(proposals) > MAX_PER_TURN:
        return None, 'GENERATED_FACT_LIMIT'
    if created_today + len(proposals) > MAX_PER_DAY:
        return None, 'GENERATED_FACT_RATE_LIMIT'
    live, seen_ids, seen_keys, accepted = set(live_keys), set(), set(), []
    for item in proposals:
        if not isinstance(item, dict) or set(item) != _PROPOSAL_FIELDS:
            return None, 'GENERATED_FACT_SHAPE'
        pid, namespace, slug = item['proposal_id'], item['namespace'], item['slug']
        value, precision = item['value'], item['precision']
        if (pid not in PROPOSAL_IDS or pid in seen_ids or namespace not in NAMESPACES
                or not isinstance(slug, str) or not _SLUG.match(slug)
                or not isinstance(value, str) or precision not in PRECISIONS):
            return None, 'GENERATED_FACT_SHAPE'
        value = re.sub(r'\s+', ' ', value).strip()
        if not value or len(value) > MAX_VALUE or any(ord(c) < 32 for c in value):
            return None, 'GENERATED_FACT_SHAPE'
        key = f'gen.{namespace}.{slug}'
        if key in live or key in seen_keys:
            return None, 'GENERATED_FACT_EXISTS'
        reason = _forbidden_reason(value) or _forbidden_reason(slug.replace('_', ' '))
        if reason:
            return None, reason
        sources = [source_text] if isinstance(source_text, str) else source_text
        if not isinstance(sources, (list, tuple)) or _echoed_from_customer(value, sources):
            return None, 'GENERATED_FACT_FROM_CUSTOMER'
        for year in (int(y) for y in _YEAR.findall(value)):
            if year > today.year or year < 1960:
                return None, 'GENERATED_FACT_TIMELINE'
            if namespace == 'car' and anchors.get('purchase_year') and year < anchors['purchase_year']:
                return None, 'GENERATED_FACT_TIMELINE'
        seen_ids.add(pid); seen_keys.add(key)
        accepted.append(dict(proposal_id=pid, namespace=namespace, key=key, value=value, precision=precision))
    return accepted, None


def select_facts(facts, needs):
    """Facts the engine hands to Luna for this turn, limited to phase-one needs.

    ``needs=None`` means phase 1 was unavailable, so every allowed fact is kept.
    """
    if needs is None:
        return list(facts)
    from .persona_query import KEY_REGISTRY
    topic_of = {key: topic for topic, keys in KEY_REGISTRY.items() for key in keys}
    topics, keys = set(needs.get('topics') or ()), set(needs.get('keys') or ())
    kept = []
    for fact in facts:
        key = fact.get('key') or fact.get('canonical_key')
        topic = topic_of.get(key)
        topic_without_exact_keys = topic in topics and not any(topic_of.get(needed) == topic for needed in keys)
        if (key in keys or topic_without_exact_keys
                or (is_generated_key(key) and namespace_of(key) in topics)):
            kept.append(fact)
    return kept


def purchase_year(facts) -> int | None:
    """Earliest year the persona got the current car, from approved facts only."""
    for fact in facts or ():
        if (fact.get('key') or fact.get('canonical_key')) in ('purchase.date', 'vehicle.current.usage_started_at'):
            match = _YEAR.search(str(fact.get('value') or ''))
            if match:
                return int(match.group(1))
    return None
