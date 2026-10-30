#!/usr/bin/env python3
"""Validate transcript inputs and produce blinded, evidence-limited A/B packs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import re
import secrets
import statistics
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "datasets" / "persona_conversations" / "frozen12.json"
COLLECTION_STATUS = ROOT / "datasets" / "persona_conversations" / "collection_status.json"
CRITERIA = ("naturalness", "intent_adherence", "continuity", "persona_consistency", "question_timing")
FLAGS = ("unsupported_claim", "wrong_actor_or_privacy", "transaction_error")
_DIRECT_IDENTIFIER = re.compile(
    r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://\S+|"
    r"(?<!\d)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\d))",
    re.IGNORECASE,
)


def load_dataset(path=DEFAULT_DATASET):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_dataset(dataset):
    if not isinstance(dataset, dict) or dataset.get("schema_version") != "persona_conversation_eval_v1":
        raise ValueError("unsupported persona conversation dataset schema")
    if dataset.get("status") != "synthetic_regression_only":
        raise ValueError("frozen regression set must remain labeled synthetic_regression_only")
    if dataset.get("human_evaluation_status") != "not_run":
        raise ValueError("synthetic fixtures cannot carry a human evaluation result")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise ValueError("frozen synthetic regression set must contain exactly 12 conversations")
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or case.get("origin") != "synthetic_fixture" or
                "SYNTHETIC" not in case.get("disclosure", "")):
            raise ValueError("every frozen conversation must be explicitly labeled synthetic")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("conversation case IDs must be nonempty and unique")
        ids.add(case_id)
        turns = case.get("turns")
        if not isinstance(turns, list) or not 4 <= len(turns) <= 8:
            raise ValueError(f"{case_id} must contain 4–8 turns")
        turn_ids = set()
        for turn in turns:
            if (not isinstance(turn, dict) or not isinstance(turn.get("customer_text"), str) or
                    not turn["customer_text"].strip()):
                raise ValueError(f"{case_id} contains an empty customer turn")
            if turn.get("turn_id") in turn_ids:
                raise ValueError(f"{case_id} has duplicate turn IDs")
            turn_ids.add(turn.get("turn_id"))
        if "human_labels" in case:
            raise ValueError("frozen fixtures must not contain human scoring labels")
    target = dataset.get("collection_target", {})
    if (target.get("real_conversations_min") != 30 or target.get("real_conversations_max") != 50 or
            target.get("real_conversations_collected") != 0 or target.get("status") != "missing"):
        raise ValueError("real conversation target must remain visibly uncollected")
    return len(cases)


def sanitize_real_conversation(raw):
    """Validate a consented, manually redacted transcript and omit private refs."""
    if not isinstance(raw, dict):
        raise ValueError("real transcript must be an object")
    provenance = raw.get("provenance")
    if (not isinstance(provenance, dict) or not _nonempty(provenance.get("source_ref")) or
            not _nonempty(provenance.get("captured_at")) or not _nonempty(provenance.get("collector_id"))):
        raise ValueError("real transcript provenance requires source_ref, captured_at and collector_id")
    try:
        captured_at = datetime.fromisoformat(provenance["captured_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("real transcript provenance captured_at must be ISO datetime") from None

    consent = raw.get("consent")
    if (not isinstance(consent, dict) or consent.get("status") != "granted" or
            consent.get("scope") != "persona_evaluation" or not _nonempty(consent.get("consent_ref"))):
        raise ValueError("real transcript requires granted persona_evaluation consent")

    redaction = raw.get("redaction")
    if (not isinstance(redaction, dict) or redaction.get("status") != "complete" or
            not _nonempty(redaction.get("reviewer_id")) or
            not _nonempty(redaction.get("reviewed_at")) or
            redaction.get("direct_identifiers_removed") is not True or
            redaction.get("private_details_removed") is not True):
        raise ValueError("real transcript requires completed privacy redaction review")
    try:
        datetime.fromisoformat(redaction["reviewed_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("redaction reviewed_at must be ISO datetime") from None

    turns = raw.get("turns")
    if not isinstance(turns, list) or not 4 <= len(turns) <= 8:
        raise ValueError("real transcript must contain 4–8 redacted turns")
    sanitized_turns = []
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            raise ValueError("real transcript turn must be an object")
        customer = turn.get("customer_text")
        reply = turn.get("reference_reply")
        if not _nonempty(customer) or not _nonempty(reply):
            raise ValueError("real transcript turns require redacted customer_text and reference_reply")
        if _DIRECT_IDENTIFIER.search(customer) or _DIRECT_IDENTIFIER.search(reply):
            raise ValueError(f"real transcript turn {index} failed the direct-identifier redaction scan")
        sanitized_turns.append({"turn_id": f"t{index}", "customer_text": customer.strip()})

    return {
        "case_id": "real-" + uuid.uuid4().hex[:12],
        "origin": "real_redacted",
        "disclosure": "CONSENTED REDACTED REAL",
        "scenario": "Redacted real conversation for persona evaluation.",
        "provenance": {
            "source_ref_sha256": _sha256(provenance["source_ref"]),
            "captured_month": captured_at.strftime("%Y-%m"),
        },
        "consent": {
            "status": "granted", "scope": "persona_evaluation",
            "consent_ref_sha256": _sha256(consent["consent_ref"]),
        },
        "privacy": {"status": "redacted_reviewed", "direct_identifiers_removed": True,
                    "private_details_removed": True},
        "turns": sanitized_turns,
    }


def build_blind_export(dataset, baseline, candidate, *, seed=0, include_real=False):
    """Build evaluator view, private unblinding key and blank label CSV."""
    if not isinstance(seed, int):
        raise ValueError("random seed must be an integer")
    synthetic = dict(dataset, cases=[c for c in dataset.get("cases", [])
                                     if isinstance(c, dict) and c.get("origin") == "synthetic_fixture"])
    validate_dataset(synthetic)
    real_cases = [c for c in dataset.get("cases", [])
                  if isinstance(c, dict) and c.get("origin") == "real_redacted"]
    if real_cases and not include_real:
        raise ValueError("real transcripts require the explicit include_real option")
    if any(not _is_sanitized_real(case) for case in real_cases):
        raise ValueError("real transcript lacks required redaction, consent, or provenance metadata")
    cases = list(synthetic["cases"]) + (real_cases if include_real else [])
    if not cases:
        raise ValueError("no conversations selected for evaluation")
    baseline_results = _index_run(baseline, cases)
    candidate_results = _index_run(candidate, cases)

    export = {"schema_version": "persona_blind_ab_v1", "dataset_id": dataset.get("dataset_id"),
              "blinded": True, "cases": []}
    key = {"schema_version": "persona_blind_key_v1", "seed": seed,
           "comparison": {"baseline_run_id": baseline["run_id"],
                          "candidate_run_id": candidate["run_id"]},
           "items": {}, "variants": {
               baseline["run_id"]: _run_metadata(baseline, baseline_results),
               candidate["run_id"]: _run_metadata(candidate, candidate_results),
           }}
    rng = random.Random(seed)
    for index, case in enumerate(cases, 1):
        blind_id = f"item-{index:03d}"
        option_run = [baseline["run_id"], candidate["run_id"]]
        rng.shuffle(option_run)
        options = dict(zip(("A", "B"), option_run))
        key["items"][blind_id] = options
        baseline_turns = baseline_results[case["case_id"]]
        candidate_turns = candidate_results[case["case_id"]]
        option_data = {
            baseline["run_id"]: baseline_turns,
            candidate["run_id"]: candidate_turns,
        }
        blind_case = {
            "blind_item_id": blind_id,
            "disclosure": "SYNTHETIC FIXTURE" if case["origin"] == "synthetic_fixture" else "CONSENTED REDACTED REAL",
            "scenario": case.get("scenario") if case["origin"] == "synthetic_fixture" else "Redacted real conversation.",
            "turns": [],
        }
        for turn in case["turns"]:
            pair_turn = {"turn_id": turn["turn_id"], "customer_text": turn["customer_text"]}
            if case["origin"] == "synthetic_fixture":
                pair_turn["expected_behavior"] = turn["expected_behavior"]
                pair_turn["must_not_claim"] = turn["must_not_claim"]
            for option, run_id in options.items():
                pair_turn[option] = {"response": option_data[run_id][turn["turn_id"]]["response"]}
            blind_case["turns"].append(pair_turn)
        export["cases"].append(blind_case)
    return export, key, labels_csv(export)


def labels_csv(export):
    columns = ["blind_item_id", "labeler_slot", "labeler_id", "independence_attested"]
    for option in ("A", "B"):
        columns.extend(f"{option}_{criterion}" for criterion in CRITERIA)
        columns.extend(f"{option}_{flag}" for flag in FLAGS)
    rows = []
    for case in export.get("cases", []):
        for slot in ("R1", "R2"):
            rows.append({"blind_item_id": case["blind_item_id"], "labeler_slot": slot, "labeler_id": "",
                         "independence_attested": ""})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def aggregate_scores(export, key, labels):
    case_ids = {case["blind_item_id"] for case in export.get("cases", [])}
    if case_ids != set(key.get("items", {})):
        raise ValueError("blind export and unblinding key do not match")
    grouped = {case_id: [] for case_id in case_ids}
    for row in labels:
        item_id = row.get("blind_item_id")
        if item_id not in grouped:
            raise ValueError("label references an unknown blind item")
        grouped[item_id].append(row)
    complete = True
    missing = []
    label_ids_by_item = {}
    for item_id, rows in grouped.items():
        ids = [row.get("labeler_id", "").strip() for row in rows]
        independent = all(_truthy(row.get("independence_attested")) for row in rows)
        if len(rows) != 2 or any(not labeler for labeler in ids) or len(set(ids)) != 2 or not independent:
            complete = False
            missing.append(item_id)
        label_ids_by_item[item_id] = len(set(labeler for labeler in ids if labeler))
        for row in rows:
            for option in ("A", "B"):
                for criterion in CRITERIA:
                    value = row.get(f"{option}_{criterion}", "")
                    if value == "":
                        complete = False
                        if item_id not in missing:
                            missing.append(item_id)
                        continue
                    try:
                        score = int(value)
                    except (TypeError, ValueError):
                        raise ValueError("human scores must be integers from 1 to 5") from None
                    if score < 1 or score > 5:
                        raise ValueError("human scores must be integers from 1 to 5")
                for flag in FLAGS:
                    value = row.get(f"{option}_{flag}", "")
                    if value != "" and not isinstance(value, bool) and str(value).lower() not in {"true", "false", "1", "0"}:
                        raise ValueError("human issue flags must be true or false")

    result = {
        "schema_version": "persona_evaluation_summary_v1",
        "human_evaluation": {
            "status": "complete" if complete else "incomplete",
            "distinct_labelers_per_item": 2,
            "independence_attestation_required": True,
            "independence_identity_verified": False,
            "labeled_items": len(case_ids) - len(missing), "expected_items": len(case_ids),
            "missing_items": sorted(missing), "labeler_count": max(label_ids_by_item.values(), default=0),
            "criteria": None, "candidate_naturalness_better_pairs": None,
            "candidate_naturalness_tied_pairs": None, "candidate_naturalness_worse_pairs": None,
            "critical_flags": None,
        },
        "variants": {run_id: dict(metadata) for run_id, metadata in key.get("variants", {}).items()},
    }
    if not complete:
        return result

    baseline_id = key["comparison"]["baseline_run_id"]
    candidate_id = key["comparison"]["candidate_run_id"]
    per_item = []
    flag_counts = {run_id: {flag: 0 for flag in FLAGS} for run_id in (baseline_id, candidate_id)}
    for case_id, rows in grouped.items():
        option_runs = key["items"][case_id]
        means = {option: {} for option in ("A", "B")}
        for option in ("A", "B"):
            for criterion in CRITERIA:
                means[option][criterion] = statistics.mean(int(row[f"{option}_{criterion}"]) for row in rows)
            for flag in FLAGS:
                if any(_truthy(row.get(f"{option}_{flag}")) for row in rows):
                    run_id = option_runs[option]
                    flag_counts[run_id][flag] += 1
        by_run = {option_runs[option]: means[option] for option in ("A", "B")}
        per_item.append({run_id: by_run[run_id] for run_id in (baseline_id, candidate_id)})

    criteria_summary = {}
    for criterion in CRITERIA:
        baseline_mean = statistics.mean(item[baseline_id][criterion] for item in per_item)
        candidate_mean = statistics.mean(item[candidate_id][criterion] for item in per_item)
        criteria_summary[criterion] = {
            "baseline_mean": round(baseline_mean, 4),
            "candidate_mean": round(candidate_mean, 4),
            "candidate_delta": round(candidate_mean - baseline_mean, 4),
        }
    naturalness_deltas = [item[candidate_id]["naturalness"] - item[baseline_id]["naturalness"]
                          for item in per_item]
    result["human_evaluation"].update({
        "criteria": criteria_summary,
        "candidate_naturalness_better_pairs": sum(delta > 0 for delta in naturalness_deltas),
        "candidate_naturalness_tied_pairs": sum(delta == 0 for delta in naturalness_deltas),
        "candidate_naturalness_worse_pairs": sum(delta < 0 for delta in naturalness_deltas),
        "critical_flags": flag_counts,
    })
    return result


def _index_run(run, cases):
    if not isinstance(run, dict) or not all(_nonempty(run.get(key)) for key in ("run_id", "model_version", "prompt_version")):
        raise ValueError("each evaluation run requires run_id, model_version and prompt_version")
    expected = {case["case_id"]: {turn["turn_id"] for turn in case["turns"]} for case in cases}
    results = run.get("results")
    if not isinstance(results, list) or {result.get("case_id") for result in results} != set(expected):
        raise ValueError("each run must include exactly the selected conversations")
    indexed = {}
    for result in results:
        case_id = result["case_id"]
        turns = result.get("turns")
        if not isinstance(turns, list) or {turn.get("turn_id") for turn in turns} != expected[case_id]:
            raise ValueError(f"run turns do not match conversation {case_id}")
        indexed[case_id] = {}
        for turn in turns:
            response = turn.get("response")
            if "response" not in turn:
                raise ValueError("run turn must include response text or explicit null for no response")
            if response is not None and not _nonempty(response):
                raise ValueError("run response must be nonempty text or null")
            latency = _finite_nonnegative(turn.get("latency_ms"), "latency_ms")
            cost_value = turn.get("cost_usd")
            cost = None if cost_value is None else _finite_nonnegative(cost_value, "cost_usd")
            attempts = turn.get("attempts")
            if attempts is not None and (not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0):
                raise ValueError("attempts must be a nonnegative integer or null when unknown")
            if not isinstance(turn.get("fallback"), bool) or not isinstance(turn.get("review"), bool):
                raise ValueError("run turns require boolean fallback and review metrics")
            passed_send_gate = turn.get("passed_send_gate", response is not None)
            if not isinstance(passed_send_gate, bool):
                raise ValueError("passed_send_gate must be a boolean when provided")
            indexed[case_id][turn["turn_id"]] = {
                "response": response.strip() if response is not None else None,
                "latency_ms": latency, "cost_usd": cost,
                "fallback": turn["fallback"], "review": turn["review"], "attempts": attempts,
                "passed_send_gate": passed_send_gate,
            }
    return indexed


def _run_metadata(run, indexed):
    turns = [turn for case in indexed.values() for turn in case.values()]
    latencies = [turn["latency_ms"] for turn in turns]
    known_costs = [turn["cost_usd"] for turn in turns if turn["cost_usd"] is not None]
    unknown_cost_count = sum(turn["cost_usd"] is None for turn in turns)
    known_cost_total = round(sum(known_costs), 8)
    known_attempts = [turn["attempts"] for turn in turns if turn["attempts"] is not None]
    unknown_attempt_count = sum(turn["attempts"] is None for turn in turns)
    fallbacks = sum(turn["fallback"] for turn in turns)
    return {
        "model_version": run["model_version"], "prompt_version": run["prompt_version"],
        "response_count": len(turns), "latency_ms_p50": statistics.median(latencies),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "missing_response_count": sum(turn["response"] is None for turn in turns),
        "known_cost_total_usd": known_cost_total,
        "cost_unknown_count": unknown_cost_count,
        "cost_total_usd": known_cost_total if unknown_cost_count == 0 else None,
        "fallback_count": fallbacks, "fallback_rate": fallbacks / len(turns),
        "review_count": sum(turn["review"] for turn in turns),
        "passed_send_gate_count": sum(turn["passed_send_gate"] for turn in turns),
        "send_gate_failure_count": sum(not turn["passed_send_gate"] for turn in turns),
        "known_attempts_total": sum(known_attempts),
        "attempts_unknown_count": unknown_attempt_count,
        "attempts_total": sum(known_attempts) if unknown_attempt_count == 0 else None,
    }


def _is_sanitized_real(case):
    return (
        isinstance(case.get("provenance"), dict) and _nonempty(case["provenance"].get("source_ref_sha256")) and
        isinstance(case.get("consent"), dict) and case["consent"].get("status") == "granted" and
        case["consent"].get("scope") == "persona_evaluation" and
        _nonempty(case["consent"].get("consent_ref_sha256")) and
        isinstance(case.get("privacy"), dict) and case["privacy"].get("status") == "redacted_reviewed" and
        case["privacy"].get("direct_identifiers_removed") is True and
        case["privacy"].get("private_details_removed") is True
    )


def _finite_nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return value


def _percentile(values, percentile):
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truthy(value):
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _write_private_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)


def _read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate the frozen synthetic set")
    validate.add_argument("--dataset", default=str(DEFAULT_DATASET))
    status = commands.add_parser("status", help="show real-data collection and human-label status")
    status.add_argument("--status-file", default=str(COLLECTION_STATUS))
    importer = commands.add_parser("import-real", help="validate, redact and hash provenance for consented data")
    importer.add_argument("input")
    importer.add_argument("--output", required=True)
    exporter = commands.add_parser("export-ab", help="create a blinded A/B pack and separate unblinding key")
    exporter.add_argument("baseline")
    exporter.add_argument("candidate")
    exporter.add_argument("--output", required=True)
    exporter.add_argument("--key-output", required=True)
    exporter.add_argument("--labels-output")
    exporter.add_argument("--dataset", default=str(DEFAULT_DATASET))
    exporter.add_argument("--real-dataset")
    exporter.add_argument("--include-real", action="store_true")
    exporter.add_argument("--seed", type=int,
                          help="reproduce an assignment; otherwise a random seed is stored only in the private key")
    aggregate = commands.add_parser("aggregate", help="aggregate human labels and run metrics after unblinding")
    aggregate.add_argument("export")
    aggregate.add_argument("key")
    aggregate.add_argument("labels")
    aggregate.add_argument("--output")
    args = parser.parse_args(argv)

    if args.command == "validate":
        dataset = load_dataset(args.dataset)
        count = validate_dataset(dataset)
        print(json.dumps({"validated_synthetic_conversations": count,
                          "real_collection_status": dataset["collection_target"]["status"],
                          "human_evaluation_status": dataset["human_evaluation_status"]}))
    elif args.command == "status":
        print(json.dumps(_read_json(args.status_file), ensure_ascii=False, indent=2))
    elif args.command == "import-real":
        raw = _read_json(args.input)
        records = raw.get("conversations") if isinstance(raw, dict) and "conversations" in raw else raw
        if not isinstance(records, list):
            records = [records]
        cases = [sanitize_real_conversation(record) for record in records]
        _write_private_json(args.output, {"dataset_id": "consented-redacted-real-import",
                                          "status": "consented_redacted_real", "cases": cases})
        print(json.dumps({"imported_redacted_conversations": len(cases), "output": args.output}))
    elif args.command == "export-ab":
        dataset = load_dataset(args.dataset)
        if args.real_dataset:
            if not args.include_real:
                raise ValueError("--real-dataset requires --include-real")
            dataset["cases"].extend(_read_json(args.real_dataset).get("cases", []))
        elif args.include_real:
            raise ValueError("--include-real requires --real-dataset")
        seed = args.seed if args.seed is not None else secrets.randbits(64)
        export, key, label_template = build_blind_export(
            dataset, _read_json(args.baseline), _read_json(args.candidate),
            seed=seed, include_real=args.include_real)
        _write_private_json(args.output, export)
        _write_private_json(args.key_output, key)
        labels_path = args.labels_output or str(Path(args.output).with_suffix(".labels.csv"))
        labels_target = Path(labels_path)
        labels_target.parent.mkdir(parents=True, exist_ok=True)
        labels_target.write_text(label_template, encoding="utf-8")
        labels_target.chmod(0o600)
        print(json.dumps({"blinded_items": len(export["cases"]),
                          "labels_template": labels_path, "real_included": args.include_real}))
    elif args.command == "aggregate":
        with Path(args.labels).open(encoding="utf-8", newline="") as handle:
            labels = list(csv.DictReader(handle))
        result = aggregate_scores(_read_json(args.export), _read_json(args.key), labels)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(2)
