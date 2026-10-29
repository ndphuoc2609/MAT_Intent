"""Select only checksum-verified, approved persona utterances from a release."""
from __future__ import annotations

import hashlib
import json
import re


_CHECKSUM_FIELDS = (
    "utterance_id", "persona_uuid", "release_id", "text", "template",
    "purpose", "topic", "key_conditions", "required_fact_ids", "claims",
    "style", "hook", "approval", "source_kind",
)


def utterance_checksum(utterance: dict) -> str:
    """Stable digest for the fields that can change an utterance's meaning."""
    payload = {key: utterance.get(key) for key in _CHECKSUM_FIELDS if key in utterance}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _approved(value, *, fixture=False) -> bool:
    return value == "fixture" if fixture else value is True or value == "approved"


def _review(reason: str) -> dict:
    return {"status": "REVIEW", "text": None, "utterance_ids": [],
            "fact_ids": [], "reason": reason}


def _matching_utterances(snapshot: dict, topic, *, fixture=False):
    return [utterance for utterance in snapshot.get("utterances", [])
            if isinstance(utterance, dict)
            and utterance.get("purpose") in {"answer_persona", "answer"}
            and (utterance.get("source_kind") in (None, "fictional_fixture") if fixture
                 else utterance.get("source_kind") != "fictional_fixture")
            and (topic is None or utterance.get("topic") == topic)]


def _validate_plan(plan, candidates):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "utterance_ids", "hook_id"} \
            or plan.get("schema_version") != "persona_reply_plan_v1":
        return None, "INVALID_PLAN"
    ids = plan.get("utterance_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 3 \
            or any(not isinstance(item, str) or not item for item in ids) \
            or len(set(ids)) != len(ids):
        return None, "INVALID_PLAN"
    available = {item.get("utterance_id") for item in candidates}
    if any(item not in available for item in ids):
        return None, "UTTERANCE_NOT_ELIGIBLE"
    if plan.get("hook_id") is not None:
        return None, "HOOK_NOT_AVAILABLE"
    return ids, None


def _validate_utterance(snapshot, utterance, revoked_ids, *, fixture=False):
    persona_uuid, release_id = snapshot["persona_uuid"], snapshot["release_id"]
    if utterance.get("persona_uuid") != persona_uuid or utterance.get("release_id") != release_id:
        return None, "UTTERANCE_SCOPE_MISMATCH"
    if fixture and utterance.get("source_kind") not in (None, "fictional_fixture"):
        return None, "FIXTURE_SOURCE_MISMATCH"
    if not fixture and utterance.get("source_kind") == "fictional_fixture":
        return None, "FICTIONAL_FIXTURE_DISABLED"
    if not _approved(utterance.get("approval"), fixture=fixture):
        return None, "UTTERANCE_NOT_APPROVED"
    text = utterance.get("text")
    if not isinstance(utterance.get("utterance_id"), str) or not utterance["utterance_id"] \
            or not isinstance(text, str) or not text.strip() or len(text) > 600 \
            or "\x00" in text or "{" in text or "}" in text:
        return None, "UTTERANCE_INVALID"
    checksum = utterance.get("checksum")
    release = snapshot.get("release") or (snapshot.get("snapshot") or {}).get("release") or {}
    if checksum != utterance_checksum(utterance) \
            and (not isinstance(release, dict) or not release.get("checksum")
                 or checksum != release["checksum"]):
        return None, "UTTERANCE_CHECKSUM_MISMATCH"

    facts = {}
    for fact in snapshot.get("facts", []):
        if not isinstance(fact, dict) or not isinstance(fact.get("fact_id"), str):
            return None, "FACT_SCOPE_MISMATCH"
        if fact["fact_id"] in facts:
            return None, "FACT_SCOPE_MISMATCH"
        facts[fact["fact_id"]] = fact
    required = utterance.get("required_fact_ids", [])
    claims = utterance.get("claims", [])
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required) \
            or len(set(required)) != len(required) \
            or not isinstance(claims, list):
        return None, "CLAIM_EVIDENCE_MISSING"
    if any(fact_id in revoked_ids for fact_id in required):
        return None, "FACT_REVOKED"

    claim_fact_ids = set()
    for claim in claims:
        if not isinstance(claim, dict):
            return None, "CLAIM_EVIDENCE_MISSING"
        claim_text = claim.get("text")
        fact_id = claim.get("fact_id")
        fact_ids = claim.get("required_fact_ids") or ([fact_id] if isinstance(fact_id, str) else None)
        evidence = claim.get("evidence")
        if evidence is None and isinstance(fact_id, str):
            evidence = [{"fact_id": fact_id, "source_ref": claim.get("source_ref")}]
        if not isinstance(claim_text, str) or not claim_text.strip() or claim_text not in text \
                or not isinstance(fact_ids, list) or not fact_ids \
                or any(not isinstance(item, str) or not item for item in fact_ids) \
                or len(set(fact_ids)) != len(fact_ids) \
                or not isinstance(evidence, list):
            return None, "CLAIM_EVIDENCE_MISSING"
        if any(not isinstance(item, dict) or not isinstance(item.get("fact_id"), str)
               for item in evidence):
            return None, "CLAIM_EVIDENCE_MISSING"
        evidence_by_id = {item["fact_id"]: item for item in evidence}
        if len(evidence_by_id) != len(evidence):
            return None, "CLAIM_EVIDENCE_MISSING"
        if set(evidence_by_id) != set(fact_ids):
            return None, "CLAIM_EVIDENCE_MISSING"
        for fact_id in fact_ids:
            fact = facts.get(fact_id)
            evidence_item = evidence_by_id.get(fact_id) or {}
            if not fact:
                return None, "FACT_NOT_IN_RELEASE"
            if fact.get("persona_uuid") not in (None, persona_uuid) \
                    or fact.get("release_id") not in (None, release_id):
                return None, "FACT_SCOPE_MISMATCH"
            if fixture and fact.get("source_kind") != "fictional_fixture":
                return None, "FIXTURE_SOURCE_MISMATCH"
            if not fixture and fact.get("source_kind") == "fictional_fixture":
                return None, "FICTIONAL_FIXTURE_DISABLED"
            if fact_id in revoked_ids:
                return None, "FACT_REVOKED"
            if fact.get("tellable") not in (True, "public") \
                    or not _approved(fact.get("approval"), fixture=fixture):
                return None, "FACT_NOT_TELLABLE"
            source_ref = fact.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref.strip() \
                    or evidence_item.get("source_ref") != source_ref:
                return None, "CLAIM_EVIDENCE_MISSING"
            claim_fact_ids.add(fact_id)
    if claim_fact_ids != set(required):
        return None, "CLAIM_EVIDENCE_MISSING"
    return text.strip(), None


