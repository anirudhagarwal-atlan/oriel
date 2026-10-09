"""Actual dlt SQLite fixture transport, separate from Customer360 acceptance."""
from importlib import util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from contextbase.connectors import service
from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1] / "examples" / "customer-360"
spec = util.spec_from_file_location("customer360_fixture_runtime", ROOT / "runtime.py")
runtime = util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class Customer360RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # Missing native runtime must fail, never skip.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.workspaces, cls.reports = {}, {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(cls.temp.name) / profile)
            cls.workspaces[profile] = workspace
            cls.reports[profile] = {
                "initial": runtime.load_profile(workspace, profile),
                "refreshed": runtime.load_profile(workspace, profile, "refreshed"),
            }
        cls.manifest = json.loads((ROOT / "manifest.json").read_text())

    def test_both_profiles_execute_actual_pinned_sqlite_connector(self):
        for profile, phases in self.reports.items():
            for phase, report in phases.items():
                with self.subTest(profile=profile, phase=phase):
                    self.assertEqual(report["status"], "passed")
                    self.assertTrue(report["actual_dlt_executed"])
                    self.assertEqual(report["runtime_versions"]["dlt"], "1.24.0")
                    self.assertEqual(report["chunk_size"], 2)
                    self.assertFalse(report["vendor_adapter_verified"])
                    self.assertFalse(report["reviews_performed"])
                    self.assertEqual(report["model_calls"], 0)
                    self.assertEqual(len(report["publications"]), 2)
                    for receipt in report["publications"]:
                        self.assertEqual((receipt["source_type"], receipt["status"]), ("sqlite", "synced"))
                        attempt = self.workspaces[profile].root / "connectors" / receipt["name"] / receipt["attempt"]
                        result = json.loads((attempt / "result.json").read_text())
                        self.assertEqual(result["status"], "success")

    def test_every_extracted_record_and_nested_value_matches_source_arrays(self):
        for profile, workspace in self.workspaces.items():
            definition = self.manifest["profiles"][profile]["phases"]["refreshed"]
            with workspace._connect() as connection:
                for resource in self.reports[profile]["refreshed"]["resources"]:
                    expected = json.loads((ROOT / definition[resource["resource"]]).read_text())
                    rows = connection.execute("SELECT record_json FROM " + resource["table"] + " ORDER BY fixture_position").fetchall()
                    self.assertEqual([json.loads(row[0]) for row in rows], expected)
                    self.assertEqual(len(rows), resource["records"])
                    self.assertTrue(resource["reconciled"])

    def test_native_scalars_and_normalized_columns_are_available_to_profiles(self):
        salesforce = self.workspaces["salesforce-gong"]
        invoices = self.workspaces["billing-support"]
        rows = salesforce.query("SELECT id, account_id, amount, currency_iso_code, is_closed FROM fixture_salesforce.opportunity ORDER BY fixture_position")["rows"]
        originals = json.loads((ROOT / "profiles/salesforce-gong/refreshed/salesforce.opportunity.json").read_text())
        self.assertEqual(rows, [{"id": row["Id"], "account_id": row["AccountId"], "amount": row["Amount"],
                                 "currency_iso_code": row["CurrencyIsoCode"], "is_closed": row["IsClosed"]} for row in originals])
        rows = invoices.query("SELECT id, customer, status, amount_remaining FROM fixture_stripe.invoices ORDER BY fixture_position")["rows"]
        originals = json.loads((ROOT / "profiles/billing-support/refreshed/stripe.invoices.json").read_text())
        self.assertEqual(rows, [{key: row[key] for key in ("id", "customer", "status", "amount_remaining")} for row in originals])
        calls = salesforce.query("SELECT call_id, transcript FROM fixture_gong.call_transcripts ORDER BY fixture_position")["rows"]
        expected = json.loads((ROOT / "profiles/salesforce-gong/refreshed/gong.call_transcripts.json").read_text())
        self.assertEqual([(row["call_id"], json.loads(row["transcript"])) for row in calls],
                         [(row["callId"], row["transcript"]) for row in expected])
        self.assertTrue(all(isinstance(row["call_id"], str) and int(row["call_id"]) > 2**53 for row in calls))

    def test_explicit_resources_exclude_other_sqlite_tables(self):
        for profile, workspace in self.workspaces.items():
            report = self.reports[profile]["refreshed"]
            expected = {resource["table"] for resource in report["resources"]}
            self.assertEqual({table["schema"] + "." + table["table"] for table in workspace.catalog()}, expected)
            for source, connection in report["source_label_mapping"].items():
                config = json.loads((workspace.root / "connectors" / connection / "source.json").read_text())
                self.assertEqual(config["params"], {"chunk_size": 2})
                self.assertEqual(config["resources"], sorted(table.split(".")[1] for table in expected if table.startswith(connection + ".")))
                self.assertEqual(set(config["credential_env"]), {"credentials"})
                self.assertNotIn("sqlite:///", json.dumps(config))
                with sqlite3.connect(workspace.root / "customer360-fixture-runtime" / (source + ".sqlite")) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='excluded_fixture_sentinel'").fetchone(), (1,))
                    self.assertEqual(db.execute("SELECT count(*) FROM excluded_fixture_sentinel").fetchone(), (1,))

    def test_refresh_is_new_extraction_with_separate_retained_receipts(self):
        for phases in self.reports.values():
            before, after = phases["initial"], phases["refreshed"]
            self.assertEqual(before["source_names"], after["source_names"])
            self.assertNotEqual(before["receipt_path"], after["receipt_path"])
            for report in (before, after):
                path = Path(report["receipt_path"])
                self.assertEqual(json.loads(path.read_text()), report)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for first, second in zip(before["publications"], after["publications"]):
                self.assertEqual(first["configuration_sha256"], second["configuration_sha256"])
                self.assertNotEqual(first["attempt"], second["attempt"])
                self.assertNotEqual(first["snapshot_sha256"], second["snapshot_sha256"])

    def test_loader_does_not_approve_customer_identity_or_context(self):
        for workspace in self.workspaces.values():
            with workspace._connect() as connection:
                tables = {row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='_contextbase'").fetchall()}
            self.assertNotIn("customer360_link_reviews", tables)
            self.assertNotIn("customer360_profile_approvals", tables)
            self.assertFalse((workspace.root / "context" / "approved.json").exists())

    def test_unrelated_or_changed_connections_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory) / "workspace")
            csv = Path(directory) / "source.csv"
            csv.write_text("id\nsynthetic-1\n")
            service.add(workspace, "csv", name="existing", path=csv)
            before = (workspace.root / "connectors/existing/source.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "dedicated workspace"):
                runtime.load_profile(workspace, "billing-support")
            self.assertEqual((workspace.root / "connectors/existing/source.json").read_bytes(), before)
            self.assertFalse(workspace.db_path.exists())
        with self.assertRaisesRegex(ValueError, "changed"):
            runtime.load_profile(self.workspaces["billing-support"], "salesforce-gong")

    def test_invalid_phase_and_fixture_traversal_fail_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory) / "workspace")
            with self.assertRaises(ValueError):
                runtime.load_profile(workspace, "billing-support", "unknown")
            self.assertFalse(workspace.root.exists())
            fixture = Path(directory) / "fixture"
            fixture.mkdir()
            manifest = json.loads((ROOT / "manifest.json").read_text())
            manifest["profiles"]["billing-support"]["phases"]["initial"]["intercom.companies"] = "../outside.json"
            (fixture / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                runtime.load_profile(workspace, "billing-support", fixture_root=fixture)
            self.assertFalse(workspace.root.exists())

    def test_reconciliation_detects_changed_published_values(self):
        original = self.workspaces["billing-support"]
        report = self.reports["billing-support"]["refreshed"]
        with tempfile.TemporaryDirectory() as directory:
            copy = Workspace(directory)
            shutil.copyfile(original.db_path, copy.db_path)
            with copy._connect(writable=True) as connection:
                connection.execute("UPDATE fixture_stripe.invoices SET amount_remaining=amount_remaining+1")
            records, _ = runtime._fixtures(ROOT, "billing-support", "refreshed")
            resources = {key: value for key, value in records["refreshed"].items() if key.startswith("stripe.")}
            schemas = {resource["resource"]: resource["columns"] for resource in report["resources"]}
            receipt = next(item for item in report["publications"] if item["name"] == "fixture_stripe")
            with self.assertRaisesRegex(RuntimeError, "differ"):
                runtime._reconcile(copy, "fixture_stripe", resources, schemas, receipt)


if __name__ == "__main__":
    unittest.main()
