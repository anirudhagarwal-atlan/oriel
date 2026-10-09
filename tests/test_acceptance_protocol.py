"""Fabricated review/provisioning inputs test contracts, never performance."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_intent import prepare_intent
from contextbase.acceptance_protocol import (freeze_protocol, validate_protocol, EVENT_RULES, TARGETS)
from contextbase.study_binding import _hash
from test_acceptance_intent import intent_fixture


def protocol_inputs():
    allocation, first = intent_fixture()
    intents = [first] + [intent_fixture(allocation, row["batch_id"])[1] for row in allocation["fixture_batches"][1:]]
    settings = {"model_id": "synthetic-test-model", "context_sha256": _hash("synthetic-reviewed-context"),
        "bootstrap": {"seed": 7, "replicates": 2000, "confidence": 0.95},
        "budgets": {"total_study_usd": 100, "comparison_usd": 90,
                    "max_model_calls": len(allocation["schedule"]) * 2, "max_model_turns_per_run": 8,
                    "run_seconds": 180, "study_seconds": 100000},
        "power": {"spec": "contextbase.power-review/v1", "author": "synthetic-author", "reviewer": "synthetic-reviewer",
            "reviewed_at": allocation["created_at"], "allocation_sha256": allocation["allocation_sha256"],
            "analysis_sha256": _hash("fabricated-test-analysis"), "analysis_method": "Fabricated paired-cluster planning test",
            "assumptions": {key: "Fabricated test assumption; no observed power evidence" for key in (
                "paired_batch_dependence", "accuracy_effect_and_variance", "cost_effect_and_variance",
                "time_effect_and_variance", "failure_rates", "basis")},
            "required_independent_batches": 30, "target_power": 0.8, "estimated_joint_power": 0.85,
            "review_decision": "approved", "no_heldout_outcomes_used": True,
            "targets": deepcopy(TARGETS), "sensitivity_and_limits": "Fabricated review, not scientific power evidence"},
        "accounting_policy": {"spec": "contextbase.accounting-review/v1", "author": "synthetic-author", "reviewer": "synthetic-reviewer",
            "reviewed_at": allocation["created_at"], "allocation_sha256": allocation["allocation_sha256"],
            "evidence_sha256": _hash("fabricated-test-accounting"), "fixture_generation_treatment": "reported_apparatus",
            "event_rules": deepcopy(EVENT_RULES), "unknown_cost_policy": "inconclusive",
            "holdout_change_policy": "retire_and_allocate_fresh", "review_decision": "approved",
            **{key: "Fabricated test allocation/rate policy, not measured cost or independent review" for key in (
                "rate_basis", "shared_allocation", "deduplication", "cold_clock", "cross_arm_waiting",
                "cache_freshness", "capture_ownership")}}}
    return allocation, intents, settings


def protocol_fixture():
    return freeze_protocol(*protocol_inputs())


class AcceptanceProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allocation, cls.intents, cls.settings = protocol_inputs()

    def fresh(self):
        return deepcopy((self.allocation, self.intents, self.settings))

    def test_complete_freeze_retains_all_evidence_and_never_authorizes_execution(self):
        allocation, intents, settings = self.fresh()
        original = deepcopy((allocation, intents, settings))
        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No live source")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            output = Path(directory) / "protocol.json"
            protocol = freeze_protocol(allocation, intents, settings, output)
            validate_protocol(protocol)
            self.assertEqual(json.loads(output.read_text()), protocol)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                freeze_protocol(allocation, intents, settings, output)
        self.assertEqual((allocation, intents, settings), original)
        self.assertEqual(len(protocol["batches"]), 30)
        self.assertEqual(protocol["planned"], allocation["schedule"])
        self.assertEqual(protocol["tasks"], allocation["tasks"])
        self.assertEqual(protocol["evidence_origin"], "synthetic_test")
        self.assertAlmostEqual(protocol["provisioning_spend_usd"], 0.9)
        self.assertFalse(protocol["claim_eligible"])
        self.assertFalse(protocol["execution_authorized"])
        self.assertEqual((protocol["model_calls"], protocol["source_calls"]), (0, 0))
        self.assertNotIn("phase_snapshots", protocol["batches"][0])

    def test_exact_batch_coverage_and_order_are_required(self):
        for change in (lambda rows: rows.pop(), lambda rows: rows.reverse(), lambda rows: rows.append(deepcopy(rows[0]))):
            allocation, intents, settings = self.fresh()
            change(intents)
            with self.assertRaisesRegex(ValueError, "every allocated batch"):
                freeze_protocol(allocation, intents, settings)

    def test_rehashed_task_flags_settings_or_runtime_edits_do_not_bypass_validation(self):
        protocol = freeze_protocol(*self.fresh())
        changes = (lambda value: value.update(execution_authorized=True),
                   lambda value: value["batches"][0]["task_commitments"].pop(next(iter(value["batches"][0]["task_commitments"]))),
                   lambda value: value["runtime"].update(python="different"),
                   lambda value: value["planned"].reverse(),
                   lambda value: value["intents"][0]["phase_plan"].update(held_out=True))
        for change in changes:
            altered = deepcopy(protocol)
            change(altered)
            altered["protocol_sha256"] = _hash({key: value for key, value in altered.items() if key != "protocol_sha256"})
            with self.assertRaises(ValueError):
                validate_protocol(altered)

    def test_power_and_accounting_require_content_and_exact_predeclared_targets(self):
        changes = (
            lambda s: s["power"].update(reviewer="SYNTHETIC-AUTHOR"),
            lambda s: s["power"]["assumptions"].pop("failure_rates"),
            lambda s: s["power"].update(required_independent_batches=31),
            lambda s: s["power"].update(estimated_joint_power=0.7),
            lambda s: s["power"]["targets"].update(cost_per_correct_ratio_maximum=0.9),
            lambda s: s["power"].update(no_heldout_outcomes_used=False),
            lambda s: s["accounting_policy"].update(capture_ownership=""),
            lambda s: s["accounting_policy"]["event_rules"]["workspace_ingestion"].update(allocation="source"),
            lambda s: s["accounting_policy"].update(unknown_cost_policy="zero"),
            lambda s: s["accounting_policy"].update(holdout_change_policy="continue"),
        )
        for change in changes:
            allocation, intents, settings = self.fresh()
            change(settings)
            with self.assertRaises(ValueError):
                freeze_protocol(allocation, intents, settings)

    def test_unknown_provisioning_costs_and_inadequate_all_in_budgets_are_rejected(self):
        allocation, intents, settings = self.fresh()
        first = intents[0]
        provisioning = deepcopy(first["provisioning"])
        provisioning["costs_usd"]["runtime"] = None
        intents[0] = prepare_intent(allocation, first["batch_id"], first["manifest"], first["baseline_capture"], provisioning)
        with self.assertRaisesRegex(ValueError, "unknown"):
            freeze_protocol(allocation, intents, settings)
        allocation, intents, settings = self.fresh()
        settings["budgets"]["total_study_usd"] = settings["budgets"]["comparison_usd"]
        with self.assertRaisesRegex(ValueError, "actual provisioning spend"):
            freeze_protocol(allocation, intents, settings)
        settings["budgets"]["total_study_usd"] = 100
        settings["budgets"]["max_model_calls"] = 1
        with self.assertRaisesRegex(ValueError, "Model-call cap"):
            freeze_protocol(allocation, intents, settings)

    def test_fixture_cost_policy_cannot_be_written_after_provisioning(self):
        allocation, intents, settings = self.fresh()
        settings["accounting_policy"]["reviewed_at"] = intents[0]["provisioning"]["finished_at"]
        with self.assertRaisesRegex(ValueError, "before provisioning"):
            freeze_protocol(allocation, intents, settings)
        settings["accounting_policy"]["reviewed_at"] = allocation["created_at"]
        settings["power"]["reviewed_at"] = (datetime.now().astimezone() + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "precede protocol freeze"):
            freeze_protocol(allocation, intents, settings)

    def test_reused_environments_and_mixed_origins_are_not_independent_inputs(self):
        allocation, intents, settings = self.fresh()
        second = intents[1]
        provisioning = deepcopy(second["provisioning"])
        provisioning["environment_id"] = intents[0]["provisioning"]["environment_id"]
        intents[1] = prepare_intent(allocation, second["batch_id"], second["manifest"], second["baseline_capture"], provisioning)
        with self.assertRaisesRegex(ValueError, "reuse environment"):
            freeze_protocol(allocation, intents, settings)
        allocation, intents, settings = self.fresh()
        first = intents[0]
        intents[0] = prepare_intent(allocation, first["batch_id"], first["manifest"], first["baseline_capture"],
                                    first["provisioning"], evidence_origin="recorded_native")
        with self.assertRaisesRegex(ValueError, "mix synthetic"):
            freeze_protocol(allocation, intents, settings)


if __name__ == "__main__":
    unittest.main()
