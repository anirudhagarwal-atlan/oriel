"""Fabricated arrays test arithmetic/guards only; they are not model evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from contextbase.study_report import ARMS, SPEC, evaluate_study


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def stamp(value):
    return value.isoformat()


def lock(manifest, observations):
    manifest["manifest_sha256"] = digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    for run in observations["runs"]:
        run["provenance"]["manifest_sha256"] = manifest["manifest_sha256"]


def fixture(batch_count=30, *, evidence_origin="synthetic_test"):
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [{"id": "task_0", "weight": 1}, {"id": "task_1", "weight": 1}]
    batches = [{"id": f"batch_{index}", "fixture_sha256": digest(["fixture", index])}
               for index in range(batch_count)]
    planned = [{"batch_id": batch["id"], "task_id": task["id"], "repeat": 0, "arm": arm}
               for batch in batches for task in tasks for arm in ARMS]
    manifest = {"spec": SPEC, "kind": "acceptance", "held_out": True,
                "evidence_origin": evidence_origin, "frozen_at": stamp(frozen),
                "model_id": "synthetic-test-model", "runtime_sha256": digest("runtime"),
                "context_sha256": digest("context"), "tasks": tasks, "batches": batches,
                "repeats": 1, "planned": planned,
                "caps": {"run_seconds": 60, "total_cost_usd": 10000},
                "bootstrap": {"seed": 7, "replicates": 2000, "confidence": 0.95},
                "power": {"minimum_independent_batches": 30, "target_power": 0.8,
                          "analysis_sha256": digest("test-only-power-placeholder"), "reviewed": True}}
    observations = {"runs": [], "batches": []}
    by_batch = {batch["id"]: batch["fixture_sha256"] for batch in batches}
    for index, item in enumerate(planned):
        source = item["arm"] == "source"
        correct = not source or item["task_id"] == "task_0"
        elapsed = 20 if source else 5
        started = frozen + timedelta(days=1, seconds=index * 100)
        observations["runs"].append({**item, "correct": correct, "started_at": stamp(started),
            "finished_at": stamp(started + timedelta(seconds=elapsed + 1)), "overhead_seconds": 1,
            "scored_evidence_sha256": digest(["score", index]),
            "provenance": {"model_id": manifest["model_id"], "runtime_sha256": manifest["runtime_sha256"],
                           "context_sha256": manifest["context_sha256"], "fixture_sha256": by_batch[item["batch_id"]]},
            "attempts": [{"correct": correct, "elapsed_seconds": elapsed,
                          "started_at": stamp(started), "finished_at": stamp(started + timedelta(seconds=elapsed)),
                          "model_cost_usd": 2 if source else 0.25, "tool_cost_usd": 0.1 if source else 0.05,
                          "evidence_sha256": digest(["attempt", index])}]})
    for batch in batches:
        for arm in ARMS:
            observations["batches"].append({"batch_id": batch["id"], "arm": arm,
                "started_at": stamp(frozen + timedelta(days=1)), "finished_at": stamp(frozen + timedelta(days=2)),
                "evidence_sha256": digest(["accounting", batch["id"], arm]),
                "costs": {"setup": 0.4, "refresh": 0.3, "common_review": 0.2, "runtime": 0.1},
                "seconds": {"setup": 2, "refresh": 1, "common_review": 1, "runtime": 0}})
    lock(manifest, observations)
    return manifest, observations


def add_phases(manifest, observations):
    """Bind one baseline task and one refreshed task to distinct source states."""
    renames = {"task_0": "initial/exposure", "task_1": "refreshed/exposure"}
    for task in manifest["tasks"]:
        task["phase_id"] = "baseline" if task["id"] == "task_0" else "refreshed"
        task["id"] = renames[task["id"]]
    for row in manifest["planned"]:
        row["task_id"] = renames[row["task_id"]]
    by_batch = {}
    for batch in manifest["batches"]:
        batch["phase_snapshots"] = {"baseline": batch["fixture_sha256"],
                                    "refreshed": digest(["refreshed-source-state", batch["id"]])}
        by_batch[batch["id"]] = batch["phase_snapshots"]
    task_phases = {task["id"]: task["phase_id"] for task in manifest["tasks"]}
    for row in observations["runs"]:
        row["task_id"] = renames[row["task_id"]]
        row["provenance"]["fixture_sha256"] = by_batch[row["batch_id"]][task_phases[row["task_id"]]]
    lock(manifest, observations)


class StudyReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # "observed" is an explicit test-only declaration to exercise the gate
        # arithmetic. The report must still refuse public-claim eligibility.
        cls.manifest, cls.observations = fixture(evidence_origin="observed")
        cls.report = evaluate_study(cls.manifest, cls.observations)

    def assert_inconclusive(self, manifest, observations):
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "inconclusive", report)
        self.assertFalse(report["claim_eligible"])
        self.assertTrue(report["reasons"])
        return report

    def test_perfect_fabricated_arrays_only_test_conditional_statistics(self):
        self.assertEqual(self.report["statistical_acceptance"], "passed", self.report)
        self.assertTrue(all(self.report["gates"].values()))
        self.assertFalse(self.report["claim_eligible"])
        self.assertIn("hashes do not authenticate", self.report["limitations"][0])
        self.assertEqual(self.report["metrics"]["workspace"]["accuracy"], 1)
        self.assertEqual(self.report["metrics"]["source"]["accuracy"], 0.5)
        synthetic_manifest, synthetic_observations = fixture()
        result = self.assert_inconclusive(synthetic_manifest, synthetic_observations)
        self.assertIn("Synthetic test arrays", " ".join(result["reasons"]))

    def test_failed_runs_and_all_overhead_are_included_exactly_once(self):
        source, workspace = self.report["metrics"]["source"], self.report["metrics"]["workspace"]
        self.assertEqual(source["planned_runs"], 60)
        self.assertEqual(source["correct_answers"], 30)
        self.assertAlmostEqual(source["total_cost_usd"], 156)
        self.assertAlmostEqual(workspace["total_cost_usd"], 48)
        self.assertAlmostEqual(source["cost_per_correct_usd"], 5.2)
        self.assertAlmostEqual(workspace["cost_per_correct_usd"], 0.8)
        self.assertAlmostEqual(source["mean_capped_seconds"], 42.5)
        self.assertAlmostEqual(workspace["mean_capped_seconds"], 8)

    def test_bootstrap_resamples_whole_pairs_not_individual_tasks(self):
        # Within every independent batch the paired accuracy lift is exactly
        # 0.5; resampling individual correct/incorrect tasks would add variance.
        self.assertEqual(self.report["paired_intervals"]["accuracy_lift"], {"lower": 0.5, "upper": 0.5})
        self.assertEqual(self.report["resampling"]["unit"], "whole paired independent batch")
        self.assertEqual(self.report["resampling"]["replicates"], 2000)
        repeated = evaluate_study(deepcopy(self.manifest), deepcopy(self.observations))
        self.assertEqual(repeated, self.report)

    def test_null_missing_negative_boolean_and_nonfinite_costs_never_become_zero(self):
        for value in (None, -1, True, "0", float("nan"), float("inf")):
            for location in ("attempt", "batch"):
                with self.subTest(value=value, location=location):
                    observations = deepcopy(self.observations)
                    if location == "attempt":
                        observations["runs"][0]["attempts"][0]["model_cost_usd"] = value
                    else:
                        observations["batches"][0]["costs"]["refresh"] = value
                    self.assert_inconclusive(self.manifest, observations)
        observations = deepcopy(self.observations)
        del observations["runs"][0]["attempts"][0]["tool_cost_usd"]
        self.assert_inconclusive(self.manifest, observations)
        observations = deepcopy(self.observations)
        del observations["batches"][0]["costs"]["runtime"]
        self.assert_inconclusive(self.manifest, observations)
        observations = deepcopy(self.observations)
        del observations["batches"][0]["seconds"]["setup"]
        self.assert_inconclusive(self.manifest, observations)

    def test_incomplete_extra_duplicate_and_reused_best_runs_cannot_pass(self):
        mutations = [lambda data: data["runs"].pop(),
                     lambda data: data["batches"].pop(),
                     lambda data: data["runs"].append(deepcopy(data["runs"][0])),
                     lambda data: data["batches"].append(deepcopy(data["batches"][0])),
                     lambda data: data["runs"][0].update(task_id="unplanned"),
                     lambda data: data["runs"][1].update(scored_evidence_sha256=data["runs"][0]["scored_evidence_sha256"])]
        for mutate in mutations:
            observations = deepcopy(self.observations)
            mutate(observations)
            self.assert_inconclusive(self.manifest, observations)
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        manifest["planned"].pop()
        observations["runs"].pop()
        lock(manifest, observations)
        self.assert_inconclusive(manifest, observations)

    def test_unlocked_or_inconsistent_identities_fail_closed(self):
        manifest = deepcopy(self.manifest)
        manifest["bootstrap"]["seed"] += 1
        self.assert_inconclusive(manifest, self.observations)
        for key in ("model_id", "runtime_sha256", "context_sha256", "fixture_sha256", "manifest_sha256"):
            observations = deepcopy(self.observations)
            observations["runs"][0]["provenance"][key] = digest("different")
            self.assert_inconclusive(self.manifest, observations)

    def test_pilot_small_unheldout_unreviewed_and_weak_resampling_are_inconclusive(self):
        for key, value in (("kind", "development_pilot"), ("held_out", False)):
            manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
            manifest[key] = value
            lock(manifest, observations)
            self.assert_inconclusive(manifest, observations)
        for container, key, value in (("power", "reviewed", False), ("power", "minimum_independent_batches", 31),
                                      ("bootstrap", "replicates", 1999), ("power", "target_power", 0.5)):
            manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
            manifest[container][key] = value
            lock(manifest, observations)
            self.assert_inconclusive(manifest, observations)
        manifest, observations = fixture(2, evidence_origin="observed")
        self.assert_inconclusive(manifest, observations)

    def test_same_fixture_cannot_be_relabelled_as_independent_batches(self):
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        manifest["batches"][1]["fixture_sha256"] = manifest["batches"][0]["fixture_sha256"]
        lock(manifest, observations)
        self.assert_inconclusive(manifest, observations)

    def test_failures_before_success_add_cost_and_time(self):
        manifest, observations = fixture()
        run = next(row for row in observations["runs"] if row["arm"] == "workspace")
        original = deepcopy(run["attempts"][0])
        failed = deepcopy(original)
        failed.update(correct=False, model_cost_usd=10, tool_cost_usd=1, evidence_sha256=digest("failed-retry"))
        original["started_at"] = failed["finished_at"]
        original["finished_at"] = stamp(datetime.fromisoformat(original["started_at"]) + timedelta(seconds=5))
        run["attempts"] = [failed, original]
        run["finished_at"] = stamp(datetime.fromisoformat(original["finished_at"]) + timedelta(seconds=1))
        report = self.assert_inconclusive(manifest, observations)
        self.assertAlmostEqual(report["metrics"]["workspace"]["total_cost_usd"], 59)
        self.assertEqual(report["metrics"]["workspace"]["attempts"], 61)
        self.assertAlmostEqual(report["metrics"]["workspace"]["mean_capped_seconds"], 8 + 5 / 60)

    def test_extra_attempts_after_success_or_mismatched_score_are_invalid(self):
        for change in ("extra", "mismatched"):
            observations = deepcopy(self.observations)
            run = next(row for row in observations["runs"] if row["arm"] == "workspace")
            if change == "extra":
                run["attempts"].append(deepcopy(run["attempts"][0]))
            else:
                run["correct"] = False
            self.assert_inconclusive(self.manifest, observations)

    def test_late_correct_answer_counts_as_failure_and_keeps_spend(self):
        manifest, observations = fixture()
        run = next(row for row in observations["runs"] if row["arm"] == "workspace")
        attempt = run["attempts"][0]
        attempt["elapsed_seconds"] = 61
        attempt["finished_at"] = stamp(datetime.fromisoformat(attempt["started_at"]) + timedelta(seconds=61))
        run["finished_at"] = stamp(datetime.fromisoformat(attempt["finished_at"]) + timedelta(seconds=1))
        report = self.assert_inconclusive(manifest, observations)
        workspace = report["metrics"]["workspace"]
        self.assertEqual(workspace["correct_answers"], 59)
        self.assertEqual(workspace["late_answers"], 1)
        self.assertAlmostEqual(workspace["total_cost_usd"], 48)
        self.assertAlmostEqual(workspace["mean_capped_seconds"], 8.9)

    def test_zero_correct_or_zero_baseline_cost_never_fabricates_ratios(self):
        for case in ("no_correct", "free_baseline"):
            observations = deepcopy(self.observations)
            for run in observations["runs"]:
                if run["arm"] == "source":
                    if case == "no_correct":
                        run["correct"] = run["attempts"][0]["correct"] = False
                    else:
                        run["attempts"][0]["model_cost_usd"] = run["attempts"][0]["tool_cost_usd"] = 0
            if case == "free_baseline":
                for batch in observations["batches"]:
                    if batch["arm"] == "source":
                        batch["costs"] = {key: 0 for key in batch["costs"]}
                    batch["costs"]["common_review"] = 0  # Preserve equal shared allocation.
            report = self.assert_inconclusive(self.manifest, observations)
            self.assertIsNone(report["paired_intervals"])
            if case == "no_correct":
                self.assertIsNone(report["metrics"]["source"]["cost_per_correct_usd"])

    def test_task_weights_affect_accuracy_and_time_but_not_actual_spend(self):
        manifest, observations = fixture()
        manifest["tasks"][1]["weight"] = 9
        lock(manifest, observations)
        report = self.assert_inconclusive(manifest, observations)
        source = report["metrics"]["source"]
        self.assertAlmostEqual(source["accuracy"], 0.1)
        self.assertAlmostEqual(source["mean_capped_seconds"], 58.1)
        self.assertAlmostEqual(source["total_cost_usd"], 156)

    def test_all_execution_phases_postdate_freeze_and_time_is_accounted(self):
        mutations = [lambda data: data["runs"][0].update(started_at="2025-12-31T00:00:00+00:00"),
                     lambda data: data["batches"][0].update(started_at="2025-12-31T00:00:00+00:00"),
                     lambda data: data["runs"][0]["attempts"][0].update(started_at="2025-12-31T00:00:00+00:00"),
                     lambda data: data["runs"][0].update(overhead_seconds=0),
                     lambda data: data["runs"][0]["attempts"][0].update(elapsed_seconds=1)]
        for mutate in mutations:
            observations = deepcopy(self.observations)
            mutate(observations)
            self.assert_inconclusive(self.manifest, observations)

    def test_execution_order_is_locked_but_receipt_list_order_is_irrelevant(self):
        manifest, observations = fixture()
        observations["runs"].reverse()
        report = self.assert_inconclusive(manifest, observations)
        self.assertIsNotNone(report["metrics"])  # Only synthetic-test eligibility blocks it.
        manifest["planned"][0], manifest["planned"][1] = manifest["planned"][1], manifest["planned"][0]
        lock(manifest, observations)
        report = self.assert_inconclusive(manifest, observations)
        self.assertIn("start order", " ".join(report["reasons"]))

    def test_cost_cap_and_unequal_common_review_are_not_silently_accepted(self):
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        manifest["caps"]["total_cost_usd"] = 1
        lock(manifest, observations)
        self.assert_inconclusive(manifest, observations)
        observations = deepcopy(self.observations)
        observations["batches"][0]["costs"]["common_review"] = 0
        self.assert_inconclusive(self.manifest, observations)

    def test_valid_observed_study_that_misses_a_gate_is_failed(self):
        observations = deepcopy(self.observations)
        for run in observations["runs"]:
            if run["arm"] == "workspace":
                run["attempts"][0]["model_cost_usd"] = 10
        report = evaluate_study(self.manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "failed")
        self.assertFalse(report["gates"]["lower_total_cost"])
        self.assertFalse(report["gates"]["cost_per_correct"])
        self.assertFalse(report["claim_eligible"])

    def test_passing_point_estimates_are_insufficient_when_paired_interval_crosses_zero(self):
        observations = deepcopy(self.observations)
        for run in observations["runs"]:
            batch = int(run["batch_id"].split("_")[-1])
            correct = batch >= 6 if run["arm"] == "source" else not 6 <= batch < 9
            run["correct"] = run["attempts"][0]["correct"] = correct
        report = evaluate_study(self.manifest, observations)
        self.assertAlmostEqual(report["metrics"]["source"]["accuracy"], 0.8)
        self.assertAlmostEqual(report["metrics"]["workspace"]["accuracy"], 0.9)
        self.assertTrue(report["gates"]["workspace_accuracy"])
        self.assertTrue(report["gates"]["accuracy_lift"])
        self.assertLessEqual(report["paired_intervals"]["accuracy_lift"]["lower"], 0)
        self.assertFalse(report["gates"]["accuracy_lift_supported"])
        self.assertEqual(report["statistical_acceptance"], "failed")

    def test_changed_phase_snapshots_preserve_batch_metrics_and_resampling(self):
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        add_phases(manifest, observations)
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed", report)
        self.assertEqual(report["phase_ids"], ["baseline", "refreshed"])
        self.assertEqual(report["task_phases"], {"initial/exposure": "baseline", "refreshed/exposure": "refreshed"})
        self.assertEqual(report["metrics"], self.report["metrics"])
        self.assertEqual(report["paired_intervals"], self.report["paired_intervals"])
        self.assertEqual(report["independent_batches"], 30)
        self.assertEqual(report["resampling"]["unit"], "whole paired independent batch")
        self.assertFalse(report["claim_eligible"])

    def test_refreshed_task_rejects_stale_initial_or_missing_source_receipt(self):
        manifest, original = deepcopy(self.manifest), deepcopy(self.observations)
        add_phases(manifest, original)
        baseline_hashes = {batch["id"]: batch["phase_snapshots"]["baseline"] for batch in manifest["batches"]}
        for case in ("stale", "missing", "null"):
            with self.subTest(case=case):
                observations = deepcopy(original)
                run = next(row for row in observations["runs"]
                           if row["task_id"] == "refreshed/exposure" and row["arm"] == "workspace")
                if case == "stale":
                    run["provenance"]["fixture_sha256"] = baseline_hashes[run["batch_id"]]
                elif case == "missing":
                    del run["provenance"]["fixture_sha256"]
                else:
                    run["provenance"]["fixture_sha256"] = None
                report = self.assert_inconclusive(manifest, observations)
                self.assertIn("phase source state", " ".join(report["reasons"]))

    def test_phase_mapping_requires_all_and_only_declared_phases_in_every_batch(self):
        base_manifest, base_observations = deepcopy(self.manifest), deepcopy(self.observations)
        add_phases(base_manifest, base_observations)
        # An implicit baseline still participates in the complete required set.
        del base_manifest["tasks"][0]["phase_id"]
        changes = [lambda batch: batch.pop("phase_snapshots"),
                   lambda batch: batch["phase_snapshots"].pop("baseline"),
                   lambda batch: batch["phase_snapshots"].pop("refreshed"),
                   lambda batch: batch["phase_snapshots"].update(unplanned=digest("unplanned")),
                   lambda batch: batch["phase_snapshots"].update(refreshed=None),
                   lambda batch: batch["phase_snapshots"].update(refreshed="not-a-digest")]
        for mutate in changes:
            manifest, observations = deepcopy(base_manifest), deepcopy(base_observations)
            mutate(manifest["batches"][-1])
            lock(manifest, observations)
            self.assert_inconclusive(manifest, observations)
        lock(base_manifest, base_observations)
        report = evaluate_study(base_manifest, base_observations)
        self.assertEqual(report["statistical_acceptance"], "passed")
        self.assertEqual(report["task_phases"]["initial/exposure"], "baseline")

    def test_invalid_phase_ids_and_explicit_baseline_require_a_mapping(self):
        for phase in (None, "", "   ", 3, False):
            manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
            manifest["tasks"][0]["phase_id"] = phase
            lock(manifest, observations)
            self.assert_inconclusive(manifest, observations)
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        manifest["tasks"][0]["phase_id"] = "baseline"
        lock(manifest, observations)
        self.assert_inconclusive(manifest, observations)

    def test_unphased_schema_retains_original_hash_binding_and_reports_baseline(self):
        self.assertEqual(self.report["phase_ids"], ["baseline"])
        self.assertEqual(self.report["task_phases"], {"task_0": "baseline", "task_1": "baseline"})
        manifest, observations = deepcopy(self.manifest), deepcopy(self.observations)
        batch = manifest["batches"][0]
        optional_hash = digest("explicit-baseline-source-state")
        batch["phase_snapshots"] = {"baseline": optional_hash}
        for run in observations["runs"]:
            if run["batch_id"] == batch["id"]:
                run["provenance"]["fixture_sha256"] = optional_hash
        lock(manifest, observations)
        report = evaluate_study(manifest, observations)
        self.assertEqual(report["statistical_acceptance"], "passed", report)
        self.assertEqual(report["metrics"], self.report["metrics"])
        # Optional mappings are checked even if task phases were left implicit.
        batch["phase_snapshots"]["extra"] = digest("extra")
        lock(manifest, observations)
        self.assert_inconclusive(manifest, observations)


if __name__ == "__main__":
    unittest.main()
