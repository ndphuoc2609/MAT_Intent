import csv
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_persona_conversations import (
    aggregate_scores,
    build_blind_export,
    load_dataset,
    sanitize_real_conversation,
    validate_dataset,
)
from scripts.run_persona_conversations import run_case
from tests.unit.test_persona_turn import semantic_v2


DATASET = Path(__file__).resolve().parents[2] / "datasets" / "persona_conversations" / "frozen12.json"
CRITERIA = ("naturalness", "intent_adherence", "continuity", "persona_consistency", "question_timing")


class PersonaEvaluationTests(unittest.TestCase):
    def test_frozen_regression_set_has_twelve_synthetic_four_to_eight_turn_conversations(self):
        dataset = load_dataset(DATASET)

        self.assertEqual(validate_dataset(dataset), 12)
        self.assertEqual(dataset["status"], "synthetic_regression_only")
        self.assertEqual(dataset["human_evaluation_status"], "not_run")
        self.assertTrue(all(case["origin"] == "synthetic_fixture" for case in dataset["cases"]))
        self.assertTrue(all(4 <= len(case["turns"]) <= 8 for case in dataset["cases"]))
        self.assertTrue(all("human_labels" not in case for case in dataset["cases"]))

    def test_turn_expectations_only_require_snapshot_enabled_facts_and_memories(self):
        dataset = load_dataset(DATASET)

        for case in dataset["cases"]:
            snapshot = case["snapshot"]
            active = set(snapshot.get("active_fact_keys", []))
            revoked = set(snapshot.get("revoked_fact_keys", []))
            for turn in case["turns"]:
                required_facts = turn.get("required_fact_keys")
                required_memories = turn.get("required_memory_topics")
                self.assertIsInstance(required_facts, list, (case["case_id"], turn["turn_id"]))
                self.assertIsInstance(required_memories, list, (case["case_id"], turn["turn_id"]))
                self.assertTrue(set(required_facts) <= active - revoked,
                                (case["case_id"], turn["turn_id"], required_facts, active, revoked))
                self.assertTrue(set(required_memories) <= set(snapshot.get("memory_topics", [])),
                                (case["case_id"], turn["turn_id"], required_memories))
                if required_memories:
                    states = snapshot.get("memory_state_by_turn") or []
                    state = states[case["turns"].index(turn)] if states else snapshot.get("memory_state")
                    self.assertEqual(state, "eligible", (case["case_id"], turn["turn_id"], state))

    def test_actor_isolation_case_seeds_other_actor_before_recall_check(self):
        dataset = load_dataset(DATASET)
        case = next(item for item in dataset["cases"]
                    if item["case_id"] == "syn-12-revoked-fact-and-other-actor")
        turns = case["turns"]
        recall_index = next(index for index, turn in enumerate(turns)
                            if turn.get("actor_id") == "actor-b" and "cuộc trò chuyện trước" in turn["customer_text"])

        self.assertTrue(any(turn.get("actor_id") == "actor-a" and
                            "actor-a-private marker" in turn["customer_text"]
                            for turn in turns[:recall_index]))
        self.assertEqual(len(turns), 4)

    def test_frozen_fact_and_memory_requirements_reach_generated_context(self):
        dataset = load_dataset(DATASET)
        observed = []

        def fake_call(post, comment, parent="", context="", *, persona_context=None, **kwargs):
            observed.append({"comment": comment, "context": context, "phase": kwargs.get("persona_phase"),
                             "persona_context": persona_context})
            return {"ok": True, "semantic": semantic_v2(comment), "reply_candidate": None,
                    "peer_signals": None, "model_effective": "test/luna",
                    "prompt_version": "persona_turn_v1", "attempts": 1, "cost_usd_est": None}

        with patch("scripts.run_persona_conversations.call_luna", side_effect=fake_call):
            for case in dataset["cases"]:
                run_case(case, "generated")

        cursor = 0
        for case in dataset["cases"]:
            for turn in case["turns"]:
                analysis, reply = observed[cursor:cursor + 2]
                self.assertEqual((analysis["phase"], reply["phase"]), ("analysis", "reply"))
                self.assertIsNone(analysis["persona_context"])
                context = reply["persona_context"]
                cursor += 2
                fact_keys = {fact["key"] for fact in context["facts"]}
                memory_topics = {memory["topic"] for memory in context["memories"]}
                self.assertTrue(set(turn["required_fact_keys"]) <= fact_keys,
                                (case["case_id"], turn["turn_id"], fact_keys))
                self.assertTrue(set(turn["required_memory_topics"]) <= memory_topics,
                                (case["case_id"], turn["turn_id"], memory_topics))
                if "cuộc trò chuyện trước" in turn["customer_text"]:
                    self.assertNotIn("actor-a-private marker", reply["context"])
                    self.assertNotIn("actor-a-private marker", repr(reply["persona_context"]))

        self.assertEqual(cursor, len(observed))

    def test_real_import_requires_provenance_redaction_and_consent(self):
        raw = self._real_input()
        missing_consent = dict(raw, consent={"status": "unknown"})

        with self.assertRaisesRegex(ValueError, "consent"):
            sanitize_real_conversation(missing_consent)
        with self.assertRaisesRegex(ValueError, "redaction"):
            sanitize_real_conversation(dict(raw, redaction={"status": "pending"}))
        with self.assertRaisesRegex(ValueError, "provenance"):
            sanitize_real_conversation(dict(raw, provenance={"captured_at": "2026-10-01T00:00:00Z"}))

    def test_real_import_keeps_only_redacted_content_and_hashed_provenance(self):
        raw = self._real_input()
        sanitized = sanitize_real_conversation(raw)

        serialized = repr(sanitized)
        self.assertEqual(sanitized["origin"], "real_redacted")
        self.assertNotIn("private-transcript-42", serialized)
        self.assertNotIn("consent-private-42", serialized)
        self.assertNotIn("reference_reply", serialized)
        self.assertIn("source_ref_sha256", sanitized["provenance"])
        self.assertEqual(len(sanitized["turns"]), 4)

    def test_real_import_rejects_unredacted_direct_identifiers(self):
        raw = self._real_input()
        raw["turns"][0]["customer_text"] = "Gọi tôi ở 0912345678 nhé."

        with self.assertRaisesRegex(ValueError, "redaction scan"):
            sanitize_real_conversation(raw)

    def test_blind_export_hides_run_identity_and_is_seed_deterministic(self):
        dataset = load_dataset(DATASET)
        baseline, candidate = self._runs(dataset)

        first, key, _ = build_blind_export(dataset, baseline, candidate, seed=17)
        second, _, _ = build_blind_export(dataset, baseline, candidate, seed=17)

        self.assertEqual(first, second)
        self.assertEqual(len(first["cases"]), 12)
        self.assertNotIn("baseline-private-id", repr(first))
        self.assertNotIn("candidate-private-id", repr(first))
        self.assertNotIn("model_version", repr(first))
        self.assertNotIn("seed", repr(first))
        self.assertEqual(key["comparison"]["baseline_run_id"], "baseline-private-id")

    def test_real_case_requires_opt_in_and_blind_export_omits_provenance_and_reference_replies(self):
        dataset = load_dataset(DATASET)
        dataset["cases"].append(sanitize_real_conversation(self._real_input()))
        baseline, candidate = self._runs(dataset)
        with self.assertRaisesRegex(ValueError, "explicit include_real"):
            build_blind_export(dataset, baseline, candidate, seed=5)

        export, _, _ = build_blind_export(dataset, baseline, candidate, seed=5, include_real=True)

        self.assertEqual(len(export["cases"]), 13)
        self.assertNotIn("private-transcript-42", repr(export))
        self.assertNotIn("consent-private-42", repr(export))
        self.assertNotIn("private://transcript/42", repr(export))
        self.assertNotIn("reference_reply", repr(export))

    def test_aggregation_requires_two_independent_raters_and_reports_real_metrics(self):
        dataset = load_dataset(DATASET)
        baseline, candidate = self._runs(dataset)
        export, key, _ = build_blind_export(dataset, baseline, candidate, seed=4)
        partial = aggregate_scores(export, key, [])

        self.assertEqual(partial["human_evaluation"]["status"], "incomplete")
        self.assertIsNone(partial["human_evaluation"]["criteria"])
        self.assertEqual(partial["variants"]["baseline-private-id"]["fallback_count"], 0)
        self.assertEqual(partial["variants"]["candidate-private-id"]["fallback_count"], 48)
        self.assertAlmostEqual(partial["variants"]["candidate-private-id"]["cost_total_usd"], 0.48)

    def test_complete_independent_labels_unblind_candidate_deltas_and_critical_claim_flags(self):
        dataset = load_dataset(DATASET)
        baseline, candidate = self._runs(dataset)
        export, key, _ = build_blind_export(dataset, baseline, candidate, seed=7)
        labels = []
        for case in export["cases"]:
            for labeler in ("reviewer-one", "reviewer-two"):
                row = {"blind_item_id": case["blind_item_id"], "labeler_id": labeler,
                       "independence_attested": "true",
                       "A_unsupported_claim": "false", "A_wrong_actor_or_privacy": "false",
                       "A_transaction_error": "false", "B_unsupported_claim": "false",
                       "B_wrong_actor_or_privacy": "false", "B_transaction_error": "false"}
                for option in ("A", "B"):
                    is_candidate = key["items"][case["blind_item_id"]][option] == "candidate-private-id"
                    score = 5 if is_candidate else 2
                    for criterion in CRITERIA:
                        row[f"{option}_{criterion}"] = str(score)
                    if is_candidate:
                        row[f"{option}_unsupported_claim"] = "true"
                labels.append(row)

        result = aggregate_scores(export, key, labels)

        self.assertEqual(result["human_evaluation"]["status"], "complete")
        self.assertEqual(result["human_evaluation"]["labeler_count"], 2)
        self.assertEqual(result["human_evaluation"]["candidate_naturalness_better_pairs"], 12)
        self.assertEqual(result["human_evaluation"]["critical_flags"]["candidate-private-id"]["unsupported_claim"], 12)
        self.assertEqual(result["human_evaluation"]["criteria"]["naturalness"]["candidate_mean"], 5)

    @staticmethod
    def _real_input():
        return {
            "conversation_id": "private-transcript-42",
            "provenance": {
                "source_ref": "private://transcript/42", "captured_at": "2026-10-01T12:00:00Z",
                "collector_id": "collector-17",
            },
            "consent": {
                "status": "granted", "consent_ref": "consent-private-42",
                "scope": "persona_evaluation",
            },
            "redaction": {
                "status": "complete", "reviewer_id": "reviewer-9",
                "reviewed_at": "2026-10-02T12:00:00Z", "direct_identifiers_removed": True,
                "private_details_removed": True,
            },
            "turns": [
                {"turn_id": index, "customer_text": f"Khách hỏi câu số {index}.",
                 "reference_reply": f"Nhân viên trả lời lượt số {index}."}
                for index in range(1, 5)
            ],
        }

    @staticmethod
    def _runs(dataset):
        def make(run_id, prefix, cost, fallback):
            return {
                "run_id": run_id, "model_version": f"{prefix}-model-1", "prompt_version": f"{prefix}-prompt-1",
                "results": [
                    {"case_id": case["case_id"], "turns": [
                        {"turn_id": turn["turn_id"], "response": f"{prefix}: {turn['customer_text']}",
                         "latency_ms": 100 if prefix == "old" else 150, "cost_usd": cost,
                         "fallback": fallback, "review": False, "attempts": 1}
                        for turn in case["turns"]
                    ]}
                    for case in dataset["cases"]
                ],
            }
        return make("baseline-private-id", "old", 0.0, False), make("candidate-private-id", "new", 0.01, True)


if __name__ == "__main__":
    unittest.main()
