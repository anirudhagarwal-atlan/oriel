"""Actual local adapter work with in-memory sources; no native/model execution.

Only the full protocol validator is stubbed; source normalization, pinned source
checks, transitions, DuckDB import/refusal, context validation and acceptance
binding execute normally. Independent protocol suites cover reconstruction.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_protocol import runtime_identity
from contextbase.context import load_context
from contextbase.refresh_state import refresh_status
from contextbase.rystic import sync_snapshot
from contextbase.study_binding import _hash
from contextbase.study_runtime import NativeStudyRuntime, RuntimeOperationError
from contextbase.study_templates import compile_templates
from contextbase.workspace import Workspace
from test_rystic_benchmark import make_fixture
from test_study_binding import binding_fixture
from test_study_transitions import MutationPair


class ConfigurationClient:
    calls = []

    def __init__(self, workspace, **kwargs):
        self.calls.append((str(workspace.root), deepcopy(kwargs)))
        self.command = ["synthetic-python", "-m", "contextbase", "--workspace", str(workspace.root),
                        "serve", "--arm", kwargs["arm"]]

    def __enter__(self):
        raise AssertionError("Configuration must never start an MCP process")


class StudyRuntimeTests(unittest.TestCase):
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
        self.plan, baseline, _ = binding_fixture(self.root, "baseline")
        self.pair = MutationPair(self.plan)
        templates = compile_templates(self.plan)
        intent = {"batch_id": "batch_001", "intent_sha256": "1" * 64, "phase_plan": self.plan,
                  "templates": templates, "baseline_capture": baseline, "provisioning_sha256": "2" * 64,
                  "provisioning": {"environment_id": "synthetic-pair-001"}}
        identity = runtime_identity()
        self.protocol = {"spec": "contextbase.acceptance-protocol/v1", "allocation_sha256": "4" * 64,
            "frozen_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "evidence_origin": "synthetic_test", "intents": [intent],
            "context_sha256": _hash(self.approved), "runtime": identity, "runtime_sha256": _hash(identity),
            "batches": [{"id": "batch_001", "fixture_sha256": "5" * 64,
                         "task_commitments": templates["task_commitments"]}]}
        self.rehash_protocol()
        self.assignments = {"batch_001": {"environment_id": "synthetic-pair-001",
                            "stripe_url": self.pair.stripe.url, "linear_url": self.pair.linear.url}}
        validator = patch("contextbase.study_runtime.validate_protocol")
        self.validator = validator.start()
        self.addCleanup(validator.stop)
        # Keep code identity stable while sibling agents edit unrelated package
        # modules; an explicit adversarial test below verifies the drift gate.
        runtime = patch("contextbase.study_runtime.runtime_identity", return_value=identity)
        runtime.start()
        self.addCleanup(runtime.stop)
        for target in ("contextbase.rystic.RysticClient.request", "contextbase.model.ClaudeCLI"):
            offline = patch(target, side_effect=AssertionError("Native/model calls are forbidden in this offline suite"))
            offline.start()
            self.addCleanup(offline.stop)
        self.factory_calls = []
        ConfigurationClient.calls = []
        self.runtime = self.construct()

    def rehash_protocol(self):
        self.protocol["protocol_sha256"] = _hash({key: value for key, value in self.protocol.items() if key != "protocol_sha256"})

    def pair_factory(self, stripe, linear):
        self.factory_calls.append((stripe, linear))
        self.assertEqual((stripe, linear), (self.pair.stripe.url, self.pair.linear.url))
        return self.pair

    def construct(self, **overrides):
        arguments = {"protocol": self.protocol, "approved_context": self.approved,
                     "assignments": self.assignments, "root": self.root / "runtime",
                     "pair_factory": self.pair_factory, "client_factory": ConfigurationClient}
        arguments.update(overrides)
        return NativeStudyRuntime(**arguments)

    def prepare(self):
        source = self.runtime.setup_source("batch_001")
        workspace = self.runtime.prepare_batch("batch_001")
        return source, workspace

    def baseline(self):
        self.prepare()
        sync = self.runtime.sync("batch_001", "baseline")
        binding = self.runtime.bind("batch_001", "baseline")["result"]
        return sync, binding

    def advance(self, index):
        change = self.plan["transitions"][index]
        return self.runtime.transition("batch_001", change["to_phase"],
            approve_plan=self.plan["plan_sha256"], approve_action=change["action_sha256"])

    def assert_receipt(self, receipt):
        json.dumps(receipt, allow_nan=False)
        start, finish = (datetime.fromisoformat(receipt[key]) for key in ("started_at", "finished_at"))
        self.assertLessEqual(start, finish)
        self.assertAlmostEqual((finish - start).total_seconds(), receipt["elapsed_seconds"], delta=.001)
        self.assertEqual(receipt["receipt_sha256"], _hash({key: value for key, value in receipt.items() if key != "receipt_sha256"}))
        self.assertEqual(receipt["model_calls"], 0)
        self.assertFalse(receipt["claim_eligible"])

    def test_constructor_validates_without_creating_files_clients_or_source_calls(self):
        self.validator.assert_called_once_with(self.protocol)
        self.assertFalse((self.root / "runtime").exists())
        self.assertFalse(self.factory_calls)
        self.assertFalse(ConfigurationClient.calls)
        self.assertEqual((self.pair.verifications, self.pair.reads, self.pair.writes), (0, [], []))
        self.assertEqual(self.runtime.receipts, [])
        with self.assertRaisesRegex(ValueError, "Configure"):
            self.runtime.pair("batch_001")

    def test_failed_protocol_gate_precedes_any_output_or_source_construction(self):
        self.validator.side_effect = ValueError("Rejected frozen protocol")
        with self.assertRaisesRegex(ValueError, "Rejected frozen protocol"):
            self.construct()
        self.assertFalse((self.root / "runtime").exists())
        self.assertFalse(self.factory_calls)

    def test_source_configuration_can_precede_fresh_exact_context_installation(self):
        original = deepcopy(self.approved)
        with patch("contextbase.context.bootstrap", side_effect=AssertionError("No inferred context")), \
             patch("contextbase.context._metric_witness", side_effect=AssertionError("No regenerated witness")), \
             patch("contextbase.context.approve_metric", side_effect=AssertionError("No new approval")), \
             patch("contextbase.rystic_workflow.propose_context", side_effect=AssertionError("No policy regeneration")):
            source, prepared = self.prepare()
        self.assert_receipt(source); self.assert_receipt(prepared)
        self.assertLessEqual(source["finished_at"], prepared["started_at"])
        self.assertEqual(source["phase_id"], "baseline")
        self.assertFalse(source["result"]["mcp_started"])
        self.assertEqual(load_context(self.runtime.workspace("batch_001")), original)
        self.assertEqual(self.approved, original)
        self.assertFalse(self.runtime.workspace("batch_001").db_path.exists())
        self.assertEqual(prepared["result"]["context_sha256"], self.protocol["context_sha256"])
        self.assertFalse(prepared["result"]["context_regenerated"])
        self.assertEqual((self.pair.verifications, self.pair.reads, self.pair.writes), (0, [], []))
        path = self.runtime.workspace("batch_001").root / "context" / "approved.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_all_four_phases_use_original_imports_exact_transitions_and_failed_refreshes(self):
        with patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            _, prepared = self.prepare()
            previous = None
            for index, phase in enumerate(("baseline", "refreshed", "guard_a", "guard_b")):
                if index:
                    transition = self.advance(index - 1)
                    self.assert_receipt(transition)
                    self.assertEqual(transition["result"]["outcome"], "applied")
                    self.assertIn("before_transition", transition["observations"])
                sync = self.runtime.sync("batch_001", phase)
                self.assert_receipt(sync)
                self.assertLessEqual(prepared["started_at"], sync["started_at"])
                result = sync["result"]
                self.assertEqual(result["readiness"], "materialized" if index < 2 else "refused_invalid_source")
                path = self.root / "runtime" / "batch_001" / (phase + "-capture.json")
                self.assertEqual(json.loads(path.read_text()), result["capture"])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                bound = self.runtime.bind("batch_001", phase)
                self.assert_receipt(bound)
                binding = bound["result"]
                self.assertEqual(binding["capture"], result["capture"])
                self.assertEqual(binding["snapshot"], result["snapshot"])
                self.assertEqual(binding["previous_binding_sha256"], previous)
                self.assertEqual(binding["task_commitments"], {
                    key: value for key, value in self.protocol["batches"][0]["task_commitments"].items()
                    if key.startswith(phase + "/")})
                if index < 2:
                    self.assertEqual(result["snapshot"]["previous_snapshot_id"], None if index == 0 else baseline_snapshot)
                    baseline_snapshot = result["snapshot"]["snapshot_id"]
                else:
                    self.assertIsNone(binding["snapshot"])
                    with self.assertRaisesRegex(ValueError, "refresh"):
                        self.runtime.workspace("batch_001").query("SELECT 1 AS value")
                for stage in ("preflight", "postflight"):
                    verified = self.runtime.verify("batch_001", binding, stage)
                    self.assert_receipt(verified)
                    self.assertEqual(verified["result"]["observed_source"], binding["capture"]["observed_source"])
                    self.assertEqual(verified["result"]["receipt"]["validation_status"], result["capture"]["receipt"]["validation_status"])
                previous = binding["binding_sha256"]
            self.assertEqual(load_context(self.runtime.workspace("batch_001")), self.approved)
            self.assertEqual(len(self.pair.writes), sum(len(row["changes"]) for row in self.plan["transitions"]))
            self.validator.assert_called_once()

    def test_context_drift_stops_before_live_reads_and_preserves_failure_clock(self):
        _, binding = self.baseline()
        before = (self.pair.verifications, len(self.pair.reads))
        path = self.runtime.workspace("batch_001").root / "context" / "approved.json"
        changed = json.loads(path.read_text()); changed["metrics"][0]["version"] += 1
        path.write_text(json.dumps(changed))
        with self.assertRaises(RuntimeOperationError) as caught:
            self.runtime.verify("batch_001", binding, "preflight")
        self.assert_receipt(caught.exception.receipt)
        self.assertEqual(caught.exception.receipt["outcome"], "failed")
        self.assertEqual((self.pair.verifications, len(self.pair.reads)), before)
        path.write_text(json.dumps(self.approved))
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.runtime.verify("batch_001", binding, "postflight")

    def test_code_drift_is_rejected_before_configuration_or_source_access(self):
        with patch("contextbase.study_runtime.runtime_identity", return_value={"changed": True}), \
             self.assertRaises(RuntimeOperationError) as caught:
            self.runtime.setup_source("batch_001")
        self.assertEqual(caught.exception.receipt["error_type"], "ValueError")
        self.assertFalse(self.factory_calls)

    def test_full_raw_source_drift_is_rejected_even_when_normalized_semantics_match(self):
        _, binding = self.baseline()
        # Retained metadata changes are not part of the financial oracle.
        self.pair.rows["stripe.customers"][0]["metadata"]["audit_marker"] = "synthetic-drift"
        with self.assertRaises(RuntimeOperationError) as caught:
            self.runtime.verify("batch_001", binding, "preflight")
        self.assertIsNotNone(caught.exception.receipt["result"])
        self.assertNotEqual(caught.exception.receipt["result"]["observed_source"], binding["capture"]["observed_source"])
        self.assertEqual(self.pair.writes, [])

    def test_transition_checks_complete_current_state_before_any_write(self):
        self.baseline()
        self.pair.rows["stripe.customers"][0]["metadata"]["audit_marker"] = "synthetic-drift"
        with self.assertRaises(RuntimeOperationError) as caught:
            self.advance(0)
        self.assertEqual(self.pair.writes, [])
        self.assertIsNone(caught.exception.receipt["result"])
        self.assertIn("before_transition", caught.exception.receipt["observations"])

    def test_unapproved_transition_and_uncertain_write_never_retry(self):
        self.baseline()
        reads = len(self.pair.reads)
        with self.assertRaises(RuntimeOperationError):
            self.runtime.transition("batch_001", "refreshed", approve_plan=self.plan["plan_sha256"], approve_action="wrong")
        self.assertEqual(self.pair.writes, [])
        self.assertEqual(len(self.pair.reads), reads)
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.advance(0)

    def test_uncertain_native_shaped_write_retains_original_transition_failure(self):
        self.baseline()
        self.pair.fail_at = 1
        self.pair.fail_after_effect = True
        with self.assertRaises(RuntimeOperationError) as caught:
            self.advance(0)
        self.assert_receipt(caught.exception.receipt)
        transition = caught.exception.receipt["result"]
        self.assertIsNotNone(transition)
        self.assertNotEqual(transition["outcome"], "applied")
        self.assertEqual(transition["write_attempts"], 1)
        self.assertEqual(len(self.pair.writes), 1)
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.advance(0)
        self.assertEqual(len(self.pair.writes), 1)

    def test_unverified_capture_is_terminal_and_not_an_expected_guard_refusal(self):
        self.prepare()
        self.pair.fail_readback = True
        self.pair.writes.append({"synthetic": "cause-in-memory-read-failure"})
        with self.assertRaises(RuntimeOperationError) as caught:
            self.runtime.sync("batch_001", "baseline")
        self.assertEqual(caught.exception.receipt["outcome"], "failed")
        status = refresh_status(self.runtime.workspace("batch_001"))
        self.assertFalse(status["snapshot_reads_allowed"])
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.runtime.bind("batch_001", "baseline")

    def test_transport_failure_after_guard_refusal_cannot_be_reclassified_as_success(self):
        self.baseline()
        self.advance(0)
        self.runtime.sync("batch_001", "refreshed")
        self.runtime.bind("batch_001", "refreshed")
        self.advance(1)

        def fail_after_refusal(*args, **kwargs):
            try:
                sync_snapshot(*args, **kwargs)
            except ValueError:
                raise TimeoutError("Synthetic unknown work after a blocked refresh") from None

        with patch("contextbase.study_runtime.sync_snapshot", side_effect=fail_after_refusal), \
             self.assertRaises(RuntimeOperationError) as caught:
            self.runtime.sync("batch_001", "guard_a")
        self.assertEqual(caught.exception.receipt["outcome"], "failed")
        self.assertEqual(caught.exception.receipt["result"]["capture"]["receipt"]["validation_status"], "blocked")
        self.assertFalse(refresh_status(self.runtime.workspace("batch_001"))["snapshot_reads_allowed"])
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.runtime.bind("batch_001", "guard_a")

    def test_saved_sync_must_match_actual_current_local_rows(self):
        self.prepare()
        self.runtime.sync("batch_001", "baseline")
        workspace = self.runtime.workspace("batch_001")
        with workspace._connect(writable=True) as connection:
            connection.execute("UPDATE stripe.charges SET amount_cents=amount_cents+1")
        with self.assertRaises(RuntimeOperationError):
            self.runtime.bind("batch_001", "baseline")

    def test_existing_workspace_and_repeated_sync_are_refused_without_warming(self):
        prior = self.root / "runtime" / "batch_001"
        prior.mkdir(parents=True)
        marker = prior / "retain.txt"; marker.write_text("synthetic-existing-data")
        with self.assertRaises(RuntimeOperationError):
            self.runtime.prepare_batch("batch_001")
        self.assertEqual(marker.read_text(), "synthetic-existing-data")
        self.assertEqual(self.pair.reads, [])

    def test_repeated_successful_import_cannot_rewrite_first_use(self):
        sync, _ = self.baseline()
        before = (self.pair.verifications, len(self.pair.reads))
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.runtime.sync("batch_001", "baseline")
        self.assertEqual((self.pair.verifications, len(self.pair.reads)), before)
        capture = self.root / "runtime" / "batch_001" / "baseline-capture.json"
        self.assertEqual(json.loads(capture.read_text()), sync["result"]["capture"])

    def test_binding_input_cannot_be_substituted_or_mutated_by_caller(self):
        _, binding = self.baseline()
        binding["claim_eligible"] = True
        before = len(self.pair.reads)
        with self.assertRaises(RuntimeOperationError):
            self.runtime.verify("batch_001", binding, "preflight")
        self.assertEqual(len(self.pair.reads), before)

    def test_only_exact_distinct_numeric_loopback_assignments_are_accepted(self):
        for address in ("https://127.0.0.1:18981", "http://localhost:18981", "http://192.0.2.1:18981",
                        "http://user:secret@127.0.0.1:18981", "http://127.0.0.1:18981/private",
                        "http://127.0.0.1:18982", "http://[::1]:18982"):
            assignments = deepcopy(self.assignments); assignments["batch_001"]["stripe_url"] = address
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.construct(assignments=assignments)
        assignments = deepcopy(self.assignments); assignments["batch_001"]["environment_id"] = "other-pair"
        with self.assertRaisesRegex(ValueError, "environment"):
            self.construct(assignments=assignments)
        with self.assertRaises(ValueError):
            self.construct(assignments={})

    def test_multi_batch_assignment_cannot_reuse_any_source_endpoint(self):
        second = deepcopy(self.protocol["intents"][0]); second["batch_id"] = "batch_002"
        second["provisioning"]["environment_id"] = "synthetic-pair-002"
        self.protocol["intents"].append(second)
        self.protocol["batches"].append({**deepcopy(self.protocol["batches"][0]), "id": "batch_002"})
        self.rehash_protocol()
        assignments = deepcopy(self.assignments)
        assignments["batch_002"] = {"environment_id": "synthetic-pair-002", "stripe_url": "http://127.0.0.1:18983",
                                    "linear_url": self.assignments["batch_001"]["stripe_url"]}
        with self.assertRaisesRegex(ValueError, "separate"):
            self.construct(assignments=assignments)

    def test_mismatched_frozen_context_fails_before_any_installation(self):
        changed = deepcopy(self.approved); changed["context_version"] = "local-" + "f" * 16
        with self.assertRaisesRegex(ValueError, "frozen approved context"):
            self.construct(approved_context=changed)
        self.assertFalse((self.root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
