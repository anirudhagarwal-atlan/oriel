"""Offline CLI wiring and gold isolation; authored captures are not live proof."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.cli import execute, main, parser
from contextbase.refresh_state import refresh_status
from contextbase.study_binding import bind_phase
from contextbase.workspace import Workspace
from test_study_binding import FakePair, binding_fixture, source_rows


class StudyBindingCliTests(unittest.TestCase):
    def test_real_local_refresh_receipt_binds_to_its_retained_fake_source_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _, _ = binding_fixture(root, "baseline")
            workspace = Workspace(root / "workspace")
            packets = []
            for phase in ("baseline", "refreshed", "guard_a", "guard_b"):
                capture_path = root / (phase + "-capture.json")
                args = parser().parse_args(["--workspace", str(workspace.root), "rystic", "sync",
                                           "--capture-output", str(capture_path)])
                guarded = phase in {"guard_a", "guard_b"}
                with patch("contextbase.rystic.RysticPair", return_value=FakePair(source_rows(plan, phase))):
                    if guarded:
                        with self.assertRaises(ValueError):
                            execute(args)
                        receipt = None
                    else:
                        receipt = execute(args)
                capture = json.loads(capture_path.read_text())
                packet = bind_phase(plan, phase, capture, root / (phase + "-bound.json"), snapshot=receipt)
                self.assertEqual(packet["snapshot_receipt"], receipt)
                self.assertEqual(packet["validation_receipt"], capture["receipt"])
                self.assertEqual(refresh_status(workspace)["snapshot_reads_allowed"], not guarded)
                packets.append(packet)
            self.assertNotEqual(packets[0]["snapshot_receipt"]["data_sha256"],
                                packets[1]["snapshot_receipt"]["data_sha256"])
            self.assertNotEqual(packets[0]["evaluator_tasks"][-1]["expected"]["result"],
                                packets[1]["evaluator_tasks"][-1]["expected"]["result"])

    def test_all_phases_use_only_saved_files_and_keep_gold_out_of_stdout(self):
        for phase, count in (("baseline", 5), ("refreshed", 5), ("guard_a", 1), ("guard_b", 1)):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, capture, snapshot = binding_fixture(root, phase)
                paths = {}
                for name, value in (("plan", plan), ("validation", capture), ("snapshot", snapshot)):
                    paths[name] = root / ("cli-" + name + ".json")
                    paths[name].write_text(json.dumps(value, allow_nan=False))
                output = root / "bound.json"
                argv = ["contextbase", "--workspace", str(root / "unused-workspace"), "rystic", "bind-phase",
                        "--plan", str(paths["plan"]), "--phase", phase,
                        "--validation", str(paths["validation"]), "--output", str(output)]
                if snapshot is not None:
                    argv += ["--snapshot", str(paths["snapshot"])]
                stdout = io.StringIO()
                with patch("sys.argv", argv), redirect_stdout(stdout), \
                     patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No source access")), \
                     patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model access")):
                    main()
                result = json.loads(stdout.getvalue())
                packet = json.loads(output.read_text())
                self.assertEqual(result["tasks"], count)
                self.assertEqual(result["binding_sha256"], packet["binding_sha256"])
                self.assertEqual(result["evidence_origin"], "synthetic_test")
                for key in ("source_calls", "model_calls", "execution_authorized", "claim_eligible", "held_out"):
                    self.assertFalse(result[key])
                for hidden in ("expected", "question", "evaluator_tasks", "public_tasks"):
                    self.assertNotIn(hidden, result)
                self.assertFalse((root / "unused-workspace").exists())
                self.assertEqual(len(packet["public_tasks"]), count)
                for task in packet["public_tasks"]:
                    self.assertEqual(set(task), {"question", "answer_schema"})

    def test_ambiguous_json_input_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "ambiguous.json"
            plan.write_text('{"spec":"first","spec":"second"}')
            output = root / "bound.json"
            args = parser().parse_args(["rystic", "bind-phase", "--plan", str(plan), "--phase", "baseline",
                                       "--validation", str(root / "not-read.json"), "--output", str(output)])
            with self.assertRaises(ValueError):
                execute(args)
            self.assertFalse(output.exists())
