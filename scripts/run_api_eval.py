"""Luna API eval (baseline A): semantic parser thật + cùng Engine.
Usage: python3 scripts/run_api_eval.py --input datasets/development/api_cases.jsonl
       --out runs/api_dev_b1.json [--limit 40] [--stratified]
Checkpoint từng ca: runs/api_checkpoints/<case_id>.json (resume không gọi lại).
Budget guard: pre-estimate 0.00032/call; dừng nếu vượt $2.00 hoặc 300 calls tích lũy.
"""
from __future__ import annotations
import json, os, sys, uuid
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.harness import make_engine
from app.facts import Facts
from app.engine import Clock
from app.semantics import validate_semantic
from app.adapters.luna import call_luna, MODEL
from datetime import datetime, timezone

def route_of(res):
    r = res["routing"]
    if "ERROR_ROLLBACK" in r: return "ERROR"
    if res["sends"]:
        return "ASK" if all(x == "ask_clarification" for x in r if x not in ("SOCIAL_ACK",)) and "ask_clarification" in r else "AUTO"
    if any(x == "ask_clarification" for x in r): return "ASK"
    if "HANDOFF_DRAFT_ONLY" in r: return "HANDOFF"
    if any(x.startswith(("REVIEW", "BLOCKED")) for x in r): return "REVIEW"
    return "REVIEW"

def stratified(cases, limit):
    by = {}
    for c in cases: by.setdefault(c["family"], []).append(c)
    out, i = [], 0
    while len(out) < limit:
        added = False
        for fam in sorted(by):
            if i < len(by[fam]):
                out.append(by[fam][i]); added = True
                if len(out) >= limit: break
        if not added: break
        i += 1
    return out

