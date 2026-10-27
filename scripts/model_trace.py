"""Append privacy-minimal model/pipeline timings for offline evaluation."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone


def append(path, trace):
    allowed = {"event_hash", "model_requested", "model_effective", "provider", "adapter",
               "schema_version", "prompt_version", "routing", "review_reasons",
               "latency_ms", "pipeline_latency_ms", "usage", "cost_usd", "cost_unknown",
               "attempts", "shadow", "question_version", "question_keys", "decisions"}
    row = {k: trace[k] for k in allowed if k in trace}
    row["ts"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
