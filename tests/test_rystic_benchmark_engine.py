"""Common engine contract with authored responses and no external calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from contextbase.rystic_benchmark import _hash, _run_comparison, _system, _write
from test_rystic_benchmark_audit import Clock, IdentityModel, Model, Tools, plan


class ComparisonEngineTests(unittest.TestCase):
    def test_global_budget_refusals_do_not_invent_model_calls_or_unknown_spend(self):
        from contextbase.study_budget import BudgetError, NoDispatch

        for stage in ("before_call", "complete"):
            class RefusedModel:
                timeout_seconds = 30

                def before_call(self, **kwargs):
                    if stage == "before_call":
                        raise BudgetError("model_call_cap")

                def complete(self, *args, **kwargs):
                    if stage == "before_call":
                        raise AssertionError("Pre-dispatch refusal must prevent complete")
                    raise NoDispatch("study_deadline")

            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                report = _run_comparison(None, None, plan(), Path(directory) / "refused.json",
                    verify=lambda: None, client=RefusedModel(), tools_factory=lambda arm: Tools())
            self.assertEqual(report["stop_reason"], "global_budget_refused")
            self.assertEqual(report["invocations"], 0)
            self.assertEqual(report["known_cost_usd"], 0)
            self.assertTrue(report["cost_complete"])
            self.assertEqual(report["trials"][0]["inferences"], [])
            self.assertFalse(report["execution_complete"])
            self.assertTrue(all(trial["started_at"] is None for trial in report["trials"][1:]))

    def test_validated_phase_caller_uses_same_loop_and_timed_verification(self):
        frozen = plan()
        for key in ("snapshot", "runtime", "spec"):
            del frozen[key]
        original = deepcopy(frozen)
        clock = Clock()
        prompts, checks = [], []

        class AuthoredModel(Model):
            def complete(self, system, messages, **kwargs):
                prompts.append((system, deepcopy(messages)))
                return super().complete(system, messages, **kwargs)

        pair = SimpleNamespace(stripe=SimpleNamespace(url="http://127.0.0.1:18981"),
                               linear=SimpleNamespace(url="http://127.0.0.1:18982"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "comparison.json"

            def verify():
                self.assertTrue(output.is_file())
                checks.append(len(prompts))
                clock.now += 2

            with patch("contextbase.rystic_benchmark.time.perf_counter", clock), \
                 patch("contextbase.rystic_benchmark._snapshot_matches", side_effect=AssertionError("Caller owns verification")):
                report = _run_comparison(Path(directory) / "unused", pair, frozen, output,
                    verify=verify, client=AuthoredModel(), tools_factory=lambda arm: Tools(),
                    kind="synthetic_phase_comparison_test")
            self.assertEqual(json.loads(output.read_text()), report)
        self.assertEqual(frozen, original)
        self.assertEqual(report["kind"], "synthetic_phase_comparison_test")
        self.assertEqual(checks, [0, len(frozen["queue"])])
        self.assertEqual(report["preflight_seconds"], 2)
        self.assertEqual(report["postflight_seconds"], 2)
        self.assertEqual(report["elapsed_seconds"], 4)
        self.assertTrue(report["execution_complete"])
        self.assertEqual(report["invocations"], len(frozen["queue"]))
        self.assertEqual(prompts[0][0], _system([], frozen["tasks"][0], frozen["budgets"]["max_turns"]))
        self.assertFalse(report["release_gates"]["claim_eligible"])

    def test_verification_failure_preserves_all_planned_failures_without_model_work(self):
        frozen = plan()
        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic_benchmark.ClaudeCLI", side_effect=AssertionError("No provider work")) as model, \
             patch("contextbase.rystic_benchmark._snapshot_matches", side_effect=AssertionError("No pilot check")):
            def verify():
                raise ValueError("Authored frozen phase mismatch")

            report = _run_comparison(None, None, frozen, Path(directory) / "comparison.json",
                verify=verify, tools_factory=lambda arm: self.fail("No tool work"),
                kind="synthetic_phase_comparison_test")
        model.assert_not_called()
        self.assertEqual(report["stop_reason"], "preflight_failed")
        self.assertEqual(report["invocations"], 0)
        self.assertEqual(len(report["trials"]), len(frozen["queue"]))
        self.assertTrue(all(trial["status"] == "skipped_preflight_failed" and not trial["correct"]
                            for trial in report["trials"]))
        self.assertTrue(all(trial["started_at"] is None and trial["finished_at"] is None
                            and trial["tool_invocations"] == [] for trial in report["trials"]))
        self.assertIsNotNone(report["preflight_started_at"])
        self.assertIsNotNone(report["preflight_finished_at"])
        self.assertIsNone(report["postflight_started_at"])
        self.assertIsNone(report["postflight_finished_at"])
        self.assertIsNotNone(report["finished_at"])

    def test_observed_timestamps_cover_inferences_tools_and_cleanup_without_changing_trace(self):
        frozen, clock = plan(), Clock()
        original = deepcopy(frozen)
        base = datetime(2026, 10, 9, tzinfo=timezone.utc)

        class WallClock:
            @staticmethod
            def now(tz):
                return base + timedelta(seconds=clock.now)

        class TimedTools(Tools):
            def __enter__(self):
                clock.now += 0.1
                return self

            def __exit__(self, *args):
                clock.now += 0.1

            def call(self, name, arguments):
                clock.now += 0.2
                self.trace.append({"tool": name, "status": "authored_trace"})
                return {"result": "synthetic"}

        class TimedModel(Model):
            def __init__(self):
                super().__init__(clock=clock, delay=0.3)
                self.calls = 0

            def complete(self, *args, **kwargs):
                self.calls += 1
                self.action = ({"tools": [{"tool": "example", "arguments": {}}]} if self.calls % 2
                               else {"answer": {"total": 7}})
                result = super().complete(*args, **kwargs)
                result.update(started_at="untrusted provider start", finished_at="untrusted provider finish",
                              measured_elapsed_seconds=999)
                return result

        def verify():
            clock.now += 0.4

        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic_benchmark.time.perf_counter", clock), \
             patch("contextbase.rystic_benchmark.datetime", WallClock):
            report = _run_comparison(None, None, frozen, Path(directory) / "timed.json", verify=verify,
                client=TimedModel(), tools_factory=lambda arm: TimedTools(), expected_model_id="synthetic-audit-model")
        self.assertEqual(frozen, original)
        stamp = datetime.fromisoformat
        self.assertEqual(stamp(report["finished_at"]) - stamp(report["started_at"]), timedelta(seconds=report["elapsed_seconds"]))
        self.assertEqual(stamp(report["preflight_finished_at"]) - stamp(report["preflight_started_at"]), timedelta(seconds=0.4))
        self.assertTrue(report["model_identity_consistent"])
        self.assertTrue(report["execution_complete"])
        for index, trial in enumerate(report["trials"]):
            self.assertEqual(trial["queue_index"], index)
            self.assertEqual(trial["queue_entry"], frozen["queue"][index])
            self.assertEqual(trial["trial_id"], _hash({"plan_sha256": frozen["plan_sha256"],
                "queue_index": index, "queue_entry": frozen["queue"][index]}))
            self.assertEqual(trial["trace"], [{"tool": "example", "status": "authored_trace"}])
            self.assertAlmostEqual(trial["elapsed_seconds"], 1)
            self.assertEqual(stamp(trial["finished_at"]) - stamp(trial["started_at"]), timedelta(seconds=1))
            self.assertEqual([item["inference_index"] for item in trial["inferences"]], [0, 1])
            for inference in trial["inferences"]:
                self.assertAlmostEqual(inference["measured_elapsed_seconds"], 0.3)
                self.assertEqual(stamp(inference["finished_at"]) - stamp(inference["started_at"]), timedelta(seconds=0.3))
                self.assertGreaterEqual(stamp(inference["started_at"]), stamp(trial["started_at"]))
                self.assertLessEqual(stamp(inference["finished_at"]), stamp(trial["finished_at"]))
            tool = trial["tool_invocations"][0]
            self.assertEqual((tool["tool_index"], tool["inference_index"], tool["tool"], tool["status"]), (0, 0, "example", "returned"))
            self.assertEqual(tool["arguments_sha256"], _hash({}))
            self.assertAlmostEqual(tool["elapsed_seconds"], 0.2)
            self.assertEqual(stamp(tool["finished_at"]) - stamp(tool["started_at"]), timedelta(seconds=0.2))

    def test_repeated_queue_entries_keep_distinct_stable_trial_identities(self):
        frozen = plan()
        frozen["queue"] = [deepcopy(frozen["queue"][0]), deepcopy(frozen["queue"][0])]
        with tempfile.TemporaryDirectory() as directory:
            reports = [_run_comparison(None, None, frozen, Path(directory) / (str(index) + ".json"), verify=lambda: None,
                                      client=Model(), tools_factory=lambda arm: Tools()) for index in range(2)]
        ids = [[trial["trial_id"] for trial in report["trials"]] for report in reports]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0][0], ids[0][1])
        self.assertEqual(reports[0]["trials"][0]["queue_entry"], reports[0]["trials"][1]["queue_entry"])

    def test_initial_report_write_is_inside_the_actual_measured_phase(self):
        clock = Clock()
        base = datetime(2026, 10, 9, tzinfo=timezone.utc)

        class WallClock:
            @staticmethod
            def now(tz):
                return base + timedelta(seconds=clock.now)

        def write(path, value, *, exclusive=False):
            if exclusive:
                clock.now += 0.5
            return _write(path, value, exclusive=exclusive)

        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic_benchmark.time.perf_counter", clock), \
             patch("contextbase.rystic_benchmark.datetime", WallClock), \
             patch("contextbase.rystic_benchmark._write", side_effect=write):
            report = _run_comparison(None, None, plan(), Path(directory) / "measured.json", verify=lambda: None,
                                    client=Model(), tools_factory=lambda arm: Tools())
        self.assertEqual(report["elapsed_seconds"], 0.5)
        self.assertEqual(datetime.fromisoformat(report["finished_at"]) - datetime.fromisoformat(report["started_at"]),
                         timedelta(seconds=0.5))
        self.assertEqual(datetime.fromisoformat(report["preflight_started_at"]), base + timedelta(seconds=0.5))

    def test_expected_model_mismatch_stops_spending_and_retains_paid_cost(self):
        for identities in ({"other-model": {}}, {"synthetic-audit-model": {}, "other-model": {}}):
            with self.subTest(identities=identities), tempfile.TemporaryDirectory() as directory:
                report = _run_comparison(None, None, plan(), Path(directory) / "mismatch.json", verify=lambda: None,
                    client=IdentityModel([identities]), tools_factory=lambda arm: Tools(), expected_model_id="synthetic-audit-model")
            self.assertEqual(report["invocations"], 1)
            self.assertEqual(report["stop_reason"], "model_identity_mismatch")
            self.assertFalse(report["model_identity_consistent"])
            self.assertFalse(report["execution_complete"])
            self.assertEqual(report["known_cost_usd"], 0.01)
            self.assertEqual(report["trials"][0]["known_cost_usd"], 0.01)
            self.assertFalse(report["trials"][0]["correct"])
            self.assertIsNotNone(report["trials"][0]["inferences"][0]["finished_at"])
            self.assertTrue(all(trial["started_at"] is None for trial in report["trials"][1:]))

    def test_model_mismatch_is_false_even_when_cost_failure_has_stop_precedence(self):
        for cost, reason in ((None, "inference_cost_unknown"), (1, "reported_cost_exceeded_budget")):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as directory:
                report = _run_comparison(None, None, plan(max_cost_usd=0.1), Path(directory) / "cost.json",
                    verify=lambda: None, client=Model(cost=cost), tools_factory=lambda arm: Tools(), expected_model_id="other-model")
            self.assertEqual(report["stop_reason"], reason)
            self.assertFalse(report["model_identity_consistent"])
            self.assertFalse(report["execution_complete"])
            self.assertEqual(report["known_cost_usd"], cost or 0)
            self.assertEqual(report["cost_complete"], cost is not None)

    def test_failed_tool_invocation_retains_its_time_and_safe_error_type(self):
        action = {"tools": [{"tool": "example", "arguments": {}}]}
        with tempfile.TemporaryDirectory() as directory:
            report = _run_comparison(None, None, plan(), Path(directory) / "failed-tool.json", verify=lambda: None,
                client=Model(action=action), tools_factory=lambda arm: Tools())
        invocation = report["trials"][0]["tool_invocations"][0]
        self.assertEqual(invocation["status"], "raised")
        self.assertEqual(invocation["error_type"], "RuntimeError")
        self.assertIsNotNone(invocation["started_at"])
        self.assertIsNotNone(invocation["finished_at"])
        self.assertGreaterEqual(invocation["elapsed_seconds"], 0)
        self.assertNotIn("Synthetic transport call failure", json.dumps(invocation))

    def test_invalid_expected_model_id_refuses_before_provider_or_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid.json"
            for identity in ("", " ", " model", True, 7):
                with self.subTest(identity=identity), self.assertRaises(ValueError):
                    _run_comparison(None, None, plan(), target, verify=lambda: self.fail("No verification"),
                                    expected_model_id=identity)
            self.assertFalse(target.exists())
