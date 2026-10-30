import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_persona_conversations import (
    _index_run,
    _run_metadata,
    build_blind_export,
    load_dataset,
)
from scripts.run_persona_conversations import run_case, run_dataset
from tests.unit.test_persona_turn import semantic_v2


DATASET = Path(__file__).resolve().parents[2] / "datasets" / "persona_conversations" / "frozen12.json"


def candidate(fact):
    text = (f"Mình làm {fact['value']}." if fact["key"] == "occupation.title"
            else f"Mình đang đi {fact['value']}.")
    claim = {
        "text": text,
        "subject": "persona",
        "predicate": fact["key"],
        "fact_ids": [fact["fact_id"]],
        "memory_ids": [],
        "message_ids": [],
    }
    return {"reply_text": text, "fact_ids": [fact["fact_id"]], "memory_ids": [],
            "claims": [claim], "asks_question": False, "question": None}


def phase_result(text, phase, *, needs=None, reply_candidate=None, semantic=None,
                 attempts=1, retry_attempts=0, cost=0.001, cost_unknown=False,
                 prompt_tokens=100, completion_tokens=20):
    result = {
        "ok": True,
        "semantic": semantic or semantic_v2(text),
        "peer_signals": None,
        "model_effective": "test/luna",
        "prompt_version": f"persona_{phase}_v1",
        "attempts": attempts,
        "retry_attempts": retry_attempts,
        "cost_usd_est": None if cost_unknown else cost,
        "cost_unknown": cost_unknown,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                   "total_tokens": prompt_tokens + completion_tokens},
    }
    if phase == "analysis":
        result["needs"] = needs or {"topics": [], "keys": []}
    else:
        result["reply_candidate"] = reply_candidate
    return result


def synthetic_case(case_id, turns, *, active_fact_keys=None, revoked_fact_keys=None,
                   memory_state="none", memory_topics=None, post_context=None):
    return {
        "case_id": case_id,
        "origin": "synthetic_fixture",
        "disclosure": "SYNTHETIC FIXTURE",
        "snapshot": {"persona_fixture": "nam_creta-v15",
                     "active_fact_keys": active_fact_keys or [],
                     "revoked_fact_keys": revoked_fact_keys or [],
                     "memory_state": memory_state,
                     "memory_topics": memory_topics or []},
        "post_context": post_context,
        "turns": turns,
    }


def price_list_semantic(text):
    return {
        "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
        "scope": "in_scope",
        "requests": [{"topic": "price", "desired_outcome": "price_list", "polarity": "positive"}],
        "operations": [
            {"op": "START_REQUEST", "request_ref": "topic:price", "evidence_excerpt": "Creta",
             "slot": {"kind": "vehicle", "value": "Creta", "raw": "Creta", "explicit": True,
                      "evidence_excerpt": "Creta"}},
            {"op": "UPDATE_SLOT", "request_ref": "topic:price", "evidence_excerpt": "Bản Cao cấp",
             "slot": {"kind": "variant", "value": "Cao cấp", "raw": "Bản Cao cấp", "explicit": True,
                      "evidence_excerpt": "Bản Cao cấp"}},
            {"op": "UPDATE_SLOT", "request_ref": "topic:price", "evidence_excerpt": "giá niêm yết",
             "slot": {"kind": "price_kind", "value": "list", "raw": "giá niêm yết", "explicit": True,
                      "evidence_excerpt": "giá niêm yết"}},
        ],
        "uncertainties": [],
    }


