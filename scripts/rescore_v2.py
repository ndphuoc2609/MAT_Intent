"""Chấm lại run cũ bằng scorer v2 (cùng scorer, cùng dataset version).
Usage: python3 scripts/rescore_v2.py runs/api_dev_b3.json runs/api_dev_b3_replay2.json"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoring import rescore

for path in sys.argv[1:]:
    d = json.load(open(path))
    excluded = d.get("excluded_cases")
    if excluded is None and d.get("replay_of"):
        # Replay không copy excluded_cases: lấy từ run gốc nó replay.
        src = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), d["replay_of"])))
        excluded = src.get("excluded_cases", [])
    out = rescore(d["results"], excluded or [])
    print(path, json.dumps(out, ensure_ascii=False))
