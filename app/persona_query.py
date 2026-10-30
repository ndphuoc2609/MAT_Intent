"""Small, closed-vocabulary parser for questions about a bound persona.

This classifies *which approved fact keys are being asked about*. It never
retrieves facts, and it only accepts evidence quoted from the current message.
"""
from __future__ import annotations

import re
from typing import Callable

from .normalize import norm

SCHEMA_VERSION = "persona_query_v1"
ADDRESS = r"(?:ban|bac|chu|co|chi|anh|ad)"

# The registry is intentionally the only source of keys accepted from a parser.
KEY_REGISTRY = {
    "identity": frozenset({"identity.name", "identity.age_years", "identity.pronouns"}),
    "family": frozenset({"family.members", "family.children"}),
    "occupation": frozenset({"occupation.title", "occupation.industry"}),
    "residence": frozenset({"location.base.province", "location.base.district"}),
    "vehicle_current": frozenset({"vehicle.current.model", "vehicle.current.year", "vehicle.current.trim", "vehicle.current.first_car", "vehicle.current.mileage_km", "vehicle.current.color", "vehicle.current.usage_started_at"}),
    "vehicle_past": frozenset({"vehicle.history.model", "vehicle.history.ownership"}),
    "vehicle_experience": frozenset({"vehicle.experience.type", "vehicle.experience.model"}),
    "purchase_finance": frozenset({"purchase.dealer", "purchase.year", "purchase.date", "purchase.finance_method", "purchase.down_payment_percent", "purchase.loan_term_years"}),
    "maintenance": frozenset({"vehicle.maintenance.type", "vehicle.maintenance.location"}),
    "travel_story": frozenset({"story.travel.destination", "story.travel.vehicle", "travel.commute", "travel.weekend.destination"}),
    "preference": frozenset({"preference.vehicle.color", "preference.vehicle.type"}),
    "opinion": frozenset({"opinion.vehicle.topic", "opinion.vehicle.stance"}),
    "location_compare": frozenset({"location.compare.from", "location.compare.to"}),
    "none": frozenset(),
    "unknown": frozenset(),
}


def _result(topic="none", keys=(), *, message_id=None, actor_id=None,
            excerpt=None, clarify=False):
    return {
        "schema_version": SCHEMA_VERSION,
        "topic": topic,
        "canonical_keys": list(keys),
        "evidence_message_id": message_id if excerpt else None,
        "evidence_actor_id": actor_id if excerpt else None,
        "evidence_excerpt": excerpt,
        "needs_clarification": clarify,
    }


