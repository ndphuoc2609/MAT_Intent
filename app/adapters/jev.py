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
                raise ValueError("Jev response must be an object")
            answers = validate_answers(data.get("answers"), questions)
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            cost = usage.get("cost")
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                cost = None
            return {"ok": True, "answers": answers, "usage": usage,
                    "cost_usd_est": float(cost) if cost is not None else None,
                    "cost_unknown": cost is None, "model_requested": self.model,
                    "model_effective": data.get("model", self.model), "provider": data.get("provider"),
                    "latency_ms": latency_ms, "attempts": 1, "error": None}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            return {"ok": False, "error": str(error)[:300], "attempts": 1,
                    "model_requested": self.model, "latency_ms": (time.perf_counter() - start) * 1000,
                    "cost_usd_est": None, "cost_unknown": True}


def confirmation_questions(proposition):
    """Separate yes/no gates; raw probabilities require per-label calibration."""
    return {
        "confirm": {"type": "noul", "instructions":
                    f"Does the current customer message clearly confirm this exact proposition: {proposition}"},
        "reject": {"type": "noul", "instructions":
                   f"Does the current customer message clearly reject this exact proposition: {proposition}"},
    }
