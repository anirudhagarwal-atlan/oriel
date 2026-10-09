"""Offline worker boundary tests: controlled source/dlt doubles, real local DuckDB.

These do not certify dlt or any connector against a live service.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import duckdb

from contextbase.connectors import worker


class ConnectorWorkerTests(unittest.TestCase):
    def test_single_resource_factory_is_wrapped_without_consuming_rows(self):
        class Resource:
            name = "messages"
            selected = True

            def __iter__(self):
                raise AssertionError("Source construction must not consume data")

        class Source:
            def __init__(self, values):
                self.resources = {resource.name: resource for resource in values}

            def with_resources(self, *names):
                for name, resource in self.resources.items():
                    resource.selected = name in names
                return self

        def decorator(factory, *, name):
            self.assertEqual(name, "kafka")
            return lambda: Source(list(factory()))

        runtime = types.SimpleNamespace(sources=types.SimpleNamespace(DltResource=Resource), source=decorator)
        source, selected = worker._as_source(runtime, Resource(), "kafka", ["messages"])
        self.assertEqual(selected, ["messages"])
        self.assertIsInstance(source, Source)
        with self.assertRaisesRegex(worker.WorkerError, "resource_selection_mismatch"):
            worker._as_source(runtime, Resource(), "kafka", ["unknown"])
        with self.assertRaisesRegex(worker.WorkerError, "invalid_source_factory"):
            worker._as_source(runtime, [{"synthetic": 1}], "kafka", [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.request_path = self.root / "request.json"
        self.result_path = self.root / "result.json"
        self.request = {"source_type": "example", "name": "example_source",
                        "credential_env": {"api_key": "TEST_CONNECTOR_KEY"}, "params": {},
                        "resources": ["events"], "staging_database": str(self.root / "staging.duckdb"),
                        "work_dir": str(self.root)}
        self.write_request()

    def write_request(self):
        self.request_path.write_text(json.dumps(self.request))
        self.request_path.chmod(0o600)

    def validated(self):
        return worker._validate_request(self.request_path, self.result_path)

    def fake_runtime(self, *, failed=False, selected=("events",), empty=False):
        source = types.SimpleNamespace(resources={name: types.SimpleNamespace(selected=True)
                                                  for name in selected})
        calls = []

        def get_source(source_type, credentials, *, resource_names, params):
            calls.append((source_type, dict(credentials), None if resource_names is None else list(resource_names), dict(params)))
            return source

        def pipeline(**kwargs):
            self.assertFalse(kwargs["enable_runtime_trace"])
            self.assertFalse(kwargs["restore_from_destination"])
            self.assertEqual(kwargs["dataset_name"], "example_source")

            def run(given, **options):
                self.assertIs(given, source)
                self.assertEqual(options, {"loader_file_format": "insert_values"})
                conn = kwargs["destination"]
                conn.execute("CREATE SCHEMA example_source")
                conn.execute("CREATE TABLE example_source._dlt_loads (id VARCHAR)")
                if not empty:
                    conn.execute("CREATE TABLE example_source.events (id BIGINT, payload VARCHAR)")
                    conn.execute("INSERT INTO example_source.events VALUES (1,'private source row')")
                return types.SimpleNamespace(has_failed_jobs=failed)

            return types.SimpleNamespace(run=run)

        fake = types.SimpleNamespace(pipeline=pipeline,
                                     destinations=types.SimpleNamespace(duckdb=lambda conn: conn))
        return (fake, duckdb, get_source), calls

    def execute(self, runtime=None):
        if runtime is None:
            runtime, _ = self.fake_runtime()
        with patch.dict(os.environ, {"TEST_CONNECTOR_KEY": "synthetic-secret"}), \
                patch.object(worker, "_runtime", return_value=runtime):
            return worker.run_request(self.request_path, self.result_path)

    def test_success_real_scratch_duckdb_and_private_sanitized_result(self):
        runtime, calls = self.fake_runtime()
        result = self.execute(runtime)
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls, [("example", {"api_key": "synthetic-secret"}, ["events"], {})])
        self.assertEqual(result["tables"], [{"schema": "example_source", "table": "events", "row_count": 1,
                                           "columns": [{"name": "id", "type": "BIGINT"},
                                                       {"name": "payload", "type": "VARCHAR"}]}])
        self.assertIn("observed_finished_at", result)
        text = self.result_path.read_text()
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("private source row", text)
        self.assertEqual(stat.S_IMODE(self.result_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(text), result)
        self.assertFalse((self.root / "pipelines").exists())
        self.assertFalse((self.root / "dlt-data").exists())
        self.assertFalse((self.root / ".dlt").exists())
        self.assertFalse((self.root / "temporary").exists())

    def test_all_credential_references_resolved_before_source_dispatch(self):
        self.request["credential_env"]["password"] = "TEST_CONNECTOR_MISSING"
        self.write_request()
        runtime, calls = self.fake_runtime()
        with patch.dict(os.environ, {}, clear=True):
            result = self.execute(runtime)
        self.assertEqual(result["error_code"], "credential_unavailable")
        self.assertEqual(calls, [])

    def test_runtime_receives_no_ambient_credentials_and_home_unchanged(self):
        home = os.environ.get("HOME")
        previous_cwd = Path.cwd()
        seen = {}

        def extract(request, credentials):
            seen.update(os.environ)
            self.assertEqual(credentials, {"api_key": "synthetic-secret"})
            self.assertEqual(Path.cwd(), self.root)
            self.assertEqual(os.environ.get("HOME"), home)
            self.assertEqual(os.environ["DLT_DATA_DIR"], str(self.root / "dlt-data"))
            self.assertEqual(os.environ["DLT_DISABLE_PLUGINS"], "true")
            self.assertEqual(os.environ["RUNTIME__DLTHUB_TELEMETRY"], "false")
            for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "NETRC"):
                self.assertEqual(os.environ[name], os.devnull)
            self.assertEqual(os.environ["AWS_EC2_METADATA_DISABLED"], "true")
            for name in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(os.environ[name], str(self.root / "temporary"))
            self.assertEqual(tempfile.gettempdir(), str(self.root / "temporary"))
            self.assertEqual((self.root / "temporary").stat().st_mode & 0o777, 0o700)
            raise RuntimeError("synthetic-secret provider detail")

        with patch.dict(os.environ, {"TEST_CONNECTOR_KEY": "synthetic-secret", "AWS_ACCESS_KEY_ID": "ambient-test",
                                     "SOURCES__TOKEN": "ambient-test", "DLT_CONFIG_FOLDER": "/unapproved"}), \
                patch.object(worker, "_extract", side_effect=extract):
            result = worker.run_request(self.request_path, self.result_path)
            self.assertEqual(os.environ["AWS_ACCESS_KEY_ID"], "ambient-test")
        self.assertNotIn("TEST_CONNECTOR_KEY", seen)
        self.assertNotIn("AWS_ACCESS_KEY_ID", seen)
        self.assertNotIn("SOURCES__TOKEN", seen)
        self.assertNotIn("DLT_CONFIG_FOLDER", seen)
        self.assertEqual(Path.cwd(), previous_cwd)
        self.assertEqual(result["error_code"], "extraction_failed")
        self.assertNotIn("synthetic-secret", self.result_path.read_text())
        self.assertFalse((self.root / "temporary").exists())

    def test_native_and_python_output_are_suppressed(self):
        code = """import os,sys,logging
