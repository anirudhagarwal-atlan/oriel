"""Acceptance: real dlt extraction against local authored sources only.

SQLite transports both source-shaped profiles. The packaged REST/GraphQL
adapters use the existing loopback harness. Vendor compatibility and actual
model execution are deliberately not established by this local suite.
"""
from contextlib import closing
from importlib import util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.connectors.worker import _PROCESS_ENV
from contextbase.customer360 import agent, answers, contexts, identity, profiles, snapshots
from contextbase.customer360.contracts import PROFILE_SPEC
from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "customer-360"


def module(name, path):
    spec = util.spec_from_file_location(name, path)
    loaded = util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


runtime = module("source_pipeline_runtime", EXAMPLE / "runtime.py")
review = module("source_pipeline_review", EXAMPLE / "review.py")
api = module("source_pipeline_api_fixture", ROOT / "examples" / "connectors" / "verify_apis.py")
LAUNCHER = ROOT / "examples" / "connectors" / "api_fixture_worker.py"


def api_command(workspace, fixture, *arguments, expect_failure=False):
    # Only the two generated fixture tokens and allowlisted process settings
    # enter the launcher; ambient vendor credentials are never copied.
    environment = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
    environment.update(CONTEXTBASE_VERIFY_REST_TOKEN=api.SYNTHETIC_TOKEN,
                       CONTEXTBASE_VERIFY_GRAPHQL_TOKEN=api.SYNTHETIC_TOKEN)
    completed = subprocess.run([sys.executable, str(LAUNCHER), "--fixture-origin", fixture.origin, "--",
                                "--workspace", str(workspace.root), *arguments],
                               env=environment, capture_output=True, text=True, timeout=180, check=False)
    if bool(completed.returncode) != expect_failure:
        raise AssertionError("Unexpected fixture CLI outcome for " + " ".join(arguments[:2]))
    return json.loads(completed.stderr if completed.returncode else completed.stdout)


