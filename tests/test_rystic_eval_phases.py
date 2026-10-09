"""Authored phase/scorer tests only: no simulator execution or model evidence."""
from copy import deepcopy
import json
import unittest

from contextbase.rystic_eval import (
    build_guarded_task, build_phase_tasks, build_tasks, score_answer,
)
from test_rystic_eval import fixture, SNAPSHOT


def validation_receipt():
    return {**SNAPSHOT, "snapshot_id": "source-state-aaaaaaaaaaaaaaaa",
            "receipt_kind": "source_state_validation"}


class RysticEvalPhaseTests(unittest.TestCase):
    def test_phase_wrapper_preserves_existing_pilot_contract(self):
        manifest = fixture()
        pilot = build_tasks(manifest, SNAPSHOT)
        phased = build_phase_tasks(manifest, SNAPSHOT, phase_id="baseline")
        self.assertEqual(len(pilot), 4)
        for before, after in zip(pilot, phased):
            self.assertEqual(set(before), {"id", "question", "answer_schema", "expected"})
            self.assertEqual(after["id"], "baseline/" + before["id"])
            self.assertEqual(after["phase_id"], "baseline")
            for key in ("question", "answer_schema", "expected"):
                self.assertEqual(before[key], after[key])

    def test_refreshed_gold_requires_changed_data_not_only_new_provenance(self):
        baseline = fixture()
        refreshed = deepcopy(baseline)
        refreshed["accounts"][0]["charges"][0]["refund_cents"] = 4333
        refreshed["accounts"][0]["issues"][0]["state_type"] = "completed"
        new_receipt = {**SNAPSHOT, "snapshot_id": "rystic-bbbbbbbbbbbbbbbb", "data_sha256": "b" * 64}
        old_tasks = {task["id"].split("/", 1)[1]: task for task in build_phase_tasks(baseline, SNAPSHOT, phase_id="baseline")}
        tasks = {task["id"].split("/", 1)[1]: task for task in build_phase_tasks(refreshed, new_receipt, phase_id="refreshed")}
        summary = tasks["exposure_summary"]["expected"]
        self.assertEqual(summary["account_count"], 1)
        self.assertEqual(summary["qualifying_issue_count"], 1)
        self.assertEqual(summary["net_collected_usd_cents"], 5668)
        for family in ("qualifying_accounts", "exposure_summary", "payment_controls", "single_account"):
            stale = deepcopy(old_tasks[family]["expected"])
            stale["provenance"] = deepcopy(tasks[family]["expected"]["provenance"])
            self.assertFalse(score_answer(stale, tasks[family]["expected"])["correct"])
            self.assertTrue(score_answer(deepcopy(tasks[family]["expected"]), tasks[family]["expected"])["correct"])
        current_with_old_receipt = deepcopy(summary)
        current_with_old_receipt["provenance"] = old_tasks["exposure_summary"]["expected"]["provenance"]
        self.assertFalse(score_answer(current_with_old_receipt, summary)["correct"])

    def test_guarded_positive_control_must_answer_and_preserves_unassigned_owner(self):
        task = build_guarded_task(fixture(), validation_receipt(), phase_id="baseline")
        self.assertEqual(task["id"], "baseline/guarded_exposure")
        self.assertEqual(task["receipt_kind"], "source_state_validation")
        self.assertEqual(task["expected"]["status"], "answered")
        self.assertEqual(task["expected"]["result"], {
            "account_count": 1, "qualifying_issue_count": 2, "net_collected_usd_cents": 6668,
        })
        self.assertEqual(task["expected"]["blockers"], [])
        always_abstain = {**deepcopy(task["expected"]), "status": "abstained", "result": None,
                          "blockers": [{"source": "stripe.customers", "record_id": "cus_1",
                                        "field": "metadata.is_internal", "reason": "missing_required_input"}]}
        self.assertFalse(score_answer(always_abstain, task["expected"])["correct"])

    def test_duplicate_logical_mapping_requires_all_actual_source_record_ids(self):
        manifest = fixture()
        manifest["accounts"][1]["account_id"] = manifest["accounts"][0]["account_id"]
        task = build_guarded_task(manifest, validation_receipt(), phase_id="guard_a")
        self.assertEqual(task["expected"]["status"], "abstained")
        self.assertIsNone(task["expected"]["result"])
        self.assertEqual(task["expected"]["blockers"], [
            {"source": "stripe.customers", "record_id": "cus_1", "field": "metadata.account_id", "reason": "ambiguous_identity"},
            {"source": "stripe.customers", "record_id": "cus_2", "field": "metadata.account_id", "reason": "ambiguous_identity"},
        ])
        for mutation in (lambda answer: answer["blockers"].clear(),
                         lambda answer: answer["blockers"].pop(),
                         lambda answer: answer["blockers"][0].update(record_id="customer_001"),
                         lambda answer: answer["blockers"][0].update(reason="missing_required_input"),
                         lambda answer: answer["blockers"].append(deepcopy(answer["blockers"][0]))):
            wrong = deepcopy(task["expected"])
            mutation(wrong)
            self.assertFalse(score_answer(wrong, task["expected"])["correct"])
        reordered = deepcopy(task["expected"])
        reordered["blockers"].reverse()
        self.assertTrue(score_answer(reordered, task["expected"])["correct"])

    def test_missing_or_null_policy_requires_specific_evidence_even_on_internal_customer(self):
        for representation in ("missing", "null"):
            with self.subTest(representation=representation):
                manifest = fixture()
                if representation == "missing":
                    del manifest["accounts"][2]["is_internal"]
                else:
                    manifest["accounts"][2]["is_internal"] = None
                task = build_guarded_task(manifest, validation_receipt(), phase_id="guard_b")
                self.assertEqual(task["expected"]["status"], "abstained")
                self.assertEqual(task["expected"]["blockers"], [{
                    "source": "stripe.customers", "record_id": "cus_3", "field": "metadata.is_internal",
                    "reason": "missing_required_input",
                }])
                wrong = deepcopy(task["expected"])
                wrong["status"] = "answered"
                wrong["blockers"] = []
                wrong["result"] = {"account_count": 1, "qualifying_issue_count": 2, "net_collected_usd_cents": 6668}
                self.assertFalse(score_answer(wrong, task["expected"])["correct"])

    def test_multiple_guarded_defects_require_complete_blocker_set(self):
        manifest = fixture()
        manifest["accounts"][1]["account_id"] = manifest["accounts"][0]["account_id"]
        manifest["accounts"][1]["is_internal"] = None
        expected = build_guarded_task(manifest, validation_receipt(), phase_id="guard_b")["expected"]
        self.assertEqual(len(expected["blockers"]), 3)
        incomplete = deepcopy(expected)
        incomplete["blockers"] = [blocker for blocker in incomplete["blockers"] if blocker["reason"] != "missing_required_input"]
        self.assertFalse(score_answer(incomplete, expected)["correct"])

    def test_zero_result_is_an_answer_not_abstention(self):
        manifest = fixture()
        for account in manifest["accounts"]:
            account["is_internal"] = True
        task = build_guarded_task(manifest, validation_receipt(), phase_id="refreshed")
        self.assertEqual(task["expected"]["status"], "answered")
        self.assertEqual(task["expected"]["result"], {
            "account_count": 0, "qualifying_issue_count": 0, "net_collected_usd_cents": 0,
        })
        self.assertEqual(task["expected"]["blockers"], [])

    def test_public_guard_contract_does_not_reveal_outcome_or_phase_category(self):
        manifest = fixture()
        ambiguous = deepcopy(manifest)
        ambiguous["accounts"][1]["account_id"] = ambiguous["accounts"][0]["account_id"]
        missing = deepcopy(manifest)
        missing["accounts"][0]["is_internal"] = None
        # Identical placeholder receipts isolate formatting: no live states or
        # actual source hashes are asserted by these unit-test arrays.
        tasks = [build_guarded_task(state, validation_receipt(), phase_id=phase)
                 for state, phase in ((manifest, "baseline"), (ambiguous, "guard_a"), (missing, "guard_b"))]
        for task in tasks[1:]:
            self.assertEqual(task["question"], tasks[0]["question"])
            self.assertEqual(task["answer_schema"], tasks[0]["answer_schema"])
        public = json.dumps({key: tasks[0][key] for key in ("question", "answer_schema")})
        for hidden in ("6668", "cus_1", "cus_2", "customer_001", "guard_a", "guard_b"):
            self.assertNotIn(hidden, public)

    def test_unrelated_corruption_remains_invalid_even_when_guarded_defect_exists(self):
        mutations = [lambda manifest: manifest.update(status="incomplete"),
                     lambda manifest: manifest["accounts"][0].pop("account_id"),
                     lambda manifest: manifest["accounts"][0].update(is_internal="false"),
                     lambda manifest: manifest["accounts"][1].update(customer_id="cus_1"),
                     lambda manifest: manifest["accounts"][0]["charges"][0].pop("disputed"),
                     lambda manifest: manifest["accounts"][0]["charges"][0].update(amount_cents=10001.0),
                     lambda manifest: manifest["accounts"][0]["issues"][0].pop("state_type")]
        for mutation in mutations:
            manifest = fixture()
            manifest["accounts"][3]["is_internal"] = None
            mutation(manifest)
            with self.assertRaises(ValueError):
                build_guarded_task(manifest, validation_receipt(), phase_id="guard_a")

    def test_native_receipt_id_prefix_does_not_disclose_guard_outcome(self):
        valid_manifest = fixture()
        blocked_manifest = deepcopy(valid_manifest)
        blocked_manifest["accounts"][0]["is_internal"] = None
        # Equal placeholder hashes isolate ID-format leakage; actual captures
        # of different states must have independently verified distinct hashes.
        valid_receipt = {**validation_receipt(), "snapshot_id": "rystic-" + "a" * 16}
        blocked_receipt = {**validation_receipt(), "snapshot_id": "source-state-" + "a" * 16}
        before = deepcopy((valid_receipt, blocked_receipt))
        valid = build_guarded_task(valid_manifest, valid_receipt, phase_id="baseline")
        blocked = build_guarded_task(blocked_manifest, blocked_receipt, phase_id="guard_b")
        self.assertEqual(valid["question"], blocked["question"])
        self.assertEqual(valid["answer_schema"], blocked["answer_schema"])
        self.assertEqual(valid["expected"]["provenance"], blocked["expected"]["provenance"])
        self.assertEqual(valid["expected"]["provenance"]["snapshot_id"], "source-validation-" + "a" * 16)
        self.assertNotEqual(valid["expected"]["status"], blocked["expected"]["status"])
        self.assertEqual((valid_receipt, blocked_receipt), before)
        stale = deepcopy(valid["expected"])
        stale["provenance"]["snapshot_id"] = valid_receipt["snapshot_id"]
        self.assertFalse(score_answer(stale, valid["expected"])["correct"])

    def test_guard_requires_source_validation_receipt_and_never_certifies_import(self):
        receipts = [SNAPSHOT,
                    {**validation_receipt(), "receipt_kind": "workspace_snapshot"},
                    {**validation_receipt(), "refresh_mode": "full_cohort_replace"},
                    {**validation_receipt(), "data_sha256": "invalid"},
                    {**validation_receipt(), "snapshot_id": None},
                    {**validation_receipt(), "namespace": "wrong_fixture"}]
        for receipt in receipts:
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                build_guarded_task(fixture(), receipt, phase_id="guard_a")
        with self.assertRaises(ValueError):
            build_phase_tasks(fixture(), validation_receipt(), phase_id="baseline")
        with self.assertRaises(ValueError):
            build_phase_tasks(fixture(), {}, phase_id="baseline")

    def test_builders_leave_input_state_untouched_and_validate_opaque_phase_ids(self):
        manifest, receipt = fixture(), validation_receipt()
        manifest["accounts"][1]["account_id"] = manifest["accounts"][0]["account_id"]
        original, original_receipt = deepcopy(manifest), deepcopy(receipt)
        result = build_guarded_task(manifest, receipt, phase_id="stage_17")
        result["expected"]["blockers"][0]["record_id"] = "changed"
        self.assertEqual(manifest, original)
        self.assertEqual(receipt, original_receipt)
        for phase in ("", "BadPhase", "bad/phase", "bad phase", None, True, "a" * 65):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                build_guarded_task(manifest, receipt, phase_id=phase)


if __name__ == "__main__":
    unittest.main()
