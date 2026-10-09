from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from contextbase.rystic_eval import build_tasks
from contextbase.study_phases import prepare_phases, recurring_tasks
from test_rystic_eval import fixture


def phase_fixture():
    manifest = fixture()
    for account in manifest["accounts"]:
        for issue in account["issues"]:
            issue["state_id"] = "state_" + issue["state_type"]
            issue["state_name"] = issue["state_type"].title()
    return manifest


class StudyPhaseTests(unittest.TestCase):
    def test_preparation_retains_inputs_and_separates_intent_from_observation(self):
        original = phase_fixture()
        untouched = deepcopy(original)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phases.json"
            plan = prepare_phases(original, path)
            self.assertEqual(json.loads(path.read_text()), plan)
            self.assertEqual(original, untouched)
            self.assertEqual(plan["model_calls"], 0)
            self.assertEqual(plan["source_calls"], 0)
            self.assertFalse(plan["execution_authorized"])
            self.assertFalse(plan["claim_eligible"])
            self.assertFalse(plan["held_out"])
            self.assertEqual(plan["evidence_origin"], "authored_intent")
            self.assertEqual(len(plan["tasks"]), 12)
            for phase in plan["phases"]:
                self.assertIsNone(phase["observed_source_sha256"])
                self.assertIsNone(phase["native_receipt_sha256"])
            with self.assertRaises(FileExistsError): prepare_phases(original, path)

    def test_refresh_changes_gold_and_sequential_guards_restore_previous_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = prepare_phases(phase_fixture(), Path(directory) / "plan.json")
        phases = {phase["id"]: phase for phase in plan["phases"]}
        self.assertEqual(phases["baseline"]["expected_guard"]["result"]["net_collected_usd_cents"], 6668)
        self.assertEqual(phases["refreshed"]["expected_guard"]["result"]["account_count"], 0)
        self.assertEqual(phases["guard_a"]["expected_ingestion"], "refuse_invalid_source")
        self.assertEqual(len(phases["guard_a"]["expected_guard"]["blockers"]), 2)
        self.assertEqual(plan["source_issue_account_ids"]["issue_7"], "customer_002")
        ambiguous_second = next(account for account in phases["guard_a"]["intent_manifest"]["accounts"] if account["customer_id"] == "cus_2")
        self.assertEqual(ambiguous_second["account_id"], "customer_001")
        self.assertEqual([row["reason"] for row in phases["guard_b"]["expected_guard"]["blockers"]], ["missing_required_input"])
        self.assertEqual(len(plan["transitions"]), 3)
        current = deepcopy(phases["baseline"]["intent_manifest"])
        for transition in plan["transitions"]:
            for action in transition["changes"]:
                if action["source"] == "linear.issues":
                    record = next(issue for account in current["accounts"] for issue in account["issues"] if issue["issue_id"] == action["record_id"])
                else:
                    record = next(account for account in current["accounts"] if account["customer_id"] == action["record_id"])
                self.assertEqual({key: record[key] for key in action["before"]}, action["before"])
                record.update(action["after"])
            self.assertEqual(current, phases[transition["to_phase"]]["intent_manifest"])
        self.assertEqual(len({phase["intent_sha256"] for phase in phases.values()}), 4)

    def test_valid_guard_controls_and_abstention_tasks_are_both_required(self):
        tasks = recurring_tasks()
        guarded = [task for task in tasks if task["family"] == "guarded_exposure"]
        self.assertEqual([task["response_kind"] for task in guarded], ["answer", "answer", "abstain", "abstain"])
        self.assertEqual(len({task["id"] for task in tasks}), 12)

    def test_invalid_input_never_creates_plan(self):
        def no_qualified(manifest):
            for account in manifest["accounts"]: account["is_internal"] = True
        def missing_completed_id(manifest):
            for account in manifest["accounts"]:
                for issue in account["issues"]:
                    if issue["state_type"] == "completed": issue.pop("state_id")
        for change in (no_qualified, missing_completed_id, lambda manifest: manifest.update(synthetic=False)):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                manifest = phase_fixture(); change(manifest)
                path = Path(directory) / "plan.json"
                with self.assertRaises(ValueError): prepare_phases(manifest, path)
                self.assertFalse(path.exists())