def main():
    args = sys.argv[1:]
    inp = args[args.index("--input") + 1]
    out_path = args[args.index("--out") + 1] if "--out" in args else "runs/api_eval.json"
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 60
    cases = [json.loads(l) for l in open(os.path.join(ROOT, inp)) if l.strip()]
    if "--stratified" in args: cases = stratified(cases, limit)
    else: cases = cases[:limit]
    # Checkpoint v2 (R13): key gắn với từng input (comment+parent+postcard) +
    # prompt + schema + model — key v1 cũ (chỉ prompt+schema+model) dùng chung
    # cho mọi case nên KHÔNG BAO GIỜ được reuse (warn + gọi lại).
    # Checkpoint chỉ lưu model response; kết quả engine tính lại khi resume.
    from app.adapters.luna import SYSTEM as _SYS
    from scripts.cache_key import full_cache_key, is_legacy_checkpoint
    from scripts import budget_ledger
    _schema = open(os.path.join(ROOT, "config", "schema_versions", "semantic_v1.json")).read()
    ckdir = args[args.index("--ckdir") + 1] if "--ckdir" in args else os.path.join(ROOT, "runs", "api_checkpoints")
    os.makedirs(ckdir, exist_ok=True)
    # budget tích lũy từ các checkpoint đã có (không tính lại) + ledger cộng dồn
    # A8: baseline ledger đọc MỘT LẦN (trước đây mỗi case đọc lại totals()
    # sau khi record -> double count cùng một call).
    _lc0, _lcost0, _lunk0, _ = budget_ledger.totals()
    _prior_calls, _prior_cost = (budget_ledger.SHARED_PRIOR_CALLS,
                                 budget_ledger.SHARED_PRIOR_COST_USD)
    _run_unknown = False
    total_calls = total_cost = 0.0
    results = []
    def _make_hook():
        reservations = []
        def hook():
            reservation_id = uuid.uuid4().hex
            allowed, _ = budget_ledger.reserve(
                budget_ledger.DEFAULT_PATH, reservation_id=reservation_id,
                max_calls=300, max_cost=2.00, reserve_cost_usd=0.00032,
                prior_calls=_prior_calls, prior_cost=_prior_cost)
            if allowed: reservations.append(reservation_id)
            return allowed
        return hook, reservations
    for case in cases:
        ck = os.path.join(ckdir, case["id"] + ".json")
        case_key = full_cache_key(comment=case["comment"], parent=case.get("parent", ""),
                                  postcard=case["postcard"], state_fp="",
                                  system=_SYS, schema=_schema, model=MODEL)
        cached_sem = None
        if os.path.exists(ck):
            rec = json.load(open(ck))
            if rec.get("cache_key_v2") == case_key and rec.get("semantic"):
                # Hit: chỉ reuse semantic, engine chạy lại để tính routing mới.
                cached_sem = rec["semantic"]
                total_calls += 0; total_cost += 0
            elif is_legacy_checkpoint(rec):
                print(f"WARNING legacy checkpoint {ck} ignored (v1 key ignores input); re-calling")
        if cached_sem is not None:
            sem = cached_sem
            errs = validate_semantic(sem, case["comment"], [case.get("parent", "")])
            if errs:
                rec = dict(cache_key_v2=case_key, id=case["id"], family=case["family"], ok=True, validation_errors=errs,
                           attempts=0, cost_usd_est=0, routing="REVIEW",
                           expected_topics=case["expected_topics"], expected_routing=case["expected_routing"],
                           pred_topics=[], topic_exact=False, topic_recall=0.0)
                results.append(rec); continue
            # chạy engine lại với semantic từ cache (xem khối engine phía dưới)
            api = dict(ok=True, semantic=sem, attempts=0, cost_usd_est=0,
                       usage=None, model_effective=MODEL)
        else:
            if (_lc0 + _prior_calls + total_calls + 1 > 300 or
                    _lcost0 + _prior_cost + total_cost + 0.00032 > 2.00 or _lunk0 > 0 or _run_unknown):
                print(f"BUDGET STOP trước {case['id']}: ledger baseline calls={_lc0} cost~{_lcost0:.4f} unknown={_lunk0}, run calls={total_calls}"); break
            post = f"{case['postcard'].get('topic', '')} — xe: {', '.join(case['postcard'].get('vehicles', []))}"
            call_guard, reservations = _make_hook()
            api = call_luna(post, case["comment"], parent=case.get("parent", ""),
                            budget_guard=call_guard)
            total_calls += api.get("attempts", 1)
            if api.get("ok"): total_cost += api.get("cost_usd_est") or 0.0
            if api.get("cost_usd_est") is None: _run_unknown = True
            budget_ledger.record(budget_ledger.DEFAULT_PATH, run_id="api_eval",
                                 kind="call", attempts=api.get("attempts", 1),
                                 cost_usd_est=api.get("cost_usd_est"),
                                 model=api.get("model_effective", MODEL),
                                 status=None if api.get("ok") else "failed")
            budget_ledger.settle_reservations(budget_ledger.DEFAULT_PATH, reservations)
            if not api.get("ok"):
                rec = dict(cache_key_v2=case_key, id=case["id"], family=case["family"], ok=False, error=api.get("error"),
                           attempts=api.get("attempts", 1), cost_usd_est=0, routing="ERROR",
                           expected_topics=case["expected_topics"], expected_routing=case["expected_routing"])
                results.append(rec); json.dump(rec, open(ck, "w"), ensure_ascii=False, indent=2); continue
            sem = api["semantic"]
            errs = validate_semantic(sem, case["comment"], [case.get("parent", "")])
            if errs and api.get("attempts", 1) < 3:
                # 1 lần repair, guard TRƯỚC repair (R14; trước đây không có).
                if (_lc0 + _prior_calls + total_calls + 1 > 300 or
                        _lcost0 + _prior_cost + total_cost + 0.00032 > 2.00 or _lunk0 > 0 or _run_unknown):
                    print(f"BUDGET STOP trước repair {case['id']}")
                else:
                    repair_guard, reservations = _make_hook()
                    rep = call_luna(post, case["comment"], parent=case.get("parent", ""),
                                    repair_note="; ".join(errs), budget_guard=repair_guard)
                    total_calls += rep.get("attempts", 1)
                    if rep.get("ok"): total_cost += rep.get("cost_usd_est") or 0.0
                    if rep.get("cost_usd_est") is None: _run_unknown = True
                    budget_ledger.record(budget_ledger.DEFAULT_PATH, run_id="api_eval",
                                         kind="repair", attempts=rep.get("attempts", 1),
                                         cost_usd_est=rep.get("cost_usd_est"),
                                         model=rep.get("model_effective", MODEL),
                                         status=None if rep.get("ok") else "failed")
                    budget_ledger.settle_reservations(budget_ledger.DEFAULT_PATH, reservations)
                    if rep.get("ok"):
                        sem2 = rep["semantic"]
                        errs2 = validate_semantic(sem2, case["comment"], [case.get("parent", "")])
                        if not errs2:
                            sem, api = sem2, rep; errs = []
                            api["repair_used"] = True
        if errs:
            rec = dict(cache_key_v2=case_key, semantic=sem, id=case["id"], family=case["family"], ok=True, validation_errors=errs,
                       attempts=api.get("attempts", 1), cost_usd_est=api.get("cost_usd_est", 0),
                       usage=api.get("usage"), model_effective=api.get("model_effective"),
                       routing="REVIEW", expected_topics=case["expected_topics"],
                       expected_routing=case["expected_routing"], pred_topics=[],
                       topic_exact=False, topic_recall=0.0)
            results.append(rec); json.dump(rec, open(ck, "w"), ensure_ascii=False, indent=2); continue
        # chạy cùng Engine (single turn, clock cố định)
        eng = make_engine(clock=Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)), facts=Facts())
        ev = dict(page_id="page_hyundai_sample", post_id="post_1", thread_root_id="root",
                  comment_id="api_" + case["id"], parent_id="root", actor_id="u_api",
                  actor_role="user", event_type="create", content_version=1,
                  text=case["comment"], event_dedupe_key="api_" + case["id"] + ":1")
        res = eng.process(ev, case["postcard"], sem)
        pred = sorted({r.get("topic") for r in sem.get("requests", []) if r.get("topic")})
        if not pred and any(o.get("op") == "SOCIAL_ACK" for o in sem.get("operations", [])):
            pred = ["social"]
        exp = sorted(case["expected_topics"])
        recall = (len(set(pred) & set(exp)) / len(exp)) if exp else (1.0 if not pred else 0.0)
        rec = dict(cache_key_v2=case_key, id=case["id"], family=case["family"], ok=True, validation_errors=[],
                   semantic=sem, engine_routing=res["routing"], repair_used=api.get("repair_used", False),
                   attempts=api.get("attempts", 1), cost_usd_est=api.get("cost_usd_est", 0),
                   usage=api.get("usage"), model_effective=api.get("model_effective"),
                   routing=route_of(res), expected_routing=case["expected_routing"],
                   pred_topics=pred, expected_topics=exp,
                   topic_exact=(pred == exp), topic_recall=recall,
                   response_text=res.get("response_text"))
        results.append(rec); json.dump(rec, open(ck, "w"), ensure_ascii=False, indent=2)
    EXCLUDED = {"api_parser_unknown_enum", "api_err_invalid_op", "api_err_bool_sarcasm",
                "api_err_fake_evidence", "api_err_no_semantic", "api_err_injection",
                "api_err_privacy_0", "api_err_privacy_1", "api_err_kill_switch",
                "api_facts_conflict", "api_promo_expired"}
    scored = [r for r in results if r["id"] not in EXCLUDED]
    excluded = [r for r in results if r["id"] in EXCLUDED]
    n = len(scored); results_scored = scored
    ok = [r for r in scored if r.get("ok") and not r.get("validation_errors")]
    exact = sum(1 for r in ok if r["topic_exact"]) / len(ok) if ok else 0
    recall = sum(r["topic_recall"] for r in scored) / n if n else 0
    rout = sum(1 for r in ok if r["routing"] == r["expected_routing"]) / len(ok) if ok else 0
    summary = dict(cases=n, excluded_cases=[r["id"] for r in excluded], api_ok=len(ok),
                   validation_errors=sum(1 for r in scored if r.get("validation_errors")),
                   api_errors=sum(1 for r in scored if not r.get("ok")),
                   topic_exact=exact, topic_recall_mean=recall, routing_match=rout,
                   total_calls=total_calls, total_cost_usd_est=total_cost,
                   prior_calls=_prior_calls, prior_cost_usd=_prior_cost,
                   cumulative_calls=_lc0 + _prior_calls + total_calls,
                   cumulative_cost_usd=_lcost0 + _prior_cost + total_cost,
                   model_requested=MODEL, results=results)
    os.makedirs(os.path.dirname(os.path.join(ROOT, out_path)), exist_ok=True)
    json.dump(summary, open(os.path.join(ROOT, out_path), "w"), ensure_ascii=False, indent=2)
    print(f"api eval: n={n} ok={len(ok)} val_err={summary['validation_errors']} api_err={summary['api_errors']} "
          f"topic_exact={exact:.3f} recall={recall:.3f} routing={rout:.3f} calls={total_calls} cost~${total_cost:.4f}")
    for r in scored:
        if not r.get("ok") or r.get("validation_errors") or (r.get("ok") and not r["topic_exact"]):
            print(f"  MISS {r['id']} ({r['family']}): exp={r.get('expected_topics')} pred={r.get('pred_topics')} "
                  f"rout={r.get('routing')}/{r.get('expected_routing')} err={r.get('validation_errors') or r.get('error')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
