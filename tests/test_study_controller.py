"""Offline controller mechanics, never observed agent performance evidence.

The source is an in-memory simulator double. The scripted model deliberately
reads private test gold from generated binding files: these are authored test
answers, not inference or held-out measurements. Only the full multi-batch
protocol compiler entrypoints are stubbed; runtime checks, DuckDB imports,
transitions, binding, scoring, storage, accounting and budgets are real.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from contextbase.acceptance_protocol import runtime_identity
from contextbase.acceptance_report import load_collection
from contextbase.context import load_context
from contextbase.evidence_store import EvidenceStore
from contextbase.study_binding import _hash
from contextbase.study_budget import StudyBudget
from contextbase.study_controller import run_study
from contextbase.study_templates import compile_templates
from contextbase.workspace import Workspace
from contextbase import study_runtime
from test_rystic_benchmark import make_fixture
from test_study_binding import binding_fixture
from test_study_costs import cost_fixture
from test_study_transitions import MutationPair


class NoTools:
    """No source tool access is needed when a fixture model echoes authored gold."""
    def __init__(self):
        self.trace = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def definitions(self):
        return []

    def call(self, *_):
        raise AssertionError("The fixture model must never request a tool")


class FixtureTools(NoTools):
    def definitions(self):
        return [{"name": "fixture_noop", "description": "Synthetic test-only no-op",
                 "inputSchema": {"type": "object"}}]

    def call(self, tool, arguments):
        if tool != "fixture_noop" or arguments != {}:
            raise AssertionError("Unexpected scripted tool request")
        return {"synthetic_test": True}


class AuthoredGoldModel:
    """Read test-only evaluator answers; explicitly not a model benchmark."""
    def __init__(self, output, *, fail_on=None, behavior=None):
        self.output = Path(output)
        self.fail_on, self.behavior = fail_on, behavior
        self.calls = []
        self.timeout_seconds = 30

    def complete(self, system, messages, max_cost_usd):
        self.calls.append({"max_cost_usd": max_cost_usd, "question": messages[0]["content"]})
        if self.fail_on == len(self.calls) and self.behavior == "raises":
            raise TimeoutError("Scripted uncertain provider outcome")
        if self.behavior == "two_turns" and len(messages) == 1:
            return {"response": json.dumps({"tools": [{"tool": "fixture_noop", "arguments": {}}]}),
                    "cost_usd": .001, "model_usage": {"synthetic-test-model": {}}, "is_error": False}
        expected = None
        for path in self.output.glob("workspaces/*/*-binding.json"):
            binding = json.loads(path.read_text())
            for public, private in zip(binding["public_tasks"], binding["evaluator_tasks"]):
                if public["question"] == messages[0]["content"]:
                    expected = private["expected"]
        if expected is None:
            raise AssertionError("No authored test gold matches the actual public question")
        response = {"response": json.dumps({"answer": expected}), "cost_usd": .001,
                    "model_usage": {"synthetic-test-model": {}}, "is_error": False}
        if self.fail_on == len(self.calls):
            if self.behavior == "unknown": response.update(cost_usd=None, spend_unknown=True)
            elif self.behavior == "mismatch": response["model_usage"] = {"different-synthetic-model": {}}
            elif self.behavior == "overspend": response["cost_usd"] = 50
        return response


class StudyControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory) / "authored-policy")
            make_fixture(workspace)
            cls.approved = load_context(workspace)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "execution"
        self.plan, baseline, _ = binding_fixture(self.root, "baseline")
        self.pair = MutationPair(self.plan)
        templates = compile_templates(self.plan)
        intent = {"batch_id": "batch_001", "intent_sha256": "1" * 64, "phase_plan": self.plan,
                  "templates": templates, "baseline_capture": baseline, "provisioning_sha256": "2" * 64,
                  "provisioning": {"environment_id": "synthetic-pair-001"}}
        runtime = runtime_identity()
        frozen = datetime.now(timezone.utc) - timedelta(seconds=1)
        history = []
        for index, kind in enumerate(("context_creation", "context_review")):
            start = frozen - timedelta(seconds=4 - index)
            history.append({"event_id": "synthetic-history-" + str(index), "kind": kind,
                            "batch_id": None, "phase_id": None, "started_at": start.isoformat(),
                            "finished_at": (start + timedelta(seconds=1)).isoformat(),
                            "elapsed_seconds": 1., "cost_usd": .01, "allocation": {"source": .5, "workspace": .5},
                            "evidence_sha256": _hash({"synthetic historical cost": kind}),
                            "duration_role": "exclusive_overhead", "cost_role": "incremental", "attempt_id": None})
        _, contract = cost_fixture()
        contract["assignments"] = {"batch_001": {"environment_id": "synthetic-pair-001",
            "stripe_url": self.pair.stripe.url, "linear_url": self.pair.linear.url}}
        for kind, rule in contract["rules"].items():
            rule.update(method="declared_zero", rate=0, max_seconds=120)
        for kind, rate in {"workspace_ingestion": .01, "refresh": .01, "workflow_configuration": .005,
                           "phase_transitions": .005, "evaluator_validation": .001, "runtime_license": .05}.items():
            contract["rules"][kind].update(method="fixed_per_operation", rate=rate)
        self.contract = contract
        from contextbase.acceptance_protocol import EVENT_RULES
        self.protocol = {"spec": "contextbase.acceptance-protocol/v1", "allocation_sha256": "4" * 64,
            "frozen_at": frozen.isoformat(), "evidence_origin": "synthetic_test", "intents": [intent],
            "context_sha256": _hash(self.approved), "runtime": runtime, "runtime_sha256": _hash(runtime),
            "model_id": "synthetic-test-model", "tasks": templates["tasks"],
            "recurring_profile": templates["recurring_profile"], "repeats": 1,
            "batches": [{"id": "batch_001", "fixture_sha256": "5" * 64,
                         "task_commitments": templates["task_commitments"]}],
            "planned": [{"batch_id": "batch_001", "task_id": task["id"], "repeat": 0, "arm": arm}
                        for task in templates["tasks"] for arm in ("source", "workspace")],
            "budgets": {"comparison_usd": 20, "total_study_usd": 30, "study_seconds": 120,
                        "run_seconds": 10, "max_model_calls": 24, "max_model_turns_per_run": 2},
            "provisioning_spend_usd": .5, "historical_context_events": history,
            "historical_context_events_sha256": _hash(history),
            "accounting_policy": {"event_rules": deepcopy(EVENT_RULES), "unknown_cost_policy": "inconclusive",
                                  "evidence_sha256": _hash(contract)},
            "bootstrap": {"seed": 7, "replicates": 2000, "confidence": .95}}
        self.rehash()
        self.factory_calls = []
        for module in ("study_controller", "study_runtime", "study_budget", "acceptance_report"):
            patcher = patch("contextbase." + module + ".validate_protocol")
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("contextbase.rystic.RysticClient.request", "contextbase.model.ClaudeCLI",
                       "contextbase.study_controller.ClaudeCLI"):
            blocker = patch(target, side_effect=AssertionError("No native/network/model calls in offline controller tests"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def rehash(self):
        self.protocol["accounting_policy"]["evidence_sha256"] = _hash(self.contract)
        self.protocol["protocol_sha256"] = _hash({key: value for key, value in self.protocol.items() if key != "protocol_sha256"})

    def pair_factory(self, stripe, linear):
        self.factory_calls.append((stripe, linear))
        self.assertEqual((stripe, linear), (self.pair.stripe.url, self.pair.linear.url))
        return self.pair

    def run_controller(self, model=None, **changes):
        self.model = model or AuthoredGoldModel(self.output)
        arguments = {"approve": self.protocol["protocol_sha256"], "allow_model_data": True,
                     "allow_source_transitions": True, "client": self.model, "pair_factory": self.pair_factory,
                     "tools_factory": lambda workspace, pair, arm: NoTools(), **changes}
        return run_study(self.protocol, self.contract, self.approved, self.output, **arguments)

    def collection(self):
        index = json.loads((self.output / "collection-index.json").read_text())
        return load_collection(EvidenceStore(self.output / "receipts"), index)[1]

    def test_all_four_phases_complete_with_real_local_receipts_and_explicit_ineligibility(self):
        original = deepcopy((self.protocol, self.contract, self.approved))
        result = self.run_controller()
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(result["completed_phases"], 4)
        self.assertEqual(result["recorded_phases"], 4)
        self.assertEqual(result["unexecuted_trials"], 0)
        self.assertEqual(result["budget"]["model_calls"], 24)
        self.assertEqual(len(self.model.calls), 24)
        self.assertEqual(result["budget"]["pending"], [])
        self.assertEqual(result["budget"]["blocked_reasons"], [])
        self.assertEqual(set(result["reasons"]), {"Synthetic source fixtures cannot support observed acceptance results",
                                                  "Injected model responses are test evidence only",
                                                  "Full billing reconciliation is required before charter readiness"})
        self.assertEqual(result["statistical_acceptance"], "inconclusive")
        self.assertFalse(result["claim_eligible"])
        self.assertFalse(result["automatic_resume_allowed"])
        self.assertEqual((self.protocol, self.contract, self.approved), original)
        self.assertAlmostEqual(result["budget"]["known_comparison_cost_usd"], .163)
        self.assertAlmostEqual(result["budget"]["known_total_cost_usd"], .663)
        collection = self.collection()
        self.assertEqual([row["binding"]["phase_id"] for row in collection["phases"]], ["baseline", "refreshed", "guard_a", "guard_b"])
        for index, packet in enumerate(collection["phases"]):
            binding, execution = packet["binding"], packet["execution"]
            self.assertTrue(execution["execution_complete"])
            self.assertTrue(all(trial["correct"] for trial in execution["trials"]))
            self.assertEqual(execution["model_backend"], "injected_test_client")
            self.assertEqual(binding["capture"]["receipt"]["validation_status"], "valid" if index < 2 else "blocked")
            if index >= 2:
                self.assertIsNone(binding["snapshot"])
        workspace = Workspace(self.output / "workspaces" / "batch_001" / "workspace")
        with self.assertRaisesRegex(ValueError, "refresh"):
            workspace.query("SELECT 1 AS value")
        expected_writes = sum(len(item["changes"]) for item in self.plan["transitions"])
        self.assertEqual(len(self.pair.writes), expected_writes)
        report = json.loads((self.output / "report.json").read_text())
        self.assertIsNone(report["metrics"])
        self.assertEqual(report["first_use"]["status"], "recorded")
        self.assertAlmostEqual(report["known_total_study_spend_usd"], result["budget"]["known_total_cost_usd"])

    def test_approval_and_invalid_cost_assignments_fail_before_output_or_source_creation(self):
        for changes in ({"approve": "f" * 64}, {"allow_model_data": False}, {"allow_source_transitions": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_controller(**changes)
            self.assertFalse(self.output.exists())
        original = deepcopy(self.contract)
        for case in ("unknown_price", "remote_assignment", "wrong_environment", "missing_rule", "stale_hash"):
            self.contract = deepcopy(original)
            if case == "unknown_price": self.contract["rules"]["refresh"]["rate"] = None
            elif case == "remote_assignment": self.contract["assignments"]["batch_001"]["stripe_url"] = "http://example.invalid:18981"
            elif case == "wrong_environment": self.contract["assignments"]["batch_001"]["environment_id"] = "another-synthetic-pair"
            elif case == "missing_rule": del self.contract["rules"]["refresh"]
            else: self.contract["rules"]["refresh"]["rationale"] = "Changed after freeze"
            if case != "stale_hash": self.rehash()
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.run_controller()
            self.assertFalse(self.output.exists())
        self.assertEqual(self.factory_calls, [])
        self.assertEqual((self.pair.verifications, self.pair.reads, self.pair.writes), (0, [], []))

    def test_call_budget_is_cumulative_across_phases_and_cannot_auto_resume(self):
        model = AuthoredGoldModel(self.output, behavior="two_turns")
        result = self.run_controller(model, tools_factory=lambda workspace, pair, arm: FixtureTools())
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(model.calls), 24)
        self.assertEqual(result["budget"]["model_calls"], 24)
        self.assertEqual(result["completed_phases"], 1)
        self.assertEqual(result["recorded_phases"], 2)
        self.assertGreater(result["unexecuted_trials"], 0)
        self.assertFalse(result["automatic_resume_allowed"])
        self.assertEqual(len(self.pair.writes), len(self.plan["transitions"][0]["changes"]))
        collection = self.collection()
        self.assertEqual(collection["phases"][0]["execution"]["invocations"], 20)
        self.assertEqual(collection["phases"][1]["execution"]["invocations"], 4)
        before = deepcopy((self.pair.reads, self.pair.writes, self.factory_calls))
        with self.assertRaises(FileExistsError):
            self.run_controller(model)
        self.assertEqual((self.pair.reads, self.pair.writes, self.factory_calls), before)
        self.assertEqual(len(model.calls), 24)
        with StudyBudget.open(self.output / "budget", self.protocol) as budget:
            self.assertEqual(budget.status()["model_calls"], 24)
            self.assertTrue(budget.status()["blocked_reasons"])

    def _assert_unknown_model_stops(self, behavior):
        model = AuthoredGoldModel(self.output, fail_on=1, behavior=behavior)
        result = self.run_controller(model)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["budget"]["model_calls"], 1)
        self.assertEqual(result["unexecuted_trials"], 23)
        self.assertEqual(len(result["budget"]["pending"]), 1)
        self.assertIn("outcome_unknown", result["budget"]["blocked_reasons"])
        self.assertEqual(self.pair.writes, [])
        self.assertFalse((self.output / "workspaces" / "batch_001" / "baseline-postflight-capture.json").exists())
        self.assertEqual(self.pair.verifications, 2, "Only the original sync and preflight may read source state")
        first = self.collection()["phases"][0]["execution"]["trials"][0]
        self.assertEqual(len(first["inferences"]), 1)
        self.assertIsNone(first["inferences"][0]["cost_usd"])
        self.assertFalse(first["correct"])
        with StudyBudget.open(self.output / "budget", self.protocol) as budget:
            self.assertEqual(len(budget.status()["pending"]), 1)
            self.assertTrue(budget.status()["blocked_reasons"])

    def test_unknown_provider_charge_retains_unresolved_reservation(self):
        self._assert_unknown_model_stops("unknown")

    def test_provider_exception_retains_potentially_paid_attempt_without_retry(self):
        self._assert_unknown_model_stops("raises")

    def test_model_identity_change_stops_before_later_phases_and_retains_paid_calls(self):
        model = AuthoredGoldModel(self.output, fail_on=13, behavior="mismatch")
        result = self.run_controller(model)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(model.calls), 13)
        self.assertEqual(result["budget"]["model_calls"], 13)
        self.assertEqual(result["budget"]["pending"], [])
        self.assertEqual(result["completed_phases"], 1)
        self.assertEqual(result["recorded_phases"], 2)
        self.assertEqual(result["unexecuted_trials"], 11)
        self.assertEqual(len(self.pair.writes), len(self.plan["transitions"][0]["changes"]))
        phase = self.collection()["phases"][-1]["execution"]
        self.assertEqual(phase["stop_reason"], "model_identity_mismatch")
        self.assertAlmostEqual(phase["known_cost_usd"], .003)

    def test_paid_overspend_is_recorded_before_terminal_stop(self):
        model = AuthoredGoldModel(self.output, fail_on=1, behavior="overspend")
        result = self.run_controller(model)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["budget"]["model_calls"], 1)
        self.assertGreater(result["budget"]["known_comparison_cost_usd"], 50)
        self.assertIn("reservation_exceeded", result["budget"]["blocked_reasons"])
        self.assertEqual(result["budget"]["pending"], [])
        self.assertEqual(self.pair.writes, [])
        self.assertFalse((self.output / "workspaces" / "batch_001" / "baseline-postflight-capture.json").exists())
        self.assertEqual(self.pair.verifications, 2)
        first = self.collection()["phases"][0]["execution"]["trials"][0]
        self.assertEqual(first["inferences"][0]["cost_usd"], 50)

    def test_uncertain_source_write_is_retained_and_never_retried(self):
        self.pair.fail_at = 1
        self.pair.fail_after_effect = True
        result = self.run_controller()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["completed_phases"], 1)
        self.assertEqual(result["budget"]["model_calls"], 10)
        self.assertEqual(len(self.model.calls), 10)
        self.assertEqual(len(self.pair.writes), 1)
        self.assertEqual(len(result["budget"]["pending"]), 1)
        self.assertEqual(result["budget"]["pending"][0]["kind"], "phase_transitions")
        self.assertEqual(result["budget"]["pending"][0]["outcome"], "unknown")
        self.assertIn("outcome_unknown", result["budget"]["blocked_reasons"])
        path = self.output / "workspaces" / "batch_001" / "refreshed-transition.json"
        transition = json.loads(path.read_text())
        self.assertEqual(transition["outcome"], "write_uncertain")
        self.assertEqual(transition["write_attempts"], 1)
        self.assertEqual(transition["operations"][0]["status"], "effect_uncertain")
        self.assertFalse((path.parent / "refreshed-binding.json").exists())
        self.assertFalse((path.parent / "guard_a-transition.json").exists())
        state = json.loads((self.output / "controller.json").read_text())
        store = EvidenceStore(self.output / "receipts")
        retained = [store.get(digest) for digest in state["operation_refs"]]
        self.assertTrue(any(row.get("operation") == "transition" and row["result"]["outcome"] == "write_uncertain"
                            for row in retained))
        self.assertEqual(len(self.collection()["phases"]), 1)

    def test_initial_source_mismatch_retains_readable_zero_phase_partial_collection(self):
        self.pair.rows["stripe.customers"][0]["name"] = "Synthetic source drift after freeze"
        result = self.run_controller()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["completed_phases"], 0)
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.pair.writes, [])
        self.assertEqual(result["unexecuted_trials"], 24)
        self.assertEqual(self.collection()["phases"], [])
        self.assertTrue((self.output / "workspaces" / "batch_001" / "baseline-capture.json").exists())

    def test_failed_binding_storage_retains_actual_failure_envelope_without_retry(self):
        original_write = study_runtime._write_new
        failures = []

        def fail_binding(path, value):
            if Path(path).name == "baseline-binding.json":
                failures.append(str(path))
                raise OSError("Synthetic binding storage failure")
            return original_write(path, value)

        with patch("contextbase.study_runtime._write_new", side_effect=fail_binding):
            result = self.run_controller()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["completed_phases"], 0)
        self.assertEqual(result["recorded_phases"], 0)
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.pair.writes, [])
        self.assertEqual(self.collection()["phases"], [])
        state = json.loads((self.output / "controller.json").read_text())
        store = EvidenceStore(self.output / "receipts")
        retained = [store.get(digest) for digest in state["operation_refs"]]
        failed = [row for row in retained if row.get("operation") == "bind"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["outcome"], "failed")
        self.assertEqual(failed[0]["error_type"], "OSError")
        self.assertIsNone(failed[0]["result"])
        events = self.collection()["events"]
        self.assertTrue(any(event["evidence_sha256"] == failed[0]["receipt_sha256"] for event in events))

    def test_slow_binding_exceeds_frozen_evaluator_limit_before_model_dispatch(self):
        self.contract["rules"]["evaluator_validation"]["max_seconds"] = .15
        self.rehash()
        original_build = study_runtime._build
        calls = []

        def slow_build(*args, **kwargs):
            calls.append("bind")
            time.sleep(.3)
            return original_build(*args, **kwargs)

        with patch("contextbase.study_runtime._build", side_effect=slow_build):
            result = self.run_controller(tools_factory=lambda *args: self.fail("No MCP startup after evaluator cap"))
        self.assertEqual(calls, ["bind"])
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("evaluator_duration_exceeded", result["budget"]["blocked_reasons"])
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(result["recorded_phases"], 0)
        self.assertEqual(self.pair.verifications, 1)
        events = self.collection()["events"]
        seconds = sum(event["elapsed_seconds"] for event in events if event["kind"] == "evaluator_validation")
        self.assertGreaterEqual(seconds, .3)
        self.assertTrue((self.output / "workspaces" / "batch_001" / "baseline-binding.json").exists())

    def test_slow_preflight_exceeds_frozen_evaluator_limit_before_mcp_or_model(self):
        self.contract["rules"]["evaluator_validation"]["max_seconds"] = 3.0
        self.rehash()
        original_verify = study_runtime.NativeStudyRuntime.verify
        stages = []

        def slow_verify(runtime, batch, binding, stage):
            stages.append(stage)
            receipt = original_verify(runtime, batch, binding, stage)
            if stage == "preflight":
                time.sleep(3.25)
            return receipt

        with patch("contextbase.study_runtime.NativeStudyRuntime.verify", new=slow_verify):
            result = self.run_controller(tools_factory=lambda *args: self.fail("No MCP startup after evaluator cap"))
        self.assertEqual(stages, ["preflight"])
        self.assertEqual(result["outcome"], "stopped")
        self.assertIn("evaluator_duration_exceeded", result["budget"]["blocked_reasons"])
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.pair.verifications, 2)
        self.assertFalse((self.output / "workspaces" / "batch_001" / "baseline-postflight-capture.json").exists())
        collection = self.collection()
        self.assertEqual(len(collection["phases"]), 1)
        execution = collection["phases"][0]["execution"]
        self.assertGreaterEqual(execution["preflight_seconds"], 3.25)
        self.assertEqual(execution["invocations"], 0)

    def test_historical_spend_and_runtime_reservation_can_stop_before_source_creation(self):
        self.protocol["budgets"]["comparison_usd"] = .06
        self.rehash()
        result = self.run_controller()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["completed_phases"], 0)
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.factory_calls, [])
        self.assertAlmostEqual(result["budget"]["known_comparison_cost_usd"], .02)
        self.assertAlmostEqual(result["budget"]["known_total_cost_usd"], .52)
        self.assertEqual(self.collection()["phases"], [])


if __name__ == "__main__":
    unittest.main()
