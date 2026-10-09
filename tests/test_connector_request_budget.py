"""Actual dlt/HTTP/DuckDB budget integration against synthetic loopback sources.

The fixture-only child changes source endpoints/declarations and denies every
other network destination. This is runtime evidence, never vendor validation.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from contextbase.connectors import describe, service, worker
from contextbase.connectors.budget import compact_summary, read_budget
from contextbase.workspace import Workspace


TOKEN = "synthetic-budget-fixture-only"
ROOT = Path(__file__).resolve().parents[1]


def rest_config(origin):
    return {"name": "intercom", "client": {"base_url": origin + "/rest/", "auth": {"type": "bearer", "token": "{token}"}},
            "resource_defaults": {"primary_key": "id", "write_disposition": "replace", "endpoint": {
                "data_selector": "data", "paginator": {"type": "cursor", "cursor_path": "next", "cursor_param": "cursor"}}},
            "resources": [
                {"name": "companies", "selected": False, "endpoint": {"path": "companies"}},
                {"name": "contacts", "parent": {"resource": "companies", "param": "company_id", "field": "id"},
                 "endpoint": {"path": "companies/{company_id}/contacts"}},
                {"name": "admins", "endpoint": {"path": "admins"}}]}


def _fixture_worker(origin, argv):
    spec = importlib.util.spec_from_file_location("fixture_guard", ROOT / "examples/connectors/api_fixture_worker.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.install_network_guard(origin)
    original = worker._runtime

    def runtime(root):
        result = original(root)
        from contextbase.connectors.upstream import sources, yaml_source
        old_load, old_entry = yaml_source.load_yaml_config, sources.get_source_entry

        def load(name):
            return rest_config(origin) if name == "intercom" else old_load(name)

        def lookup(name):
            entry = copy.deepcopy(old_entry(name))
            if name == "linear":
                entry.graphql_config["endpoint"] = origin + "/graphql"
                entry.graphql_config["resources"] = [{"name": name, "query":
                    "query($cursor: String) { " + name + "(first: 1, after: $cursor) { nodes { id value } pageInfo { hasNextPage endCursor } } }",
                    "data_path": "data." + name + ".nodes", "cursor_path": "data." + name + ".pageInfo"}
                    for name in ("issues", "users")]
            return entry

        yaml_source.load_yaml_config = load
        sources.load_yaml_config = load
        sources.get_source_entry = lookup
        return result

    worker._runtime = runtime
    return worker.main(argv)


class Fixture:
    def __init__(self):
        self.requests = []
        self.value = 1
        self.failure = False
        self.trickle = False
        self.lock = threading.Lock()
        self.connection_dir = None

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                target = urlsplit(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw) if raw else {}
                durable_reserved = None
                if fixture.connection_dir is not None:
                    status = json.loads((fixture.connection_dir / "status.json").read_text())
                    durable_reserved = read_budget(fixture.connection_dir / status["attempt"] / "request-budget.json")["counts"]["reserved"]
                with fixture.lock:
                    fixture.requests.append({"method": self.command, "path": target.path, "query": target.query,
                                             "durable_reserved": durable_reserved})
                    value, failure, trickle = fixture.value, fixture.failure, fixture.trickle
                status = 200
                if target.path == "/graphql":
                    name = "issues" if "issues(" in payload["query"] else "users"
                    second = payload.get("variables", {}).get("cursor") == "next"
                    body = {"data": {name: {"nodes": [{"id": name + ("_2" if second else "_1"), "value": value}],
                        "pageInfo": {"hasNextPage": not second, "endCursor": None if second else "next"}}}}
                elif target.path == "/rest/companies":
                    body = {"data": [{"id": "company_a"}, {"id": "company_b"}], "next": None}
                elif target.path == "/rest/admins":
                    body = {"data": [{"id": "admin_1", "value": value}], "next": None}
                elif target.path in {"/rest/companies/company_a/contacts", "/rest/companies/company_b/contacts"}:
                    second = parse_qs(target.query).get("cursor") == ["next"]
                    body = {"data": [{"id": target.path.split("/")[3] + ("_2" if second else "_1"), "value": value}],
                            "next": None if second else "next"}
                    if failure:
                        status, body = 503, {"error": "synthetic unavailable"}
                else:
                    status, body = 404, {"error": "undeclared fixture path"}
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    if trickle:
                        for byte in encoded:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.1)
                    else:
                        self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = handle_request
            do_POST = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class ConnectorRequestBudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.fixture = Fixture().__enter__()
        self.fixture.connection_dir = self.workspace.root / "connectors/sample"
        self.addCleanup(self.fixture.__exit__)
        native_run = subprocess.run

        def run(command, **kwargs):
            if command[1:3] == ["-m", "contextbase.connectors.worker"]:
                command = [command[0], str(Path(__file__).resolve()), "--budget-worker", self.fixture.origin, *command[3:]]
            return native_run(command, **kwargs)

        original_describe = describe

        def fixture_describe(source_type):
            entry = original_describe(source_type)
            if source_type == "intercom":
                config = rest_config(self.fixture.origin)
                entry["resource_defaults"] = config["resource_defaults"]
                entry["resources"] = [{"name": row["name"], "selected": row.get("selected", True),
                                      "declared": row, "limitations": []} for row in config["resources"]]
            return entry

        self.env = patch.dict(os.environ, {"ORIEL_BUDGET_TEST_TOKEN": TOKEN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runner = patch.object(service.subprocess, "run", side_effect=run)
        self.run_mock = self.runner.start()
        self.addCleanup(self.runner.stop)
        self.catalog = patch.object(service, "describe", side_effect=fixture_describe)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def configure(self, source_type):
        resources = ["contacts", "admins"] if source_type == "intercom" else ["issues", "users"]
        service.add(self.workspace, source_type, name="sample", resources=resources,
                    credential_env={"token" if source_type == "intercom" else "api_key": "ORIEL_BUDGET_TEST_TOKEN"})

    def sync(self, max_requests, max_seconds=20):
        return service.sync(self.workspace, "sample", request_budget={"max_requests": max_requests, "max_seconds": max_seconds},
                            bootstrap_context=False)

    def status(self):
        return json.loads((self.workspace.root / "connectors/sample/status.json").read_text())

    def total(self, table):
        return self.workspace.query("SELECT SUM(value) AS amount FROM sample." + table)["rows"][0]["amount"]

    def assert_budget(self, count, *, completed):
        status = self.status()
        ledger_path = self.workspace.root / "connectors/sample" / status["attempt"] / "request-budget.json"
        ledger = read_budget(ledger_path)
        self.assertEqual(status["request_budget"], compact_summary(ledger))
        self.assertNotIn("requests", status["request_budget"])
        self.assertEqual(status["request_budget"]["requests_file"], "request-budget.json")
        self.assertEqual(ledger["counts"]["reserved"], count)
        self.assertEqual(ledger_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ledger["status"] == "completed", completed)
        self.assertNotIn(TOKEN, json.dumps(ledger))
        self.assertNotIn(self.fixture.origin, json.dumps(ledger))
        self.assertEqual([row["durable_reserved"] for row in self.fixture.requests], list(range(1, count + 1)))
        return ledger

    def test_rest_budget_shared_across_resources_parent_dependencies_and_child_pages(self):
        self.configure("intercom")
        self.sync(6)
        self.assertEqual(self.total("contacts"), 4)
        self.assertEqual(self.total("admins"), 1)
        self.assertEqual(len(self.fixture.requests), 6)
        ledger = self.assert_budget(6, completed=True)
        self.assertEqual(ledger["counts"]["succeeded"], 6)
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.value = 9
        with self.assertRaises(RuntimeError):
            self.sync(5)
        self.assertEqual(len(self.fixture.requests), 5)
        self.assertEqual(self.total("contacts"), 4)
        self.assertEqual(self.total("admins"), 1)
        ledger = self.assert_budget(5, completed=False)
        self.assertEqual(ledger["stop_reason"], "request_limit")
        self.assertEqual(ledger["counts"]["pending"], 0)

    def test_graphql_budget_shared_across_resources_and_pages(self):
        self.configure("linear")
        self.sync(4)
        self.assertEqual(self.total("issues"), 2)
        self.assertEqual(self.total("users"), 2)
        self.assertEqual(len(self.fixture.requests), 4)
        self.assert_budget(4, completed=True)
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.value = 7
        with self.assertRaises(RuntimeError):
            self.sync(3)
        self.assertEqual(len(self.fixture.requests), 3)
        self.assertEqual(self.total("issues"), 2)
        self.assert_budget(3, completed=False)

    def test_failed_dispatch_counted_and_not_retried_or_published(self):
        self.configure("intercom")
        self.sync(6)
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.failure = True
        with self.assertRaises(RuntimeError):
            self.sync(10)
        ledger = self.assert_budget(len(self.fixture.requests), completed=False)
        self.assertEqual(ledger["counts"]["failed"], 1)
        self.assertEqual(len([row for row in self.fixture.requests if row["path"].endswith("contacts")]), 1)
        self.assertEqual(self.total("contacts"), 4)

    def test_parent_hard_deadline_retains_pending_ledger_for_trickling_body(self):
        self.configure("intercom")
        self.sync(6)
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.trickle = True
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.sync(10, max_seconds=3)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(len(self.fixture.requests), 1)
        ledger = self.assert_budget(1, completed=False)
        self.assertEqual(ledger["counts"]["pending"], 1)
        self.assertEqual(self.total("contacts"), 4)
        status = self.status()
        retained = json.loads((self.workspace.root / "connectors/sample" / status["attempt"] / "receipt.json").read_text())
        self.assertEqual(retained, status)
        self.assertEqual(retained["error_code"], "timeout")

    def test_opt_in_refuses_unsupported_family_before_worker_or_credentials(self):
        csv = self.root / "synthetic.csv"
        csv.write_text("id,value\none,1\n")
        service.add(self.workspace, "csv", name="sample", path=csv)
        with self.assertRaisesRegex(ValueError, "only REST and GraphQL"):
            self.sync(1)
        self.run_mock.assert_not_called()
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(self.status()["error_code"], "preflight_failed")

    def test_graphql_stream_remains_pending_until_entire_body_received(self):
        self.configure("linear")
        self.sync(4)
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.trickle = True
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.sync(10, max_seconds=3)
        ledger = self.assert_budget(1, completed=False)
        self.assertEqual(ledger["counts"]["pending"], 1)
        self.assertEqual(ledger["counts"]["succeeded"], 0)
        self.assertEqual(self.total("issues"), 2)


class BudgetWorkerStatusTests(unittest.TestCase):
    def test_stopped_budget_cannot_be_reported_as_worker_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            request = {"source_type": "intercom", "name": "sample", "credential_env": {"token": "ORIEL_BUDGET_TEST_TOKEN"},
                       "params": {}, "resources": ["contacts"], "staging_database": str(root / "snapshot.duckdb"),
                       "work_dir": str(root), "request_budget": {"max_requests": 10, "max_seconds": 20}}
            request_path, result_path = root / "request.json", root / "result.json"
            request_path.write_text(json.dumps(request))
            request_path.chmod(0o600)

            def stopped(request, credentials, *, budget):
                budget.fail("transport_failed")
                return {"status": "success"}

            with patch.dict(os.environ, {"ORIEL_BUDGET_TEST_TOKEN": TOKEN}), patch.object(worker, "_extract", side_effect=stopped):
                result = worker.run_request(request_path, result_path)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "request_budget_failed")
            self.assertEqual(result["request_budget"]["status"], "stopped")
            self.assertEqual(result["request_budget"]["counts"]["reserved"], 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--budget-worker":
        sys.exit(_fixture_worker(sys.argv[2], sys.argv[3:]))
    unittest.main()
