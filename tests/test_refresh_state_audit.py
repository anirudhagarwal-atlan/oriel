"""Regression checks for receipt mismatch and accidental journal corruption.

These tests use authored source doubles. They do not establish authentication
against an operator with arbitrary database access or native simulator fidelity.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from contextbase.refresh_state import begin_refresh, finish_failed_refresh, refresh_status
from contextbase.rystic import snapshot_info, sync_snapshot
from contextbase.rystic_recipe import NAMESPACE
from contextbase.rystic_validation import capture_validation
from contextbase.workspace import Workspace
from test_rystic import SnapshotPair


class RefreshStateAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="contextbase-refresh-audit-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(Path(temporary.name) / "workspace")
        self.pair = SnapshotPair()
        self.original = sync_snapshot(self.workspace, self.pair, NAMESPACE)

    def fail_refresh(self):
        attempt = begin_refresh(self.workspace, NAMESPACE)
        finish_failed_refresh(self.workspace, attempt["attempt_id"], outcome="unverified")
        return attempt

    def assert_blocked(self):
        with self.assertRaises(ValueError):
            self.workspace.query("SELECT count(*) FROM stripe.customers")

    def test_modified_success_metadata_cannot_match_only_by_content_id(self):
        changes = [
            {"observed_at": "2099-01-01T00:00:00Z"},
            {"refresh_mode": "bundled_capture"},
            {"simulators": {"stripe": {"version": "unsupported_synthetic_version"}}},
            {"consistency": "invented transactional snapshot"},
        ]
        with self.workspace._connect() as conn:
            sequence, original_payload = conn.execute("SELECT sequence,payload FROM _contextbase.rystic_snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
        for change in changes:
            with self.subTest(change=change):
                altered = json.loads(original_payload)
                altered.update(change)
                with self.workspace._connect(writable=True) as conn:
                    conn.execute("UPDATE _contextbase.rystic_snapshots SET payload=? WHERE sequence=?", [json.dumps(altered), sequence])
                self.assert_blocked()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.rystic_snapshots SET payload=? WHERE sequence=?", [original_payload, sequence])
        self.assertTrue(refresh_status(self.workspace)["snapshot_reads_allowed"])

    def test_demoting_a_failed_attempt_does_not_resurrect_an_older_success(self):
        failed = self.fail_refresh()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.refresh_attempts SET sequence=0 WHERE attempt_id=?", [failed["attempt_id"]])
        self.assert_blocked()

    def test_promoting_an_older_attempt_is_not_a_valid_recovery(self):
        self.fail_refresh()
        with self.workspace._connect(writable=True) as conn:
            earliest = conn.execute("SELECT attempt_id FROM _contextbase.refresh_attempts ORDER BY sequence LIMIT 1").fetchone()[0]
            conn.execute("UPDATE _contextbase.refresh_attempts SET sequence=999 WHERE attempt_id=?", [earliest])
        self.assert_blocked()

    def test_sequence_collision_cannot_create_two_competing_latest_attempts(self):
        failed = self.fail_refresh()
        with self.workspace._connect(writable=True) as conn:
            sequence = conn.execute("SELECT sequence FROM _contextbase.refresh_attempts WHERE attempt_id=?", [failed["attempt_id"]]).fetchone()[0]
            earlier = conn.execute("SELECT attempt_id FROM _contextbase.refresh_attempts WHERE attempt_id<>?", [failed["attempt_id"]]).fetchone()[0]
            with self.assertRaises(duckdb.Error):
                conn.execute("UPDATE _contextbase.refresh_attempts SET sequence=? WHERE attempt_id=?", [sequence, earlier])
        self.assert_blocked()

    def test_mismatched_valid_observation_cannot_commit_or_clear_blocked_reads(self):
        observed = capture_validation(self.pair, NAMESPACE)
        self.assertEqual(observed["receipt"]["validation_status"], "valid")
        altered = deepcopy(observed)
        altered["tables"]["stripe.charges"][0]["amount_cents"] += 1
        with patch("contextbase.rystic_validation.capture_validation", return_value=altered):
            with self.assertRaisesRegex(ValueError, "do not match"):
                sync_snapshot(self.workspace, self.pair, NAMESPACE)
        status = refresh_status(self.workspace)
        self.assertEqual(status["latest_attempt"]["outcome"], "storage_failed")
        self.assertEqual(snapshot_info(self.workspace), self.original)
        self.assertEqual(status["last_successful_snapshot"], self.original)
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
