"""Reviewed projection integration over actual local connector publications."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from contextbase.connectors import service
from contextbase.customer360.contracts import PROFILE_SPEC, sha256, validate_profile
from contextbase.customer360.identity import create_customer, propose_links, approve_links
from contextbase.customer360.profiles import (propose_profile, approve_profile, load_profile, materialize,
                                              records_for_customer, readable_profile, readable_customer)
from contextbase.customer360.snapshots import capture_snapshot
from contextbase.workspace import Workspace


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = Workspace(self.root / "workspace")
        self.customer = create_customer(self.w, "example-customer")
        self.billing = self.root / "billing.csv"
        self.billing.write_text("invoice_id,party_ref,amount,currency\ninv-a,party-a,150,USD\ninv-b,party-a,250,USD\ninv-c,missing,80,EUR\n")
        self.support = self.root / "support.csv"
        self.support.write_text("case_id,organization,body,status\ncase-a,org-a,Needs follow-up,open\ncase-b,org-a,Request received,open\n")
        for connection, path in (("billing", self.billing), ("support", self.support)):
            service.add(self.w, "csv", name=connection, path=path)
            service.sync(self.w, connection, bootstrap_context=False)
        links = [{"connection": connection, "entity": entity, "source_id": key,
                  "customer_id": self.customer["id"], "basis": "Explicit synthetic mapping reviewed independently"}
                 for connection, entity, key in (("billing", "party", "party-a"), ("support", "organization", "org-a"))]
        proposed = propose_links(self.w, links)
        approve_links(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-owner")
        self.profile = {"spec_version": PROFILE_SPEC, "name": "customer_review", "description": "Billing and support fields",
                        "bindings": [
                            {"connection": "billing", "entity": "invoice", "table": "billing.records", "kind": "billing",
                             "record_id": "invoice_id", "customer_ref": {"connection": "billing", "entity": "party", "column": "party_ref"},
                             "fields": {"amount": "amount", "currency": "currency"}},
                            {"connection": "support", "entity": "case", "table": "support.records", "kind": "support",
                             "record_id": "case_id", "customer_ref": {"connection": "support", "entity": "organization", "column": "organization"},
                             "fields": {"text": "body", "status": "status"}}]}
        self.snapshot = capture_snapshot(self.w, ["billing", "support"])

    def reviewed(self):
        proposed = propose_profile(self.w, self.profile, self.snapshot["snapshot_id"])
        return approve_profile(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")

    def read(self, snapshot=None):
        return records_for_customer(self.w, self.profile["name"], self.customer["id"],
                                    (snapshot or self.snapshot)["snapshot_id"])

    def test_reviewed_projection_joins_independent_source_keys(self):
        approved = self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        result = self.read()
        self.assertEqual(receipt["row_count"], 5)
        self.assertEqual(result["capabilities"], ["billing", "support"])
        self.assertEqual(len(result["records"]), 4)
        bills = [r for r in result["records"] if r["kind"] == "billing"]
        self.assertEqual(sum(r["fields"]["amount"] for r in bills), 400)
        self.assertEqual({r["source_id"] for r in bills}, {"inv-a", "inv-b"})
        self.assertEqual(receipt["coverage"][0]["identity"], {"resolved": 2, "unresolved": 1})
        self.assertEqual(approved["approval_kind"], "local_operator_mapping_review")
        self.assertEqual(load_profile(Workspace(self.w.root), self.profile["name"]), approved)

    def test_unapproved_and_wrong_hash_cannot_publish(self):
        with self.assertRaises(ValueError):
            materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        proposed = propose_profile(self.w, self.profile, self.snapshot["snapshot_id"])
        with self.assertRaises(ValueError):
            approve_profile(self.w, proposed["proposal_id"], "0" * 64, "example-reviewer")

    def test_stale_identity_and_changed_source_prevent_approval(self):
        proposed = propose_profile(self.w, self.profile, self.snapshot["snapshot_id"])
        link = propose_links(self.w, [{"connection": "billing", "entity": "party", "source_id": "alias-a",
                                     "customer_id": self.customer["id"], "basis": "Reviewed replacement source key"}])
        approve_links(self.w, link["proposal_id"], link["approval_sha256"], "example-owner")
        with self.assertRaisesRegex(ValueError, "Identity or schema changed"):
            approve_profile(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
        proposed = propose_profile(self.w, self.profile, self.snapshot["snapshot_id"])
        self.billing.write_text(self.billing.read_text().replace(",150,", ",175,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        with self.assertRaises(ValueError):
            approve_profile(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")

    def test_refresh_reuses_reviewed_mapping_and_retains_history(self):
        approved = self.reviewed()
        first = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        self.billing.write_text(self.billing.read_text().replace(",150,", ",175,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        updated = capture_snapshot(self.w, ["billing", "support"])
        with self.assertRaises(ValueError):
            self.read()
        second = materialize(Workspace(self.w.root), self.profile["name"], updated["snapshot_id"])
        self.assertEqual(first["profile_approval"], second["profile_approval"])
        self.assertEqual(load_profile(self.w, self.profile["name"]), approved)
        bills = [r for r in self.read(updated)["records"] if r["kind"] == "billing"]
        self.assertEqual(sum(r["fields"]["amount"] for r in bills), 425)
        materialize(self.w, self.profile["name"], updated["snapshot_id"])
        self.assertEqual(self.w.query("SELECT count(*) n FROM _contextbase.customer360_record_history")["rows"], [{"n": 10}])

    def test_projection_tampering_and_unknown_customer_are_rejected(self):
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        with self.assertRaisesRegex(ValueError, "belonging"):
            records_for_customer(self.w, self.profile["name"], "other-workspace-customer", self.snapshot["snapshot_id"])
        with self.w._connect(writable=True) as conn:
            conn.execute("UPDATE customer360.records SET fields=? WHERE source_id='inv-a'", ['{"amount":9000,"currency":"USD"}'])
        with self.assertRaisesRegex(ValueError, "projection changed"):
            self.read()

    def test_duplicate_physical_binding_and_unobserved_columns_fail(self):
        duplicate = copy.deepcopy(self.profile)
        duplicate["bindings"].append({**duplicate["bindings"][0], "entity": "again"})
        with self.assertRaisesRegex(ValueError, "bound only once"):
            validate_profile(duplicate)
        invalid = copy.deepcopy(self.profile)
        invalid["bindings"][0]["fields"]["amount"] = 'amount); DROP TABLE billing.records; --'
        with self.assertRaisesRegex(ValueError, "column is absent"):
            propose_profile(self.w, invalid, self.snapshot["snapshot_id"])

    def test_duplicate_record_ids_refuse_complete_projection(self):
        self.billing.write_text(self.billing.read_text() + "inv-a,party-a,1,USD\n")
        service.sync(self.w, "billing", bootstrap_context=False)
        snapshot = capture_snapshot(self.w, ["billing", "support"])
        with self.assertRaisesRegex(ValueError, "duplicate record IDs"):
            propose_profile(self.w, self.profile, snapshot["snapshot_id"])

    def test_large_record_refused_before_projection(self):
        self.support.write_text("case_id,organization,body,status\ncase-a,org-a," + "x" * 100_000 + ",open\n")
        service.sync(self.w, "support", bootstrap_context=False)
        snapshot = capture_snapshot(self.w, ["billing", "support"])
        with self.assertRaisesRegex(ValueError, "bounded projection"):
            propose_profile(self.w, self.profile, snapshot["snapshot_id"])

    def test_empty_snapshot_can_be_materialized_without_inventing_evidence(self):
        self.profile["bindings"] = [self.profile["bindings"][1]]
        self.support.write_text("case_id,organization,body,status\n")
        service.sync(self.w, "support", bootstrap_context=False)
        self.snapshot = capture_snapshot(self.w, ["support"])
        self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        self.assertEqual(receipt["row_count"], 0)
        self.assertEqual(self.read()["records"], [])

    def store_receipt(self, receipt):
        with self.w._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.customer360_materializations SET payload=? WHERE materialization_id=?",
                         [json.dumps(receipt), receipt["materialization_id"]])

    def test_materialization_receipt_is_sealed_and_repeat_reuses_exact_receipt(self):
        self.reviewed()
        first = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        expected = sha256({key: value for key, value in first.items() if key != "receipt_sha256"})
        self.assertEqual(first["receipt_sha256"], expected)
        self.assertEqual(materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"]), first)
        result = self.read()
        self.assertEqual(result["coverage"], first["coverage"])
        self.assertEqual(result["capabilities"], first["capabilities"])
        self.assertEqual(result["profile_version"], first["profile_version"])

    def test_unsealed_receipt_metadata_edits_fail_read_and_reuse(self):
        self.reviewed()
        original = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        edits = {
            "coverage": lambda r: r["coverage"][0].update(identity={"resolved": 3}),
            "capabilities": lambda r: r.update(capabilities=["billing", "support", "usage"]),
            "row_count": lambda r: r.update(row_count=999),
            "profile_version": lambda r: r.update(profile_version=999),
            "created_at": lambda r: r.update(created_at="2000-01-01T00:00:00+00:00"),
            "missing_hash": lambda r: r.pop("receipt_sha256"),
        }
        for field, edit in edits.items():
            with self.subTest(field=field):
                altered = copy.deepcopy(original)
                edit(altered)
                self.store_receipt(altered)
                with self.assertRaisesRegex(ValueError, "receipt changed or lacks"):
                    self.read()
                with self.assertRaisesRegex(ValueError, "receipt changed or lacks"):
                    materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
                with self.w._connect() as conn:
                    stored = json.loads(conn.execute("SELECT payload FROM _contextbase.customer360_materializations").fetchone()[0])
                self.assertEqual(stored, altered)
                self.store_receipt(original)
        self.assertEqual(len(self.read()["records"]), 4)

    def test_rehashed_metadata_still_must_match_profile_and_evidence(self):
        self.reviewed()
        original = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        edits = {
            "coverage": lambda r: r["coverage"][0].update(identity={"resolved": 3}),
            "capabilities": lambda r: r.update(capabilities=["billing"]),
            "row_count": lambda r: r.update(row_count=999),
            "profile_version": lambda r: r.update(profile_version=True),
            "meaning": lambda r: r.update(meaning="business_metrics_certified"),
        }
        for field, edit in edits.items():
            with self.subTest(field=field):
                altered = copy.deepcopy(original)
                edit(altered)
                altered["receipt_sha256"] = sha256({key: value for key, value in altered.items() if key != "receipt_sha256"})
                self.store_receipt(altered)
                with self.assertRaisesRegex(ValueError, "metadata disagrees"):
                    self.read()
                with self.assertRaisesRegex(ValueError, "metadata disagrees"):
                    materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
                self.store_receipt(original)

    def test_repeat_materialization_refuses_modified_history(self):
        self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        with self.w._connect(writable=True) as conn:
            row = conn.execute("SELECT record_key,payload FROM _contextbase.customer360_record_history LIMIT 1").fetchone()
            value = json.loads(row[1])
            value["fields"]["synthetic_extra"] = "modified history"
            conn.execute("UPDATE _contextbase.customer360_record_history SET payload=? WHERE record_key=?",
                         [json.dumps(value), row[0]])
        with self.assertRaisesRegex(ValueError, "history no longer matches"):
            materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        with self.assertRaisesRegex(ValueError, "history no longer matches"):
            self.read()
        with self.w._connect() as conn:
            stored = json.loads(conn.execute("SELECT payload FROM _contextbase.customer360_materializations").fetchone()[0])
        self.assertEqual(stored, receipt)

    def test_projection_json_boolean_and_float_do_not_equal_integer_evidence(self):
        self.billing.write_text(self.billing.read_text().replace(",150,", ",1,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        self.snapshot = capture_snapshot(self.w, ["billing", "support"])
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        for value in (True, 1.0):
            with self.subTest(value=value):
                with self.w._connect(writable=True) as conn:
                    conn.execute("UPDATE customer360.records SET fields=? WHERE source_id='inv-a'",
                                 [json.dumps({"amount": value, "currency": "USD"})])
                with self.assertRaisesRegex(ValueError, "projection changed"):
                    self.read()

    def test_scalar_nanoseconds_and_interval_months_survive_projection(self):
        path = self.root / "precise-events.parquet"
        with duckdb.connect() as conn:
            conn.sql("SELECT 'event-a' AS event_id, 'party-a' AS party_ref, "
                     "TIMESTAMP_NS '2026-01-01 00:00:00.123456001' AS seen_at, INTERVAL '1 month' AS period").write_parquet(str(path))
        service.add(self.w, "parquet", name="precision", path=path)
        service.sync(self.w, "precision", bootstrap_context=False)
        self.profile["bindings"] = [{"connection": "precision", "entity": "event", "table": "precision.records", "kind": "usage",
                                     "record_id": "event_id", "customer_ref": {"connection": "billing", "entity": "party", "column": "party_ref"},
                                     "fields": {"seen_at": "seen_at", "period": "period"}}]
        self.snapshot = capture_snapshot(self.w, ["precision"])
        approved = self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        records = self.read()["records"]
        self.assertEqual(records[0]["fields"], {"seen_at": "2026-01-01 00:00:00.123456001", "period": "1 month"})
        types = {item["name"]: item["type"] for item in approved["candidate"]["schema_contract"][0]["columns"]}
        self.assertEqual(types["seen_at"], "TIMESTAMP_NS")
        self.assertEqual(types["period"], "INTERVAL")

    def test_atomic_profile_context_contains_full_validated_history(self):
        approved = self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        with readable_profile(self.w, self.profile["name"], self.snapshot["snapshot_id"]) as (conn, snapshot, observed_approval, observed_receipt, payload):
            self.assertEqual(snapshot, self.snapshot)
            self.assertEqual(observed_approval, approved)
            self.assertEqual(observed_receipt, receipt)
            self.assertEqual(payload["scope"], "trusted_local_operator_selected_profile")
            self.assertEqual(payload["receipt_sha256"], receipt["receipt_sha256"])
            self.assertEqual(payload["profile_approval"], approved["approval_sha256"])
            self.assertEqual(len(payload["records"]), 5)
            self.assertEqual(sum(record["customer_id"] is None for record in payload["records"]), 1)
            for record in payload["records"]:
                self.assertIn("source_row_sha256", record)
                self.assertIn("source_table", record)
                self.assertIn("link_status", record)
            self.assertEqual(conn.execute("SELECT sum(amount) FROM billing.records").fetchone()[0], 480)
            with self.assertRaisesRegex(ValueError, "sync is running"):
                service.sync(self.w, "billing", bootstrap_context=False)
        self.assertFalse((self.w.root / "connectors/billing/sync.lock").exists())

    def test_atomic_customer_context_preserves_public_payload_and_query_scope(self):
        self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        previous = self.read()
        with readable_customer(self.w, self.profile["name"], self.customer["id"], self.snapshot["snapshot_id"]) as (conn, _, _, observed_receipt, payload):
            self.assertEqual(payload, previous)
            self.assertEqual(observed_receipt, receipt)
            self.assertEqual(set(payload), {"customer_id", "snapshot_id", "profile_version", "capabilities", "source_observed_at", "coverage", "records"})
            self.assertEqual({record["source_id"] for record in payload["records"]}, {"inv-a", "inv-b", "case-a", "case-b"})
            rows = conn.execute("SELECT record_key FROM customer360.records WHERE profile=? AND customer_id=? AND materialization_id=?",
                                [self.profile["name"], self.customer["id"], receipt["materialization_id"]]).fetchall()
            self.assertEqual({row[0] for row in rows}, {record["record_key"] for record in payload["records"]})
            with self.assertRaisesRegex(ValueError, "sync is running"):
                service.sync(self.w, "support", bootstrap_context=False)

    def test_atomic_consumer_work_commits_or_rolls_back_with_same_transaction(self):
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        with self.assertRaisesRegex(ValueError, "synthetic answer failure"):
            with readable_customer(self.w, self.profile["name"], self.customer["id"], self.snapshot["snapshot_id"]) as (conn, _, _, _, _):
                conn.execute("CREATE TABLE _contextbase.answer_probe AS SELECT 1 AS value")
                raise ValueError("synthetic answer failure")
        with self.w._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='answer_probe'").fetchone()[0], 0)
        self.assertFalse((self.w.root / "connectors/billing/sync.lock").exists())
        with readable_profile(self.w, self.profile["name"], self.snapshot["snapshot_id"]) as (conn, _, _, _, _):
            conn.execute("CREATE TABLE _contextbase.answer_probe AS SELECT 2 AS value")
        with self.w._connect() as conn:
            self.assertEqual(conn.execute("SELECT value FROM _contextbase.answer_probe").fetchone()[0], 2)

    def assert_contexts_refuse_before_yield(self, pattern):
        contexts = (readable_profile(self.w, self.profile["name"], self.snapshot["snapshot_id"]),
                    readable_customer(self.w, self.profile["name"], self.customer["id"], self.snapshot["snapshot_id"]))
        for context in contexts:
            yielded = False
            with self.assertRaisesRegex(ValueError, pattern):
                with context:
                    yielded = True
            self.assertFalse(yielded)

    def test_atomic_contexts_refuse_altered_receipt_before_yield(self):
        self.reviewed()
        receipt = materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        receipt["capabilities"] = ["usage"]
        self.store_receipt(receipt)
        self.assert_contexts_refuse_before_yield("receipt changed or lacks")

    def test_atomic_contexts_refuse_new_source_publication_before_yield(self):
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        service.sync(self.w, "billing", bootstrap_context=False)
        self.assert_contexts_refuse_before_yield("no longer matches")

    def test_atomic_contexts_refuse_changed_profile_and_unmaterialized_profile(self):
        self.reviewed()
        self.assert_contexts_refuse_before_yield("Materialize the current")
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        changed = copy.deepcopy(self.profile)
        changed["description"] = "Revised reviewed description"
        proposed = propose_profile(self.w, changed, self.snapshot["snapshot_id"])
        approve_profile(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
        self.assert_contexts_refuse_before_yield("context changed")

    def test_atomic_contexts_refuse_changed_identity_before_yield(self):
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        proposed = propose_links(self.w, [{"connection": "billing", "entity": "party", "source_id": "new-alias",
                                          "customer_id": self.customer["id"], "basis": "Synthetic reviewed extra identity"}])
        approve_links(self.w, proposed["proposal_id"], proposed["approval_sha256"], "example-owner")
        self.assert_contexts_refuse_before_yield("context changed")

    def test_atomic_customer_membership_is_checked_before_yield(self):
        self.reviewed()
        materialize(self.w, self.profile["name"], self.snapshot["snapshot_id"])
        yielded = False
        with self.assertRaisesRegex(ValueError, "belonging"):
            with readable_customer(self.w, self.profile["name"], "unknown-customer", self.snapshot["snapshot_id"]):
                yielded = True
        self.assertFalse(yielded)


if __name__ == "__main__":
    unittest.main()
