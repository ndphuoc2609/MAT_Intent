"""Strict structured-output contract for one-call persona turns."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

from .persona_query import KEY_REGISTRY
from .semantics import TOPICS, semantic_model_json_schema
from .persona_fact_policy import NAMESPACES, PROPOSAL_IDS

PEER_INTENTS = ("social", "owns_vehicle", "buying_interest", "switching_vehicle",
                "declines", "transaction", "correction", "other_person", "ai_identity", "unknown")
ADDRESS_TERMS = ("bác", "anh", "chị", "chú", "cô", "bạn")
LEAD_FIELDS = ("current_vehicle", "interested_vehicle", "purchase_timeline", "finance_intent",
               "budget", "usage_need", "ownership_status", "purchase_intent")
QUESTION_FIELDS = LEAD_FIELDS
QUESTION_KINDS = ("conversation", "clarification", "lead")
PERSONA_PREDICATES = frozenset(key for keys in KEY_REGISTRY.values() for key in keys)


NEED_TOPICS = tuple(sorted((*KEY_REGISTRY, *NAMESPACES)))
NEED_KEYS = tuple(sorted({key for keys in KEY_REGISTRY.values() for key in keys}))


def persona_analysis_model_schema():
    """Strict phase-one contract: interpret the comment and request evidence only."""
    base = persona_turn_model_schema()
    return {"type": "object", "properties": {
        "schema_version": {"enum": ["persona_analysis_v1"]},
        "semantic": base["properties"]["semantic"],
        "needs": {"type": "object", "properties": {
            "topics": {"type": "array", "items": {"enum": list(NEED_TOPICS)}},
            "keys": {"type": "array", "items": {"enum": list(NEED_KEYS)}},
        }, "required": ["topics", "keys"], "additionalProperties": False},
        "diagnostics": base["properties"]["diagnostics"],
        "peer_interpretation": base["properties"]["peer_interpretation"],
    }, "required": ["schema_version", "semantic", "needs", "diagnostics", "peer_interpretation"],
        "additionalProperties": False}


def persona_turn_model_schema(persona_predicates=None, *, proposals=False):
    """Provider strict schema. Semantic stays the existing v2 contract."""
    text = lambda maximum: {"type": "string", "minLength": 1, "maxLength": maximum}
    identifiers = {"type": "array", "maxItems": 40,
                   "items": {"type": "string", "minLength": 1, "maxLength": 200}}
    predicates = set(persona_predicates) if persona_predicates is not None else set(PERSONA_PREDICATES)
    if proposals:
        predicates.update(f"gen.{namespace}" for namespace in NAMESPACES)
    claim = {"type": "object", "properties": {
        "text": text(600), "subject": {"enum": ["persona", "customer"]},
        "predicate": {"enum": sorted(predicates | set(LEAD_FIELDS))},
        "fact_ids": identifiers, "memory_ids": identifiers, "message_ids": identifiers,
    }, "required": ["text", "subject", "predicate", "fact_ids", "memory_ids", "message_ids"],
        "additionalProperties": False}
    reply_properties = {
        "reply_text": text(600), "fact_ids": identifiers, "memory_ids": identifiers,
        "claims": {"type": "array", "maxItems": 20, "items": claim},
        "asks_question": {"type": "boolean"},
        "question": {"anyOf": [{"type": "object", "properties": {
            "kind": {"enum": list(QUESTION_KINDS)},
            "field": {"enum": [*QUESTION_FIELDS, None]}, "text": text(300),
        }, "required": ["kind", "field", "text"], "additionalProperties": False},
            {"type": "null"}]},
    }
    if proposals:
        reply_properties["fact_proposals"] = {"type": "array", "maxItems": 2, "items": {
            "type": "object", "properties": {
                "proposal_id": {"enum": list(PROPOSAL_IDS)},
                "namespace": {"enum": list(NAMESPACES)},
                "slug": {"type": "string", "minLength": 2, "maxLength": 40,
                         "pattern": "^[a-z][a-z0-9_]{1,39}$"},
                "value": text(160),
                "precision": {"enum": ["exact", "approximate", "month", "year", "none"]},
            }, "required": ["proposal_id", "namespace", "slug", "value", "precision"],
            "additionalProperties": False}}
    reply = {"type": "object", "properties": reply_properties,
        "required": ["reply_text", "fact_ids", "memory_ids", "claims", "asks_question", "question"] +
                   (["fact_proposals"] if proposals else []),
        "additionalProperties": False}
    diagnostics = {"type": "object", "properties": {
        "intent_confidence": {"type": "array", "maxItems": len(TOPICS), "items": {
            "type": "object", "properties": {"topic": {"enum": TOPICS},
                "confidence": {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1},
                                           {"type": "null"}]},
                "evidence_excerpt": text(500)},
            "required": ["topic", "confidence", "evidence_excerpt"],
            "additionalProperties": False}},
    }, "required": ["intent_confidence"], "additionalProperties": False}
    peer = {"type": "object", "properties": {
        "speaker_intent": {"enum": list(PEER_INTENTS)},
        "address_term": {"enum": [*ADDRESS_TERMS, None]},
        "persona_topic": {"enum": [*sorted(KEY_REGISTRY), None]},
        "asks_persona_question": {"type": "boolean"},
        "claims": {"type": "array", "maxItems": len(LEAD_FIELDS), "items": {
            "type": "object", "properties": {"predicate": {"enum": list(LEAD_FIELDS)},
                "value": {"type": "string", "minLength": 1, "maxLength": 180},
                "evidence_excerpt": text(500)},
            "required": ["predicate", "value", "evidence_excerpt"],
            "additionalProperties": False}},
        "evidence_excerpt": text(500),
    }, "required": ["speaker_intent", "address_term", "persona_topic", "asks_persona_question",
                  "claims", "evidence_excerpt"], "additionalProperties": False}
    semantic_schema = semantic_model_json_schema("2")
    return {"type": "object", "properties": {
        "schema_version": {"enum": ["persona_turn_v1"]},
        "semantic": semantic_schema,
        "reply": {"anyOf": [reply, {"type": "null"}]},
        "diagnostics": {"anyOf": [diagnostics, {"type": "null"}]},
        "peer_interpretation": {"anyOf": [peer, {"type": "null"}]},
    }, "required": ["schema_version", "semantic", "reply", "diagnostics", "peer_interpretation"],
        "additionalProperties": False}


def persona_post_model_schema(persona_predicates=None):
    """Strict second phase for a post authored by the bound persona."""
    text = lambda maximum: {"type": "string", "minLength": 1, "maxLength": maximum}
    identifiers = {"type": "array", "maxItems": 40,
                   "items": {"type": "string", "minLength": 1, "maxLength": 200}}
    predicates = set(persona_predicates) if persona_predicates is not None else set(PERSONA_PREDICATES)
    predicates.update(f"gen.{namespace}" for namespace in NAMESPACES)
    claim = {"type": "object", "properties": {
        "text": text(600), "subject": {"enum": ["persona"]},
        "predicate": {"enum": sorted(predicates)},
        "fact_ids": identifiers, "memory_ids": identifiers, "message_ids": identifiers,
    }, "required": ["text", "subject", "predicate", "fact_ids", "memory_ids", "message_ids"],
        "additionalProperties": False}
    proposal = {"type": "object", "properties": {
        "proposal_id": {"enum": list(PROPOSAL_IDS)},
        "namespace": {"enum": list(NAMESPACES)},
        "slug": {"type": "string", "minLength": 2, "maxLength": 40,
                 "pattern": "^[a-z][a-z0-9_]{1,39}$"},
        "value": text(160),
        "precision": {"enum": ["exact", "approximate", "month", "year", "none"]},
    }, "required": ["proposal_id", "namespace", "slug", "value", "precision"],
        "additionalProperties": False}
    return {"type": "object", "properties": {
        "schema_version": {"enum": ["persona_post_v1"]},
        "post_text": text(3000), "fact_ids": identifiers,
        "claims": {"type": "array", "minItems": 1, "maxItems": 40, "items": claim},
        "fact_proposals": {"type": "array", "maxItems": 2, "items": proposal},
    }, "required": ["schema_version", "post_text", "fact_ids", "claims", "fact_proposals"],
        "additionalProperties": False}


def _unique_strings(value):
    return (isinstance(value, list) and len(value) <= 40 and
            all(isinstance(item, str) and item.strip() and len(item) <= 200 for item in value) and
            len(value) == len(set(value)))


def _valid_reply(value, *, proposals=False, persona_predicates=None):
    expected = {"reply_text", "fact_ids", "memory_ids", "claims", "asks_question", "question"}
    if proposals:
        expected.add("fact_proposals")
    if not isinstance(value, dict) or set(value) != expected:
        return False
    text = value["reply_text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return False
    if not _unique_strings(value["fact_ids"]) or not _unique_strings(value["memory_ids"]):
        return False
    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > 20:
        return False
    proposal_rows = value.get("fact_proposals", [])
    proposal_ids = set()
    if proposals:
        if not isinstance(proposal_rows, list) or len(proposal_rows) > 2:
            return False
        if any(not isinstance(p, dict) or set(p) != {
                "proposal_id", "namespace", "slug", "value", "precision"} for p in proposal_rows):
            return False
        # The policy validates proposal content later, with live keys and customer text.
        proposal_ids = {p.get("proposal_id") for p in proposal_rows}
        if (len(proposal_ids) != len(proposal_rows) or
                not proposal_ids <= set(PROPOSAL_IDS) or
                any(p.get("namespace") not in NAMESPACES or not isinstance(p.get("slug"), str)
                    or not isinstance(p.get("value"), str) or not p.get("value", "").strip()
                    or p.get("precision") not in ("exact", "approximate", "month", "year", "none")
                    for p in proposal_rows)):
            return False
    fact_ids, memory_ids = set(), set()
    allowed_persona_predicates = set(persona_predicates or PERSONA_PREDICATES) | {
        f"gen.{namespace}" for namespace in NAMESPACES
    }
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
                "text", "subject", "predicate", "fact_ids", "memory_ids", "message_ids"}:
            return False
        claim_text = claim["text"]
        predicate = claim["predicate"]
        allowed = allowed_persona_predicates if claim["subject"] == "persona" else set(LEAD_FIELDS)
        if (claim["subject"] not in ("persona", "customer") or
                not isinstance(predicate, str) or not isinstance(claim_text, str) or
                not claim_text.strip() or len(claim_text) > 600 or
                claim_text not in text or predicate not in allowed or
                not _unique_strings(claim["fact_ids"]) or
                not _unique_strings(claim["memory_ids"]) or
                not _unique_strings(claim["message_ids"]) or
                (not proposals and any(fid in PROPOSAL_IDS for fid in claim["fact_ids"])) or
                (proposals and any(fid in PROPOSAL_IDS and fid not in proposal_ids for fid in claim["fact_ids"]))):
            return False
        fact_ids.update(claim["fact_ids"])
        memory_ids.update(claim["memory_ids"])
    if (set(value["fact_ids"]) != fact_ids or set(value["memory_ids"]) != memory_ids or
            (not proposals and any(fid in PROPOSAL_IDS for fid in value["fact_ids"])) or
            (proposals and any(fid in PROPOSAL_IDS and fid not in proposal_ids for fid in value["fact_ids"]))):
        return False
    asks = value["asks_question"]
    question = value["question"]
    if type(asks) is not bool:
        return False
    if not asks:
        return question is None
    if (not isinstance(question, dict) or set(question) != {"kind", "field", "text"} or
            question["kind"] not in QUESTION_KINDS or
            question["field"] not in (*QUESTION_FIELDS, None) or
            not isinstance(question["text"], str) or not question["text"].strip() or
            len(question["text"]) > 300 or question["text"] not in text):
        return False
    if question["kind"] == "conversation" and question["field"] is not None:
        return False
    if question["kind"] == "lead" and question["field"] is None:
        return False
    return True


def _valid_diagnostics(value, semantic, evidence_texts):
    if not isinstance(value, dict) or set(value) != {"intent_confidence"}:
        return False
    entries = value["intent_confidence"]
    if not isinstance(entries, list) or len(entries) > len(TOPICS):
        return False
    requests = semantic.get("requests") if isinstance(semantic.get("requests"), list) else []
    selected = {row.get("topic") for row in requests if isinstance(row, dict) and
                isinstance(row.get("topic"), str)}
    labels = semantic.get("intent_labels")
    if isinstance(labels, list):
        selected.update(labels)
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"topic", "confidence", "evidence_excerpt"}:
            return False
        topic, confidence, excerpt = item["topic"], item["confidence"], item["evidence_excerpt"]
        confidence_valid = confidence is None
        if type(confidence) in (int, float) and 0 <= confidence <= 1:
            try:
                confidence_valid = math.isfinite(confidence)
            except OverflowError:
                confidence_valid = False
        if (not isinstance(topic, str) or topic not in TOPICS or topic not in selected or topic in seen or
                not confidence_valid or
                not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 500):
            return False
        if not any(excerpt in source for source in evidence_texts if isinstance(source, str)):
            return False
        seen.add(topic)
    return True


def _valid_peer(value, current_text):
    if not isinstance(value, dict) or set(value) != {
            "speaker_intent", "address_term", "persona_topic", "asks_persona_question",
            "claims", "evidence_excerpt"}:
        return False
    if (value["speaker_intent"] not in PEER_INTENTS or
            value["address_term"] not in (*ADDRESS_TERMS, None) or
            value["persona_topic"] not in (*KEY_REGISTRY, None) or
            type(value["asks_persona_question"]) is not bool or
            not isinstance(value["evidence_excerpt"], str) or not value["evidence_excerpt"].strip() or
            len(value["evidence_excerpt"]) > 500 or value["evidence_excerpt"] not in current_text):
        return False
    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > len(LEAD_FIELDS):
        return False
    seen = set()
    for item in claims:
        if not isinstance(item, dict) or set(item) != {"predicate", "value", "evidence_excerpt"}:
            return False
        excerpt = item["evidence_excerpt"]
        predicate = item["predicate"]
        if (not isinstance(predicate, str) or predicate not in LEAD_FIELDS or predicate in seen or
                not isinstance(item["value"], str) or not item["value"].strip() or len(item["value"]) > 180 or
                not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 500 or
                excerpt not in current_text):
            return False
        seen.add(predicate)
    return True


def parse_persona_turn(envelope: Any, *, current_text="", evidence_texts=None,
                       proposals=False, persona_predicates=None):
    """Split the envelope. Optional sections fail closed without losing semantics."""
    if (not isinstance(envelope, dict) or envelope.get("schema_version") != "persona_turn_v1" or
            not isinstance(envelope.get("semantic"), dict) or
            envelope["semantic"].get("schema_version") != "2"):
        raise ValueError("invalid persona_turn_v1 envelope")
    semantic = copy.deepcopy(envelope["semantic"])
    if not isinstance(current_text, str):
        current_text = ""
    evidence = ([current_text] if evidence_texts is None else
                (list(evidence_texts) if isinstance(evidence_texts, (list, tuple)) else []) + [current_text])
    reply = envelope.get("reply")
    diagnostics = envelope.get("diagnostics")
    peer = envelope.get("peer_interpretation")
    result = {
        "semantic": semantic,
        "reply_candidate": copy.deepcopy(reply) if _valid_reply(
            reply, proposals=proposals, persona_predicates=persona_predicates) else None,
        "diagnostics": copy.deepcopy(diagnostics) if _valid_diagnostics(diagnostics, semantic, evidence) else None,
        "peer_signals": copy.deepcopy(peer) if _valid_peer(peer, current_text) else None,
    }
    if proposals:
        result["fact_proposals"] = (copy.deepcopy(reply.get("fact_proposals", []))
                                    if result["reply_candidate"] is not None else [])
    return result


def parse_persona_analysis(envelope: Any, *, current_text="", evidence_texts=None):
    """Validate phase one as a unit; invalid needs must trigger caller fallback."""
    if (not isinstance(envelope, dict) or envelope.get("schema_version") != "persona_analysis_v1" or
            not isinstance(envelope.get("semantic"), dict) or
            envelope["semantic"].get("schema_version") != "2"):
        raise ValueError("invalid persona_analysis_v1 envelope")
    needs = envelope.get("needs")
    if not isinstance(needs, dict) or set(needs) != {"topics", "keys"}:
        raise ValueError("invalid persona needs")
    topics, keys = needs["topics"], needs["keys"]
    if (not isinstance(topics, list) or len(topics) != len(set(topics)) or
            any(not isinstance(item, str) or item not in NEED_TOPICS for item in topics) or
            not isinstance(keys, list) or len(keys) != len(set(keys)) or
            any(not isinstance(item, str) or item not in NEED_KEYS for item in keys)):
        raise ValueError("invalid persona needs")
    semantic = copy.deepcopy(envelope["semantic"])
    if not isinstance(current_text, str):
        current_text = ""
    evidence = ([current_text] if evidence_texts is None else
                (list(evidence_texts) if isinstance(evidence_texts, (list, tuple)) else []) + [current_text])
    return {"semantic": semantic, "needs": copy.deepcopy(needs),
        "diagnostics": copy.deepcopy(envelope.get("diagnostics")) if _valid_diagnostics(
            envelope.get("diagnostics"), semantic, evidence) else None,
        "peer_signals": copy.deepcopy(envelope.get("peer_interpretation")) if _valid_peer(
            envelope.get("peer_interpretation"), current_text) else None}


def parse_persona_post(envelope: Any, *, persona_predicates=None, supplied_fact_ids=None,
                       return_reason=False):
    """Return a post only when its claims and new-fact references are internally complete."""
    invalid = lambda reason: (None, reason) if return_reason else None
    if (not isinstance(envelope, dict) or envelope.get("schema_version") != "persona_post_v1" or
            set(envelope) != {"schema_version", "post_text", "fact_ids", "claims", "fact_proposals"}):
        return invalid("POST_ENVELOPE_SHAPE")
    text = envelope["post_text"]
    facts, claims, proposals = envelope["fact_ids"], envelope["claims"], envelope["fact_proposals"]
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        return invalid("POST_TEXT_INVALID")
    if not _unique_strings(facts) or not isinstance(claims, list) or not claims or len(claims) > 40:
        return invalid("POST_CLAIMS_INVALID")
    if not isinstance(proposals, list) or len(proposals) > 2:
        return invalid("POST_PROPOSALS_INVALID")
    proposal_ids, proposal_by_id = set(), {}
    slug_pattern = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
    for proposal in proposals:
        if (not isinstance(proposal, dict) or set(proposal) != {
                "proposal_id", "namespace", "slug", "value", "precision"} or
                not isinstance(proposal["proposal_id"], str) or
                proposal["proposal_id"] not in PROPOSAL_IDS or
                not isinstance(proposal["namespace"], str) or
                proposal["namespace"] not in NAMESPACES or
                not isinstance(proposal["slug"], str) or not slug_pattern.fullmatch(proposal["slug"]) or
                not isinstance(proposal["value"], str) or not proposal["value"].strip() or
                len(proposal["value"]) > 160 or
                proposal["precision"] not in ("exact", "approximate", "month", "year", "none")):
            return invalid("POST_PROPOSAL_SHAPE")
        proposal_ids.add(proposal["proposal_id"])
        proposal_by_id[proposal["proposal_id"]] = proposal
    if len(proposal_ids) != len(proposals):
        return invalid("POST_PROPOSAL_DUPLICATE")
    allowed_predicates = set(persona_predicates or PERSONA_PREDICATES) | {
        f"gen.{namespace}" for namespace in NAMESPACES}
    referenced_facts = set()
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != {
                "text", "subject", "predicate", "fact_ids", "memory_ids", "message_ids"} or
                claim["subject"] != "persona" or not isinstance(claim["predicate"], str) or
                claim["predicate"] not in allowed_predicates or
                not isinstance(claim["text"], str) or not claim["text"].strip() or
                len(claim["text"]) > 600 or claim["text"] not in text or
                not _unique_strings(claim["fact_ids"]) or not _unique_strings(claim["memory_ids"]) or
                not _unique_strings(claim["message_ids"])):
            return invalid("POST_CLAIM_SHAPE")
        if any(claim["predicate"] != f"gen.{proposal_by_id[proposal_id]['namespace']}"
               for proposal_id in set(claim["fact_ids"]) & proposal_ids):
            return invalid("POST_PROPOSAL_PREDICATE_MISMATCH")
        referenced_facts.update(claim["fact_ids"])
    if set(facts) != referenced_facts:
        return invalid("POST_CLAIM_FACT_IDS_MISMATCH")
    if not proposal_ids <= referenced_facts:
        return invalid("POST_PROPOSAL_UNREFERENCED")
    supplied = set(supplied_fact_ids or ())
    if not (set(facts) - proposal_ids) <= supplied:
        return invalid("POST_FACT_NOT_SUPPLIED")
    if any(fact_id in PROPOSAL_IDS and fact_id not in proposal_ids for fact_id in facts):
        return invalid("POST_UNKNOWN_PROPOSAL_ID")
    result = {"post_text": text, "fact_ids": copy.deepcopy(facts),
              "claims": copy.deepcopy(claims), "fact_proposals": copy.deepcopy(proposals)}
    return (result, None) if return_reason else result
