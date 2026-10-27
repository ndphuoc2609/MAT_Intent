"""Regression A7 (resume/cache) + A8 (budget) — mock hoàn toàn, 0 API thật.
Chạy: python3 -m unittest tests.unit.test_review_infra2
"""
import contextlib, io, json, os, re, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import scripts.run_multiturn_eval as runner
from scripts import budget_ledger as budget
import app.adapters.luna as adapter


def _semantic(text):
    return dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
                requests=[dict(topic="social", desired_outcome="ack", polarity="positive")],
                operations=[dict(op="SOCIAL_ACK", evidence_excerpt=text)], uncertainties=[])


def _fixture_luna(post, comment, parent="", context="", **kwargs):
    """Fixture theo audit resume_probe: lượt 1 propose, lượt sau CONFIRM bằng
    pending id đọc từ context hiện tại."""
    if comment == "Lái thử Creta Hà Nội sáng mai":
        ops = [dict(op="START_REQUEST", request_ref="topic:testdrive", evidence_excerpt="Creta",
                    slot={"kind": "vehicle", "value": "Creta"}),
               dict(op="UPDATE_SLOT", request_ref="topic:testdrive", evidence_excerpt="Hà Nội",
                    slot={"kind": "location", "value": "Hà Nội"}),
               dict(op="UPDATE_SLOT", request_ref="topic:testdrive", evidence_excerpt="sáng mai",
                    slot={"kind": "datetime", "value": "sáng mai"})]
    else:
        pid = re.search(r"id=(q_\w+)", context).group(1)
        ops = [dict(op="CONFIRM_PENDING", request_ref="topic:testdrive", pending_id=pid,
                    evidence_excerpt="oke")]
    sem = dict(schema_version="1", sentiment="NEU", sarcasm="absent", scope="in_scope",
               requests=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")],
               operations=ops, uncertainties=[])
    return dict(ok=True, semantic=sem, attempts=1, cost_usd_est=0, model_effective="FIXTURE-NO-API")


class TestA7Resume(unittest.TestCase):
    def _run(self, d, inp_text, out_name, ckdir):
        inp = Path(d) / "convo.jsonl"
        inp.write_text(json.dumps({"id": "resume_probe", "group": "testdrive",
                                   "postcard": {"topic": "Creta", "vehicles": ["Creta"]},
                                   "turns": [{"actor": "uA", "text": t} for t in inp_text]},
                                  ensure_ascii=False) + "\n")
        out = Path(d) / out_name
        sys.argv = ["run_multiturn_eval.py", "--input", str(inp), "--out", str(out),
                    "--ckdir", str(ckdir), "--prior-calls", "0", "--prior-cost", "0"]
        with contextlib.redirect_stdout(io.StringIO()):
            runner.main()
        return json.loads(out.read_text())

    def test_resume_reuses_cache_zero_new_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = str(Path(d) / "ledger.jsonl")
            with patch.object(runner, "call_luna", _fixture_luna), \
                 patch.object(budget, "DEFAULT_PATH", ledger):
                ck = str(Path(d) / "cache")
                turns = ["Lái thử Creta Hà Nội sáng mai", "oke"]
                r1 = self._run(d, turns, "run1.json", ck)
                r2 = self._run(d, turns, "run2.json", ck)
            self.assertEqual(r1["new_calls"], 2)
            self.assertEqual(r2["new_calls"], 0)
            c1, c2 = r1["conversations"][0], r2["conversations"][0]
            self.assertEqual([t["routing"] for t in c1["turns"]],
                             [t["routing"] for t in c2["turns"]])
            self.assertEqual([t["response_text"] for t in c1["turns"]],
                             [t["response_text"] for t in c2["turns"]])
            # Final state logic bằng nhau (bỏ id ngẫu nhiên).
            def logical(conv):
                last = conv["turns"][-1]
                return [(r["topic"], r["status"],
                         {k: v.get("value") for k, v in r["slots"].items()})
                        for r in last["requests"]]
            self.assertEqual(logical(c1), logical(c2))

    def test_changed_input_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = str(Path(d) / "ledger.jsonl")
            with patch.object(runner, "call_luna", _fixture_luna), \
                 patch.object(budget, "DEFAULT_PATH", ledger):
                ck = str(Path(d) / "cache")
                self._run(d, ["Lái thử Creta Hà Nội sáng mai", "oke"], "run1.json", ck)
                r2 = self._run(d, ["Lái thử Creta Hà Nội sáng mai", "oke nhé"], "run2.json", ck)
            # Lượt 2 đổi text -> key khác -> phải gọi lại (không reuse mù).
            self.assertGreaterEqual(r2["new_calls"], 1)


