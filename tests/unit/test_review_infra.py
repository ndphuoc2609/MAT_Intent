"""Tests offline cho hạ tầng R13/R14 (không gọi model, không dùng transport thật).
"""
import sys, os, unittest, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.cache_key import (full_cache_key, state_fingerprint, is_legacy_checkpoint,
                               request_logical_key, pending_logical_key)
from scripts import budget_ledger
from scripts.resume import build_request_map, build_pending_map, remap_semantic

SYS, SCH, MOD = "SYSTEM", "SCHEMA", "model-x"
PC = {"topic": "Creta", "vehicles": ["Creta"]}

def _req(rid, topic, status, slots):
    return {"id": rid, "topic": topic, "status": status, "slots": slots}

class TestCacheKey(unittest.TestCase):
    def _key(self, comment="giá Creta", fp="fp1"):
        return full_cache_key(comment=comment, parent="", postcard=PC, state_fp=fp,
                              system=SYS, schema=SCH, model=MOD)
    def test_same_inputs_same_key(self):
        self.assertEqual(self._key(), self._key())
    def test_comment_changes_key(self):
        self.assertNotEqual(self._key("giá Creta"), self._key("giá Tucson"))
    def test_state_changes_key(self):
        self.assertNotEqual(self._key(fp="fp1"), self._key(fp="fp2"))
    def test_state_fingerprint_ignores_random_ids(self):
        r1 = [_req("aaa", "price", "OPEN", {"vehicle": {"value": "Creta"}})]
        r2 = [_req("zzz", "price", "OPEN", {"vehicle": {"value": "Creta"}})]
        self.assertEqual(state_fingerprint(r1, [], []), state_fingerprint(r2, [], []))
    def test_state_fingerprint_sees_slot_change(self):
        r1 = [_req("a", "price", "OPEN", {"vehicle": {"value": "Creta"}})]
        r2 = [_req("a", "price", "OPEN", {"vehicle": {"value": "Tucson"}})]
        self.assertNotEqual(state_fingerprint(r1, [], []), state_fingerprint(r2, [], []))
    def test_legacy_detection(self):
        self.assertTrue(is_legacy_checkpoint({"cache_key": "abcd1234"}))
        self.assertFalse(is_legacy_checkpoint({"cache_key_v2": "x" * 64, "cache_key": "abcd"}))
        self.assertFalse(is_legacy_checkpoint({}))