def _pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _deterministic(text: str):
    """Return an exact high-confidence match, or None when no rule applies."""
    value = norm(text)
    value = re.sub(r"\b(?:bac|chu|chi|anh|ad)\b", "ban", value)
    # Ask-before-negation: a negative/correction must not be interpreted as
    # permission to retrieve the personal fact being denied or withdrawn.
    if _pattern(value, (r"\b(khong|ko|chua|dung)\b.{0,45}\b(hoi|can|muon|biet)\b",
                        r"\b(thoi|nua)\b.{0,20}\b(hoi|can|muon)\b")):
        return _result()

    rules = (
        ("vehicle_current", ("vehicle.current.model",),
         (r"\bban (?:dang )?(?:di|chay|lai) con (?:gi|nao)\b",
          r"\bxe (?:cua )?ban (?:dang )?(?:di|chay|lai) (?:la )?xe (?:gi|nao)\b")),
        ("vehicle_current", ("vehicle.current.usage_started_at",),
         (rf"\b{ADDRESS}\s+(?:dang\s+)?(?:chay|di|dung|lai|xai)\b.{{0,20}}\b(?:bao lau|may nam|may thang|bao nhieu nam|bao nhieu thang)\b",)),
        ("vehicle_current", ("vehicle.current.color",),
         (rf"\b{ADDRESS}\s+(?:dang\s+)?(?:di|chay)\s+(?:xe\s+)?mau\s+(?:gi|nao)\b",)),
        ("vehicle_current", ("vehicle.current.model",),
         (rf"\b{ADDRESS}\s+(?:dang\s+)?(?:di|lai|chay|su dung)\s+(?:bang\s+)?xe\s+(?:gi|nao)\b",)),
        ("purchase_finance", ("purchase.dealer",),
         (rf"\b{ADDRESS}\s+mua\s+(?:xe|creta|tucson)\s+o\s+(?:dau|dai ly nao)\b",
          rf"\bxe\s+(?:do|nay)\s+{ADDRESS}\s+mua\s+o\s+(?:dau|dai ly nao)\b")),
        ("identity", ("identity.age_years",),
         (r"\bban\s+(?:nam\s+)?(?:bao nhieu tuoi|may tuoi)\b",
          r"\btuoi cua ban\b", r"\bban\s+bao nhieu tuoi roi\b")),
        ("identity", ("identity.name",),
         (r"\bten ban la gi\b", r"\bminh goi ban la gi\b")),
        ("family", ("family.members",),
         (r"\bban co (?:gia dinh|anh chi em|anh em|chi em)\b",
          r"\bban co may (?:anh chi em|nguoi trong gia dinh)\b")),
        ("family", ("family.children",),
         (r"\bban co con chua\b", r"\bban co may dua con\b")),
        ("occupation", ("occupation.title",),
         (rf"\b{ADDRESS} lam nghe gi\b", r"\bcong viec cua ban la gi\b",
          r"\bban lam gi\b")),
        ("residence", ("location.base.province",),
         (r"\bban (?:dang )?(?:song|o|cu tru) o dau\b", r"\bban o dau vay\b", r"\b(?:anh|ban) (?:o|song) dau\b", r"\bnha (?:ban|anh) o dau\b")),
        ("residence", ("location.base.province",),
         (r"\bban (?:dang )?(?:song|o) o tinh nao\b", r"\bban o tinh nao\b")),
        ("residence", ("location.base.district",),
         (r"\bban (?:dang )?(?:song|o) o quan nao\b", r"\bban o (?:quan|khu) nao\b", r"\b(?:ban|anh) song o khu nao\b")),
        ("vehicle_past", ("vehicle.history.model",),
         (r"\btruoc day ban (?:da )?(?:tung )?(?:di|lai|chay|su dung) xe gi\b",
          r"\bxe truoc day cua ban la xe gi\b")),
        ("vehicle_current", ("vehicle.current.trim",),
         (r"\b(?:xe|creta) (?:cua )?(?:ban|anh) (?:la )?ban (?:gi|nao)\b", r"\b(?:ban|anh) (?:di|chay|mua) ban (?:gi|nao)\b")),
        ("vehicle_current", ("vehicle.current.first_car",),
         (r"\b(?:xe|creta) (?:cua )?(?:ban|anh).{0,20}\b(?:dau doi|dau tien)\b",
          r"\b(?:xe dau doi|xe dau tien) cua (?:ban|anh)\b")),
        ("vehicle_current", ("vehicle.current.mileage_km",),
         (r"\b(?:xe|creta).{0,30}(?:bao nhieu|may).{0,12}(?:km|cay|ki lo)\b", r"\b(?:ban|anh).{0,30}(?:di|chay).{0,12}(?:bao nhieu|may).{0,12}(?:km|cay)\b")),
        ("vehicle_current", ("vehicle.current.model",),
         (r"\b(?:hien(?: nay)? )?ban (?:dang )?(?:di|lai|chay|su dung) xe gi\b",
          r"\bxe hien tai cua ban la xe gi\b", r"\bban dang di xe nao\b")),
        ("vehicle_experience", ("vehicle.experience.model",),
         (r"\bban da tung lai xe gi\b", r"\bban tung trai nghiem xe gi\b")),
        ("purchase_finance", ("purchase.dealer",),
         (r"\bban mua xe o (?:dai ly nao|dau)\b",
          r"\bban mua (?:creta|tucson|xe) o dai ly nao\b")),
        ("purchase_finance", ("purchase.year",),
         (r"\b(?:ban|anh) mua (?:xe|creta) nam nao\b",)),
        ("purchase_finance", ("purchase.date",),
         (r"\b(?:ban|anh) mua (?:xe|creta) (?:khi nao|luc nao|bao gio|thang may)\b",
          r"\bxe (?:cua )?(?:ban|anh) mua (?:khi nao|bao gio)\b")),
        ("purchase_finance", ("purchase.finance_method",),
         (r"\b(?:ban|anh) mua (?:xe|creta) tra (?:gop|thang) khong\b",
          r"\b(?:ban|anh).{0,25}mua xe bang cach nao\b")),
        ("purchase_finance", ("purchase.down_payment_percent",),
         (r"\b(?:ban|anh).{0,25}tra truoc.{0,15}(?:bao nhieu|may phan tram|nhieu)\b",)),
        ("purchase_finance", ("purchase.loan_term_years",),
         (r"\b(?:ban|anh).{0,30}(?:tra gop|gop).{0,15}(?:may nam|bao lau)\b",)),
        ("purchase_finance", ("purchase.down_payment_percent", "purchase.loan_term_years"),
         (r"\b(?:ban|anh).{0,30}(?:mua|tra).{0,10}tra gop\b", r"\b(?:ban|anh).{0,25}mua xe bang cach nao\b")),
        ("travel_story", ("travel.commute",),
         (r"\b(?:ban|anh).{0,20}di lam.{0,20}(?:tuyen|duong|o dau|the nao)\b",)),
        ("travel_story", ("travel.weekend.destination",),
         (r"\b(?:ban|anh).{0,20}cuoi tuan.{0,15}(?:ve|di) dau\b", r"\bcuoi tuan (?:ban|anh).{0,10}(?:ve|di) dau\b")),
        ("maintenance", ("vehicle.maintenance.type",),
         (r"\bban bao duong xe nhu the nao\b", r"\bban bao duong xe o dau\b")),
        ("travel_story", ("story.travel.destination",),
         (r"\bban tung di du lich o dau\b", r"\bchuyen di gan day cua ban\b")),
        ("preference", ("preference.vehicle.color",),
         (r"\bban thich xe mau gi\b", r"\bban thich mau xe nao\b")),
        ("preference", ("preference.vehicle.type",),
         (r"\bban thich dong xe nao\b", r"\bban thich kieu xe nao\b")),
        ("opinion", ("opinion.vehicle.topic", "opinion.vehicle.stance"),
         (rf"\b{ADDRESS}\s+(?:di|chay|dung)(?:\s+xe)?\s+(?:thay|cam thay)\s+(?:sao|the nao|nhu nao)\b",
          rf"\b{ADDRESS}\s+(?:danh gia|nhan xet|cam nhan).{{0,35}}\b(?:xe|creta|tucson)\b",
          rf"\b{ADDRESS}\s+thay\s+(?:xe|creta|tucson).{{0,25}}\b(?:the nao|nhu nao|ra sao)\b",
          r"\bquan diem cua ban ve (?:xe|creta|tucson)\b",
          r"\bban nghi gi ve (?:xe|creta|tucson)\b")),
        ("location_compare", ("location.compare.from", "location.compare.to"),
         (r"\bban thay song o dau tot hon\b", r"\bban so sanh khu vuc nao\b")),
    )
    for topic, keys, patterns in rules:
        if _pattern(value, patterns):
            return _result(topic, keys, excerpt=text)
    return None


