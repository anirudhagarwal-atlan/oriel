"""Local onboarding discovery over real committed CSV/DuckDB publications."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from contextbase.connectors import service
from contextbase.customer360 import contexts, identity, onboarding, profiles, snapshots
from contextbase.customer360.contracts import sha256
from contextbase.workspace import Workspace
from tests import test_customer360_profiles as fixtures
from tests.test_customer360_contexts import definitions


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.w
        self.snapshot = self.fixture.snapshot["snapshot_id"]
        self.profile = self.fixture.profile["name"]

    def ready(self):
        approved = self.fixture.reviewed()
        receipt = profiles.materialize(self.w, self.profile, self.snapshot)
        candidate = contexts.propose_context(self.w, self.profile, self.snapshot, definitions())
        context = contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")
        return approved, receipt, context

    def link(self, source_id="new-alias"):
        return {"connection": "billing", "entity": "party", "source_id": source_id,
                "customer_id": self.fixture.customer["id"], "basis": "Explicit synthetic alias requiring review"}

    def test_empty_workspace_discovery_does_not_initialize_or_write(self):
        workspace = Workspace(self.fixture.root / "not-created")
        state = onboarding.workspace_state(workspace)
        self.assertFalse(state["exists"])
        self.assertIsNone(state["workspace_id"])
        self.assertEqual((state["customers"], state["profiles"], state["sources"]), ([], [], []))
        for kind in ("links", "profile", "context"):
            self.assertEqual(onboarding.pending_proposals(workspace, kind)["proposals"], [])
        self.assertFalse(workspace.root.exists())

    def test_committed_source_schema_and_samples_available_before_mapping(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("No upstream calls")) as network, \
                patch.object(service, "_load", side_effect=AssertionError("No credential-resolving source loading")):
            result = onboarding.inspect_sources(self.w, self.snapshot, sample_rows=2)
        network.assert_not_called()
        self.assertEqual(result["snapshot_id"], self.snapshot)
        self.assertEqual(result["manifest_sha256"], self.fixture.snapshot["manifest_sha256"])
        self.assertEqual(result["upstream_completeness"], "not_verified")
        self.assertEqual({s["name"] for s in result["sources"]}, {"billing", "support"})
        table = next(s for s in result["sources"] if s["name"] == "billing")["tables"][0]
        self.assertEqual(table["table"], "billing.records")
        self.assertEqual(table["rows"], 3)
        self.assertEqual(table["sampled_rows"], 2)
        self.assertTrue(table["samples_truncated"])
        self.assertEqual(table["samples"][0]["values"], {"invoice_id": "inv-a", "party_ref": "party-a", "amount": 150, "currency": "USD"})
        self.assertEqual(table["samples"][0]["omitted_fields"], [])
        self.assertEqual({c["name"] for c in table["columns"]}, {"invoice_id", "party_ref", "amount", "currency"})
        self.assertEqual({s["name"]: s["receipt"]["finished_at"] for s in self.fixture.snapshot["sources"]}, result["source_observed_at"])
        serialized = json.dumps(result)
        self.assertNotIn("credential_env", serialized)
        self.assertNotIn(str(self.fixture.root), serialized)
        with self.w._connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM information_schema.tables WHERE table_name='customer360_profile_approvals'").fetchone())

    def test_inspection_bounds_omit_large_cells_and_preserve_null(self):
        self.fixture.support.write_text("case_id,organization,body,status\ncase-a,org-a," + "x" * 3000 + ",open\ncase-b,org-a,,closed\n")
        service.sync(self.w, "support", bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        result = onboarding.inspect_sources(self.w, self.snapshot)
        samples = next(s for s in result["sources"] if s["name"] == "support")["tables"][0]["samples"]
        self.assertEqual(samples[0]["omitted_fields"], [{"field": "body", "reason": "cell_size_limit"}])
        self.assertNotIn("body", samples[0]["values"])
        self.assertIsNone(samples[1]["values"]["body"])
        self.assertEqual(samples[1]["omitted_fields"], [])
        schema_only = onboarding.inspect_sources(self.w, self.snapshot, sample_rows=0)
        self.assertTrue(all(not t["samples"] for s in schema_only["sources"] for t in s["tables"]))
        for count in (-1, True, 6, 1.5):
            with self.assertRaises(ValueError):
                onboarding.inspect_sources(self.w, self.snapshot, sample_rows=count)
        with patch.object(onboarding, "MAX_INSPECTION_BYTES", 100), self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)
        with patch.object(onboarding, "MAX_COLUMNS", 1), self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)
        with patch.object(onboarding, "MAX_TABLES", 1), self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)

    def test_source_locks_and_single_transaction_are_held_during_sampling(self):
        original = onboarding._samples
        sampled = []
        def inspect(connection, table, count):
            sampled.append(id(connection))
            for source in ("billing", "support"):
                with self.assertRaises(ValueError):
                    with service._lock(self.w.root / "connectors" / source):
                        self.fail("Selected source lock was not held")
            return original(connection, table, count)
        with patch.object(onboarding, "_samples", side_effect=inspect):
            onboarding.inspect_sources(self.w, self.snapshot)
        self.assertEqual(len(sampled), 2)
        self.assertEqual(len(set(sampled)), 1)

    def test_native_sample_display_preserves_precise_decimal_timestamp_and_interval(self):
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE SCHEMA example")
            connection.execute("CREATE TABLE example.records (amount DECIMAL(30,6), event_at TIMESTAMP_NS, duration INTERVAL, flag BOOLEAN)")
            connection.execute("INSERT INTO example.records VALUES (12345678901234567890.123456, TIMESTAMP_NS '2026-01-01 00:00:00.123456789', INTERVAL '1 month 2 days', true)")
            table = {"table": "example.records", "columns": [
                {"name": "amount", "type": "DECIMAL(30,6)"}, {"name": "event_at", "type": "TIMESTAMP_NS"},
                {"name": "duration", "type": "INTERVAL"}, {"name": "flag", "type": "BOOLEAN"}]}
            sample = onboarding._samples(connection, table, 1)[0]
        self.assertEqual(sample["values"]["amount"], "12345678901234567890.123456")
        self.assertEqual(sample["values"]["event_at"], "2026-01-01 00:00:00.123456789")
        self.assertEqual(sample["values"]["duration"], "1 month 2 days")
        self.assertIs(sample["values"]["flag"], True)
        self.assertEqual(sample["omitted_fields"], [])

    def test_inspection_rejects_stale_failed_or_changed_source(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        with patch.object(snapshots, "_now", return_value=future), self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)
        with self.w._connect(writable=True) as connection:
            connection.execute("UPDATE billing.records SET amount=999 WHERE invoice_id='inv-a'")
        with self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)
        service.sync(self.w, "billing", bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        self.fixture.billing.unlink()
        with self.assertRaises(FileNotFoundError):
            service.sync(self.w, "billing", bootstrap_context=False)
        with self.assertRaises(ValueError):
            onboarding.inspect_sources(self.w, self.snapshot)
        state = onboarding.workspace_state(self.w)
        billing = next(s for s in state["sources"] if s["name"] == "billing")
        self.assertEqual(billing["status"], "failed")
        self.assertIsNotNone(billing["last_publication"])
        self.assertEqual(state["authority"], "discovery_only_revalidate_before_action")

    def test_pending_link_proposals_are_exact_and_reviewed_ones_disappear(self):
        first = identity.propose_links(self.w, [self.link()])
        self.assertEqual(onboarding.pending_proposals(self.w)["proposals"], [first])
        conflicting = identity.propose_links(self.w, [self.link("duplicate"), self.link("duplicate")])
        pending = onboarding.pending_proposals(self.w, limit=1)
        self.assertEqual(pending["proposals"], [conflicting])
        self.assertTrue(pending["truncated"])
        identity.approve_links(self.w, first["proposal_id"], first["approval_sha256"], "synthetic-reviewer")
        pending = onboarding.pending_proposals(self.w)
        self.assertEqual(pending["proposals"], [conflicting])
        self.assertEqual(pending["proposals"][0]["status"], "conflict")
        with self.assertRaises(ValueError):
            identity.approve_links(self.w, conflicting["proposal_id"], conflicting["approval_sha256"], "synthetic-reviewer")

    def test_pending_profile_and_context_proposals_match_original_exact_hashes(self):
        profile = profiles.propose_profile(self.w, self.fixture.profile, self.snapshot)
        self.assertEqual(onboarding.pending_proposals(self.w, "profile")["proposals"], [profile])
        profiles.approve_profile(self.w, profile["proposal_id"], profile["approval_sha256"], "synthetic-reviewer")
        self.assertEqual(onboarding.pending_proposals(self.w, "profile")["proposals"], [])
        profiles.materialize(self.w, self.profile, self.snapshot)
        context = contexts.propose_context(self.w, self.profile, self.snapshot, definitions())
        self.assertEqual(onboarding.pending_proposals(self.w, "context")["proposals"], [context])
        contexts.approve_context(self.w, context["proposal_id"], context["approval_sha256"], "synthetic-reviewer")
        self.assertEqual(onboarding.pending_proposals(self.w, "context")["proposals"], [])
        for kind, limit in (("sql", 1), ("links", True), ("context", 0), ("profile", 101)):
            with self.assertRaises(ValueError):
                onboarding.pending_proposals(self.w, kind, limit=limit)

    def test_pending_stale_context_remains_discoverable_but_not_approvable(self):
        self.ready()
        pending = contexts.propose_context(self.w, self.profile, self.snapshot, definitions())
        replacement = contexts.propose_context(self.w, self.profile, self.snapshot, definitions())
        contexts.approve_context(self.w, replacement["proposal_id"], replacement["approval_sha256"], "synthetic-reviewer")
        result = onboarding.pending_proposals(self.w, "context")
        self.assertEqual(result["proposals"], [pending])
        self.assertEqual(result["scope"], "stored_pending_proposals_not_approval_authority")
        with self.assertRaises(ValueError):
            contexts.approve_context(self.w, pending["proposal_id"], pending["approval_sha256"], "synthetic-reviewer")

    def test_pending_display_never_silently_truncates_an_exact_proposal(self):
        identity.propose_links(self.w, [self.link()])
        with patch.object(onboarding, "MAX_LIST_BYTES", 100), self.assertRaises(ValueError):
            onboarding.pending_proposals(self.w)

    def test_configured_unsynced_sources_and_customer_discovery_truncation(self):
        workspace = Workspace(self.fixture.root / "configured-only")
        service.add(workspace, "csv", name="example", path=self.fixture.billing)
        state = onboarding.workspace_state(workspace)
        self.assertFalse(state["exists"])
        self.assertEqual(state["sources"][0]["status"], "configured")
        self.assertIsNone(state["sources"][0]["last_publication"])
        self.assertFalse(workspace.db_path.exists())
        identity.create_customer(self.w, "another-example-customer")
        with patch.object(onboarding, "MAX_DISCOVERY_ITEMS", 1), patch.object(onboarding, "_source_statuses", return_value=[]):
            bounded = onboarding.workspace_state(self.w)
        self.assertEqual(len(bounded["customers"]), 1)
        self.assertTrue(bounded["truncated"]["customers"])

    def test_pending_payload_and_identity_candidate_index_tampering_refused(self):
        proposal = identity.propose_links(self.w, [self.link()])
        with self.w._connect(writable=True) as connection:
            connection.execute("UPDATE _contextbase.customer360_link_candidates SET source_id='altered' WHERE proposal_id=?", [proposal["proposal_id"]])
        with self.assertRaises(ValueError):
            onboarding.pending_proposals(self.w)
        profile = profiles.propose_profile(self.w, self.fixture.profile, self.snapshot)
        with self.w._connect(writable=True) as connection:
            value = json.loads(connection.execute("SELECT payload FROM _contextbase.customer360_profile_candidates WHERE proposal_id=?", [profile["proposal_id"]]).fetchone()[0])
            value["profile"]["description"] = "Altered after proposal"
            connection.execute("UPDATE _contextbase.customer360_profile_candidates SET payload=? WHERE proposal_id=?", [json.dumps(value), profile["proposal_id"]])
        with self.assertRaises(ValueError):
            onboarding.pending_proposals(self.w, "profile")

    def test_workspace_state_is_bounded_metadata_without_source_text_or_credentials(self):
        approved, receipt, context = self.ready()
        before = self.w.db_path.stat().st_mtime_ns
        state = onboarding.workspace_state(self.w)
        self.assertTrue(state["exists"])
        self.assertEqual(state["workspace_id"], self.fixture.customer["workspace_id"])
        self.assertEqual(state["customers"], [{"id": self.fixture.customer["id"], "label": "example-customer"}])
        self.assertEqual(len(state["profiles"]), 1)
        found = state["profiles"][0]
        self.assertEqual(found["name"], self.profile)
        self.assertEqual(found["approval_sha256"], approved["approval_sha256"])
        self.assertEqual(found["capabilities"], ["billing", "support"])
        self.assertEqual(found["materialization"]["materialization_id"], receipt["materialization_id"])
        self.assertTrue(found["materialization"]["matches_current_profile"])
        self.assertTrue(found["materialization"]["matches_current_identity"])
        self.assertEqual(found["context"]["context_id"], context["context_id"])
        self.assertTrue(found["context"]["matches_current_profile"])
        raw = json.dumps(state)
        for excluded in ("Needs follow-up", "credential_env", "ORIEL_", str(self.fixture.billing), "source_row_sha256"):
            self.assertNotIn(excluded, raw)
        self.assertEqual(before, self.w.db_path.stat().st_mtime_ns)
        with patch.object(onboarding, "MAX_DISCOVERY_ITEMS", 1):
            with self.assertRaises(ValueError):  # Two configured sources exceed declared source scope.
                onboarding.workspace_state(self.w)

    def test_workspace_state_surfaces_changed_mapping_without_claiming_current_context(self):
        self.ready()
        revised = copy.deepcopy(self.fixture.profile)
        revised["description"] = "Newly reviewed mapping meaning"
        candidate = profiles.propose_profile(self.w, revised, self.snapshot)
        approved = profiles.approve_profile(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")
        state = onboarding.workspace_state(self.w)
        profile = state["profiles"][0]
        self.assertEqual(profile["approval_sha256"], approved["approval_sha256"])
        self.assertFalse(profile["materialization"]["matches_current_profile"])
        self.assertFalse(profile["context"]["matches_current_profile"])
        with self.assertRaises(ValueError):
            contexts.current_context(self.w, self.profile, self.snapshot)

    def test_workspace_discovery_rejects_retained_metadata_hash_tampering(self):
        _, receipt, _ = self.ready()
        with self.w._connect(writable=True) as connection:
            value = json.loads(connection.execute("SELECT payload FROM _contextbase.customer360_materializations WHERE materialization_id=?", [receipt["materialization_id"]]).fetchone()[0])
            value["row_count"] += 1
            connection.execute("UPDATE _contextbase.customer360_materializations SET payload=? WHERE materialization_id=?", [json.dumps(value), receipt["materialization_id"]])
        with self.assertRaises(ValueError):
            onboarding.workspace_state(self.w)


if __name__ == "__main__":
    unittest.main()
