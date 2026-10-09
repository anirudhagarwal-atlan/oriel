"""Offline authored template consistency, not native or model benchmark evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.rystic_eval import build_phase_tasks
from contextbase.rystic_validation import capture_validation
from contextbase.sequential_study import PROVENANCE_SLOT, task_commitment
from contextbase.study_binding import _hash, bind_phase
from contextbase.study_templates import SPEC, compile_templates
from test_study_binding import binding_fixture, FakePair, fake_snapshot, rehash_plan, source_rows


class StudyTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan, _, _ = binding_fixture(self.root, "baseline")

    def test_compiler_is_pure_complete_deterministic_and_development_only(self):
        original = deepcopy(self.plan)
        with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native access")) as source, \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model access")) as model:
            compiled = compile_templates(self.plan)
            source.assert_not_called()
            model.assert_not_called()
        self.assertEqual(compile_templates(self.plan), compiled)
        self.assertEqual(self.plan, original)
        self.assertEqual(compiled["spec"], SPEC)
        self.assertEqual(compiled["evidence_origin"], "authored_intent")
        self.assertEqual(compiled["tasks"], self.plan["tasks"])
        self.assertEqual(len(compiled["public_templates"]), 12)
        self.assertEqual(len(compiled["evaluator_templates"]), 12)
        self.assertEqual(set(compiled["task_commitments"]), {task["id"] for task in self.plan["tasks"]})
        self.assertEqual(compiled["templates_sha256"], _hash({key: value for key, value in compiled.items() if key != "templates_sha256"}))
        for flag in ("held_out", "execution_authorized", "claim_eligible"):
            self.assertIs(compiled[flag], False)
        self.assertEqual((compiled["source_calls"], compiled["model_calls"]), (0, 0))
        text = json.dumps(compiled)
        self.assertNotIn("0"*64, text)
        self.assertNotIn("f"*64, text)
        for key in ("snapshot_receipt", "validation_receipt", "observed_source_sha256", "data_sha256"):
            self.assertNotIn(key, compiled)
        for public, private in zip(compiled["public_templates"], compiled["evaluator_templates"]):
            self.assertEqual(set(public), {"question_template", "answer_schema"})
            self.assertEqual(public["question_template"].count(PROVENANCE_SLOT), 1)
            self.assertNotIn("provenance", private["semantic_expected"])
            self.assertEqual(private["semantic_expected_sha256"], _hash(private["semantic_expected"]))

    def test_preobservation_commitments_match_actual_binder_for_every_phase(self):
        compiled = compile_templates(self.plan)
        templates = {private["id"]: (public, private) for public, private in zip(compiled["public_templates"], compiled["evaluator_templates"])}
        seen = []
        for phase in self.plan["recurring_profile"]["phase_order"]:
            capture = capture_validation(FakePair(source_rows(self.plan, phase)), self.plan["phases"][0]["intent_manifest"]["namespace"])
            packet = bind_phase(self.plan, phase, capture, self.root / (phase + "-bound.json"), snapshot=fake_snapshot(capture))
            for public, private in zip(packet["public_tasks"], packet["evaluator_tasks"]):
                identity = private["id"]
                seen.append(identity)
                template, semantic = templates[identity]
                provenance = json.dumps(private["expected"]["provenance"], sort_keys=True)
                self.assertEqual(template["question_template"].replace(PROVENANCE_SLOT, provenance), public["question"])
                self.assertEqual(template["answer_schema"], public["answer_schema"])
                self.assertEqual(semantic["semantic_expected"], {key: value for key, value in private["expected"].items() if key != "provenance"})
                self.assertEqual(compiled["task_commitments"][identity], task_commitment(public, private["expected"], self.plan["scorer_sha256"]))
                self.assertEqual(compiled["task_commitments"][identity]["scorer_sha256"], packet["task_artifacts"][identity]["scorer_sha256"])
        self.assertEqual(seen, [task["id"] for task in compiled["tasks"]])

    def test_changed_semantic_gold_remains_distinct_across_refresh_and_guards(self):
        compiled = compile_templates(self.plan)
        expected = {task["id"]: task["semantic_expected"] for task in compiled["evaluator_templates"]}
        self.assertNotEqual(expected["baseline/exposure_summary"], expected["refreshed/exposure_summary"])
        self.assertEqual(expected["baseline/guarded_exposure"]["status"], "answered")
        for phase in ("guard_a", "guard_b"):
            self.assertEqual(expected[phase + "/guarded_exposure"]["status"], "abstained")
            self.assertIsNone(expected[phase + "/guarded_exposure"]["result"])
            self.assertTrue(expected[phase + "/guarded_exposure"]["blockers"])
        self.assertNotEqual(expected["guard_a/guarded_exposure"]["blockers"], expected["guard_b/guarded_exposure"]["blockers"])

    def test_plan_hash_scorer_action_chain_coverage_and_claim_tampering_fail_before_write(self):
        for case in ("hash", "scorer", "intent", "action", "coverage", "held_out"):
            plan = deepcopy(self.plan)
            output = self.root / (case + ".json")
            if case == "hash": plan["created_at"] = "2026-01-01T00:00:00Z"
            if case == "scorer": plan["scorer_sha256"] = "a"*64
            if case == "intent": plan["phases"][1]["intent_manifest"]["accounts"][0]["name"] = "Changed synthetic label"
            if case == "action": plan["transitions"][0]["changes"][0]["after"]["state_name"] = "Changed synthetic state"
            if case == "coverage": plan["tasks"].pop()
            if case == "held_out": plan["held_out"] = True
            if case != "hash": rehash_plan(plan)
            with self.subTest(case=case), self.assertRaises(ValueError):
                compile_templates(plan, output)
            self.assertFalse(output.exists())

    def test_missing_duplicate_or_preexisting_provenance_placeholder_is_rejected(self):
        real_builder = build_phase_tasks
        for case in ("missing", "duplicate", "sentinel"):
            def malformed(*args, **kwargs):
                tasks = real_builder(*args, **kwargs)
                provenance = json.dumps(tasks[0]["expected"]["provenance"], sort_keys=True)
                if case == "missing": tasks[0]["question"] = tasks[0]["question"].replace(provenance, "")
                if case == "duplicate": tasks[0]["question"] += provenance
                if case == "sentinel": tasks[0]["question"] += PROVENANCE_SLOT
                return tasks
            with self.subTest(case=case), patch("contextbase.study_templates.build_phase_tasks", side_effect=malformed), self.assertRaises(ValueError):
                compile_templates(self.plan)

    def test_only_top_level_gold_provenance_is_removed(self):
        def extended(*args, **kwargs):
            tasks = build_phase_tasks(*args, **kwargs)
            tasks[0]["expected"]["nested"] = {"provenance": {"semantic_rule": "retain this literal"}}
            return tasks
        with patch("contextbase.study_templates.build_phase_tasks", side_effect=extended):
            compiled = compile_templates(self.plan)
        self.assertEqual(compiled["evaluator_templates"][0]["semantic_expected"]["nested"],
                         {"provenance": {"semantic_rule": "retain this literal"}})

    def test_two_provenance_probes_detect_leaks_into_question_schema_or_semantic_gold(self):
        for case in ("question", "schema", "gold"):
            def leaking(*args, **kwargs):
                tasks = build_phase_tasks(*args, **kwargs)
                source_hash = tasks[0]["expected"]["provenance"]["data_sha256"]
                if case == "question": tasks[0]["question"] += " Additional source hash: " + source_hash
                if case == "schema": tasks[0]["answer_schema"]["description"] = source_hash
                if case == "gold": tasks[0]["expected"]["leaked_source_hash"] = source_hash
                return tasks
            output = self.root / ("leaked-" + case + ".json")
            with self.subTest(case=case), patch("contextbase.study_templates.build_phase_tasks", side_effect=leaking), \
                 self.assertRaisesRegex(ValueError, "Provenance leaked"):
                compile_templates(self.plan, output)
            self.assertFalse(output.exists())

    def test_optional_output_is_exclusive_and_preserves_exact_compiled_values(self):
        output = self.root / "nested" / "templates.json"
        compiled = compile_templates(self.plan, output)
        self.assertEqual(json.loads(output.read_text()), compiled)
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            compile_templates(self.plan, output)
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
