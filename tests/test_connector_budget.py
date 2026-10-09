"""Local-only request budget checks; no connector or network calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from contextbase.connectors import budget


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class ConnectorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # macOS's standard temporary path traverses a system /var symlink.
        # Use the real path; production ledger paths reject symlink traversal.
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.path = self.directory / "request-budget.json"

    def tearDown(self):
        self.temporary.cleanup()

    def make(self, requests=3, seconds=60):
        return budget.ExtractionBudget(self.path, {"max_requests": requests, "max_seconds": seconds})

    def rewrite(self, value):
        value.pop("ledger_sha256", None)
        value["ledger_sha256"] = hashlib.sha256(budget._canonical(value)).hexdigest()
        self.path.write_bytes(budget._canonical(value))

    def test_exact_positive_integer_limits_no_coercion_or_unknown_fields(self):
        good = {"max_requests": 100_000, "max_seconds": 3600}
        self.assertEqual(budget.validate_limits(good), good)
        self.assertIsNot(budget.validate_limits(good), good)
        invalid = [None, [], {}, {**good, "url": "synthetic"}]
        for field in good:
            for value in (True, False, 0, -1, 1.0, "1", None, good[field] + 1):
                invalid.append({**good, field: value})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(budget.BudgetError):
                budget.validate_limits(value)
        self.assertFalse(self.path.exists())

    def test_reservation_is_private_durable_before_return_and_summary_detached(self):
        instance = self.make()
        reservation = instance.reserve_request()
        self.assertEqual(reservation["request_id"], 1)
        self.assertGreater(reservation["timeout_seconds"], 0)
        self.assertLessEqual(reservation["timeout_seconds"], 60)
        stored = budget.read_budget(self.path)
        self.assertEqual(stored["counts"]["pending"], 1)
        self.assertEqual(stored["counts"]["reserved"], 1)
        self.assertEqual(stored["requests"][0]["outcome"], "pending")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        stored["requests"][0]["outcome"] = "succeeded"
        self.assertEqual(instance.summary()["counts"]["pending"], 1)
        self.assertEqual(budget.read_budget(self.path)["counts"]["pending"], 1)

    def test_success_finish_is_sealed_and_idempotent(self):
        instance = self.make()
        item = instance.reserve_request()
        completed = instance.complete_request(item["request_id"], status_code=200)
        self.assertEqual(completed["status"], "active")
        finished = instance.finish()
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["counts"]["succeeded"], 1)
        self.assertEqual(finished["counts"]["pending"], 0)
        self.assertEqual(instance.finish(), finished)
        self.assertEqual(budget.read_budget(self.path), finished)
        with self.assertRaises(budget.BudgetExceeded):
            instance.reserve_request()
        with self.assertRaises(budget.BudgetError):
            instance.fail()

    def test_compact_summary_validates_full_ledger_retains_identity_and_detaches(self):
        instance = self.make()
        reservation = instance.reserve_request()
        instance.complete_request(reservation["request_id"], status_code=200)
        full = instance.finish()
        compact = budget.compact_summary(full)
        self.assertNotIn("requests", compact)
        self.assertEqual(compact["requests_file"], "request-budget.json")
        self.assertEqual(compact["ledger_sha256"], full["ledger_sha256"])
        self.assertEqual(compact["counts"], full["counts"])
        self.assertEqual(set(compact), (set(full) - {"requests"}) | {"requests_file"})
        compact["counts"]["succeeded"] = 99
        self.assertEqual(full["counts"]["succeeded"], 1)
        self.assertEqual(budget.read_budget(self.path)["requests"], full["requests"])
        with self.assertRaises(budget.BudgetError):
            budget.compact_summary(compact)
        full["requests"][0]["status_code"] = 201
        with self.assertRaisesRegex(budget.BudgetError, "hash_mismatch"):
            budget.compact_summary(full)

    def test_no_resume_or_overwrite_of_existing_ledger(self):
        self.make().reserve_request()
        before = self.path.read_bytes()
        with self.assertRaises(budget.BudgetStorageError):
            self.make()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(budget.read_budget(self.path)["counts"]["pending"], 1)

    def test_killed_worker_retains_durable_pending_without_inventing_failure(self):
        code = ("import sys,time; from contextbase.connectors.budget import ExtractionBudget; "
                "b=ExtractionBudget(sys.argv[1],{'max_requests':2,'max_seconds':60}); "
                "b.reserve_request(); print('reserved',flush=True); time.sleep(60)")
        process = subprocess.Popen([sys.executable, "-c", code, str(self.path)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "reserved")
            process.kill()
            process.communicate(timeout=5)
            report = budget.read_budget(self.path)
            self.assertEqual(report["status"], "active")
            self.assertEqual(report["counts"]["reserved"], 1)
            self.assertEqual(report["counts"]["pending"], 1)
            self.assertEqual(report["counts"]["failed"], 0)
            self.assertIsNone(report["requests"][0]["completed_elapsed_seconds"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_request_exhaustion_counts_attempts_and_preserves_first_stop(self):
        instance = self.make(requests=1)
        item = instance.reserve_request()
        instance.complete_request(item["request_id"], status_code=204)
        with self.assertRaisesRegex(budget.BudgetExceeded, "request_limit"):
            instance.reserve_request()
        report = instance.fail("worker_failed")
        self.assertEqual(report["stop_reason"], "request_limit")
        self.assertTrue(report["limits_exceeded"]["requests"])
        self.assertEqual(report["counts"]["reserved"], 1)
        self.assertEqual(instance.finish()["status"], "stopped")

    def test_parallel_reservations_never_exceed_shared_count(self):
        instance = self.make(requests=7)
        barrier = threading.Barrier(12)

        def attempt(_):
            barrier.wait(timeout=5)
            try:
                result = instance.reserve_request()
            except budget.BudgetExceeded:
                return None
            instance.complete_request(result["request_id"], status_code=200)
            return result["request_id"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(12)))
        self.assertEqual(sorted(value for value in results if value is not None), list(range(1, 8)))
        stored = budget.read_budget(self.path)
        self.assertEqual(stored["counts"]["reserved"], 7)
        self.assertEqual(stored["counts"]["succeeded"], 7)
        self.assertEqual(stored["stop_reason"], "request_limit")

    def test_failed_attempt_stops_and_is_not_retried(self):
        instance = self.make()
        item = instance.reserve_request()
        report = instance.complete_request(item["request_id"], outcome="failed", status_code=503)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["stop_reason"], "request_failed")
        with self.assertRaises(budget.BudgetExceeded):
            instance.reserve_request()
        with self.assertRaises(budget.BudgetError):
            instance.complete_request(item["request_id"])

    def test_timeout_and_unknown_are_terminal_without_invented_response(self):
        for outcome in ("timeout", "unknown"):
            with self.subTest(outcome=outcome):
                self.path = self.directory / (outcome + ".json")
                instance = self.make()
                item = instance.reserve_request()
                report = instance.complete_request(item["request_id"], outcome=outcome)
                self.assertEqual(report["counts"][outcome], 1)
                self.assertIsNone(report["requests"][0]["status_code"])
                self.assertEqual(report["stop_reason"], "request_" + outcome)
                with self.assertRaises(budget.BudgetExceeded):
                    instance.remaining_seconds()

    def test_interruption_preserves_pending_and_finish_cannot_claim_success(self):
        instance = self.make()
        instance.reserve_request()
        report = instance.fail("interrupted")
        self.assertEqual(report["counts"]["pending"], 1)
        self.assertEqual(report["counts"]["unknown"], 0)
        self.assertIsNone(report["requests"][0]["completed_elapsed_seconds"])
        self.assertEqual(instance.finish()["status"], "stopped")
        self.assertEqual(budget.read_budget(self.path)["counts"]["pending"], 1)

    def test_finish_with_pending_is_terminal_and_late_completion_stays_stopped(self):
        instance = self.make()
        item = instance.reserve_request()
        self.assertEqual(instance.finish()["stop_reason"], "pending_requests")
        report = instance.complete_request(item["request_id"], status_code=200)
        self.assertEqual(report["counts"]["succeeded"], 1)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "pending_requests")

    def test_shared_deadline_does_not_reset_and_late_response_is_recorded(self):
        clock = Clock()
        with patch.object(budget.time, "monotonic", clock):
            instance = self.make(seconds=2)
            first = instance.reserve_request()
            clock.value += 1.5
            self.assertEqual(instance.remaining_seconds(), 0.5)
            instance.complete_request(first["request_id"], status_code=200)
            second = instance.reserve_request()
            self.assertEqual(second["timeout_seconds"], 0.5)
            clock.value += 0.5
            report = instance.complete_request(second["request_id"], status_code=200)
            self.assertEqual(report["counts"]["succeeded"], 2)
            self.assertEqual(report["status"], "stopped")
            self.assertEqual(report["stop_reason"], "deadline")
            self.assertTrue(report["limits_exceeded"]["seconds"])
            with self.assertRaises(budget.BudgetExceeded):
                instance.reserve_request()

    def test_expired_before_reservation_writes_stop_without_counting_send(self):
        clock = Clock()
        with patch.object(budget.time, "monotonic", clock):
            instance = self.make(seconds=1)
            clock.value += 1
            with self.assertRaises(budget.BudgetExceeded):
                instance.reserve_request()
        report = budget.read_budget(self.path)
        self.assertEqual(report["counts"]["reserved"], 0)
        self.assertEqual(report["stop_reason"], "deadline")

    def test_disk_latency_cannot_return_expired_dispatch_permission(self):
        clock = Clock()
        with patch.object(budget.time, "monotonic", clock):
            instance = self.make(seconds=1)
            original = instance._persist

            def slow_write(*args, **kwargs):
                original(*args, **kwargs)
                clock.value += 1

            with patch.object(instance, "_persist", slow_write), self.assertRaises(budget.BudgetExceeded):
                instance.reserve_request()
        report = budget.read_budget(self.path)
        self.assertEqual(report["stop_reason"], "deadline")
        self.assertEqual(report["counts"]["pending"], 1)

    def test_persistence_failure_prevents_dispatch_and_sanitizes_error(self):
        instance = self.make()
        before = self.path.read_bytes()
        with patch.object(budget.os, "fsync", side_effect=OSError("synthetic-sensitive-error")):
            with self.assertRaises(budget.BudgetStorageError) as caught:
                instance.reserve_request()
        self.assertNotIn("synthetic-sensitive-error", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(budget.read_budget(self.path)["counts"]["reserved"], 0)
        with self.assertRaises(budget.BudgetStorageError):
            instance.reserve_request()
        self.assertFalse(list(self.directory.glob(".budget-*")))

    def test_completion_storage_failure_retains_pending_on_disk_and_known_memory(self):
        instance = self.make()
        item = instance.reserve_request()
        with patch.object(budget.os, "fsync", side_effect=OSError("synthetic")):
            with self.assertRaises(budget.BudgetStorageError):
                instance.complete_request(item["request_id"], status_code=200)
        self.assertEqual(budget.read_budget(self.path)["counts"]["pending"], 1)
        self.assertEqual(instance.summary()["counts"]["succeeded"], 1)
        self.assertEqual(instance.summary()["stop_reason"], "persistence_failed")
        with self.assertRaises(budget.BudgetStorageError):
            instance.reserve_request()

    def test_refuses_unsafe_directory_symlink_hardlink_fifo_and_mode(self):
        self.directory.chmod(0o755)
        with self.assertRaises(budget.BudgetStorageError):
            self.make()
        self.directory.chmod(0o700)
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(budget.BudgetStorageError):
            budget.ExtractionBudget(alias / "other.json", {"max_requests": 1, "max_seconds": 1})
        instance = self.make()
        target = self.directory / "linked.json"
        target.symlink_to(self.path)
        with self.assertRaises(budget.BudgetStorageError):
            budget.read_budget(target)
        hardlink = self.directory / "hardlink.json"
        os.link(self.path, hardlink)
        with self.assertRaises(budget.BudgetStorageError):
            budget.read_budget(self.path)
        hardlink.unlink()
        fifo = self.directory / "fifo.json"
        os.mkfifo(fifo, mode=0o600)
        with self.assertRaises(budget.BudgetStorageError):
            budget.read_budget(fifo)
        self.path.chmod(0o644)
        with self.assertRaises(budget.BudgetStorageError):
            budget.read_budget(self.path)
        with self.assertRaises(budget.BudgetStorageError):
            instance.reserve_request()

    def test_reader_rejects_tamper_bool_counts_unknown_fields_and_duplicate_keys(self):
        self.make().reserve_request()
        original = budget.read_budget(self.path)
        for mutation in (lambda row: row["counts"].update(reserved=True),
                         lambda row: row.update(raw_exception="synthetic"),
                         lambda row: row["requests"][0].update(outcome="succeeded"),
                         lambda row: row.update(status="completed")):
            row = json.loads(json.dumps(original))
            mutation(row)
            self.rewrite(row)
            with self.assertRaises(budget.BudgetError):
                budget.read_budget(self.path)
        self.path.write_bytes(b'{"spec":"one","spec":"two"}')
        with self.assertRaises(budget.BudgetError):
            budget.read_budget(self.path)
        original["requests"][0]["status_code"] = 200
        self.path.write_bytes(budget._canonical(original))
        with self.assertRaises(budget.BudgetError):
            budget.read_budget(self.path)

    def test_external_replacement_or_directory_replacement_stops_writer(self):
        instance = self.make()
        replacement = self.directory / "replacement"
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(self.path)
        with self.assertRaises(budget.BudgetStorageError):
            instance.reserve_request()

    def test_no_request_content_or_arbitrary_failure_text_api(self):
        instance = self.make()
        item = instance.reserve_request()
        for outcome in ("GET https://synthetic.invalid", "pending", None, True, [], {}):
            with self.subTest(outcome=outcome), self.assertRaises(budget.BudgetError):
                instance.complete_request(item["request_id"], outcome=outcome)
        for status in (True, 200.0, "200", 99, 600):
            with self.subTest(status=status), self.assertRaises(budget.BudgetError):
                instance.complete_request(item["request_id"], status_code=status)
        with self.assertRaises(budget.BudgetError):
            instance.fail("synthetic error text")
        with self.assertRaises(budget.BudgetError):
            instance.complete_request(True)
        with self.assertRaises(budget.BudgetError):
            instance.complete_request(item["request_id"], outcome="unknown", status_code=200)
        self.assertNotIn("synthetic", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
