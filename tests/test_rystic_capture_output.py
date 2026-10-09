"""Exact local observation retention with source doubles; no runtime or model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from contextbase import rystic
from contextbase.refresh_state import begin_refresh, refresh_status
from contextbase.rystic import snapshot_info, sync_snapshot
from contextbase.rystic_recipe import NAMESPACE
from contextbase.rystic_validation import capture_validation
from contextbase.workspace import Workspace, canonical
from test_rystic import SnapshotPair


class RysticCaptureOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root / "workspace")
        self.pair = SnapshotPair()

    def read_rows(self):
        with self.workspace._connect() as conn:
            return {name: conn.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
                    for name in rystic.SCHEMAS}

    def assert_no_source_calls(self):
        self.assertEqual(self.pair.verifications, 0)
        self.assertEqual(self.pair.stripe.calls, [])
        self.assertEqual(self.pair.linear.calls, [])

    def test_exact_capture_is_private_and_complete_before_same_attempt_import(self):
        target = self.root / "captures" / "valid.json"
        captures, handles = [], []
        reserve, store = rystic._reserve_capture_output, rystic._store_snapshot

        def reserved(*args):
            handle = reserve(*args)
            handles.append(handle)
            return handle

        def start(*args):
            self.assert_no_source_calls()
            self.assertTrue(target.is_file())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_bytes(), b"")
            return begin_refresh(*args)

        def observe(*args):
            value = capture_validation(*args)
            captures.append(deepcopy(value))
            return value

        def materialize(*args, **kwargs):
            self.assertTrue(handles[0].closed)
            self.assertEqual(json.loads(target.read_text()), captures[0])
            self.assertEqual(kwargs["validation"], captures[0]["receipt"])
            return store(*args, **kwargs)

        with patch("contextbase.rystic._reserve_capture_output", side_effect=reserved), \
             patch("contextbase.refresh_state.begin_refresh", side_effect=start), \
             patch("contextbase.rystic_validation.capture_validation", side_effect=observe) as capture, \
             patch("contextbase.rystic._store_snapshot", side_effect=materialize):
            snapshot = sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        self.assertEqual(capture.call_count, 1)
        saved = json.loads(target.read_text())
        self.assertEqual(saved, captures[0])
        self.assertEqual(set(saved), {"receipt", "tables", "observed_source"})
        self.assertFalse(saved["receipt"]["workspace_imported"])
        self.assertEqual(snapshot["data_sha256"], saved["receipt"]["data_sha256"])
        self.assertEqual(snapshot["source_validation_sha256"],
                         hashlib.sha256(canonical(saved["receipt"]).encode()).hexdigest())
        status = refresh_status(self.workspace)
        self.assertEqual(status["latest_attempt"]["validation"], saved["receipt"])
        self.assertEqual(status["latest_attempt"]["materialized_snapshot"], snapshot)
        self.assertTrue(status["snapshot_reads_allowed"])

    def test_blocked_capture_retains_raw_metadata_and_preserves_blocked_history(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.read_rows()
        self.pair.customers[0]["metadata"]["account_id"] = "customer_001"
        target = self.root / "blocked.json"
        with self.assertRaisesRegex(ValueError, "inspect refresh_status"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        saved = json.loads(target.read_text())
        self.assertIsNone(saved["tables"])
        self.assertEqual(saved["receipt"]["validation_status"], "blocked")
        self.assertTrue(saved["receipt"]["complete"])
        self.assertFalse(saved["receipt"]["workspace_imported"])
        self.assertEqual({row["metadata"]["account_id"]
                          for row in saved["observed_source"]["rows"]["stripe.customers"]}, {"customer_001"})
        status = refresh_status(self.workspace)
        self.assertEqual(status["latest_attempt"]["outcome"], "refused_invalid_source")
        self.assertEqual(status["latest_attempt"]["validation"], saved["receipt"])
        self.assertEqual(status["last_successful_snapshot"], original)
        self.assertEqual(self.read_rows(), before)
        with self.assertRaisesRegex(ValueError, "latest required refresh"):
            self.workspace.query("SELECT * FROM stripe.customers")

    def test_unknown_source_failure_retains_unverified_capture_without_false_evidence(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.pair.fail_linear = True
        target = self.root / "unverified.json"
        with self.assertRaises(ValueError):
            sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        saved = json.loads(target.read_text())
        self.assertIsNone(saved["tables"])
        self.assertIsNone(saved["observed_source"])
        self.assertFalse(saved["receipt"]["complete"])
        self.assertFalse(saved["receipt"]["workspace_imported"])
        self.assertEqual(saved["receipt"]["validation_status"], "unverified")
        self.assertNotIn("data_sha256", saved["receipt"])
        self.assertNotIn("Synthetic source fetch failure", target.read_text())
        status = refresh_status(self.workspace)
        self.assertEqual(status["latest_attempt"]["outcome"], "unverified")
        self.assertEqual(status["latest_attempt"]["validation"], saved["receipt"])
        self.assertEqual(status["last_successful_snapshot"], original)
        self.assertFalse(status["snapshot_reads_allowed"])

    def test_existing_file_and_dangling_symlink_refuse_before_source_or_refresh(self):
        regular = self.root / "existing.json"
        regular.write_bytes(b"Existing authored contents")
        dangling = self.root / "dangling.json"
        destination = self.root / "absent.json"
        dangling.symlink_to(destination)
        for target in (regular, dangling):
            with self.subTest(target=target.name), \
                 patch("contextbase.refresh_state.begin_refresh", side_effect=AssertionError("No new attempt")), \
                 self.assertRaisesRegex(ValueError, "already exists"):
                sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        self.assertEqual(regular.read_bytes(), b"Existing authored contents")
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(destination.exists())
        self.assert_no_source_calls()
        self.assertFalse(self.workspace.root.exists())

    def test_database_and_wal_targets_refuse_before_reservation(self):
        for target in (self.workspace.db_path, Path(str(self.workspace.db_path) + ".wal")):
            with self.subTest(target=target.name), self.assertRaisesRegex(ValueError, "database or its WAL"):
                sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        self.assertFalse(self.workspace.root.exists())
        self.assert_no_source_calls()

    def test_capture_write_and_durability_failures_block_old_snapshot(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.read_rows()
        self.pair.charges[1]["amount_refunded"] += 1
        for step in ("json.dump", "os.fsync"):
            target = self.root / (step.replace(".", "-") + ".json")
            with self.subTest(step=step), \
                 patch("contextbase.rystic." + step, side_effect=OSError("Synthetic private failure")), \
                 self.assertRaisesRegex(ValueError, "capture could not be retained") as error:
                sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
            self.assertNotIn("Synthetic private failure", str(error.exception))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            status = refresh_status(self.workspace)
            self.assertEqual(status["latest_attempt"]["outcome"], "storage_failed")
            self.assertFalse(status["snapshot_reads_allowed"])
            self.assertEqual(snapshot_info(self.workspace), original)
            self.assertEqual(self.read_rows(), before)

    def test_import_failure_leaves_exact_capture_without_claiming_import_success(self):
        original = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.read_rows()
        target = self.root / "failed-import.json"
        self.pair.charges[1]["amount_refunded"] += 1
        with patch.object(self.workspace, "_metadata", side_effect=ValueError("Authored storage failure")), \
             self.assertRaisesRegex(ValueError, "Authored storage failure"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        saved = json.loads(target.read_text())
        self.assertEqual(saved["receipt"]["validation_status"], "valid")
        self.assertFalse(saved["receipt"]["workspace_imported"])
        status = refresh_status(self.workspace)
        self.assertEqual(status["latest_attempt"]["outcome"], "storage_failed")
        self.assertEqual(status["latest_attempt"]["validation"], saved["receipt"])
        self.assertEqual(snapshot_info(self.workspace), original)
        self.assertEqual(self.read_rows(), before)
        self.assertFalse(status["snapshot_reads_allowed"])

    def test_reserved_handle_closes_if_refresh_cannot_begin(self):
        target = self.root / "never-started.json"
        handles = []
        reserve = rystic._reserve_capture_output

        def reserved(*args):
            handle = reserve(*args)
            handles.append(handle)
            return handle

        with patch("contextbase.rystic._reserve_capture_output", side_effect=reserved), \
             patch("contextbase.refresh_state.begin_refresh", side_effect=ValueError("Authored begin failure")), \
             self.assertRaisesRegex(ValueError, "Authored begin failure"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE, capture_output=target)
        self.assertTrue(handles[0].closed)
        self.assertEqual(target.read_bytes(), b"")
        self.assertFalse(self.workspace.db_path.exists())
        self.assert_no_source_calls()
