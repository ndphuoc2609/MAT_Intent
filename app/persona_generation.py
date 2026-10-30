"""Fail-closed checks for generated persona replies and their source manifest.

This is a lexical and provenance validator, not a natural-language entailment
proof. A reply that cannot be checked is returned for review, never as OK.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
import copy
from typing import Any

from .normalize import norm
from .persona_query import KEY_REGISTRY
from .persona_fact_policy import GENERATED_PREDICATES, is_generated_key, namespace_of, predicate_for


MANIFEST_VERSION = "persona_generation_manifest_v1"
_PERSONA_PREDICATES = frozenset(key for keys in KEY_REGISTRY.values() for key in keys) | GENERATED_PREDICATES
_CUSTOMER_PREDICATES = frozenset({
    "current_vehicle", "interested_vehicle", "purchase_timeline", "finance_intent",
    "budget", "usage_need", "ownership_status", "purchase_intent",
})
_QUESTION_KINDS = frozenset({"conversation", "clarification", "lead"})
_QUESTION_FIELDS = frozenset({
    "usage_need", "current_vehicle", "interested_vehicle", "purchase_timeline",
    "finance_intent", "budget", "ownership_status", "purchase_intent",
})
_CUSTOMER_ROLES = frozenset({"user", "customer", "commenter"})
_ID_FIELDS = {"fact_ids", "memory_ids", "message_ids"}
_CLAIM_FIELDS = {"text", "subject", "predicate", "fact_ids", "memory_ids", "message_ids"}
_CANDIDATE_FIELDS = {"reply_text", "fact_ids", "memory_ids", "claims", "product_claims", "asks_question", "question"}
_QUESTION_CANDIDATE_FIELDS = {"kind", "field", "text"}

_STOP_WORDS = frozenset(norm(word) for word in (
    "mình", "tôi", "em", "anh", "chị", "bạn", "bác", "chú", "cô", "ông", "bà",
    "đang", "đã", "sẽ", "thì", "là", "có", "và", "hay", "một", "của", "cho",
    "ở", "về", "đi", "chạy", "lái", "dùng", "sử dụng", "xe", "chiếc", "con",
    "này", "đó", "kia", "với", "như", "theo", "khoảng", "tầm", "cũng", "rồi",
    "nữa", "thêm", "thường", "nếu", "khi", "rất", "khá", "hơi", "mình", "nhỉ",
))
_GENERIC_CAPITALIZED = frozenset(norm(word) for word in (
    "Mình", "Bạn", "Tôi", "Em", "Anh", "Chị", "Bác", "Chú", "Cô", "Ông", "Bà",
    "Dạ", "Vâng", "Ừ", "Đúng", "Nếu", "Khi", "Với", "Còn", "Cuối", "Hồi",
    "Mình", "Cảm", "Chúc", "Xin", "Hy vọng", "Ok", "Đây", "Được", "Thế", "Ờ", "À", "AI",
))
_NEGATION_RE = re.compile(r"\b(?:khong|ko|chua|chang|cha|never|not)\b")
_NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])")
_CAPITALIZED_RE = re.compile(r"[^\W\d_]+(?:[’'-][^\W\d_]+)?", re.UNICODE)
_MILEAGE_KEYS = frozenset({"vehicle.current.mileage_km"})

_TRANSACTION_RE = re.compile(
    r"\b(?:nhan coc|dat coc|coc xe|coc|"
    r"giu xe|giu cho|giu lich|hen lich|hen gap|hen ban|dat lich|dat hen|dat xe|"
    r"lich giao xe|giao xe|chuyen khoan|chot don|xac nhan don|giao dich|"
    r"goi lai|goi cho ban|goi ban|lien he lai|lien he voi ban|lien he ban|"
    r"nhan tin lai|nhan tin cho ban|xac nhan lich)\b"
)
_PRODUCT_ASSERTION_RE = re.compile(r"\b(?:gia|khuyen mai|uu dai|giam gia|chiet khau|con hang|con xe|"
    r"ton kho|bao hanh|thong so|dung tich|dong co|dai ly|phien ban)\b")
_SYSTEM_RE = re.compile(
    r"\b(?:khong phai|khong la|khong phai la)\s+(?:ai|bot|robot|nguoi that)\b|"
    r"\b(?:minh|toi|em)\s+la\s+nguoi that\b|"
    r"\b(?:i am not|i'm not|not an?|i am)\s+(?:an?\s+)?(?:ai|bot|robot|real person)\b|"
    r"\b(?:system prompt|prompt he thong|huong dan noi bo|lenh noi bo|"
    r"bo qua (?:moi )?(?:chi dan|huong dan)|ignore previous instructions)\b"
)
_CONTACT_TERM_RE = re.compile(r"\b(?:so dien thoai|so lien lac|sdt|email|zalo|dia chi|inbox|phone)\b")
_CONTACT_ASK_RE = re.compile(
    r"\b(?:xin|gui|de lai|cho minh biet|chia se|nhan tin|lien he|goi|"
    r"cua ban|ban co|o dau|cho minh)\b"
)
_SPOUSE_OPINION_RE = re.compile(
    r"\b(?:vo|chong|con|bo|me)\b.{0,45}\b(?:thich|ghet|dong y|nghi|muon|cam thay)\b"
)
_FAMILY_RE = re.compile(
    r"\b(?:gia dinh|ca nha|anh chi em|vo chong|bo me)\b|"
    r"\b(?:vo|chong|bo|me|con cai|con trai|con gai)\s+(?:minh|toi|em|ban|anh|chi)\b|"
    r"\b(?:minh|toi|em|ban|anh|chi)\s+(?:co|cua)\s+(?:vo|chong|bo|me|con(?: trai| gai| cai)?|gia dinh)\b"
)
_EMOTION_RE = re.compile(r"\b(?:minh|toi|em)\b.{0,45}\b(?:met|vui|buon|lo lang|thich|ghet|yeu|hai long|chan|so)\b")
_CAUSE_RE = re.compile(
    r"\b(?:vi the|do vay|vi vay|nen minh|nen toi|boi vi|nho vay)\b.{0,100}\b"
    r"(?:quyet dinh|chon mua|mua xe|doi xe|ban xe|vay|tra gop)\b"
)
_TIME_RE = re.compile(
    r"\b(?:luc ay|hoi do|nam ngoai|nam nay|nam kia|thang truoc|thang sau|"
    r"hom qua|tu lau|ngay truoc|truoc day)\b"
)
_BENIGN_REACTION_ATOM = (
    r"(?:a|da|vang|dung roi|ok|u|chao(?: ban)?|xin chao(?: ban)?|cam on(?: ban)?|co gi dau(?: ban)?|co chi dau|"
    r"khong co gi|khong sao|xin loi(?: nhe)?|minh hieu nham xin loi nhe|"
    r"minh ghi nhan nhu cau nay nhe|duoc minh dung o day nhe|u vay minh dung o day nhe|"
    r"a minh hieu nham y ban xin loi nhe|co gi dau ban cu di xem thu nhe|"
    r"minh cung (?:trao doi|tro chuyen) thoi|duoc vay minh dung o day nhe|"
    r"(?:(?:minh|toi|em)\s+)?(?:hieu(?: roi| ma| y ban)?|dong y|rat hieu|"
    r"rat vui duoc ho tro|vui duoc giup|vui duoc tro chuyen|san sang tro chuyen)"
    r")"
)
_BENIGN_REACTION_RE = re.compile(r"^" + _BENIGN_REACTION_ATOM +
                                 r"(?:\s+" + _BENIGN_REACTION_ATOM + r")*(?:\s+(?:nhe|nha|a))?$")
_SAFE_AI_DISCLOSURE_RE = re.compile(
    r"^(?:day la tro ly ai ho tro tra loi theo ho so cua nguoi viet|"
    r"day la nhan vat mo phong bang ai trong sandbox)[.! ]*$"
)
_SAFE_NO_INFO_RE = re.compile(
    r"^(?:(?:a\s+)?(?:minh|toi|em)\s+)?"
    r"(?:chua co|khong co|chua du|khong du)\s+(?:du\s+)?"
    r"(?:thong tin|can cu|du kien)"
    r"(?:\s+(?:chinh xac|cu the|xac nhan|da xac minh|duoc xac minh))?"
    r"(?:\s+(?:ve|cho)\s+(?:dieu do|chuyen do|viec do|cau do|dieu nay|chuyen nay|viec nay|cau nay))?"
    r"(?:\s+de\s+(?:chia se|tra loi|noi|khang dinh|xac nhan)"
    r"(?:\s+(?:dieu do|chuyen do|viec do|cau do|dieu nay|chuyen nay|viec nay|cau nay))?)?"
    r"(?:\s+nhe)?$"
)
_SAFE_TOPICAL_NO_INFO_RE = re.compile(
    r"^(?:con\s+)?[\w\s-]{1,100}?\s+thi\s+"
    r"(?:(?:minh|toi|em)\s+)?(?:chua co|khong co|chua du|khong du)\s+(?:du\s+)?"
    r"(?:thong tin|can cu|du kien)"
    r"(?:\s+(?:chinh xac|cu the|xac nhan|da xac minh|duoc xac minh))?"
    r"(?:\s+(?:de chia se|de tra loi|de noi|de khang dinh|de xac nhan|nhe))?$"
)
_SELF_HISTORY_RE = re.compile(
    r"\b(?:minh|toi|em)\s+(?:chua tung|chua bao gio|chua|khong|da|tung)\s+"
    r"(?:co xe|so huu|lai(?:\s+thu)?|thu(?:\s+xe)?|di xe|dung xe|trai nghiem)\b|"
    r"\b(?:trai nghiem|lai thu|thu xe|di xe|dung xe)\s+(?:cua|minh|toi|em)\b"
)
_EXPECTED_UNIT = {
    "identity.age_years": re.compile(r"\b(?:tuoi|nam tuoi|years old)\b"),
    "purchase.year": re.compile(r"\b(?:nam|doi|model year)\b"),
    "vehicle.current.year": re.compile(r"\b(?:nam|doi|model year)\b"),
    "vehicle.current.mileage_km": re.compile(r"\b(?:km|cay so|kilomet)\b"),
}


def generation_digest(value: Any) -> str:
    """Return a stable SHA-256 digest of a JSON-compatible value."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_chain(snapshot):
    if not isinstance(snapshot, dict):
        return []
    chain, current = [snapshot], snapshot
    for _ in range(2):
        nested = current.get("snapshot")
        if not isinstance(nested, dict):
            break
        chain.append(nested)
        current = nested
    return chain


