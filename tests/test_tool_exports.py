"""Functional generic-workflow proof on CRM/product schemas outside the simulator."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import duckdb

from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "tool-exports"
FIXTURES = EXAMPLE / "fixtures"
spec = importlib.util.spec_from_file_location("tool_exports_oracle", EXAMPLE / "oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class ToolExportTests(unittest.TestCase):
    def test_independent_oracle_handles_boundaries_deduplication_and_exclusions(self):
        initial = oracle.expected(*oracle.read_fixture())
        self.assertEqual(initial["unused_seats_at_low_activation"], 51)
        self.assertEqual([row["organization_id"] for row in initial["accounts"]],
                         ["org_001", "org_003", "org_006", "org_007", "org_008"])
        self.assertEqual(initial["accounts"][0]["activated_members"], 4)
        self.assertEqual(initial["accounts"][1]["activated_members"], 0)
        self.assertEqual(oracle.expected(*oracle.read_fixture("product-events-refreshed.jsonl")),
                         {"accounts": [{"organization_id": "org_007", "licensed_seats": 8,
                                        "activated_members": 0, "unused_seats": 8}],
                          "unused_seats_at_low_activation": 8})

    def test_mapping_ambiguity_missing_keys_and_unknown_targets_are_distinct(self):
        rows = oracle.read_fixture(mapping="workspace-map-unresolved.csv")
        self.assertEqual(oracle.mapping_issues(*rows), [
            {"issue": "ambiguous_workspace", "source_key": "ws_alpha"},
            {"issue": "missing_workspace", "source_key": "ws_theta"},
            {"issue": "unmapped_customer", "source_key": "org_008"},
        ])
        with self.assertRaisesRegex(ValueError, "Resolve identity mapping"):
            oracle.expected(*rows)
        accounts, links, activity = copy.deepcopy(oracle.read_fixture())
        links[0]["customer_key"] = "org_missing"
        self.assertIn({"issue": "unknown_customer", "source_key": "org_missing"},
                      oracle.mapping_issues(accounts, links, activity))
        accounts.append(dict(accounts[0]))
        self.assertIn({"issue": "duplicate_customer", "source_key": "org_001"},
                      oracle.mapping_issues(accounts, links, activity))

    def test_metric_sql_refuses_unresolved_identity_instead_of_dropping_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            w = Workspace(Path(temporary) / "workspace")
            for filename, schema, table in (("crm-seats.csv", "sales", "seats"),
                                            ("workspace-map-unresolved.csv", "directory", "workspace_map"),
                                            ("product-events-initial.jsonl", "telemetry", "events")):
                w.ingest(FIXTURES / filename, schema, table)
            self.assertEqual(w.query((EXAMPLE / "sql/mapping-issues.sql").read_text())["rows"],
                             oracle.mapping_issues(*oracle.read_fixture(mapping="workspace-map-unresolved.csv")))
            with self.assertRaisesRegex(duckdb.InvalidInputException, "Resolve identity mapping"):
                w.query((EXAMPLE / "sql/unused-seats.sql").read_text())

    def test_cli_review_export_separate_consumer_refresh_and_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "proof"
            process = subprocess.run([sys.executable, str(EXAMPLE / "verify.py"), "--output", str(output)],
                                     capture_output=True, text=True, cwd=ROOT, check=True)
            self.assertEqual(json.loads(process.stdout)["verification"], "passed")
            receipt = json.loads((output / "receipt.json").read_text())
            self.assertEqual(receipt["model_calls"], 0)
            self.assertEqual(receipt["rystic_commands"], 0)
            self.assertTrue(receipt["context"]["approved_context_unchanged_after_refresh"])
            self.assertTrue(receipt["source_files_unchanged"])
            self.assertEqual(receipt["context"]["exported_registry_status"], "candidate")
            self.assertEqual(receipt["second_consumer_initial"]["rows"], [{"unused_seats_at_low_activation": 51}])
            self.assertEqual(receipt["second_consumer_refreshed"]["rows"], [{"unused_seats_at_low_activation": 8}])
            for item in receipt["commands"]:
                self.assertEqual(item["exit_code"] != 0, item["expected_failure"])
            for path in FIXTURES.iterdir():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), receipt["fixture_sha256"][path.name])
            context = output / "bundle/context.json"
            context.write_text(context.read_text() + "\n")
            untrusted_consumer = output / "tampered-consumer"
            rejected = subprocess.run([
                sys.executable, str(EXAMPLE / "reuse.py"), "--workspace", str(untrusted_consumer),
                "--bundle", str(output / "bundle"), "--crm", str(FIXTURES / "crm-seats.csv"),
                "--mapping", str(FIXTURES / "workspace-map-reviewed.csv"),
                "--events", str(FIXTURES / "product-events-initial.jsonl"),
            ], capture_output=True, text=True, cwd=ROOT)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Bundle content does not match", rejected.stderr)
            self.assertFalse(untrusted_consumer.exists())


if __name__ == "__main__":
    unittest.main()
