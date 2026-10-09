"""Tests for independent scoring, semantic traps, and the actual tool paths."""
import json
from pathlib import Path
import tempfile
import unittest

from contextbase.benchmark import (TABLES, TASKS, gold_answers, load_rows,
                                   run_model_benchmark, run_replay, score_number)
from contextbase.model import parse_action
from contextbase.workspace import init_demo


class BenchmarkTests(unittest.TestCase):
    def test_python_gold_handles_paid_status_fiscal_edges_and_duplicate_joins(self):
        rows = {table: [] for table in TABLES}
        rows["billing.subscriptions"] = [
            {"account_id": "customer_1", "status": "active", "mrr": 10},
            {"account_id": "customer_1", "status": "active", "mrr": 20},
            {"account_id": "customer_2", "status": "active", "mrr": 0},
            {"account_id": "customer_3", "status": "active", "mrr": -10},
            {"account_id": "customer_4", "status": "canceled", "mrr": 1000},
            {"account_id": "customer_5", "status": "active", "mrr": 5},
        ]
        rows["crm.opportunities"] = [
            {"stage": stage, "arr": amount, "close_date": date}
            for stage, amount, date in [
                ("S1", 9000, "2026-09-01"), ("S2", 10, "2026-08-01"),
                ("S3", 20, "2026-10-31"), ("S4", 5000, "2026-11-01"),
                ("S2", 5000, "2026-07-31"),
            ]
        ]
        rows["product.usage"] = [
            {"account_id": "customer_1", "period": period, "active_users": value}
            for period, value in [("previous", 60), ("previous", 40), ("current", 35), ("current", 35)]
        ] + [{"account_id": "customer_5", "period": "previous", "active_users": 100}]
        rows["support.tickets"] = [
            {"account_id": account, "status": "open", "priority": "high"}
            for account in ["customer_1", "customer_1", "customer_5"]
        ]
        self.assertEqual(gold_answers(rows), {
            "active_arr": 420.0, "active_accounts": 2,
            "eligible_pipeline": 30.0, "at_risk_arr": 360.0,
        })

    def test_structured_score_rejects_incidental_numbers_and_non_finite(self):
        for answer in ["The old total was 100, but now 500", "100", True, None,
                       float("nan"), float("inf"), 101]:
            self.assertFalse(score_number(answer, 100))
        self.assertTrue(score_number(100.004, 100))
        self.assertFalse(score_number(100.006, 100))

    def test_actions_are_bounded_and_not_arbitrary_commands(self):
        self.assertEqual(parse_action('{"answer":5}'), {"answer": 5})
        self.assertEqual(len(parse_action('{"tool":"catalog","arguments":{}}')["tools"]), 1)
        for value in ["[]", '{"answer":"5"}', '{"answer":true}', '{"shell":"whoami"}',
                      '{"answer":NaN}', '{"answer":Infinity}',
                      '{"tools":[]}', '{"tools":' + json.dumps([{"tool": "catalog", "arguments": {}}] * 5) + '}']:
            with self.assertRaises(ValueError):
                parse_action(value)

    def test_replay_all_arms_match_independent_gold_with_pagination(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = init_demo(Path(temporary) / "demo", accounts=12)
            report = run_replay(workspace, page_size=3)
            self.assertEqual(report["kind"], "authored_plan_replay")
            self.assertEqual(len(report["trials"]), 16)
            self.assertTrue(all(t["plan_matches_gold"] for t in report["trials"]))
            self.assertTrue(any(t["tool_calls"] > 4 for t in report["trials"] if t["arm"] == "per_source"))
            self.assertNotIn("accuracy", report)

    def test_model_harness_uses_tool_results_and_keeps_native_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = init_demo(Path(temporary) / "demo", accounts=4)
            sql = TASKS[0]["sql"]

            class FakeModel:
                def complete(self, system, messages, max_cost_usd):
                    if len(messages) == 1:
                        action = {"tools": [{"tool": "query", "arguments": {"sql": sql}}]}
                    else:
                        value = messages[-1]["content"][0]["result"]["rows"][0]["answer"]
                        action = {"answer": value}
                    return {"action": action, "cost_usd": 0.01,
                            "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 5},
                            "model_usage": {"test-double": {}}, "is_error": False}

            output = Path(temporary) / "report.json"
            report = run_model_benchmark(workspace, output, task_ids=["active_arr"],
                                         arms=["sql"], client=FakeModel())
            self.assertTrue(report["trials"][0]["correct"])
            self.assertEqual(report["invocations"], 2)
            self.assertEqual(report["cost_usd"], 0.02)
            self.assertEqual(report["trials"][0]["inferences"][0]["usage"]["cache_read_input_tokens"], 20)
            self.assertTrue(output.exists())

    def test_live_runner_rejects_private_edits_before_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = init_demo(Path(temporary) / "demo", accounts=4)
            with workspace._connect(writable=True) as connection:
                connection.execute("UPDATE crm.accounts SET name='changed fixture' WHERE account_id='customer_001'")

            class MustNotRun:
                def complete(self, *args, **kwargs):
                    raise AssertionError("Modified data reached a model")

            with self.assertRaisesRegex(ValueError, "modified synthetic data"):
                run_model_benchmark(workspace, Path(temporary) / "report.json", client=MustNotRun())

    def test_unknown_spend_stops_all_trials_and_retains_known_subtotal(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = init_demo(Path(temporary) / "demo", accounts=4)
            for missing in [None, float("nan"), float("inf"), -1, RuntimeError("sensitive exception fixture")]:
                with self.subTest(missing=type(missing).__name__):
                    class FakeModel:
                        calls = 0

                        def complete(self, system, messages, max_cost_usd):
                            self.calls += 1
                            if self.calls == 1:
                                return {"action": {"tools": [{"tool": "catalog", "arguments": {}}]}, "cost_usd": 0.02}
                            if isinstance(missing, Exception):
                                raise missing
                            return {"action": {"answer": 1}, "cost_usd": missing}

                    client = FakeModel()
                    result = run_model_benchmark(workspace, Path(temporary) / "unknown.json",
                                                 task_ids=["active_arr"], arms=["sql", "metrics"], client=client)
                    self.assertEqual(client.calls, 2)
                    self.assertEqual(result["completed_trials"], 1)
                    self.assertEqual(result["stop_reason"], "inference_cost_unknown")
                    self.assertIsNone(result["cost_usd"])
                    self.assertEqual(result["known_cost_usd"], 0.02)
                    self.assertEqual(result["trials"][0]["known_cost_usd"], 0.02)
                    self.assertTrue(result["spend_unknown"])
                    self.assertFalse(result["cost_complete"])
                    text = json.dumps(result, allow_nan=False)
                    self.assertNotIn("sensitive exception fixture", text)

    def test_reported_cost_of_failed_inference_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = init_demo(Path(temporary) / "demo", accounts=4)

            class FailedModel:
                def complete(self, *args, **kwargs):
                    return {"is_error": True, "cost_usd": 0.07,
                            "action": {"answer": 1}, "usage": {"input_tokens": 30}}

            result = run_model_benchmark(workspace, Path(temporary) / "failed.json",
                                         task_ids=["active_arr"], arms=["sql"], client=FailedModel())
            self.assertEqual(result["cost_usd"], 0.07)
            self.assertEqual(result["trials"][0]["cost_usd"], 0.07)
            self.assertEqual(result["trials"][0]["status"], "inference_error")
            self.assertIsNone(result["trials"][0]["answer"])
            self.assertFalse(result["spend_unknown"])

    def test_all_budget_inputs_are_finite_and_have_valid_types(self):
        for key in ["max_cost_usd", "max_wall_seconds"]:
            for value in [float("nan"), float("inf"), -float("inf"), True, "1", 0]:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    run_model_benchmark(None, "unused.json", **{key: value})
        for key in ["max_turns", "max_calls"]:
            for value in [True, 1.5, float("nan"), "1", 0]:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    run_model_benchmark(None, "unused.json", **{key: value})


if __name__ == "__main__":
    unittest.main()