class TestA8Budget(unittest.TestCase):
    def _exercise(self, prior, max_calls, nturns, fake):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inp = d / "input.jsonl"
            out = d / "out.json"
            ledger = d / "ledger.jsonl"
            inp.write_text(json.dumps({"id": "budget_probe", "group": "social",
                                       "postcard": {"vehicles": ["Creta"]},
                                       "turns": [{"actor": "uA", "text": "chào"} for _ in range(nturns)]}) + "\n")
            original_totals = budget.totals
            with patch.object(runner, "call_luna", fake), \
                 patch.object(budget, "DEFAULT_PATH", str(ledger)), \
                 patch.object(budget, "totals", lambda path=None: original_totals(path or str(ledger))):
                sys.argv = ["run_multiturn_eval.py", "--input", str(inp), "--out", str(out),
                            "--ckdir", str(d / "cache"), "--prior-calls", str(prior),
                            "--prior-cost", "0", "--max-calls", str(max_calls)]
                with contextlib.redirect_stdout(io.StringIO()):
                    runner.main()
            data = json.loads(out.read_text())
            return dict(new_calls=data["new_calls"], cumulative_calls=data["cumulative_calls"],
                        budget_hit=data["budget_hit"],
                        turns_run=sum(len(c["turns"]) for c in data["conversations"]))

    def test_conformant_adapter_guard_caps_at_300(self):
        # Adapter ĐÚNG contract: gọi budget_guard trước mỗi attempt.
        def guarded(post, comment, **kw):
            guard = kw.get("budget_guard")
            attempts = 0
            for _ in range(3):
                if guard is not None and not guard():
                    break
                attempts += 1
                break  # thành công ngay attempt đầu
            return dict(ok=True, semantic=_semantic(comment), attempts=attempts,
                        cost_usd_est=0, model_effective="FIXTURE-NO-API")
        r = self._exercise(299, 300, 1, guarded)
        self.assertEqual(r["new_calls"], 1)
        self.assertEqual(r["cumulative_calls"], 300)
        self.assertFalse(r["budget_hit"])

    def test_cap_two_allows_exactly_two_attempts(self):
        def one_attempt(post, comment, **kw):
            return dict(ok=True, semantic=_semantic(comment), attempts=1,
                        cost_usd_est=0, model_effective="FIXTURE-NO-API")
        r = self._exercise(0, 2, 3, one_attempt)
        self.assertEqual(r["new_calls"], 2)
        self.assertEqual(r["turns_run"], 2)
        self.assertTrue(r["budget_hit"])

    def test_unknown_transport_failure_is_persisted_as_unknown_cost(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inp, out, ledger = d / "input.jsonl", d / "out.json", d / "ledger.jsonl"
            inp.write_text(json.dumps({"id": "unknown_probe", "group": "social",
                                       "postcard": {"vehicles": ["Creta"]},
                                       "turns": [{"actor": "uA", "text": "chào"}]}) + "\n")
            original_totals = budget.totals
            def failed(post, comment, **kwargs):
                self.assertTrue(kwargs["budget_guard"]())
                return dict(ok=False, error="timeout", attempts=1,
                            cost_usd_est=None, cost_unknown=True)
            with patch.object(runner, "call_luna", failed), \
                 patch.object(budget, "DEFAULT_PATH", str(ledger)), \
                 patch.object(budget, "totals", lambda path=None: original_totals(path or str(ledger))):
                sys.argv = ["run_multiturn_eval.py", "--input", str(inp), "--out", str(out),
                            "--ckdir", str(d / "cache"), "--prior-calls", "0", "--prior-cost", "0"]
                with contextlib.redirect_stdout(io.StringIO()):
                    runner.main()
            row, = budget._read_records(str(ledger))
            self.assertIsNone(row["cost_usd_est"])
            self.assertEqual(row["status"], "unknown_cost")
            self.assertEqual(budget.totals(str(ledger))[2], 1)

    def test_missing_usage_is_unknown_not_zero(self):
        response = {"choices": [{"message": {"content": json.dumps(_semantic("chào"))}}],
                    "model": "FIXTURE-NO-API"}  # KHÔNG có usage
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            r = adapter.call_luna("post", "chào")
        self.assertTrue(r.get("ok"))
        self.assertIsNone(r.get("cost_usd_est"))
        self.assertTrue(r.get("cost_unknown"))

    def test_invalid_token_counts_are_unknown_without_retry(self):
        invalid = (None, -1, 1.5, float("nan"), float("inf"), -float("inf"), "10", True, {}, [], 10 ** 400)
        for field in ("prompt_tokens", "completion_tokens"):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    usage = {"prompt_tokens": 10, "completion_tokens": 20, field: value}
                    response = {"choices": [{"message": {"content": json.dumps(_semantic("chào"))}}],
                                "usage": usage, "model": "FIXTURE-NO-API"}
                    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                         patch.object(adapter.urllib.request, "build_opener") as build_opener, \
                         patch.object(adapter.time, "sleep"):
                        build_opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps(response).encode())
                        result = adapter.call_luna("post", "chào")
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["attempts"], 1)
                    self.assertIsNone(result["cost_usd_est"])
                    self.assertTrue(result["cost_unknown"])

    def test_ledger_breakdown_splits_statuses(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = str(Path(d) / "ledger.jsonl")
            budget.record(ledger, run_id="t", kind="call", attempts=2, cost_usd_est=0.001)
            budget.record(ledger, run_id="t", kind="call", attempts=1, cost_usd_est=0.0,
                          status="failed")
            budget.record(ledger, run_id="t", kind="call", attempts=1, cost_usd_est=None)
            br = budget.breakdown(ledger)
            self.assertEqual(br["attempted"], 4)
            self.assertEqual(br["completed"], 2)
            self.assertEqual(br["failed"], 1)
            self.assertEqual(br["unknown_cost"], 1)
            self.assertAlmostEqual(br["actual_cost_usd"], 0.001)


class TestOpenRouterCredentials(unittest.TestCase):
    def test_missing_or_blank_key_fails_before_guard_and_network(self):
        for value in (None, "", " \t"):
            with self.subTest(value=value), patch.dict(os.environ), \
                 patch.object(adapter.urllib.request, "build_opener") as build_opener:
                if value is None:
                    os.environ.pop("OPENROUTER_API_KEY", None)
                else:
                    os.environ["OPENROUTER_API_KEY"] = value
                guard_calls = []
                result = adapter.call_luna("post", "chào", budget_guard=lambda: guard_calls.append(True))
                self.assertFalse(result["ok"])
                self.assertEqual(result.get("attempts"), 0)
                self.assertIn("OPENROUTER_API_KEY", result["error"])
                self.assertEqual(guard_calls, [])
                build_opener.assert_not_called()

    def test_key_is_only_sent_in_openrouter_bearer_header(self):
        response = {"choices": [{"message": {"content": json.dumps(_semantic("chào"))}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    "model": "openai/gpt-6-luna"}
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps(response).encode())
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.side_effect = open_request
            result = adapter.call_luna("post", "chào")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["semantic"], _semantic("chào"))
        self.assertEqual(result.get("attempts"), 1)
        self.assertAlmostEqual(result["cost_usd_est"], 0.000011)
        request, = requests
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-key")
        self.assertNotIn(b"test-only-key", request.data)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "openai/gpt-6-luna")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_adapter_rejects_embedded_or_non_object_json_without_retry(self):
        semantic = json.dumps(_semantic("chào"))
        for content in (f"prefix {semantic} suffix", f"[{semantic}]"):
            with self.subTest(content=content[:20]), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                 patch.object(adapter.urllib.request, "build_opener") as build_opener, \
                 patch.object(adapter.time, "sleep"):
                calls = []
                response = {"choices": [{"message": {"content": content}}]}
                build_opener.return_value.open.side_effect = lambda *a, **k: (
                    calls.append(True) or io.BytesIO(json.dumps(response).encode()))
                result = adapter.call_luna("post", "chào")
            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(len(calls), 1)

    def test_retry_requires_another_budget_approval(self):
        approvals = iter((True, False))
        requests = []
        def rate_limited(request, timeout):
            requests.append(request)
            raise adapter.urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, io.BytesIO(b"retry"))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener, \
             patch.object(adapter.time, "sleep"):
            build_opener.return_value.open.side_effect = rate_limited
            result = adapter.call_luna("post", "chào", budget_guard=lambda: next(approvals))
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("budget", result["error"])
        self.assertIsNone(result.get("cost_usd_est"))
        self.assertTrue(result.get("cost_unknown"))

    def test_retryable_http_failure_then_success_keeps_retry_cost_unknown(self):
        response = {"choices": [{"message": {"content": json.dumps(_semantic("chào"))}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    "model": "openai/gpt-6-luna"}
        requests = []
        def rate_limit_then_success(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise adapter.urllib.error.HTTPError(request.full_url, 429, "rate limited", {},
                                                     io.BytesIO(b"retry"))
            return io.BytesIO(json.dumps(response).encode())
        approvals = []
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener, \
             patch.object(adapter.time, "sleep"):
            build_opener.return_value.open.side_effect = rate_limit_then_success
            result = adapter.call_luna("post", "chào", budget_guard=lambda: approvals.append(True) or True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(approvals), 2)
        self.assertIsNone(result["retry_cost_usd_est"])
        self.assertTrue(result["retry_cost_unknown"])

    def test_timeout_retry_remains_unknown_cost(self):
        response = {"choices": [{"message": {"content": json.dumps(_semantic("chào"))}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    "model": "openai/gpt-6-luna"}
        requests = []
        def timeout_then_success(request, timeout):
            requests.append(request)
            if len(requests) == 1: raise TimeoutError("connection dropped")
            return io.BytesIO(json.dumps(response).encode())
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener, \
             patch.object(adapter.time, "sleep"):
            build_opener.return_value.open.side_effect = timeout_then_success
            result = adapter.call_luna("post", "chào", budget_guard=lambda: True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["retry_cost_unknown"])
        self.assertIsNone(result["retry_cost_usd_est"])

    def test_http_error_does_not_expose_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
             patch.object(adapter.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.side_effect = adapter.urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions", 401, "unauthorized", {},
                io.BytesIO(b"invalid credential test-only-key"))
            result = adapter.call_luna("post", "chào")
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("attempts"), 1)
        self.assertIn("401", result["error"])
        self.assertNotIn("test-only-key", json.dumps(result))
        self.assertEqual(result.get("cost_usd_est"), 0.0)
        self.assertFalse(result.get("cost_unknown"))


if __name__ == "__main__":
    unittest.main()
