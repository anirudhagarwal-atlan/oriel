"""Durable refusal/recovery using schema-faithful authored source doubles."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.cli import execute, parser
from contextbase.refresh_state import begin_refresh, finish_failed_refresh, refresh_status, _finish
from contextbase.rystic import snapshot_info, sync_snapshot, load_example, _store_snapshot
from contextbase.rystic_recipe import NAMESPACE
from contextbase.rystic_validation import capture_validation
from contextbase.workspace import Workspace
from test_rystic import SnapshotPair


class RefreshStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(Path(temporary.name) / "workspace")
        self.pair = SnapshotPair()

    def rows(self):
        with self.workspace._connect() as conn:
            return {table: conn.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                    for table in ("stripe.customers", "stripe.charges", "linear.issues")}

    def test_untracked_status_is_local_and_creates_nothing(self):
        status = refresh_status(self.workspace)
        self.assertEqual(status["status"], "untracked")
        self.assertTrue(status["snapshot_reads_allowed"])
        self.assertFalse(self.workspace.root.exists())
        output = execute(parser().parse_args(["--workspace", str(self.workspace.root), "refresh-status"]))
        self.assertEqual(output, status)
        self.assertFalse(self.workspace.root.exists())

    def test_complete_refusal_preserves_rows_blocks_reads_and_recovers(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.rows()
        self.pair.customers[0]["metadata"]["account_id"] = "customer_001"
        with self.assertRaisesRegex(ValueError, "inspect refresh_status"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE)
        status = refresh_status(Workspace(self.workspace.root))
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["last_successful_snapshot"], original)
        self.assertEqual(status["latest_attempt"]["outcome"], "refused_invalid_source")
        validation = status["latest_attempt"]["validation"]
        self.assertTrue(validation["complete"])
        self.assertFalse(validation["workspace_imported"])
        self.assertEqual(validation["hash_kind"], "observed_cohort_projection/v1")
        self.assertEqual({blocker["record_id"] for blocker in validation["blockers"]}, {"cus_001", "cus_002"})
        for read in (lambda: self.workspace.query("SELECT * FROM stripe.customers"), self.workspace.catalog,
                     lambda: self.workspace.transform("old_counts", "SELECT count(*) AS n FROM stripe.customers"),
                     lambda: self.workspace.plan_change("stripe.customers", "customer_id", "cus_001", {"name": "Synthetic change"})):
            with self.assertRaisesRegex(ValueError, "latest required refresh"): read()
        self.assertEqual(self.rows(), before)
        self.pair.customers[0]["metadata"]["account_id"] = "customer_002"
        self.pair.charges[1]["amount_refunded"] += 1
        fresh = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        status = refresh_status(self.workspace)
        self.assertEqual(status["status"], "recorded_snapshot")
        self.assertTrue(status["snapshot_reads_allowed"])
        self.assertNotEqual(fresh["data_sha256"], original["data_sha256"])
        self.assertEqual(status["latest_attempt"]["materialized_snapshot"], fresh)
        self.assertEqual(self.workspace.query("SELECT amount_refunded_cents FROM stripe.charges WHERE charge_id='ch_001'")["rows"], [{"amount_refunded_cents": 3334}])

    def test_transport_failure_is_unverified_without_invented_blockers(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        del self.pair.customers[0]["metadata"]["is_internal"]
        self.pair.fail_linear = True
        with self.assertRaises(ValueError): sync_snapshot(self.workspace, self.pair, NAMESPACE)
        status = refresh_status(self.workspace)
        evidence = status["latest_attempt"]["validation"]
        self.assertEqual(status["latest_attempt"]["outcome"], "unverified")
        self.assertFalse(evidence["complete"])
        self.assertNotIn("data_sha256", evidence)
        self.assertEqual(evidence["blockers"], [])
        self.assertEqual(status["last_successful_snapshot"], original)
        self.assertNotIn("Synthetic source fetch failure", json.dumps(status))

    def test_interrupted_attempt_and_superseded_completion_cannot_expose_old_rows(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.rows()
        first = begin_refresh(self.workspace, NAMESPACE)
        self.assertFalse(refresh_status(Workspace(self.workspace.root))["snapshot_reads_allowed"])
        observed = capture_validation(self.pair, NAMESPACE)
        second = begin_refresh(self.workspace, NAMESPACE)
        receipt = {**snapshot_info(self.workspace), "observed_at": observed["receipt"]["observed_at"]}
        with self.assertRaisesRegex(ValueError, "superseded"):
            _store_snapshot(self.workspace, observed["tables"], receipt,
                            refresh_attempt_id=first["attempt_id"], validation=observed["receipt"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(refresh_status(self.workspace)["latest_attempt"]["attempt_id"], second["attempt_id"])
        self.assertEqual(refresh_status(self.workspace)["latest_attempt"]["outcome"], "in_progress")
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertTrue(refresh_status(self.workspace)["snapshot_reads_allowed"])

    def test_storage_failure_rolls_back_snapshot_but_persists_blocking_attempt(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.rows()
        self.pair.charges[1]["amount_refunded"] += 1
        with patch.object(self.workspace, "_metadata", side_effect=ValueError("Authored storage failure")):
            with self.assertRaisesRegex(ValueError, "Authored storage failure"):
                sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertEqual(self.rows(), before)
        self.assertEqual(snapshot_info(self.workspace), original)
        self.assertEqual(refresh_status(self.workspace)["latest_attempt"]["outcome"], "storage_failed")
        with self.assertRaises(ValueError): self.workspace.query("SELECT 1 AS answer")

    def test_loading_a_capture_cannot_clear_a_required_refresh_failure(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        attempt = begin_refresh(self.workspace, NAMESPACE)
        finish_failed_refresh(self.workspace, attempt["attempt_id"], outcome="unverified")
        load_example(self.workspace)
        self.assertEqual(refresh_status(self.workspace)["status"], "blocked")
        with self.assertRaises(ValueError): self.workspace.query("SELECT count(*) FROM stripe.customers")

    def test_previously_reviewed_change_cannot_apply_after_refresh_failure(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        change = self.workspace.plan_change("stripe.customers", "customer_id", "cus_001", {"name": "Example updated"})
        before = self.rows()
        begin_refresh(self.workspace, NAMESPACE)
        with self.assertRaisesRegex(ValueError, "latest required refresh"):
            self.workspace.apply_change(change["plan_id"], change["approval_sha256"])
        self.assertEqual(self.rows(), before)

    def test_receipt_corruption_does_not_fall_back_to_untracked(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        with self.workspace._connect(writable=True) as conn:
            row = conn.execute("SELECT sequence,payload FROM _contextbase.refresh_attempts ORDER BY sequence DESC LIMIT 1").fetchone()
            receipt = json.loads(row[1]); receipt["outcome"] = "in_progress"
            conn.execute("UPDATE _contextbase.refresh_attempts SET payload=? WHERE sequence=?", [json.dumps(receipt), row[0]])
        with self.assertRaisesRegex(ValueError, "metadata changed"):
            self.workspace.query("SELECT 1 AS answer")