class TestBudgetLedger(unittest.TestCase):
    def test_fractional_call_count_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            for name in ("max_calls", "est_attempts", "prior_calls"):
                for value in (0.5, 1.5, 300.5, 1.0):
                    with self.subTest(name=name, value=value):
                        self.assertFalse(budget_ledger.guard(path, **{name: value})[0])

    def test_invalid_persisted_attempts_cannot_reduce_or_hide_usage(self):
        invalid = (-100, 0.5, 1.0, None, True, "1", "bad", float("nan"), float("inf"), {}, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            for value in invalid:
                with self.subTest(value=value):
                    with open(path, "w") as ledger:
                        ledger.write(json.dumps({"attempts": 1, "cost_usd_est": 0.01}) + "\n")
                        ledger.write(json.dumps({"attempts": value, "cost_usd_est": 0.02}) + "\n")
                    self.assertEqual(budget_ledger.totals(path), (1, 0.01, 1, 2))
                    allowed, reason = budget_ledger.guard(path)
                    self.assertFalse(allowed)
                    self.assertIn("unknown", reason)

    def test_malformed_ledger_rows_block_future_spending(self):
        malformed = ('{"attempts":', "not-json", "null", "[]", "42", '"text"', "{}",
                     '{"cost_usd_est": 0.01}')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            for row in malformed:
                with self.subTest(row=row):
                    with open(path, "w") as ledger:
                        ledger.write(row + "\n")
                        ledger.write(json.dumps({"attempts": 1, "cost_usd_est": 0.01}) + "\n")
                    self.assertEqual(budget_ledger.totals(path), (1, 0.01, 1, 2))
                    allowed, reason = budget_ledger.guard(path)
                    self.assertFalse(allowed)
                    self.assertIn("unknown", reason)

    def test_invalid_guard_inputs_fail_closed(self):
        invalid = (-1, float("nan"), float("inf"), -float("inf"), "bad", "1", None, True, {}, [], 10 ** 400)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            for name in ("prior_calls", "prior_cost", "est_attempts", "est_cost", "max_calls", "max_cost"):
                for value in invalid:
                    with self.subTest(name=name, value=value):
                        try:
                            allowed, reason = budget_ledger.guard(path, **{name: value})
                        except (TypeError, ValueError, OverflowError):
                            self.fail("invalid budget inputs must deny spending without raising")
                        self.assertFalse(allowed)
                        self.assertTrue(reason)

    def test_negative_prior_usage_cannot_reopen_exhausted_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            budget_ledger.record(path, run_id="full", kind="call", attempts=300, cost_usd_est=2.0)
            self.assertFalse(budget_ledger.guard(path, prior_calls=-1)[0])

    def test_invalid_persisted_costs_are_unknown(self):
        invalid = (-0.1, float("nan"), float("inf"), -float("inf"), "bad", "0.01", True, {}, [], 10 ** 400)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            for value in invalid:
                with self.subTest(value=value):
                    with open(path, "w") as ledger:
                        ledger.write(json.dumps({"attempts": 1, "cost_usd_est": value}) + "\n")
                    self.assertEqual(budget_ledger.totals(path), (1, 0.0, 1, 1))
                    allowed, reason = budget_ledger.guard(path)
                    self.assertFalse(allowed)
                    self.assertIn("unknown", reason)
                    summary = budget_ledger.breakdown(path)
                    self.assertEqual(summary["unknown_cost"], 1)
                    self.assertEqual(summary["actual_cost_usd"], 0.0)

    def test_record_invalid_cost_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            entry = budget_ledger.record(path, run_id="invalid", kind="call", attempts=1,
                                         cost_usd_est=float("nan"))
            self.assertIsNone(entry["cost_usd_est"])
            self.assertTrue(entry["cost_unknown"])
            self.assertEqual(entry["status"], "unknown_cost")

    def test_prior_calls_are_added_to_ledger_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            budget_ledger.record(path, run_id="sandbox", kind="call", attempts=23, cost_usd_est=0.01)
            self.assertTrue(budget_ledger.guard(path, prior_calls=276, prior_cost=0.0854)[0])
            budget_ledger.record(path, run_id="sandbox", kind="call", attempts=1, cost_usd_est=0.001)
            ok, reason = budget_ledger.guard(path, prior_calls=276, prior_cost=0.0854)
            self.assertFalse(ok)
            self.assertIn("calls", reason)

    def test_prior_cost_is_added_to_ledger_and_estimated_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            budget_ledger.record(path, run_id="sandbox", kind="call", attempts=1, cost_usd_est=1.9)
            self.assertTrue(budget_ledger.guard(path, prior_calls=276, prior_cost=0.0854,
                                              est_cost=0.01)[0])
            ok, reason = budget_ledger.guard(path, prior_calls=276, prior_cost=0.0854,
                                             est_cost=0.02)
            self.assertFalse(ok)
            self.assertIn("cost", reason)

    def test_append_and_guard(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "ledger.jsonl")
        self.assertEqual(budget_ledger.totals(path), (0, 0.0, 0, 0))
        budget_ledger.record(path, run_id="r1", kind="call", attempts=2, cost_usd_est=0.001)
        budget_ledger.record(path, run_id="r1", kind="repair", attempts=1, cost_usd_est=0.0005)
        calls, cost, unknown, n = budget_ledger.totals(path)
        self.assertEqual((calls, n), (3, 2))
        self.assertAlmostEqual(cost, 0.0015)
        ok, _ = budget_ledger.guard(path, max_calls=300, max_cost=2.0)
        self.assertTrue(ok)
        ok, reason = budget_ledger.guard(path, max_calls=3, max_cost=2.0)
        self.assertFalse(ok)
        # Append-only: ghi thêm không xóa cũ.
        lines = open(path).read().strip().split("\n")
        self.assertEqual(len(lines), 2)
    def test_unknown_cost_blocks_spend(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "ledger.jsonl")
        budget_ledger.record(path, run_id="r1", kind="call", attempts=1, cost_usd_est=None)
        ok, reason = budget_ledger.guard(path, max_calls=300, max_cost=2.0)
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_reconcile_verified_failed_cost_appends_and_unblocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            original = budget_ledger.record(path, run_id="r401", kind="call", attempts=1,
                                            cost_usd_est=None, status="failed")

            adjustment = budget_ledger.reconcile_unknown_cost(
                path, original_ts=original["ts"], run_id="r401", cost_usd_est=0.0,
                reason="OpenRouter HTTP 401; failed model attempt is unbilled")

            lines = open(path).read().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), original)
            self.assertEqual(adjustment["kind"], "reconcile")
            self.assertEqual(budget_ledger.totals(path), (1, 0.0, 0, 2))
            self.assertTrue(budget_ledger.guard(path)[0])
            self.assertEqual(budget_ledger.breakdown(path), dict(
                attempted=1, completed=0, failed=1, unknown_cost=0, actual_cost_usd=0.0))

    def test_reconciliation_must_target_one_unknown_failed_attempt_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            original = budget_ledger.record(path, run_id="r401", kind="call", attempts=1,
                                            cost_usd_est=None, status="failed")
            kwargs = dict(path=path, original_ts=original["ts"], run_id="r401",
                          cost_usd_est=0.0, reason="verified no-charge HTTP error")
            with self.assertRaises(ValueError):
                budget_ledger.reconcile_unknown_cost(**{**kwargs, "original_ts": "wrong"})
            budget_ledger.reconcile_unknown_cost(**kwargs)
            with self.assertRaises(ValueError):
                budget_ledger.reconcile_unknown_cost(**kwargs)
            self.assertEqual(budget_ledger.totals(path), (1, 0.0, 0, 2))

    def test_unknown_cost_success_can_be_reconciled_from_provider_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            original = budget_ledger.record(path, run_id="r-usage", kind="call", attempts=1,
                                            cost_usd_est=None, status="unknown_cost")
            budget_ledger.reconcile_unknown_cost(
                path, original_ts=original["ts"], run_id="r-usage", cost_usd_est=0.001,
                reason="verified from provider usage")
            self.assertEqual(budget_ledger.totals(path), (1, 0.001, 0, 2))
            self.assertTrue(budget_ledger.guard(path)[0])

