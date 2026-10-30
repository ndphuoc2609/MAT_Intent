#!/usr/bin/env python3
"""Run the frozen synthetic persona conversations through Luna and the shared Engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.luna import MODEL, call_luna
from app.context import fmt_context
from app.facts import Facts
from app.harness import make_engine
from app.persona_fixture import seed_nam_creta
from app.persona_store import PersonaStore, migrate_sqlite
from app.engine import Clock
from app.semantics import attach_context_provenance, validate_semantic
from scripts.evaluate_persona_conversations import DEFAULT_DATASET, load_dataset, validate_dataset


VARIANTS = ("legacy", "generated")


def run_case(case, variant):
    """Run one already-validated synthetic case in an isolated in-memory engine."""
    if variant not in VARIANTS:
        raise ValueError("variant must be legacy or generated")
    if not isinstance(case, dict) or case.get("origin") != "synthetic_fixture":
        raise ValueError("the runner accepts synthetic fixture conversations only")
    snapshot_config = case.get("snapshot") or {}
    if snapshot_config.get("persona_fixture") != "nam_creta-v15":
        raise ValueError(f"{case.get('case_id')} requires the frozen nam_creta-v15 snapshot")

    page_id = "synthetic-eval-page"
    post_id = f"{case['case_id']}-post"
    root_id = f"{case['case_id']}-root"
    case_id = case["case_id"]
    post_text = case.get("post_context") or "Synthetic evaluation post for Nam Creta fixture."
    channel = case.get("channel") or "public"
    postcard = {"id": post_id, "page_id": page_id, "text": post_text,
                "vehicles": [], "persona_binding": True, "channel": channel}

    # The comparison mirrors the Meta service's empty product-facts setup.
    # Only the explicitly bound persona fixture is available for persona replies.
    engine = make_engine(clock=Clock(datetime(2026, 10, 30, tzinfo=timezone.utc)),
                         facts=Facts(facts=[]))
    engine.auto_send_generated = variant == "generated"
    try:
        migrate_sqlite(engine.store.conn)
        persona_store = PersonaStore(engine.store)
        persona_id, release_id = seed_nam_creta(persona_store)
        persona_store.bind_post(page_id, post_id, persona_id, release_id)
        engine.persona_store = persona_store
        engine.allow_persona_fixtures = True
        _apply_snapshot_filters(persona_store, persona_id, release_id, snapshot_config)

        turn_results = []
        for index, turn in enumerate(case["turns"], 1):
            actor_id = turn.get("actor_id") or case.get("actor_id") or "actor-a"
            event = {
                "page_id": page_id,
                "post_id": post_id,
                "thread_root_id": root_id,
                "parent_id": turn.get("parent_id") or root_id,
                "actor_id": actor_id,
                "comment_id": f"{case_id}-{turn['turn_id']}",
                "event_dedupe_key": f"synthetic-eval:{case_id}:{turn['turn_id']}",
                "text": turn["customer_text"],
                "actor_role": turn.get("actor_role") or "user",
                "event_type": "create",
                "content_version": 1,
                "channel": turn.get("channel") or channel,
            }
            _apply_memory_state(engine, case, event, snapshot_config, index)
            engine_context = engine.context(event, postcard)
            replies = [payload['text'] for payload in engine.sender.sent
                       if payload.get('persona_scope', {}).get('actor_id') == actor_id]
            serialized_context = _model_context(dict(engine_context, recent_bot_replies=replies[-5:]), event["channel"])
            generation_context = None
            if variant == "generated":
                event["generation_mode"] = "generated"
                api_result = _persona_model_turn(engine, event, postcard, engine_context,
                                                  serialized_context, post_text)
                generation_context = api_result.pop("generation_context", None)
            else:
                started = time.perf_counter()
                try:
                    api_result = call_luna(
                        post=post_text,
                        comment=event["text"],
                        parent="\n".join(engine_context.get("parent_texts") or []),
                        context=serialized_context,
                        model=MODEL,
                        schema_version="2",
                    )
                except Exception as exc:
                    api_result = {"ok": False, "error": f"adapter exception: {type(exc).__name__}",
                                  "attempts": None, "cost_usd_est": None, "cost_unknown": True}
                api_result["model_latency_ms"] = round(max(0.0, (time.perf_counter() - started) * 1000), 3)
                api_result["phase_calls"] = [{"phase": "legacy", "attempts": api_result.get("attempts"),
                    "latency_ms": api_result["model_latency_ms"],
                    "cost_usd_est": api_result.get("cost_usd_est"), "usage": api_result.get("usage", {})}]
            latency_ms = round(max(0.0, api_result.get("model_latency_ms", 0.0) or 0.0), 3)

            semantic = api_result.get("semantic") if api_result.get("ok") else None
            candidate = api_result.get("reply_candidate") if variant == "generated" and api_result.get("ok") else None
            peer_signals = api_result.get("peer_signals") if variant == "generated" and api_result.get("ok") else None
            result = engine.process(
                event,
                postcard,
                semantic,
                expected_state_token=engine_context["state_token"],
                trusted_context_messages=engine_context["messages"],
                reply_candidate=candidate,
                generation_context=generation_context,
                peer_signals=peer_signals,
            )

            generated_draft = None
            if variant == "generated" and result.get("draft_id"):
                generated_draft = next((draft for draft in engine.generated_drafts(event)
                                        if draft["draft_id"] == result["draft_id"]), None)

            candidate_text = candidate.get("reply_text") if isinstance(candidate, dict) else None
            response = result.get("response_text") if result.get("outbound_permission") else None
            routing = result.get("routing") or []
            if not isinstance(response, str) or not response.strip():
                response = None
            if result.get("response_kind") == "approved_fallback":
                response_origin = "engine_approved_fallback"
            elif candidate_text and response == candidate_text:
                response_origin = "generated_candidate"
            elif response is not None:
                response_origin = "legacy_engine"
            else:
                response_origin = None

            if variant == "generated":
                review_status = "SENT" if result.get("outbound_permission") else "NO_SEND"
                fallback_sent = result.get("response_kind") == "approved_fallback"
                validation_status = ("REVIEW" if fallback_sent and result.get("generation_validation") not in (None, "OK")
                    else (generated_draft or {}).get("validation_status")) or (
                    "OK" if result.get("generation_validation") == "OK" else
                    "REVIEW" if result.get("generation_validation") else None)
                review_needed = False
            else:
                fake_sent = bool(result.get("outbound_permission")) and bool(result.get("sends"))
                review_status = "FAKE_SENT" if fake_sent else "REVIEW"
                validation_status = None
                review_needed = not fake_sent or any(
                    "REVIEW" in str(route) or "BLOCKED" in str(route) for route in routing)

            turn_results.append({
                "turn_id": turn["turn_id"],
                "response": response.strip() if response is not None else None,
                "candidate_text": candidate_text if isinstance(candidate_text, str) else None,
                "response_origin": response_origin,
                "no_response_reason": ((generated_draft or {}).get("reason") or api_result.get("error"))
                                       or result.get("generation_validation")
                                       or next((route for route in reversed(routing)
                                                if route.startswith(("REVIEW", "BLOCKED", "SEND_"))), None)
                                       if response is None else None,
                "fact_ids": list((result.get("response_plan") or {}).get(
                    "facts_used", (result.get("response_plan") or {}).get("fact_ids", [])) or []),
                "persona_needs": api_result.get("needs"),
                "persona_gaps": (generation_context or {}).get("gaps", []),
                "persona_facts_supplied": [{key: fact.get(key) for key in
                    ("fact_id", "key", "value", "precision", "source_kind") if key in fact}
                    for fact in (generation_context or {}).get("facts", [])],
                "proposal_results": list(result.get("proposal_results") or []),
                "latency_ms": latency_ms,
                "cost_usd": api_result.get("cost_usd_est"),
                "attempts": _attempt_count(api_result.get("attempts")),
                "retry_attempts": _attempt_count(api_result.get("retry_attempts")),
                "usage": api_result.get("usage", {}),
                "phase_calls": api_result.get("phase_calls", []),
                "fallback": result.get("response_kind") in ("fallback_draft", "approved_fallback"),
                "review": bool(review_needed),
                "review_status": review_status,
                "passed_send_gate": bool(result.get("outbound_permission")),
                "validation_status": validation_status,
                "generation_validation": result.get("generation_validation"),
                "validation_errors": list(result.get('errors') or []),
                "review_reason": result.get("generation_validation") if result.get("response_kind") == "approved_fallback"
                    else (generated_draft or {}).get('reason') or api_result.get('error'),
                "automated_reviewer": None,
                "routing": list(routing),
                "outbound_permission": bool(result.get("outbound_permission")),
                "model_version": api_result.get("model_effective") or MODEL,
                "prompt_version": api_result.get("prompt_version") or (
                    "persona_turn_v1" if variant == "generated" else "luna_system_v2"),
            })
        return {"case_id": case_id, "turns": turn_results}
    finally:
        engine.store.conn.close()
        engine.sender.receiver.conn.close()


def run_dataset(dataset, variant, *, case_ids=None):
    if variant not in VARIANTS:
        raise ValueError("variant must be legacy or generated")
    validate_dataset(dataset)
    cases = dataset["cases"]
    if case_ids:
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("--case values must be unique")
        by_id = {case["case_id"]: case for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in by_id]
        if missing:
            raise ValueError("unknown conversation IDs: " + ", ".join(missing))
        cases = [by_id[case_id] for case_id in case_ids]
    results = [run_case(case, variant) for case in cases]
    first_turn = next((turn for result in results for turn in result["turns"]), {})
    return {
        "run_id": f"{variant}-{uuid.uuid4().hex}",
        "dataset_id": dataset["dataset_id"],
        "variant": variant,
        "synthetic_only": True,
        "human_evaluation_status": "not_run",
        "model_version": first_turn.get("model_version") or MODEL,
        "prompt_version": first_turn.get("prompt_version") or (
            "persona_turn_v1" if variant == "generated" else "luna_system_v2"),
        "fake_delivery": "shared Engine FakeSender only; no Meta or external sends",
        "generated_delivery": "Engine FakeSender auto-send for valid generated replies; no Meta sends",
        "human_approval": False,
        "selected_case_ids": [result["case_id"] for result in results],
        "results": results,
    }


def _apply_snapshot_filters(persona_store, persona_id, release_id, snapshot_config):
    snapshot = persona_store.get_release_snapshot(persona_id, release_id)
    facts = snapshot.get("facts", [])
    active_keys = snapshot_config.get("active_fact_keys")
    active_keys = set(active_keys) if isinstance(active_keys, list) else {fact.get("key") for fact in facts}
    revoked_keys = set(snapshot_config.get("revoked_fact_keys") or [])
    for fact in facts:
        if fact.get("key") not in active_keys or fact.get("key") in revoked_keys:
            persona_store.revoke_fact(fact["fact_id"], "synthetic evaluation snapshot filter", "synthetic-evaluator")

    memory_state = snapshot_config.get("memory_state")
    memory_topics = snapshot_config.get("memory_topics", [])
    allowed_topics = set(memory_topics) if isinstance(memory_topics, list) else set()
    if memory_state == "none" or (memory_state is None and not snapshot_config.get("memory_state_by_turn")):
        allowed_topics.clear()
    for memory in snapshot.get("memories", []):
        if memory.get("topic") not in allowed_topics:
            persona_store.revoke_memory(memory["memory_id"], "synthetic evaluation memory filter",
                                        "synthetic-evaluator")


def _apply_memory_state(engine, case, event, snapshot_config, index):
    """Replace derived cooldown state with the case's frozen synthetic state."""
    state_by_turn = snapshot_config.get("memory_state_by_turn")
    state = (state_by_turn[index - 1] if isinstance(state_by_turn, list) and index <= len(state_by_turn)
             else snapshot_config.get("memory_state", "none"))
    if state not in {"none", "eligible", "cooldown_active", "already_delivered"}:
        raise ValueError(f"unsupported memory_state {state!r} in {case['case_id']}")
    scope = tuple(event[key] for key in ("page_id", "post_id", "thread_root_id", "actor_id"))
    with engine.store.lock:
        # These rows represent state supplied by the fixture, so clear any prior
        # derived delivery for this scope before applying this turn's snapshot.
        engine.store.conn.execute(
            "DELETE FROM persona_memory_deliveries WHERE page_id=? AND post_id=? AND thread_root_id=? AND actor_id=?",
            scope)
        if state in {"cooldown_active", "already_delivered"}:
            snapshot = engine.persona_store.get_binding_snapshot(event["page_id"], event["post_id"])
            memory = next((item for item in snapshot.get("memories", [])
                           if item.get("topic") in set(snapshot_config.get("memory_topics") or [])), None)
            if memory:
                ordinal = engine._generation_turn_ordinal(event)
                engine.store.conn.execute(
                    "INSERT INTO persona_memory_deliveries VALUES (?,?,?,?,?,?,?,?)",
                    (f"synthetic-eval-state:{case['case_id']}:{event['comment_id']}",
                     *scope, max(0, ordinal - 1), json.dumps([memory["memory_id"]]),
                     engine.clock.now().isoformat()))
        engine.store.conn.commit()