def _within_reply_limits(text: str) -> bool:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    claim_paragraphs = sum(not re.search(r"[?？]", part) for part in paragraphs)
    return len(text) <= 600 and claim_paragraphs <= 3 and len(re.findall(r"[?？]", text)) <= 1


def render_persona_reply(release_snapshot: dict, *, topic=None, plan=None,
                         revoked_ids=(), max_chars=600,
                         allow_fictional_fixture=False) -> dict:
    """Render a release-bound reply; a selector may choose IDs, never wording."""
    if not isinstance(release_snapshot, dict) \
            or not isinstance(release_snapshot.get("persona_uuid"), str) \
            or not isinstance(release_snapshot.get("release_id"), str):
        return _review("RELEASE_NOT_APPROVED")
    if not isinstance(release_snapshot.get("facts"), list) \
            or not isinstance(release_snapshot.get("utterances"), list):
        return _review("RELEASE_INVALID")
    source_kind = release_snapshot.get("source_kind") or \
        (release_snapshot.get("snapshot", {}).get("persona") or {}).get("source_kind")
    fixture = source_kind == "fictional_fixture"
    release = release_snapshot.get("release") or (release_snapshot.get("snapshot") or {}).get("release") or {}
    if fixture:
        if allow_fictional_fixture is not True:
            return _review("FICTIONAL_FIXTURE_DISABLED")
        if release_snapshot.get("approved") is not False \
                or release_snapshot.get("fixture_only") is not True \
                or not isinstance(release, dict) or release.get("status") != "FIXTURE":
            return _review("RELEASE_NOT_APPROVED")
    elif release_snapshot.get("approved") is not True \
            or release_snapshot.get("fixture_only") is True \
            or (isinstance(release, dict) and release.get("status") not in (None, "PUBLISHED")):
        return _review("RELEASE_NOT_APPROVED")
    if topic is not None and not isinstance(topic, str):
        return _review("INVALID_TOPIC")
    if not isinstance(revoked_ids, (list, tuple, set, frozenset)) \
            or any(not isinstance(item, str) for item in revoked_ids):
        return _review("REVOCATION_DATA_INVALID")

    candidates = _matching_utterances(release_snapshot, topic, fixture=fixture)
    eligible = [item for item in candidates if _approved(item.get("approval"), fixture=fixture)]
    ids = [item.get("utterance_id") for item in candidates]
    if any(not isinstance(item, str) or not item for item in ids) \
            or len(ids) != len(set(ids)):
        return _review("DUPLICATE_OR_INVALID_UTTERANCE_ID")
    if plan is None:
        if not eligible and candidates:
            return _review("UTTERANCE_NOT_APPROVED")
        if not eligible:
            return {"status": "UNKNOWN", "text": None, "utterance_ids": [],
                    "fact_ids": [], "reason": "NO_MATCHING_UTTERANCE"}
        if len(eligible) != 1:
            return _review("SELECTOR_REQUIRED")
        selected = [eligible[0]]
    else:
        ids, reason = _validate_plan(plan, eligible)
        if reason:
            return _review(reason)
        by_id = {item["utterance_id"]: item for item in eligible}
        selected = [by_id[item] for item in ids]

    revoked_ids = set(revoked_ids or ())
    rendered, used_facts = [], set()
    for utterance in selected:
        text, reason = _validate_utterance(release_snapshot, utterance, revoked_ids, fixture=fixture)
        if reason:
            return _review(reason)
        rendered.append(text)
        used_facts.update(utterance.get("required_fact_ids", []))
    reply = "\n\n".join(rendered)
    limit = min(600, max_chars) if isinstance(max_chars, int) and max_chars > 0 else 600
    if len(reply) > limit or not _within_reply_limits(reply):
        return _review("REPLY_LIMIT_EXCEEDED")
    return {"status": "OK", "text": reply,
            "utterance_ids": [item["utterance_id"] for item in selected],
            "fact_ids": sorted(used_facts), "reason": None,
            "fictional_fixture": fixture}
