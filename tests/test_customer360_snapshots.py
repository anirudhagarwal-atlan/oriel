"""Synthetic local CSV/service checks, not live/native vendor validation."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.connectors.evidence import observe_table
from contextbase.customer360 import snapshots
from contextbase.workspace import Workspace


class Customer360SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.now = datetime.now(timezone.utc)
        self.clock = patch.object(snapshots, "_now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def source(self, name="crm", *, when=None):
        when = when or self.now - timedelta(seconds=10)
        path = self.root / (name + ".csv")
        path.write_text("account_id,seats\naccount_001,3\naccount_002,7\n")
        with patch.object(service, "_now", return_value=when.isoformat()):
            service.add(self.workspace, "csv", name=name, path=path)
            service.sync(self.workspace, name, bootstrap_context=False)
        return path

    def committed(self, name="crm"):
        with self.workspace._connect() as conn:
            return json.loads(conn.execute("SELECT payload FROM _contextbase.connector_receipts WHERE name=?", [name]).fetchone()[0])

    def change_receipt(self, mutate, name="crm"):
        receipt = self.committed(name)
        mutate(receipt)
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.connector_receipts SET payload=? WHERE name=?", [json.dumps(receipt), name])

    def test_capture_deduplicates_unchanged_publications_and_records_scope(self):
        self.source("product")
        self.source("crm")
        manifest = snapshots.capture_snapshot(self.workspace, ["product", "crm"])
        again = snapshots.capture_snapshot(self.workspace, ["crm", "product"])
        self.assertEqual(manifest, again)
        self.assertEqual(snapshots.load_snapshot(self.workspace, manifest["snapshot_id"]), manifest)
        self.assertEqual(snapshots.assert_snapshot_current(self.workspace, manifest["snapshot_id"]), manifest)
        self.assertEqual(manifest["coverage"]["connections"], ["crm", "product"])
        self.assertEqual(manifest["coverage"]["total_rows"], 4)
        self.assertEqual(manifest["coverage"]["upstream_completeness"], "not_verified")
        self.assertEqual(manifest["freshness"]["age_basis"], "oldest_extraction_started_at")
        table = manifest["sources"][0]["tables"][0]
        self.assertEqual(table["table"], "crm.records")
        self.assertEqual(table["rows"], 2)
        self.assertEqual(table, self.committed()["table_evidence"][0])
        self.assertNotIn("account_001", json.dumps(manifest))
        with self.workspace._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM _contextbase.customer360_snapshots").fetchone()[0], 1)

    def test_same_count_edit_before_first_capture_is_refused(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE crm.records SET seats=99 WHERE account_id='account_001'")
        with self.assertRaisesRegex(ValueError, "content or schema changed"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_content_drift_after_capture_refuses_current_but_keeps_history(self):
        self.source()
        manifest = snapshots.capture_snapshot(self.workspace, ["crm"])
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE crm.records SET seats=99 WHERE account_id='account_001'")
        with self.assertRaisesRegex(ValueError, "content or schema changed"):
            snapshots.assert_snapshot_current(self.workspace, manifest["snapshot_id"])
        self.assertEqual(snapshots.load_snapshot(self.workspace, manifest["snapshot_id"]), manifest)

    def test_schema_change_is_refused(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("ALTER TABLE crm.records ADD COLUMN extra BOOLEAN")
        with self.assertRaisesRegex(ValueError, "content or schema changed"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_new_unchanged_sync_has_new_publication_identity(self):
        self.source()
        original = snapshots.capture_snapshot(self.workspace, ["crm"])
        with patch.object(service, "_now", return_value=(self.now - timedelta(seconds=1)).isoformat()):
            service.sync(self.workspace, "crm", bootstrap_context=False)
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            snapshots.assert_snapshot_current(self.workspace, original["snapshot_id"])
        refreshed = snapshots.capture_snapshot(self.workspace, ["crm"])
        self.assertNotEqual(original["snapshot_id"], refreshed["snapshot_id"])
        self.assertEqual(original["sources"][0]["tables"], refreshed["sources"][0]["tables"])

    def test_failed_latest_attempt_cannot_reuse_old_publication(self):
        self.source()
        original = snapshots.capture_snapshot(self.workspace, ["crm"])
        with patch.object(Workspace, "ingest", side_effect=ValueError("synthetic failure")):
            with self.assertRaises(ValueError):
                service.sync(self.workspace, "crm", bootstrap_context=False)
        self.assertEqual(self.workspace.query("SELECT SUM(seats) AS seats FROM crm.records")["rows"], [{"seats": 10}])
        for call in (lambda: snapshots.capture_snapshot(self.workspace, ["crm"]),
                     lambda: snapshots.assert_snapshot_current(self.workspace, original["snapshot_id"])):
            with self.assertRaisesRegex(ValueError, "failed latest sync"):
                call()

    def test_reconfigured_unpublished_source_is_refused(self):
        original_file = self.source()
        original = snapshots.capture_snapshot(self.workspace, ["crm"])
        replacement = self.root / "replacement.csv"
        replacement.write_text(original_file.read_text())
        service.add(self.workspace, "csv", name="crm", path=replacement, replace_config=True)
        with self.assertRaisesRegex(ValueError, "reconfigured"):
            snapshots.capture_snapshot(self.workspace, ["crm"])
        with self.assertRaisesRegex(ValueError, "reconfigured"):
            snapshots.assert_snapshot_current(self.workspace, original["snapshot_id"])

    def test_stale_source_and_stored_freshness_limit(self):
        self.source(when=self.now - timedelta(seconds=100))
        with self.assertRaisesRegex(ValueError, "stale"):
            snapshots.capture_snapshot(self.workspace, ["crm"], max_age_seconds=50)
        manifest = snapshots.capture_snapshot(self.workspace, ["crm"], max_age_seconds=150)
        with patch.object(snapshots, "_now", return_value=self.now + timedelta(seconds=60)):
            with self.assertRaisesRegex(ValueError, "stale"):
                snapshots.assert_snapshot_current(self.workspace, manifest["snapshot_id"])
            self.assertEqual(snapshots.load_snapshot(self.workspace, manifest["snapshot_id"]), manifest)

    def test_cross_source_publication_skew_is_explicit_and_enforced(self):
        self.source("crm", when=self.now - timedelta(seconds=200))
        self.source("product", when=self.now - timedelta(seconds=10))
        with self.assertRaisesRegex(ValueError, "skew"):
            snapshots.capture_snapshot(self.workspace, ["crm", "product"], max_skew_seconds=100)
        manifest = snapshots.capture_snapshot(self.workspace, ["crm", "product"], max_skew_seconds=200)
        self.assertEqual(manifest["freshness"]["publication_skew_seconds"], 190)

    def test_future_and_naive_source_timestamps_are_refused(self):
        self.source(when=self.now + timedelta(seconds=30))
        with self.assertRaisesRegex(ValueError, "future"):
            snapshots.capture_snapshot(self.workspace, ["crm"])
        self.change_receipt(lambda value: value.update(finished_at="2026-10-09T12:00:00"))
        with self.assertRaisesRegex(ValueError, "timezone-bearing"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_receipt_hash_and_row_count_tampering_are_refused(self):
        self.source()
        self.change_receipt(lambda value: value["tables"][0].update(rows=999))
        with self.assertRaisesRegex(ValueError, "counts and evidence"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_missing_logical_evidence_requires_explicit_refresh(self):
        self.source()
        self.change_receipt(lambda value: value.pop("table_evidence"))
        with self.assertRaisesRegex(ValueError, "refresh the source"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_receipt_and_status_disagreement_is_refused(self):
        self.source()
        self.change_receipt(lambda value: value.update(snapshot_sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "status and committed"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_import_provenance_tampering_is_refused(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.imports SET sha256=? WHERE source='connector:crm'", ["0" * 64])
        with self.assertRaisesRegex(ValueError, "import provenance"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_extra_missing_and_view_tables_refuse_ownership(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.extra AS SELECT 1 AS value")
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            snapshots.capture_snapshot(self.workspace, ["crm"])
        with self.workspace._connect(writable=True) as conn:
            conn.execute("DROP TABLE crm.extra")
            conn.execute("DROP TABLE crm.records")
            conn.execute("CREATE VIEW crm.records AS SELECT 1 AS value")
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            snapshots.capture_snapshot(self.workspace, ["crm"])

    def test_same_attempt_bookkeeping_failure_uses_atomic_database_receipt(self):
        path = self.root / "crm.csv"
        path.write_text("account_id,seats\naccount_001,3\n")
        service.add(self.workspace, "csv", name="crm", path=path)
        original_write = service._write
        def fail_after_publish(path, value, **kwargs):
            if path.name == "receipt.json":
                raise OSError("synthetic bookkeeping failure")
            return original_write(path, value, **kwargs)
        with patch.object(service, "_write", side_effect=fail_after_publish):
            result = service.sync(self.workspace, "crm", bootstrap_context=False)
        self.assertIn("database_receipt_committed", result["bookkeeping"])
        with patch.object(snapshots, "_now", return_value=datetime.now(timezone.utc)):
            self.assertEqual(snapshots.capture_snapshot(self.workspace, ["crm"])["coverage"]["total_rows"], 1)

    def test_running_sync_lock_refuses_capture_and_releases_other_locks(self):
        self.source("crm")
        self.source("product")
        with service._lock(self.workspace.root / "connectors/product"):
            with self.assertRaisesRegex(ValueError, "sync is running"):
                snapshots.capture_snapshot(self.workspace, ["product", "crm"])
            self.assertFalse((self.workspace.root / "connectors/crm/sync.lock").exists())

    def test_missing_and_invalid_selection_does_not_create_state(self):
        for names in ([], ["../crm"], ["crm", "crm"], "crm", [True]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                snapshots.capture_snapshot(self.workspace, names)
        with self.assertRaises(ValueError):
            snapshots.capture_snapshot(self.workspace, ["crm"])
        self.assertFalse(self.workspace.root.exists())

    def test_invalid_freshness_limits_fail_before_capture(self):
        for value in (None, True, -1, float("inf"), float("nan"), "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshots.capture_snapshot(self.workspace, ["crm"], max_age_seconds=value)
        with self.assertRaises(ValueError):
            snapshots.capture_snapshot(self.workspace, ["crm"], max_age_seconds=0)
        self.assertFalse(self.workspace.root.exists())

    def test_manifest_tamper_is_detected_and_never_overwritten(self):
        self.source()
        manifest = snapshots.capture_snapshot(self.workspace, ["crm"])
        corrupted = dict(manifest, observed_at=(self.now - timedelta(seconds=1)).isoformat())
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.customer360_snapshots SET payload=?", [json.dumps(corrupted)])
        for call in (lambda: snapshots.load_snapshot(self.workspace, manifest["snapshot_id"]),
                     lambda: snapshots.capture_snapshot(self.workspace, ["crm"])):
            with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
                call()
        with self.workspace._connect() as conn:
            self.assertEqual(json.loads(conn.execute("SELECT payload FROM _contextbase.customer360_snapshots").fetchone()[0]), corrupted)

    def test_readable_snapshot_materializes_and_rolls_back_atomically(self):
        self.source()
        manifest = snapshots.capture_snapshot(self.workspace, ["crm"])
        with snapshots.readable_snapshot(self.workspace, manifest["snapshot_id"]) as (conn, observed):
            self.assertEqual(observed, manifest)
            conn.execute("CREATE SCHEMA customer360")
            conn.execute("CREATE TABLE customer360.accounts AS SELECT * FROM crm.records")
            with self.assertRaisesRegex(ValueError, "sync is running"):
                service.sync(self.workspace, "crm", bootstrap_context=False)
        self.assertEqual(self.workspace.query("SELECT COUNT(*) AS n FROM customer360.accounts")["rows"], [{"n": 2}])
        with self.assertRaisesRegex(ValueError, "synthetic consumer failure"):
            with snapshots.readable_snapshot(self.workspace, manifest["snapshot_id"]) as (conn, _):
                conn.execute("CREATE TABLE customer360.aborted AS SELECT 1 AS value")
                raise ValueError("synthetic consumer failure")
        self.assertFalse((self.workspace.root / "connectors/crm/sync.lock").exists())
        with self.workspace._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='customer360' AND table_name='aborted'").fetchone()[0], 0)

    def test_readable_snapshot_refuses_drift_before_yield(self):
        self.source()
        manifest = snapshots.capture_snapshot(self.workspace, ["crm"])
        with self.workspace._connect(writable=True) as conn:
            conn.execute("DELETE FROM crm.records")
        yielded = False
        with self.assertRaisesRegex(ValueError, "content or schema changed"):
            with snapshots.readable_snapshot(self.workspace, manifest["snapshot_id"]):
                yielded = True
        self.assertFalse(yielded)

    def test_table_evidence_is_order_independent_lossless_and_type_aware(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.typed (label VARCHAR COLLATE NOCASE, amount DECIMAL(30,10), moment TIMESTAMPTZ, flags BOOLEAN[], payload BLOB)")
            conn.execute("INSERT INTO crm.typed VALUES ('a', 12345678901234567890.1234567890, TIMESTAMPTZ '2026-01-01 00:00:00+00', [true,false], 'bytes'), ('A',0.0000000001,NULL,[],NULL)")
            original = observe_table(conn, "crm", "typed")
            conn.execute("CREATE TABLE crm.reordered AS SELECT * FROM crm.typed ORDER BY label COLLATE \"binary\" DESC")
            reordered = observe_table(conn, "crm", "reordered")
            self.assertEqual(original["content_sha256"], reordered["content_sha256"])
            conn.execute("UPDATE crm.typed SET amount=amount+0.0000000001 WHERE label COLLATE \"binary\"='a'")
            self.assertNotEqual(original["content_sha256"], observe_table(conn, "crm", "typed")["content_sha256"])
            conn.execute("CREATE TABLE crm.as_text AS SELECT CAST(amount AS VARCHAR) AS amount FROM crm.typed")
            self.assertNotEqual(original["schema_sha256"], observe_table(conn, "crm", "as_text")["schema_sha256"])

    def test_table_evidence_preserves_duplicate_counts_and_rejects_nonfinite(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            original = observe_table(conn, "crm", "records")
            conn.execute("INSERT INTO crm.records SELECT * FROM crm.records")
            doubled = observe_table(conn, "crm", "records")
            self.assertEqual(doubled["rows"], original["rows"] * 2)
            self.assertNotEqual(doubled["content_sha256"], original["content_sha256"])
            conn.execute("CREATE TABLE crm.invalid AS SELECT 'NaN'::DOUBLE AS value")
            with self.assertRaisesRegex(ValueError, "Nonfinite"):
                observe_table(conn, "crm", "invalid")

    def test_nanosecond_and_interval_representation_edits_change_evidence(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.precise AS SELECT TIMESTAMP_NS '2026-01-01 00:00:00.123456001' AS moment")
            before = observe_table(conn, "crm", "precise")
            conn.execute("UPDATE crm.precise SET moment=TIMESTAMP_NS '2026-01-01 00:00:00.123456002'")
            self.assertNotEqual(before["content_sha256"], observe_table(conn, "crm", "precise")["content_sha256"])
            conn.execute("CREATE TABLE crm.duration AS SELECT INTERVAL '1 month' AS duration")
            before = observe_table(conn, "crm", "duration")
            # DuckDB elides UPDATE for values equal under interval comparison;
            # replace the row to actually change its retained representation.
            conn.execute("DELETE FROM crm.duration")
            conn.execute("INSERT INTO crm.duration VALUES (INTERVAL '30 days')")
            self.assertNotEqual(before["content_sha256"], observe_table(conn, "crm", "duration")["content_sha256"])

    def test_union_and_nested_nanoseconds_fail_without_incomplete_hashes(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.union_value AS SELECT union_value(a := 1) AS value")
            conn.execute("CREATE TABLE crm.nested_precision AS SELECT [TIMESTAMP_NS '2026-01-01 00:00:00.123456001'] AS value")
            for table in ("union_value", "nested_precision"):
                with self.subTest(table=table), self.assertRaisesRegex(ValueError, "Unsupported precision or union"):
                    observe_table(conn, "crm", table)

    def test_signed_zero_and_map_entry_order_are_not_silently_lost(self):
        self.source()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.floats AS SELECT '-0.0'::DOUBLE AS value")
            before = observe_table(conn, "crm", "floats")
            conn.execute("DELETE FROM crm.floats")
            conn.execute("INSERT INTO crm.floats VALUES ('0.0'::DOUBLE)")
            self.assertNotEqual(before["content_sha256"], observe_table(conn, "crm", "floats")["content_sha256"])
            conn.execute("CREATE TABLE crm.maps AS SELECT map(['a','b'], [1,2]) AS value")
            before = observe_table(conn, "crm", "maps")
            conn.execute("DELETE FROM crm.maps")
            conn.execute("INSERT INTO crm.maps VALUES (map(['b','a'], [2,1]))")
            self.assertNotEqual(before["content_sha256"], observe_table(conn, "crm", "maps")["content_sha256"])


if __name__ == "__main__":
    unittest.main()