class PersonaRunnerTests(unittest.TestCase):
    def test_generated_uses_needs_scoped_analysis_then_reply_and_aggregates_phase_metrics(self):
        case = synthetic_case("runner-cost", [
            {"turn_id": "t1", "customer_text": "Bạn đang đi bản nào?"},
            {"turn_id": "t2", "customer_text": "Bạn làm nghề gì?"},
        ], active_fact_keys=["vehicle.current.model", "vehicle.current.trim", "occupation.title"])
        observed = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            observed.append({"comment": comment, "phase": phase,
                             "persona_context": copy.deepcopy(persona_context)})
            key, topic = (("vehicle.current.trim", "vehicle_current") if "bản nào" in comment
                          else ("occupation.title", "occupation"))
            if phase == "analysis":
                return phase_result(comment, phase,
                    needs={"topics": [topic], "keys": [key]}, attempts=2,
                    retry_attempts=1, cost=0.003, prompt_tokens=210, completion_tokens=40)
            fact = next(row for row in persona_context["facts"] if row["key"] == key)
            return phase_result(comment, phase, reply_candidate=candidate(fact),
                attempts=1, cost=0.002 if "bản nào" in comment else None,
                cost_unknown="nghề gì" in comment, prompt_tokens=180, completion_tokens=35)

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            run = run_case(case, "generated")

        self.assertEqual(luna.call_count, 4)
        self.assertEqual([call["phase"] for call in observed],
                         ["analysis", "reply", "analysis", "reply"])
        self.assertEqual([turn["turn_id"] for turn in run["turns"]], ["t1", "t2"])
        self.assertEqual([turn["response"] for turn in run["turns"]],
                         ["Mình đang đi 1.5 Cao cấp.", "Mình làm Kỹ sư cơ khí."])
        self.assertEqual(run["turns"][0]["cost_usd"], 0.005)
        self.assertIsNone(run["turns"][1]["cost_usd"])
        self.assertTrue(run["turns"][0]["attempts"] == run["turns"][1]["attempts"] == 3)
        self.assertTrue(run["turns"][0]["retry_attempts"] == run["turns"][1]["retry_attempts"] == 1)
        self.assertEqual(run["turns"][0]["usage"], {
            "prompt_tokens": 390, "completion_tokens": 75, "total_tokens": 465})
        for turn in run["turns"]:
            self.assertEqual([phase["phase"] for phase in turn["phase_calls"]], ["analysis", "reply"])
            self.assertAlmostEqual(turn["latency_ms"],
                                   sum(phase["latency_ms"] for phase in turn["phase_calls"]), places=2)
        for analysis, reply in (observed[:2], observed[2:]):
            self.assertIsNone(analysis["persona_context"])
            self.assertIsNotNone(reply["persona_context"])
            self.assertEqual(reply["persona_context"]["phase_one_analysis"]["needs"],
                {"topics": ["vehicle_current" if "bản nào" in analysis["comment"] else "occupation"],
                 "keys": ["vehicle.current.trim" if "bản nào" in analysis["comment"] else "occupation.title"]})
            fact_keys = {fact["key"] for fact in reply["persona_context"]["facts"]}
            expected_key = "vehicle.current.trim" if "bản nào" in analysis["comment"] else "occupation.title"
            self.assertEqual(fact_keys, {expected_key})
        self.assertTrue(all(turn["validation_status"] == "OK" for turn in run["turns"]))
        self.assertTrue(all(turn["review_status"] == "SENT" for turn in run["turns"]))
        self.assertTrue(all(turn["automated_reviewer"] is None for turn in run["turns"]))

    def test_legacy_variant_uses_one_semantic_only_adapter_call_per_turn(self):
        case = synthetic_case("runner-legacy", [
            {"turn_id": "t1", "customer_text": "Bạn đang đi xe gì?"},
            {"turn_id": "t2", "customer_text": "Cảm ơn bạn."},
        ], active_fact_keys=["vehicle.current.model"])
        seen = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            seen.append((comment, persona_context, json.loads(context)))
            return {"ok": True, "semantic": semantic_v2(comment),
                    "model_effective": "test/luna", "prompt_version": "luna_system_v2",
                    "attempts": 1, "cost_usd_est": 0.002}

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            run = run_case(case, "legacy")

        self.assertEqual(luna.call_count, len(case["turns"]))
        self.assertTrue(all(persona_context is None for _, persona_context, _ in seen))
        self.assertTrue(all("persona_context" not in context for _, _, context in seen))
        self.assertTrue(all(turn["cost_usd"] == 0.002 for turn in run["turns"]))

    def test_dataset_runner_selects_cases_and_matches_blind_export_run_shape(self):
        dataset = load_dataset(DATASET)
        case = dataset["cases"][0]

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            if phase == "analysis":
                return phase_result(comment, phase)
            return phase_result(comment, phase, reply_candidate=None, cost_unknown=True)

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            run = run_dataset(dataset, "generated", case_ids=[case["case_id"]])

        self.assertEqual(luna.call_count, len(case["turns"]) * 2)
        self.assertEqual(run["selected_case_ids"], [case["case_id"]])
        indexed = _index_run(run, [case])
        metadata = _run_metadata(run, indexed)
        self.assertEqual(metadata["response_count"], len(case["turns"]))
        self.assertEqual(metadata["cost_unknown_count"], len(case["turns"]))
        self.assertIsNone(metadata["cost_total_usd"])

    def test_invalid_generated_candidate_uses_engine_approved_fallback(self):
        case = synthetic_case("runner-review", [
            {"turn_id": "t1", "customer_text": "Bạn đang đi xe gì?"},
        ], active_fact_keys=["vehicle.current.model"])
        unsupported = {"reply_text": "Mình đã lái thử Hyundai Tucson.", "fact_ids": [],
                       "memory_ids": [], "claims": [], "asks_question": False,
                       "question": None}

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            if phase == "analysis":
                return phase_result(comment, phase, needs={"topics": ["vehicle_current"],
                    "keys": ["vehicle.current.model"]})
            return phase_result(comment, phase, reply_candidate=unsupported, cost_unknown=True)

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            run = run_case(case, "generated")

        result = run["turns"][0]
        self.assertEqual(luna.call_count, 2)
        self.assertEqual(result["response"], "Mình đang đi Hyundai Creta.")
        self.assertNotEqual(result["response"], unsupported["reply_text"])
        self.assertEqual(result["response_origin"], "engine_approved_fallback")
        self.assertEqual(result["review_status"], "SENT")
        self.assertIsNone(result["automated_reviewer"])

    def test_runner_never_approves_or_reports_an_unsent_candidate_as_a_reply(self):
        case = synthetic_case("runner-no-auto-review", [
            {"turn_id": "t1", "customer_text": "Bạn đang đi xe gì?"},
        ], active_fact_keys=["vehicle.current.model"])

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            if phase == "analysis":
                return phase_result(comment, phase, needs={"topics": ["vehicle_current"],
                    "keys": ["vehicle.current.model"]})
            fact = next(item for item in persona_context["facts"]
                        if item["key"] == "vehicle.current.model")
            return phase_result(comment, phase, reply_candidate=candidate(fact))

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call), \
             patch("app.state.FakeSender.send", return_value={"status": "failed"}), \
             patch("app.persona_runtime.PersonaGenerationRuntime.review_generated",
                   side_effect=AssertionError("runner must not use review flow")):
            result = run_case(case, "generated")["turns"][0]

        self.assertIsNone(result["response"])
        self.assertTrue(result["candidate_text"])
        self.assertEqual(result["review_status"], "NO_SEND")
        self.assertFalse(result["review"])
        self.assertFalse(result["passed_send_gate"])

    def test_schema_review_preserves_exact_diagnostic_in_run(self):
        text = 'Cảm ơn bạn.'
        case = synthetic_case('runner-schema-diagnostic', [{'turn_id': 't1', 'customer_text': text}])
        invalid = dict(semantic_v2(text), requests=[])
        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            if kwargs.get("persona_phase") == "analysis":
                return phase_result(comment, "analysis", semantic=invalid)
            self.fail("reply phase must not run after invalid phase-one semantics")

        with patch('scripts.run_persona_conversations.call_luna', side_effect=fake_call) as luna:
            turn = run_case(case, 'generated')['turns'][0]
        self.assertEqual(luna.call_count, 1)
        self.assertIn('REVIEW_SCHEMA', turn['routing'])
        self.assertIn('intent_labels must match the ordered unique request topics', turn['validation_errors'])

    def test_analysis_failure_falls_back_to_semantic_only_without_running_reply_phase(self):
        case = synthetic_case("runner-analysis-fallback", [
            {"turn_id": "t1", "customer_text": "Cảm ơn bạn."},
        ])
        calls = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            calls.append((kwargs.get("persona_phase"), kwargs.get("schema_version"), persona_context))
            if kwargs.get("persona_phase") == "analysis":
                return {"ok": False, "error": "analysis output invalid", "error_code": "MODEL_OUTPUT_INVALID_JSON",
                    "attempts": 1, "cost_usd_est": None, "cost_unknown": True,
                    "usage": {"prompt_tokens": 80, "completion_tokens": 10}}
            if kwargs.get("schema_version") == "2":
                return {"ok": True, "semantic": semantic_v2(comment), "attempts": 2,
                    "retry_attempts": 1, "cost_usd_est": 0.004, "cost_unknown": False,
                    "usage": {"prompt_tokens": 90, "completion_tokens": 25,
                              "total_tokens": 115}, "model_effective": "test/luna"}
            self.fail("the fallback path must not make a reply-generation call")

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            turn = run_case(case, "generated")["turns"][0]

        self.assertEqual(luna.call_count, 2)
        self.assertEqual([(phase, schema) for phase, schema, _ in calls],
                         [("analysis", None), (None, "2")])
        self.assertIsNone(calls[0][2])
        self.assertIsNone(calls[1][2])
        self.assertEqual([phase["phase"] for phase in turn["phase_calls"]],
                         ["analysis", "fallback_semantic"])
        self.assertEqual(turn["attempts"], 3)
        self.assertEqual(turn["retry_attempts"], 1)
        self.assertIsNone(turn["cost_usd"])
        self.assertTrue(turn["phase_calls"][0]["cost_usd_est"] is None)
        self.assertEqual(turn["phase_calls"][1]["cost_usd_est"], 0.004)
        self.assertIsNone(turn["persona_needs"])

    def test_adapter_error_records_null_response_and_continues_to_later_turn(self):
        case = synthetic_case("runner-provider-error", [
            {"turn_id": "t1", "customer_text": "first turn adapter exception"},
            {"turn_id": "t2", "customer_text": "Bạn đang đi xe gì?"},
        ], active_fact_keys=["vehicle.current.model"])

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            if "exception" in comment and phase == "analysis":
                raise TimeoutError("network details are not copied into the run")
            if "exception" in comment:
                return {"ok": False, "error": "semantic fallback failed", "attempts": 1,
                        "cost_usd_est": None, "cost_unknown": True}
            if phase == "analysis":
                return phase_result(comment, phase, needs={"topics": ["vehicle_current"],
                    "keys": ["vehicle.current.model"]})
            fact = next(row for row in persona_context["facts"]
                        if row["key"] == "vehicle.current.model")
            return phase_result(comment, phase, reply_candidate=candidate(fact), cost=0.01)

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
            case_result = run_case(case, "generated")

        run = {"run_id": "provider-error", "model_version": "test/luna",
               "prompt_version": "persona_turn_v1", "results": [case_result]}
        indexed = _index_run(run, [case])
        metrics = _run_metadata(run, indexed)

        self.assertEqual(luna.call_count, 4)
        self.assertIsNone(case_result["turns"][0]["response"])
        self.assertIsNone(case_result["turns"][0]["attempts"])
        self.assertEqual(case_result["turns"][0]["no_response_reason"], "semantic fallback failed")
        self.assertEqual([phase["phase"] for phase in case_result["turns"][0]["phase_calls"]],
                         ["analysis", "fallback_semantic"])
        self.assertEqual(case_result["turns"][0]["phase_calls"][0]["error"],
                         "adapter exception: TimeoutError")
        self.assertEqual(case_result["turns"][1]["response"], "Mình đang đi Hyundai Creta.")
        self.assertEqual(metrics["missing_response_count"], 1)
        self.assertEqual(metrics["cost_unknown_count"], 1)
        self.assertEqual(metrics["attempts_unknown_count"], 1)
        self.assertIsNone(metrics["cost_total_usd"])
        self.assertIsNone(metrics["attempts_total"])

    def test_price_intent_has_no_sample_product_fact_or_quote_in_either_variant(self):
        text = "Creta Bản Cao cấp giá niêm yết bao nhiêu?"
        case = synthetic_case("runner-no-product-samples", [
            {"turn_id": "t1", "customer_text": text},
        ], active_fact_keys=[])

        for variant in ("legacy", "generated"):
            def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
                if variant == "legacy":
                    return {"ok": True, "semantic": price_list_semantic(comment),
                            "model_effective": "test/luna", "prompt_version": "luna_system_v2",
                            "attempts": 1, "cost_usd_est": None}
                phase = kwargs.get("persona_phase")
                return phase_result(comment, phase,
                    semantic=price_list_semantic(comment) if phase == "analysis" else None,
                    reply_candidate=None, cost_unknown=True)

            with self.subTest(variant=variant), patch(
                    "scripts.run_persona_conversations.call_luna", side_effect=fake_call) as luna:
                result = run_case(case, variant)["turns"][0]

            self.assertEqual(luna.call_count, 1 if variant == "legacy" else 2)
            self.assertEqual(result["fact_ids"], [])
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SAMPLE", serialized)
            self.assertNotIn("f_list_creta", serialized)
            self.assertNotIn("699.000.000", serialized)
            self.assertNotIn("699000000", serialized)
            self.assertFalse(result["passed_send_gate"])
            self.assertIsNone(result["response"])
            if variant == "generated":
                self.assertFalse(result["review"])
                self.assertEqual(result["review_status"], "NO_SEND")
            else:
                self.assertTrue(result["review"])
                self.assertEqual(result["review_status"], "REVIEW")

    def test_context_and_snapshot_filters_are_actor_scoped_and_respected(self):
        case = synthetic_case("runner-isolation", [
            {"turn_id": "t1", "actor_id": "actor-a", "customer_text": "actor-a-private marker"},
            {"turn_id": "t2", "actor_id": "actor-b", "customer_text": "Bạn đang đi xe gì?"},
            {"turn_id": "t3", "actor_id": "actor-b", "customer_text": "actor-b-private marker"},
        ], active_fact_keys=["occupation.title"], revoked_fact_keys=["vehicle.current.model"],
           post_context="Bài synthetic đề cập Hyundai Tucson.")
        contexts = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            contexts.append((comment, phase, json.loads(context), copy.deepcopy(persona_context)))
            if phase == "analysis":
                return phase_result(comment, phase, needs={"topics": ["occupation"],
                    "keys": ["occupation.title"]})
            return phase_result(comment, phase, reply_candidate=None, cost_unknown=True)

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call):
            run_case(case, "generated")

        second_actor_reply = next(row for row in contexts
                                  if row[0] == case["turns"][1]["customer_text"] and row[1] == "reply")
        second_actor_context = second_actor_reply[3]
        self.assertEqual({fact["key"] for fact in second_actor_context["facts"]}, {"occupation.title"})
        self.assertNotIn("vehicle.current.model", {fact["key"] for fact in second_actor_context["facts"]})
        self.assertNotIn("actor-a-private marker", repr(second_actor_reply[2]))
        self.assertNotIn("actor-a-private marker", repr(second_actor_context["messages"]))
        self.assertIn("Hyundai Tucson", repr(second_actor_reply[2]["postcard"]))
        self.assertNotIn("Hyundai Tucson", repr(second_actor_context["messages"]))

    def test_frozen_memory_state_and_topics_are_passed_to_generated_context(self):
        dataset = load_dataset(DATASET)
        case = copy.deepcopy(next(item for item in dataset["cases"]
                                  if item["case_id"] == "syn-10-memory-topic-and-cooldown"))
        contexts = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            phase = kwargs.get("persona_phase")
            if phase == "analysis":
                needs = ({"topics": ["travel_story"], "keys": ["travel.weekend.destination"]}
                         if "kể một chuyện" in comment else {"topics": [], "keys": []})
                result = phase_result(comment, phase, needs=needs)
                if "kể một chuyện" in comment:
                    result["peer_signals"] = {"speaker_intent": "social", "address_term": "bạn",
                        "persona_topic": "travel_story", "asks_persona_question": True,
                        "evidence_excerpt": comment, "claims": []}
                return result
            contexts.append(copy.deepcopy(persona_context))
            candidate_value = None
            if "kể một chuyện" in comment and persona_context["memories"]:
                memory = persona_context["memories"][0]
                fact_id = memory["fact_ids"][0]
                fact = next(item for item in persona_context["facts"] if item["fact_id"] == fact_id)
                text = memory["approved_summary"]
                claim = {"text": text, "subject": "persona", "predicate": fact["key"],
                         "fact_ids": [fact_id], "memory_ids": [memory["memory_id"]],
                         "message_ids": []}
                candidate_value = {"reply_text": text, "fact_ids": [fact_id],
                                   "memory_ids": [memory["memory_id"]], "claims": [claim],
                                   "asks_question": False, "question": None}
            result = phase_result(comment, phase, reply_candidate=candidate_value, cost_unknown=True)
            return result

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call):
            run = run_case(case, "generated")

        self.assertEqual(len(contexts), len(case["turns"]))
        self.assertFalse(contexts[0]["memories"])
        self.assertFalse(contexts[1]["memories"])
        self.assertEqual([memory["topic"] for memory in contexts[2]["memories"]], ["travel_story"])
        self.assertTrue(all(memory["topic"] == "travel_story"
                            for context in contexts for memory in context["memories"]))
        self.assertEqual(contexts[2]["memories"][0]["approved_summary"],
                         "Cuối tuần mình về Nam Định.")
        self.assertFalse(contexts[3]["memories"])
        self.assertEqual(run["turns"][2]["validation_status"], "OK")
        self.assertEqual(run["turns"][2]["response"], "Cuối tuần mình về Nam Định.")
        self.assertEqual(run["turns"][2]["review_status"], "SENT")

    def test_evaluator_preserves_unknown_cost_and_reports_partial_totals(self):
        dataset = load_dataset(DATASET)
        case = dataset["cases"][0]
        run = {"run_id": "cost-test", "model_version": "test-model", "prompt_version": "test-prompt",
               "results": [{"case_id": case["case_id"], "turns": [
                   {"turn_id": turn["turn_id"], "response": "synthetic reply", "latency_ms": 10,
                    "cost_usd": None if index == 0 else 0.01, "attempts": 1,
                    "fallback": False, "review": False}
                   for index, turn in enumerate(case["turns"])
               ]}]}

        indexed = _index_run(run, [case])
        metadata = _run_metadata(run, indexed)

        self.assertIsNone(indexed[case["case_id"]]["t1"]["cost_usd"])
        self.assertEqual(metadata["cost_unknown_count"], 1)
        self.assertAlmostEqual(metadata["known_cost_total_usd"], 0.03)
        self.assertIsNone(metadata["cost_total_usd"])

    def test_blind_export_keeps_null_response_and_unknown_run_metrics(self):
        dataset = load_dataset(DATASET)

        def run(run_id, missing):
            results = []
            for case_index, case in enumerate(dataset["cases"]):
                turns = []
                for turn_index, turn in enumerate(case["turns"]):
                    unknown = missing and case_index == 0 and turn_index == 0
                    turns.append({"turn_id": turn["turn_id"],
                                  "response": None if unknown else "fixture output",
                                  "latency_ms": 10, "cost_usd": None if unknown else 0.01,
                                  "attempts": None if unknown else 1,
                                  "fallback": False, "review": unknown})
                results.append({"case_id": case["case_id"], "turns": turns})
            return {"run_id": run_id, "model_version": "test-model",
                    "prompt_version": "test-prompt", "results": results}

        export, key, _ = build_blind_export(dataset, run("baseline", True),
                                            run("candidate", False), seed=11)
        first_turn = export["cases"][0]["turns"][0]
        self.assertIn(None, [first_turn["A"]["response"], first_turn["B"]["response"]])
        metadata = key["variants"]["baseline"]
        self.assertEqual(metadata["missing_response_count"], 1)
        self.assertEqual(metadata["cost_unknown_count"], 1)
        self.assertIsNone(metadata["cost_total_usd"])
        self.assertEqual(metadata["attempts_unknown_count"], 1)
        self.assertIsNone(metadata["attempts_total"])


if __name__ == "__main__":
    unittest.main()
