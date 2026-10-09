"""Audit regressions on fabricated arrays; no observed model evidence is used."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from contextbase.study_report import evaluate_study
from test_study_report import fixture, stamp


class StudyReportAuditTests(unittest.TestCase):
    def assert_rejected(self, manifest, observations, reason):
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "inconclusive", report)
        self.assertFalse(report["claim_eligible"])
        self.assertIsNone(report["metrics"])
        self.assertIn(reason, " ".join(report["reasons"]))

    def test_complete_attempt_receipt_cannot_reappear_in_other_arm(self):
        manifest, observations = fixture(evidence_origin="observed")
        # Same purported invocation receipt, contradictory model/tool spend,
        # duration and run identity. Distinct scored wrappers must not hide it.
        observations["runs"][1]["attempts"][0]["evidence_sha256"] = observations["runs"][0]["attempts"][0]["evidence_sha256"]
        self.assert_rejected(manifest, observations, "attempt receipt was reused")

    def test_complete_attempt_receipt_cannot_be_counted_twice_within_retry(self):
        manifest, observations = fixture(evidence_origin="observed")
        run = observations["runs"][1]
        success = run["attempts"][0]
        failure = deepcopy(success)
        failure["correct"] = False
        success["started_at"] = failure["finished_at"]
        success["finished_at"] = stamp(datetime.fromisoformat(success["started_at"]) + timedelta(seconds=5))
        run["attempts"] = [failure, success]
        run["finished_at"] = stamp(datetime.fromisoformat(success["finished_at"]) + timedelta(seconds=1))
        self.assert_rejected(manifest, observations, "attempt receipt was reused")

    def test_batch_allocation_receipt_cannot_be_recharged_to_independent_fixture(self):
        manifest, observations = fixture(evidence_origin="observed")
        observations["batches"][2]["evidence_sha256"] = observations["batches"][0]["evidence_sha256"]
        self.assert_rejected(manifest, observations, "accounting receipt was reused")

    def test_nonoverlapping_phase_sum_must_fit_accounting_period(self):
        manifest, observations = fixture(evidence_origin="observed")
        accounting = observations["batches"][0]
        accounting["finished_at"] = stamp(datetime.fromisoformat(accounting["started_at"]) + timedelta(seconds=2))
        self.assertTrue(all(value <= 2 for value in accounting["seconds"].values()))
        self.assertEqual(sum(accounting["seconds"].values()), 4)
        self.assert_rejected(manifest, observations, "phase times exceed")

    def test_unknown_attempt_cost_cannot_be_silently_excluded(self):
        for field in ("retry_cost_usd", "model_cost_usds", "unallocated_cost_usd"):
            with self.subTest(field=field):
                manifest, observations = fixture(evidence_origin="observed")
                observations["runs"][1]["attempts"][0][field] = 1000
                self.assert_rejected(manifest, observations, "unknown fields may hide unaccounted spend")

    def test_accounting_at_exact_boundary_remains_valid(self):
        manifest, observations = fixture(evidence_origin="observed")
        for accounting in observations["batches"]:
            accounting["finished_at"] = stamp(datetime.fromisoformat(accounting["started_at"]) + timedelta(seconds=4))
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed", report)
        self.assertFalse(report["claim_eligible"])


if __name__ == "__main__":
    unittest.main()
