"""Semantic schema v1 + validator nghiêm ngặt. Model chỉ ĐỀ XUẤT operations."""
from __future__ import annotations
import json, math, re
from .normalize import (COLORS, LOCATIONS, PRICE_KINDS, SERVICE_TOPICS, VARIANTS, VEHICLES,
                        canon_slot, norm)

TOPICS = ["price", "promo", "loan", "stock", "spec", "dealer", "testdrive",
          "buy", "service", "complaint", "registration", "social"]
OPS = ["START_REQUEST", "UPDATE_SLOT", "ANSWER_PENDING", "CONFIRM_PENDING",
       "REJECT_PENDING", "CANCEL_REQUEST", "SET_CONTACT_PREFERENCE",
       "REQUEST_HUMAN", "REPORT_COMPLAINT", "SOCIAL_ACK", "UNRESOLVED"]
SENTIMENTS = ["POS", "NEG", "NEU"]
SARCASM = ["present", "absent", "uncertain"]
SCOPES = ["in_scope", "out_of_scope", "unknown"]
POLARITY = ["positive", "negative", "conditional", "unknown"]
MAX_NUMERIC_SLOT = 1_000_000_000_000
# Schema theo từng operation (R09): required/allowed fields + kiểu dữ liệu.
# Field lạ, kiểu sai, slot kind lạ đều bị reject (không âm thầm drop).
SLOT_KINDS = {"vehicle", "variant", "color", "location", "price_kind", "datetime",
              "down_payment", "down_payment_unit", "dealer", "service_topic",
              "compare_vehicle"}
CONTACT_SCOPES = ("call", "public_reply", "inbox", "all",
                  "allow_call", "allow_public_reply", "allow_inbox", "allow_all")
_BASE_FIELDS = {"evidence_excerpt", "source_message_id"}
OP_SPEC = {
    "START_REQUEST": {"required": [], "allowed": _BASE_FIELDS | {"request_ref", "request", "topic", "slot"}},
    "UPDATE_SLOT": {"required": ["request_ref", "slot"], "allowed": _BASE_FIELDS | {"request_ref", "slot"}},
    "ANSWER_PENDING": {"required": ["request_ref", "slot", "pending_id"], "allowed": _BASE_FIELDS | {"request_ref", "slot", "pending_id"}},
    "CONFIRM_PENDING": {"required": ["request_ref", "pending_id"], "allowed": _BASE_FIELDS | {"request_ref", "pending_id"}},
    "REJECT_PENDING": {"required": ["request_ref", "pending_id"], "allowed": _BASE_FIELDS | {"request_ref", "pending_id"}},
    "CANCEL_REQUEST": {"required": ["request_ref"], "allowed": _BASE_FIELDS | {"request_ref"}},
    "SET_CONTACT_PREFERENCE": {"required": ["scope"], "allowed": _BASE_FIELDS | {"scope", "request_ref"}},
    "REQUEST_HUMAN": {"required": [], "allowed": _BASE_FIELDS | {"request_ref", "reason", "human_source"}},
    "REPORT_COMPLAINT": {"required": [], "allowed": _BASE_FIELDS | {"request_ref", "reason"}},
    "SOCIAL_ACK": {"required": [], "allowed": _BASE_FIELDS | {"request_ref"}},
    "UNRESOLVED": {"required": ["reason"], "allowed": _BASE_FIELDS | {"reason", "request_ref"}},
}