class TestResumeRemap(unittest.TestCase):
    def test_request_and_pending_remap(self):
        old_reqs = [_req("old-r1", "price", "OPEN", {"vehicle": {"value": "Creta"}})]
        new_reqs = [_req("new-r9", "price", "OPEN", {"vehicle": {"value": "Creta"}})]
        req_map = build_request_map(old_reqs, new_reqs)
        self.assertEqual(req_map, {"old-r1": "new-r9"})
        old_p = [{"id": "old-p1", "request_id": "old-r1", "qtype": "yes_no",
                  "expected_slot": None, "options": ["yes", "no"], "status": "OPEN"}]
        new_p = [{"id": "new-p7", "request_id": "new-r9", "qtype": "yes_no",
                  "expected_slot": None, "options": ["yes", "no"], "status": "OPEN"}]
        pend_map = build_pending_map(old_p, new_p, old_reqs, new_reqs)
        self.assertEqual(pend_map, {"old-p1": "new-p7"})
        sem = {"operations": [
            {"op": "UPDATE_SLOT", "request_ref": "old-r1"},
            {"op": "CONFIRM_PENDING", "request_ref": "old-r1", "pending_id": "old-p1"}]}
        out = remap_semantic(sem, req_map, pend_map)
        self.assertEqual(out["operations"][0]["request_ref"], "new-r9")
        self.assertEqual(out["operations"][1]["pending_id"], "new-p7")
    def test_unmappable_raises(self):
        sem = {"operations": [{"op": "CONFIRM_PENDING", "request_ref": "topic:price",
                               "pending_id": "ghost"}]}
        with self.assertRaises(ValueError):
            remap_semantic(sem, {}, {})

class TestFmtContext(unittest.TestCase):
    def test_context_includes_ids(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "scripts"))
        from run_multiturn_eval import fmt_context
        ctx = {"recent": [],
               "active_requests": [{"id": "r-1", "topic": "price", "status": "OPEN",
                                    "slots": {"vehicle": {"value": "Creta"}}}],
               "pending": [{"id": "q-1", "request_id": "r-1", "qtype": "yes_no",
                            "proposition": "Xác nhận?", "expected_slot": None,
                            "options": ["yes", "no"]}]}
        out = fmt_context(ctx)
        self.assertIn("r-1", out)
        self.assertIn("q-1", out)
        self.assertIn("yes_no", out)

if __name__ == "__main__":
    unittest.main()
