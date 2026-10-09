"""Offline CLI integration: authored source state, actual local DuckDB, no model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.cli import execute, parser
from contextbase.refresh_state import refresh_status
from contextbase.rystic_benchmark import _read, STUDY_EVIDENCE_MAX_BYTES
from contextbase.sequential_study import task_commitment
from contextbase.workspace import Workspace
from test_study_binding import binding_fixture
from test_study_transitions import MutationPair


class StudyCommandsTests(unittest.TestCase):
    def test_study_receipt_reader_supports_larger_ledgers_without_widening_other_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = root / "observations.json"
            # A valid large JSON document can contain whitespace. It proves the
            # CLI reaches validation beyond the old 8 MiB reader ceiling without
            # fabricating a successful study or allocating a huge test fixture.
            observations.write_text(" " * (8 * 1024 * 1024) + "{}")
            with self.assertRaisesRegex(ValueError, "exceeds"):
                _read(observations)
            self.assertEqual(_read(observations, max_bytes=STUDY_EVIDENCE_MAX_BYTES), {})
            manifest = root / "protocol.json"
            manifest.write_text('{"spec":"unsupported"}')
            report_path = root / "report.json"
            args = parser().parse_args(["rystic", "study-report", "--manifest", str(manifest),
                "--observations", str(observations), "--output", str(report_path)])
            with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No live access")), \
                 patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
                result = execute(args)
            self.assertEqual(result["statistical_acceptance"], "inconclusive")
            self.assertIn("Unsupported study specification", result["reasons"])
            self.assertFalse(result["claim_eligible"])
            small = root / "small.json"
            small.write_text('{"a":1}')
            self.assertEqual(_read(small, max_bytes=7), {"a": 1})
            with self.assertRaisesRegex(ValueError, "exceeds"):
                _read(small, max_bytes=6)
            for limit in (0, True, 1.5, STUDY_EVIDENCE_MAX_BYTES + 1):
                with self.assertRaises(ValueError):
                    _read(small, max_bytes=limit)

    def test_templates_precede_all_transitions_and_match_each_observed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared, _, _ = binding_fixture(root, "baseline")
            prepared_path = root / "prepared-baseline.json"
            pair = MutationPair(prepared)
            workspace = Workspace(root / "workspace")

            def command(*arguments):
                args = parser().parse_args(["--workspace", str(workspace.root), "rystic", *arguments])
                return execute(args)

            templates_path = root / "templates.json"
            with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No live access")), \
                 patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
                summary = command("task-templates", "--plan", str(prepared_path), "--output", str(templates_path))
            templates = json.loads(templates_path.read_text())
            self.assertEqual(summary["tasks"], 12)
            self.assertEqual(summary["source_calls"], 0)
            self.assertEqual(summary["model_calls"], 0)
            self.assertFalse(workspace.root.exists())
            original_templates = templates_path.read_bytes()
            checked_tasks = 0

            with patch("contextbase.rystic.RysticPair", return_value=pair), \
                 patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
                for index, phase in enumerate(prepared["phases"]):
                    phase_id = phase["id"]
                    if index:
                        transition = prepared["transitions"][index - 1]
                        receipt_path = root / (phase_id + "-transition.json")
                        result = command("apply-phase", "--plan", str(prepared_path),
                            "--from-phase", transition["from_phase"], "--to-phase", transition["to_phase"],
                            "--approve-plan", prepared["plan_sha256"], "--approve-action", transition["action_sha256"],
                            "--output", str(receipt_path))
                        self.assertEqual(result["outcome"], "applied")
                        self.assertTrue(result["after_state_matches"])
                        self.assertFalse(result["workspace_imported"])
                        self.assertFalse(result["claim_eligible"])
                        self.assertEqual(result["model_calls"], 0)
                        self.assertNotIn("after_capture", result)
                        self.assertNotIn("operations", result)
                        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
                    capture_path = root / (phase_id + "-capture.json")
                    if phase["kind"] == "guarded":
                        with self.assertRaises(ValueError):
                            command("sync", "--capture-output", str(capture_path))
                        self.assertFalse(refresh_status(workspace)["snapshot_reads_allowed"])
                        with self.assertRaises(ValueError):
                            workspace.query("SELECT COUNT(*) FROM stripe.customers")
                        snapshot_args = []
                    else:
                        snapshot = command("sync", "--capture-output", str(capture_path))
                        snapshot_path = root / (phase_id + "-snapshot.json")
                        snapshot_path.write_text(json.dumps(snapshot))
                        self.assertTrue(refresh_status(workspace)["snapshot_reads_allowed"])
                        snapshot_args = ["--snapshot", str(snapshot_path)]
                    bound_path = root / (phase_id + "-bound.json")
                    bound_summary = command("bind-phase", "--plan", str(prepared_path), "--phase", phase_id,
                        "--validation", str(capture_path), "--output", str(bound_path), *snapshot_args)
                    self.assertFalse(bound_summary["claim_eligible"])
                    packet = json.loads(bound_path.read_text())
                    for public, private in zip(packet["public_tasks"], packet["evaluator_tasks"]):
                        self.assertEqual(task_commitment(public, private["expected"], prepared["scorer_sha256"]),
                                         templates["task_commitments"][private["id"]])
                        checked_tasks += 1
                    self.assertEqual(templates_path.read_bytes(), original_templates)
            self.assertEqual(checked_tasks, 12)
            self.assertEqual(len(pair.writes), sum(len(item["changes"]) for item in prepared["transitions"]))


if __name__ == "__main__":
    unittest.main()
