"""Real local snapshot lifecycle and controlled remote-worker failure checks."""
import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from contextbase.connectors import available, describe
from contextbase.connectors import service
from contextbase.workspace import Workspace


class ConnectorServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root / "workspace")
        self.csv = self.root / "accounts.csv"
        self.csv.write_text("account_id,seats\naccount_001,3\naccount_002,7\n")

    def configure_file(self):
        return service.add(self.workspace, "csv", name="crm", path=self.csv)

    def total(self):
        return self.workspace.query("SELECT SUM(seats) AS seats FROM crm.records")["rows"]

    def test_missing_source_preflight_records_failed_refresh_without_replacing_data(self):
        from contextbase.customer360.snapshots import capture_snapshot, assert_snapshot_current
        self.configure_file()
        before = service.sync(self.workspace, "crm", bootstrap_context=False)
        snapshot = capture_snapshot(self.workspace, ["crm"])
        self.csv.unlink()
        with self.assertRaises(FileNotFoundError):
            service.sync(self.workspace, "crm", bootstrap_context=False)
        source = service.configured(self.workspace)["sources"][0]
        self.assertEqual(source["last_sync"]["status"], "failed")
        self.assertEqual(source["last_sync"]["error_code"], "preflight_failed")
        self.assertNotEqual(source["last_sync"]["attempt"], before["attempt"])
        self.assertEqual(source["last_snapshot"]["attempt"], before["attempt"])
        self.assertEqual(self.total(), [{"seats": 10}])
        with self.assertRaisesRegex(ValueError, "failed latest sync"):
            assert_snapshot_current(self.workspace, snapshot["snapshot_id"])
        self.csv.write_text("account_id,seats\naccount_001,5\n")
        service.sync(self.workspace, "crm", bootstrap_context=False)
        self.assertEqual(capture_snapshot(self.workspace, ["crm"])["coverage"]["total_rows"], 1)

    def test_catalog_requires_no_optional_runtime(self):
        self.assertEqual(available()["count"], 101)
        self.assertEqual(describe("sqlite")["parameter_names"], ["chunk_size", "reflection_level", "schema"])
        self.assertEqual(describe("linear")["parameter_names"], [])

    def test_csv_connect_sync_bootstrap_query_and_refresh(self):
        added = self.configure_file()
        self.assertFalse(added["connected"])
        receipt = service.sync(self.workspace, "crm")
        self.assertEqual(receipt["status"], "synced")
        self.assertEqual(receipt["context"]["status"], "draft")
        self.assertTrue(Path(receipt["context"]["path"]).is_file())
        self.assertEqual(self.total(), [{"seats": 10}])
        self.csv.write_text("account_id,seats\naccount_001,4\n")
        updated = service.sync(self.workspace, "crm")
        self.assertNotEqual(updated["snapshot_sha256"], receipt["snapshot_sha256"])
        self.assertEqual(self.total(), [{"seats": 4}])
        with self.workspace._connect() as conn:
            stored = json.loads(conn.execute("SELECT payload FROM _contextbase.connector_receipts WHERE name='crm'").fetchone()[0])
        self.assertEqual(stored["snapshot_sha256"], updated["snapshot_sha256"])
        self.assertEqual(service.configured(self.workspace)["sources"][0]["last_sync"]["status"], "synced")

    def test_configuration_stores_references_without_resolving_environment(self):
        with patch("os.environ", {}):
            result = service.add(self.workspace, "linear", credential_env={"api_key": "SYNTHETIC_LINEAR_KEY"})
        self.assertEqual(result["credential_env"], {"api_key": "SYNTHETIC_LINEAR_KEY"})
        config = self.workspace.root / "connectors/linear/source.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.workspace.root.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            service.add(self.workspace, "linear")

    def test_input_and_reloaded_configuration_cannot_override_auth_or_endpoints(self):
        for params in ({"token": "synthetic-secret"}, {"endpoint": "https://example.invalid"},
                       {"resources": [{"query": "mutation { synthetic }"}]}):
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, "noncredential"):
                service.add(self.workspace, "linear", params=params)
        for name in ("Uppercase", "../escape"):
            with self.assertRaisesRegex(ValueError, "lowercase"):
                service.add(self.workspace, "linear", name=name)
        for name in ("LANG", "SSL_CERT_FILE", "HOME"):
            with self.assertRaises(ValueError):
                service.add(self.workspace, "linear", credential_env={"api_key": name})
        with self.assertRaisesRegex(ValueError, "unique resource"):
            service.add(self.workspace, "linear", resources=["issues", "issues"])
        service.add(self.workspace, "linear")
        path = self.workspace.root / "connectors/linear/source.json"
        value = json.loads(path.read_text())
        value["params"] = {"token": "synthetic-secret"}
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "noncredential"), patch.object(service.subprocess, "run") as run:
            service.sync(self.workspace, "linear")
        run.assert_not_called()

    def test_preexisting_schema_is_not_overwritten(self):
        self.configure_file()
        self.workspace.ingest(self.csv, "crm", "unmanaged")
        with self.assertRaisesRegex(ValueError, "other data"):
            service.sync(self.workspace, "crm")
        self.assertEqual([item["table"] for item in self.workspace.catalog()], ["unmanaged"])

    def test_explicit_reconfiguration_preserves_data_and_discloses_unsynced_config(self):
        original = self.configure_file()
        service.sync(self.workspace, "crm")
        self.assertFalse(service.configured(self.workspace)["sources"][0]["configuration_changed_since_sync"])
        replacement = self.root / "replacement.csv"
        replacement.write_text("account_id,seats\naccount_001,12\n")
        service.add(self.workspace, "csv", name="crm", path=replacement, replace_config=True)
        listed = service.configured(self.workspace)["sources"][0]
        self.assertTrue(listed["configuration_changed_since_sync"])
        self.assertEqual(listed["last_sync"]["status"], "synced")
        self.assertEqual(self.total(), [{"seats": 10}])
        backups = list((self.workspace.root / "connectors/crm").glob("config-before-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["path"], original["path"])
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        service.sync(self.workspace, "crm")
        self.assertEqual(self.total(), [{"seats": 12}])
        self.assertFalse(service.configured(self.workspace)["sources"][0]["configuration_changed_since_sync"])
        with self.assertRaisesRegex(ValueError, "same name and source type"):
            service.add(self.workspace, "linear", name="crm", replace_config=True)

    def test_source_update_cannot_race_a_running_sync(self):
        self.configure_file()
        directory = self.workspace.root / "connectors/crm"
        with service._lock(directory):
            with self.assertRaisesRegex(ValueError, "sync is running"):
                service.add(self.workspace, "csv", name="crm", path=self.csv, replace_config=True)

    def test_failed_reconfigured_refresh_exposes_previous_snapshot_and_config(self):
        self.configure_file()
        original = service.sync(self.workspace, "crm")
        replacement = self.root / "replacement.csv"
        replacement.write_text("account_id,seats\naccount_001,12\n")
        service.add(self.workspace, "csv", name="crm", path=replacement, replace_config=True)
        with patch.object(Workspace, "ingest", side_effect=ValueError("synthetic extraction failure")):
            with self.assertRaises(ValueError):
                service.sync(self.workspace, "crm")
        listed = service.configured(self.workspace)["sources"][0]
        self.assertEqual(listed["last_sync"]["status"], "failed")
        self.assertEqual(listed["last_snapshot"]["snapshot_sha256"], original["snapshot_sha256"])
        self.assertTrue(listed["configuration_changed_since_sync"])
        self.assertEqual(self.total(), [{"seats": 10}])

    def test_sync_loads_configuration_only_after_acquiring_lock(self):
        self.configure_file()
        replacement = self.root / "replacement.csv"
        replacement.write_text("account_id,seats\naccount_001,12\n")
        original_lock = service._lock

        @contextmanager
        def reconfigure_before_lock(directory):
            with patch.object(service, "_lock", original_lock):
                service.add(self.workspace, "csv", name="crm", path=replacement, replace_config=True)
            with original_lock(directory):
                yield

        with patch.object(service, "_lock", reconfigure_before_lock):
            service.sync(self.workspace, "crm")
        self.assertEqual(self.total(), [{"seats": 12}])

    def test_missing_tables_refuses_whole_publication(self):
        config = self.configure_file()
        service.sync(self.workspace, "crm")
        stage = self.root / "partial.duckdb"
        with duckdb.connect(str(stage)) as conn:
            conn.execute("CREATE SCHEMA crm")
            conn.execute("CREATE TABLE crm.other AS SELECT 999 AS seats")
        with self.assertRaisesRegex(ValueError, "missing previously"):
            service._publish(self.workspace, config, stage)
        self.assertEqual(self.total(), [{"seats": 10}])

    def test_long_observed_table_names_are_quoted_and_catalogued(self):
        config = service.add(self.workspace, "sqlite", name="warehouse", credential_env={"credentials": "SYNTHETIC_SQLITE_URL"})
        table = "_nested_" + "x" * 80
        stage = self.root / "long-name.duckdb"
        with duckdb.connect(str(stage)) as conn:
            conn.execute('CREATE SCHEMA warehouse')
            conn.execute(f'CREATE TABLE warehouse."{table}" AS SELECT 42 AS value')
        service._publish(self.workspace, config, stage)
        self.assertEqual(self.workspace.catalog()[0]["table"], table)

    def test_worker_failure_is_sanitized_and_keeps_old_snapshot(self):
        service.add(self.workspace, "sqlite", name="warehouse", credential_env={"credentials": "SYNTHETIC_SQLITE_URL"})
        stage = self.root / "initial.duckdb"
        with duckdb.connect(str(stage)) as conn:
            conn.execute("CREATE SCHEMA warehouse")
            conn.execute("CREATE TABLE warehouse.accounts AS SELECT 10 AS seats")
        config, _, _ = service._load(self.workspace, "warehouse")
        service._publish(self.workspace, config, stage)

        def fail(command, **kwargs):
            request = json.loads(Path(command[command.index("--request") + 1]).read_text())
            self.assertEqual(request["resources"], [])
            Path(command[-1]).write_text(json.dumps({"status": "failed", "error_code": "credential_unavailable",
                                                    "message": "synthetic-sensitive-message"}))
            return subprocess.CompletedProcess(command, 1)
        with patch.object(service.subprocess, "run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "credential_unavailable") as error:
                service.sync(self.workspace, "warehouse")
        self.assertNotIn("synthetic-sensitive-message", str(error.exception))
        self.assertEqual(self.workspace.query("SELECT * FROM warehouse.accounts")["rows"], [{"seats": 10}])

    def test_timeout_retains_workspace_and_unlocks(self):
        service.add(self.workspace, "linear")
        with patch.object(service.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 1)):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                service.sync(self.workspace, "linear", timeout=1)
        directory = self.workspace.root / "connectors/linear"
        self.assertEqual(json.loads((directory / "status.json").read_text())["error_code"], "timeout")
        self.assertFalse((directory / "sync.lock").exists())
        self.assertFalse(self.workspace.db_path.exists())

    def test_parent_cleans_worker_scratch_after_timeout_or_abnormal_exit(self):
        outside = self.root / "outside.txt"
        outside.write_text("retain this unrelated file")
        for timeout in (True, False):
            name = "timed_out" if timeout else "abnormal_exit"
            service.add(self.workspace, "linear", name=name)

            def interrupted(command, **kwargs):
                attempt = Path(command[-1]).parent
                for directory in ("pipelines", "dlt-data", ".dlt", "temporary"):
                    scratch = attempt / directory
                    scratch.mkdir()
                    (scratch / "partial.txt").write_text("synthetic private diagnostic")
                (attempt / "temporary/linked.txt").symlink_to(outside)
                (attempt / "snapshot.duckdb").write_bytes(b"retained staging artifact")
                if timeout:
                    raise subprocess.TimeoutExpired("worker", 1)
                return subprocess.CompletedProcess(command, 9)

            with patch.object(service.subprocess, "run", side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    service.sync(self.workspace, name, timeout=1)
            attempt = next((self.workspace.root / "connectors" / name).glob("attempt-*"))
            self.assertFalse(any((attempt / directory).exists() for directory in ("pipelines", "dlt-data", ".dlt", "temporary")))
            self.assertTrue((attempt / "snapshot.duckdb").exists())
            self.assertTrue((attempt / "request.json").exists())
            self.assertEqual(outside.read_text(), "retain this unrelated file")

    def test_postpublication_receipt_error_does_not_claim_rollback(self):
        self.configure_file()
        original = service._write
        def fail_receipt(path, value, **kwargs):
            if path.name == "receipt.json":
                raise OSError("synthetic disk error")
            return original(path, value, **kwargs)
        with patch.object(service, "_write", side_effect=fail_receipt):
            receipt = service.sync(self.workspace, "crm", bootstrap_context=False)
        self.assertEqual(receipt["status"], "synced")
        self.assertIn("database_receipt_committed", receipt["bookkeeping"])
        self.assertEqual(self.total(), [{"seats": 10}])
        restored = service.configured(self.workspace)["sources"][0]["last_sync"]
        self.assertEqual(restored["status"], "synced")
        self.assertEqual(restored["bookkeeping"], "recovered_from_database_receipt")

    def test_mixpanel_export_requires_date_parameters_before_worker(self):
        service.add(self.workspace, "mixpanel", resources=["export"])
        with patch.object(service.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                service.sync(self.workspace, "mixpanel")
        run.assert_not_called()

    def test_rest_parameters_are_discoverable_and_validated_before_worker(self):
        entry = describe("mixpanel")
        self.assertEqual(entry["resource_parameters"]["export"]["required"], ["from_date", "to_date"])
        params = {"export": {"from_date": "2026-01-01", "to_date": "2026-01-02"}}
        service.add(self.workspace, "mixpanel", resources=["export"], params=params)
        config, loaded, _ = service._load(self.workspace, "mixpanel")
        service._check_resources(loaded, config["resources"], config["params"])
        for invalid in ({"export": {"url": "https://example.invalid"}},
                        {"export": {"api_key": "synthetic-test-value"}},
                        {"export": {"unknown": 1}}, {"export": [1]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.add(self.workspace, "mixpanel", name="invalid", params=invalid)
        with self.assertRaisesRegex(ValueError, "selected resources"):
            service._check_resources(entry, ["cohorts"], params)

    def test_sql_parameters_have_types_and_filesystem_selection_is_explicit(self):
        with self.assertRaises(ValueError):
            service.add(self.workspace, "sqlite", params={"chunk_size": -1})
        config = service.add(self.workspace, "sqlite", params={"chunk_size": 100, "reflection_level": "minimal"})
        self.assertEqual(config["params"]["chunk_size"], 100)
        with self.assertRaises(ValueError):
            service.add(self.workspace, "s3", params={})
        params = {"tables": {"orders": {"file_glob": "orders/*.csv", "format": "csv"}}}
        config = service.add(self.workspace, "s3", params=params, resources=["orders"])
        self.assertNotIn("aws_session_token", config["credential_env"])
        service._load(self.workspace, "s3")
        service._check_resources(describe("s3"), ["orders"], params)
        with self.assertRaises(ValueError):
            service._check_resources(describe("s3"), ["unknown"], params)

    def test_cli_source_to_query(self):
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(self.workspace.root), *args],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        cli("source", "add", "csv", "--name", "crm", "--path", str(self.csv))
        self.assertEqual(cli("source", "sync", "crm")["context"]["status"], "draft")
        self.assertEqual(cli("query", "SELECT SUM(seats) AS seats FROM crm.records")["rows"], [{"seats": 10}])


if __name__ == "__main__":
    unittest.main()