def _model_context(engine_context, channel):
    payload = json.loads(fmt_context(engine_context))
    payload["channel"] = channel
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _attempt_count(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _persona_model_turn(engine, event, postcard, engine_context, serialized_context, post_text):
    """Mirror the sandbox's analysis -> needs-scoped facts -> reply calls."""
    calls = []

    def invoke(label, **kwargs):
        started = time.perf_counter()
        try:
            result = call_luna(post=post_text, comment=event["text"],
                parent="\n".join(engine_context.get("parent_texts") or []),
                context=serialized_context, model=MODEL, **kwargs)
        except Exception as exc:
            result = {"ok": False, "error": f"adapter exception: {type(exc).__name__}",
                      "attempts": None, "cost_usd_est": None, "cost_unknown": True}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "adapter returned a non-object", "attempts": 0,
                      "cost_unknown": True}
        result["model_latency_ms"] = round(max(0.0, (time.perf_counter() - started) * 1000), 3)
        calls.append((label, result))
        return result

    def combined(*, semantic=None, candidate=None, peer_signals=None, needs=None,
                 generation_context=None, phase_errors=(), ok=True, error=None):
        attempt_values, retry_values = [], []
        total_latency, total_cost, cost_unknown = 0.0, 0.0, False
        usage, phase_calls = {}, []
        for label, result in calls:
            attempts = _attempt_count(result.get("attempts"))
            retries = result.get("retry_attempts")
            if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
                retries = max(0, attempts - 1) if attempts is not None else None
            attempt_values.append(attempts); retry_values.append(retries)
            total_latency += result.get("model_latency_ms", 0.0) or 0.0
            cost = result.get("cost_usd_est")
            unknown = bool(result.get("cost_unknown", cost is None)) or not isinstance(cost, (int, float))
            if unknown:
                cost_unknown = True
            else:
                total_cost += cost
            phase_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = phase_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = usage.get(key, 0) + value
            phase_calls.append({"phase": label, "attempts": attempts,
                "latency_ms": result.get("model_latency_ms"),
                "cost_usd_est": None if unknown else cost,
                "usage": {key: value for key, value in phase_usage.items()
                          if key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "error_code": result.get("error_code"), "error": result.get("error")})
        return {"ok": ok, "semantic": semantic, "reply_candidate": candidate,
            "peer_signals": peer_signals, "needs": needs, "generation_context": generation_context,
            "phase_errors": list(phase_errors), "attempts": sum(attempt_values)
                if all(value is not None for value in attempt_values) else None,
            "retry_attempts": sum(retry_values) if all(value is not None for value in retry_values) else None,
            "model_latency_ms": total_latency, "cost_usd_est": None if cost_unknown else total_cost,
            "cost_unknown": cost_unknown, "usage": usage, "phase_calls": phase_calls,
            "model_effective": next((result.get("model_effective") for _, result in reversed(calls)
                                      if result.get("model_effective")), MODEL),
            "prompt_version": "persona_analysis_v1+persona_turn_v1",
            "error": error}

    analysis = invoke("analysis", persona_phase="analysis", max_tokens=1200)
    if analysis.get("error_code") == "MODEL_OUTPUT_TRUNCATED":
        analysis = invoke("analysis_retry", persona_phase="analysis", max_tokens=3200,
                          repair_note="Lần trước JSON bị cắt; chỉ trả JSON đúng schema, súc tích.")
    phase_errors = []
    if not analysis.get("ok"):
        phase_errors.append({"phase": "analysis", "error_code": analysis.get("error_code"),
                             "error": analysis.get("error")})
        fallback = invoke("fallback_semantic", schema_version="2", max_tokens=1200)
        if not fallback.get("ok"):
            return combined(ok=False, error=fallback.get("error"), phase_errors=phase_errors)
        semantic = fallback.get("semantic")
        try:
            generation_context = engine.persona_generation_context(event, postcard, None)
        except Exception as exc:
            generation_context = None
            phase_errors.append({"phase": "context", "error": type(exc).__name__})
        return combined(semantic=semantic, needs=None, generation_context=generation_context,
                        phase_errors=phase_errors)

    semantic = analysis.get("semantic")
    checked = attach_context_provenance(semantic, event, engine_context, postcard)
    semantic_errors = validate_semantic(checked, event["text"],
        [post_text] + [row["text"] for row in engine_context.get("recent", [])
                       if isinstance(row, dict) and isinstance(row.get("text"), str)] +
        [text for text in engine_context.get("parent_texts", []) if isinstance(text, str)])
    calls[0][1]["semantic_validation"] = semantic_errors
    if semantic_errors:
        return combined(semantic=semantic, peer_signals=analysis.get("peer_signals"),
                        needs=analysis.get("needs"), phase_errors=phase_errors)

    needs = analysis.get("needs")
    try:
        generation_context = engine.persona_generation_context(event, postcard, needs)
    except Exception as exc:
        generation_context = None
        phase_errors.append({"phase": "context", "error": type(exc).__name__})
    if not isinstance(generation_context, dict):
        phase_errors.append({"phase": "context", "error": "persona_generation_context_unavailable"})
        return combined(semantic=semantic, peer_signals=analysis.get("peer_signals"),
                        needs=needs, phase_errors=phase_errors)

    reply_context = dict(generation_context, phase_one_analysis={"semantic": semantic, "needs": needs})
    reply = invoke("reply", persona_phase="reply", persona_context=reply_context, max_tokens=1200)
    if reply.get("error_code") == "MODEL_OUTPUT_TRUNCATED":
        reply = invoke("reply_retry", persona_phase="reply", persona_context=reply_context,
            max_tokens=3200, repair_note="Lần trước JSON bị cắt; chỉ trả lời ngắn, giữ nguyên semantic.")
    if not reply.get("ok"):
        phase_errors.append({"phase": "reply", "error_code": reply.get("error_code"),
                             "error": reply.get("error")})
    return combined(semantic=semantic, candidate=reply.get("reply_candidate") if reply.get("ok") else None,
        peer_signals=analysis.get("peer_signals"), needs=needs, generation_context=generation_context,
        phase_errors=phase_errors)


def _write_run(path, run):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="run a selected frozen synthetic case; repeat for multiple IDs")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        parser.error("OPENROUTER_API_KEY must be set; the runner does not substitute a fake model response")
    run = run_dataset(load_dataset(args.dataset), args.variant, case_ids=args.case_ids)
    _write_run(args.output, run)
    print(json.dumps({"run_id": run["run_id"], "variant": run["variant"],
                      "conversations": len(run["results"]),
                      "turns": sum(len(result["turns"]) for result in run["results"]),
                      "output": args.output,
                      "human_evaluation_status": run["human_evaluation_status"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"persona conversation run failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
