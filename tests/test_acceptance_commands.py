"""Complete offline CLI binding with authored fixtures and actual local DuckDB."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_binding import _validate_binding
from contextbase.cli import execute, parser
from contextbase.rystic import sync_snapshot
from contextbase.study_transitions import apply_transition
from contextbase.workspace import Workspace
from test_acceptance_protocol import protocol_inputs
from test_study_transitions import MutationPair


class AcceptanceCommandTests(unittest.TestCase):
    def test_allocated_protocol_and_all_phase_bindings_use_retained_real_workspace_receipts(self):
        allocation, intents, settings = protocol_inputs()
        original = intents[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def save(name, value):
                path = root / name
                path.write_text(json.dumps(value))
                return str(path)

            def command(*args):
                return execute(parser().parse_args(["--workspace", str(root / "unused"), "rystic", *args]))

            allocation_path = save("allocation.json", allocation)
            intent_path = root / "intent.json"
            protocol_path = root / "protocol.json"
            with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native source construction")), \
                 patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model calls")):
                intent_result = command("acceptance-intent", "--allocation", allocation_path, "--batch", original["batch_id"],
                    "--manifest", save("manifest.json", original["manifest"]),
                    "--capture", save("provisioning-capture.json", original["baseline_capture"]),
                    "--provisioning", save("provisioning.json", original["provisioning"]), "--output", str(intent_path))
                self.assertEqual(intent_result["intent_sha256"], original["intent_sha256"])
                self.assertFalse(intent_result["execution_authorized"])
                result = command("acceptance-protocol", "--allocation", allocation_path,
                    "--intents", save("intents.json", intents), "--settings", save("settings.json", settings),
                    "--output", str(protocol_path))
                self.assertEqual(result["batches"], 30)
                self.assertEqual(result["planned_runs"], len(allocation["schedule"]))
                self.assertEqual(result["source_calls"], 0)
                self.assertEqual(result["model_calls"], 0)
                self.assertFalse(result["execution_authorized"])
                self.assertFalse(result["claim_eligible"])
                protocol = json.loads(protocol_path.read_text())
                frozen_bytes = protocol_path.read_bytes()
                intent = protocol["intents"][0]
                prepared = intent["phase_plan"]
                pair = MutationPair(prepared)
                workspace = Workspace(root / "workspace")
                previous_path = None
                for index, phase in enumerate(prepared["phases"]):
                    phase_id = phase["id"]
                    transition_path = None
                    if index:
                        declared = prepared["transitions"][index - 1]
                        transition_path = root / (phase_id + "-transition.json")
                        apply_transition(pair, prepared, declared["from_phase"], declared["to_phase"], transition_path,
                            approve_plan=prepared["plan_sha256"], approve_action=declared["action_sha256"])
                    capture_path = root / (phase_id + "-capture.json")
                    snapshot_args = []
                    if phase["kind"] == "guarded":
                        with self.assertRaises(ValueError):
                            sync_snapshot(workspace, pair, original["manifest"]["namespace"], capture_output=capture_path)
                        with self.assertRaises(ValueError):
                            workspace.query("SELECT COUNT(*) FROM stripe.customers")
                    else:
                        snapshot = sync_snapshot(workspace, pair, original["manifest"]["namespace"], capture_output=capture_path)
                        snapshot_args = ["--snapshot", save(phase_id + "-snapshot.json", snapshot)]
                    bound_path = root / (phase_id + "-bound.json")
                    chain_args = (["--previous-binding", str(previous_path), "--transition", str(transition_path)]
                                  if index else [])
                    summary = command("acceptance-bind", "--protocol", str(protocol_path), "--batch", intent["batch_id"],
                        "--phase", phase_id, "--capture", str(capture_path), "--output", str(bound_path),
                        *snapshot_args, *chain_args)
                    self.assertEqual(summary["protocol_sha256"], protocol["protocol_sha256"])
                    self.assertEqual(summary["evidence_origin"], "synthetic_test")
                    self.assertFalse(summary["claim_eligible"])
                    self.assertFalse(summary["execution_authorized"])
                    self.assertNotIn("evaluator_tasks", summary)
                    self.assertEqual(bound_path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(protocol_path.read_bytes(), frozen_bytes)
                    if index == 0:
                        execution_path = root / "execution-plan.json"
                        execution_summary = command("acceptance-execution-plan", "--protocol", str(protocol_path),
                            "--binding", str(bound_path), "--output", str(execution_path))
                        self.assertEqual(execution_summary["planned_trials"], 10)
                        self.assertFalse(execution_summary["execution_authorized"])
                        self.assertEqual(execution_summary["model_calls"], 0)
                        self.assertEqual(execution_path.stat().st_mode & 0o777, 0o600)
                    previous_path = bound_path
                final = json.loads(previous_path.read_text())
                _validate_binding(protocol, final)
                self.assertEqual(final["phase_id"], "guard_b")
                self.assertEqual(len(pair.writes), sum(len(t["changes"]) for t in prepared["transitions"]))
                self.assertFalse((root / "unused").exists())


if __name__ == "__main__":
    unittest.main()