def _snapshot_context(snapshot):
    chain = _snapshot_chain(snapshot)
    if not chain:
        return None
    persona = next((part["persona"] for part in chain if isinstance(part.get("persona"), dict)), {})
    release = next((part["release"] for part in chain if isinstance(part.get("release"), dict)), {})
    persona_ids = [part[key] for part in chain for key in ("persona_uuid", "persona_id")
                   if isinstance(part.get(key), str) and part[key]]
    if isinstance(persona.get("id"), str) and persona["id"]:
        persona_ids.append(persona["id"])
    release_ids = [part["release_id"] for part in chain
                   if isinstance(part.get("release_id"), str) and part["release_id"]]
    if isinstance(release.get("id"), str) and release["id"]:
        release_ids.append(release["id"])
    if len(set(persona_ids)) > 1 or len(set(release_ids)) > 1:
        return None
    persona_id = persona_ids[0] if persona_ids else None
    release_id = release_ids[0] if release_ids else None
    source_kinds = [part["source_kind"] for part in chain
                    if isinstance(part.get("source_kind"), str) and part["source_kind"]]
    if isinstance(persona.get("source_kind"), str) and persona["source_kind"]:
        source_kinds.append(persona["source_kind"])
    if len(set(source_kinds)) > 1:
        return None
    source_kind = source_kinds[0] if source_kinds else None
    is_fixture = source_kind == "fictional_fixture"
    status = release.get("status")
    approvals = [part["approved"] for part in chain if isinstance(part.get("approved"), bool)]
    if isinstance(release.get("approved"), bool):
        approvals.append(release["approved"])
    fixture_values = [part["fixture_only"] for part in chain
                      if isinstance(part.get("fixture_only"), bool)]
    if isinstance(release.get("fixture_only"), bool):
        fixture_values.append(release["fixture_only"])
    if len(set(approvals)) > 1 or len(set(fixture_values)) > 1:
        return None
    approved = approvals[0] if approvals else None
    fixture_only = fixture_values[0] if fixture_values else None
    if not isinstance(persona_id, str) or not persona_id \
            or not isinstance(release_id, str) or not release_id:
        return None
    if is_fixture:
        if approved is not False or fixture_only is not True or status != "FIXTURE":
            return None
    elif approved is not True or fixture_only is True or status not in (None, "PUBLISHED"):
        return None
    return {
        "persona_uuid": persona_id,
        "release_id": release_id,
        "source_kind": source_kind,
        "fixture": is_fixture,
        "release_checksum": release.get("checksum"),
    }


def _parse_date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid date")
    raw = value.strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError("invalid date") from exc


def _privacy_is_public(row):
    for key in ("privacy", "privacy_level", "visibility", "sensitivity"):
        if key in row and row[key] is not None and norm(str(row[key])) not in {
                "public", "publicly tellable"}:
            return False
    if row.get("is_private") is True or row.get("private") is True or row.get("sensitive") is True:
        return False
    return True


def _valid_public_fact(row, context, today, revoked):
    if not isinstance(row, dict):
        return False
    fact_id = row.get("fact_id")
    key = row.get("key") or row.get("canonical_key")
    if not isinstance(fact_id, str) or not fact_id \
            or not isinstance(key, str) or (key not in _PERSONA_PREDICATES and not is_generated_key(key)):
        return False
    generated = is_generated_key(key)
    if generated:
        if row.get("persona_uuid") != context["persona_uuid"] \
                or row.get("release_id") != context["release_id"]:
            return False
    elif row.get("persona_uuid", context["persona_uuid"]) != context["persona_uuid"] \
            or row.get("release_id", context["release_id"]) != context["release_id"]:
        return False
    if fact_id in revoked or row.get("revoked") is True or row.get("revoked_at"):
        return False
    if row.get("tellable") not in (True, "public") or not _privacy_is_public(row):
        return False
    if not isinstance(row.get("source_ref"), str) or not row["source_ref"].strip():
        return False
    source_kind = row.get("source_kind")
    if generated:
        if row.get("generated") is not True or source_kind != "generated" or row.get("approval") != "generated":
            return False
    else:
        if row.get("generated") is True or source_kind == "generated" or row.get("approval") == "generated":
            return False
        if context["fixture"]:
            if source_kind != "fictional_fixture" or row.get("approval") != "fixture":
                return False
        elif source_kind == "fictional_fixture" or row.get("approval") not in (True, "approved"):
            return False
    value = row.get("value", row.get("value_text", row.get("value_integer")))
    if value is None:
        return False
    for field in ("valid_from", "valid_to", "as_of"):
        try:
            parsed = _parse_date(row.get(field))
        except ValueError:
            return False
        if field == "valid_from" and parsed and parsed > today:
            return False
        if field == "valid_to" and parsed and parsed <= today:
            return False
    if key in _MILEAGE_KEYS and (not row.get("as_of") or "undated" in norm(str(row.get("precision", "")))):
        return False
    return True


def allowed_generation_facts(snapshot, revoked_ids=(), allow_fictional_fixture=False) -> list:
    """Return only approved, public, in-scope, currently valid facts."""
    context = _snapshot_context(snapshot)
    if not context or (context["fixture"] and allow_fictional_fixture is not True):
        return []
    chain = _snapshot_chain(snapshot)
    facts = next((part["facts"] for part in chain if isinstance(part.get("facts"), list)), None)
    if not isinstance(facts, list):
        return []
    if not isinstance(revoked_ids, (list, tuple, set, frozenset)) \
            or any(not isinstance(value, str) for value in revoked_ids):
        return []
    ids = [item.get("fact_id") for item in facts if isinstance(item, dict)]
    if any(not isinstance(value, str) or not value for value in ids) \
            or len(ids) != len(set(ids)):
        return []
    today = date.today()
    nested_revocations = []
    for part in chain:
        part_revoked = part.get("revoked_fact_ids", ())
        if not isinstance(part_revoked, (list, tuple, set, frozenset)) \
                or any(not isinstance(value, str) for value in part_revoked):
            return []
        nested_revocations.extend(part_revoked)
    revoked = set(revoked_ids) | set(nested_revocations)
    return sorted((row for row in facts if _valid_public_fact(row, context, today, revoked)),
                  key=lambda row: row["fact_id"])


