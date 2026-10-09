"""Actual dlt/HTTP/DuckDB fetch-scope tests against synthetic loopback sources.

These exercise wire-shape and fetched page/row controls only. They do not prove
vendor permissions, field/history authorization, read-only vendor APIs or DNS
controls. The fixture child permits no non-loopback source access.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from contextbase import connectors
from contextbase.connectors import service, worker
from contextbase.connectors.budget import read_budget
from contextbase.connectors.fetch_scope import compact_summary, compile_scope, plan_hash, read_scope
from contextbase.connectors.upstream import yaml_source
from contextbase.workspace import Workspace


def _fixture_module():
    spec = importlib.util.spec_from_file_location("scope_budget_fixture", Path(__file__).with_name("test_connector_request_budget.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _fixture_module()


def declaration(origin):
    config = fixtures.rest_config(origin)
    config["resource_defaults"]["endpoint"]["params"] = {"state": "open"}
    return config


def description(source_type, origin, original):
    entry = original(source_type)
    if source_type == "intercom":
        config = declaration(origin)
        entry["resource_defaults"] = config["resource_defaults"]
        entry["resources"] = [{"name": resource["name"], "selected": resource.get("selected", True),
                               "declared": resource, "limitations": []} for resource in config["resources"]]
    return entry


def _fixture_worker(origin, mode, argv):
    spec = importlib.util.spec_from_file_location("scope_network_fixture", fixtures.ROOT / "examples/connectors/api_fixture_worker.py")
    network = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(network)
    network.install_network_guard(origin)
    original_describe = connectors.describe
    connectors.describe = lambda source_type: description(source_type, origin, original_describe)
    original_load = yaml_source.load_yaml_config
    yaml_source.load_yaml_config = lambda name: declaration(origin) if name == "intercom" else original_load(name)
    original_paginator = yaml_source._runtime_paginator

    def paginator(*args, **kwargs):
        value = original_paginator(*args, **kwargs)
        update = value.update_request

        def altered(request):
            update(request)
            if value.has_next_page:
                if mode == "changed_filter":
                    request.params["state"] = "closed"
                elif mode == "changed_origin":
                    # Mutation after the existing paginator's own origin check
                    # requires the final prepared-request guard to stop it.
                    target = urlsplit(origin)
                    changed = "http://127.0.0.1:" + str(1 if target.port != 1 else 2)
                    request.url = request.url.replace(origin, changed, 1)
        value.update_request = altered
        return value

    yaml_source._runtime_paginator = paginator
    original_runtime = worker._runtime

    def runtime(root):
        dlt, duckdb, get_source = original_runtime(root)
        from contextbase.connectors.upstream import sources
        if mode == "changed_declaration":
            changed = declaration(origin)
            changed["resource_defaults"]["endpoint"]["params"]["state"] = "closed"
            # The worker has already compiled the original declaration. This
            # simulates a changed adapter input before real source construction.
            yaml_source.load_yaml_config = lambda name: copy.deepcopy(changed) if name == "intercom" else original_load(name)
        sources.load_yaml_config = yaml_source.load_yaml_config

        def source(*args, **kwargs):
            result = get_source(*args, **kwargs)
            if mode == "missing_child":
                # A deliberately defective downstream processing step hides a
                # parent AFTER the guard observed its raw page. The real dlt
                # load must not certify the omitted child fetch as complete.
                result.resources["companies"].add_filter(lambda item: item["id"] != "company_b")
            return result
        return dlt, duckdb, source

    worker._runtime = runtime
    return worker.main(argv)


class ScopedRESTExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.fixture = fixtures.Fixture().__enter__()
        self.addCleanup(self.fixture.__exit__)
        self.directory = self.workspace.root / "connectors/sample"
        self.fixture.connection_dir = self.directory
        self.mode = "normal"
        original_describe = connectors.describe
        describe = lambda source_type: description(source_type, self.fixture.origin, original_describe)
        for target, attribute, value in ((connectors, "describe", describe), (service, "describe", describe)):
            owned = patch.object(target, attribute, side_effect=value)
            owned.start()
            self.addCleanup(owned.stop)
        original_load = yaml_source.load_yaml_config
        load = patch.object(yaml_source, "load_yaml_config", side_effect=lambda name:
                            declaration(self.fixture.origin) if name == "intercom" else original_load(name))
        load.start()
        self.addCleanup(load.stop)
        environment = patch.dict(os.environ, {"ORIEL_FETCH_SCOPE_TEST_TOKEN": fixtures.TOKEN})
        environment.start()
        self.addCleanup(environment.stop)
        original_run = subprocess.run

        def run(command, **kwargs):
            if command[1:3] == ["-m", "contextbase.connectors.worker"]:
                command = [command[0], str(Path(__file__).resolve()), "--scope-worker", self.fixture.origin,
                           self.mode, *command[3:]]
            return original_run(command, **kwargs)
        runner = patch.object(service.subprocess, "run", side_effect=run)
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        service.add(self.workspace, "intercom", name="sample", resources=["contacts"],
                    credential_env={"token": "ORIEL_FETCH_SCOPE_TEST_TOKEN"})

    def scope(self, *, pages=10, rows=20):
        return {"origin": self.fixture.origin, "fetch_resources": ["companies", "contacts"],
                "max_pages": pages, "max_rows": rows}

    def sync(self, *, pages=10, rows=20):
        return service.sync(self.workspace, "sample", request_budget={"max_requests": 20, "max_seconds": 25},
                            fetch_scope=self.scope(pages=pages, rows=rows), bootstrap_context=False)

    def state(self):
        status = json.loads((self.directory / "status.json").read_text())
        attempt = self.directory / status["attempt"]
        return status, read_scope(attempt / "fetch-scope.json"), read_budget(attempt / "request-budget.json")

    def total(self):
        return self.workspace.query("SELECT SUM(value) AS total FROM sample.contacts")["rows"][0]["total"]

    def clear(self):
        with self.fixture.lock:
            self.fixture.requests.clear()
            self.fixture.value = 9

    def assert_evidence(self, *, completed, requests):
        status, scope, budget = self.state()
        self.assertEqual(status["fetch_scope"], compact_summary(scope))
        self.assertEqual(scope["status"] == "completed", completed)
        self.assertEqual(scope["counts"]["requests"], requests)
        self.assertEqual(budget["counts"]["reserved"], requests)
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertEqual([item["durable_reserved"] for item in self.fixture.requests], list(range(1, requests + 1)))
        self.assertNotIn(fixtures.TOKEN, json.dumps(scope))
        self.assertNotIn("company_a", json.dumps(scope))
        self.assertNotIn("company_b", json.dumps(scope))
        self.assertEqual((self.directory / status["attempt"] / "fetch-scope.json").stat().st_mode & 0o777, 0o600)
        return scope

    def test_actual_parent_and_child_pages_counted_while_only_child_is_published(self):
        result = self.sync()
        scope = self.assert_evidence(completed=True, requests=5)
        self.assertEqual(scope["counts"], {"requests": 5, "pages": 5, "rows": 6, "pending": 0})
        self.assertEqual(scope["coverage"]["companies"]["rows"], 2)
        self.assertEqual(scope["coverage"]["contacts"]["rows"], 4)
        self.assertEqual(scope["coverage"]["contacts"]["expected_chains"], 2)
        self.assertEqual(scope["coverage"]["contacts"]["completed_chains"], 2)
        self.assertEqual(scope["plan"]["selected_outputs"], ["contacts"])
        self.assertEqual(self.total(), 4)
        tables = self.workspace.query("SELECT table_name FROM information_schema.tables WHERE table_schema='sample'")["rows"]
        self.assertEqual(tables, [{"table_name": "contacts"}])
        self.assertEqual(result["fetch_scope"], compact_summary(scope))

    def test_changed_origin_and_static_filter_are_refused_before_an_extra_dispatch(self):
        self.sync()
        for mode in ("changed_filter", "changed_origin"):
            with self.subTest(mode=mode):
                self.clear()
                self.mode = mode
                with patch.object(service, "_publish", wraps=service._publish) as publish:
                    with self.assertRaises(RuntimeError):
                        self.sync()
                    publish.assert_not_called()
                scope = self.assert_evidence(completed=False, requests=2)
                self.assertEqual(scope["stop_reason"], "fetch_scope_request_changed")
                self.assertEqual(scope["counts"]["pending"], 0)
                self.assertTrue(all("state=open" in item["query"] for item in self.fixture.requests))
                self.assertEqual(self.total(), 4)

    def test_page_and_observed_row_overruns_refuse_publication_and_preserve_previous_data(self):
        self.sync()
        for limits, expected_requests, expected_rows, reason in (
                ({"pages": 4}, 4, 5, "fetch_scope_page_limit"),
                ({"rows": 3}, 3, 4, "fetch_scope_row_limit")):
            with self.subTest(reason=reason):
                self.clear()
                with patch.object(service, "_publish", wraps=service._publish) as publish:
                    with self.assertRaises(RuntimeError):
                        self.sync(**limits)
                    publish.assert_not_called()
                scope = self.assert_evidence(completed=False, requests=expected_requests)
                self.assertEqual(scope["stop_reason"], reason)
                self.assertEqual(scope["counts"]["rows"], expected_rows)
                self.assertEqual(self.total(), 4)

    def test_completed_load_with_missing_child_chain_cannot_claim_complete_scope(self):
        self.sync()
        self.clear()
        self.mode = "missing_child"
        with patch.object(service, "_publish", wraps=service._publish) as publish:
            with self.assertRaises(RuntimeError):
                self.sync()
            publish.assert_not_called()
        scope = self.assert_evidence(completed=False, requests=3)
        self.assertEqual(scope["stop_reason"], "fetch_scope_incomplete")
        self.assertEqual(scope["coverage"]["contacts"]["expected_chains"], 2)
        self.assertEqual(scope["coverage"]["contacts"]["completed_chains"], 1)
        self.assertEqual(self.total(), 4)

    def test_changed_declaration_after_compile_is_refused_before_first_dispatch(self):
        self.sync()
        self.clear()
        self.mode = "changed_declaration"
        with patch.object(service, "_publish", wraps=service._publish) as publish:
            with self.assertRaises(RuntimeError):
                self.sync()
            publish.assert_not_called()
        scope = self.assert_evidence(completed=False, requests=0)
        self.assertEqual(scope["stop_reason"], "fetch_scope_declaration_changed")
        self.assertEqual(scope["counts"]["pages"], 0)
        self.assertEqual(self.total(), 4)

    def test_worker_recompiles_and_refuses_plan_mismatch_or_unsupported_scope_before_credentials(self):
        original = {"source_type": "intercom", "name": "sample", "credential_env": {"token": "ORIEL_FETCH_SCOPE_TEST_TOKEN"},
                    "resources": ["contacts"], "params": {}, "request_budget": {"max_requests": 20, "max_seconds": 25},
                    "fetch_scope": self.scope()}
        original["fetch_scope_sha256"] = plan_hash(compile_scope("intercom", ["contacts"], {}, original["fetch_scope"]))
        bad_hash = {**original, "fetch_scope_sha256": "0" * 64}
        unsupported = {**original, "source_type": "linear", "resources": ["issues"],
                       "fetch_scope": {**self.scope(), "fetch_resources": ["issues"]}}
        missing_parent = {**original, "fetch_scope": {**self.scope(), "fetch_resources": ["contacts"]}}
        unsupported_control = {**original, "fetch_scope": {**self.scope(), "fields": ["id"]}}
        for number, request in enumerate((bad_hash, unsupported, missing_parent, unsupported_control)):
            with self.subTest(case=number):
                directory = self.root / ("worker-refusal-" + str(number))
                directory.mkdir(mode=0o700)
                request = {**request, "work_dir": str(directory), "staging_database": str(directory / "snapshot.duckdb")}
                path, result = directory / "request.json", directory / "result.json"
                path.write_text(json.dumps(request))
                path.chmod(0o600)
                environment = Mock()
                environment.get.side_effect = AssertionError("Credentials must not be resolved")
                with patch.object(worker.os, "environ", environment), \
                        patch.object(worker, "_runtime", side_effect=AssertionError("Runtime must not load")) as runtime:
                    with self.assertRaises(worker.WorkerError) as refused:
                        worker.run_request(path, result)
                self.assertEqual(refused.exception.code, "fetch_scope_failed")
                environment.get.assert_not_called()
                runtime.assert_not_called()
                self.assertFalse(result.exists())
                self.assertFalse((directory / "snapshot.duckdb").exists())
        self.assertEqual(self.fixture.requests, [])
        self.runner.assert_not_called()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scope-worker":
        sys.exit(_fixture_worker(sys.argv[2], sys.argv[3], sys.argv[4:]))
    unittest.main()
