"""Static connector plans through the actual CLI; no source/model execution."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contextbase import cli


ROOT = Path(__file__).resolve().parents[1]


class SourcePlanCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "must-not-be-created"

    def run_cli(self, *arguments):
        result = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(self.workspace),
                                 "source", "plan", *arguments], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertFalse(self.workspace.exists())
        return result

    def test_actual_cli_separates_selected_outputs_and_required_parent_fetches(self):
        result = self.run_cli("discord", "--resource", "messages", "--resource", "channels")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        plan = json.loads(result.stdout)
        self.assertEqual(plan["requested_resources"], ["messages", "channels"])
        self.assertEqual(plan["selected_outputs"], ["channels", "messages"])
        self.assertEqual(plan["required_fetch_resources"], ["channels", "guilds", "messages"])
        self.assertEqual(plan["dependency_edges"], [{"resource": "channels", "requires": "guilds"},
                                                    {"resource": "messages", "requires": "channels"}])
        self.assertEqual(plan["status"], "declaration_derived")
        self.assertFalse(plan["default_selection_used"])
        self.assertTrue(plan["limitations"])
        self.assertEqual(set(plan), {"source_type", "execution_family", "requested_resources", "selected_outputs",
                                     "required_fetch_resources", "dependency_edges", "default_selection_used", "status", "limitations"})

    def test_actual_cli_defaults_and_unknown_fetch_scope_are_explicit(self):
        result = self.run_cli("github")
        self.assertEqual(result.returncode, 0, result.stderr)
        defaults = json.loads(result.stdout)
        self.assertTrue(defaults["default_selection_used"])
        self.assertEqual(defaults["requested_resources"], [])
        self.assertNotIn("pull_requests", defaults["selected_outputs"])
        result = self.run_cli("postgres")
        self.assertEqual(result.returncode, 0, result.stderr)
        unknown = json.loads(result.stdout)
        self.assertEqual(unknown["status"], "fetch_scope_unknown")
        self.assertIsNone(unknown["selected_outputs"])
        self.assertIsNone(unknown["required_fetch_resources"])

    def test_actual_cli_invalid_selections_fail_without_creating_workspace(self):
        for arguments in (("not_a_connector",), ("discord", "--resource", "no_such_resource"),
                          ("discord", "--resource", "messages", "--resource", "messages"),
                          ("discord", "--resource", "../messages")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(set(json.loads(result.stderr)), {"error"})

    def test_plan_dispatch_does_not_even_construct_workspace(self):
        arguments = cli.parser().parse_args(["--workspace", str(self.workspace), "source", "plan", "discord", "--resource", "messages"])
        with patch.object(cli, "Workspace", side_effect=AssertionError("No workspace access")):
            result = cli.execute(arguments)
        self.assertEqual(result["selected_outputs"], ["messages"])
        self.assertFalse(self.workspace.exists())


if __name__ == "__main__":
    unittest.main()
