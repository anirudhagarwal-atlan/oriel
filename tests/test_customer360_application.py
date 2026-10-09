"""Actual loopback HTTP and local jobs; not browser UI or real-model evidence."""
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from contextbase.customer360 import application, identity, webserver
from contextbase.customer360.contracts import PROFILE_SPEC, sha256
from contextbase.workspace import Workspace


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.handlers = [], []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.handlers.extend(name for name in values if name.lower().startswith("on"))
        if tag == "script":
            self.scripts.append(values.get("src"))


class ApplicationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / "workspace")
        self.factory_calls = 0
        def unused_scripted_factory():
            self.factory_calls += 1
            raise AssertionError("This offline HTTP suite must not dispatch a model.")
        self.factory = unused_scripted_factory
        self.app = application.Application(self.workspace, model_client_factory=self.factory)
        self.server = webserver.Server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        status, headers, _ = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200, "The packaged application assets must exist before this suite runs.")
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, value = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.csrf = value["csrf"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def request(self, method, path, *, body=None, headers=(), authenticated=True,
                origin=True, csrf=True, length=True, host=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host:
                connection.putheader("Host", self.server.origin.removeprefix("http://"))
            if authenticated and hasattr(self, "cookie"):
                connection.putheader("Cookie", self.cookie)
            if method == "POST":
                if origin:
                    connection.putheader("Origin", self.server.origin)
                if csrf and hasattr(self, "csrf"):
                    connection.putheader("X-Oriel-CSRF", self.csrf)
                connection.putheader("Content-Type", "application/json")
            if body is not None and length:
                connection.putheader("Content-Length", str(len(body)))
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders(body)
            result = connection.getresponse()
            raw = result.read()
            response_headers = dict(result.getheaders())
            value = json.loads(raw) if response_headers.get("Content-Type", "").startswith("application/json") else raw.decode()
            return result.status, response_headers, value
        finally:
            connection.close()

    def submit(self, operation, arguments, request_id=None):
        value = {"operation": operation, "arguments": arguments}
        if request_id is not None:
            value["request_id"] = request_id
        return self.request("POST", "/api/jobs", body=json.dumps(value).encode())

    def wait(self, job_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status, _, value = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if value["status"] in ("succeeded", "failed"):
                return value
            time.sleep(0.02)
        self.fail("Bounded local job did not finish.")

    def run_job(self, operation, arguments, *, failure=False):
        status, _, submitted = self.submit(operation, arguments)
        self.assertEqual(status, 202)
        job = self.wait(submitted["job_id"])
        self.assertEqual(job["status"], "failed" if failure else "succeeded", job.get("error"))
        return job if failure else job["result"]

    def test_http_integer_precision_preserves_typed_storage_and_approval_hashes(self):
        safe = (1 << 53) - 1
        large = safe + 2  # A value JavaScript Number would round, below BIGINT's limit.
        source = Path(self.temp.name) / "typed-source.csv"
        source.write_text("id,amount,safe_positive,safe_negative,ordinary,active,body\n"
                          f"{large},{-large},{safe},{-safe},7,true,Exact synthetic first record\n"
                          f"{-large},{large},{safe},{-safe},0,false,Exact synthetic second record\n")
        self.run_job("source.add", {"source_type": "csv", "name": "typed", "path": str(source)})
        self.run_job("source.sync", {"name": "typed"})
        snapshot = self.run_job("snapshot", {"connections": ["typed"]})
        status, _, submitted = self.submit("source.inspect", {"snapshot_id": snapshot["snapshot_id"], "sample_rows": 2})
        self.assertEqual(status, 202)
        inspected = self.wait(submitted["job_id"])
        self.assertEqual(inspected["status"], "succeeded", inspected.get("error"))
        status, headers, inspected = self.request("GET", "/api/jobs/" + submitted["job_id"])
        self.assertEqual(headers["X-Oriel-Integer-Encoding"], "unsafe-integers-as-decimal-strings")
        table = inspected["result"]["sources"][0]["tables"][0]
        self.assertEqual(next(column["type"] for column in table["columns"] if column["name"] == "id"), "BIGINT")
        self.assertEqual(table["rows"], 2)
        values = {row["values"]["id"]: row["values"] for row in table["samples"]}
        self.assertEqual(set(values), {str(large), str(-large)})
        self.assertEqual(values[str(large)]["amount"], str(-large))
        self.assertEqual(values[str(-large)]["amount"], str(large))
        for row in values.values():
            self.assertIs(type(row["id"]), str)
            self.assertIs(type(row["amount"]), str)
            self.assertIs(type(row["safe_positive"]), int)
            self.assertEqual(row["safe_positive"], safe)
            self.assertIs(type(row["safe_negative"]), int)
            self.assertEqual(row["safe_negative"], -safe)
            self.assertIs(type(row["ordinary"]), int)
            self.assertIs(type(row["active"]), bool)
        self.assertTrue(values[str(large)]["active"])
        self.assertFalse(values[str(-large)]["active"])
        original_job = self.app.job(submitted["job_id"])
        stored_job = json.loads((self.app.root / ("job-" + submitted["job_id"] + ".json")).read_text())
        self.assertEqual(stored_job, {key: value for key, value in original_job.items() if key != "presentation"})
        self.assertNotIn("presentation", stored_job)
        stored_values = original_job["result"]["sources"][0]["tables"][0]["samples"]
        self.assertEqual({row["values"]["id"] for row in stored_values}, {large, -large})
        self.assertTrue(all(type(row["values"]["id"]) is int for row in stored_values))

        customer = self.run_job("customer.create", {"label": "example-exact-integer-customer"})
        links = self.run_job("links.propose", {"links": [{"connection": "typed", "entity": "record", "source_id": str(value),
                                                           "customer_id": customer["id"], "basis": "Explicit synthetic source IDs"}
                                                          for value in (large, -large)]})
        self.run_job("links.approve", {"proposal_id": links["proposal_id"], "approval_sha256": links["approval_sha256"], "reviewer": "synthetic-http-review"})
        profile = {"spec_version": PROFILE_SPEC, "name": "typed_review", "description": "Typed synthetic HTTP evidence",
                   "bindings": [{"connection": "typed", "entity": "record", "table": "typed.records", "kind": "billing", "record_id": "id",
                                 "customer_ref": {"connection": "typed", "entity": "record", "column": "id"},
                                 "fields": {"native_id": "id", "amount": "amount", "safe_value": "safe_positive", "active": "active", "text": "body"}}]}
        mapping = self.run_job("profile.propose", {"profile": profile, "snapshot_id": snapshot["snapshot_id"]})
        self.run_job("profile.approve", {"proposal_id": mapping["proposal_id"], "approval_sha256": mapping["approval_sha256"], "reviewer": "synthetic-http-review"})
        self.run_job("materialize", {"profile_name": profile["name"], "snapshot_id": snapshot["snapshot_id"]})
        definition = {"name": "exact_negative_records", "description": "Count the exact observed negative amount",
                      "kind": "billing", "operation": "count", "field": None, "unit": "records", "currency_field": None,
                      "filters": [{"field": "amount", "operator": "eq", "value": -large}]}
        context = self.run_job("context.propose", {"profile_name": profile["name"], "snapshot_id": snapshot["snapshot_id"], "definitions": [definition]})
        self.assertEqual(context["definitions"][0]["filters"][0]["value"], str(-large))
        with self.workspace._connect() as connection:
            stored = json.loads(connection.execute("SELECT payload FROM _contextbase.customer360_context_candidates WHERE proposal_id=?", [context["proposal_id"]]).fetchone()[0])
            raw = connection.execute("SELECT id,amount,active FROM typed.records ORDER BY id").fetchall()
        self.assertEqual(raw, [(-large, large, False), (large, -large, True)])
        self.assertIs(type(stored["definitions"][0]["filters"][0]["value"]), int)
        self.assertEqual(stored["definitions"][0]["filters"][0]["value"], -large)
        self.assertEqual(context["approval_sha256"], sha256(stored))
        self.assertEqual(context["definition_sha256"], sha256([definition]))
        state = self.request("GET", "/api/state")[2]
        entry, = state["presentation"]["calculation_scopes"]
        self.assertEqual(entry["path"], ["pending", "context", "proposals", 0, "definitions", 0])
        self.assertEqual(entry["definition_sha256"], sha256(definition))
        self.assertIn("integer " + str(-large), entry["scope"]["text"])
        self.assertNotIn("string ", entry["scope"]["text"])
        self.assertNotIn(definition["description"], entry["scope"]["text"])
        self.assertEqual(entry["scope"]["snapshot_id"], snapshot["snapshot_id"])
        self.run_job("context.approve", {"proposal_id": context["proposal_id"], "approval_sha256": context["approval_sha256"], "reviewer": "synthetic-http-review"})
        scope = {"profile_name": profile["name"], "snapshot_id": snapshot["snapshot_id"], "customer_id": customer["id"]}
        retained = self.run_job("review.save", {**scope, "quote_limit": 20})
        self.assertEqual(retained["brief"]["metrics"][0]["result"]["values"], [{"currency": None, "value": 1}])
        records = {record["source_id"]: record for record in retained["records"]}
        self.assertEqual(records[str(large)]["fields"]["native_id"], str(large))
        self.assertEqual(records[str(large)]["fields"]["amount"], str(-large))
        self.assertIs(type(records[str(large)]["fields"]["safe_value"]), int)
        self.assertIs(type(records[str(large)]["fields"]["active"]), bool)
        for quote in retained["brief"]["quotes"]:
            exact = self.run_job("evidence", {**scope, **{key: quote[key] for key in ("evidence_id", "field", "start", "end")}})
            self.assertEqual(exact["source_id"], quote["source_id"])
            self.assertEqual(exact["quote_id"], quote["quote_id"])
            self.assertEqual(exact["text"], quote["text"])
        self.assertEqual(self.factory_calls, 0)

    def test_http_bootstrap_intent_consent_validation_and_exact_forwarding(self):
        # A routing double only. No source inspection, inference or approval is
        # claimed by this HTTP test; staged agent behavior is tested separately.
        intent = '  Review status "open" without assuming it means renewal risk.  '
        arguments = {"profile_name": "synthetic_profile", "snapshot_id": "synthetic-snapshot",
                     "allow_model_data": True, "max_cost_usd": 0.25, "max_turns": 4, "intent": intent}
        client = object()
        with patch.object(application.agent, "bootstrap", return_value={"transport_test": True}) as bootstrap:
            for invalid in ("", " ", "first\nsecond", "x" * 10001, "\U0001f600" * 3000, 7, False):
                failed = self.run_job("context.bootstrap", {**arguments, "intent": invalid}, failure=True)
                self.assertEqual(failed["error"]["code"], "operation_refused")
            failed = self.run_job("context.bootstrap", {**arguments, "allow_model_data": False}, failure=True)
            self.assertIn("consent", failed["error"]["message"])
            self.assertEqual(self.factory_calls, 0)
            bootstrap.assert_not_called()
            with patch.object(self.app, "model_client_factory", return_value=client) as factory:
                status, _, submitted = self.submit("context.bootstrap", arguments)
                self.assertEqual(status, 202)
                job = self.wait(submitted["job_id"])
                self.assertEqual(job["status"], "succeeded")
                bootstrap.assert_called_once_with(self.workspace, **arguments, client=client)
                factory.assert_called_once_with()
                self.assertEqual(job["request_sha256"], sha256({"operation": "context.bootstrap", "arguments": arguments}))
                self.run_job("context.bootstrap", {key: value for key, value in arguments.items() if key != "intent"})
                self.assertNotIn("intent", bootstrap.call_args.kwargs)
        self.assertFalse(self.workspace.db_path.exists())

    def test_derived_legacy_scope_preserves_typed_values_and_original_artifact(self):
        definition = {"name": "synthetic_identifier", "description": "Unsupported descriptive assertion",
                      "kind": "commercial", "operation": "sum", "field": "amount", "unit": "currency",
                      "currency_field": "currency", "filters": [
                          {"field": "status", "operator": "eq", "value": "open"},
                          {"field": "active", "operator": "eq", "value": True},
                          {"field": "quantity", "operator": "in", "value": [1, 1.0, "1", None, 9007199254740993]}]}
        artifact = {"result": {"snapshot_id": "old-snapshot", "customer_id": "old-customer",
                              "definition": definition, "result_id": "historical-result"}}
        before = application.canonical(artifact)
        presented = application._presentation(artifact)
        self.assertEqual(application.canonical(artifact), before)
        self.assertNotIn("calculation_scope", artifact["result"])
        entry, = presented["presentation"]["calculation_scopes"]
        self.assertEqual(entry["path"], ["result", "definition"])
        self.assertEqual(entry["definition_sha256"], sha256(definition))
        self.assertEqual(entry["scope"]["snapshot_id"], "old-snapshot")
        self.assertEqual(entry["scope"]["customer_id"], "old-customer")
        for literal in ('string "open"', 'boolean true', 'integer 1', 'number 1.0', 'string "1"', 'null', 'integer 9007199254740993'):
            self.assertIn(literal, entry["scope"]["text"])
        self.assertNotIn(definition["description"], entry["scope"]["text"])
        self.assertNotIn(definition["name"], entry["scope"]["text"])
        self.assertEqual(presented["result"], artifact["result"])

    def test_http_session_origin_csrf_and_body_boundaries(self):
        self.assertEqual(self.request("GET", "/api/state", authenticated=False)[0], 401)
        for headers, host in (([], False), ([("Host", "example.invalid")], False), ([("Host", "example.invalid")], True),
                              ([("Origin", "https://example.invalid")], True), ([("Sec-Fetch-Site", "cross-site")], True)):
            self.assertEqual(self.request("GET", "/api/session", headers=headers, host=host)[0], 403)
        operation = b'{"operation":"customer.create","arguments":{"label":"example-customer"}}'
        for options in ({"authenticated": False}, {"origin": False}, {"csrf": False},
                        {"headers": [("X-Oriel-CSRF", "incorrect")]},
                        {"headers": [("Origin", "https://example.invalid")]},
                        {"headers": [("Cookie", self.cookie)]}):
            self.assertEqual(self.request("POST", "/api/jobs", body=operation, **options)[0], 403)
        for body in (b'{"operation":"customer.create","operation":"ask","arguments":{}}',
                     b'{"operation":"ask","arguments":{"max_cost_usd":NaN}}',
                     b'{"operation":"ask","arguments":{"max_cost_usd":Infinity}}',
                     b'{"operation":"ask","arguments":{"x":1,"x":2}}', b'[]', b'\xff'):
            self.assertEqual(self.request("POST", "/api/jobs", body=body)[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", body=b"", length=False)[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", body=b"", length=False,
                                      headers=[("Content-Length", str(webserver.MAX_BODY + 1))])[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", body=operation, headers=[("Content-Length", str(len(operation)))])[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", body=operation, headers=[("Transfer-Encoding", "chunked")])[0], 400)
        for path in ("/../application.py", "/%2e%2e/application.py", "/app.js/../../application.py", "/api/jobs/../state"):
            self.assertIn(self.request("GET", path)[0], (400, 404))
        self.assertEqual(self.request("GET", self.server.origin + "/api/session")[0], 400)
        self.assertEqual(list(self.app.root.glob("job-*.json")), [])
        self.assertEqual(self.factory_calls, 0)

    def test_http_source_plan_is_authenticated_static_and_keeps_parent_fetches_separate(self):
        route = "/api/source-plan?type=discord&resource=messages"
        self.assertEqual(self.request("GET", route, authenticated=False)[0], 401)
        before = sorted(str(path.relative_to(self.workspace.root)) for path in self.workspace.root.rglob("*"))
        with patch.object(application.service, "sync", side_effect=AssertionError("No extraction")), \
                patch.object(application.service, "_load", side_effect=AssertionError("No source configuration")), \
                patch.object(self.workspace, "_connect", side_effect=AssertionError("No workspace read")):
            status, _, plan = self.request("GET", route)
            self.assertEqual(status, 200)
            self.assertEqual(plan["source_type"], "discord")
            self.assertEqual(plan["execution_family"], "dlt_rest")
            self.assertEqual(plan["requested_resources"], ["messages"])
            self.assertEqual(plan["selected_outputs"], ["messages"])
            self.assertEqual(plan["required_fetch_resources"], ["channels", "guilds", "messages"])
            self.assertEqual(plan["dependency_edges"], [
                {"resource": "channels", "requires": "guilds"},
                {"resource": "messages", "requires": "channels"}])
            self.assertFalse(plan["default_selection_used"])
            self.assertEqual(plan["status"], "declaration_derived")
            self.assertTrue(plan["limitations"])
            status, _, repeated = self.request("GET", route + "&resource=channels")
            self.assertEqual(status, 200)
            self.assertEqual(repeated["requested_resources"], ["messages", "channels"])
            self.assertEqual(repeated["selected_outputs"], ["channels", "messages"])
            status, _, defaults = self.request("GET", "/api/source-plan?type=github")
            self.assertEqual(status, 200)
            self.assertTrue(defaults["default_selection_used"])
            self.assertEqual(defaults["requested_resources"], [])
            self.assertNotIn("pull_requests", defaults["selected_outputs"])
            status, _, unknown = self.request("GET", "/api/source-plan?type=postgres")
            self.assertEqual(status, 200)
            self.assertEqual(unknown["status"], "fetch_scope_unknown")
            self.assertIsNone(unknown["required_fetch_resources"])
        after = sorted(str(path.relative_to(self.workspace.root)) for path in self.workspace.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(self.app._jobs, {})
        self.assertFalse(self.workspace.db_path.exists())
        self.assertEqual(self.factory_calls, 0)
        self.assertFalse({"params", "url", "credentials", "auth", "endpoint"} & set(plan))

    def test_http_source_plan_refuses_malformed_unknown_or_unbounded_queries_without_jobs(self):
        queries = ("", "type=", "type=discord&type=github", "resource=messages", "type=not_a_connector",
                   "type=discord&resource=missing_resource", "type=discord&resource=", "type=discord&resource=messages&resource=messages",
                   "type=discord&resource=messages&auth=synthetic", "type=discord&resource", "type=%FF",
                   "type=discord&resource=%00", "type=discord&resource=" + "a" * 129,
                   "type=discord" + "&r=x" * 258, "type=discord&resource=" + "a" * 2048)
        for query in queries:
            with self.subTest(query=query[:80]):
                status, _, result = self.request("GET", "/api/source-plan?" + query)
                self.assertEqual(status, 400)
                self.assertIn(result["error"]["code"], {"request_refused", "invalid_path"})
                self.assertNotIn("synthetic", result["error"]["message"])
        self.assertEqual(self.app._jobs, {})
        self.assertEqual(list(self.app.root.glob("job-*.json")), [])
        self.assertFalse(self.workspace.db_path.exists())
        self.assertEqual(self.factory_calls, 0)

    def test_private_workspace_single_owner_and_job_argument_authority(self):
        self.assertEqual(self.workspace.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.app.root.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, "already owns"):
            application.Application(self.workspace, model_client_factory=self.factory)
        occupied_port = Workspace(Path(self.temp.name) / "occupied-port")
        with self.assertRaises(OSError):
            webserver.serve(occupied_port, port=self.server.server_port)
        recovered = application.Application(occupied_port, model_client_factory=self.factory)
        recovered.close()  # A failed bind must not retain application ownership.
        shared = Workspace(Path(self.temp.name) / "shared")
        shared.root.mkdir(mode=0o755)
        os.chmod(shared.root, 0o755)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            application.Application(shared, model_client_factory=self.factory)
        self.assertEqual(shared.root.stat().st_mode & 0o777, 0o755)
        database_shared = Workspace(Path(self.temp.name) / "shared-database")
        database_shared.root.mkdir(mode=0o700)
        identity.create_customer(database_shared, "example-private-directory")
        os.chmod(database_shared.db_path, 0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            application.Application(database_shared, model_client_factory=self.factory)
        self.assertEqual(database_shared.db_path.stat().st_mode & 0o777, 0o644)
        for key, value in (("workspace", "../other-workspace"), ("client", "replacement"),
                           ("model_client_factory", "replacement"), ("sql", "SELECT 1"), ("command", "example")):
            status, _, _ = self.submit("ask", {key: value})
            self.assertEqual(status, 400)
        self.assertEqual(self.submit("execute", {})[0], 400)
        consent = self.run_job("ask", {"allow_model_data": False}, failure=True)
        self.assertEqual(consent["error"]["code"], "operation_refused")
        self.assertIn("consent", consent["error"]["message"])
        request_id = str(uuid.uuid4())
        label = '<script>window.syntheticExecution=true</script><img src=x onerror="syntheticExecution=true">'
        status, _, submitted = self.submit("customer.create", {"label": label}, request_id)
        self.assertEqual(status, 202)
        result = self.wait(submitted["job_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["label"], label)
        self.assertEqual(self.submit("customer.create", {"label": label}, request_id)[2]["job_id"], request_id)
        self.assertEqual(self.submit("customer.create", {"label": "different"}, request_id)[0], 400)
        self.assertEqual(len(identity.list_customers(self.workspace)), 1)
        self.assertEqual(self.workspace.db_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.app.root.glob("job-*.json")))
        status, headers, _ = self.request("GET", "/api/jobs/" + request_id)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, html = self.request("GET", "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        parser = AssetParser()
        parser.feed(html)
        self.assertEqual(parser.scripts, ["/app.js"])
        self.assertEqual(parser.handlers, [])
        status, _, javascript = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("textContent", javascript)
        self.assertIsNone(re.search(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b|\bdocument\.write\s*\(|\beval\s*\(|\bnew\s+Function\s*\(", javascript))
        self.assertNotIn(label, html)
        self.assertEqual(self.factory_calls, 0)

    def test_real_sample_workflow_is_serialized_explicit_and_reviewed(self):
        request_id = str(uuid.uuid4())
        status, _, first = self.submit("sample.connect", {"profile": "billing-support"}, request_id)
        self.assertEqual(status, 202)
        duplicate = self.submit("sample.connect", {"profile": "billing-support"}, request_id)
        self.assertEqual(duplicate[0], 202)
        self.assertEqual(duplicate[2]["job_id"], request_id)
        self.assertEqual(self.submit("customer.create", {"label": "must-wait"})[0], 409)
        state = self.request("GET", "/api/state")[2]
        self.assertTrue(state["busy"])
        self.assertEqual(state["active_job_id"], request_id)
        extracted = self.wait(first["job_id"])
        self.assertEqual(extracted["status"], "succeeded", extracted.get("error"))
        self.assertTrue(extracted["result"]["actual_dlt_executed"])
        self.assertFalse(extracted["result"]["vendor_adapter_verified"])
        plan = self.run_job("sample.prepare", {"profile": "billing-support"})
        self.assertFalse(plan["approvals_performed"])
        with self.workspace._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM _contextbase.customer360_links").fetchone(), (0,))
        for proposed in plan["links"]:
            arguments = {"proposal_id": proposed["proposal_id"], "approval_sha256": proposed["approval_sha256"], "reviewer": "synthetic-http-review"}
            if proposed["status"] == "conflict":
                self.run_job("links.approve", arguments, failure=True)
        proposed = next(value for value in plan["links"] if value["status"] == "proposed")
        self.run_job("links.approve", {"proposal_id": proposed["proposal_id"], "approval_sha256": "0" * 64, "reviewer": "synthetic-http-review"}, failure=True)
        self.run_job("links.approve", {"proposal_id": proposed["proposal_id"], "approval_sha256": proposed["approval_sha256"], "reviewer": "synthetic-http-review"})
        mapping = self.run_job("profile.propose", {"profile": plan["mapping"], "snapshot_id": plan["snapshot_id"]})
        self.run_job("profile.approve", {"proposal_id": mapping["proposal_id"], "approval_sha256": mapping["approval_sha256"], "reviewer": "synthetic-http-review"})
        self.run_job("materialize", {"profile_name": plan["profile"], "snapshot_id": plan["snapshot_id"]})
        definition = self.run_job("context.propose", {"profile_name": plan["profile"], "snapshot_id": plan["snapshot_id"],
                                                       "definitions": plan["definitions"], "gaps": plan["gaps"]})
        context = self.run_job("context.approve", {"proposal_id": definition["proposal_id"], "approval_sha256": definition["approval_sha256"], "reviewer": "synthetic-http-review"})
        scope = {"profile_name": plan["profile"], "snapshot_id": plan["snapshot_id"], "customer_id": plan["customer_ids"]["customer_a"]}
        before = self.run_job("brief", {**scope, "fields": ["text", "updates"], "quote_limit": 20})
        amounts = next(value["result"]["values"] for value in before["metrics"] if value["result"]["definition_name"] == "outstanding_invoices")
        # Independent arithmetic from authored source arrays, never product SQL.
        source = Path(__file__).resolve().parents[1] / "examples/customer-360/profiles/billing-support/initial/stripe.invoices.json"
        expected = {}
        for row in json.loads(source.read_text()):
            if row["customer"] == "cus_synthetic_a" and row["status"] == "open" and row["amount_remaining"] > 0:
                expected[row["currency"].upper()] = expected.get(row["currency"].upper(), 0) + row["amount_remaining"]
        self.assertEqual({value["currency"]: Decimal(value["value"]) for value in amounts}, expected)
        quote = before["quotes"][0]
        exact = self.run_job("evidence", {**scope, **{key: quote[key] for key in ("evidence_id", "field", "start", "end")}})
        self.assertEqual(exact, quote)
        saved = self.run_job("review.save", {**scope, "fields": ["text", "updates"], "quote_limit": 20})
        self.run_job("sample.connect", {"profile": "billing-support", "phase": "refreshed"})
        refreshed = self.run_job("sample.prepare", {"profile": "billing-support"})
        self.assertEqual(refreshed["customer_ids"], plan["customer_ids"])
        for proposal in refreshed["links"]:
            if proposal["status"] == "proposed":
                self.run_job("links.approve", {"proposal_id": proposal["proposal_id"], "approval_sha256": proposal["approval_sha256"], "reviewer": "synthetic-http-review"})
        self.run_job("materialize", {"profile_name": plan["profile"], "snapshot_id": refreshed["snapshot_id"]})
        compared = self.run_job("review.compare", {"review_id": saved["review_id"], "current_snapshot_id": refreshed["snapshot_id"]})
        self.assertEqual(compared["definition_sha256"], context["definition_sha256"])
        self.assertNotEqual(compared["previous_snapshot_id"], compared["snapshot_id"])
        self.assertTrue(compared["metric_changes"])
        self.assertEqual(self.run_job("review.load", {"review_id": saved["review_id"]}), saved)
        self.assertEqual(self.factory_calls, 0)

    def test_http_latest_passage_roundtrip_retains_exact_time_and_requires_paired_locator(self):
        extracted = self.run_job("sample.connect", {"profile": "billing-support", "phase": "refreshed"})
        self.assertTrue(extracted["actual_dlt_executed"])
        self.assertFalse(extracted["vendor_adapter_verified"])
        plan = self.run_job("sample.prepare", {"profile": "billing-support"})
        for proposal in plan["links"]:
            if proposal["status"] == "proposed":
                self.run_job("links.approve", {"proposal_id": proposal["proposal_id"],
                    "approval_sha256": proposal["approval_sha256"], "reviewer": "synthetic-http-review"})
        mapping = self.run_job("profile.propose", {"profile": plan["mapping"], "snapshot_id": plan["snapshot_id"]})
        self.run_job("profile.approve", {"proposal_id": mapping["proposal_id"],
            "approval_sha256": mapping["approval_sha256"], "reviewer": "synthetic-http-review"})
        profile_scope = {"profile_name": plan["profile"], "snapshot_id": plan["snapshot_id"]}
        self.run_job("materialize", profile_scope)
        candidate = self.run_job("context.propose", {**profile_scope, "definitions": plan["definitions"], "gaps": plan["gaps"]})
        self.run_job("context.approve", {"proposal_id": candidate["proposal_id"],
            "approval_sha256": candidate["approval_sha256"], "reviewer": "synthetic-http-review"})
        scope = {**profile_scope, "customer_id": plan["customer_ids"]["customer_a"]}
        selected = self.run_job("evidence.latest", {**scope, "kind": "support", "limit": 5})
        self.assertEqual(selected["status"], "latest_unique")
        self.assertTrue(selected["coverage"]["complete"])
        self.assertEqual(selected["coverage"]["upstream_completeness"], "not_verified")
        self.assertEqual(len(selected["quotes"]), 1)
        quote = selected["quotes"][0]
        self.assertEqual(quote["passage_name"], "support_update")
        self.assertEqual(quote["json_pointer"], "/conversation_parts/0/body")
        self.assertEqual(quote["time"]["status"], "known")
        self.assertEqual(quote["time"]["encoding"], "unix_seconds")
        self.assertEqual(quote["time"]["meaning"], "passage_created")
        self.assertEqual(selected["latest_time"], {key: quote["time"][key] for key in ("unix_seconds", "utc")})
        self.assertEqual(quote["time_lineage"]["json_pointer"], "/conversation_parts/0/created_at")
        self.assertEqual(quote["time_lineage"]["passage_json_pointer"], quote["json_pointer"])
        fixture = Path(__file__).resolve().parents[1] / "examples/customer-360/profiles/billing-support/refreshed/intercom.conversations.json"
        row = next(row for row in json.loads(fixture.read_text()) if row["id"] == quote["source_id"])
        part = row["conversation_parts"]["conversation_parts"][0]
        self.assertEqual(quote["text"], part["body"])
        self.assertEqual(quote["time"]["unix_seconds"], str(part["created_at"]))
        self.assertEqual(quote["time"]["utc"], datetime.fromtimestamp(part["created_at"], timezone.utc).isoformat().replace("+00:00", "Z"))
        locator = {key: quote[key] for key in ("evidence_id", "field", "start", "end", "passage_name", "json_pointer")}
        opened = self.run_job("evidence", {**scope, **locator})
        self.assertEqual(opened, quote)
        self.assertEqual(opened["quote_id"], sha256({key: value for key, value in quote.items() if key != "quote_id"}))
        for missing in ("passage_name", "json_pointer"):
            incomplete = {key: value for key, value in locator.items() if key != missing}
            failed = self.run_job("evidence", {**scope, **incomplete}, failure=True)
            self.assertEqual(failed["error"]["code"], "operation_refused")
            self.assertNotIn("result", failed)
        self.assertEqual(self.run_job("evidence", {**scope, **locator}), quote)
        self.assertEqual(self.factory_calls, 0)

    def test_interrupted_model_jobs_are_retained_without_dispatch_on_restart(self):
        # Authored interruption receipts exercise recovery without ever paying
        # for or pretending to have executed a model request.
        self.app.close()
        request = {"operation": "ask", "arguments": {"allow_model_data": True}}
        jobs = []
        for status in ("queued", "running"):
            job_id = str(uuid.uuid4())
            receipt = {"job_id": job_id, "operation": "ask", "request_sha256": sha256(request), "status": status,
                       "created_at": datetime.now(timezone.utc).isoformat(), "model_origin": "scripted_agent_test"}
            application._write(self.app.root / ("job-" + job_id + ".json"), receipt)
            jobs.append(job_id)
        restarted = application.Application(self.workspace, model_client_factory=self.factory)
        try:
            for job_id in jobs:
                result = restarted.job(job_id)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], "interrupted")
                self.assertNotIn("result", result)
                self.assertEqual(restarted.submit("ask", request["arguments"], job_id), {"job_id": job_id, "status": "failed"})
                self.assertEqual(json.loads((restarted.root / ("job-" + job_id + ".json")).read_text())["status"], "failed")
            self.assertEqual(restarted.state()["model_origin"], "scripted_agent_test")
            self.assertFalse(restarted.state()["busy"])
            self.assertEqual(self.factory_calls, 0)
        finally:
            restarted.close()
        replacement = webserver.Server(self.app)
        replacement_thread = threading.Thread(target=replacement.serve_forever, daemon=True)
        replacement_thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", replacement.server_port, timeout=5)
        try:
            connection.request("GET", "/api/session", headers={"Cookie": self.cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()
            replacement.shutdown()
            replacement.server_close()
            replacement_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
