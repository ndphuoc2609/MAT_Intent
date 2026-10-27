import io
import json
import os
import unittest
from unittest.mock import patch

from app.adapters.jev import JevOpenRouter, MODEL, confirmation_questions, intent_questions
from app.adapters.decision_fast_path import social_ack_candidate, social_ack_eligible


class JevAdapterTests(unittest.TestCase):
    def test_fast_path_is_narrow_and_requires_an_explicit_threshold(self):
        self.assertTrue(social_ack_eligible("Cảm ơn bạn!"))
        self.assertFalse(social_ack_eligible("👍"))
        self.assertFalse(social_ack_eligible("Cảm ơn bạn!", has_pending=True))
        self.assertFalse(social_ack_eligible("Cảm ơn, cho giá Creta?"))
        self.assertIsNone(social_ack_candidate("Cảm ơn!", {"noul": 0.99}, None))
        self.assertIsNone(social_ack_candidate("Cảm ơn!", {"noul": 0.89}, 0.90))
        semantic = social_ack_candidate("Cảm ơn!", {"noul": 0.95}, 0.90)
        self.assertEqual(semantic["operations"][0]["op"], "SOCIAL_ACK")

    def test_intent_questions_keep_multi_intent_as_independent_decisions(self):
        questions = intent_questions()
        self.assertEqual(set(questions), {"price", "promo", "loan", "stock", "spec", "dealer",
                                          "testdrive", "buy", "service", "complaint", "registration", "social"})
        self.assertTrue(all(question["type"] == "noul" for question in questions.values()))
        self.assertEqual(set(confirmation_questions("x")), {"confirm", "reject"})

    def test_pinned_model_uses_decisions_api_and_reports_provider_cost(self):
        response = {"model": "typesafe/jev-1.14-20261014", "provider": "TypeSafe",
                    "answers": {key: {"type": "noul", "noul": 0.91}
                                for key in intent_questions()},
                    "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.00001}}
        requests = []
        def open_request(request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(response).encode())
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             patch("app.adapters.jev.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = open_request
            result = JevOpenRouter().decide({"text": "giá Creta"}, intent_questions(), timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model_requested"], MODEL)
        self.assertEqual(result["model_effective"], response["model"])
        self.assertEqual(result["cost_usd_est"], 0.00001)
        request, timeout = requests[0]
        self.assertEqual(timeout, 3)
        self.assertEqual(request.full_url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(request.get_header("Authorization"), "Bearer key")
        self.assertEqual(json.loads(request.data)["model"], MODEL)

    def test_no_key_and_budget_denial_fail_before_network(self):
        with patch.dict(os.environ):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with patch("app.adapters.jev.urllib.request.build_opener") as opener:
                result = JevOpenRouter().decide("text", intent_questions(), budget_guard=lambda: self.fail())
                self.assertEqual(result["attempts"], 0)
                opener.assert_not_called()
            os.environ["OPENROUTER_API_KEY"] = "key"
            guard = []
            with patch("app.adapters.jev.urllib.request.build_opener") as opener:
                result = JevOpenRouter().decide("text", intent_questions(), budget_guard=lambda: False)
                self.assertEqual(result["attempts"], 0)
                opener.assert_not_called()

    def test_missing_usage_cost_is_unknown_not_hard_coded(self):
        response = {"model": MODEL, "answers": {key: {"type": "noul", "noul": 0.9}
                                                   for key in intent_questions()}}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             patch("app.adapters.jev.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = JevOpenRouter().decide("text", intent_questions())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["cost_usd_est"])
        self.assertTrue(result["cost_unknown"])

    def test_malformed_answer_key_type_and_probability_fall_back(self):
        malformed = [
            {"answers": {"wrong": {"type": "noul", "noul": 0.9}}},
            {"answers": {"price": {"type": "choice", "choice": "yes"}}},
            {"answers": {"price": {"type": "noul", "noul": 1.01}}},
            {"answers": {"price": {"type": "noul", "noul": float("nan")}}},
        ]
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             patch("app.adapters.jev.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = [
                io.BytesIO(json.dumps({"answers": row["answers"]}).encode()) for row in malformed]
            for _ in malformed:
                result = JevOpenRouter().decide("text", {"price": {"type": "noul"}})
                self.assertFalse(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