def _reject_nan(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        raise ValueError("NaN/Infinity rejected")
    if isinstance(obj, dict):
        for v in obj.values(): _reject_nan(v)
    if isinstance(obj, list):
        for v in obj: _reject_nan(v)

def parse_json_strict(text: str):
    def bad(x): raise ValueError(f"invalid constant {x}")
    return json.loads(text, parse_constant=bad)


def attach_context_provenance(sem, event, context, postcard):
    """Migrate v1 inherited slots only when one same-actor source is proven."""
    import copy
    if not isinstance(sem, dict) or sem.get("schema_version") != "1":
        return sem
    out = copy.deepcopy(sem)
    vehicles = postcard.get("vehicles") or []
    post_text = postcard.get("text") or ""
    vehicle = canon_slot("vehicle", vehicles[0]) if len(vehicles) == 1 else None
    if not isinstance(post_text, str):
        post_text = ""
    recent = [m for m in context.get("recent", []) if isinstance(m, dict)]
    sources = [m for m in context.get("messages", []) if isinstance(m, dict)] + recent
    for op in out.get("operations") or []:
        slot = op.get("slot") if isinstance(op, dict) else None
        if not isinstance(slot, dict) or slot.get("explicit", True) is not False:
            continue
        source = None
        excerpt = slot.get("evidence_excerpt") or op.get("evidence_excerpt")
        if (not slot.get("source_message_id") and slot.get("kind") == "vehicle" and
                vehicle is not None and canon_slot("vehicle", slot.get("value")) == vehicle and
                isinstance(excerpt, str) and
                _positive_slot_evidence("vehicle", slot.get("value"), slot, [excerpt],
                                        [post_text or str(vehicles[0])])):
            source = {"message_id": event.get("post_id"),
                      "actor_id": postcard.get("page_id") or event.get("page_id"),
                      "text": post_text or str(vehicles[0]), "role": "post",
                      "vehicles": list(vehicles)}
        elif not slot.get("source_message_id") and isinstance(excerpt, str):
            candidates = [m for m in sources
                          if m.get("message_id") and m.get("role") not in ("post", "current") and
                          m.get("actor_id") == event.get("actor_id") and
                          norm(excerpt) in norm(m.get("text") or "") and
                          _positive_slot_evidence(slot.get("kind"), slot.get("value"), slot,
                                                  [excerpt], [m.get("text") or ""])]
            if len(candidates) == 1:
                source = candidates[0]
        if source:
            slot["source_message_id"] = source["message_id"]
            slot["source_actor_id"] = source.get("actor_id")
            slot["source_state_token"] = context.get("state_token")
        elif slot.get("source_message_id"):
            source = next((m for m in sources if m.get("message_id") == slot["source_message_id"]), None)
            # Legacy v1 canonical-post provenance carries only its ID; enrich
            # it from the current immutable postcard before validation.
            if source is None and slot["source_message_id"] == event.get("post_id") and post_text:
                source = {"message_id": event.get("post_id"),
                          "actor_id": postcard.get("page_id") or event.get("page_id"),
                          "text": post_text, "role": "post", "vehicles": list(vehicles)}
            if (source is None and slot["source_message_id"] == event.get("post_id") and
                    vehicle is not None and canon_slot("vehicle", slot.get("value")) == vehicle):
                source = {"message_id": event.get("post_id"),
                          "actor_id": postcard.get("page_id") or event.get("page_id"),
                          "text": str(vehicles[0]), "role": "post", "vehicles": list(vehicles)}
            if source:
                slot.setdefault("source_actor_id", source.get("actor_id"))
                slot.setdefault("source_state_token", context.get("state_token"))
    return out


_TOP_FIELDS_V1 = {"schema_version", "sentiment", "sarcasm", "scope", "requests", "operations", "uncertainties"}
_TOP_FIELDS_V2 = _TOP_FIELDS_V1 | {"intent_labels", "category", "tags"}


def semantic_json_schema(version="1"):
    """Versioned strict JSON Schema shared by Luna and offline tooling."""
    string = lambda limit: {"type": "string", "maxLength": limit}
    scalar = {"oneOf": [string(256), {"type": "number", "minimum": -MAX_NUMERIC_SLOT,
                                      "maximum": MAX_NUMERIC_SLOT}]}
    down_payment = {"type": "object", "properties": {
        "amount": {"type": "number", "minimum": 0, "maximum": MAX_NUMERIC_SLOT},
        "unit": {"type": ["string", "null"], "maxLength": 32}},
        "required": ["amount"], "additionalProperties": False}
    slot = {"type": "object", "properties": {
        "kind": {"enum": sorted(SLOT_KINDS)},
        "value": {"oneOf": [string(256), {"type": "number"}, down_payment]},
        "raw": string(256), "evidence_excerpt": {**string(500), "minLength": 1},
        "explicit": {"type": "boolean"}, "confirmed": {"type": "boolean"},
        "extra": {"type": "object", "maxProperties": 10, "additionalProperties": scalar},
        "source_message_id": string(500), "source_request_id": string(500),
        "source_actor_id": string(500), "source_state_token": string(128)},
        "required": ["kind", "value"], "additionalProperties": False,
        "allOf": [{"if": {"properties": {"kind": {"const": "down_payment"}}, "required": ["kind"]},
                   "then": {"properties": {"value": down_payment}},
                   "else": {"properties": {"value": scalar}}}]}
    prop = {
        "request_ref": string(500), "request": {"type": "object", "properties": {
            "topic": {"enum": TOPICS}, "desired_outcome": string(120)},
            "required": ["topic"], "additionalProperties": False},
        "topic": {"enum": TOPICS}, "slot": slot,
        "evidence_excerpt": {**string(500), "minLength": 1}, "source_message_id": string(500),
        "pending_id": string(500), "scope": {"enum": list(CONTACT_SCOPES)},
        "reason": string(500), "human_source": {"enum": ["customer_explicit", "model_proposed", "policy"]}}
    operation_variants = []
    for op, spec in OP_SPEC.items():
        allowed = {"op", "evidence_excerpt"} | spec["allowed"]
        operation_variants.append({"type": "object", "properties": {
            key: ({"const": op} if key == "op" else prop[key]) for key in sorted(allowed)},
            "required": ["op", "evidence_excerpt", *spec["required"]],
            "additionalProperties": False})
    properties = {
        "schema_version": {"const": str(version)}, "sentiment": {"enum": SENTIMENTS},
        "sarcasm": {"enum": SARCASM}, "scope": {"enum": SCOPES},
        "requests": {"type": "array", "maxItems": 12, "items": {"type": "object",
            "properties": {"topic": {"enum": TOPICS}, "desired_outcome": {**string(120), "minLength": 1},
                           "polarity": {"enum": POLARITY}},
            "required": ["topic", "desired_outcome", "polarity"], "additionalProperties": False}},
        "operations": {"type": "array", "minItems": 1, "maxItems": 24,
                       "items": {"oneOf": operation_variants}},
        "uncertainties": {"type": "array", "maxItems": 12, "items": {"type": "object",
            "properties": {"alternatives": {"type": "array", "minItems": 2, "maxItems": 8,
                                             "items": {**string(256), "minLength": 1}},
                           "different_action": {"type": "boolean"}},
            "required": ["alternatives", "different_action"], "additionalProperties": False}}}
    if str(version) == "2":
        properties.update({"intent_labels": {"type": "array", "maxItems": 12,
                                              "items": {"enum": TOPICS}},
                           "category": {"type": ["string", "null"], "maxLength": 80},
                           "tags": {"type": "array", "maxItems": 16, "items": string(64)}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"MAT Intent semantic v{version}", "type": "object",
            "properties": properties, "required": list(properties), "additionalProperties": False}


def semantic_model_json_schema(version="1"):
    """Provider strict subset. App validation enforces the omitted constraints."""
    def walk(node):
        if isinstance(node, list):
            return [walk(value) for value in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in {"$schema", "title", "description", "minLength", "maxLength",
                       "minItems", "maxItems", "maxProperties", "minimum", "maximum",
                       "allOf", "if", "then", "else"}:
                continue
            if key == "oneOf":
                out["anyOf"] = [walk(item) for item in value]
            elif key == "const":
                out["enum"] = [value]
            elif key == "properties":
                out[key] = {name: walk(schema) for name, schema in value.items()}
            elif key == "additionalProperties" and value is not False:
                # Slot extras are a closed vocabulary in the strict provider schema.
                out["properties"] = {kind: {"anyOf": [walk(value), {"type": "null"}]}
                                      for kind in sorted(SLOT_KINDS)}
                out["required"] = sorted(SLOT_KINDS)
                out[key] = False
            else:
                out[key] = walk(value)
        if out.get("type") == "object" and "properties" in out:
            props = out["properties"]
            required = set(out.get("required", []))
            for name, schema in props.items():
                if name not in required:
                    props[name] = {"anyOf": [schema, {"type": "null"}]}
            out["required"] = list(props)
            out["additionalProperties"] = False
        if isinstance(out.get("type"), list):
            types = out.pop("type")
            base = out
            out = {"anyOf": [{"type": kind, **base} for kind in types]}
        return out

    return walk(semantic_json_schema(version))


def normalize_model_semantic(semantic):
    """Drop null placeholders required by strict structured-output schemas."""
    import copy
    if not isinstance(semantic, dict):
        return semantic
    out = copy.deepcopy(semantic)
    for request in out.get("requests", []):
        if isinstance(request, dict) and request.get("desired_outcome") is None:
            request.pop("desired_outcome")
    for operation in out.get("operations", []):
        if not isinstance(operation, dict):
            continue
        for field in OP_SPEC.get(operation.get("op"), {}).get("allowed", ()):
            if field in operation and operation[field] is None:
                operation.pop(field)
        request = operation.get("request")
        if isinstance(request, dict) and request.get("desired_outcome") is None:
            request.pop("desired_outcome")
        slot = operation.get("slot")
        if isinstance(slot, dict):
            for field in ("raw", "evidence_excerpt", "explicit", "confirmed", "extra", "source_message_id",
                          "source_request_id", "source_actor_id", "source_state_token"):
                if slot.get(field) is None:
                    slot.pop(field)
            extra = slot.get("extra")
            if isinstance(extra, dict):
                slot["extra"] = {kind: value for kind, value in extra.items() if value is not None}
            value = slot.get("value")
            if slot.get("kind") == "down_payment" and isinstance(value, dict):
                slot["value"] = {key: item for key, item in value.items()
                                  if key != "unit" or item is not None}
    return out


def _slot_aliases(kind, value):
    if not isinstance(kind, str) or kind not in SLOT_KINDS:
        return set()
    target = canon_slot(kind, value)
    aliases = {"vehicle": VEHICLES, "variant": VARIANTS, "color": COLORS,
               "location": LOCATIONS, "price_kind": PRICE_KINDS,
               "service_topic": SERVICE_TOPICS, "compare_vehicle": VEHICLES}.get(kind, {})
    values = {str(value)}
    values.update(alias for alias, canonical in aliases.items()
                  if canon_slot(kind, canonical) == target)
    return {norm(v) for v in values if isinstance(v, str) and v.strip()}


def _present(text, alias):
    return bool(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", norm(text)))


def _positive_mention(text, alias):
    text = norm(text)
    for mention in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text):
        prefix = text[max(0, mention.start() - 36):mention.start()]
        if not re.search(r"(?:\bkhong(?:\s+phai|\s+lay|\s+muon)?|\bko|\bkhong\s+con)\s+(?:\w+\s+){0,2}$", prefix):
            return True
    return False


def _positive_slot_evidence(kind, value, slot, evidence_texts, source_texts=()):
    """Check value↔excerpt entailment with only known lexical aliases."""
    aliases = _slot_aliases(kind, value)
    evidence_texts = [text for text in evidence_texts if isinstance(text, str)]
    source_texts = [text for text in source_texts if isinstance(text, str)]
    if kind == "datetime" and isinstance(value, str) and isinstance(slot.get("raw"), str):
        raw = norm(slot["raw"])
        try:
            from datetime import datetime
            from .normalize import parse_time_expression
            target = datetime.fromisoformat(value)
            parsed_kind, parsed = parse_time_expression(slot["raw"], target)
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            if parsed_kind == "time_only":
                hour, minute = parsed if isinstance(parsed, tuple) else (parsed, 0)
                return (target.hour == hour and target.minute == minute and
                        any(raw and raw in norm(excerpt) for excerpt in evidence_texts))
    if kind == "down_payment" and isinstance(value, dict):
        amount, unit = value.get("amount"), value.get("unit")
        if (type(amount) not in (int, float) or abs(amount) > MAX_NUMERIC_SLOT or
                isinstance(amount, float) and not math.isfinite(amount)):
            return False
        amount_aliases = {norm(str(amount))}
        if float(amount).is_integer():
            amount_aliases.add(str(int(amount)))
        unit_alias = norm(str(unit)) if isinstance(unit, str) and unit.strip() else None
        for excerpt in evidence_texts:
            containing = [t for t in source_texts if norm(excerpt) in norm(t)] or [excerpt]
            if any(any(_positive_mention(t, alias) for alias in amount_aliases) and
                   (unit_alias is None or _positive_mention(t, unit_alias)) for t in containing):
                return True
        return False
    if kind == "down_payment_unit":
        if not isinstance(value, str) or not value.strip():
            return False
        return any(_positive_mention(whole, norm(value))
                   for excerpt in evidence_texts
                   for whole in ([t for t in source_texts if norm(excerpt) in norm(t)] or [excerpt]))
    if kind == "down_payment":
        return False
    for source in evidence_texts:
        nsource = norm(source)
        if any(_present(nsource, alias) for alias in aliases):
            for alias in aliases:
                for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", nsource):
                    # An excerpt may omit a negation immediately before it.
                    containing = [t for t in source_texts if norm(source) in norm(t)] or [source]
                    positive = False
                    for whole in containing:
                        if _positive_mention(whole, alias):
                            positive = True
                            break
                    if positive:
                        return True
    return False

def validate_semantic(sem, event_text: str, context_texts: list[str]) -> list[str]:
    """Trả về danh sách lỗi; rỗng = hợp lệ. Không âm thầm drop field lạ."""
    errs = []
    if not isinstance(sem, dict): return ["semantic must be object"]
    try: _reject_nan(sem)
    except ValueError as e: return [str(e)]
    version = sem.get("schema_version")
    if version not in ("1", "2"):
        errs.append("schema_version must be '1' or '2'")
    allowed_top = _TOP_FIELDS_V2 if version == "2" else _TOP_FIELDS_V1
    for field in sem:
        if field not in allowed_top:
            errs.append(f"unknown top-level field {field!r}")
    if version == "2":
        for field in ("intent_labels", "category", "tags"):
            if field not in sem:
                errs.append(f"schema v2 requires {field}")
        labels = sem.get("intent_labels", [])
        if not isinstance(labels, list) or len(labels) > 12 or any(x not in TOPICS for x in labels):
            errs.append("intent_labels must be a list of known topics (max 12)")
        if "category" in sem and sem["category"] is not None and (
                not isinstance(sem["category"], str) or len(sem["category"]) > 80):
            errs.append("category must be a string of at most 80 characters")
        tags = sem.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 16 or any(not isinstance(x, str) or len(x) > 64 for x in tags):
            errs.append("tags must contain at most 16 strings of at most 64 characters")
    if sem.get("sentiment") not in SENTIMENTS: errs.append("bad sentiment")
    if sem.get("sarcasm") not in SARCASM:
        errs.append("sarcasm must be present/absent/uncertain (no bool/float probability)")
    if sem.get("scope") not in SCOPES: errs.append("bad scope")
    uncertainties = sem.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        errs.append("uncertainties must be list")
    else:
        if len(uncertainties) > 12: errs.append("uncertainties exceeds 12 items")
        for i, uncertainty in enumerate(uncertainties):
            if not isinstance(uncertainty, dict):
                errs.append(f"uncertainties[{i}] must be object")
                continue
            if set(uncertainty) != {"alternatives", "different_action"}:
                errs.append(f"uncertainties[{i}] must contain only alternatives and different_action")
            if type(uncertainty.get("different_action")) is not bool:
                errs.append(f"uncertainties[{i}].different_action must be bool")
            alternatives = uncertainty.get("alternatives")
            if (not isinstance(alternatives, list) or not 2 <= len(alternatives) <= 8
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 256
                           for value in alternatives)):
                errs.append(f"uncertainties[{i}].alternatives must contain at least two non-empty strings")
    if not isinstance(sem.get("requests"), list): errs.append("requests must be list")
    else:
        if len(sem["requests"]) > 12: errs.append("requests exceeds 12 items")
        for i, r in enumerate(sem["requests"]):
            if not isinstance(r, dict): errs.append(f"requests[{i}] not object"); continue
            if set(r) != {"topic", "desired_outcome", "polarity"}:
                errs.append(f"requests[{i}] has missing or unknown fields")
            if r.get("topic") not in TOPICS: errs.append(f"requests[{i}] bad topic")
            if r.get("polarity") not in POLARITY: errs.append(f"requests[{i}] bad polarity")
            if not isinstance(r.get("desired_outcome"), str) or not r["desired_outcome"].strip() or len(r["desired_outcome"]) > 120:
                errs.append(f"requests[{i}] missing/invalid desired_outcome")
    if not isinstance(sem.get("operations"), list) or not sem.get("operations"):
        errs.append("operations must be non-empty list")
        return errs
    if len(sem["operations"]) > 24: errs.append("operations exceeds 24 items")
    if sem.get("scope") != "in_scope" and any(
            isinstance(o, dict) and o.get("op") not in ("UNRESOLVED", "SOCIAL_ACK")
            for o in sem["operations"]):
        errs.append("state-changing operations require in_scope")
    for i, op in enumerate(sem["operations"]):
        if not isinstance(op, dict): errs.append(f"operations[{i}] not object"); continue
        t = op.get("op")
        if t not in OPS:
            errs.append(f"operations[{i}] unknown op {t!r} (rejected, not dropped)")
            continue
        spec = OP_SPEC[t]
        for req_field in spec["required"]:
            if not op.get(req_field):
                errs.append(f"operations[{i}] {t} needs {req_field}")
        for k in op:
            if k != "op" and k not in spec["allowed"]:
                errs.append(f"operations[{i}] {t} has disallowed field {k!r}")
        if "topic" in op and op["topic"] is not None and op["topic"] not in TOPICS:
            errs.append(f"operations[{i}] bad topic")
        if "request" in op and op["request"] is not None:
            request = op["request"]
            if (not isinstance(request, dict) or set(request) - {"topic", "desired_outcome"} or
                    request.get("topic") not in TOPICS or
                    "desired_outcome" in request and
                    (not isinstance(request["desired_outcome"], str) or len(request["desired_outcome"]) > 120)):
                errs.append(f"operations[{i}] request must contain a known topic and optional desired_outcome")
        for f in ("request_ref", "pending_id", "scope", "reason", "human_source", "source_message_id"):
            if f in op and op[f] is not None and not isinstance(op[f], str):
                errs.append(f"operations[{i}] {f} must be string")
            elif isinstance(op.get(f), str) and len(op[f]) > 500:
                errs.append(f"operations[{i}] {f} exceeds 500 characters")
        ex = op.get("evidence_excerpt")
        if not isinstance(ex, str) or not ex.strip() or len(ex) > 500:
            errs.append(f"operations[{i}] missing evidence_excerpt")
        elif not any(norm(ex) in h for h in [norm(event_text)] + [norm(x) for x in context_texts]):
            errs.append(f"operations[{i}] evidence not found in event/context")
        # A6: op-level source là canonical; slot-level source khác op-level
        # là mâu thuẫn provenance (engine cũng kiểm lại bằng store).
        if op.get("source_message_id") and isinstance(op.get("slot"), dict) and op["slot"].get("source_message_id"):
            if op["source_message_id"] != op["slot"]["source_message_id"]:
                errs.append(f"operations[{i}] slot source differs from op source (canonical is op-level)")
        slot = op.get("slot")
        if slot is not None:
            if not isinstance(slot, dict):
                errs.append(f"operations[{i}] slot must be object")
            else:
                # A6: nested fields nghiêm ngặt — slot chỉ có đúng các key
                # này; extra values phải là scalar (không dict/list lồng).
                for sk in slot:
                    if sk not in ("kind", "value", "raw", "evidence_excerpt", "explicit", "confirmed", "extra",
                                  "source_message_id", "source_request_id", "source_actor_id", "source_state_token"):
                        errs.append(f"operations[{i}] slot has disallowed field {sk!r}")
                kind = slot.get("kind")
                if not isinstance(kind, str) or kind not in SLOT_KINDS:
                    errs.append(f"operations[{i}] unknown slot kind {slot.get('kind')!r}")
                if "value" in slot and (type(slot["value"]) not in (str, int, float, dict)
                                         or isinstance(slot["value"], str) and len(slot["value"]) > 256
                        or isinstance(slot["value"], float) and not math.isfinite(slot["value"])
                        or type(slot["value"]) in (int, float) and abs(slot["value"]) > MAX_NUMERIC_SLOT):
                    errs.append(f"operations[{i}] slot value has wrong type")
                if "raw" in slot and (not isinstance(slot["raw"], str) or len(slot["raw"]) > 256):
                    errs.append(f"operations[{i}] slot.raw must be a string of at most 256 characters")
                if "evidence_excerpt" in slot and (not isinstance(slot["evidence_excerpt"], str)
                                                     or not slot["evidence_excerpt"].strip()
                                                     or len(slot["evidence_excerpt"]) > 500):
                    errs.append(f"operations[{i}] slot.evidence_excerpt must be non-empty text of at most 500 characters")
                if kind != "down_payment" and type(slot.get("value")) not in (str, int, float):
                    errs.append(f"operations[{i}] {kind} slot value must be scalar")
                if kind == "down_payment" and type(slot.get("value")) is not dict:
                    errs.append(f"operations[{i}] down_payment value must be an object")
                if slot.get("kind") == "down_payment" and "value" in slot:
                    value = slot["value"]
                    if (not isinstance(value, dict) or set(value) - {"amount", "unit"}
                            or type(value.get("amount")) not in (int, float)
                            or type(value.get("amount")) is float and not math.isfinite(value.get("amount"))
                            or abs(value.get("amount", 0)) > MAX_NUMERIC_SLOT
                            or value.get("unit") is not None and
                            (not isinstance(value.get("unit"), str) or len(value["unit"]) > 32)):
                        errs.append(f"operations[{i}] down_payment value must contain amount and optional unit")
                for bf in ("explicit", "confirmed"):
                    if bf in slot and not isinstance(slot[bf], bool):
                        errs.append(f"operations[{i}] slot.{bf} must be bool")
                for sf in ("source_message_id", "source_request_id", "source_actor_id", "source_state_token"):
                    if sf in slot and slot[sf] is not None and not isinstance(slot[sf], str):
                        errs.append(f"operations[{i}] slot.{sf} must be string")
                    elif isinstance(slot.get(sf), str) and len(slot[sf]) > (128 if sf == "source_state_token" else 500):
                        errs.append(f"operations[{i}] slot.{sf} exceeds its length limit")
                if slot.get("explicit", True) is False and not all(
                        slot.get(key) for key in ("source_message_id", "source_actor_id", "source_state_token")):
                    errs.append(f"operations[{i}] inferred slot needs message, actor and state provenance")
                extra = slot.get("extra")
                if extra is not None:
                    if not isinstance(extra, dict):
                        errs.append(f"operations[{i}] slot.extra must be object")
                    else:
                        for ek, ev in extra.items():
                            if ek == slot.get("kind"):
                                errs.append(f"operations[{i}] extra slot duplicates primary kind {ek!r}")
                            if ek not in SLOT_KINDS:
                                errs.append(f"operations[{i}] unknown extra slot kind {ek!r}")
                            if (type(ev) not in (str, int, float) or
                                    isinstance(ev, str) and len(ev) > 256 or
                                    isinstance(ev, float) and not math.isfinite(ev) or
                                    type(ev) in (int, float) and abs(ev) > MAX_NUMERIC_SLOT):
                                errs.append(f"operations[{i}] extra slot {ek!r} must be scalar")
                # If the operation excerpt is about the intent, match the
                # slot value directly against the current message; never use
                # an unrelated excerpt as proof of that value.
                if (slot.get("explicit", True) is not False and "kind" in slot and "value" in slot
                        and isinstance(op.get("evidence_excerpt"), str)):
                    slot_excerpt = slot.get("evidence_excerpt") or op["evidence_excerpt"]
                    supported = _positive_slot_evidence(
                        slot.get("kind"), slot["value"], slot, [slot_excerpt], [event_text])
                    if not supported:
                        supported = _positive_slot_evidence(
                            slot.get("kind"), slot["value"], slot, [event_text], [event_text])
                    if not supported:
                        errs.append(f"operations[{i}] slot evidence does not support its value")
                if isinstance(slot.get("extra"), dict):
                    for extra_kind, extra_value in slot["extra"].items():
                        if (not _positive_slot_evidence(extra_kind, extra_value,
                                                         {"raw": str(extra_value)},
                                                         [event_text], [event_text])):
                            errs.append(f"operations[{i}] extra slot evidence does not support {extra_kind}")
        if t == "SET_CONTACT_PREFERENCE" and op.get("scope") not in CONTACT_SCOPES:
            errs.append(f"operations[{i}] bad contact scope")
        if t == "REQUEST_HUMAN" and op.get("human_source") is not None and op["human_source"] not in (
                "customer_explicit", "model_proposed", "policy"):
            errs.append(f"operations[{i}] bad human_source (customer_explicit|model_proposed|policy)")
        # human_source mặc định = customer_explicit (giữ hành vi cũ): chỉ khi
        # model ghi rõ model_proposed thì engine mới tách phần factual ra gửi
        # kèm draft; khách yêu cầu người rõ ràng không bao giờ bị vô hiệu hoá
        # chỉ vì có facts.
        if t == "UNRESOLVED" and not op.get("reason"):
            errs.append(f"operations[{i}] UNRESOLVED needs reason")
        if t == "START_REQUEST":
            # START cho topic có polarity negative trong requests -> mâu thuẫn
            # (phủ định phải nhắm vào request đang mở bằng UPDATE/CANCEL,
            # không được START một request mới từ meaning phủ định).
            ref = op.get("request_ref")
            ref_topic = ref[6:] if isinstance(ref, str) and ref.startswith("topic:") else None
            nested_topic = op.get("request", {}).get("topic") if isinstance(op.get("request"), dict) else None
            topics = [x for x in (ref_topic, op.get("topic"), nested_topic) if x]
            topic = topics[0] if topics else None
            if len(set(topics)) > 1:
                errs.append(f"operations[{i}] START_REQUEST topic conflicts across fields")
            declared_topics = {r.get("topic") for r in sem.get("requests") or [] if isinstance(r, dict)}
            if topic and declared_topics and topic not in declared_topics:
                errs.append(f"operations[{i}] START_REQUEST topic {topic!r} is not declared in requests")
            for r in sem.get("requests") or []:
                if isinstance(r, dict) and topic and r.get("topic") == topic and r.get("polarity") == "negative":
                    errs.append(f"operations[{i}] START_REQUEST conflicts with negative polarity for {topic}")
    return errs
