"""Scorer v3 cho multiturn (R12). Khác scorer cũ ở những điểm sau, và mọi
điểm đều ảnh hưởng verdict/exit code (không chỉ in ra):
1. Không zip(): số lượt run != số lượt dataset -> STRUCTURAL_FAIL, exit 2.
2. Request match theo identity (topic + actor + vehicle/variant nếu expected
   nêu), không lấy request mới nhất cùng topic (latest_req cũ chọn nhầm khi
   nhiều request cùng topic).
3. Final-state fail làm conv FAIL và ảnh hưởng exit code (scorer cũ chỉ in).
4. Send precision vs expected-action recall tách riêng: precision = lượt có
   gửi thật mà khớp số gửi expected / tổng lượt có gửi thật; recall = lượt
   expected có gửi mà gửi khớp / tổng lượt expected có gửi.
5. Provenance: slot source_message_id phải là comment thật trong hội thoại
   (lấy từ run turns, không đoán theo pattern conv_id_t{i}); slot explicit
   mà không có source -> fail.
6. ASK-necessary: lượt hỏi làm rõ chỉ hợp lệ khi request tương ứng thật sự
   thiếu slot mà pending hỏi (xét state trước lượt + đúng request đó).
7. API/schema failure (validation_errors/errors, semantic thiếu) tính vào cả
   availability lẫn e2e: lượt đó FAIL, không được loại khỏi mẫu số.
8. Raw/adjusted/post-hoc tách riêng: không có file adjudication thì adjusted
   == raw và post-hoc rỗng. Adjudication (nếu có) là file JSON riêng, mỗi
   override phải có lý do, và được liệt kê đầy đủ trong output.
"""
from __future__ import annotations

def slot_val(slot):
    if isinstance(slot, dict): return slot.get("value")
    return slot

def match_req(requests, er, actor):
    """Match expected request theo identity. Trả (req, error).
    actor=None nghĩa là không lọc theo actor (dùng cho final-state đa actor);
    per-turn thì truyền actor của lượt đó."""
    want_actor = er.get("actor", actor)
    cands = []
    for r in requests:
        if r["topic"] != er["topic"]:
            continue
        ractor = r.get("actor_id") if r.get("actor_id") is not None else actor
        if want_actor is not None and ractor != want_actor:
            continue
        cands.append(r)
    slots = er.get("slots") or {}
    veh, var = slots.get("vehicle"), slots.get("variant")
    if veh is not None:
        cands = [r for r in cands if slot_val((r.get("slots") or {}).get("vehicle")) == veh]
    if var is not None:
        cands = [r for r in cands if slot_val((r.get("slots") or {}).get("variant")) == var]
    if not cands:
        return None, f"missing request {er['topic']} (identity vehicle={veh} variant={var})"
    if len(cands) > 1:
        return None, (f"ambiguous match for {er['topic']} (vehicle={veh} variant={var}): "
                      f"{len(cands)} candidates, scorer refuses latest-pick")
    return cands[0], None

def check_turn(turn_out, exp, comment_ids, prev_requests):
    """Trả (fails, info). info: send stats + ask-necessary notes."""
    fails = []
    actor = turn_out.get("actor")
    if turn_out.get("validation_errors"):
        fails.append(f"schema validation: {turn_out['validation_errors']}")
    if turn_out.get("errors"):
        fails.append(f"api/run errors: {turn_out['errors']}")
    if turn_out.get("semantic") is None:
        fails.append("missing semantic (api failure)")
    for er in exp.get("requests", []):
        r, err = match_req(turn_out.get("requests", []), er, actor)
        if err:
            fails.append(err); continue
        if "status" in er and r["status"] != er["status"]:
            fails.append(f"{er['topic']} status {r['status']} != {er['status']}")
        if "status_in" in er and r["status"] not in er["status_in"]:
            fails.append(f"{er['topic']} status {r['status']} not in {er['status_in']}")
        for k, v in (er.get("slots") or {}).items():
            got = slot_val((r.get("slots") or {}).get(k))
            if got != v:
                fails.append(f"{er['topic']} slot {k}={got!r} != {v!r}")
            # Provenance: slot khớp expected phải có source là comment thật.
            actual_slot = (r.get("slots") or {}).get(k)
            if isinstance(actual_slot, dict):
                src = actual_slot.get("source_message_id")
                if src not in comment_ids:
                    fails.append(f"{er['topic']} slot {k} provenance bad: source {src!r} "
                                 f"not a comment in this conversation")
    if "sends" in exp and turn_out.get("sends_n") != exp["sends"]:
        fails.append(f"sends {turn_out.get('sends_n')} != {exp['sends']}")
    for rc in exp.get("routing_contains", []):
        if rc not in (turn_out.get("routing") or []):
            fails.append(f"routing missing {rc} (got {turn_out.get('routing')})")
    rt = turn_out.get("response_text") or ""
    for c in exp.get("response_contains", []):
        if c not in rt: fails.append(f"response missing {c!r}")
    for c in exp.get("response_forbidden", []):
        if c in rt: fails.append(f"response contains forbidden {c!r}")
    if exp.get("has_pending"):
        if not any(p.get("status") == "OPEN" and p.get("actor_id") == actor
                   for p in turn_out.get("pendings") or []):
            fails.append("expected open pending, none")
    if "prefs" in exp:
        prefs = turn_out.get("prefs")
        if prefs is None:
            fails.append("prefs not recorded in run turn")
        else:
            # Expected dạng cũ: {"call": "call"} (dict kênh chặn); dạng mới:
            # list các kênh chặn. So sánh dưới dạng set kênh chặn.
            want = exp["prefs"]
            if isinstance(want, dict): want_set = set(want.keys())
            elif isinstance(want, (list, set)): want_set = set(want)
            else: want_set = {want}
            got = set(prefs) if isinstance(prefs, list) else {prefs}
            if got != want_set:
                fails.append(f"prefs {got!r} != {want_set!r}")
    for pur in exp.get("ledger_purposes", []):
        if pur not in (turn_out.get("ledger_purposes_added") or []):
            fails.append(f"ledger missing purpose {pur}")
    # Pending linkage: op tham chiếu pending phải mở trước lượt.
    for op in (turn_out.get("semantic") or {}).get("operations", []) or []:
        if op.get("op") in ("ANSWER_PENDING", "CONFIRM_PENDING", "REJECT_PENDING"):
            if op.get("pending_id") not in (turn_out.get("open_pendings_before") or []):
                fails.append(f"pending link bad: {op.get('op')} -> {op.get('pending_id')}")
    # ASK-necessary: pending mới trong lượt phải hỏi slot mà request tương ứng
    # thật sự thiếu ở state trước lượt.
    prev_pend_ids = set()
    if prev_requests is not None:
        # open_pendings_before trong run cũ là list id (str), không phải dict.
        prev_pend_ids = {p if isinstance(p, str) else p.get("id")
                         for p in (turn_out.get("open_pendings_before") or [])}
    for p in turn_out.get("pendings") or []:
        if p.get("id") in prev_pend_ids: continue
        if p.get("status") != "OPEN": continue
        req_before = None
        for r in (prev_requests or []):
            if r.get("id") == p.get("request_id"):
                req_before = r; break
        if req_before is None: continue
        asked = p.get("expected_slot")
        if asked and asked in (req_before.get("slots") or {}):
            fails.append(f"unnecessary ask: pending {p.get('id')} asks {asked} "
                         f"already filled before turn")
    return fails
