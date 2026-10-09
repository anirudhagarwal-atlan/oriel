"""Fabricated recurring ledgers test evidence contracts, never model performance."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from contextbase.study_report import ARMS, ARTIFACT_FIELDS, COMPONENTS, RECURRING_SPEC, evaluate_study
from test_study_report import digest, fixture, lock, stamp


def recurring_fixture(*, evidence_origin="synthetic_test"):
    manifest, original = fixture(evidence_origin=evidence_origin)
    order = ["baseline", "refreshed", "guard_a", "guard_b"]
    kinds = {phase: (phase if phase in {"baseline", "refreshed"} else "guarded") for phase in order}
    manifest["recurring_profile"] = {"spec": RECURRING_SPEC, "phase_order": order, "phase_kinds": kinds}
    manifest["tasks"] = [{"id": phase + ("_single_record" if phase == "baseline" else "_exposure"),
                          "weight": 1, "phase_id": phase,
                          "control_kind": "single_record" if phase == "baseline" else "guarded",
                          "response_kind": "abstain" if kinds[phase] == "guarded" else "answer"} for phase in order]
    manifest["planned"] = []
    observations = {"runs": [], "batches": original["batches"], "phases": [], "transitions": [], "first_use": []}
    frozen = datetime.fromisoformat(manifest["frozen_at"])
    for batch_index, batch in enumerate(manifest["batches"]):
        batch_id = batch["id"]
        base = frozen + timedelta(days=1, seconds=batch_index * 1000)
        snapshots = {phase: digest(["source-state", batch_id, phase]) for phase in order}
        batch["phase_snapshots"] = snapshots
        batch["task_artifacts"] = {task["id"]: {field: digest([batch_id, task["id"], field]) for field in ARTIFACT_FIELDS}
                                   for task in manifest["tasks"]}
        for index, phase in enumerate(order):
            phase_start = base + timedelta(seconds=index * 45)
            task = manifest["tasks"][index]
            for arm in ARMS:
                source = arm == "source"
                run_start = phase_start + timedelta(seconds=5 if source else 30)
                duration = 20 if source else 5
                correct = not source or phase in {"baseline", "guard_b"}
                run_key = {"batch_id": batch_id, "task_id": task["id"], "repeat": 0, "arm": arm}
                manifest["planned"].append(run_key)
                run = {**run_key, "correct": correct, "started_at": stamp(run_start),
                       "finished_at": stamp(run_start + timedelta(seconds=duration + 1)), "overhead_seconds": 1,
                       "scored_evidence_sha256": digest(["score", batch_id, phase, arm]),
                       "provenance": {"model_id": manifest["model_id"], "runtime_sha256": manifest["runtime_sha256"],
                                      "context_sha256": manifest["context_sha256"], "fixture_sha256": snapshots[phase],
                                      **batch["task_artifacts"][task["id"]]},
                       "attempts": [{"correct": correct, "started_at": stamp(run_start),
                                     "finished_at": stamp(run_start + timedelta(seconds=duration)),
                                     "elapsed_seconds": duration, "model_cost_usd": 2 if source else 0.25,
                                     "tool_cost_usd": 0.1 if source else 0.05,
                                     "evidence_sha256": digest(["attempt", batch_id, phase, arm])}]}
                observations["runs"].append(run)
                guarded = kinds[phase] == "guarded"
                observations["phases"].append({"batch_id": batch_id, "arm": arm, "phase_id": phase,
                    "source_sha256": snapshots[phase], "started_at": stamp(phase_start),
                    "ready_at": stamp(phase_start + timedelta(seconds=3 if index == 0 else 1/3)),
                    "finished_at": stamp(phase_start + timedelta(seconds=40)),
                    "evidence_sha256": digest(["phase-receipt", batch_id, phase, arm]),
                    "receipt_kind": "source_state_validation" if source or guarded else "materialized_snapshot",
                    "readiness_mode": "refused_invalid_source" if not source and guarded else "ready",
                    "costs": {"setup": 0.4 if index == 0 else 0, "refresh": 0 if index == 0 else 0.1,
                              "common_review": 0.2 if index == 0 else 0, "runtime": 0.1 if index == 0 else 0},
                    "seconds": {"setup": 2 if index == 0 else 0, "refresh": 0 if index == 0 else 1/3,
                                "common_review": 1 if index == 0 else 0, "runtime": 0}})
                if index == 0:
                    observations["first_use"].append({**run_key, "cold_started_at": stamp(base),
                        "finished_at": run["finished_at"], "evidence_sha256": digest(["first-use", batch_id, arm])})
            if index:
                observations["transitions"].append({"batch_id": batch_id, "from_phase_id": order[index-1], "to_phase_id": phase,
                    "before_sha256": snapshots[order[index-1]], "after_sha256": snapshots[phase],
                    "started_at": stamp(phase_start - timedelta(seconds=5)), "finished_at": stamp(phase_start),
                    "evidence_sha256": digest(["transition", batch_id, phase])})
        for accounting in observations["batches"]:
            if accounting["batch_id"] == batch_id:
                accounting["started_at"] = stamp(base)
                accounting["finished_at"] = stamp(base + timedelta(seconds=175))
    lock(manifest, observations)
    return manifest, observations


class RecurringStudyReportTests(unittest.TestCase):
    def assert_invalid(self, manifest, observations, message):
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "inconclusive", report)
        self.assertFalse(report["charter_ready"])
        self.assertFalse(report["claim_eligible"])
        self.assertIn(message, " ".join(report["reasons"]))
        return report

    def test_generic_warm_statistics_are_not_recurring_charter_acceptance(self):
        manifest, observations = fixture(evidence_origin="observed")
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed")
        self.assertFalse(report["charter_ready"])
        self.assertIn("recurring", " ".join(report["charter_missing"]).lower())
        self.assertEqual(report["first_use"]["status"], "unknown")
        source = report["query_latency"]["by_arm"]["source"]
        self.assertEqual(source["observed_seconds"], {"count": 60, "p50": 21, "p95": 21})
        self.assertEqual(source["capped_seconds"], {"count": 60, "p50": 40.5, "p95": 60})
        self.assertEqual(source["correct_within_cap_seconds"]["count"], 30)
        self.assertEqual(source["failed_or_late_count"], 30)

    def test_recurring_ledger_and_explicit_first_use_only_prove_conditional_contract(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed", report)
        self.assertTrue(report["charter_ready"])
        self.assertFalse(report["claim_eligible"])
        self.assertEqual(report["first_use"]["by_arm"]["source"]["observed_seconds"], {"count": 30, "p50": 26, "p95": 26})
        self.assertEqual(report["first_use"]["by_arm"]["workspace"]["observed_seconds"], {"count": 30, "p50": 36, "p95": 36})
        self.assertEqual(set(report["query_latency"]["by_phase"]), {"baseline", "refreshed", "guard_a", "guard_b"})
        manifest["evidence_origin"] = "synthetic_test"
        lock(manifest, observations)
        report = self.assert_invalid(manifest, observations, "Synthetic test arrays")
        self.assertEqual(report["first_use"]["status"], "recorded")

    def test_missing_profile_phase_or_artifact_provenance_fails_closed(self):
        for case in ("kinds", "abstain", "artifacts", "artifact_hash", "phase_binding"):
            with self.subTest(case=case):
                manifest, observations = recurring_fixture(evidence_origin="observed")
                if case == "kinds":
                    manifest["recurring_profile"]["phase_kinds"]["refreshed"] = "baseline"
                    reason = "coverage requires"
                elif case == "abstain":
                    manifest["tasks"][-1]["response_kind"] = "answer"
                    reason = "response_kind"
                elif case == "artifacts":
                    del manifest["batches"][0]["task_artifacts"][manifest["tasks"][0]["id"]]
                    reason = "artifact bindings"
                elif case == "artifact_hash":
                    observations["runs"][0]["provenance"]["scorer_sha256"] = digest("different-scorer")
                    reason = "identity differs"
                else:
                    observations["runs"][4]["provenance"]["fixture_sha256"] = manifest["batches"][0]["phase_snapshots"]["baseline"]
                    reason = "identity differs"
                lock(manifest, observations)
                self.assert_invalid(manifest, observations, reason)

    def test_missing_duplicated_or_incorrect_phase_receipts_are_rejected(self):
        for case in ("missing", "duplicate", "reused", "stale", "false_materialization", "early_readiness"):
            with self.subTest(case=case):
                manifest, observations = recurring_fixture(evidence_origin="observed")
                phase = observations["phases"][5]  # Workspace guard_a.
                if case == "missing":
                    observations["phases"].pop()
                    reason = "Missing recurring"
                elif case == "duplicate":
                    observations["phases"].append(deepcopy(phase))
                    reason = "Duplicate or unplanned"
                elif case == "reused":
                    phase["evidence_sha256"] = observations["phases"][0]["evidence_sha256"]
                    reason = "receipt was reused"
                elif case == "stale":
                    phase["source_sha256"] = manifest["batches"][0]["phase_snapshots"]["baseline"]
                    reason = "source state differs"
                elif case == "false_materialization":
                    phase["receipt_kind"] = "materialized_snapshot"
                    phase["readiness_mode"] = "ready"
                    reason = "refusing an invalid source"
                else:
                    phase["ready_at"] = phase["finished_at"]
                    reason = "before phase readiness"
                self.assert_invalid(manifest, observations, reason)

    def test_transition_chaining_and_chronology_bind_both_arms(self):
        for case in ("missing", "prior_phase_running", "next_phase_running", "wrong_hash"):
            with self.subTest(case=case):
                manifest, observations = recurring_fixture(evidence_origin="observed")
                transition = observations["transitions"][0]
                if case == "missing":
                    observations["transitions"].pop()
                    reason = "Missing recurring source transition"
                elif case == "prior_phase_running":
                    transition["started_at"] = observations["phases"][0]["ready_at"]
                    reason = "follow both previous arms"
                elif case == "next_phase_running":
                    transition["finished_at"] = observations["phases"][2]["finished_at"]
                    reason = "follow both previous arms"
                else:
                    transition["before_sha256"] = digest("unrelated")
                    reason = "before/after source states"
                self.assert_invalid(manifest, observations, reason)

    def test_all_phase_costs_and_time_reconcile_to_existing_batch_totals(self):
        for field in ("costs", "seconds"):
            manifest, observations = recurring_fixture(evidence_origin="observed")
            observations["phases"][2][field]["refresh"] += 0.1
            self.assert_invalid(manifest, observations, "do not reconcile")
        manifest, observations = recurring_fixture(evidence_origin="observed")
        observations["phases"][0]["seconds"]["runtime"] = 30
        self.assert_invalid(manifest, observations, "overlaps accounted query time")

    def test_phase_schedule_cannot_return_to_an_earlier_source_state(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        manifest["planned"][0], manifest["planned"][2] = manifest["planned"][2], manifest["planned"][0]
        lock(manifest, observations)
        self.assert_invalid(manifest, observations, "preserve declared phase order")

    def test_query_quantiles_remain_unweighted_when_primary_task_weights_change(self):
        manifest, observations = fixture(evidence_origin="observed")
        manifest["tasks"][1]["weight"] = 9
        lock(manifest, observations)
        report = evaluate_study(manifest, observations)
        self.assertAlmostEqual(report["metrics"]["source"]["mean_capped_seconds"], 58.1)
        self.assertEqual(report["query_latency"]["by_arm"]["source"]["capped_seconds"]["p50"], 40.5)

    def test_first_use_is_never_inferred_or_cherry_picked(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        del observations["first_use"]
        report = self.assert_invalid(manifest, observations, "explicit first-use")
        self.assertEqual(report["first_use"]["status"], "unknown")
        for case in ("missing", "later_run", "omit_setup", "wrong_finish", "reused_receipt"):
            with self.subTest(case=case):
                manifest, observations = recurring_fixture(evidence_origin="observed")
                row = observations["first_use"][0]
                if case == "missing":
                    observations["first_use"].pop()
                    reason = "Missing explicit first-use"
                elif case == "later_run":
                    row["task_id"] = manifest["tasks"][1]["id"]
                    reason = "first planned run"
                elif case == "omit_setup":
                    row["cold_started_at"] = observations["runs"][0]["started_at"]
                    reason = "include baseline phase preparation"
                elif case == "wrong_finish":
                    row["finished_at"] = observations["runs"][0]["started_at"]
                    reason = "finish disagrees"
                else:
                    observations["first_use"][1]["evidence_sha256"] = row["evidence_sha256"]
                    reason = "receipt was reused"
                self.assert_invalid(manifest, observations, reason)

    def test_wrong_first_answer_retains_cold_overhead_and_receives_query_cap(self):
        manifest, observations = recurring_fixture()
        for run in observations["runs"]:
            if run["arm"] == "source" and run["task_id"] == "baseline_single_record":
                run["correct"] = run["attempts"][0]["correct"] = False
        report = self.assert_invalid(manifest, observations, "Synthetic test arrays")
        source = report["first_use"]["by_arm"]["source"]
        self.assertEqual(source["correct_first_answers"], 0)
        self.assertEqual(source["observed_seconds"]["p50"], 26)
        self.assertEqual(source["capped_seconds"]["p50"], 65)  # Five cold seconds + sixty-second query cap.

    def test_observed_preparation_time_cannot_disappear_from_batch_accounting(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        for row in observations["phases"] + observations["batches"]:
            row["seconds"] = {component: 0 for component in COMPONENTS}
        self.assert_invalid(manifest, observations, "omits observed preparation time")
        manifest, observations = recurring_fixture(evidence_origin="observed")
        row = observations["phases"][1]
        row["ready_at"] = stamp(datetime.fromisoformat(row["started_at"]) + timedelta(seconds=25))
        self.assert_invalid(manifest, observations, "omits observed preparation time")

    def test_cold_start_cannot_precede_its_accounted_batch_window(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        observations["first_use"][0]["cold_started_at"] = manifest["frozen_at"]
        self.assert_invalid(manifest, observations, "outside its batch accounting window")

    def test_one_measurement_receipt_cannot_play_multiple_roles(self):
        for source, destination in (("attempt", "phase"), ("phase", "transition"),
                                     ("transition", "accounting"), ("accounting", "first_use"), ("scored", "attempt")):
            with self.subTest(source=source, destination=destination):
                manifest, observations = recurring_fixture(evidence_origin="observed")
                records = {"attempt": (observations["runs"][0]["attempts"][0], "evidence_sha256"),
                           "scored": (observations["runs"][0], "scored_evidence_sha256"),
                           "phase": (observations["phases"][0], "evidence_sha256"),
                           "transition": (observations["transitions"][0], "evidence_sha256"),
                           "accounting": (observations["batches"][0], "evidence_sha256"),
                           "first_use": (observations["first_use"][0], "evidence_sha256")}
                record, field = records[destination]
                other, other_field = records[source]
                record[field] = other[other_field]
                self.assert_invalid(manifest, observations, "reused across evidence roles")

    def test_unknown_cost_fields_in_recurring_records_are_rejected(self):
        for record_type in ("phases", "transitions", "first_use"):
            for value in (None, 1000):
                with self.subTest(record_type=record_type, value=value):
                    manifest, observations = recurring_fixture(evidence_origin="observed")
                    observations[record_type][0]["unallocated_cost_usd"] = value
                    self.assert_invalid(manifest, observations, "unknown fields may hide unaccounted spend")

    def test_single_record_and_positive_guarded_controls_are_required(self):
        for task_index, reason in ((0, "single_record control"), (1, "positive guarded answer control")):
            manifest, observations = recurring_fixture(evidence_origin="observed")
            manifest["tasks"][task_index]["control_kind"] = "analytic"
            lock(manifest, observations)
            self.assert_invalid(manifest, observations, reason)

    def test_per_task_scorecard_keeps_source_control_wins_and_reconciles_spend(self):
        manifest, observations = recurring_fixture(evidence_origin="observed")
        first_run_finishes = {}
        for run in observations["runs"]:
            if run["task_id"] == "baseline_single_record" and run["arm"] == "source":
                attempt = run["attempts"][0]
                attempt["elapsed_seconds"] = 1
                attempt["finished_at"] = stamp(datetime.fromisoformat(attempt["started_at"]) + timedelta(seconds=1))
                attempt["model_cost_usd"] = attempt["tool_cost_usd"] = 0.01
                run["finished_at"] = stamp(datetime.fromisoformat(run["started_at"]) + timedelta(seconds=2))
                first_run_finishes[(run["batch_id"], run["arm"])] = run["finished_at"]
        for first_use in observations["first_use"]:
            if first_use["arm"] == "source":
                first_use["finished_at"] = first_run_finishes[(first_use["batch_id"], first_use["arm"])]
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed", report)
        tasks = report["task_scorecard"]["by_task"]
        control = tasks["baseline_single_record"]
        self.assertEqual(control["control_kind"], "single_record")
        self.assertGreater(control["workspace_minus_source"]["total_cost_difference_usd"], 0)
        self.assertGreater(control["workspace_minus_source"]["mean_capped_seconds_difference"], 0)
        self.assertEqual(control["by_arm"]["source"]["correct_answers"], 30)
        self.assertEqual(control["by_arm"]["source"]["planned_runs"], 30)
        self.assertIn("no per-task significance", report["task_scorecard"]["scope"])
        for arm in ARMS:
            self.assertAlmostEqual(sum(task["by_arm"][arm]["total_cost_usd"] for task in tasks.values()),
                                   report["metrics"][arm]["total_cost_usd"])
            self.assertEqual(sum(task["by_arm"][arm]["correct_answers"] for task in tasks.values()),
                             report["metrics"][arm]["correct_answers"])


if __name__ == "__main__":
    unittest.main()
