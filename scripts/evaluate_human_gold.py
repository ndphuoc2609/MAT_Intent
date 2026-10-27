"""Split human labels without actor/thread leakage and score engine predictions.

Rows use: id, thread_id, actor_id, timestamp, labels[], slots{}, action,
risk_group, auto_processed. Gold rows must say label_source='human'; generated
or contract fixtures are deliberately refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone


def read_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _components(rows):
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x: parent[x] = find(parent[x])
        return parent[x]
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra
    for row in rows:
        tid, aid = row.get("thread_id"), row.get("actor_id")
        if not isinstance(tid, str) or not tid or not isinstance(aid, str) or not aid:
            raise ValueError("every gold row needs thread_id and actor_id")
        union("thread:" + tid, "actor:" + aid)
    groups = {}
    for row in rows:
        groups.setdefault(find("thread:" + row["thread_id"]), []).append(row)
    for group in groups.values():
        for row in group:
            if not isinstance(row.get("timestamp"), str):
                raise ValueError("every gold row needs an ISO timestamp")
            parsed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("gold timestamps must include a timezone")
    return list(groups.values())


def split_human_gold(rows, fractions=(0.7, 0.15, 0.15)):
    return split_human_gold_with_report(rows, fractions)[0]


def split_human_gold_with_report(rows, fractions=(0.7, 0.15, 0.15)):
    if any(row.get("label_source") != "human" for row in rows):
        raise ValueError("gold input must contain only label_source='human' rows")
    if not math.isclose(sum(fractions), 1.0) or any(x <= 0 for x in fractions):
        raise ValueError("split fractions must be positive and sum to 1")
    parse_time = lambda row: datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
    ordered = sorted(rows, key=parse_time)
    if not ordered:
        return ({"train": [], "validation": [], "test": []},
                {"time_cuts": {"train_end": None, "validation_end": None}, "boundary_groups": []})
    train_end = parse_time(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fractions[0]) - 1)])
    validation_end = parse_time(ordered[min(len(ordered) - 1,
        math.ceil(len(ordered) * (fractions[0] + fractions[1])) - 1)])
    groups = _components(rows)
    result = {"train": [], "validation": [], "test": []}
    boundary_groups = []
    for group in groups:
        times = [parse_time(row) for row in group]
        earliest, latest = min(times), max(times)
        fold = "train" if latest <= train_end else "validation" if latest <= validation_end else "test"
        result[fold].extend(group)
        crosses = []
        if earliest <= train_end < latest: crosses.append("train_validation")
        if earliest <= validation_end < latest: crosses.append("validation_test")
        if crosses:
            identity = sorted({"thread:" + row["thread_id"] for row in group} |
                              {"actor:" + row["actor_id"] for row in group})
            group_id = hashlib.sha256("\n".join(identity).encode()).hexdigest()[:12]
            boundary_groups.append({"group_id": group_id, "size": len(group),
                                    "first_at": earliest.isoformat(), "last_at": latest.isoformat(),
                                    "crosses": crosses, "assigned_to": fold})
    report = {"time_cuts": {"train_end": train_end.isoformat(),
                            "validation_end": validation_end.isoformat()},
              "boundary_groups": boundary_groups}
    return result, report


def _labels(row):
    return set(row.get("labels") or [])


def _slots(row):
    slots = row.get("slots") or {}
    return {(kind, json.dumps(value, ensure_ascii=False, sort_keys=True)) for kind, value in slots.items()}


def _percentile(values, p):
    values = sorted(values)
    if not values: return None
    return values[min(len(values) - 1, math.ceil(p * len(values)) - 1)]


def _critical_error(gold, prediction):
    if (prediction is None or not isinstance(gold.get("action"), str) or
            not isinstance(prediction.get("action"), str) or not prediction["action"]):
        return None
    expected, actual = gold["action"].upper(), str(prediction.get("action", "")).upper()
    unsafe = actual in {"AUTO", "SEND", "AUTO_REPLY", "CONFIRM", "CANCEL", "COMMIT"}
    sends = prediction.get("sends")
    unsafe |= prediction.get("outbound_permission") is True or type(sends) is int and sends > 0
    if expected in {"REVIEW", "HANDOFF", "WAIT", "STOP"}:
        return unsafe
    if gold.get("risk_group") == "high" and expected not in {"AUTO", "SEND", "RESPOND"}:
        return unsafe
    return False


def score(gold_rows, prediction_rows):
    if any(row.get("label_source") != "human" for row in gold_rows):
        raise ValueError("scoring requires human-gold labels; synthetic fixtures are not accepted")
    predictions = {row["id"]: row for row in prediction_rows}
    gold = {row["id"]: row for row in gold_rows}
    if len(gold) != len(gold_rows) or len(predictions) != len(prediction_rows):
        raise ValueError("duplicate row id")
    unknown = predictions.keys() - gold.keys()
    if unknown: raise ValueError("predictions include IDs absent from gold")

    def metrics(rows):
        tp = {}; fp = {}; fn = {}; slot_tp = slot_fp = slot_fn = exact_slots = 0
        action_total = action_ok = covered = critical = critical_assessed = critical_unknown = 0
        latencies, costs = [], []
        for row in rows:
            pred = predictions.get(row["id"])
            pred = pred or {}
            expected, got = _labels(row), _labels(pred)
            for label in expected | got:
                tp[label] = tp.get(label, 0) + int(label in expected and label in got)
                fp[label] = fp.get(label, 0) + int(label not in expected and label in got)
                fn[label] = fn.get(label, 0) + int(label in expected and label not in got)
            expected_slots, got_slots = _slots(row), _slots(pred)
            slot_tp += len(expected_slots & got_slots)
            slot_fp += len(got_slots - expected_slots)
            slot_fn += len(expected_slots - got_slots)
            exact_slots += int(expected_slots == got_slots)
            if row.get("action") is not None:
                action_total += 1
                action_ok += int(pred.get("action") == row.get("action"))
            covered += int(pred.get("covered", pred.get("action") is not None))
            critical_result = _critical_error(row, pred if predictions.get(row["id"]) is not None else None)
            if critical_result is None:
                critical_unknown += 1
            else:
                critical_assessed += 1
                critical += int(critical_result)
            latency = pred.get("pipeline_latency_ms", pred.get("latency_ms"))
            if type(latency) in (int, float): latencies.append(float(latency))
            if type(pred.get("cost_usd")) in (int, float): costs.append(float(pred["cost_usd"]))
        labels = sorted(tp.keys() | fp.keys() | fn.keys())
        f1s = []
        recalls = {}
        for label in labels:
            denom = 2 * tp.get(label, 0) + fp.get(label, 0) + fn.get(label, 0)
            f1s.append(2 * tp.get(label, 0) / denom if denom else 0.0)
            support = tp.get(label, 0) + fn.get(label, 0)
            recalls[label] = tp.get(label, 0) / support if support else None
        precision = slot_tp / (slot_tp + slot_fp) if slot_tp + slot_fp else 0.0
        recall = slot_tp / (slot_tp + slot_fn) if slot_tp + slot_fn else 0.0
        return {"n": len(rows), "covered": covered, "coverage": covered / len(rows) if rows else None,
                "intent_macro_f1": sum(f1s) / len(f1s) if f1s else None,
                "intent_recall": recalls, "slot_accuracy": exact_slots / len(rows) if rows else None,
                "slot_precision": precision, "slot_recall": recall,
                "slot_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                "action_correctness": action_ok / action_total if action_total else None,
                "critical_errors": critical if critical_assessed else None,
                "critical_error_assessed": critical_assessed, "critical_error_unknown": critical_unknown,
                "critical_error_rate": critical / critical_assessed if critical_assessed else None,
                "latency_p50_ms": _percentile(latencies, .50), "latency_p95_ms": _percentile(latencies, .95),
                "cost_requests_known": len(costs), "cost_unknown_requests": len(rows) - len(costs),
                "cost_per_request_usd": sum(costs) / len(costs) if costs else None}

    report = {"all": metrics(gold_rows),
              "high_risk": metrics([r for r in gold_rows if r.get("risk_group") == "high"]),
              "auto_processed": metrics([r for r in gold_rows
                                          if predictions.get(r["id"], {}).get("auto_processed") is True]),
              "missing_predictions": len(gold_rows) - len(gold.keys() & predictions.keys())}
    return report


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    split = commands.add_parser("split")
    split.add_argument("--gold", required=True)
    split.add_argument("--out-dir", required=True)
    evaluate = commands.add_parser("score")
    evaluate.add_argument("--gold", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--out")
    args = parser.parse_args()
    gold_rows = read_jsonl(args.gold)
    if args.command == "split":
        folds, split_report = split_human_gold_with_report(gold_rows)
        os.makedirs(args.out_dir, exist_ok=True)
        for name, rows in folds.items():
            with open(os.path.join(args.out_dir, f"{name}.jsonl"), "w", encoding="utf-8") as stream:
                for row in rows: stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"counts": {name: len(rows) for name, rows in folds.items()},
                          **split_report}, ensure_ascii=False))
    else:
        report = score(gold_rows, read_jsonl(args.predictions))
        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as stream: stream.write(output + "\n")
        print(output)


if __name__ == "__main__":
    main()