def _safe_candidate_text(candidate, max_length=600):
    if not isinstance(candidate, dict):
        return None
    text = candidate.get("reply_text")
    if not isinstance(text, str) or not text.strip() or len(text) > max_length or "\x00" in text:
        return None
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        return None
    return text.strip()


def _persona_authored_post(snapshot, context, facts_by_id):
    post = next((part["persona_authored_post"] for part in _snapshot_chain(snapshot)
                 if "persona_authored_post" in part), None)
    if post is None:
        return None, None
    if (not isinstance(post, dict) or not isinstance(post.get("text"), str)
            or len(post["text"]) > 10000 or not _id_list(post.get("fact_ids"), max_len=40)
            or post.get("persona_uuid") != context["persona_uuid"]
            or post.get("release_id") != context["release_id"]
            or any(fid not in facts_by_id for fid in post["fact_ids"])):
        return None, "AUTHORED_POST_INVALID"
    return post, None


def _fact_values_conflict(left, right):
    left_value, right_value = _raw_value(left), _raw_value(right)
    if left_value is None or right_value is None:
        return False
    if isinstance(left_value, (int, float)) and not isinstance(left_value, bool):
        if not isinstance(right_value, (int, float)) or isinstance(right_value, bool):
            return True
        return left_value != right_value
    if isinstance(left_value, bool):
        return left_value != right_value
    if isinstance(left_value, str) and isinstance(right_value, str):
        return norm(left_value) != norm(right_value)
    return left_value != right_value


def _contradicts_persona_authored_post(candidate, post, facts_by_id):
    if not post:
        return False
    authored = [facts_by_id[fid] for fid in post["fact_ids"]]
    for claim in candidate["claims"]:
        if claim["subject"] != "persona":
            continue
        for reply_fact_id in claim["fact_ids"]:
            reply_fact = facts_by_id[reply_fact_id]
            reply_key = reply_fact.get("key") or reply_fact.get("canonical_key")
            for source in authored:
                source_key = source.get("key") or source.get("canonical_key")
                if source_key == reply_key and _fact_values_conflict(source, reply_fact):
                    return True
                # shortcut: generated trip values with disjoint place names conflict; upgrade when stories have event IDs.
                if (is_generated_key(source_key) and is_generated_key(reply_key)
                        and namespace_of(source_key) == namespace_of(reply_key) == "trip"):
                    source_places = set(_specific_entities(str(_raw_value(source) or "")))
                    reply_places = set(_specific_entities(str(_raw_value(reply_fact) or "")))
                    if source_places and reply_places and not source_places & reply_places:
                        return True
    return False


def _has_conflicting_persona_claim_facts(candidate, facts_by_id):
    seen = {}
    for claim in candidate["claims"]:
        if claim["subject"] != "persona":
            continue
        for fact_id in claim["fact_ids"]:
            if fact_id not in facts_by_id:
                continue
            fact = facts_by_id[fact_id]
            key = fact.get("key") or fact.get("canonical_key")
            previous = seen.get(key)
            if previous is not None and _fact_values_conflict(previous, fact):
                return True
            seen[key] = fact
    return False


def _manifest_review(candidate, reason):
    payload = {"version": MANIFEST_VERSION,
               "candidate_digest": _digest_or_none(candidate),
               "fact_ids": [], "memory_ids": [], "message_ids": [], "reason": reason}
    return {**payload, "digest": generation_digest(payload)}