def _valid_parser_output(value, text):
    required = {"schema_version", "topic", "canonical_keys", "evidence_excerpt", "needs_clarification"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        return None
    topic, keys = value["topic"], value["canonical_keys"]
    if topic not in KEY_REGISTRY or not isinstance(keys, list) or len(keys) > 5:
        return None
    if any(not isinstance(key, str) or key not in KEY_REGISTRY[topic] for key in keys):
        return None
    if len(set(keys)) != len(keys) or type(value["needs_clarification"]) is not bool:
        return None
    excerpt = value["evidence_excerpt"]
    if topic in ("none", "unknown"):
        if keys or excerpt is not None or value["needs_clarification"] is (topic == "none"):
            return None
    elif (not keys or not isinstance(excerpt, str) or not excerpt.strip()
          or len(excerpt) > 500 or excerpt not in text):
        return None
    return topic, keys, excerpt, value["needs_clarification"]


def _semantic_allows_persona(semantic):
    if semantic is None:
        return True
    if not isinstance(semantic, dict) or semantic.get("scope") != "in_scope":
        return False
    labels = semantic.get("intent_labels")
    requests = semantic.get("requests")
    if isinstance(labels, list) and "social" in labels:
        return True
    if isinstance(requests, list) and any(isinstance(req, dict) and req.get("topic") == "social"
                                         for req in requests):
        return True
    # Older semantic callers may emit only SOCIAL_ACK for a social question.
    operations = semantic.get("operations")
    return (isinstance(operations, list) and bool(operations)
            and all(isinstance(op, dict) and op.get("op") == "SOCIAL_ACK" for op in operations))


def parse_persona_query(text: str, message_id: str, actor_id: str, *,
                        parser: Callable[[str], dict] | None = None,
                        semantic: dict | None = None, policy_blocked: bool = False):
    """Classify a current-message question about a persona.

    ``parser`` is an optional structured-output callable, invoked only when the
    deterministic Vietnamese rules do not match. Its output must use the exact
    ``persona_query_v1`` fields and registered keys. It is never called unless
    the caller supplies it. A supplied automotive semantic/policy gate cannot
    be overridden by this parser.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
        return _result("unknown", clarify=True)
    if policy_blocked or not _semantic_allows_persona(semantic):
        return _result()

    match = _deterministic(text)
    if match is not None:
        match["evidence_message_id"] = message_id
        match["evidence_actor_id"] = actor_id
        return match

    if parser is not None:
        try:
            parsed = _valid_parser_output(parser(text), text)
        except Exception:
            parsed = None
        if parsed:
            topic, keys, excerpt, clarify = parsed
            return _result(topic, keys, message_id=message_id, actor_id=actor_id,
                           excerpt=excerpt, clarify=clarify)
        return _result("unknown", clarify=True)
    return _result()


def parse_persona_questions(text, message_id, actor_id, *, semantic=None):
    """Split explicit personal questions; keep evidence tied to the original text."""
    primary = parse_persona_query(text, message_id, actor_id, semantic=semantic)
    if not isinstance(text, str) or not text.strip() or len(text) > 10_000 or not _semantic_allows_persona(semantic):
        return [primary]
    questions = []
    for clause in re.split(r"[,;?？]|\s+(?:và|còn|va)\s+", text):
        clause = clause.strip()
        if not clause:
            continue
        # An omitted subject is inherited only inside the same question, not from another turn.
        expanded = clause
        if questions and re.match(r"^(?:làm nghề|mua xe|trả trước|góp mấy|đi xe|ở đâu|lam nghe|mua xe|tra truoc|gop may|di xe|o dau)\b", clause, re.I):
            expanded = "Bạn " + clause
        query = parse_persona_query(expanded, message_id, actor_id, semantic=semantic)
        if query['topic'] != 'none':
            query['evidence_excerpt'] = clause
            if not any(q['canonical_keys'] == query['canonical_keys'] and q['topic'] == query['topic'] for q in questions):
                questions.append(query)
    return questions or [primary]
