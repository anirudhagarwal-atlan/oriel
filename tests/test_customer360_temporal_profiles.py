"""Reviewed temporal mappings over real local CSV publications, no providers.

Attachments enrich an existing source record only after a unique typed join and
an independently reviewed customer association. They never add counted rows.
"""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.customer360.contracts import PROFILE_SPEC, connections, observed_contract, sha256, validate_profile
from contextbase.customer360.identity import create_customer, propose_links, approve_links
from contextbase.customer360.profiles import (propose_profile, approve_profile, materialize,
                                              records_for_customer, readable_profile)
from contextbase.customer360.snapshots import capture_snapshot
from contextbase.workspace import Workspace


class TemporalProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.customer = create_customer(self.workspace, "example-customer")
        self.other = create_customer(self.workspace, "another-example-customer")
        self.rows = [
            {"record id": "r1", "customer ref": "org-a", 'call "key"': "call-a", "body": "First exact passage."},
            {"record id": "r2", "customer ref": "org-a", 'call "key"': "call-a", "body": "Second exact passage."},
        ]
        self.metadata = [
            {"metadata id": "m1", 'join "key"': "call-a", "organization": "party-a", "occurred at": "2026-01-01T10:00:00Z", "note": "Mapped metadata."},
            {"metadata id": "m2", 'join "key"': "call-b", "organization": "party-b", "occurred at": "2026-01-02T10:00:00Z", "note": "Other customer content."},
        ]
        for connection, rows in (("messages", self.rows), ("metadata", self.metadata)):
            path = self.root / (connection + ".csv")
            self.write(path, rows)
            service.add(self.workspace, "csv", name=connection, path=path)
            service.sync(self.workspace, connection, bootstrap_context=False)
        links = [
            {"connection": "messages", "entity": "organization", "source_id": "org-a", "customer_id": self.customer["id"], "basis": "Reviewed synthetic relationship"},
            {"connection": "metadata", "entity": "organization", "source_id": "party-a", "customer_id": self.customer["id"], "basis": "Independently reviewed synthetic metadata relationship"},
            {"connection": "metadata", "entity": "organization", "source_id": "party-b", "customer_id": self.other["id"], "basis": "Other independently reviewed synthetic relationship"},
        ]
        proposal = propose_links(self.workspace, links)
        approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-reviewer")
        self.profile = {
            "spec_version": PROFILE_SPEC, "name": "temporal_review", "description": "Reviewed passage and attachment mapping",
            "bindings": [{
                "connection": "messages", "entity": "message", "table": "messages.records", "kind": "conversation",
                "record_id": "record id", "customer_ref": {"connection": "messages", "entity": "organization", "column": "customer ref"},
                "fields": {"text": "body"},
                "attachments": [{"name": "event", "connection": "metadata", "entity": "event", "table": "metadata.records",
                    "record_id": "metadata id", "local_key": 'call "key"', "remote_key": 'join "key"',
                    "customer_ref": {"connection": "metadata", "entity": "organization", "column": "organization"},
                    "fields": {"started_at": "occurred at", "note": "note"}}],
                "passages": [{"name": "body", "field": "text", "text_path": "", "time": {
                    "source": "attachment", "attachment": "event", "field": "started_at", "path": "",
                    "encoding": "rfc3339", "meaning": "event_started"}}],
            }],
        }
        self.snapshot = capture_snapshot(self.workspace, ["messages", "metadata"])

    @staticmethod
    def write(path, rows):
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def refresh(self, connection):
        self.write(self.root / (connection + ".csv"), self.rows if connection == "messages" else self.metadata)
        service.sync(self.workspace, connection, bootstrap_context=False)
        self.snapshot = capture_snapshot(self.workspace, ["messages", "metadata"])

    def propose(self):
        return propose_profile(self.workspace, self.profile, self.snapshot["snapshot_id"])

    def review(self):
        proposal = self.propose()
        return approve_profile(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-reviewer")

    def project(self):
        return materialize(self.workspace, self.profile["name"], self.snapshot["snapshot_id"])

    def read(self):
        return records_for_customer(self.workspace, self.profile["name"], self.customer["id"], self.snapshot["snapshot_id"])

    def test_same_customer_attachment_has_lineage_without_fanout(self):
        approved = self.review()
        receipt = self.project()
        result = self.read()
        self.assertEqual(receipt["row_count"], 2)
        self.assertEqual(result["coverage"], [{"connection": "messages", "entity": "message", "kind": "conversation", "records": 2, "identity": {"resolved": 2}}])
        self.assertEqual(result["capabilities"], ["conversation"])
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM customer360.records")["rows"], [{"n": 2}])
        for record in result["records"]:
            attachment = record["attachments"]["event"]
            self.assertEqual(attachment["status"], "matched")
            self.assertEqual(attachment["fields"]["note"], "Mapped metadata.")
            self.assertEqual(attachment["source_id"], "m1")
            self.assertEqual(attachment["source_table"], "metadata.records")
            self.assertEqual(attachment["snapshot_id"], self.snapshot["snapshot_id"])
            self.assertEqual(attachment["local_key"]["type"], "VARCHAR")
            self.assertEqual(attachment["customer_ref"]["value"], "party-a")
            self.assertEqual(len(attachment["source_row_sha256"]), 64)
        self.assertNotIn("Other customer content.", json.dumps(result))
        other = records_for_customer(self.workspace, self.profile["name"], self.other["id"], self.snapshot["snapshot_id"])
        self.assertEqual(other["records"], [])
        self.assertEqual(approved["candidate"]["profile"], self.profile)

    def test_attachment_connections_columns_and_key_types_are_reviewed(self):
        self.assertEqual(connections(self.profile), ["messages", "metadata"])
        contract = observed_contract(validate_profile(self.profile), self.snapshot)
        by_table = {item["table"]: {column["name"] for column in item["columns"]} for item in contract}
        self.assertIn('call "key"', by_table["messages.records"])
        self.assertEqual(by_table["metadata.records"], {"metadata id", 'join "key"', "organization", "occurred at", "note"})
        missing = capture_snapshot(self.workspace, ["messages"])
        with self.assertRaisesRegex(ValueError, "exactly the profile"):
            propose_profile(self.workspace, self.profile, missing["snapshot_id"])
        self.profile["bindings"][0]["attachments"][0]["fields"]["note"] = "unobserved"
        with self.assertRaisesRegex(ValueError, "column is absent"):
            self.propose()

    def test_missing_and_unresolved_attachment_never_expose_remote_content(self):
        self.metadata[0]["organization"] = "unreviewed-party"
        self.rows[1]['call "key"'] = "not-a-call"
        self.refresh("metadata")
        self.refresh("messages")
        self.review()
        self.project()
        indexed = {row["source_id"]: row["attachments"]["event"] for row in self.read()["records"]}
        self.assertEqual(indexed["r1"]["reason"], "unresolved_customer")
        self.assertEqual(indexed["r2"]["reason"], "no_matching_row")
        for attachment in indexed.values():
            self.assertEqual(attachment["status"], "unknown")
            self.assertNotIn("fields", attachment)
            self.assertNotIn("source_id", attachment)
            self.assertNotIn("customer_ref", attachment)
        self.assertNotIn("Mapped metadata.", json.dumps(indexed))

    def test_missing_local_key_is_unknown(self):
        self.rows[1]['call "key"'] = ""
        self.refresh("messages")
        self.review()
        self.project()
        record = next(row for row in self.read()["records"] if row["source_id"] == "r2")
        self.assertEqual(record["attachments"]["event"]["reason"], "missing_local_key")

    def test_resolved_cross_customer_join_refuses_before_publication(self):
        self.review()
        initial = self.project()
        self.metadata[0]["organization"] = "party-b"
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "independently resolved base customer"):
            self.project()
        self.assertEqual(self.workspace.query("SELECT DISTINCT materialization_id FROM customer360.records")["rows"],
                         [{"materialization_id": initial["materialization_id"]}])
        with self.assertRaises(ValueError):
            self.read()

    def test_duplicate_remote_keys_and_ids_refuse(self):
        self.metadata.append({**self.metadata[0], "metadata id": "m3"})
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "duplicate remote keys"):
            self.propose()
        self.metadata[-1]['join "key"'] = "call-c"
        self.metadata[-1]["metadata id"] = "m1"
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "duplicate record IDs"):
            self.propose()

    def test_keys_are_exact_typed_and_do_not_coerce_numbers(self):
        for row in self.rows:
            row['call "key"'] = 1
        self.refresh("messages")
        with self.assertRaisesRegex(ValueError, "exactly matching observed SQL types"):
            self.propose()
        self.rows[0]['call "key"'] = self.rows[1]['call "key"'] = 1.5
        self.metadata[0]['join "key"'] = 1.5
        self.metadata[1]['join "key"'] = 2.5
        self.refresh("messages")
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "exact string or integer"):
            self.propose()

    def test_time_only_change_updates_evidence_and_unchanged_refresh_does_not(self):
        self.review()
        initial = self.project()
        before = {row["source_id"]: row for row in self.read()["records"]}
        self.refresh("metadata")
        unchanged = self.project()
        same = {row["source_id"]: row for row in self.read()["records"]}
        self.assertEqual({key: row["evidence_id"] for key, row in before.items()},
                         {key: row["evidence_id"] for key, row in same.items()})
        self.metadata[0]["occurred at"] = "2026-01-03T10:00:00Z"
        self.refresh("metadata")
        changed = self.project()
        after = {row["source_id"]: row for row in self.read()["records"]}
        self.assertEqual(initial["profile_approval"], changed["profile_approval"])
        self.assertEqual(unchanged["row_count"], changed["row_count"])
        for key in before:
            self.assertEqual(before[key]["fields"], after[key]["fields"])
            self.assertNotEqual(before[key]["evidence_id"], after[key]["evidence_id"])
            self.assertNotEqual(before[key]["attachments"]["event"]["source_row_sha256"],
                                after[key]["attachments"]["event"]["source_row_sha256"])

    def test_attachment_schema_or_pending_source_change_refuses(self):
        self.review()
        self.project()
        self.metadata[0]["occurred at"] = "different type"
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "Mapped column types changed"):
            self.project()
        with self.assertRaises(ValueError):
            self.read()

    def test_tampered_attachment_history_cannot_be_read_or_reused(self):
        self.review()
        receipt = self.project()
        with self.workspace._connect(writable=True) as conn:
            key, encoded = conn.execute("SELECT record_key,payload FROM _contextbase.customer360_record_history LIMIT 1").fetchone()
            record = json.loads(encoded)
            record["attachments"]["event"]["fields"]["note"] = "Injected metadata"
            conn.execute("UPDATE _contextbase.customer360_record_history SET payload=? WHERE record_key=?",
                         [json.dumps(record), key])
        with self.assertRaisesRegex(ValueError, "history no longer matches"):
            self.read()
        with self.assertRaisesRegex(ValueError, "history no longer matches"):
            self.project()
        self.assertEqual(receipt["row_count"], 2)

    def test_legacy_record_and_observed_contract_hashes_are_preserved(self):
        binding = self.profile["bindings"][0]
        binding.pop("attachments")
        binding.pop("passages")
        self.snapshot = capture_snapshot(self.workspace, ["messages"])
        self.review()
        self.project()
        with readable_profile(self.workspace, self.profile["name"], self.snapshot["snapshot_id"]) as (_, _, _, _, payload):
            for record in payload["records"]:
                legacy_content = {"fields": record["fields"], "source_id": record["source_id"], "customer_ref": "org-a"}
                self.assertEqual(record["source_row_sha256"], sha256(legacy_content))
                self.assertEqual(record["evidence_id"], sha256([record["record_key"], sha256(legacy_content)]))
                self.assertNotIn("attachments", record)
        self.assertTrue(all("attachments" not in row for row in self.read()["records"]))
        self.assertEqual(observed_contract(self.profile, self.snapshot), [{"table": "messages.records", "columns": [
            {"name": "body", "type": "VARCHAR"}, {"name": "customer ref", "type": "VARCHAR"},
            {"name": "record id", "type": "VARCHAR"}]}])

    def test_attachment_rows_and_bytes_have_same_caps(self):
        self.metadata.append({**self.metadata[1], "metadata id": "m3", 'join "key"': "call-c"})
        self.refresh("metadata")
        with patch("contextbase.customer360.profiles.MAX_ROWS_PER_BINDING", 2):
            with self.assertRaisesRegex(ValueError, "bounded projection size"):
                self.propose()
        self.metadata[0]["note"] = "x" * 65_000
        self.refresh("metadata")
        with self.assertRaisesRegex(ValueError, "bounded projection size"):
            self.propose()

    def test_passage_contract_rejects_unreviewed_paths_sources_and_extra_fields(self):
        binding = self.profile["bindings"][0]
        valid = copy.deepcopy(self.profile)
        self.assertEqual(validate_profile(valid), valid)
        mutations = [
            lambda b: b["passages"][0].update(text_path="not/a/pointer"),
            lambda b: b["passages"][0].update(field="unknown"),
            lambda b: b["passages"][0]["time"].update(field="unmapped"),
            lambda b: b["passages"][0]["time"].update(attachment="unknown"),
            lambda b: b["passages"][0]["time"].update(path="/bad~2escape/time"),
            lambda b: b["passages"][0]["time"].update(meaning="latest"),
            lambda b: b["passages"][0]["time"].update(encoding="inferred"),
            lambda b: b["passages"][0]["time"].update(source="item"),
            lambda b: b["attachments"][0].update(table="outside.records"),
            lambda b: b["attachments"].append(copy.deepcopy(b["attachments"][0])),
            lambda b: b["passages"].append(copy.deepcopy(b["passages"][0])),
            lambda b: b.update(attachments=b["attachments"] * 9),
            lambda b: b.update(passages=b["passages"] * 17),
        ]
        for mutate in mutations:
            candidate = copy.deepcopy(valid)
            mutate(candidate["bindings"][0])
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_profile(candidate)
        for timestamp in (
            {"source": "field", "field": "text", "path": "/time", "encoding": "unix_seconds", "meaning": "record_created"},
            {"source": "item", "path": "/created_at", "encoding": "unix_seconds", "meaning": "passage_created"},
        ):
            binding["passages"][0].update(text_path="/items/*/body", time=timestamp)
            self.assertEqual(validate_profile(self.profile), self.profile)


if __name__ == "__main__":
    unittest.main()
