"""Independent runner audit: no model calls, simulators, or external traffic."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from contextbase.rystic_benchmark import ARMS, PLAN_SPEC, _hash, _runtime, run_pilot


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Tools:
    def __init__(self, *, fail_close=False):
        self.trace = []
        self.fail_close = fail_close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.fail_close:
            raise RuntimeError("Synthetic transport cleanup failure")

    def definitions(self):
        return []

    def call(self, name, arguments):
        self.trace.append({"tool": name, "status": "failed", "elapsed_ms": 5})
        raise RuntimeError("Synthetic transport call failure")


class Model:
    def __init__(self, *, cost=0.01, clock=None, delay=0, action=None):
        self.cost, self.clock, self.delay, self.action = cost, clock, delay, action

    def complete(self, system, messages, max_cost_usd):
        if self.clock:
            self.clock.now += self.delay
        return {"cost_usd": self.cost, "cost_known": True, "is_error": False,
                "model_usage": {"synthetic-audit-model": {"inputTokens": 1}},
                "response": json.dumps(self.action or {"answer": {"total": 7}})}


class IdentityModel(Model):
    def __init__(self, identities):
        super().__init__()
        self.identities = iter(identities)

    def complete(self, *args, **kwargs):
        response = super().complete(*args, **kwargs)
        response["model_usage"] = next(self.identities)
        return response


def plan(**budget_changes):
    # Deliberately simple gold lets tests isolate runner invariants from scoring.
    tasks = [{"id": f"audit_{index}", "question": "Synthetic audit question.",
              "answer_schema": {"type": "object"}, "expected": {"total": 7}}
             for index in range(4)]
    value = {"spec": PLAN_SPEC, "kind": "development_pilot", "held_out": False,
             "arms": list(ARMS), "tasks": tasks, "runtime": _runtime(),
             "queue": [{"task": task["id"], "arm": arm} for task in tasks for arm in ARMS],
             "budgets": {"max_turns": 2, "max_calls": 20, "max_cost_usd": 3,
                         "max_wall_seconds": 120, "trial_seconds": 30, **budget_changes},
             "snapshot": {"refresh_mode": "full_cohort_replace"},
             "accounting": {}, "limitations": []}
    value["plan_sha256"] = _hash(value)
    return value


class RysticBenchmarkAuditTests(unittest.TestCase):
    def run_case(self, *, frozen=None, model=None, factory=None, checks=None, clock=None, default_factory=False):
        frozen = frozen or plan()
        pair = SimpleNamespace(stripe=SimpleNamespace(url="http://127.0.0.1:18981"),
                               linear=SimpleNamespace(url="http://127.0.0.1:18982"))
        with tempfile.TemporaryDirectory() as root, patch(
                "contextbase.rystic_benchmark._snapshot_matches", side_effect=checks) as verifier:
            args = dict(approve=frozen["plan_sha256"], allow_model_data=True,
                        client=model or Model(), tools_factory=None if default_factory else factory or (lambda arm: Tools()))
            workspace = Path(root) / "unused-workspace"
            if clock:
                with patch("contextbase.rystic_benchmark.time.perf_counter", clock):
                    report = run_pilot(workspace, pair, frozen, Path(root) / "audit.json", **args)
            else:
                report = run_pilot(workspace, pair, frozen, Path(root) / "audit.json", **args)
            self.assertEqual(json.loads((Path(root) / "audit.json").read_text()), report)
            self.assertGreaterEqual(verifier.call_count, 1)
            return report

    def test_default_factory_supplies_simulator_urls_only_to_source_arm(self):
        from contextbase.evaluation_tools import StdioMCPClient
        constructed = []

        class OfflineClient(StdioMCPClient):
            def __init__(self, *args, **kwargs):
                # Retain the real constructor's URL and arm validation.
                super().__init__(*args, **kwargs)
                constructed.append(self.arm)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def definitions(self):
                return []

        with patch("contextbase.evaluation_tools.StdioMCPClient", OfflineClient):
            report = self.run_case(default_factory=True)
        self.assertEqual(constructed.count("sql_context"), 4)
        self.assertEqual(constructed.count("rystic_source"), 4)
        self.assertTrue(report["execution_complete"])
        self.assertTrue(all(trial["correct"] for trial in report["trials"]))

    def test_postflight_failure_cannot_complete_or_preserve_correct_answers(self):
        report = self.run_case(checks=[None, ValueError("Changed synthetic source")])
        self.assertEqual(report["source_stability"], "postflight_failed")
        self.assertFalse(any(trial["correct"] for trial in report["trials"]))
        self.assertFalse(report["execution_complete"])

    def test_cleanup_failure_cannot_preserve_a_success_score(self):
        report = self.run_case(factory=lambda arm: Tools(fail_close=True))
        self.assertTrue(all(trial["status"] == "tool_or_transport_error" for trial in report["trials"]))
        self.assertFalse(any(trial["correct"] for trial in report["trials"]))

    def test_failed_tool_calls_keep_their_measured_trace(self):
        action = {"tools": [{"tool": "example", "arguments": {}}]}
        report = self.run_case(model=Model(action=action))
        self.assertEqual(report["trials"][0]["status"], "tool_or_transport_error")
        self.assertEqual(report["trials"][0]["trace"], [{"tool": "example", "status": "failed", "elapsed_ms": 5}])

    def test_known_cost_overshoot_invalidates_answer_and_stops(self):
        report = self.run_case(frozen=plan(max_cost_usd=0.05), model=Model(cost=0.1))
        self.assertEqual(report["invocations"], 1)
        self.assertIsNotNone(report["stop_reason"])
        self.assertFalse(report["trials"][0]["correct"])
        self.assertGreater(report["known_cost_usd"], report["plan"]["budgets"]["max_cost_usd"])

    def test_answer_after_global_deadline_fails_even_inside_trial_deadline(self):
        clock = Clock()
        report = self.run_case(frozen=plan(max_wall_seconds=1, trial_seconds=0.8),
                               model=Model(clock=clock, delay=0.6), clock=clock)
        self.assertTrue(report["trials"][0]["correct"])
        self.assertFalse(report["trials"][1]["correct"])
        self.assertEqual(report["stop_reason"], "wall_clock_budget")

    def test_missing_or_changing_model_identity_stops_further_spend(self):
        for second, reason in (({}, "model_identity_unavailable"),
                               ({"other-audit-model": {}}, "model_identity_changed")):
            with self.subTest(reason=reason):
                model = IdentityModel([{"synthetic-audit-model": {}}, second])
                report = self.run_case(model=model)
                self.assertEqual(report["invocations"], 2)
                self.assertEqual(report["stop_reason"], reason)
                self.assertFalse(report["execution_complete"])
                self.assertFalse(report["trials"][1]["correct"])
                if reason == "model_identity_unavailable":
                    self.assertIsNone(report["model_identity_consistent"])

    def test_unknown_inference_spend_stops_and_is_not_treated_as_zero(self):
        report = self.run_case(model=Model(cost=None))
        self.assertEqual(report["invocations"], 1)
        self.assertEqual(report["stop_reason"], "inference_cost_unknown")
        self.assertFalse(report["cost_complete"])
        self.assertFalse(report["trials"][0]["cost_complete"])
        self.assertFalse(report["execution_complete"])
        self.assertFalse(any(trial["correct"] for trial in report["trials"]))

    def test_zero_calls_after_preflight_failure(self):
        report = self.run_case(checks=ValueError("Synthetic source mismatch"))
        self.assertEqual(report["invocations"], 0)
        self.assertEqual(report["stop_reason"], "preflight_failed")
        self.assertFalse(report["execution_complete"])
        self.assertTrue(all(trial["status"] == "skipped_preflight_failed" for trial in report["trials"]))

    def test_global_deadline_stops_a_multi_tool_batch_before_next_call(self):
        clock = Clock()
        called = []

        class SlowTools(Tools):
            def call(self, name, arguments):
                called.append(name)
                clock.now += 0.6
                return {"result": "synthetic"}

        class Scripted(Model):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def complete(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    clock.now += 0.6
                else:
                    self.action = {"tools": [{"tool": "example", "arguments": {}}] * 4}
                return super().complete(*args, **kwargs)

        report = self.run_case(frozen=plan(max_wall_seconds=1, trial_seconds=1), model=Scripted(),
                               factory=lambda arm: SlowTools(), clock=clock)
        self.assertEqual(called, ["example"])
        self.assertEqual(report["stop_reason"], "wall_clock_budget")
        self.assertFalse(report["trials"][1]["correct"])


if __name__ == "__main__":
    unittest.main()
