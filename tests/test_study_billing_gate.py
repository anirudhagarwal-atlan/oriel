"""Charter gating around authored statistics; no native or provider evidence.

The local controller still runs against in-memory fixtures and scripted model
responses. Only the final statistical report is replaced with authored outcomes
to isolate the missing-billing gate from statistical acceptance decisions.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_study_controller as fixtures


class StudyBillingGateTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.StudyControllerTests.setUpClass.__func__)
    setUp = fixtures.StudyControllerTests.setUp
    rehash = fixtures.StudyControllerTests.rehash
    pair_factory = fixtures.StudyControllerTests.pair_factory
    run_controller = fixtures.StudyControllerTests.run_controller

    def assert_billing_gate(self, statistical_acceptance, gates):
        authored = {"spec": "contextbase.acceptance-report/v1", "statistical_acceptance": statistical_acceptance,
                    "charter_ready": statistical_acceptance == "passed", "claim_eligible": False,
                    "gates": gates, "reasons": ["Authored statistics for this offline controller test only"],
                    "limitations": []}
        with patch("contextbase.study_controller.build_report", side_effect=lambda *args: deepcopy(authored)):
            result = self.run_controller()
        self.assertEqual(result["outcome"], "completed")
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["statistical_acceptance"], statistical_acceptance)
        self.assertEqual(result["statistical_acceptance"], statistical_acceptance)
        self.assertEqual(report["gates"], gates)
        self.assertFalse(report["charter_ready"])
        self.assertFalse(report["claim_eligible"])
        self.assertFalse(report["controller_evidence"]["full_billing_reconciliation_verified"])
        self.assertTrue(report["controller_evidence"]["excluded_analysis_overhead"])
        reason = "Full billing reconciliation is required before charter readiness"
        self.assertEqual(report["reasons"].count(reason), 1)
        self.assertIn(reason, result["reasons"])
        self.assertIn(authored["reasons"][0], report["reasons"])

    def test_scoped_statistical_pass_cannot_imply_all_in_charter_readiness(self):
        self.assert_billing_gate("passed", {"accuracy": True, "cost": True, "time": True})

    def test_missing_billing_does_not_replace_a_scoped_statistical_failure(self):
        self.assert_billing_gate("failed", {"accuracy": True, "cost": False, "time": True})


if __name__ == "__main__":
    unittest.main()
