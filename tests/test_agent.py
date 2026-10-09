import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.agent import ask
from contextbase.tools import Toolkit
from contextbase.workspace import init_demo


def inference(action, cost=0.01, **extra):
    return {"response": json.dumps(action), "cost_usd": cost,
            "usage": {"input_tokens": 5, "output_tokens": 4}, **extra}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, messages, max_cost_usd):
        self.calls.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = init_demo(Path(self.temp.name) / "demo", accounts=4)

    def test_normal_final_answer_and_usage_are_preserved(self):
        model = FakeModel([inference({"answer": "Active ARR is $12,000 in this local snapshot."})])
        result = ask(self.w, "What is active ARR?", client=model)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "Active ARR is $12,000 in this local snapshot.")
        self.assertEqual(result["reported_cost_usd"], 0.01)
        self.assertEqual(result["known_cost_usd"], 0.01)
        self.assertFalse(result["spend_unknown"])

    def test_unknown_cost_stops_without_executing_tools(self):
        for cost in [None, float("nan"), float("inf"), -1, True]:
            with self.subTest(cost=cost):
                model = FakeModel([inference({"tool": "catalog", "arguments": {}}, cost=cost)])
                with patch.object(Toolkit, "call") as execute:
                    result = ask(self.w, "Show the catalog", client=model)
                execute.assert_not_called()
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(result["status"], "inference_cost_unknown")
                self.assertIsNone(result["reported_cost_usd"])
                self.assertTrue(result["spend_unknown"])
                json.dumps(result, allow_nan=False)

    def test_exception_retains_prior_known_spend_and_stops(self):
        model = FakeModel([
            inference({"tool": "catalog", "arguments": {}}, cost=0.03),
            RuntimeError("sensitive exception fixture"),
        ])
        result = ask(self.w, "Show active ARR", client=model)
        self.assertEqual(result["known_cost_usd"], 0.03)
        self.assertIsNone(result["reported_cost_usd"])
        self.assertTrue(result["spend_unknown"])
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn("sensitive exception fixture", json.dumps(result))

    def test_provider_error_preserves_cost_and_does_not_accept_answer(self):
        model = FakeModel([inference({"answer": "Must not accept this"}, cost=0.04, is_error=True)])
        result = ask(self.w, "Show active ARR", client=model)
        self.assertEqual(result["status"], "inference_error")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["reported_cost_usd"], 0.04)
        self.assertFalse(result["spend_unknown"])

    def test_nonfinite_answers_and_duplicate_keys_are_rejected(self):
        for raw in ['{"answer":NaN}', '{"answer":1e999}', '{"answer":true}', '{"answer":"a","answer":"b"}']:
            with self.subTest(raw=raw):
                model = FakeModel([{"response": raw, "cost_usd": 0.01}])
                result = ask(self.w, "Show active ARR", client=model, max_turns=1)
                self.assertEqual(result["status"], "invalid_model_response")
                self.assertIsNone(result["answer"])

    def test_tool_response_byte_limit_applies_before_next_model_call(self):
        model = FakeModel([
            inference({"tool": "query", "arguments": {"sql": "SELECT 1"}}),
            inference({"answer": "The result was too large; use an aggregate."}),
        ])
        oversized = {"rows": [{"description": "é" * 31_000}]}
        with patch.object(Toolkit, "call", return_value=oversized):
            result = ask(self.w, "Show data", client=model)
        sent = json.dumps(model.calls[1], ensure_ascii=False)
        self.assertNotIn("é" * 20, sent)
        self.assertLess(len(sent.encode()), 60_000)
        self.assertTrue(result["trace"][0]["result"]["truncated"])
        self.assertGreater(result["trace"][0]["result"]["omitted_response_bytes"], 60_000)

    def test_invalid_budgets_and_turns_do_not_call_model(self):
        model = FakeModel([])
        for cost in [float("nan"), float("inf"), -float("inf"), True, 0, -1, "1"]:
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                ask(self.w, "Show data", max_cost_usd=cost, client=model)
        for turns in [True, 1.5, float("nan"), 0, 13]:
            with self.subTest(turns=turns), self.assertRaises(ValueError):
                ask(self.w, "Show data", max_turns=turns, client=model)
        self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
