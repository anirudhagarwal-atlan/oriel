import json
import subprocess
import unittest
from unittest.mock import patch

from contextbase.model import ClaudeCLI, parse_action


class ModelTests(unittest.TestCase):
    def test_nonzero_cli_exit_preserves_reported_spend_without_stderr(self):
        native = {"is_error": True, "total_cost_usd": 0.037,
                  "usage": {"input_tokens": 120, "output_tokens": 3},
                  "modelUsage": {"test-model": {"costUSD": 0.037}}, "result": ""}
        completed = subprocess.CompletedProcess([], 1, json.dumps(native), "sensitive stderr fixture")
        with patch("contextbase.model.subprocess.run", return_value=completed):
            result = ClaudeCLI(executable="/fake/claude").complete("Synthetic test", [])
        self.assertTrue(result["is_error"])
        self.assertEqual(result["cost_usd"], 0.037)
        self.assertEqual(result["usage"]["input_tokens"], 120)
        self.assertFalse(result["spend_unknown"])
        self.assertNotIn("sensitive stderr fixture", json.dumps(result))

    def test_timeouts_and_invalid_output_have_unknown_spend(self):
        client = ClaudeCLI(executable="/fake/claude")
        timeout = subprocess.TimeoutExpired("synthetic command", 1, stderr="sensitive stderr fixture")
        with patch("contextbase.model.subprocess.run", side_effect=timeout):
            result = client.complete("Synthetic test", [])
        self.assertTrue(result["spend_unknown"])
        self.assertIsNone(result["cost_usd"])
        self.assertNotIn("sensitive stderr fixture", json.dumps(result))
        with patch("contextbase.model.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not JSON", "")):
            result = client.complete("Synthetic test", [])
        self.assertTrue(result["spend_unknown"])
        self.assertTrue(result["is_error"])

    def test_missing_and_invalid_cost_are_never_zero(self):
        for cost in [None, -1, True, "0.1"]:
            native = {"total_cost_usd": cost, "result": '{"answer":2}'}
            with self.subTest(cost=cost), patch("contextbase.model.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(native), "")):
                result = ClaudeCLI(executable="/fake/claude").complete("Synthetic test", [])
                self.assertIsNone(result["cost_usd"])
                self.assertTrue(result["spend_unknown"])

    def test_budget_and_timeout_validation_happens_before_subprocess(self):
        for value in [float("nan"), float("inf"), -float("inf"), True, 0, -1, "1"]:
            with self.subTest(value=value), patch("contextbase.model.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    ClaudeCLI(executable="/fake/claude").complete("Synthetic test", [], max_cost_usd=value)
                with self.assertRaises(ValueError):
                    ClaudeCLI(executable="/fake/claude", timeout_seconds=value)
                run.assert_not_called()

    def test_safe_json_rejects_nonfinite_nested_numbers_and_duplicate_keys(self):
        invalid = [
            '{"answer":1e999}', '{"answer":true}', '{"answer":1,"answer":2}',
            '{"tools":[{"tool":"query","arguments":{"limit":NaN}}]}',
            '{"tools":[{"tool":"list_records","arguments":{"filters":{"value":Infinity}}}]}',
            '```',
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_action(value, allow_text_answer=True)
        self.assertEqual(parse_action('{"answer":"Active ARR is $12,000."}', allow_text_answer=True)["answer"],
                         "Active ARR is $12,000.")
        self.assertEqual(parse_action('{"answer":-1.5}')["answer"], -1.5)


if __name__ == "__main__":
    unittest.main()
