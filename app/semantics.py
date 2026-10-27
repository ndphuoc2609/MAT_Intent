"""Semantic schema v1 + validator nghiêm ngặt. Model chỉ ĐỀ XUẤT operations."""
from __future__ import annotations
import json, math
from .normalize import norm

TOPICS = ["price", "promo", "loan", "stock", "spec", "dealer", "testdrive",
          "buy", "service", "complaint", "registration", "social"]
OPS = ["START_REQUEST", "UPDATE_SLOT", "ANSWER_PENDING", "CONFIRM_PENDING",
       "REJECT_PENDING", "CANCEL_REQUEST", "SET_CONTACT_PREFERENCE",
       "REQUEST_HUMAN", "REPORT_COMPLAINT", "SOCIAL_ACK", "UNRESOLVED"]
SENTIMENTS = ["POS", "NEG", "NEU"]
SARCASM = ["present", "absent", "uncertain"]
SCOPES = ["in_scope", "out_of_scope", "unknown"]
POLARITY = ["positive", "negative", "conditional", "unknown"]
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

def validate_semantic(sem, event_text: str, context_texts: list[str]) -> list[str]:
    """Trả về danh sách lỗi; rỗng = hợp lệ. Không âm thầm drop field lạ."""
    errs = []
    if not isinstance(sem, dict): return ["semantic must be object"]
    try: _reject_nan(sem)
    except ValueError as e: return [str(e)]
    if sem.get("schema_version") != "1":
        errs.append("schema_version must be '1'")
    if sem.get("sentiment") not in SENTIMENTS: errs.append("bad sentiment")
    if sem.get("sarcasm") not in SARCASM:
        errs.append("sarcasm must be present/absent/uncertain (no bool/float probability)")
    if sem.get("scope") not in SCOPES: errs.append("bad scope")
    uncertainties = sem.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        errs.append("uncertainties must be list")
    else:
        for i, uncertainty in enumerate(uncertainties):
            if not isinstance(uncertainty, dict):
                errs.append(f"uncertainties[{i}] must be object")
                continue
            if set(uncertainty) != {"alternatives", "different_action"}:
                errs.append(f"uncertainties[{i}] must contain only alternatives and different_action")
            if type(uncertainty.get("different_action")) is not bool:
                errs.append(f"uncertainties[{i}].different_action must be bool")
            alternatives = uncertainty.get("alternatives")
            if (not isinstance(alternatives, list) or len(alternatives) < 2
                    or any(not isinstance(value, str) or not value.strip() for value in alternatives)):
                errs.append(f"uncertainties[{i}].alternatives must contain at least two non-empty strings")
    if not isinstance(sem.get("requests"), list): errs.append("requests must be list")
    else:
        for i, r in enumerate(sem["requests"]):
            if not isinstance(r, dict): errs.append(f"requests[{i}] not object"); continue
            if r.get("topic") not in TOPICS: errs.append(f"requests[{i}] bad topic")
            if r.get("polarity") not in POLARITY: errs.append(f"requests[{i}] bad polarity")
            if not r.get("desired_outcome"): errs.append(f"requests[{i}] missing desired_outcome")
    if not isinstance(sem.get("operations"), list) or not sem.get("operations"):
        errs.append("operations must be non-empty list")
        return errs
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
        for f in ("request_ref", "pending_id", "scope", "reason", "human_source", "source_message_id"):
            if f in op and op[f] is not None and not isinstance(op[f], str):
                errs.append(f"operations[{i}] {f} must be string")
        ex = op.get("evidence_excerpt")
        if not isinstance(ex, str) or not ex.strip():
            errs.append(f"operations[{i}] missing evidence_excerpt")
        elif not any(norm(ex) in h for h in [norm(event_text)] + [norm(x) for x in context_texts]):
            errs.append(f"operations[{i}] evidence not found in event/context")
        # A6: op-level source là canonical; slot-level source khác op-level
        # là mâu thuẫn provenance (engine cũng kiểm lại bằng store).
        if op.get("source_message_id") and (op.get("slot") or {}).get("source_message_id"):
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
                    if sk not in ("kind", "value", "raw", "explicit", "confirmed", "extra", "source_message_id"):
                        errs.append(f"operations[{i}] slot has disallowed field {sk!r}")
                if slot.get("kind") not in SLOT_KINDS:
                    errs.append(f"operations[{i}] unknown slot kind {slot.get('kind')!r}")
                if "value" in slot and not isinstance(slot["value"], (str, int, float, dict)):
                    errs.append(f"operations[{i}] slot value has wrong type")
                for bf in ("explicit", "confirmed"):
                    if bf in slot and not isinstance(slot[bf], bool):
                        errs.append(f"operations[{i}] slot.{bf} must be bool")
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
                            if type(ev) not in (str, int, float):
                                errs.append(f"operations[{i}] extra slot {ek!r} must be scalar")
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
