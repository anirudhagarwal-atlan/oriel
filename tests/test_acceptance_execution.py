"""Actual shared-loop receipts consumed offline, using authored model outputs.

Only the full protocol compiler gate is stubbed; acceptance binding checks,
execution plan regeneration, clocks, tool dispatch, scoring and collection all
run normally. These scripted responses are never model/acceptance evidence.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_report import _interval, _phase, prepare_execution_plan
from contextbase.rystic_benchmark import _hash, _run_comparison
import test_acceptance_binding as binding_tests


class ScriptedTools:
    def __init__(self, fail=False):
        self.trace = []
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def definitions(self):
        return [{"name": "inspect_fixture", "description": "Authored test tool", "inputSchema": {"type": "object"}}]

    def call(self, name, arguments):
        self.trace.append({"tool": name, "status": "test_only"})
        if self.fail:
            raise ValueError("Authored test failure")
        return {"stored_call_id": 1, "result": {"fixture": "synthetic"}}


class ScriptedModel:
    def __init__(self, plan, *, mode="normal"):
        self.answers = {task["question"]: task["expected"] for task in plan["tasks"]}
        self.calls = 0
        self.mode = mode

    def complete(self, system, messages, **kwargs):
        self.calls += 1
        first = len(messages) == 1
        action = ({"tools": [{"tool": "inspect_fixture", "arguments": {"label": "synthetic-é", "index": index}}
                             for index in (1, 2)]} if first else {"answer": self.answers[messages[0]["content"]]})
        return {"response": json.dumps(action), "cost_usd": None if self.mode == "unknown_second" and self.calls == 2 else .01,
                "model_usage": {"wrong-test-model" if self.mode == "wrong_model" else "scripted-acceptance-test": {}}}


class AcceptanceExecutionTests(unittest.TestCase):
    setUp = binding_tests.AcceptanceBindingTests.setUp
    capture = binding_tests.AcceptanceBindingTests.capture

    def prepared(self, *, limits=None):
        self.protocol.update(model_id="scripted-acceptance-test", context_sha256="6" * 64,
                             runtime_sha256="7" * 64,
                             budgets={"max_model_turns_per_run": 4, "max_model_calls": 80, "comparison_usd": 3,
                                      "study_seconds": 120, "run_seconds": 30})
        self.protocol["planned"] = [{"batch_id": "batch_001", "task_id": task["id"], "repeat": 0, "arm": arm}
                                    for task in self.plan["tasks"] for arm in ("source", "workspace")]
        binding = binding_tests.AcceptanceBindingTests.baseline(self)
        with patch("contextbase.acceptance_report.validate_protocol") as checked:
            plan = prepare_execution_plan(self.protocol, binding, limits=limits)
        checked.assert_called_once()
        return binding, plan

    def execute(self, *, mode="normal", failed_tools=False, limits=None, preflight_failure=False):
        binding, plan = self.prepared(limits=limits)
        model = ScriptedModel(plan, mode=mode)
        with tempfile.TemporaryDirectory() as directory:
            def verify():
                if preflight_failure:
                    raise ValueError("Authored source mismatch")

            execution = _run_comparison(None, None, plan, Path(directory) / "execution.json", verify=verify,
                client=model, tools_factory=lambda arm: ScriptedTools(failed_tools),
                expected_model_id=self.protocol["model_id"], kind="rystic_acceptance_phase")
        return {"binding": binding, "execution": execution}

    def test_real_ten_trial_phase_with_multiple_tools_is_accepted_as_scripted_evidence(self):
        packet = self.execute()
        reasons = []
        _, execution, details, calls = _phase(self.protocol, packet, reasons)
        self.assertEqual(len(execution["trials"]), 10)
        self.assertEqual(len(details), 10)
        self.assertEqual(calls, 20)
        self.assertTrue(all(detail["correct"] for _, detail in details))
        self.assertAlmostEqual(sum(detail["query_cost"] for _, detail in details), .2)
        self.assertEqual(reasons, ["Injected model responses are test evidence only"])
        _interval(execution, "actual phase")
        for trial in execution["trials"]:
            _interval(trial, "actual trial")
            self.assertEqual(len(trial["tool_invocations"]), 2)
            self.assertNotEqual(trial["tool_invocations"][0]["arguments_sha256"], trial["tool_invocations"][1]["arguments_sha256"])
            self.assertEqual(trial["tool_invocations"][0]["arguments_sha256"], _hash({"label": "synthetic-é", "index": 1}))

    def test_paid_wrong_model_cost_survives_skipped_trials(self):
        packet = self.execute(mode="wrong_model")
        reasons = []
        _, execution, details, calls = _phase(self.protocol, packet, reasons)
        self.assertEqual(execution["stop_reason"], "model_identity_mismatch")
        self.assertFalse(execution["model_identity_consistent"])
        self.assertEqual(len(execution["trials"]), 10)
        self.assertEqual((len(details), calls), (1, 1))
        self.assertEqual(details[0][1]["query_cost"], .01)
        self.assertFalse(details[0][1]["correct"])
        self.assertIn("Invocation model identity is missing or differs from the frozen model", reasons)
        self.assertIn("The full frozen schedule was not executed", reasons)

    def test_unknown_spend_preserves_preceding_known_invocation_cost(self):
        packet = self.execute(mode="unknown_second")
        reasons = []
        _, execution, details, calls = _phase(self.protocol, packet, reasons)
        self.assertEqual(execution["stop_reason"], "inference_cost_unknown")
        self.assertEqual((len(details), calls), (1, 2))
        self.assertEqual(details[0][1]["query_cost"], .01)
        self.assertEqual(execution["known_cost_usd"], .01)
        self.assertFalse(execution["cost_complete"])
        self.assertIn("A provider invocation has unknown spend", reasons)

    def test_preflight_failure_retains_all_skips_without_fabricated_attempts(self):
        packet = self.execute(preflight_failure=True)
        reasons = []
        _, execution, details, calls = _phase(self.protocol, packet, reasons)
        self.assertEqual(len(execution["trials"]), 10)
        self.assertEqual((details, calls), ([], 0))
        self.assertTrue(all(trial["started_at"] is None and trial["finished_at"] is None for trial in execution["trials"]))
        self.assertIn("The full frozen schedule was not executed", reasons)

    def test_raised_tool_retains_request_prefix_and_cost_without_a_success(self):
        packet = self.execute(failed_tools=True)
        reasons = []
        _, execution, details, calls = _phase(self.protocol, packet, reasons)
        self.assertEqual((len(details), calls), (10, 10))
        self.assertAlmostEqual(sum(detail["query_cost"] for _, detail in details), .1)
        self.assertTrue(all(not detail["correct"] for _, detail in details))
        self.assertTrue(all(len(trial["tool_invocations"]) == 1 for trial in execution["trials"]))

    def test_answered_receipt_cannot_drop_duplicate_or_change_tool_evidence(self):
        packet = self.execute()
        for mutate in (lambda trial: trial["tool_invocations"].clear(),
                       lambda trial: trial["tool_invocations"].pop(),
                       lambda trial: trial["tool_invocations"][1].update(arguments_sha256=trial["tool_invocations"][0]["arguments_sha256"]),
                       lambda trial: trial["tool_invocations"][0].update(inference_index=1)):
            with self.subTest(mutate=mutate):
                altered = deepcopy(packet)
                mutate(altered["execution"]["trials"][0])
                with self.assertRaises(ValueError):
                    _phase(self.protocol, altered, [])

    def test_hiding_paid_cost_or_changing_queue_identity_is_rejected(self):
        packet = self.execute(mode="wrong_model")
        for mutate in (lambda value: value["execution"].update(known_cost_usd=0),
                       lambda value: value["execution"]["trials"][0].update(known_cost_usd=0),
                       lambda value: value["execution"]["trials"][0].update(queue_index=1)):
            altered = deepcopy(packet)
            mutate(altered)
            with self.assertRaises(ValueError):
                _phase(self.protocol, altered, [])
