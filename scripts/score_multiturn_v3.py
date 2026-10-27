"""Chấm multiturn bằng scorer v3 (R12).
Usage: python3 scripts/score_multiturn_v3.py runs/mt_dev_v3.json datasets/multiturn/dev.jsonl [--adjudicate adj.json] [--out out.json]
Exit: 0 = tất cả pass (turn + final + availability); 1 = có fail; 2 = structural
(số lượt lệch, conv thiếu, run/dataset không khớp).
Adjudication (nếu có): {"conv_id:turn": {"override": "pass"|"fail", "reason": "..."}}.
Không có file thì adjusted == raw, post-hoc rỗng — không tự bịa adjudication.
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.scoring_v3 import check_turn, match_req

def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: score_multiturn_v3.py <run.json> <dataset.jsonl> [--adjudicate f] [--out f]")
        return 2
    run_path, ds_path = args[0], args[1]
    adj, out_path = {}, None
    i = 2
    while i < len(args):
        if args[i] == "--adjudicate": adj = json.load(open(args[i + 1])); i += 2
        elif args[i] == "--out": out_path = args[i + 1]; i += 2
        else: i += 1
    run = json.load(open(run_path))
    convos = {}
    for line in open(ds_path):
        if line.strip():
            d = json.loads(line); convos[d["id"]] = d
    result = {"run": run_path, "dataset": ds_path, "conversations": [],
              "structural_failures": [], "post_hoc": []}
    total = passed = 0
    send_actual = send_actual_ok = send_exp = send_exp_ok = 0
    avail_total = avail_ok = 0
    final_total = final_ok = 0
    structural = False
    for conv in run.get("conversations", []):
        cid = conv["id"]
        src = convos.get(cid)
        entry = {"id": cid, "group": conv.get("group"), "turns": [], "final": None,
                 "verdict": "PASS"}
        if src is None:
            entry["verdict"] = "STRUCTURAL_FAIL"
            result["structural_failures"].append(f"conv {cid} not in dataset")
            structural = True; result["conversations"].append(entry); continue
        run_turns, ds_turns = conv.get("turns", []), src.get("turns", [])
        if len(run_turns) != len(ds_turns):
            entry["verdict"] = "STRUCTURAL_FAIL"
            result["structural_failures"].append(
                f"conv {cid}: run turns {len(run_turns)} != dataset turns {len(ds_turns)}")
            structural = True; result["conversations"].append(entry); continue
        comment_ids = {t.get("comment_id") for t in run_turns if t.get("comment_id")}
        prev_requests = []
        for to, st in zip(run_turns, ds_turns):
            exp = st.get("expected") or {}
            fails = check_turn(to, exp, comment_ids, prev_requests)
            key = f"{cid}:{to.get('turn')}"
            raw_pass = not fails
            adj_entry = None
            if key in adj:
                adj_entry = adj[key]
                result["post_hoc"].append({"turn": key, **adj_entry, "raw_fails": fails})
                raw_pass = (adj_entry.get("override") == "pass")
            total += 1
            avail_total += 1
            if not (to.get("validation_errors") or to.get("errors") or to.get("semantic") is None):
                avail_ok += 1
            if raw_pass: passed += 1
            else: entry["verdict"] = "FAIL"
            if to.get("sends_n", 0) > 0:
                send_actual += 1
                if "sends" in exp and to.get("sends_n") == exp["sends"]: send_actual_ok += 1
            if exp.get("sends", 0) > 0:
                send_exp += 1
                if to.get("sends_n") == exp["sends"]: send_exp_ok += 1
            entry["turns"].append({"turn": to.get("turn"), "pass": raw_pass,
                                  "fails": fails, "adjudicated": adj_entry})
            prev_requests = to.get("requests", [])
        # Final-state (ảnh hưởng verdict).
        last = run_turns[-1] if run_turns else None
        final_exp = src.get("final") or {}
        final_fails = []
        if last:
            for er in final_exp.get("requests", []):
                r, err = match_req(last.get("requests", []), er, None)
                if err: final_fails.append(err)
                elif "status" in er and r["status"] != er["status"]:
                    final_fails.append(f"final {er['topic']} status {r['status']} != {er['status']}")
        final_total += 1
        if final_fails:
            entry["verdict"] = "FAIL"
        else:
            final_ok += 1
        entry["final"] = {"pass": not final_fails, "fails": final_fails}
        result["conversations"].append(entry)
    for cid in convos:
        if cid not in {c["id"] for c in run.get("conversations", [])}:
            result["structural_failures"].append(f"dataset conv {cid} missing from run")
            structural = True
    result["summary"] = {
        "turns": f"{passed}/{total}",
        "send_precision": f"{send_actual_ok}/{send_actual}" if send_actual else "n/a",
        "send_recall": f"{send_exp_ok}/{send_exp}" if send_exp else "n/a",
        "final_state": f"{final_ok}/{final_total}",
        "availability": f"{avail_ok}/{avail_total}",
        "structural_failures": len(result["structural_failures"]),
        "post_hoc_overrides": len(result["post_hoc"]),
    }
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    for c in result["conversations"]:
        print(f"{c['verdict']} {c['id']} final={'PASS' if (c['final'] or {}).get('pass') else 'FAIL'}")
        for t in c["turns"]:
            if not t["pass"]:
                print(f"  turn {t['turn']} FAIL: {t['fails']}")
        if c["final"] and not c["final"]["pass"]:
            print(f"  FINAL FAIL: {c['final']['fails']}")
    if result["structural_failures"]:
        print("STRUCTURAL:", result["structural_failures"])
    if out_path:
        json.dump(result, open(out_path, "w"), ensure_ascii=False, indent=2)
    if structural:
        return 2
    failed_convs = [c for c in result["conversations"] if c["verdict"] != "PASS"]
    return 0 if (passed == total and not failed_convs) else 1

if __name__ == "__main__":
    raise SystemExit(main())
