"""Chạy contract regression qua cùng Engine (không logic riêng trong test).
Usage: python3 scripts/run_contract.py [--split dev|holdout|all] [--only ID...]
Ghi runs/contract_run.json + runs/checkpoints/<id>.json (checkpoint từng ca).
"""
from __future__ import annotations
import json, os, sys, copy
from datetime import datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.harness import make_engine
from app.facts import Facts, SEED_FACTS
from app.engine import Clock

def resolve(sem, eng, event):
    if sem is None: return None
    sem = copy.deepcopy(sem)
    scope = (event.get("page_id"), event.get("post_id"), event.get("thread_root_id"))
    def rep(obj):
        if isinstance(obj, dict):
            return {k: rep(v) for k, v in obj.items()}
        if isinstance(obj, list): return [rep(v) for v in obj]
        if obj == "$PENDING":
            if not all(scope): return "$PENDING_MISSING"
            pend = eng.store.open_pending_for(event["actor_id"], page_id=scope[0],
                                              post_id=scope[1], thread_root_id=scope[2])
            return pend[-1]["id"] if pend else "$PENDING_MISSING"
        if obj == "$OTHER_REQUEST":
            if not all(scope): return "$OTHER_MISSING"
            rows = eng.store.conn.execute(
                "SELECT id, actor_id FROM requests WHERE page_id=? AND post_id=? AND thread_root_id=?",
                scope).fetchall()
            for rid, actor in rows:
                if actor != event["actor_id"]: return rid
            return "$OTHER_MISSING"
        return obj
    return rep(sem)

def check(scen, out):
    fails = []
    got = sorted((r["topic"], r["status"]) for r in out["final_requests"])
    exp = sorted((r["topic"], r["status"]) for r in scen["expected_final"])
    if got != exp: fails.append(f"final_requests got={got} exp={exp}")
    sends = sum(len(t["result"]["sends"]) for t in out["traces"])
    if sends != scen["expected_sends"]: fails.append(f"sends got={sends} exp={scen['expected_sends']}")
    texts = [t["result"].get("response_text") or "" for t in out["traces"]]
    for sub in scen.get("text_contains", []):
        if not any(sub in tx for tx in texts): fails.append(f"text missing {sub!r} in {texts}")
    for sub in scen.get("forbidden_text", []):
        if any(sub in tx for tx in texts): fails.append(f"forbidden text {sub!r} present")
    routings = [r for t in out["traces"] for r in t["result"]["routing"]]
    for r in scen.get("routing_contains", []):
        if r not in routings: fails.append(f"routing missing {r} in {routings}")
    if scen.get("expected_slots"):
        req = next((r for r in out["final_requests"] if r["topic"] in
                    [e["topic"] for e in scen["expected_final"]]), None)
        if req:
            slots = {k: v["value"] for k, v in req["slots"].items()}
            for k, v in scen["expected_slots"].items():
                if slots.get(k) != v: fails.append(f"slot {k} got={slots.get(k)} exp={v}")
            for k in scen.get("forbidden_slots", []):
                if k in req["slots"]: fails.append(f"forbidden slot {k} present")
    if scen.get("expected_pending"):
        got_p = sorted(p["status"] for p in out.get("final_pending", []))
        exp_p = sorted(p["status"] for p in scen["expected_pending"])
        if got_p != exp_p: fails.append(f"pending got={got_p} exp={exp_p}")
    return fails

def main():
    args = sys.argv[1:]
    split = "all"; only = set()
    if "--split" in args: split = args[args.index("--split") + 1]
    if "--only" in args: only = set(args[args.index("--only") + 1:])
    out_dir = os.environ.get("MAT_INTENT_CONTRACT_DIR", os.path.join(ROOT, "runs"))
    if "--out-dir" in args: out_dir = args[args.index("--out-dir") + 1]
    path = os.path.join(ROOT, "datasets", "contract_regression", "scenarios.jsonl")
    scens = [json.loads(l) for l in open(path) if l.strip()]
    os.makedirs(os.path.join(out_dir, "checkpoints"), exist_ok=True)
    results = []
    for scen in scens:
        if only and scen["id"] not in only: continue
        if not only and split != "all" and scen["split"] != split: continue
        clock = Clock(datetime.fromisoformat(scen.get("clock", "2026-10-24T09:00:00+07:00")))
        facts = Facts(facts=list(SEED_FACTS) + scen.get("facts_extra", []))
        eng = make_engine(clock=clock, facts=facts, kill_switch=scen.get("kill_switch", False))
        traces = []
        for turn in scen["turns"]:
            if turn.get("advance_hours"): clock.advance(hours=turn["advance_hours"])
            sem = resolve(turn.get("semantic"), eng, turn["event"])
            res = eng.process(turn["event"], scen["postcard"], sem)
            traces.append(dict(turn=turn["event"]["comment_id"], result={
                k: v for k, v in res.items() if k != "audit"}))
        with eng.store.lock:
            reqs = eng.store.conn.execute("SELECT id, topic, status, version, slots, actor_id FROM requests").fetchall()
        pend = eng.store.conn.execute("SELECT id, request_id, status, proposition FROM pending").fetchall()
        out = dict(scenario_id=scen["id"], traces=traces,
                   final_requests=[dict(id=r[0], topic=r[1], status=r[2], version=r[3],
                                        slots=json.loads(r[4]), actor_id=r[5]) for r in reqs],
                   final_pending=[dict(id=q[0], request_id=q[1], status=q[2], proposition=q[3]) for q in pend],
                   ledger=eng.store.ledger())
        fails = check(scen, out)
        rec = dict(id=scen["id"], family=scen["family"], split=scen["split"],
                   group=scen["group"], turns=len(scen["turns"]),
                   actors=len({t["event"]["actor_id"] for t in scen["turns"]}),
                   passed=not fails, fails=fails)
        results.append(rec)
        with open(os.path.join(out_dir, "checkpoints", scen["id"] + ".json"), "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    passed = sum(1 for r in results if r["passed"])
    by_split = {}
    for r in results:
        by_split.setdefault(r["split"], [0, 0]); by_split[r["split"]][1] += 1
        by_split[r["split"]][0] += 1 if r["passed"] else 0
    multi = sum(1 for r in results if r["turns"] > 1)
    multiu = sum(1 for r in results if r["actors"] > 1)
    err = sum(1 for r in results if r["id"].startswith("err_"))
    summary = dict(total=len(results), passed=passed, failed=len(results) - passed,
                   by_split=by_split, multi_turn_scenarios=multi,
                   multi_user_scenarios=multiu, error_race_scenarios=err,
                   failures=[r for r in results if not r["passed"]])
    with open(os.path.join(out_dir, "contract_run.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"contract: {passed}/{len(results)} pass  by_split={by_split} "
          f"multi_turn={multi} multi_user={multiu} error_race={err}")
    for r in summary["failures"][:40]:
        print(f"FAIL {r['id']} ({r['family']}/{r['split']}): {r['fails']}")
    return 0 if passed == len(results) else 1

if __name__ == "__main__":
    raise SystemExit(main())
