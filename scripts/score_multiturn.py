"""Chấm hội thoại nhiều lượt: per-turn + per-conversation, tách biệt scorer
với runner (runner không đọc expected). Counts chỉ là counts, không thay
độ đúng nội dung. HANDOFF/IGNORE không tính là thành công chung chung:
một turn chỉ pass khi mọi check expected của nó pass.
Usage: python3 scripts/score_multiturn.py runs/mt_dev_v2.json datasets/multiturn/dev.jsonl
"""
import json, sys

def slot_val(slot):
    if isinstance(slot, dict): return slot.get("value")
    return slot

def latest_req(requests, topic, actor):
    cands = [r for r in requests if r["topic"] == topic and r["actor_id"] == actor]
    return cands[-1] if cands else None

def check_turn(turn_out, exp, comment_ids):
    fails = []
    if exp is None: return fails
    actor = turn_out["actor"]
    for er in exp.get("requests", []):
        ractor = er.get("actor", actor)
        r = latest_req(turn_out["requests"], er["topic"], ractor)
        if r is None:
            fails.append(f"missing request {er['topic']} for {ractor}"); continue
        if "status" in er and r["status"] != er["status"]:
            fails.append(f"{er['topic']} status {r['status']} != {er['status']}")
        if "status_in" in er and r["status"] not in er["status_in"]:
            fails.append(f"{er['topic']} status {r['status']} not in {er['status_in']}")
        for k, v in (er.get("slots") or {}).items():
            got = slot_val((r["slots"] or {}).get(k))
            if got != v:
                fails.append(f"{er['topic']} slot {k}={got!r} != {v!r}")
    if "sends" in exp and turn_out["sends_n"] != exp["sends"]:
        fails.append(f"sends {turn_out['sends_n']} != {exp['sends']}")
    for rc in exp.get("routing_contains", []):
        if rc not in (turn_out["routing"] or []):
            fails.append(f"routing missing {rc} (got {turn_out['routing']})")
    rt = turn_out["response_text"] or ""
    for c in exp.get("response_contains", []):
        if c not in rt: fails.append(f"response missing {c!r}")
    for c in exp.get("response_forbidden", []):
        if c in rt: fails.append(f"response contains forbidden {c!r}")
    if exp.get("has_pending"):
        if not any(p["status"] == "OPEN" and p["actor_id"] == actor for p in turn_out["pendings"]):
            fails.append("expected open pending, none")
    if "prefs" in exp:
        for scope, want in exp["prefs"].items():
            if turn_out["prefs"] != want:
                fails.append(f"prefs {turn_out['prefs']!r} != {want!r}")
    for pur in exp.get("ledger_purposes", []):
        if pur not in turn_out["ledger_purposes_added"]:
            fails.append(f"ledger missing purpose {pur} (got {turn_out['ledger_purposes_added']})")
    return fails

def main():
    run = json.load(open(sys.argv[1]))
    convos = {json.loads(l)["id"]: json.loads(l) for l in open(sys.argv[2]) if l.strip()}
    total_turns = passed_turns = 0
    link_ok = link_total = 0
    auto_turns = auto_pass = 0
    dup = 0
    prov_bad = []
    for conv in run["conversations"]:
        src = convos[conv["id"]]
        comment_ids = {f"{conv['id']}_t{i}" for i in range(len(src["turns"]))}
        c_turns = c_pass = 0
        print(f"\n== {conv['id']} ({conv['group']}) ==")
        for to, st in zip(conv["turns"], src["turns"]):
            exp = st.get("expected") or {}
            fails = check_turn(to, exp, comment_ids)
            # Pending linkage: op tham chiếu pending phải là pending đang mở trước lượt.
            for op in (to.get("semantic") or {}).get("operations", []) or []:
                if op.get("op") in ("ANSWER_PENDING", "CONFIRM_PENDING", "REJECT_PENDING"):
                    link_total += 1
                    if op.get("pending_id") in (to["open_pendings_before"] or []): link_ok += 1
                    else: fails.append(f"pending link bad: {op.get('op')} -> {op.get('pending_id')} (open before: {to['open_pendings_before']})")
            if to.get("validation_errors"): fails.append(f"validation: {to['validation_errors']}")
            if to["routing"] and "SKIPPED" in to["routing"][0]: fails.append(f"skipped: {to['routing']}")
            total_turns += 1; c_turns += 1
            if not fails:
                passed_turns += 1; c_pass += 1
            else:
                print(f"  turn {to['turn']} [{to['actor']}] {st['text']!r} FAIL:")
                for f in fails: print(f"    - {f}")
            if exp.get("sends", 0) >= 1:
                auto_turns += 1
                if not fails: auto_pass += 1
            if "sends" in exp and to["sends_n"] > exp["sends"]:
                dup += 1; print(f"  turn {to['turn']} DUPLICATE sends {to['sends_n']} > {exp['sends']}")
            # Provenance: slot nào cũng phải từ một comment trong hội thoại.
            for r in to["requests"]:
                for k, v in (r["slots"] or {}).items():
                    if isinstance(v, dict) and v.get("source_message_id") not in comment_ids:
                        prov_bad.append((conv["id"], to["turn"], r["topic"], k, v.get("source_message_id")))
        # Final-state check của conversation.
        final_exp = src.get("final") or {}
        last = conv["turns"][-1] if conv["turns"] else None
        if last:
            for er in final_exp.get("requests", []):
                ractor = er.get("actor", None)
                if ractor:
                    r = latest_req(last["requests"], er["topic"], ractor)
                else:
                    cands = [x for x in last["requests"] if x["topic"] == er["topic"]]
                    r = cands[-1] if cands else None
                ok = r is not None and (("status" not in er) or r["status"] == er["status"])
                if not ok:
                    print(f"  FINAL FAIL: {er} (got {r})")
        print(f"  turns pass: {c_pass}/{c_turns}")
    print(f"\nTOTAL turns: {passed_turns}/{total_turns} | auto turns pass: {auto_pass}/{auto_turns} | "
          f"pending link: {link_ok}/{link_total} | duplicate-effect turns: {dup} | "
          f"provenance violations: {len(prov_bad)} {prov_bad[:5]}")
    print(f"calls: new={run['new_calls']} cumulative={run['cumulative_calls']} "
          f"cost=${run['new_cost_usd']:.5f} cumulative=${run['cumulative_cost_usd']:.4f} budget_hit={run['budget_hit']}")

if __name__ == "__main__":
    main()
