"""Public CLI/application budget boundaries; no vendor or provider access."""
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_customer360_application as http


class CustomerSyncBudgetTests(unittest.TestCase):
    setUp = http.ApplicationHTTPTests.setUp
    close = http.ApplicationHTTPTests.close
    request = http.ApplicationHTTPTests.request
    submit = http.ApplicationHTTPTests.submit
    wait = http.ApplicationHTTPTests.wait
    run_job = http.ApplicationHTTPTests.run_job

    def test_http_explicit_budget_is_a_sync_option_not_a_vendor_parameter(self):
        budget = {"max_requests": 7, "max_seconds": 20}
        seen = []
        def observed(workspace, name, **kwargs):
            seen.append((workspace.root, name, kwargs))
            return {"status": "observed_test_dispatch", "actual_network_calls": 0}
        with patch("contextbase.customer360.application.service.sync", observed):
            result = self.run_job("source.sync", {"name": "example_api", "request_budget": budget})
        self.assertEqual(result["actual_network_calls"], 0)
        self.assertEqual(seen, [(self.workspace.root, "example_api", {"bootstrap_context": False, "request_budget": budget})])
        status, _, _ = self.submit("source.sync", {"name": "example_api", "request_budget": budget, "params": {"endpoint": "untrusted"}})
        self.assertEqual(status, 400)
        self.assertEqual(len(seen), 1)

    def test_file_source_cannot_claim_enforced_http_budget_and_keeps_published_data(self):
        path = Path(self.temp.name) / "synthetic.csv"
        path.write_text("id,value\ncustomer_123,10\n")
        self.run_job("source.add", {"source_type": "csv", "name": "sample_file", "path": str(path)})
        self.run_job("source.sync", {"name": "sample_file"})
        path.write_text("id,value\ncustomer_123,20\n")
        self.run_job("source.sync", {"name": "sample_file", "request_budget": {"max_requests": 1, "max_seconds": 10}}, failure=True)
        self.assertEqual(self.workspace.query("SELECT value FROM sample_file.records")["rows"], [{"value": 10}])
        self.assertEqual(self.factory_calls, 0)

    def test_cli_requires_complete_explicit_budget_before_source_access(self):
        command = [sys.executable, "-m", "contextbase", "--workspace", str(self.workspace.root), "source", "sync", "unknown_source"]
        for option in ("--max-requests", "--request-seconds"):
            with self.subTest(option=option):
                result = subprocess.run([*command, option, "5"], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Use --max-requests and --request-seconds together", result.stdout + result.stderr)
        self.assertFalse((self.workspace.root / "connectors/unknown_source").exists())

    def test_cli_budget_option_refuses_unsupported_source_without_dispatch(self):
        path = Path(self.temp.name) / "synthetic.csv"
        path.write_text("id,value\ncustomer_123,10\n")
        self.run_job("source.add", {"source_type": "csv", "name": "sample_file", "path": str(path)})
        result = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(self.workspace.root),
                                 "source", "sync", "sample_file", "--no-bootstrap", "--max-requests", "2", "--request-seconds", "10"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REST", result.stdout + result.stderr)
        self.assertEqual(self.factory_calls, 0)


if __name__ == "__main__":
    unittest.main()
