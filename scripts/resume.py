"""Resume equivalence (R13): checkpoint semantic tham chiếu pending/request
bằng id ngẫu nhiên của engine cũ; resume sang engine mới phải remap các id
đó sang id mới theo logical key (topic + identity slots cho request, request
logic + qtype + expected_slot cho pending). Không remap được -> semantic đó
không dùng lại được (trả lỗi rõ ràng, không đoán id).
"""
from __future__ import annotations
from scripts.cache_key import request_logical_key, pending_logical_key

def build_request_map(old_requests, new_requests):
    """Map old request id -> new request id theo logical key. Key trùng nhau
    (2 request cùng logical) -> không map (mơ hồ, phải hỏi lại)."""
    by_key = {}
    for r in new_requests:
        by_key.setdefault(request_logical_key(r), []).append(r.get("id"))
    mapping = {}
    for r in old_requests:
        cands = by_key.get(request_logical_key(r), [])
        if len(cands) == 1:
            mapping[r.get("id")] = cands[0]
    return mapping

def build_pending_map(old_pendings, new_pendings, old_reqs, new_reqs):
    """Map old pending id -> new pending id theo logical key."""
    old_by_id = {r.get("id"): r for r in old_reqs}
    new_by_id = {r.get("id"): r for r in new_reqs}
    by_key = {}
    for p in new_pendings:
        by_key.setdefault(pending_logical_key(p, new_by_id), []).append(p.get("id"))
    mapping = {}
    for p in old_pendings:
        cands = by_key.get(pending_logical_key(p, old_by_id), [])
        if len(cands) == 1:
            mapping[p.get("id")] = cands[0]
    return mapping

def try_remap_cached(sem, snapshot, cur_requests, cur_pendings):
    """Remap checkpoint semantic sang engine hiện tại (A7, dùng thật trong
    runner). snapshot: state_snapshot lúc checkpoint được ghi (requests +
    pendings kèm id cũ); cur_*: state logic hiện tại (kèm id mới).
    Trả (semantic_đã_remap, None) hoặc (None, lý_do) khi không remap được
    (snapshot thiếu, id mơ hồ, request/pending đã đổi logic) — khi đó
    runner PHẢI gọi lại adapter, không được dùng semantic cũ với id cũ."""
    if not snapshot or not isinstance(snapshot, dict):
        return None, "no state snapshot in checkpoint"
    old_reqs = snapshot.get("requests") or []
    # Chỉ map pending đang OPEN ở cả hai phía: semantic cache được sinh ra
    # khi pending đó còn OPEN; pending đã SUPERSEDED/EXPIRED không phải là
    # mục tiêu remap (dùng nó sẽ thành REVIEW_BAD_PENDING).
    old_pends = [p for p in (snapshot.get("pendings") or []) if p.get("status") == "OPEN"]
    cur_pendings = [p for p in (cur_pendings or []) if p.get("status") == "OPEN"]
    req_map = build_request_map(old_reqs, cur_requests or [])
    pend_map = build_pending_map(old_pends, cur_pendings or [], old_reqs, cur_requests or [])
    try:
        return remap_semantic(sem, req_map, pend_map), None
    except ValueError as e:
        return None, str(e)

def remap_semantic(sem, req_map, pend_map):
    """Viết lại request_ref (id trần) và pending_id trong operations theo map.
    Ref nào không map được -> raise ValueError (không đoán)."""
    import copy
    sem = copy.deepcopy(sem)
    for op in sem.get("operations", []) or []:
        ref = op.get("request_ref")
        if ref and not str(ref).startswith("topic:"):
            if ref not in req_map:
                raise ValueError(f"cannot remap request_ref {ref!r} (no logical match)")
            op["request_ref"] = req_map[ref]
        pid = op.get("pending_id")
        if pid:
            if pid not in pend_map:
                raise ValueError(f"cannot remap pending_id {pid!r} (no logical match)")
            op["pending_id"] = pend_map[pid]
    return sem
