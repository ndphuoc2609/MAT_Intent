"""Replay cached Luna extraction qua Engine hiện tại (0 model call).
Dùng khi sửa engine/policy/scoring sau một batch API — đúng phương pháp §12:
không chạy lại model cho mọi case sau mỗi sửa code.
Usage: python3 scripts/replay_api.py runs/api_dev_b3.json runs/api_dev_b3_replay.json
"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from run_api_eval import route_of
from app.harness import make_engine
from app.facts import Facts
from app.engine import Clock
from datetime import datetime, timezone

def main():
    src, dst = sys.argv[1], sys.argv[2]
    d = json.load(open(os.path.join(ROOT, src)))
    cases = {c["id"]: c for c in map(json.loads, open(os.path.join(ROOT, "datasets", "development", "api_cases.jsonl")))}
    out = []
    for r in d["results"]:
        if "semantic" not in r: continue
        c = cases.get(r["id"])
        if not c: continue
        eng = make_engine(clock=Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)), facts=Facts())
        ev = dict(page_id="page_hyundai_sample", post_id="post_1", thread_root_id="root",
                  comment_id="api_" + r["id"], parent_id="root", actor_id="u_api",
                  actor_role="user", event_type="create", content_version=1,
                  text=c["comment"], event_dedupe_key="api_" + r["id"] + ":1")
        res = eng.process(ev, c["postcard"], r["semantic"])
        nr = dict(r); nr["routing"] = route_of(res); nr["response_text"] = res.get("response_text")
        nr["engine_routing"] = res["routing"]; out.append(nr)
    EXCLUDED = set(d.get("excluded_cases", []))
    scored = [r for r in out if r["id"] not in EXCLUDED]
    ok = [r for r in scored if r.get("ok") and not r.get("validation_errors")]
    summary = dict(cases=len(scored), api_ok=len(ok),
                   topic_exact=sum(1 for r in ok if r["topic_exact"]) / len(ok) if ok else 0,
                   topic_recall_mean=sum(r["topic_recall"] for r in scored) / len(scored) if scored else 0,
                   routing_match=sum(1 for r in ok if r["routing"] == r["expected_routing"]) / len(ok) if ok else 0,
                   model_calls=0, replay_of=src, results=out)
    json.dump(summary, open(os.path.join(ROOT, dst), "w"), ensure_ascii=False, indent=2)
    print(f"replay: n={len(scored)} topic_exact={summary['topic_exact']:.3f} "
          f"recall={summary['topic_recall_mean']:.3f} routing={summary['routing_match']:.3f} (0 calls)")
    for r in scored:
        if not r["topic_exact"] or r["routing"] != r["expected_routing"]:
            print(f"  {r['id']} {r['expected_routing']}->{r['routing']} {r['expected_topics']}->{r['pred_topics']}")

if __name__ == "__main__":
    main()
