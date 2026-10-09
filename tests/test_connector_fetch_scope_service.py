"""Offline parent/CLI scope contracts with real private ledgers and DuckDB.

Worker doubles author synthetic staged data; no HTTP request is sent and these
tests make no claim of live extraction or vendor authorization.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import duckdb
import requests

from contextbase.cli import execute, parser
from contextbase.connectors import service
from contextbase.connectors.budget import ExtractionBudget, compact_summary as compact_budget
from contextbase.customer360.application import Application
from contextbase.workspace import Workspace


SCOPE = {"origin": "https://api.intercom.io", "fetch_resources": ["contacts"], "max_pages": 3, "max_rows": 10}
BUDGET = {"max_requests": 5, "max_seconds": 30}


class FetchScopeEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / "workspace")

    def cli(self, *arguments):
        return execute(parser().parse_args(["--workspace", str(self.workspace.root), "source", "sync", "sample", *arguments]))

    def test_cli_scope_is_all_or_none_and_requires_both_request_limits(self):
        fields = [("--scope-origin", SCOPE["origin"]), ("--fetch-resource", "contacts"),
                  ("--max-pages", "3"), ("--max-rows", "10")]
        budget = ["--max-requests", "5", "--request-seconds", "30"]
        full = [value for pair in fields for value in pair]
        with patch.object(service, "sync") as sync:
            for missing in range(len(fields)):
                args = [value for index, pair in enumerate(fields) if index != missing for value in pair]
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "together"):
                    self.cli(*args, *budget)
            for limits in ([], budget[:2], budget[2:]):
                with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, "together"):
                    self.cli(*full, *limits)
            sync.assert_not_called()
        self.assertFalse(self.workspace.root.exists())

    def test_cli_scope_forwarding_keeps_output_selection_separate(self):
        args = ["--scope-origin", SCOPE["origin"], "--fetch-resource", "contacts", "--fetch-resource", "companies",
                "--max-pages", "3", "--max-rows", "10", "--max-requests", "5", "--request-seconds", "30", "--no-bootstrap"]
        with patch.object(service, "sync", return_value={"transport_test": True}) as sync:
            self.assertEqual(self.cli(*args), {"transport_test": True})
            self.assertEqual(sync.call_args.args[1], "sample")
            self.assertEqual(sync.call_args.kwargs, {"timeout": 300, "bootstrap_context": False, "request_budget": BUDGET,
                                                    "fetch_scope": {**SCOPE, "fetch_resources": ["contacts", "companies"]}})
            self.cli()
            self.assertIsNone(sync.call_args.kwargs["fetch_scope"])
            self.assertIsNone(sync.call_args.kwargs["request_budget"])
        self.assertFalse(self.workspace.root.exists())

    def test_actual_cli_incomplete_scope_refuses_before_workspace_creation(self):
        completed = subprocess.run(
            [sys.executable, "-m", "contextbase", "--workspace", str(self.workspace.root),
             "source", "sync", "sample", "--scope-origin", SCOPE["origin"],
             "--max-requests", "5", "--request-seconds", "30"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("together", completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertFalse(self.workspace.root.exists())

    def test_application_source_sync_forwards_scope_without_model_authority(self):
        def forbidden_model():
            raise AssertionError("No model may be initialized by source sync.")
        app = Application(self.workspace, model_client_factory=forbidden_model)
        self.addCleanup(app.close)
        with patch.object(service, "sync", return_value={"transport_test": True}) as sync:
            submitted = app.submit("source.sync", {"name": "sample", "request_budget": BUDGET, "fetch_scope": SCOPE})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                receipt = app.job(submitted["job_id"])
                if receipt["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(receipt["status"], "succeeded")
            sync.assert_called_once_with(self.workspace, "sample", bootstrap_context=False,
                                         request_budget=BUDGET, fetch_scope=SCOPE)
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                app.submit("source.sync", {"name": "sample", "fetch_scope": SCOPE, "client": "not-permitted"})


class FetchScopePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.config = service.add(self.workspace, "intercom", name="sample", resources=["contacts"],
                                  credential_env={"token": "SYNTHETIC_SCOPE_TOKEN"})
        initial = self.root / "initial.duckdb"
        self.stage(initial, 10)
        self.initial = service._publish(self.workspace, self.config, initial)

    def stage(self, path, value):
        with duckdb.connect(str(path)) as conn:
            conn.execute("CREATE SCHEMA sample")
            conn.execute("CREATE TABLE sample.contacts AS SELECT 'synthetic_1' AS id, ?::BIGINT AS value", [value])

    def current_value(self):
        return self.workspace.query("SELECT value FROM sample.contacts")["rows"]

    def latest_attempt(self):
        directory = self.workspace.root / "connectors/sample"
        status = json.loads((directory / "status.json").read_text())
        return directory / status["attempt"], status

    def worker(self, mode="success"):
        def run(command, **kwargs):
            from contextbase.connectors.fetch_scope import FetchScope, compile_scope, compact_summary, plan_hash, read_scope
            request = json.loads(Path(command[command.index("--request") + 1]).read_text())
            self.assertEqual(request["fetch_scope"], SCOPE)
            self.assertEqual(request["resources"], ["contacts"])
            plan = compile_scope(request["source_type"], request["resources"], request["params"], request["fetch_scope"])
            self.assertEqual(request["fetch_scope_sha256"], plan_hash(plan))
            attempt = Path(request["work_dir"])
            budget = ExtractionBudget(attempt / "request-budget.json", request["request_budget"])
            if mode == "missing":
                result = {"status": "success", "request_budget": compact_budget(budget.finish())}
            else:
                if mode == "wrong_plan":
                    plan = compile_scope(request["source_type"], request["resources"], request["params"], {**SCOPE, "max_rows": 9})
                guard = FetchScope(attempt / "fetch-scope.json", plan)
                wire = requests.Request("GET", SCOPE["origin"] + "/contacts")
                guard.begin_chain("contacts", wire)
                prepared = wire.prepare()
                guard.prepare_request(wire, prepared)
                budget_request = budget.reserve_request()
                guard.before_dispatch(prepared)
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                if mode == "failed":
                    summary = guard.fail("worker_failed")
                    budget_summary = budget.fail("worker_failed")
                elif mode == "unfinished_success":
                    summary = read_scope(attempt / "fetch-scope.json")
                    budget.complete_request(budget_request["request_id"], status_code=200)
                    budget_summary = budget.finish()
                else:
                    response = requests.Response()
                    response.status_code = 200
                    response.request = prepared
                    rows = [{"id": "synthetic_1", "value": 20}]
                    response._content = json.dumps({"data": rows, "pages": {"next": None}}).encode()
                    guard.observe_page("contacts", response, rows, False)
                    budget.complete_request(budget_request["request_id"], status_code=200)
                    summary = guard.finish()
                    if mode == "budget_extra_request":
                        extra_request = budget.reserve_request()
                        budget.complete_request(extra_request["request_id"], status_code=200)
                    budget_summary = budget.finish()
                result = {"status": "error" if mode == "failed" else "success", "error_code": "fetch_scope_failed",
                          "fetch_scope": compact_summary(summary), "request_budget": compact_budget(budget_summary)}
                if mode == "worker_tamper":
                    result["fetch_scope"]["counts"]["rows"] = True  # bool must not equal integer 1.
                if mode == "ledger_tamper":
                    path = attempt / "fetch-scope.json"
                    raw = json.loads(path.read_text())
                    raw["status"] = "active"
                    path.write_text(json.dumps(raw))
            self.stage(Path(request["staging_database"]), 20)
            Path(command[-1]).write_text(json.dumps(result))
            return subprocess.CompletedProcess(command, 1 if mode == "failed" else 0)
        return run

    def sync(self):
        return service.sync(self.workspace, "sample", request_budget=BUDGET, fetch_scope=SCOPE, bootstrap_context=False)

    def test_scope_requires_request_budget_before_loading_configuration(self):
        with patch.object(service, "_load", side_effect=AssertionError("Must not read config")), \
                patch.object(service.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "request budget"):
            service.sync(self.workspace, "sample", fetch_scope=SCOPE)
        run.assert_not_called()
        self.assertEqual(self.current_value(), [{"value": 10}])

    def test_invalid_scope_preflight_never_launches_worker_or_reads_credentials(self):
        environment_get = os.environ.get
        def no_credential_read(name, default=None):
            if name == "SYNTHETIC_SCOPE_TOKEN":
                raise AssertionError("No credential may be read before scope validation.")
            return environment_get(name, default)
        for scope in ({**SCOPE, "fetch_resources": []}, {**SCOPE, "max_rows": True},
                      {**SCOPE, "origin": "https://example.invalid"}, {**SCOPE, "extra": "refused"}):
            with self.subTest(scope_keys=sorted(scope)), patch.object(service.subprocess, "run") as run, \
                    patch.object(os.environ, "get", side_effect=no_credential_read), \
                    self.assertRaises(ValueError):
                service.sync(self.workspace, "sample", request_budget=BUDGET, fetch_scope=scope)
            run.assert_not_called()
            attempt, status = self.latest_attempt()
            self.assertEqual(status["error_code"], "preflight_failed")
            self.assertEqual(status["fetch_scope"]["status"], "unavailable")
            self.assertFalse((attempt / "request.json").exists())
            self.assertEqual(self.current_value(), [{"value": 10}])

    def test_completed_scope_is_independently_reconciled_and_published(self):
        from contextbase.connectors.fetch_scope import compact_summary, read_scope
        with patch.object(service.subprocess, "run", side_effect=self.worker()):
            receipt = self.sync()
        self.assertEqual(receipt["status"], "synced")
        attempt, status = self.latest_attempt()
        self.assertEqual(receipt["fetch_scope"], compact_summary(read_scope(attempt / "fetch-scope.json")))
        self.assertEqual(receipt["fetch_scope"]["status"], "completed")
        self.assertEqual(receipt["fetch_scope"]["counts"], {"requests": 1, "pages": 1, "rows": 1, "pending": 0})
        self.assertEqual(self.current_value(), [{"value": 20}])
        committed = service.configured(self.workspace)["sources"][0]["last_snapshot"]
        self.assertEqual(committed["fetch_scope"], receipt["fetch_scope"])
        self.assertEqual(committed["fetch_scope_sha256"], receipt["fetch_scope"]["plan_sha256"])
        self.assertEqual(status["fetch_scope"], receipt["fetch_scope"])

    def test_missing_tampered_mismatched_or_failed_evidence_never_publishes(self):
        for mode in ("missing", "worker_tamper", "ledger_tamper", "wrong_plan", "failed", "unfinished_success"):
            with self.subTest(mode=mode), patch.object(service.subprocess, "run", side_effect=self.worker(mode)), \
                    patch.object(service, "_publish", side_effect=AssertionError("Must not publish incomplete scope")), \
                    self.assertRaises((RuntimeError, ValueError)):
                self.sync()
            attempt, status = self.latest_attempt()
            self.assertEqual(status["status"], "failed")
            self.assertIn("fetch_scope", status)
            self.assertEqual(json.loads((attempt / "receipt.json").read_text())["status"], "failed")
            self.assertEqual(self.current_value(), [{"value": 10}])
            self.assertEqual(service.configured(self.workspace)["sources"][0]["last_snapshot"]["snapshot_sha256"], self.initial["snapshot_sha256"])

    def test_timeout_retains_pending_scope_and_old_publication_without_retry(self):
        with patch.object(service.subprocess, "run", side_effect=self.worker("timeout")) as run, \
                self.assertRaisesRegex(RuntimeError, "timeout"):
            self.sync()
        run.assert_called_once()
        attempt, status = self.latest_attempt()
        self.assertEqual(status["error_code"], "timeout")
        self.assertEqual(status["fetch_scope"]["counts"]["pending"], 1)
        self.assertEqual(status["fetch_scope"]["status"], "active")
        self.assertEqual(status["request_budget"]["counts"]["pending"], 1)
        self.assertTrue((attempt / "fetch-scope.json").is_file())
        self.assertEqual(self.current_value(), [{"value": 10}])

    def test_completed_ledgers_with_different_request_counts_never_publish(self):
        from contextbase.connectors.budget import read_budget
        from contextbase.connectors.fetch_scope import read_scope
        with patch.object(service.subprocess, "run", side_effect=self.worker("budget_extra_request")), \
                patch.object(service, "_publish", side_effect=AssertionError("Must not publish inconsistent ledgers")) as publish, \
                self.assertRaisesRegex(RuntimeError, "counts did not reconcile"):
            self.sync()
        publish.assert_not_called()
        attempt, status = self.latest_attempt()
        scope, budget = read_scope(attempt / "fetch-scope.json"), read_budget(attempt / "request-budget.json")
        self.assertEqual(scope["status"], "completed")
        self.assertEqual(budget["status"], "completed")
        self.assertEqual(scope["counts"]["requests"], 1)
        self.assertEqual(scope["counts"]["pages"], 1)
        self.assertEqual(budget["counts"]["reserved"], 2)
        self.assertEqual(budget["counts"]["succeeded"], 2)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["fetch_scope"]["counts"]["requests"], 1)
        self.assertEqual(status["request_budget"]["counts"]["reserved"], 2)
        self.assertEqual(self.current_value(), [{"value": 10}])
        self.assertEqual(service.configured(self.workspace)["sources"][0]["last_snapshot"]["snapshot_sha256"],
                         self.initial["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
