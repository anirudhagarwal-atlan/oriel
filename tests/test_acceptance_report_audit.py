"""Adversarial offline collector regressions, never performance evidence.

The fixture authors every response and receipt. As in test_acceptance_report,
only protocol/binding reconstruction is stubbed; ledger validation, chronology,
independent scoring, complete scheduling and arithmetic execute normally.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from contextbase.acceptance_report import build_report, verification_evidence
from contextbase.rystic_benchmark import _hash as engine_hash
from contextbase.study_binding import _hash
from test_acceptance_report import fixture


def shifted(value, seconds):
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def add_paid_tool_turn(trial, *, unknown_final=False):
    """Keep original wall time and known price while retaining a real tool span."""
    original = deepcopy(trial["inferences"][0])
    start = trial["started_at"]
    first = deepcopy(original)
    first.update(finished_at=shifted(start, 1), measured_elapsed_seconds=1.,
                 response=json.dumps({"tools": [{"tool": "synthetic_read", "arguments": {}}]}))
    second = deepcopy(original)
    second.update(inference_index=1, started_at=shifted(start, 1.1),
                  measured_elapsed_seconds=trial["elapsed_seconds"] - 1.1)
    if unknown_final:
        second.update(cost_usd=None, cost_known=False, spend_unknown=True, is_error=True)
        second.pop("response")
        trial.update(status="inference_cost_unknown", answer=None, correct=False, cost_complete=False)
    else:
        first["cost_usd"] = original["cost_usd"] / 2
        second["cost_usd"] = original["cost_usd"] / 2
    trial["inferences"] = [first, second]
    trial["tool_invocations"] = [{"tool_index": 0, "inference_index": 0, "tool": "synthetic_read",
        "arguments_sha256": engine_hash({}), "started_at": shifted(start, 1),
        "finished_at": shifted(start, 1.1), "elapsed_seconds": .1, "status": "returned"}]


class AcceptanceReportAuditTests(unittest.TestCase):
    def setUp(self):
        for name in ("validate_protocol", "_validate_stored"):
            patcher = patch("contextbase.acceptance_report." + name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("contextbase.rystic.RysticPair", "contextbase.model.ClaudeCLI"):
            patcher = patch(target, side_effect=AssertionError("Offline audit must not execute native/model calls"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_inconclusive(self, report, reason):
        self.assertEqual(report["statistical_acceptance"], "inconclusive", report["reasons"])
        self.assertFalse(report["charter_ready"])
        self.assertFalse(report["claim_eligible"])
        self.assertIn(reason, " ".join(report["reasons"]))

    def test_authored_control_fixture_reaches_conditional_statistics_only(self):
        protocol, collection = fixture(1)
        original = deepcopy((protocol, collection))
        report = build_report(protocol, collection)
        self.assertEqual(report["statistical_acceptance"], "passed", report["reasons"])
        self.assertFalse(report["claim_eligible"])
        self.assertEqual((protocol, collection), original)

    def test_unknown_engine_trial_and_inference_charges_cannot_disappear(self):
        for level in ("execution", "trial", "inference"):
            protocol, collection = fixture(1)
            execution = collection["phases"][0]["execution"]
            target = {"execution": execution, "trial": execution["trials"][0],
                      "inference": execution["trials"][0]["inferences"][0]}[level]
            target["extra_unaccounted_usd"] = 100
            with self.subTest(level=level):
                self.assert_inconclusive(build_report(protocol, collection), "unknown fields")

    def test_unknown_tool_fee_field_cannot_disappear(self):
        protocol, collection = fixture(1)
        execution = collection["phases"][0]["execution"]
        add_paid_tool_turn(execution["trials"][0])
        execution["invocations"] += 1
        # First prove this tool receipt is valid before introducing the charge.
        control = build_report(protocol, collection)
        self.assertEqual(control["statistical_acceptance"], "passed", control["reasons"])
        execution["trials"][0]["tool_invocations"][0]["extra_tool_fee_usd"] = 100
        self.assert_inconclusive(build_report(protocol, collection), "unknown fields")

    def test_preflight_cannot_move_after_queries(self):
        protocol, collection = fixture(1)
        execution = collection["phases"][0]["execution"]
        for suffix in ("started_at", "finished_at", "seconds"):
            execution["preflight_" + suffix] = execution["postflight_" + suffix]
        self.assert_inconclusive(build_report(protocol, collection), "Preflight must finish before")

    def test_postflight_cannot_move_before_both_arms_finish(self):
        protocol, collection = fixture(1)
        execution = collection["phases"][0]["execution"]
        execution.update(postflight_started_at=execution["preflight_finished_at"],
                         postflight_finished_at=shifted(execution["preflight_finished_at"], .1))
        self.assert_inconclusive(build_report(protocol, collection), "Postflight must follow")

    def test_verification_cannot_escape_the_recorded_phase_interval(self):
        protocol, collection = fixture(1)
        execution = collection["phases"][0]["execution"]
        execution.update(preflight_started_at=shifted(execution["started_at"], -1),
                         preflight_finished_at=shifted(execution["started_at"], -.9))
        self.assert_inconclusive(build_report(protocol, collection), "Preflight lies outside")

    def test_zero_duration_placeholder_cannot_replace_measured_verification(self):
        protocol, collection = fixture(1)
        event = next(row for row in collection["events"] if row["kind"] == "evaluator_validation")
        event.update(finished_at=event["started_at"], elapsed_seconds=0.)
        self.assert_inconclusive(build_report(protocol, collection), "Original verification")

    def test_rehashed_zero_work_verification_cannot_claim_source_stability(self):
        for stage in ("preflight", "postflight"):
            protocol, collection = fixture(1)
            execution = collection["phases"][0]["execution"]
            old_receipt = _hash(verification_evidence(execution, stage))
            event = next(row for row in collection["events"] if row["evidence_sha256"] == old_receipt)
            execution[stage + "_finished_at"] = execution[stage + "_started_at"]
            execution[stage + "_seconds"] = 0.
            event.update(finished_at=event["started_at"], elapsed_seconds=0.,
                         evidence_sha256=_hash(verification_evidence(execution, stage)))
            with self.subTest(stage=stage):
                self.assert_inconclusive(build_report(protocol, collection), "positive")

    def test_first_use_requires_a_receipt_distinct_from_other_accounting_roles(self):
        for kind in ("workflow_configuration", "workspace_ingestion", "phase_transitions", "evaluator_validation"):
            protocol, collection = fixture(1)
            event = next(row for row in collection["events"] if row["kind"] == kind)
            collection["first_use"][0]["evidence_sha256"] = event["evidence_sha256"]
            with self.subTest(kind=kind):
                self.assert_inconclusive(build_report(protocol, collection), "Cold-start receipt reused")

    def test_correct_source_answer_cannot_be_downgraded_using_fabricated_failure_status(self):
        cases = (("trial_deadline", "actual deadline"), ("turn_limit", "turn limit"),
                 ("inference_error", "error receipt"), ("invalid_model_response", "parses successfully"),
                 ("tool_or_transport_error", "downgraded"), ("cost_budget", "downgraded"))
        for status, expected in cases:
            protocol, collection = fixture(1)
            trial = collection["phases"][0]["execution"]["trials"][0]
            trial.update(status=status, correct=False, answer=None)
            if status == "tool_or_transport_error":
                trial["error_type"] = "FabricatedTransportError"
            with self.subTest(status=status):
                self.assert_inconclusive(build_report(protocol, collection), expected)

    def test_separate_source_fees_cannot_hide_inside_included_model_spend(self):
        protocol, collection = fixture(1)
        trial = collection["phases"][0]["execution"]["trials"][0]
        event = deepcopy(next(row for row in collection["events"] if row["kind"] == "source_tool_calls"
                              and row["allocation"]["source"] == 1))
        event.update(event_id="hidden-source-fee", phase_id="baseline", cost_usd=.01,
                     duration_role="within_attempt", cost_role="included_in_attempt", attempt_id=trial["trial_id"],
                     started_at=trial["started_at"], finished_at=trial["finished_at"],
                     elapsed_seconds=trial["elapsed_seconds"], evidence_sha256=_hash("hidden-source-fee"))
        collection["events"].append(event)
        report = build_report(protocol, collection)
        self.assert_inconclusive(report, "Separate source fees cannot be hidden")
        self.assertLess(event["cost_usd"], trial["known_cost_usd"])

    def test_work_after_final_query_still_consumes_the_study_deadline(self):
        protocol, collection = fixture(1)
        deadline = protocol["budgets"]["study_seconds"]
        event = deepcopy(next(row for row in collection["events"] if row["kind"] == "workflow_configuration"))
        event.update(event_id="late-configuration", phase_id=None, cost_usd=.001,
                     evidence_sha256=_hash("late-configuration"), elapsed_seconds=1.,
                     started_at=shifted(protocol["frozen_at"], deadline + 1),
                     finished_at=shifted(protocol["frozen_at"], deadline + 2))
        collection["events"].append(event)
        self.assert_inconclusive(build_report(protocol, collection), "study elapsed deadline exceeded")

    def test_cost_only_billing_interval_does_not_invent_postquery_work_time(self):
        protocol, collection = fixture(1)
        event = next(row for row in collection["events"] if row["kind"] == "runtime_license")
        event["finished_at"] = shifted(protocol["frozen_at"], protocol["budgets"]["study_seconds"] + 10)
        event["elapsed_seconds"] = (datetime.fromisoformat(event["finished_at"])
                                    - datetime.fromisoformat(event["started_at"])).total_seconds()
        report = build_report(protocol, collection)
        self.assertEqual(report["statistical_acceptance"], "passed", report["reasons"])
        self.assertFalse(report["claim_eligible"])

    def test_unknown_partial_execution_preserves_known_paid_calls_and_overhead(self):
        protocol, collection = fixture(1)
        execution = collection["phases"][0]["execution"]
        source, skipped = execution["trials"]
        add_paid_tool_turn(source, unknown_final=True)
        skipped.update(started_at=None, finished_at=None, status="skipped_inference_cost_unknown",
                       answer=None, correct=False, elapsed_seconds=0., known_cost_usd=0., cost_complete=True,
                       inferences=[], tool_invocations=[])
        execution.update(known_cost_usd=source["known_cost_usd"], cost_complete=False,
                         stop_reason="inference_cost_unknown", execution_complete=False)
        original = deepcopy(collection)
        expected_model = sum(trial["known_cost_usd"] for packet in collection["phases"]
                             for trial in packet["execution"]["trials"])
        expected_overhead = sum(event["cost_usd"] for event in collection["events"])
        report = build_report(protocol, collection)
        self.assert_inconclusive(report, "unknown spend")
        self.assertIn("Missing executed trials", " ".join(report["reasons"]))
        self.assertAlmostEqual(report["known_comparison_spend_usd"], expected_model + expected_overhead)
        self.assertAlmostEqual(report["known_total_study_spend_usd"], expected_model + expected_overhead + 1.)
        self.assertEqual(report["model_calls"], 8)
        self.assertEqual(source["known_cost_usd"], .03)
        self.assertIsNone(source["inferences"][1]["cost_usd"])
        self.assertEqual(collection, original)
        self.assertIsNone(report["paired_intervals"])
        self.assertIsNone(report["gates"])


if __name__ == "__main__":
    unittest.main()