def _digest_or_none(value):
    try:
        return generation_digest(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _review(reason, candidate=None):
    return {"status": "REVIEW", "text": _safe_candidate_text(candidate),
            "fact_ids": [], "memory_ids": [], "reason": reason,
            "manifest": _manifest_review(candidate, reason)}


def _id_list(value, max_len=50):
    return isinstance(value, list) and len(value) <= max_len \
        and all(isinstance(item, str) and item.strip() for item in value) \
        and len(value) == len(set(value))


def _validate_shape(candidate, max_text_length=600):
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        return None
    text = _safe_candidate_text(candidate, max_text_length)
    if text is None or text != candidate["reply_text"]:
        return None
    if not _id_list(candidate["fact_ids"]) or not _id_list(candidate["memory_ids"]):
        return None
    claims = candidate["claims"]
    if not isinstance(claims, list) or len(claims) > 20:
        return None
    for item in claims:
        if not isinstance(item, dict) or set(item) != _CLAIM_FIELDS \
                or not isinstance(item.get("text"), str) or not item["text"].strip() \
                or len(item["text"]) > 600 or item["text"] not in text \
                or text.count(item["text"]) != 1 \
                or not isinstance(item.get("subject"), str) or item["subject"] not in {"persona", "customer"} \
                or not isinstance(item.get("predicate"), str) or not item["predicate"] \
                or any(not _id_list(item.get(key)) for key in _ID_FIELDS):
            return None
    product_claims = candidate['product_claims']
    if not isinstance(product_claims, list) or len(product_claims) > 12:
        return None
    for item in product_claims:
        if (not isinstance(item, dict) or set(item) != {'text', 'product_fact_id', 'product_type',
                'effective_at', 'vehicle', 'variant', 'location'}
                or not isinstance(item.get('text'), str) or not item['text'] or item['text'] not in text
                or not isinstance(item.get('product_fact_id'), str) or not item['product_fact_id'].strip()
                or item.get('product_type') not in {'list_price','promo','stock','spec','warranty','dealer'}
                or not isinstance(item.get('effective_at'), str) or not item['effective_at'].strip()
                or any(item.get(key) is not None and not isinstance(item.get(key), str)
                    for key in ('vehicle','variant','location'))):
            return None
        if len(item['text']) > 600 or text.count(item['text']) != 1:
            return None
    if type(candidate["asks_question"]) is not bool:
        return None
    question = candidate["question"]
    if candidate["asks_question"]:
        if not isinstance(question, dict) or set(question) != _QUESTION_CANDIDATE_FIELDS \
                or not isinstance(question.get("kind"), str) or question["kind"] not in _QUESTION_KINDS \
                or question.get("field") is not None and (not isinstance(question["field"], str)
                                                            or question["field"] not in _QUESTION_FIELDS) \
                or not isinstance(question.get("text"), str) or not question["text"].strip() \
                or len(question["text"]) > 240 or question["text"] not in text \
                or text.count(question["text"]) != 1:
            return None
    elif question is not None:
        return None
    return text


def _number_value(value):
    raw = str(value).strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        digits = re.sub(r"[.,]", "", raw)
        return str(int(digits))
    if re.fullmatch(r"\d+[.,]\d+", raw):
        return raw.replace(",", ".")
    digits = re.sub(r"\D", "", raw)
    return str(int(digits)) if digits else ""


def _number_spans(text):
    return [(match.group(0), match.start(), match.end()) for match in _NUMBER_RE.finditer(text)]


def _contains_phrase(text, phrase):
    if not text or not phrase:
        return False
    escaped = re.escape(phrase)
    escaped = escaped.replace(r"\ ", r"\s+")
    return bool(re.search(r"(?<!\w)" + escaped + r"(?!\w)", text, re.UNICODE))


def _unit_ok(text, fact):
    key = fact.get("key") or fact.get("canonical_key")
    unit = norm(str(fact.get("unit") or ""))
    expected = _EXPECTED_UNIT.get(key)
    if unit in {"percent", "%", "percentage"}:
        expected = re.compile(r"(?:%|\bphan tram\b|\bpercent\b)")
    elif unit in {"km", "kilometer", "kilometre", "kilometers", "kilometres"}:
        expected = re.compile(r"\b(?:km|cay so|kilomet)\b")
    elif unit in {"years", "year", "nam"} and key == "identity.age_years":
        expected = _EXPECTED_UNIT["identity.age_years"]
    elif unit in {"years", "year", "nam"}:
        expected = re.compile(r"\b(?:nam|years?)\b")
    elif unit in {"months", "month", "thang"}:
        expected = re.compile(r"\b(?:thang|months?)\b")
    elif unit in {"vnd", "dong", "currency"}:
        expected = re.compile(r"\b(?:dong|vnd|trieu|ty)\b")
    if expected is None:
        return True
    for raw, start, end in _number_spans(text):
        context = norm(text[max(0, start - 20):min(len(text), end + 30)])
        if expected.search(context):
            return True
    return False


def _raw_value(fact):
    return fact.get("value", fact.get("value_text", fact.get("value_integer", fact.get("value_number"))))


def _fact_aliases(fact):
    value = _raw_value(fact)
    aliases = []
    if isinstance(value, str):
        aliases.append(value)
        key = fact.get("key") or fact.get("canonical_key")
        # Numeric calendar months are often stored zero-padded (03/2024),
        # while natural replies render them as "tháng 3/2024".
        if key in {"purchase.date", "vehicle.current.usage_started_at"}:
            match = re.fullmatch(r"\s*0?([1-9]|1[0-2])[/-](\d{4})\s*", value)
            if match:
                aliases.append(f"{int(match.group(1))}/{match.group(2)}")
    explicit = fact.get("aliases")
    if isinstance(explicit, str):
        aliases.append(explicit)
    elif isinstance(explicit, list):
        aliases.extend(item for item in explicit if isinstance(item, str) and item.strip())
    key = fact.get("key") or fact.get("canonical_key")
    value_norm = norm(str(value)) if value is not None else ""
    vehicle_aliases = {
        "hyundai creta": ("creta",), "hyundai tucson": ("tucson",),
        "hyundai santa fe": ("santa fe", "santafe"),
    }
    if key in {"vehicle.current.model", "vehicle.history.model", "vehicle.experience.model"}:
        aliases.extend(vehicle_aliases.get(value_norm, ()))
    return list(dict.fromkeys(aliases))


def _fact_value_supported(text, fact):
    value = _raw_value(fact)
    value_type = fact.get("value_type")
    key = fact.get("key") or fact.get("canonical_key")
    text_norm = norm(text)
    if isinstance(value, bool) or value_type == "boolean":
        if key == "vehicle.current.first_car":
            affirmative = re.search(r"\b(?:xe dau tien|chiec xe dau tien|xe dau doi)\b", text_norm)
            negative = re.search(r"\b(?:khong phai|khong la|chua tung)\b.{0,30}\b(?:xe dau tien|xe dau doi)\b", text_norm)
            return bool(affirmative) and not negative if value is True else bool(negative)
        return False
    if isinstance(value, (int, float)) or value_type in {"integer", "number"}:
        wanted = _number_value(value)
        matches = [raw for raw, _, _ in _number_spans(text)
                   if _number_value(raw) == wanted]
        return bool(matches) and _unit_ok(text, fact)
    aliases = _fact_aliases(fact)
    for alias in aliases:
        normalized = norm(alias)
        if len(normalized) < 2:
            continue
        if _contains_phrase(text_norm, normalized):
            return _unit_ok(text, fact) if _number_spans(alias) else True
    # Longer approved descriptions can be paraphrased around a short, specific
    # phrase from that description; a one-word overlap is not enough.
    if isinstance(value, str) and len(value) > 100:
        tokens = [word for word in norm(value).split() if len(word) > 2 and word not in _STOP_WORDS]
        for size in (3, 2):
            for index in range(max(0, len(tokens) - size + 1)):
                if _contains_phrase(text_norm, " ".join(tokens[index:index + size])):
                    return True
    return False


def _product_value_supported(text, fact):
    value, unit = fact.get('value'), norm(str(fact.get('unit') or ''))
    normalized = norm(text)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = int(value) if int(value) == value else value
        forms = {str(n), f'{n:,}'.replace(',', '.'), f'{n:,}'}
        if unit == 'vnd':
            if n % 1_000_000 == 0: forms.add(f'{n // 1_000_000} trieu')
            if n % 1_000_000_000 == 0: forms.add(f'{n // 1_000_000_000} ty')
        unit_words = {'years': ('nam',), 'percent': ('%', 'phan tram'),
                      'percent_per_year': ('%/nam', 'phan tram/nam'), 'vnd': ('dong', 'vnd', 'trieu', 'ty')}
        for form in forms:
            if form in normalized and (not unit_words.get(unit) or any(word in normalized for word in unit_words[unit])):
                return True
        return False
    if isinstance(value, str):
        return norm(value) in normalized
    return False


def _product_claims_valid(candidate, product_facts, now, verified_at=None):
    if not candidate.get('product_claims'):
        return True, None, []
    if not isinstance(product_facts, list):
        return False, 'PRODUCT_SOURCE_UNKNOWN', []
    by_id = {fact.get('id'): fact for fact in product_facts if isinstance(fact, dict)}
    cited, grounded = [], []
    from .facts import Facts
    if isinstance(verified_at, str):
        try: verified_at = datetime.fromisoformat(verified_at.replace('Z', '+00:00'))
        except ValueError: return False, 'PRODUCT_FACT_STALE_OR_MISMATCHED', []
    for claim in candidate['product_claims']:
        fact = by_id.get(claim['product_fact_id'])
        fact_verified_at = fact.get('verified_at') if fact else None
        if isinstance(fact_verified_at, str):
            try: fact_verified_at = datetime.fromisoformat(fact_verified_at.replace('Z', '+00:00'))
            except ValueError: return False, 'PRODUCT_FACT_STALE_OR_MISMATCHED', []
            fact = dict(fact, verified_at=fact_verified_at)
            by_id[claim['product_fact_id']] = fact
        if (not fact or fact.get('source') in (None, 'SAMPLE') or fact.get('type') != claim['product_type']
                or fact.get('valid_from') != claim['effective_at']
                or any(claim[key] != fact.get(key) for key in ('vehicle','variant','location'))
                or not Facts([fact], verified_at=fact_verified_at or verified_at).is_fresh(fact, now)):
            return False, 'PRODUCT_FACT_STALE_OR_MISMATCHED', []
        if fact.get('vehicle') and not _contains_phrase(norm(claim['text']), norm(fact['vehicle'])):
            return False, 'PRODUCT_VEHICLE_MISMATCH', []
        if fact.get('variant') and not _contains_phrase(norm(claim['text']), norm(fact['variant'])):
            return False, 'PRODUCT_VARIANT_MISMATCH', []
        if fact.get('location') and not _contains_phrase(norm(claim['text']), norm(fact['location'])):
            return False, 'PRODUCT_LOCATION_MISMATCH', []
        extra = fact.get('extra') or {}
        supported = _product_value_supported(claim['text'], fact)
        if fact.get('type') == 'spec':
            supported = any(_product_value_supported(claim['text'], {'value': value, 'unit': 'years' if key == 'warranty_years' else fact.get('unit')})
                or isinstance(value, str) and norm(str(value)) in norm(claim['text'])
                for key, value in extra.items())
        elif fact.get('type') == 'dealer':
            supported = supported or any(isinstance(value, str) and len(norm(value)) > 3
                and _contains_phrase(norm(claim['text']), norm(value)) for value in extra.values())
        if not supported:
            return False, 'PRODUCT_VALUE_MISMATCH', []
        cited.append(fact['id'])
        aliases = [fact.get(key) for key in ('vehicle','variant','location') if fact.get(key)]
        aliases.extend(str(value) for value in extra.values() if isinstance(value, (str,int,float)))
        grounded.append({'text': claim['text'], 'aliases': aliases})
    if len(cited) != len(set(cited)):
        return False, 'DUPLICATE_PRODUCT_CLAIM', []
    for sentence in _sentences(candidate['reply_text']):
        if _PRODUCT_ASSERTION_RE.search(norm(sentence)) and not any(claim['text'] in sentence for claim in candidate['product_claims']):
            if not _safe_epistemic_clause(sentence) and not (candidate['asks_question'] and sentence == candidate['question']['text']):
                return False, 'PRODUCT_CLAIM_REQUIRED', []
    return True, None, grounded


def _fact_number_values(fact):
    value = _raw_value(fact)
    values = []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        values.extend(raw for raw, _, _ in _number_spans(str(value)))
    for alias in _fact_aliases(fact):
        values.extend(raw for raw, _, _ in _number_spans(alias))
    return {_number_value(raw) for raw in values}


def _number_is_in_sources(raw, facts=(), memories=(), messages=()):
    target = _number_value(raw)
    for fact in facts:
        if target in _fact_number_values(fact):
            return True
    for memory in memories:
        if target in {_number_value(value) for value, _, _ in _number_spans(memory.get("approved_summary", ""))}:
            return True
    for message in messages:
        if target in {_number_value(value) for value, _, _ in _number_spans(message.get("text", ""))}:
            return True
    return False


def _specific_entities(text):
    entities = []
    sentence_starts = {0}
    sentence_starts.update(match.end() for match in re.finditer(r"[.!?\n]\s*", text))
    for match in _CAPITALIZED_RE.finditer(text):
        token = match.group(0)
        if not token or not token[0].isupper() or len(token) < 2:
            continue
        if match.end() < len(text) and text[match.end()].isdigit():
            continue
        prefix = text[:match.start()]
        if match.start() in sentence_starts or not prefix.strip() or prefix.rstrip().endswith((".", "!", "?", "\n")):
            continue
        if norm(token) in _GENERIC_CAPITALIZED:
            continue
        entities.append(norm(token))
    return entities


def _entity_in_sources(entity, facts=(), memories=(), messages=()):
    haystacks = []
    for fact in facts:
        haystacks.extend(_fact_aliases(fact))
    haystacks.extend(memory.get("approved_summary", "") for memory in memories)
    haystacks.extend(message.get("text", "") for message in messages)
    return any(_contains_phrase(norm(value), entity) for value in haystacks if isinstance(value, str))


def _all_specific_details_grounded(text, facts=(), memories=(), messages=()):
    for raw, _, _ in _number_spans(text):
        if not _number_is_in_sources(raw, facts, memories, messages):
            return False
    for entity in _specific_entities(text):
        if not _entity_in_sources(entity, facts, memories, messages):
            return False
    return True


_SAFE_EPISTEMIC_DEICTIC = (
    "dieu do|chuyen do|viec do|cau do|dieu nay|chuyen nay|viec nay|cau nay"
)
_SAFE_GENERIC_UNCERTAINTY_RE = re.compile(
    r"^(?:(?:a\s+)?(?:minh|toi|em)\s+)?"
    r"(?:khong the khang dinh|khong the xac nhan|khong dam khang dinh|"
    r"chua the xac nhan|chua ro|khong ro)\s+"
    r"(?:" + _SAFE_EPISTEMIC_DEICTIC + r")"
    r"(?:\s+nhe)?$"
)
_SAFE_UNKNOWN_DEICTIC_RE = re.compile(
    r"^(?:(?:a\s+)?(?:minh|toi|em)\s+)?"
    r"khong biet\s+(?:" + _SAFE_EPISTEMIC_DEICTIC + r")"
    r"(?:\s+nhe)?$"
)


def _clean_clause(text):
    """Normalize punctuation and whitespace for bounded clause matching."""
    return re.sub(r"\s+", " ", re.sub(r"[,.!?;:]+", " ", norm(text))).strip()


def _safe_epistemic_clause(text):
    """Recognize only generic, non-personal uncertainty/refusal clauses.

    The topical form may name a topic, but it cannot claim personal history,
    family facts, feelings, causes, or transactional follow-up. Open-ended
    ``I don't know that ...`` tails are deliberately excluded.
    """
    clause = _clean_clause(text)
    if not clause:
        return False
    if any(pattern.search(clause) for pattern in (
            _FAMILY_RE, _SPOUSE_OPINION_RE, _EMOTION_RE, _CAUSE_RE,
            _TIME_RE, _TRANSACTION_RE, _SELF_HISTORY_RE)):
        return False
    if _SAFE_TOPICAL_NO_INFO_RE.fullmatch(clause):
        topic = clause.split(" thi ", 1)[0]
        # Generic topics are allowed ("còn đi lâu chưa"), but a first-person
        # history cannot become safe just by appending "mình chưa có thông tin".
        # "trẻ em" is a noun phrase, not the first-person pronoun "em".
        topic = re.sub(r"\btre em\b", "", topic)
        return not re.search(r"\b(?:minh|toi|em)\b", topic)
    return any(pattern.fullmatch(clause) for pattern in (
        _SAFE_NO_INFO_RE, _SAFE_GENERIC_UNCERTAINTY_RE, _SAFE_UNKNOWN_DEICTIC_RE))


def _explicit_negation(text):
    # A separate safe uncertainty clause must not turn a positive persona
    # claim into a negated claim. Keep every other clause in the negation
    # check, so this cannot bless personal-history statements.
    clauses = re.split(r"[,;:]+|\s+(?:nhung|va)\s+", text, flags=re.IGNORECASE)
    substantive = [clause for clause in clauses if not _safe_epistemic_clause(clause)]
    return bool(_NEGATION_RE.search(norm(" ".join(substantive))))


def _date_supported(text, facts, memories):
    if not _TIME_RE.search(norm(text)):
        return True
    for fact in facts:
        key = fact.get("key") or fact.get("canonical_key")
        if (key in {"purchase.date", "purchase.year", "vehicle.current.usage_started_at",
                    "vehicle.current.year", "vehicle.history.ownership", "story.travel.destination"}
                or is_generated_key(key)) \
                and _fact_value_supported(text, fact):
            return True
    return any(memory.get("occurred_at") and _contains_phrase(norm(text), norm(str(memory["occurred_at"])))
               for memory in memories)


def _soft_fabrication_reason(text, predicates, facts, memories, messages):
    normalized = norm(text)
    referenced_text = " ".join(
        [str(_raw_value(fact) or "") for fact in facts]
        + [str(memory.get("approved_summary", "")) for memory in memories]
        + [str(message.get("text", "")) for message in messages]
    )
    if _SPOUSE_OPINION_RE.search(normalized) and not _SPOUSE_OPINION_RE.search(norm(referenced_text)):
        return "UNSUPPORTED_PERSONAL_DETAIL"
    family_terms = bool(_FAMILY_RE.search(normalized))
    family_supported = any(predicate.startswith(("family.", "gen.family")) for predicate in predicates) \
        or bool(_FAMILY_RE.search(norm(referenced_text)))
    if family_terms and not family_supported:
        return "UNSUPPORTED_PERSONAL_DETAIL"
    if _EMOTION_RE.search(normalized):
        feeling_prefixes = ("opinion.", "preference.", "gen.taste", "gen.hobby", "gen.memory", "gen.habit")
        feeling_supported = any(predicate.startswith(feeling_prefixes) for predicate in predicates) \
            and any(_fact_value_supported(text, fact) for fact in facts if
                    (fact.get("key") or fact.get("canonical_key", "")).startswith(feeling_prefixes))
        if not feeling_supported and not _EMOTION_RE.search(norm(referenced_text)):
            return "UNSUPPORTED_PERSONAL_DETAIL"
    if _CAUSE_RE.search(normalized) and not _CAUSE_RE.search(norm(referenced_text)):
        return "UNSUPPORTED_PERSONAL_DETAIL"
    if _TIME_RE.search(normalized) and not _date_supported(text, facts, memories):
        return "UNSUPPORTED_PERSONAL_DETAIL"
    return None


def _question_count(text):
    count = 0
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    for chunk in chunks:
        if not chunk.strip():
            continue
        marks = len(re.findall(r"[?？]", chunk))
        n = norm(chunk)
        wh = list(re.finditer(
            r"\b(?:gi|nao|sao|dau|the nao|nhu nao|bao lau|bao nhieu|may tuoi|ai)\b", n))
        if marks:
            count += max(marks, len(wh))
            continue
        # A bounded ``topic ... thì mình chưa có thông tin`` clause reports
        # uncertainty. Its embedded question words are not a question to the
        # reader unless the author used an explicit question mark.
        if _safe_epistemic_clause(chunk):
            continue
        if re.search(r"\bco gi dau\b", n):
            continue
        # Count every interrogative particle after an addressee. This catches
        # multiple unpunctuated questions such as "Bạn đi phố à Bạn đổi xe à".
        address = re.search(r"\b(?:ban|anh|chi|bac|chu|co|em)\b", n)
        if not address:
            continue
        rest = n[address.end():]
        wh_matches = list(re.finditer(
            r"\b(?:gi|nao|sao|dau|the nao|nhu nao|bao lau|bao nhieu|may tuoi|ai)\b", rest))
        if wh_matches:
            count += len(wh_matches)
            continue
        if re.search(r"\bhay\b", rest):
            count += 1
            continue
        tail_matches = list(re.finditer(r"\b(?:khong|chua|a|nhi|ha)\b", rest))
        for match in tail_matches:
            suffix = rest[match.end():]
            if not suffix.strip() or re.match(r"\s*[,;]?\s*(?:ban|anh|chi|bac|chu|co|em)\b", suffix):
                count += 1
    return count


def _valid_question(candidate, allowed_question):
    asks = candidate["asks_question"]
    question = candidate["question"]
    if not asks:
        return "QUESTION_NOT_ALLOWED" if _question_count(candidate["reply_text"]) else None
    if not isinstance(allowed_question, dict) or set(allowed_question) != {"kind", "field"}:
        return "QUESTION_NOT_ALLOWED"
    if not isinstance(allowed_question.get("kind"), str) \
            or allowed_question["kind"] not in _QUESTION_KINDS \
            or allowed_question.get("field") is not None \
            and (not isinstance(allowed_question["field"], str)
                 or allowed_question["field"] not in _QUESTION_FIELDS):
        return "QUESTION_NOT_ALLOWED"
    if question["kind"] != allowed_question["kind"] or question["field"] != allowed_question["field"]:
        return "QUESTION_NOT_ALLOWED"
    if _question_count(candidate["reply_text"]) != 1:
        return "REPLY_LIMIT_EXCEEDED"
    if _question_count(question["text"]) != 1:
        return "QUESTION_MISMATCH"
    return None


def _memory_checksum(memory):
    payload = {key: value for key, value in memory.items()
               if key not in {"checksum", "eligible_for_generation"}}
    return _digest_or_none(payload)


def _valid_memory(memory, context, facts_by_id, allowed_ids, revoked_ids=()):
    if not isinstance(memory, dict) or memory.get("memory_id") not in allowed_ids:
        return False
    memory_id = memory.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id or memory_id in revoked_ids \
            or memory.get("persona_uuid") != context["persona_uuid"] \
            or memory.get("release_id") != context["release_id"]:
        return False
    fictional_story = memory.get('label') == 'fictional_generated'
    expected_approval = "fixture" if context["fixture"] else "approved"
    if fictional_story:
        if memory.get('approval') != 'generated' or memory.get('source_kind') != 'generated':
            return False
    else:
        if memory.get("approval") != expected_approval or memory.get("source_kind") == "fictional_fixture" and not context["fixture"]:
            return False
        if context["fixture"] and memory.get("source_kind") != "fictional_fixture":
            return False
    if memory.get("revoked") is True or memory.get("revoked_at"):
        return False
    if memory.get("tellable") not in (True, "public") or not _privacy_is_public(memory):
        return False
    if not isinstance(memory.get("source_ref"), str) or not memory["source_ref"].strip():
        return False
    if not fictional_story and not context["fixture"] and (not isinstance(memory.get("consent_ref"), str)
                                    or not memory["consent_ref"].strip()):
        return False
    if not isinstance(memory.get("approved_summary"), str) or not memory["approved_summary"].strip() \
            or len(memory["approved_summary"]) > 600:
        return False
    ids = memory.get("fact_ids")
    if not _id_list(ids, max_len=30) or not ids or any(value not in facts_by_id for value in ids):
        return False
    try:
        if not isinstance(memory.get("checksum"), str) or memory["checksum"] != _memory_checksum(memory):
            return False
    except (TypeError, ValueError):
        return False
    linked = [facts_by_id[value] for value in ids]
    if not any(_fact_value_supported(memory["approved_summary"], fact) for fact in linked):
        return False
    if not _all_specific_details_grounded(memory["approved_summary"], linked):
        return False
    if _soft_fabrication_reason(memory["approved_summary"],
                                [fact.get("key") or fact.get("canonical_key", "") for fact in linked],
                                linked, [memory], []):
        return False
    return True


def _same_span_persona_sources(claim, claims, facts_by_id, memory_by_id,
                               context, allowed_memory_ids, revoked_memory_ids):
    """Collect only approved sources explicitly cited by persona claims on this span.

    The per-claim predicate check still uses that claim's own citations. This
    narrower group is for checking other names, numbers, and dates that appear
    in a shared sentence containing multiple independently grounded claims.
    """
    fact_ids, memory_ids, predicates = set(), set(), set()
    for other in claims:
        if other.get("subject") != "persona" or other.get("text") != claim["text"]:
            continue
        fact_ids.update(other.get("fact_ids", ()))
        memory_ids.update(other.get("memory_ids", ()))
        predicate = other.get("predicate")
        if predicate in _PERSONA_PREDICATES:
            predicates.add(predicate)

    facts = [facts_by_id[value] for value in sorted(fact_ids) if value in facts_by_id]
    memories = [memory_by_id[value] for value in sorted(memory_ids)
                if value in memory_by_id
                and _valid_memory(memory_by_id[value], context, facts_by_id,
                                  allowed_memory_ids, revoked_memory_ids)]
    linked_fact_ids = {fact_id for memory in memories for fact_id in memory.get("fact_ids", [])}
    known_fact_ids = {fact["fact_id"] for fact in facts}
    facts.extend(facts_by_id[value] for value in sorted(linked_fact_ids - known_fact_ids)
                 if value in facts_by_id)
    return facts, memories, predicates


def _messages_by_id(messages):
    if not isinstance(messages, (list, tuple)) or len(messages) > 500:
        return None
    result = {}
    for message in messages:
        if not isinstance(message, dict):
            return None
        message_id = message.get("message_id")
        text = message.get("text")
        if not isinstance(message_id, str) or not message_id or message_id in result \
                or not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            return None
        result[message_id] = message
    return result


def _customer_claim_supported(claim, messages):
    text = claim["text"]
    evidence = [messages.get(message_id) for message_id in claim["message_ids"]]
    if not evidence or any(not isinstance(item, dict) for item in evidence):
        return False
    actors = [item.get("actor_id") for item in evidence]
    if any(not isinstance(actor, str) or not actor for actor in actors) or len(set(actors)) != 1:
        return False
    scope_values = [item.get(key) for item in evidence
                    for key in ("page_id", "post_id", "thread_root_id")]
    if any(value is not None and not isinstance(value, (str, int)) for value in scope_values):
        return False
    scopes = [tuple(item.get(key) for key in ("page_id", "post_id", "thread_root_id"))
              for item in evidence]
    if len(set(scopes)) != 1:
        return False
    for item in evidence:
        role = item.get("role", item.get("actor_role"))
        if not isinstance(role, str) or role not in _CUSTOMER_ROLES \
                or item.get("verified") is False or item.get("is_verified") is False:
            return False
    predicate = claim["predicate"]
    n = norm(text)
    mapping = {
        "current_vehicle": r"\b(?:xe|di|lai|chay|su dung|dang di|dang lai)\b",
        "interested_vehicle": r"\b(?:quan tam|tim hieu|thich|muon xem|dang tim)\b",
        "purchase_timeline": r"\b(?:mua|du dinh|dinh|nam|thang|khi nao|bao gio|sau|truoc)\b",
        "finance_intent": r"\b(?:tra gop|tra truoc|vay|tra thang|tien mat|tai chinh)\b",
        "budget": r"\b(?:ngan sach|tam gia|khoang|trong tam|trieu|ty|dong|budget)\b",
        "usage_need": r"\b(?:di lai|di pho|di xa|di lam|gia dinh|cho con|hang ngay|nhu cau|su dung)\b",
        "ownership_status": r"\b(?:da co|dang di|dang so huu|mua roi|xe cua minh|co xe)\b",
        "purchase_intent": r"\b(?:mua|tim mua|du dinh|se mua|dang tim|muon mua)\b",
    }
    if not re.search(mapping[predicate], n):
        return False
    evidence_text = " ".join(norm(item["text"]) for item in evidence)
    tokens = [token for token in re.findall(r"[^\W\d_]+", n, re.UNICODE)
              if token not in _STOP_WORDS and len(token) > 2]
    if any(not re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", evidence_text)
           for token in tokens):
        return False
    return _all_specific_details_grounded(text, messages=evidence)


def _sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _unsupported_personal_sentence(text, claims, question, product_claims=()):
    for sentence in _sentences(text):
        if _SAFE_AI_DISCLOSURE_RE.fullmatch(norm(sentence)):
            continue
        remainder = sentence
        for claim in claims:
            remainder = remainder.replace(claim["text"], " ")
        for claim in product_claims:
            remainder = remainder.replace(claim['text'], ' ')
        if question:
            remainder = remainder.replace(question, " ")
        remainder = _clean_clause(remainder)
        if not remainder or _BENIGN_REACTION_RE.fullmatch(remainder) \
                or _safe_epistemic_clause(remainder):
            continue
        return True
    return False


def _make_manifest(candidate, context, facts, memories, messages, ids, product_facts=None):
    evidence_fact_ids = set(ids["fact_ids"])
    for memory_id in ids["memory_ids"]:
        evidence_fact_ids.update(memories[memory_id].get("fact_ids", []))
    fact_rows = []
    for item in sorted((facts[value] for value in evidence_fact_ids), key=lambda row: row["fact_id"]):
        fact_rows.append({"fact_id": item["fact_id"], "source_ref": item["source_ref"],
                          "checksum": _digest_or_none(item)})
    memory_rows = []
    for item in sorted((memories[value] for value in ids["memory_ids"]), key=lambda row: row["memory_id"]):
        memory_rows.append({"memory_id": item["memory_id"], "source_ref": item["source_ref"],
                            "checksum": item["checksum"]})
    message_rows = []
    for item in sorted((messages[value] for value in ids["message_ids"]), key=lambda row: row["message_id"]):
        message_rows.append({"message_id": item["message_id"],
                             "actor_id": item.get("actor_id"),
                             "checksum": _digest_or_none(item)})
    manifest = {
        "version": MANIFEST_VERSION,
        "persona_uuid": context["persona_uuid"],
        "release_id": context["release_id"],
        "release_checksum": context.get("release_checksum"),
        "candidate_digest": generation_digest(candidate),
        "fact_ids": sorted(evidence_fact_ids),
        "claim_fact_ids": sorted(ids["fact_ids"]), "facts": fact_rows,
        "memory_ids": sorted(ids["memory_ids"]), "memories": memory_rows,
        "message_ids": sorted(ids["message_ids"]), "messages": message_rows,
    }
    if candidate.get('product_claims'):
        by_id = product_facts or {}
        manifest['product_fact_ids'] = sorted({claim['product_fact_id'] for claim in candidate['product_claims']})
        manifest['product_facts'] = [{'id': fact_id, 'type': by_id[fact_id].get('type'),
            'checksum': _digest_or_none(by_id[fact_id])} for fact_id in manifest['product_fact_ids']
            if fact_id in by_id]
    manifest["digest"] = generation_digest(manifest)
    return manifest


def validate_generated_reply(candidate, snapshot, *, messages=(), allowed_memory_ids=(),
                             allowed_question=None, revoked_ids=(),
                             allow_fictional_fixture=False, channel=None,
                             product_facts=None, product_verified_at=None, now=None,
                             _max_text_length=600) -> dict:
    """Validate a generated reply against approved facts and current turn evidence.

    ``REVIEW`` retains a short candidate string for the operator draft queue. It
    is never interchangeable with ``OK``; callers must gate sending on status.
    """
    if isinstance(candidate, dict):
        candidate = copy.deepcopy(candidate)
        candidate.setdefault('product_claims', [])
    text = _validate_shape(candidate, max_text_length=_max_text_length)
    if text is None:
        return _review("INVALID_CANDIDATE", candidate)
    if len(candidate["memory_ids"]) > 1:
        return _review("MEMORY_LIMIT_EXCEEDED", candidate)
    if len(text) > _max_text_length or not text.strip():
        return _review("REPLY_LIMIT_EXCEEDED", candidate)
    if not isinstance(revoked_ids, (list, tuple, set, frozenset)) \
            or any(not isinstance(value, str) for value in revoked_ids):
        return _review("REVOCATION_DATA_INVALID", candidate)
    if not isinstance(allowed_memory_ids, (list, tuple, set, frozenset)) \
            or any(not isinstance(value, str) for value in allowed_memory_ids):
        return _review("MEMORY_ALLOWLIST_INVALID", candidate)
    context = _snapshot_context(snapshot)
    if not context:
        source_kind = snapshot.get("source_kind") if isinstance(snapshot, dict) else None
        if source_kind == "fictional_fixture" and allow_fictional_fixture is not True:
            return _review("FICTIONAL_FIXTURE_DISABLED", candidate)
        return _review("RELEASE_NOT_APPROVED", candidate)
    if context["fixture"] and allow_fictional_fixture is not True:
        return _review("FICTIONAL_FIXTURE_DISABLED", candidate)
    chain = _snapshot_chain(snapshot)
    raw_facts = next((part["facts"] for part in chain if isinstance(part.get("facts"), list)), None)
    if raw_facts is None:
        return _review("RELEASE_INVALID", candidate)
    facts_list = allowed_generation_facts(snapshot, revoked_ids=revoked_ids,
                                         allow_fictional_fixture=allow_fictional_fixture)
    facts_by_id = {item["fact_id"]: item for item in facts_list}
    if len(facts_by_id) != len(facts_list):
        return _review("FACT_SCOPE_MISMATCH", candidate)
    if any(value not in facts_by_id for value in candidate["fact_ids"]):
        return _review("FACT_NOT_ALLOWED", candidate)
    if any(fact_id not in facts_by_id for claim in candidate["claims"]
           for fact_id in claim["fact_ids"]):
        return _review("FACT_NOT_ALLOWED", candidate)
    product_by_id = {fact.get('id'): fact for fact in product_facts or () if isinstance(fact, dict)}
    product_ok, product_error, product_grounding = _product_claims_valid(
        candidate, product_facts, now or datetime.now().astimezone(), product_verified_at)
    if not product_ok:
        return _review(product_error, candidate)
    authored_post, authored_post_error = _persona_authored_post(snapshot, context, facts_by_id)
    if authored_post_error:
        return _review(authored_post_error, candidate)
    if _has_conflicting_persona_claim_facts(candidate, facts_by_id):
        return _review("CONFLICTING_PERSONA_CLAIMS", candidate)
    if _contradicts_persona_authored_post(candidate, authored_post, facts_by_id):
        return _review("PERSONA_POST_CONTRADICTION", candidate)
    raw_memories = next((part["memories"] for part in chain
                         if isinstance(part.get("memories"), list)), [])
    revoked_memory_ids = set(revoked_ids)
    for part in chain:
        part_revoked = part.get("revoked_memory_ids", ())
        if not isinstance(part_revoked, (list, tuple, set, frozenset)) \
                or any(not isinstance(value, str) for value in part_revoked):
            return _review("REVOCATION_DATA_INVALID", candidate)
        revoked_memory_ids.update(part_revoked)
    if not isinstance(raw_memories, list):
        raw_memories = []
    if not _id_list(list(allowed_memory_ids), max_len=500):
        return _review("MEMORY_ALLOWLIST_INVALID", candidate)
    allowed_memories = set(allowed_memory_ids)
    memory_rows = [item for item in raw_memories if isinstance(item, dict)]
    memory_ids_in_snapshot = [item.get("memory_id") for item in memory_rows]
    if any(not isinstance(value, str) or not value for value in memory_ids_in_snapshot) \
            or len(memory_ids_in_snapshot) != len(set(memory_ids_in_snapshot)):
        return _review("MEMORY_SCOPE_MISMATCH", candidate)
    memory_by_id = {item.get("memory_id"): item for item in memory_rows
                    if isinstance(item.get("memory_id"), str)}
    if any(value not in allowed_memories or value not in memory_by_id
           for value in candidate["memory_ids"]):
        return _review("MEMORY_NOT_ALLOWED", candidate)
    message_by_id = _messages_by_id(messages)
    if message_by_id is None:
        return _review("MESSAGE_EVIDENCE_INVALID", candidate)

    claim_fact_ids = set()
    claim_memory_ids = set()
    claim_message_ids = set()
    used_predicates = []
    used_facts = []
    used_memories = []
    used_messages = []
    for item in candidate["claims"]:
        predicate, subject = item["predicate"], item["subject"]
        fact_ids, memory_ids, message_ids = item["fact_ids"], item["memory_ids"], item["message_ids"]
        if subject == "persona":
            if predicate not in _PERSONA_PREDICATES:
                return _review("CLAIM_PREDICATE_INVALID", candidate)
            if message_ids:
                return _review("SOURCE_SUBJECT_MISMATCH", candidate)
            if not fact_ids and not memory_ids:
                return _review("CLAIM_EVIDENCE_MISMATCH", candidate)
            if any(value not in facts_by_id for value in fact_ids):
                return _review("FACT_NOT_ALLOWED", candidate)
            if any(value not in allowed_memories or value not in memory_by_id for value in memory_ids):
                return _review("MEMORY_NOT_ALLOWED", candidate)
            cited_facts = [facts_by_id[value] for value in fact_ids]
            cited_memories = []
            for memory_id in memory_ids:
                memory = memory_by_id[memory_id]
                if not _valid_memory(memory, context, facts_by_id, allowed_memories,
                                     revoked_memory_ids):
                    return _review("MEMORY_INVALID", candidate)
                cited_memories.append(memory)
            predicate_facts = [fact for fact in cited_facts
                               if (fact.get("key") or fact.get("canonical_key")) == predicate
                               or predicate_for(fact.get("key") or fact.get("canonical_key")) == predicate]
            memory_predicates = [facts_by_id[fact_id] for memory in cited_memories
                                 for fact_id in memory.get("fact_ids", [])
                                 if (facts_by_id[fact_id].get("key") or facts_by_id[fact_id].get("canonical_key")) == predicate
                                 or predicate_for(facts_by_id[fact_id].get("key") or facts_by_id[fact_id].get("canonical_key")) == predicate]
            if not predicate_facts and not memory_predicates:
                return _review("CLAIM_PREDICATE_MISMATCH", candidate)
            if any(not _fact_value_supported(item["text"], fact) for fact in cited_facts):
                return _review("CLAIM_VALUE_MISMATCH", candidate)
            if any(not any(_fact_value_supported(item["text"], fact) for fact in
                           (facts_by_id[fact_id] for fact_id in memory.get("fact_ids", [])))
                   for memory in cited_memories):
                return _review("CLAIM_VALUE_MISMATCH", candidate)
            if _explicit_negation(item["text"]):
                negative_supported = any(
                    _raw_value(fact) is False and _fact_value_supported(item["text"], fact)
                    for fact in cited_facts
                ) and len(_NEGATION_RE.findall(norm(item["text"]))) == 1
                negative_supported = negative_supported or any(
                    isinstance(_raw_value(fact), str)
                    and _explicit_negation(_raw_value(fact))
                    and _contains_phrase(norm(item["text"]), norm(_raw_value(fact)))
                    for fact in cited_facts
                )
                if not negative_supported:
                    return _review("CLAIM_NEGATION_MISMATCH", candidate)
            detail_facts, detail_memories, detail_predicates = _same_span_persona_sources(
                item, candidate["claims"], facts_by_id, memory_by_id, context,
                allowed_memories, revoked_memory_ids)
            if not _date_supported(item["text"], detail_facts,
                                   detail_memories):
                return _review("UNSUPPORTED_PERSONAL_DETAIL", candidate)
            reason = _soft_fabrication_reason(item["text"], detail_predicates,
                                              detail_facts, detail_memories, [])
            if reason:
                return _review(reason, candidate)
            if not _all_specific_details_grounded(item["text"], detail_facts, detail_memories):
                return _review("UNSUPPORTED_SPECIFIC_DETAIL", candidate)
            used_facts.extend(cited_facts)
            used_memories.extend(cited_memories)
        else:
            if predicate not in _CUSTOMER_PREDICATES:
                return _review("CLAIM_PREDICATE_INVALID", candidate)
            if fact_ids or memory_ids:
                return _review("SOURCE_SUBJECT_MISMATCH", candidate)
            if not message_ids or any(value not in message_by_id for value in message_ids):
                return _review("MESSAGE_EVIDENCE_MISSING", candidate)
            cited_messages = [message_by_id[value] for value in message_ids]
            if not _customer_claim_supported(item, message_by_id):
                return _review("MESSAGE_EVIDENCE_MISMATCH", candidate)
            used_messages.extend(cited_messages)
        used_predicates.append(predicate)
        claim_fact_ids.update(fact_ids)
        claim_memory_ids.update(memory_ids)
        claim_message_ids.update(message_ids)

    if set(candidate["fact_ids"]) != claim_fact_ids or set(candidate["memory_ids"]) != claim_memory_ids:
        return _review("CLAIM_EVIDENCE_MISMATCH", candidate)
    if len(candidate["fact_ids"]) != len(claim_fact_ids) or len(candidate["memory_ids"]) != len(claim_memory_ids):
        return _review("CLAIM_EVIDENCE_MISMATCH", candidate)
    if candidate["fact_ids"] and any(value not in facts_by_id for value in candidate["fact_ids"]):
        return _review("FACT_NOT_ALLOWED", candidate)

    question_error = _valid_question(candidate, allowed_question)
    if question_error:
        return _review(question_error, candidate)
    question_text = candidate["question"]["text"] if candidate["asks_question"] else None
    normalized_text = norm(text)
    if _TRANSACTION_RE.search(normalized_text):
        return _review("TRANSACTIONAL_CONTENT", candidate)
    if _SYSTEM_RE.search(normalized_text):
        return _review("SYSTEM_OR_IDENTITY_CLAIM", candidate)
    effective_channel = channel or (snapshot.get("channel") if isinstance(snapshot, dict) else None)
    if effective_channel == "public" and _CONTACT_TERM_RE.search(normalized_text) \
            and _CONTACT_ASK_RE.search(normalized_text):
        return _review("PUBLIC_CONTACT_SOLICITATION", candidate)
    if _unsupported_personal_sentence(text, candidate["claims"], question_text, candidate['product_claims']):
        return _review("UNSUPPORTED_PERSONAL_DETAIL", candidate)
    grounding_messages = list(used_messages) + [{'text': item['text']} for item in product_grounding]

    # A text mention can be copied from another message without becoming a
    # persona fact. Only IDs actually cited above contribute to this manifest.
    used_facts_by_id = {item["fact_id"]: item for item in used_facts}
    used_memories_by_id = {item["memory_id"]: item for item in used_memories}
    used_messages_by_id = {item["message_id"]: item for item in used_messages}
    overall_soft_reason = _soft_fabrication_reason(
        text, used_predicates, list(used_facts_by_id.values()),
        list(used_memories_by_id.values()), grounding_messages)
    if overall_soft_reason:
        return _review(overall_soft_reason, candidate)
    if not _all_specific_details_grounded(text, list(used_facts_by_id.values()),
                                          list(used_memories_by_id.values()), grounding_messages):
        return _review("UNSUPPORTED_SPECIFIC_DETAIL", candidate)
    # Exact top-level evidence unions prevent hidden, unreferenced sources.
    ids = {"fact_ids": claim_fact_ids, "memory_ids": claim_memory_ids,
           "message_ids": claim_message_ids}
    manifest = _make_manifest(candidate, context, facts_by_id, memory_by_id,
                              message_by_id, ids, product_by_id)
    return {"status": "OK", "text": text,
            "fact_ids": sorted(claim_fact_ids), "memory_ids": sorted(claim_memory_ids),
            "reason": None, "manifest": manifest}


def validate_generated_post(candidate, snapshot, *, messages=(), allowed_memory_ids=(),
                             revoked_ids=(), allow_fictional_fixture=False,
                             channel="public") -> dict:
    """Validate authored post content with the reply fact and claim safety gates.

    The caller validates and persists any ``fact_proposals`` first, then passes
    this normalized content candidate against the same-persona snapshot.
    """
    expected = {"post_text", "fact_ids", "memory_ids", "claims", "asks_question", "question"}
    if not isinstance(candidate, dict) or set(candidate) != expected:
        return _review("INVALID_CANDIDATE", candidate)
    text = _safe_candidate_text({"reply_text": candidate.get("post_text")}, max_length=3000)
    if text is None or text != candidate["post_text"]:
        return _review("INVALID_CANDIDATE", candidate)
    normalized = {**candidate, "reply_text": text}
    normalized.pop("post_text")
    question = candidate.get("question")
    allowed_question = ({"kind": question.get("kind"), "field": question.get("field")}
                        if candidate.get("asks_question") and isinstance(question, dict) else None)
    result = validate_generated_reply(
        normalized, snapshot, messages=messages, allowed_memory_ids=allowed_memory_ids,
        allowed_question=allowed_question, revoked_ids=revoked_ids,
        allow_fictional_fixture=allow_fictional_fixture, channel=channel,
        _max_text_length=3000)
    result["manifest"]["candidate_digest"] = _digest_or_none(candidate)
    result["manifest"]["digest"] = generation_digest({
        key: value for key, value in result["manifest"].items() if key != "digest"})
    return result

