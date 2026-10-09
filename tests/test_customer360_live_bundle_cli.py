"""Repository CLI boundary checks; never vendor or model evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMMAND = [sys.executable, str(ROOT / "scripts/inspect_customer360_live_bundle.py")]


class LiveBundleCLITests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run([*COMMAND, *arguments], cwd=ROOT, capture_output=True,
                              text=True, timeout=10, check=False)

    def test_help_distinguishes_inspection_from_live_acceptance(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("does not pass the live acceptance gate", " ".join(result.stdout.split()))
        self.assertIn("--capability-sha256", result.stdout)

    def test_argument_failure_does_not_echo_submitted_values(self):
        result = self.run_cli("--unexpected-option", "synthetic-private-marker")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("synthetic-private-marker", result.stderr)
        self.assertEqual(json.loads(result.stderr),
                         {"status": "refused", "error_code": "invalid_inspection_arguments"})

    def test_missing_bundle_refuses_without_paths_or_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("--bundle", str(Path(directory) / "synthetic-private-path"),
                                  "--capabilities", str(Path(directory) / "capabilities.json"),
                                  "--capability-sha256", "0" * 64,
                                  "--checked-at", "2026-10-09T12:00:00Z")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("synthetic-private-path", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        report = json.loads(result.stderr)
        self.assertFalse(report["live_gate_passed"])
        self.assertFalse(report["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
