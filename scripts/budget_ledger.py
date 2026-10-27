"""Budget ledger cộng dồn, append-only (R14).
Vấn đề cũ: mỗi runner giữ budget trong bộ nhớ + per-ckdir, nên chạy nhiều
ckdir/run khác nhau vẫn vượt tổng 300 calls / $2.00 mà không ai chặn.
Ledger này là MỘT file JSONL duy nhất (mặc định runs/budget_ledger.jsonl):
mỗi attempt (gồm cả repair và retry nội bộ adapter, vì attempts đã bao gồm
chúng) append một dòng, không bao giờ ghi đè hay xóa. Guard kiểm tra TRƯỚC
mỗi attempt và TRƯỚC mỗi repair.

Usage thiếu: adapter không trả usage -> cost_usd_est=None, cost_unknown=True.
Khi đó guard chi phí KHÔNG xác minh được -> fail-closed: chặn spend tiếp
(ngoại trừ replay từ cache, cost = 0) cho tới khi có usage trở lại. Calls
vẫn đếm bình thường.
"""
from __future__ import annotations
import json, math, os
from datetime import datetime, timezone

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "runs", "budget_ledger.jsonl")

def _valid_amount(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False

def _read_records(path):
    records = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if not line.strip(): continue
                try: records.append(json.loads(line))
                except (ValueError, RecursionError): records.append(None)
    return records

def _reconciled_costs(records):
    targets, events = {}, {}
    for i, rec in enumerate(records):
        if not isinstance(rec, dict): continue
        key = (rec.get("ts"), rec.get("run_id"))
        if rec.get("kind") == "reconcile":
            target = rec.get("reconciles")
            if (rec.get("status") == "reconciled" and type(rec.get("attempts")) is int
                    and rec["attempts"] == 0
                    and _valid_amount(rec.get("cost_usd_est"))
                    and isinstance(rec.get("reason"), str) and rec["reason"].strip()
                    and isinstance(target, dict) and set(target) == {"ts", "run_id"}
                    and all(isinstance(v, str) and v for v in target.values())):
                target = (target["ts"], target["run_id"])
                events.setdefault(target, []).append((i, float(rec["cost_usd_est"])))
        elif (type(rec.get("attempts")) is int and rec["attempts"] > 0
              and isinstance(rec.get("ts"), str) and isinstance(rec.get("run_id"), str)
              and not _valid_amount(rec.get("cost_usd_est"))
              and rec.get("status") in ("failed", "unknown_cost")):
            targets[key] = targets.get(key, 0) + 1
    costs, valid_rows = {}, set()
    for key, rows in events.items():
        if targets.get(key) == 1 and len(rows) == 1:
            valid_rows.add(rows[0][0])
            costs[key] = rows[0][1]
    return costs, valid_rows

def totals(path=DEFAULT_PATH):
    """Trả (calls, cost_known, unknown_count, entries). File không có -> 0."""
    calls, cost, unknown, n = 0, 0.0, 0, 0
    records = _read_records(path)
    reconciled, valid_rows = _reconciled_costs(records)
    for i, rec in enumerate(records):
        n += 1
        if i in valid_rows: continue
        if isinstance(rec, dict) and rec.get("kind") == "reconcile":
            unknown += 1
            continue
        if (not isinstance(rec, dict) or type(rec.get("attempts")) is not int
                or not _valid_amount(rec["attempts"])):
            unknown += 1
            continue
        calls += rec["attempts"]
        if not _valid_amount(rec.get("cost_usd_est")):
            key = ((rec.get("ts"), rec.get("run_id"))
                   if isinstance(rec.get("ts"), str) and isinstance(rec.get("run_id"), str) else None)
            if key is not None and key in reconciled: cost += reconciled[key]
            else: unknown += 1
        else:
            cost += float(rec.get("cost_usd_est") or 0)
    return calls, cost, unknown, n

def record(path, *, run_id, kind, attempts, cost_usd_est, model="", note="", status=None):
    """Append một attempt. kind: 'call' | 'repair' | 'retry'. status:
    'completed' | 'failed' | 'unknown_cost' (mặc định suy ra từ cost:
    cost None -> 'unknown_cost', ngược lại 'completed'). Báo cáo tách
    attempted/reserved/completed/failed/actual cost/unknown cost bằng
    breakdown() thay vì cộng gộp một số duy nhất."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not _valid_amount(cost_usd_est):
        cost_usd_est = None
    if status is None:
        status = "unknown_cost" if cost_usd_est is None else "completed"
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
           "kind": kind, "attempts": int(attempts),
           "cost_usd_est": cost_usd_est,
           "cost_unknown": cost_usd_est is None,
           "status": status,
           "model": model, "note": note}
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec

def reconcile_unknown_cost(path=DEFAULT_PATH, *, original_ts, run_id, cost_usd_est, reason):
    """Append an audited cost correction without changing the original attempt."""
    if not isinstance(original_ts, str) or not original_ts or not isinstance(run_id, str) or not run_id:
        raise ValueError("reconciliation target must identify an attempt")
    if not _valid_amount(cost_usd_est) or not isinstance(reason, str) or not reason.strip():
        raise ValueError("reconciliation needs a known non-negative cost and reason")
    records = _read_records(path)
    targets = [r for r in records if isinstance(r, dict) and r.get("ts") == original_ts
               and r.get("run_id") == run_id and r.get("status") in ("failed", "unknown_cost")
               and type(r.get("attempts")) is int and r["attempts"] > 0
               and not _valid_amount(r.get("cost_usd_est"))]
    if len(targets) != 1:
        raise ValueError("reconciliation target must be one unknown-cost attempt")
    if any(isinstance(r, dict) and r.get("kind") == "reconcile"
           and r.get("reconciles") == {"ts": original_ts, "run_id": run_id} for r in records):
        raise ValueError("attempt already has a reconciliation")
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
           "kind": "reconcile", "attempts": 0, "cost_usd_est": float(cost_usd_est),
           "cost_unknown": False, "status": "reconciled",
           "reconciles": {"ts": original_ts, "run_id": run_id}, "reason": reason.strip()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec

def breakdown(path=DEFAULT_PATH):
    """Tách attempted/completed/failed/unknown_cost/actual cost (A8)."""
    attempted = completed = failed = unknown_cost = 0
    cost = 0.0
    records = _read_records(path)
    reconciled, valid_rows = _reconciled_costs(records)
    for i, rec in enumerate(records):
        if i in valid_rows: continue
        if not isinstance(rec, dict): continue
        if rec.get("kind") == "reconcile":
            unknown_cost += 1
            continue
        a = rec.get("attempts", 0)
        if type(a) is not int or a < 0:
            unknown_cost += 1
            continue
        attempted += a
        st = rec.get("status")
        if st == "failed": failed += a
        if not _valid_amount(rec.get("cost_usd_est")):
            key = ((rec.get("ts"), rec.get("run_id"))
                   if isinstance(rec.get("ts"), str) and isinstance(rec.get("run_id"), str) else None)
            if key is not None and key in reconciled: cost += reconciled[key]
            else: unknown_cost += a
        elif st != "failed":
            completed += a
            cost += float(rec.get("cost_usd_est") or 0)
    return dict(attempted=attempted, completed=completed, failed=failed,
                unknown_cost=unknown_cost, actual_cost_usd=cost)

def guard(path=DEFAULT_PATH, *, max_calls=300, max_cost=2.00, est_attempts=1,
          est_cost=0.0, cost_unknown=False, prior_calls=0, prior_cost=0.0):
    """Trả (allowed, reason). est_cost=None hoặc cost_unknown=True -> cost
    không xác minh được -> chặn spend (fail-closed). prior_* chỉ gồm usage
    trước đó chưa có trong ledger này, tránh đếm hai lần."""
    for name, value in (("max_calls", max_calls), ("max_cost", max_cost),
                        ("est_attempts", est_attempts), ("est_cost", est_cost),
                        ("prior_calls", prior_calls), ("prior_cost", prior_cost)):
        if (not _valid_amount(value)
                or (name in ("max_calls", "est_attempts", "prior_calls") and type(value) is not int)):
            return False, f"invalid budget input: {name}; spend blocked fail-closed"
    calls, cost, unknown, _ = totals(path)
    calls += prior_calls
    cost += prior_cost
    if calls + est_attempts > max_calls:
        return False, f"calls {calls}+{est_attempts} > {max_calls}"
    if unknown > 0 or cost_unknown or est_cost is None:
        return False, f"cost unknown (ledger unknown entries: {unknown}), spend blocked fail-closed"
    if cost + est_cost > max_cost:
        return False, f"cost {cost:.5f}+{est_cost:.5f} > {max_cost}"
    return True, f"ok (calls {calls}, cost known {cost:.5f}, unknown {unknown})"