def approve_api_context(workspace):
    """Explicit synthetic operator review, independent of the example oracle."""
    customer = identity.create_customer(workspace, "example-api-customer")["id"]
    links = [{"connection": "crm", "entity": "contact", "source_id": key, "customer_id": customer,
              "basis": "Reviewed synthetic account membership for the local fixture"}
             for key in ("contact_001", "contact_002")]
    proposed = identity.propose_links(workspace, links)
    identity.approve_links(workspace, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
    snapshot = snapshots.capture_snapshot(workspace, ["crm", "tracker"])["snapshot_id"]
    definition = {"spec_version": PROFILE_SPEC, "name": "api_review", "description": "Synthetic contacts and issues",
                  "bindings": [
                      {"connection": "crm", "entity": "contact", "table": "crm.contacts", "kind": "usage", "record_id": "id",
                       "customer_ref": {"connection": "crm", "entity": "contact", "column": "id"},
                       "fields": {"seats": "custom_attributes__seats", "status": "custom_attributes__account_status"}},
                      {"connection": "tracker", "entity": "issue", "table": "tracker.issues", "kind": "support", "record_id": "id",
                       "customer_ref": {"connection": "crm", "entity": "contact", "column": "description"},
                       "fields": {"text": "description", "status": "state__type", "priority": "priority"}}]}
    proposed = profiles.propose_profile(workspace, definition, snapshot)
    profiles.approve_profile(workspace, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
    profiles.materialize(workspace, "api_review", snapshot)
    definitions = [{"name": "active_seats", "description": "Sum observed seats on active synthetic contacts",
                    "kind": "usage", "operation": "sum", "field": "seats", "unit": "number", "currency_field": None,
                    "filters": [{"field": "status", "operator": "eq", "value": "active"}]}]
    proposed = contexts.propose_context(workspace, "api_review", snapshot, definitions,
                                        gaps=[{"code": "synthetic_only", "description": "These API responses are authored local fixtures."}])
    context = contexts.approve_context(workspace, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
    return {"profile": "api_review", "snapshot_id": snapshot, "customer_id": customer, "context_id": context["context_id"]}


class ScriptedMetricSelection:
    """Deterministic model-transport double, never an actual model smoke run."""
    def __init__(self, definition_name):
        self.definition_name, self.calls = definition_name, 0

    def complete(self, system, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            result = {"tools": [{"tool": "metric", "arguments": {"definition_name": self.definition_name}}]}
        else:
            metric = json.loads(messages[-1]["content"])["result"]
            result = {"answer": {"metrics": [{"definition_name": self.definition_name, "result_id": metric["result_id"]}], "quotes": []}}
        return {"response": json.dumps(result), "cost_usd": 0.0, "cost_known": True,
                "model_usage": {"scripted-test-double": {}}, "is_error": False}


class SourcePipelineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # A missing native runtime fails the gate, never skips it.
        api.prerequisites()
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.native = {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(cls.root / profile)
            extraction = runtime.load_profile(workspace, profile, "initial")
            approved = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
            cls.native[profile] = (workspace, extraction, approved)
        cls.api_workspace = Workspace(cls.root / "loopback")
        cls.api_receipts = {}
        with api.FixtureServer() as fixture:
            for source, connection, credential, environment, resource in (
                    ("intercom", "crm", "token", "CONTEXTBASE_VERIFY_REST_TOKEN", "contacts"),
                    ("linear", "tracker", "api_key", "CONTEXTBASE_VERIFY_GRAPHQL_TOKEN", "issues")):
                api_command(cls.api_workspace, fixture, "source", "add", source, "--name", connection,
                            "--credential-env", credential + "=" + environment, "--resource", resource)
                cls.api_receipts[connection] = api_command(cls.api_workspace, fixture, "source", "sync", connection, "--no-bootstrap")
            cls.api_requests = list(fixture.requests)
            cls.closed_origin = fixture.origin
        cls.api_review = approve_api_context(cls.api_workspace)

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_case.cleanup)
        self.case_root = Path(self.temp_case.name)

    def copy_workspace(self, source, name):
        target = self.case_root / name
        shutil.copytree(source.root, target)
        return Workspace(target)

    def test_real_connector_runtime_extracts_both_profiles(self):
        for profile, (workspace, extraction, approved) in self.native.items():
            with self.subTest(profile=profile):
                self.assertTrue(extraction["actual_dlt_executed"])
                self.assertEqual(extraction["status"], "passed")
                self.assertEqual(extraction["runtime_versions"]["dlt"], "1.24.0")
                self.assertFalse(extraction["vendor_adapter_verified"])
                self.assertEqual(extraction["model_calls"], 0)
                self.assertEqual(len(extraction["publications"]), 2)
                manifest = json.loads((EXAMPLE / "manifest.json").read_text())["profiles"][profile]["phases"]["initial"]
                for resource in extraction["resources"]:
                    expected = json.loads((EXAMPLE / manifest[resource["resource"]]).read_text())
                    actual = workspace.query("SELECT record_json FROM " + resource["table"] + " ORDER BY fixture_position")["rows"]
                    self.assertEqual([json.loads(row["record_json"]) for row in actual], expected)
                for receipt in extraction["publications"]:
                    self.assertEqual((receipt["source_type"], receipt["status"]), ("sqlite", "synced"))
                    worker = workspace.root / "connectors" / receipt["name"] / receipt["attempt"] / "result.json"
                    self.assertEqual(json.loads(worker.read_text())["status"], "success")
                state = contexts.current_context(workspace, approved["profile"], approved["snapshot_id"])
                self.assertEqual(state["context_id"], approved["context_id"])

    def test_pagination_nested_records_and_resource_bounds(self):
        for protocol, offsets in (("rest", {0, 2}), ("graphql", {0, 2, 4})):
            pages = [request for request in self.api_requests if request["protocol"] == protocol]
            self.assertEqual({request["offset"] for request in pages}, offsets)
            self.assertTrue(all(request["status"] == 200 for request in pages))
        oracle = api.expected_rows(api.CONTACTS_INITIAL, api.ISSUES_INITIAL)
        contacts = self.api_workspace.query("SELECT id,custom_attributes__account_status AS status,custom_attributes__seats AS seats FROM crm.contacts ORDER BY id")["rows"]
        issues = self.api_workspace.query("SELECT id,description AS account_id,priority,state__type AS state FROM tracker.issues ORDER BY id")["rows"]
        self.assertEqual(contacts, oracle["contacts"])
        self.assertEqual(issues, oracle["issues"])
        self.assertEqual({row["schema"] + "." + row["table"] for row in self.api_workspace.catalog() if row["schema"] in {"crm", "tracker"}},
                         {"crm.contacts", "tracker.issues"})
        for connection, receipt in self.api_receipts.items():
            guard = json.loads((self.api_workspace.root / "connectors" / connection / receipt["attempt"] / "result.json.network.json").read_text())
            self.assertEqual(guard["blocked_network_attempts"], 0)
            self.assertGreaterEqual(guard["allowed_connects"], 2)
        for workspace, extraction, _ in self.native.values():
            self.assertEqual({item["schema"] + "." + item["table"] for item in workspace.catalog() if item["schema"].startswith("fixture_")},
                             {resource["table"] for resource in extraction["resources"]})
            for source in extraction["source_label_mapping"]:
                with closing(sqlite3.connect(workspace.root / "customer360-fixture-runtime" / (source + ".sqlite"))) as database:
                    self.assertEqual(database.execute("SELECT count(*) FROM excluded_fixture_sentinel").fetchone(), (1,))

    def test_partial_extraction_is_not_published(self):
        for protocol, connection in (("rest", "crm"), ("graphql", "tracker")):
            with self.subTest(protocol=protocol):
                workspace = self.copy_workspace(self.api_workspace, "partial-" + protocol)
                before = workspace.query(api.SQL)["rows"]
                before_tables = {table: workspace.query("SELECT * FROM " + table + " ORDER BY id")["rows"]
                                 for table in ("crm.contacts", "tracker.issues")}
                old = next(item for item in service.configured(workspace)["sources"] if item["name"] == connection)["last_snapshot"]
                with api.FixtureServer() as fixture:
                    fixture.set_state("refreshed", failure=protocol)
                    result = api_command(workspace, fixture, "source", "sync", connection, "--no-bootstrap", expect_failure=True)
                    requests = list(fixture.requests)
                self.assertIn("previous data was retained", result["error"])
                self.assertTrue(any(item["offset"] == 0 and item["status"] == 200 for item in requests))
                self.assertTrue(any(item["offset"] == 2 and item["status"] == 503 for item in requests))
                source = next(item for item in service.configured(workspace)["sources"] if item["name"] == connection)
                self.assertEqual(source["last_snapshot"], old)
                self.assertEqual(source["last_sync"]["status"], "failed")
                self.assertNotEqual(source["last_sync"]["attempt"], old["attempt"])
                self.assertEqual(workspace.query(api.SQL)["rows"], before)
                for table, rows in before_tables.items():
                    self.assertEqual(workspace.query("SELECT * FROM " + table + " ORDER BY id")["rows"], rows)
                committed = old["table_evidence"]
                self.assertEqual(source["last_snapshot"]["table_evidence"], committed)
                with self.assertRaisesRegex(ValueError, "failed latest sync"):
                    contexts.inspect_context(workspace, self.api_review["profile"], self.api_review["snapshot_id"])
                with self.assertRaisesRegex(ValueError, "failed latest sync"):
                    answers.brief(workspace, self.api_review["profile"], self.api_review["snapshot_id"], self.api_review["customer_id"])

        # The shared HTTP-attempt budget must stop actual extraction before
        # page two for either transport, while retaining the prior publication.
        for protocol, connection in (("rest", "crm"), ("graphql", "tracker")):
            with self.subTest(protocol=protocol, refusal="request_budget"):
                workspace = self.copy_workspace(self.api_workspace, "budget-" + protocol)
                before = workspace.query(api.SQL)["rows"]
                old = next(item for item in service.configured(workspace)["sources"] if item["name"] == connection)["last_snapshot"]
                with api.FixtureServer() as fixture:
                    api_command(workspace, fixture, "source", "sync", connection, "--no-bootstrap",
                                "--max-requests", "1", "--request-seconds", "120", expect_failure=True)
                    observed = list(fixture.requests)
                self.assertEqual(len(observed), 1)
                self.assertEqual((observed[0]["offset"], observed[0]["status"]), (0, 200))
                current = next(item for item in service.configured(workspace)["sources"] if item["name"] == connection)
                self.assertEqual(current["last_snapshot"], old)
                self.assertEqual(current["last_sync"]["status"], "failed")
                budget = current["last_sync"]["request_budget"]
                self.assertEqual(budget["counts"]["reserved"], 1)
                self.assertEqual(budget["counts"]["succeeded"], 1)
                self.assertEqual(budget["stop_reason"], "request_limit")
                self.assertEqual(workspace.query(api.SQL)["rows"], before)
                with self.assertRaisesRegex(ValueError, "failed latest sync"):
                    contexts.inspect_context(workspace, self.api_review["profile"], self.api_review["snapshot_id"])
                with self.assertRaisesRegex(ValueError, "failed latest sync"):
                    answers.brief(workspace, self.api_review["profile"], self.api_review["snapshot_id"], self.api_review["customer_id"])

    def test_context_only_reads_committed_duckdb_snapshot(self):
        for profile, (original, _, approved) in self.native.items():
            with self.subTest(profile=profile):
                workspace = self.copy_workspace(original, "unsynced-" + profile)
                before = contexts.inspect_context(workspace, approved["profile"], approved["snapshot_id"])
                customer = next(iter(approved["customer_ids"].values()))
                answer_before = answers.brief(workspace, approved["profile"], approved["snapshot_id"], customer)
                source, table, field = (("salesforce", "opportunity", "Amount") if profile == "salesforce-gong"
                                        else ("stripe", "invoices", "amount_remaining"))
                with closing(sqlite3.connect(workspace.root / "customer360-fixture-runtime" / (source + ".sqlite"))) as database, database:
                    database.execute(f'UPDATE "{table}" SET "{field}"=987654321')
                    self.assertEqual(database.execute(f'SELECT DISTINCT "{field}" FROM "{table}"').fetchall(), [(987654321,)])
                self.assertEqual(contexts.inspect_context(workspace, approved["profile"], approved["snapshot_id"]), before)
                answer_after = answers.brief(workspace, approved["profile"], approved["snapshot_id"], customer)
                self.assertEqual(answer_after["answer_id"], answer_before["answer_id"])
                self.assertEqual(answer_after["metrics"], answer_before["metrics"])
                self.assertEqual(answer_after["source_observed_at"], answer_before["source_observed_at"])

    def test_answer_path_works_with_vendor_network_unavailable(self):
        # The fixture API server has actually stopped. In-process socket calls
        # are additionally blocked so accidental source access cannot succeed.
        scopes = [(self.api_workspace, self.api_review["profile"], self.api_review["snapshot_id"], self.api_review["customer_id"])]
        scopes += [(workspace, approved["profile"], approved["snapshot_id"], next(iter(approved["customer_ids"].values())))
                   for workspace, _, approved in self.native.values()]
        with patch.object(socket.socket, "connect", side_effect=OSError("synthetic network unavailable")) as connect:
            for index, (original, profile, snapshot, customer) in enumerate(scopes):
                with self.subTest(profile=profile):
                    workspace = self.copy_workspace(original, "offline-" + str(index))
                    brief = answers.brief(workspace, profile, snapshot, customer)
                    self.assertTrue(brief["metrics"])
                    definition_name = brief["metrics"][0]["result"]["definition_name"]
                    scripted = ScriptedMetricSelection(definition_name)
                    result = agent.ask(workspace, profile, snapshot, customer, "Show the reviewed metric for this customer.",
                                       allow_model_data=True, max_turns=2, max_cost_usd=0.01, client=scripted)
                    self.assertEqual(result["status"], "answered")
                    self.assertEqual(result["answer"]["metrics"][0]["result"], brief["metrics"][0]["result"])
                    self.assertEqual(result["answer"]["context_id"], brief["context_id"])
                    self.assertEqual(result["answer"]["snapshot_id"], snapshot)
                    self.assertEqual(scripted.calls, 2)
                    self.assertEqual(result["model_origin"], "injected_client_unverified")
                    self.assertFalse(result["claim_eligible"])
                    receipt = json.loads(Path(result["receipt_path"]).read_text())
                    self.assertEqual(receipt["model_origin"], "injected_client_unverified")
                    self.assertEqual(receipt["usage"]["model_calls"], 2)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
