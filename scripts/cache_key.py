"""Cache key v2 cho model checkpoints (R13).
Key cũ chỉ hash (SYSTEM + schema + MODEL) — hai comment khác nhau, hai state
khác nhau vẫn chung key. Key v2 hash toàn bộ đầu vào quyết định semantic:
comment, parent, postcard, context/state fingerprint (requests dạng logical
handles topic+slots, pendings dạng logical key, recent texts), SYSTEM, schema
và effective model version. Checkpoint v2 chỉ lưu model response (semantic);
kết quả engine luôn tính lại khi resume (không tin routing/response cũ).

Legacy: checkpoint chỉ có cache_key v1 (16 hex, không có cache_key_v2) thì
runner PHẢI cảnh báo và gọi lại (fallback = live call), không được reuse
im lặng — vì key v1 không chứng minh được input/state trùng khớp.
"""
from __future__ import annotations
import hashlib, json

def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)

def _slot_logical(slots) -> dict:
    # Keep every field that can change grounding or policy, including lineage;
    # random request/pending handles are mapped separately.
    return dict(slots or {})

def request_logical_key(req: dict) -> str:
    """Logical request identity with all slots/outcomes and no random request id."""
    return _canon({k: req.get(k) for k in ("page_id", "post_id", "thread_root_id", "actor_id",
                                           "topic", "status", "version", "desired_outcomes")} |
                   {"slots": _slot_logical(req.get("slots"))})

def pending_logical_key(pend: dict, req_by_id=None) -> str:
    """Logical pending identity with proposition, status, options and basis."""
    req_key = None
    if req_by_id is not None and pend.get("request_id") in (req_by_id or {}):
        req_key = request_logical_key(req_by_id[pend["request_id"]])
    return _canon({"request": req_key, **{k: pend.get(k) for k in
                   ("actor_id", "status", "version", "qtype", "expected_slot", "options",
                    "proposition", "issued_at", "expires_at", "basis_version", "basis_slots")}})

def state_fingerprint(requests, pendings, recent_texts, *, context=None, policy=None) -> str:
    """Fingerprint state trước lượt: requests (logical) + pendings mở (logical)
    + recent texts. Id ngẫu nhiên KHÔNG nằm trong fingerprint (resume sang
    engine mới vẫn khớp nếu nội dung logic giống nhau)."""
    req_by_id = {r.get("id"): r for r in (requests or []) if r.get("id")}
    reqs = sorted(request_logical_key(r) for r in (requests or []))
    pends = sorted(pending_logical_key(p, req_by_id)
                   for p in (pendings or []) if p.get("status") == "OPEN")
    return hashlib.sha256(_canon({"requests": reqs, "pendings": pends,
                                  "recent": list(recent_texts or []), "context": context,
                                  "policy": policy}).encode()).hexdigest()

def full_cache_key(*, comment, parent, postcard, state_fp, system, schema,
                   model) -> str:
    """Key v2 đầy đủ (trả hex 64). Mọi thành phần ảnh hưởng semantic đều có mặt.
    A7: KHÔNG đưa context_text (fmt_context) vào key nữa — context_text chứa
    request/pending UUID ngẫu nhiên của engine hiện tại nên resume sang engine
    mới luôn cache miss dù state logic giống hệt. state_fp đã bao gồm mọi thứ
    context_text đóng góp thêm (requests/pendings dạng logical + recent texts);
    UUID không phải là một phần của input logic."""
    payload = {"comment": comment, "parent": parent, "postcard": postcard,
               "state_fp": state_fp, "system": system, "schema": schema,
               "model": model}
    return hashlib.sha256(_canon(payload).encode()).hexdigest()

def is_legacy_checkpoint(rec: dict) -> bool:
    """Checkpoint cũ: có cache_key v1 nhưng không có cache_key_v2."""
    return bool(rec.get("cache_key")) and not rec.get("cache_key_v2")
