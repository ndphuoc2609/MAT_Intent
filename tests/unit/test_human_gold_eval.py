import unittest

from scripts.evaluate_human_gold import score, split_human_gold


def row(i, thread, actor, day):
    return {"id": str(i), "thread_id": thread, "actor_id": actor,
            "timestamp": f"2026-10-{day:02d}T09:00:00+07:00", "label_source": "human",
            "labels": ["price"], "slots": {}, "action": "REVIEW",
            "risk_group": "normal", "auto_processed": False}


class HumanGoldEvalTests(unittest.TestCase):
    def test_split_keeps_shared_actor_and_thread_in_one_fold(self):
        rows = [row(i, f"t{i}", f"a{i // 2}", i + 1) for i in range(20)]
        folds = split_human_gold(rows)
        fold_by_id = {r["id"]: name for name, items in folds.items() for r in items}
        for i in range(0, 20, 2):
            self.assertEqual(fold_by_id[str(i)], fold_by_id[str(i + 1)])
        self.assertGreater(len(folds["train"]), len(folds["test"]))
        with self.assertRaisesRegex(ValueError, "label_source"):
            split_human_gold([{**rows[0], "label_source": "synthetic_contract"}])

    def test_metrics_include_missing_predictions_and_risk_slices(self):
        gold = [{**row(1, "thread", "actor", 1), "labels": ["price", "stock"],
                 "slots": {"vehicle": "Creta", "color": "Trắng"}, "action": "HANDOFF",
                 "risk_group": "high"}]
        pred = [{"id": "1", "labels": ["price"], "slots": {"vehicle": "Creta"},
                 "action": "REVIEW", "covered": True, "auto_processed": True,
                 "critical_error": True, "pipeline_latency_ms": 50, "cost_usd": 0.001}]
        report = score(gold, pred)
        self.assertEqual(report["all"]["intent_macro_f1"], 0.5)
        self.assertAlmostEqual(report["all"]["slot_f1"], 2 / 3)
        self.assertEqual(report["all"]["action_correctness"], 0.0)
        self.assertEqual(report["high_risk"]["critical_errors"], 1)
        self.assertEqual(report["auto_processed"]["latency_p95_ms"], 50)
        missing = score(gold, [])
        self.assertEqual(missing["all"]["coverage"], 0.0)
        self.assertEqual(missing["missing_predictions"], 1)


if __name__ == "__main__":
    unittest.main()
