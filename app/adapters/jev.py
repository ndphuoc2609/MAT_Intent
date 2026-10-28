"""OpenRouter Decisions adapter for pinned TypeSafe Jev 1.14.

This adapter returns typed decisions only. It does not change semantic
operations, state, policy, or thresholds; callers must calibrate any gate.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

MODEL = "typesafe/jev-1.14"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
INTENT_CRITERIA = {
    "price": "Hỏi giá xe, giá niêm yết, giá lăn bánh hoặc chi phí mua xe.",
    "promo": "Hỏi khuyến mãi, ưu đãi, quà tặng hoặc giảm giá.",
    "loan": "Hỏi vay mua xe, trả góp, lãi suất hoặc khoản trả trước.",
    "stock": "Hỏi xe có sẵn, màu xe hoặc phiên bản còn hàng.",
    "spec": "Hỏi thông số hoặc tính năng kỹ thuật của xe.",
    "dealer": "Hỏi đại lý, showroom hoặc địa chỉ bán xe.",
    "testdrive": "Đề nghị hoặc hỏi về việc lái thử xe.",
    "buy": "Nói rõ muốn mua, đặt cọc hoặc chốt mua xe.",
    "service": "Hỏi bảo hành, bảo dưỡng hoặc sửa chữa.",
    "complaint": "Khiếu nại, tranh chấp hoặc báo trải nghiệm xấu cần xử lý.",
    "registration": "Hỏi đăng ký, biển số hoặc thủ tục xe.",
    "social": "Chào hỏi, cảm ơn, khen hoặc tương tác xã giao không yêu cầu nghiệp vụ.",
}


class DecisionModel(Protocol):
    """Common interface for Jev Decisions, Luna Decisions, and other gates."""

    def decide(self, state, questions, *, timeout=10, budget_guard=None): ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def intent_questions():
    """Return independent yes/no questions, preserving multi-intent comments."""
    return {topic: {"type": "noul", "instructions": f"Does the customer express this intent: {desc}"}
            for topic, desc in INTENT_CRITERIA.items()}


def validate_answers(answers, questions):
    """Require exact typed Noul answers and finite probabilities in [0, 1]."""
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Jev answer keys do not match requested questions")
    for key, question in questions.items():
        answer = answers.get(key)
        if not isinstance(question, dict) or question.get("type") != "noul":
            raise ValueError("unsupported Jev question type")
        if not isinstance(answer, dict) or set(answer) != {"type", "noul"} or answer.get("type") != "noul":
            raise ValueError(f"Jev answer {key!r} has the wrong type or fields")
        probability = answer.get("noul")
        if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"Jev answer {key!r} probability is outside [0, 1]")
    return answers


class JevOpenRouter:
    model = MODEL

    @staticmethod
    def _failure(code, *, attempts=1, latency_ms=None, http_status=None, usage=None,
                 cost_usd_est=None, cost_unknown=True):
        result = {"ok": False, "error_code": code, "error": f"Jev request failed ({code})",
                  "attempts": attempts, "model_requested": MODEL,
                  "cost_usd_est": cost_usd_est, "cost_unknown": cost_unknown}
        if isinstance(usage, dict):
            result["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                               if type(usage.get(key)) is int and usage[key] >= 0}
        if latency_ms is not None:
            result["latency_ms"] = latency_ms
        if http_status is not None:
            result["http_status"] = http_status
        return result

    def decide(self, state, questions, *, timeout=10, budget_guard=None):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return {"ok": False, "error": "OPENROUTER_API_KEY is not configured", "attempts": 0,
                    "cost_usd_est": None, "cost_unknown": True}
        if budget_guard is not None and not budget_guard():
            return {"ok": False, "error": "budget guard denied before Jev request", "attempts": 0,
                    "cost_usd_est": None, "cost_unknown": True}
        body = json.dumps({"model": self.model, "state": state, "questions": questions},
                          ensure_ascii=False, separators=(",", ":")).encode()
        req = urllib.request.Request(ENDPOINT, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {key}")
        start = time.perf_counter()
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
                data = json.load(response)
            latency_ms = (time.perf_counter() - start) * 1000
            if not isinstance(data, dict):
                return self._failure("INVALID_RESPONSE", latency_ms=latency_ms)
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            cost = usage.get("cost")
            cost_known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            if cost_known:
                cost = float(cost)
            else:
                cost = None
            try:
                answers = validate_answers(data.get("answers"), questions)
            except (TypeError, ValueError):
                return self._failure("INVALID_RESPONSE", latency_ms=latency_ms, usage=usage,
                                     cost_usd_est=cost, cost_unknown=not cost_known)
            return {"ok": True, "answers": answers, "usage": usage,
                    "cost_usd_est": cost, "cost_unknown": not cost_known, "model_requested": self.model,
                    "model_effective": data.get("model", self.model), "provider": data.get("provider"),
                    "latency_ms": latency_ms, "attempts": 1, "error": None}
        except urllib.error.HTTPError as error:
            body = error.read(8192)
            try:
                payload = json.loads(body)
                provider_error = payload.get("error") if isinstance(payload, dict) else None
                detail = provider_error.get("message", "") if isinstance(provider_error, dict) else ""
            except (TypeError, ValueError, UnicodeError):
                detail = ""
            normalized = detail.lower() if isinstance(detail, str) else ""
            model_blocked = (error.code == 404 and "model blocked by guardrail" in normalized and
                             "matching your guardrail restrictions and data policy" in normalized)
            code = ("MODEL_BLOCKED" if model_blocked else
                    {401: "HTTP_401", 402: "HTTP_402", 403: "HTTP_403", 404: "HTTP_404",
                     429: "HTTP_429"}.get(error.code, "PROVIDER_ERROR"))
            return self._failure(code, latency_ms=(time.perf_counter() - start) * 1000,
                                 http_status=error.code)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._failure("INVALID_RESPONSE", latency_ms=(time.perf_counter() - start) * 1000)
        except urllib.error.URLError as error:
            code = "TIMEOUT" if isinstance(error.reason, TimeoutError) else "PROVIDER_ERROR"
            return self._failure(code, latency_ms=(time.perf_counter() - start) * 1000)
        except TimeoutError:
            return self._failure("TIMEOUT", latency_ms=(time.perf_counter() - start) * 1000)
        except (OSError, ValueError):
            return self._failure("PROVIDER_ERROR", latency_ms=(time.perf_counter() - start) * 1000)


def confirmation_questions(proposition):
    """Separate yes/no gates; raw probabilities require per-label calibration."""
    return {
        "confirm": {"type": "noul", "instructions":
                    f"Does the current customer message clearly confirm this exact proposition: {proposition}"},
        "reject": {"type": "noul", "instructions":
                   f"Does the current customer message clearly reject this exact proposition: {proposition}"},
    }
