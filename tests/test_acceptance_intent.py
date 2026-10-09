"""Fabricated allocation/provisioning readbacks; never native acceptance evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_intent import PROVISIONING_SPEC, SPEC, prepare_intent, validate_allocation, validate_intent
from contextbase.rystic import PINS
from contextbase.rystic_recipe import seed_recipe
from contextbase.rystic_validation import capture_validation
from contextbase.study_allocation import allocate_study
from contextbase.study_binding import _hash
from test_rystic_recipe import FakePair as FakeRecipePair
from test_study_binding import FakePair as FakeCapturePair, source_rows


def rehash(value, field):
    value[field] = _hash({key: item for key, item in value.items() if key != field})


def intent_inputs(allocation=None, batch_id=None, *, assigned_owners=True):
    """Reusable in-memory fixture for intent/protocol/binding contract tests."""
    if allocation is None:
        allocation = allocate_study(None, master_seed=91, development_seeds=[7, 31],
                                    account_counts=[12], repeats=1, workload="recurring_v1")
        allocation["created_at"] = "2026-01-01T00:00:00+00:00"
        rehash(allocation, "allocation_sha256")
    batch_id = batch_id or allocation["fixture_batches"][0]["batch_id"]
    index, fixture = next((index, fixture) for index, fixture in enumerate(allocation["fixture_batches"]) if fixture["batch_id"] == batch_id)
    pair = FakeRecipePair()
    pair.verify = lambda: {source: {"product": product, "version": version} for source, (product, version) in PINS.items()}
    if not assigned_owners:
        pair.linear.users = []
    manifest = seed_recipe(pair, account_count=fixture["accounts"], seed=fixture["seed"], variant=fixture["variant"])
    baseline_plan = {"phases": [{"id": "baseline", "intent_manifest": manifest}],
                     "source_issue_account_ids": {issue["issue_id"]: account["account_id"]
                         for account in manifest["accounts"] for issue in account["issues"]}}
    capture = capture_validation(FakeCapturePair(source_rows(baseline_plan, "baseline")), manifest["namespace"])
    allocated = datetime.fromisoformat(allocation["created_at"].replace("Z", "+00:00"))
    stamp = lambda seconds: (allocated + timedelta(seconds=100 + 100*index + seconds)).isoformat()
    capture["receipt"].update(observed_at=stamp(20), source_read_seconds=1)
    provisioning = {"spec": PROVISIONING_SPEC, "allocation_sha256": allocation["allocation_sha256"], "batch_id": batch_id,
        "environment_id": "synthetic-native-pair-" + batch_id, "started_at": stamp(0), "finished_at": stamp(30),
        "evidence_sha256": _hash(["test-provisioning", batch_id]), "costs_usd": {"fixture_generation": 0.01, "runtime": 0.02},
        "isolation_review": {"reviewer": "synthetic_isolation_reviewer", "reviewed_at": stamp(31), "method": "separate_native_pair",
                             "evidence_sha256": _hash(["test-isolation-review", batch_id])},
        "holdout_review": {"reviewer": "synthetic_holdout_reviewer", "reviewed_at": stamp(32), "no_tuning": True,
                           "development_seeds_sha256": _hash(allocation["development_seeds"])}}
    return deepcopy(allocation), batch_id, manifest, capture, provisioning


def intent_fixture(allocation=None, batch_id=None, *, evidence_origin="synthetic_test", unknown_cost=False):
    """Return allocation and fabricated intent; this does not create native fixtures."""
    values = intent_inputs(allocation, batch_id)
    if unknown_cost:
        values[-1]["costs_usd"]["runtime"] = None
    return values[0], prepare_intent(*values, evidence_origin=evidence_origin)


class AcceptanceIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = intent_inputs()
        cls.intent = prepare_intent(*cls.inputs)

    def fresh(self):
        return deepcopy(self.inputs)

    def test_fresh_intent_revalidates_deterministically_without_native_or_model_calls(self):
        values = self.fresh()
        original = deepcopy(values)
        with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native calls")) as source, \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model calls")) as model:
            intent = prepare_intent(*values)
            self.assertEqual(validate_intent(intent, values[0]), intent)
            source.assert_not_called()
            model.assert_not_called()
        self.assertEqual(values, original)
        self.assertEqual(intent, self.intent)
        self.assertEqual(intent["spec"], SPEC)
        self.assertEqual(intent["created_at"], values[-1]["finished_at"])
        self.assertEqual(intent["phase_plan"]["created_at"], intent["created_at"])
        self.assertEqual(len(intent["templates"]["public_templates"]), 12)
        self.assertFalse(intent["claim_eligible"])
        self.assertFalse(intent["execution_authorized"])
        self.assertEqual((intent["source_calls"], intent["model_calls"]), (0, 0))
        self.assertFalse(intent["phase_plan"]["held_out"])
        self.assertFalse(intent["templates"]["held_out"])
        self.assertNotIn("held_out", intent)
        self.assertNotIn("__private_owner_probe__", json.dumps(intent))
        self.assertEqual(intent["identity_map_sha256"], _hash(intent["identity_map"]))
        self.assertEqual(intent["baseline_capture_sha256"], _hash(values[3]))

    def test_allocator_replay_refuses_rehashed_seed_schedule_and_parameter_changes(self):
        for case in ("seed", "schedule", "counts", "workload", "unknown", "missing_parameters", "development_seed"):
            allocation = deepcopy(self.inputs[0])
            if case == "seed": allocation["fixture_batches"][0]["seed"] += 1
            if case == "schedule": allocation["schedule"][0], allocation["schedule"][1] = allocation["schedule"][1], allocation["schedule"][0]
            if case == "counts": allocation["account_counts"] = [13]
            if case == "workload": allocation["workload"] = "warm_v1"
            if case == "unknown": allocation["post_selection"] = True
            if case == "missing_parameters": del allocation["account_counts"]
            if case == "development_seed": allocation["development_seeds"].append(allocation["fixture_batches"][0]["seed"])
            rehash(allocation, "allocation_sha256")
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_allocation(allocation)

    def test_allocated_seed_count_variant_and_seeded_semantics_must_match(self):
        for case in ("seed", "count", "variant", "amount", "order", "title", "flag"):
            values = self.fresh()
            manifest = values[2]
            if case == "seed": manifest["seed"] = 7
            if case == "count": manifest["account_count"] += 1
            if case == "variant": manifest["variant"] = "demo_v1"
            if case == "amount": manifest["accounts"][0]["charges"][0]["amount_cents"] += 1
            if case == "order": manifest["accounts"].reverse()
            if case == "title": manifest["accounts"][0]["issues"][0]["title"] += " changed"
            if case == "flag": manifest["accounts"][0]["is_internal"] = not manifest["accounts"][0]["is_internal"]
            with self.subTest(case=case), self.assertRaises(ValueError):
                prepare_intent(*values)

    def test_full_readback_must_preserve_physical_identity_owner_and_source_semantics(self):
        for case in ("customer_id", "issue_id", "state_id", "owner", "mapping", "raw_missing", "normalized_tamper", "wrong_pin"):
            values = self.fresh()
            manifest, capture = values[2], values[3]
            issue = next(issue for account in manifest["accounts"] for issue in account["issues"] if issue["owner_id"])
            if case == "customer_id": manifest["accounts"][0]["customer_id"] = "unrelated_customer"
            if case == "issue_id": issue["issue_id"] = "unrelated_issue"
            if case == "state_id":
                for account in manifest["accounts"]:
                    for row in account["issues"]:
                        if row["state_type"] == "started": row["state_id"] = "different_started_state"
            if case == "owner": issue["owner_id"], issue["owner_name"] = "different_owner", "Different synthetic owner"
            if case == "mapping": capture["observed_source"]["rows"]["stripe.customers"][0]["metadata"]["account_id"] = "unrelated_account"
            if case == "raw_missing": capture["observed_source"]["rows"]["linear.issues"].pop()
            if case == "normalized_tamper": capture["tables"]["stripe.charges"][0]["amount_cents"] += 1
            if case == "wrong_pin": capture["receipt"]["simulators"]["stripe"]["version"] = "unsupported"
            with self.subTest(case=case), self.assertRaises(ValueError):
                prepare_intent(*values)

    def test_guaranteed_unassigned_controls_and_empty_roster_behavior_are_retained(self):
        empty = intent_inputs(assigned_owners=False)
        self.assertEqual(prepare_intent(*empty)["identity_map"]["owners"], {})
        values = self.fresh()
        unassigned = next(issue for account in values[2]["accounts"] for issue in account["issues"] if issue["owner_id"] is None)
        unassigned.update(owner_id="assigned_control", owner_name="Assigned synthetic control")
        with self.assertRaisesRegex(ValueError, "unassigned owner control"):
            prepare_intent(*values)
        values = self.fresh()
        assigned = next(issue for account in values[2]["accounts"] for issue in account["issues"] if issue["owner_id"] is not None)
        assigned.update(owner_id=None, owner_name=None)
        with self.assertRaisesRegex(ValueError, "uniformly empty or populated"):
            prepare_intent(*values)

    def test_review_and_provisioning_chronology_prevent_backdated_or_unreviewed_intent(self):
        for case in ("early_start", "capture_before_start", "early_finish", "early_review", "wrong_allocation", "wrong_batch", "wrong_method", "tuned", "wrong_dev_hash"):
            values = self.fresh()
            allocation, _, _, capture, provisioning = values
            if case == "early_start": provisioning["started_at"] = "2025-01-01T00:00:00Z"
            if case == "capture_before_start": capture["receipt"]["source_read_seconds"] = 21
            if case == "early_finish": provisioning["finished_at"] = provisioning["started_at"]
            if case == "early_review": provisioning["holdout_review"]["reviewed_at"] = provisioning["started_at"]
            if case == "wrong_allocation": provisioning["allocation_sha256"] = "a"*64
            if case == "wrong_batch": provisioning["batch_id"] = "another_batch"
            if case == "wrong_method": provisioning["isolation_review"]["method"] = "shared_pair"
            if case == "tuned": provisioning["holdout_review"]["no_tuning"] = False
            if case == "wrong_dev_hash": provisioning["holdout_review"]["development_seeds_sha256"] = "b"*64
            with self.subTest(case=case), self.assertRaises(ValueError):
                prepare_intent(*values)

    def test_costs_remain_explicit_and_unknown_never_becomes_zero(self):
        values = self.fresh()
        values[-1]["costs_usd"]["runtime"] = None
        intent = prepare_intent(*values)
        self.assertFalse(intent["costs_complete"])
        self.assertIsNone(intent["provisioning"]["costs_usd"]["runtime"])
        self.assertEqual(validate_intent(intent, values[0]), intent)
        for value in (True, -1, "0", float("nan"), float("inf")):
            values = self.fresh()
            values[-1]["costs_usd"]["runtime"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_intent(*values)
        for mutation in (lambda costs: costs.pop("runtime"), lambda costs: costs.update(untracked_cost=1)):
            values = self.fresh()
            mutation(values[-1]["costs_usd"])
            with self.assertRaises(ValueError):
                prepare_intent(*values)

    def test_rehashing_flags_derived_semantics_or_code_cannot_promote_development_artifacts(self):
        for case in ("flag", "phase_plan", "template", "code", "unknown", "identity"):
            intent = deepcopy(self.intent)
            if case == "flag": intent["execution_authorized"] = True
            if case == "phase_plan": intent["phase_plan"]["held_out"] = True
            if case == "template": intent["templates"]["public_templates"][0]["question_template"] += " changed"
            if case == "code": intent["code_hashes"]["generator_sha256"] = "a"*64
            if case == "unknown": intent["held_out"] = True
            if case == "identity": intent["physical_ids"]["stripe_customers"].pop()
            rehash(intent, "intent_sha256")
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_intent(intent, self.inputs[0])
        values = list(self.fresh())
        values[2] = deepcopy(self.intent["phase_plan"])
        with self.assertRaises(ValueError):
            prepare_intent(*values)
        with self.assertRaises(ValueError):
            validate_intent(self.intent["phase_plan"], self.inputs[0])

    def test_exclusive_output_default_origin_and_complete_retained_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "intent.json"
            intent = prepare_intent(*self.fresh(), output)
            self.assertEqual(json.loads(output.read_text()), intent)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(intent["evidence_origin"], "synthetic_test")
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                prepare_intent(*self.fresh(), output)
            self.assertEqual(output.read_bytes(), before)
        native_label = prepare_intent(*self.fresh(), evidence_origin="recorded_native")
        self.assertFalse(native_label["claim_eligible"])
        self.assertEqual(native_label["origin_authentication"], "operator_assertion_only")


if __name__ == "__main__":
    unittest.main()