from unittest.mock import patch
from contextbase.connectors import worker
def fail(*args):
 print('synthetic-secret-stdout')
 print('synthetic-secret-stderr',file=sys.stderr)
 os.write(1,b'synthetic-secret-native-out')
 os.write(2,b'synthetic-secret-native-err')
 logging.critical('synthetic-secret-log')
 raise RuntimeError('synthetic-secret-exception')
with patch.object(worker,'_extract',side_effect=fail):
 worker.run_request(sys.argv[1],sys.argv[2])
"""
        env = dict(os.environ, TEST_CONNECTOR_KEY="synthetic-secret")
        output = subprocess.run([sys.executable, "-c", code, str(self.request_path), str(self.result_path)],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=15)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, b"")
        self.assertEqual(output.stderr, b"")
        self.assertEqual(json.loads(self.result_path.read_text())["error_code"], "extraction_failed")

    def test_missing_dependency_is_safe_error(self):
        with patch.dict(os.environ, {"TEST_CONNECTOR_KEY": "synthetic-secret"}), \
                patch.object(worker, "_runtime", side_effect=ImportError("synthetic-secret path")):
            result = worker.run_request(self.request_path, self.result_path)
        self.assertEqual(result["error_code"], "runtime_dependency_missing")
        self.assertNotIn("synthetic-secret", self.result_path.read_text())

    def test_missing_source_factory_dependency_is_explicit(self):
        runtime, _ = self.fake_runtime()
        def source(*args, **kwargs):
            raise ImportError("sensitive internal details")
        result = self.execute((*runtime[:2], source))
        self.assertEqual(result["error_code"], "connector_dependency_missing")

    def test_failed_load_is_never_success(self):
        runtime, _ = self.fake_runtime(failed=True)
        self.assertEqual(self.execute(runtime)["error_code"], "extraction_incomplete")

    def test_silent_resource_selection_loss_is_refused(self):
        runtime, _ = self.fake_runtime(selected=("different",))
        self.assertEqual(self.execute(runtime)["error_code"], "resource_selection_mismatch")
        self.assertFalse(Path(self.request["staging_database"]).exists())

    def test_empty_selection_uses_all_selected_source_resources(self):
        self.request["resources"] = []
        self.write_request()
        runtime, calls = self.fake_runtime(selected=("events", "accounts"))
        result = self.execute(runtime)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["resources"], ["events", "accounts"])
        self.assertIsNone(calls[0][2])

    def test_empty_source_selection_is_refused(self):
        self.request["resources"] = []
        self.write_request()
        runtime, _ = self.fake_runtime(selected=())
        self.assertEqual(self.execute(runtime)["error_code"], "resource_selection_mismatch")

    def test_empty_output_schema_is_explicit(self):
        runtime, _ = self.fake_runtime(empty=True)
        self.assertEqual(self.execute(runtime)["error_code"], "extraction_empty_schema")

    def test_existing_outputs_are_not_overwritten(self):
        for path in (self.result_path, Path(self.request["staging_database"]),
                     Path(self.request["staging_database"] + ".wal")):
            with self.subTest(path=path.name):
                path.write_text("sentinel")
                try:
                    with self.assertRaisesRegex(worker.WorkerError, "output_exists"):
                        self.validated()
                    self.assertEqual(path.read_text(), "sentinel")
                finally:
                    path.unlink()

    def test_request_permissions_and_symlinks_refused(self):
        self.request_path.chmod(0o644)
        with self.assertRaises(worker.WorkerError):
            self.validated()
        self.request_path.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(self.request_path)
        with self.assertRaises(worker.WorkerError):
            worker._validate_request(link, self.result_path)

    def test_private_root_and_no_outside_paths(self):
        self.root.chmod(0o755)
        with self.assertRaises(worker.WorkerError):
            self.validated()
        self.root.chmod(0o700)
        self.request["staging_database"] = str(self.root.parent / "outside.duckdb")
        self.write_request()
        with self.assertRaises(worker.WorkerError):
            self.validated()

    def test_resource_identifiers_duplicates_and_unknown_fields_refused(self):
        cases = ({"resources": ["events", "events"]},
                 {"resources": ["../events"]}, {"name": "unsafe;sql"}, {"extra": True},
                 {"credential_env": {"api_key": "HOME"}})
        original = dict(self.request)
        for values in cases:
            with self.subTest(values=values):
                self.request = {**original, **values}
                self.write_request()
                with self.assertRaises(worker.WorkerError):
                    self.validated()

    def test_duplicate_json_keys_and_nonfinite_values_refused(self):
        for text in ('{"source_type":"a","source_type":"b"}',
                     json.dumps({**self.request, "params": {"x": float("nan")}})):
            self.request_path.write_text(text)
            with self.assertRaises(worker.WorkerError):
                self.validated()

    def test_cli_failure_has_no_traceback(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            status = worker.main(["--request", str(self.root / "missing"), "--result", str(self.result_path)])
        self.assertEqual(status, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_runtime_rejects_unreviewed_dlt_version_before_provider_imports(self):
        imported = []
        def import_module(name):
            imported.append(name)
            return types.SimpleNamespace(__version__="99.0")
        with patch.object(worker.importlib, "import_module", side_effect=import_module):
            with self.assertRaisesRegex(worker.WorkerError, "runtime_version_unsupported"):
                worker._runtime(self.root)
        self.assertEqual(imported, ["dlt"])

    def test_runtime_uses_only_memory_provider_and_private_paths(self):
        class MemoryProvider:
            def __init__(self): self.settings = {}
            def set_value(self, key, value, pipeline): self.settings[key] = value
        class RunContext:
            def __init__(self, root): self.root = root
            def initial_providers(self): raise AssertionError("ambient provider constructed")
        class Pluggable:
            def __init__(self, context):
                self.context = context
                self.providers = types.SimpleNamespace(providers=context.initial_providers())
        class Container(dict):
            def __setitem__(self, key, value):
                provider, = value.providers.providers
                value.context.runtime_config = types.SimpleNamespace(**provider.settings["runtime"])
                super().__setitem__(key, value)
        container = Container()
        modules = {
            "dlt": types.SimpleNamespace(__version__="1.24.0"),
            "dlt.common.configuration.container": types.SimpleNamespace(Container=lambda: container),
            "dlt.common.configuration.specs.pluggable_run_context": types.SimpleNamespace(PluggableRunContext=Pluggable),
            "dlt.common.runtime.run_context": types.SimpleNamespace(RunContext=RunContext),
            "dlt.common.configuration.providers.dictionary": types.SimpleNamespace(DictionaryProvider=MemoryProvider),
            "duckdb": duckdb,
            "contextbase.connectors.upstream.sources": types.SimpleNamespace(get_source=lambda **kwargs: kwargs),
            "contextbase.connectors.upstream.source_fetch": types.SimpleNamespace(configure_verified_sources=lambda path: None),
        }
        with patch.object(worker.importlib, "import_module", side_effect=modules.__getitem__):
            _, _, source_factory = worker._runtime(self.root)
        context = container[Pluggable]
        self.assertEqual(context.context.global_dir, str(self.root / "dlt-data"))
        self.assertEqual(context.context.data_dir, str(self.root / "dlt-data"))
        self.assertIsNone(context.context.module)
        provider, = context.providers.providers
        self.assertIs(source_factory()["resource_config"], provider)
        self.assertEqual(provider.settings["providers"], {"enable_airflow_secrets": False, "enable_google_secrets": False})
        self.assertFalse(context.context.runtime_config.dlthub_telemetry)
        self.assertEqual(provider.settings["normalize"]["workers"], 1)


if __name__ == "__main__":
    unittest.main()
