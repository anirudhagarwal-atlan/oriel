"""Fabricated ledger contracts, not observed cost or performance evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from contextbase.acceptance_accounting import summarize_events, SPEC
from contextbase.acceptance_protocol import EVENT_RULES
from contextbase.study_binding import _hash


EPOCH = datetime.fromisoformat("2026-01-02T00:00:00+00:00")


def accounting_protocol():
    provisions = []
    for index, batch in enumerate(("batch-1", "batch-2")):
        provisions.append({"batch_id": batch, "provisioning": {
            "started_at": (EPOCH - timedelta(seconds=1000 - index * 100)).isoformat(),
            "finished_at": (EPOCH - timedelta(seconds=970 - index * 100)).isoformat(),
            "evidence_sha256": _hash("provision-" + batch),
            "costs_usd": {"fixture_generation": 0.25, "runtime": 0.5}}})
    return {"protocol_sha256": _hash("synthetic-ledger-protocol"), "frozen_at": EPOCH.isoformat(),
            "batches": [{"id": "batch-1"}, {"id": "batch-2"}], "intents": provisions,
            "recurring_profile": {"phase_order": ["baseline", "refreshed", "guard_a", "guard_b"]},
            "accounting_policy": {"event_rules": deepcopy(EVENT_RULES), "unknown_cost_policy": "inconclusive"},
            "historical_context_events": [], "historical_context_events_sha256": _hash([]),
            "provisioning_spend_usd": 1.5}


def event_fixture(identifier="event-1", **changes):
    event = {"event_id": identifier, "kind": "workspace_ingestion", "batch_id": "batch-1",
             "phase_id": "baseline", "started_at": (EPOCH + timedelta(seconds=10)).isoformat(),
             "finished_at": (EPOCH + timedelta(seconds=20)).isoformat(), "elapsed_seconds": 10,
             "cost_usd": 2.0, "allocation": {"source": 0, "workspace": 1},
             "evidence_sha256": _hash("event-receipt-" + identifier),
             "duration_role": "exclusive_overhead", "cost_role": "incremental", "attempt_id": None}
    event.update(changes)
    return event


def at(event, start, finish):
    event.update(started_at=(EPOCH + timedelta(seconds=start)).isoformat(),
                 finished_at=(EPOCH + timedelta(seconds=finish)).isoformat(), elapsed_seconds=finish - start)
    return event


class AcceptanceAccountingTests(unittest.TestCase):
    def setUp(self):
        self.protocol = accounting_protocol()

    def summarize(self, events):
        return summarize_events(self.protocol, events, validate_protocol=False)

    def historical(self, events):
        self.protocol["historical_context_events"] = deepcopy(events)
        self.protocol["historical_context_events_sha256"] = _hash(events)

    def test_fixed_and_global_shared_allocation_preserves_actual_seconds(self):
        review = at(event_fixture("review", kind="context_review", batch_id=None, phase_id=None,
                                  allocation={"source": 0.5, "workspace": 0.5}, cost_usd=8), -100, -80)
        self.historical([review])
        ingestion = event_fixture(cost_usd=2)
        fee = event_fixture("license", kind="runtime_license", batch_id=None, phase_id=None,
                            allocation={"source": 0.25, "workspace": 0.75}, cost_usd=4,
                            duration_role="cost_only")
        inputs = deepcopy((self.protocol, [review, ingestion, fee]))
        result = self.summarize([review, ingestion, fee])
        self.assertEqual(result["spec"], SPEC)
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["comparison"]["cost_usd"], 14)
        self.assertEqual(result["comparison"]["seconds"], 30)
        self.assertEqual(result["per_batch"]["batch-1"]["source"]["cost_usd"], 2.5)
        self.assertEqual(result["per_batch"]["batch-1"]["workspace"]["cost_usd"], 5.5)
        self.assertEqual(result["per_batch"]["batch-2"]["workspace"]["cost_usd"], 3.5)
        self.assertEqual(result["per_batch"]["batch-1"]["workspace"]["seconds"], 15)
        self.assertEqual(result["per_batch"]["batch-2"]["source"]["seconds"], 5)
        self.assertEqual((self.protocol, [review, ingestion, fee]), inputs)
        self.assertEqual(result["normalized_events"], inputs[1])
        self.assertEqual(result["events_sha256"], _hash(inputs[1]))
        self.assertFalse(result["full_all_in_complete"])
        self.assertFalse(result["claim_eligible"])
        json.dumps(result, allow_nan=False)

    def test_unknown_additive_charges_stay_null_with_known_lower_bound(self):
        unknown = event_fixture("unknown", cost_usd=None)
        known = at(event_fixture("known", cost_usd=5), 20, 25)
        result = self.summarize([unknown, known])
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["comparison"]["cost_usd"])
        self.assertEqual(result["comparison"]["known_cost_usd"], 5)
        self.assertEqual(result["comparison"]["seconds"], 15)
        self.assertIsNone(result["per_batch"]["batch-1"]["workspace"]["cost_usd"])
        self.assertEqual(result["per_batch"]["batch-1"]["source"]["cost_usd"], 0)
        self.assertEqual(result["per_batch"]["batch-2"]["workspace"]["cost_usd"], 0)
        self.assertEqual(result["unknown_reasons"], ["Unknown cost_usd for event unknown"])

    def test_unknown_global_runtime_charges_propagate_only_to_allocated_arms(self):
        event = at(event_fixture(kind="runtime_license", batch_id=None, phase_id=None,
                                cost_usd=None, duration_role="cost_only",
                                allocation={"source": 1, "workspace": 0}), -200, 50)
        result = self.summarize([event])
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["comparison"]["seconds"], 0)
        for row in result["per_batch"].values():
            self.assertIsNone(row["source"]["cost_usd"])
            self.assertEqual(row["workspace"]["cost_usd"], 0)

    def test_attempt_owned_model_tool_retry_and_startup_subspans_never_add(self):
        rows = []
        for kind in ("model_calls", "source_tool_calls", "retries_failures",
                     "source_mcp_startup", "workspace_mcp_startup"):
            source = kind == "source_mcp_startup"
            rows.append(event_fixture(kind, kind=kind, duration_role="within_attempt",
                                      cost_role="included_in_attempt", attempt_id="attempt-source" if source else "attempt-workspace",
                                      allocation={"source": 1 if source else 0, "workspace": 0 if source else 1}))
        result = self.summarize(rows)
        self.assertEqual(result["comparison"]["cost_usd"], 0)
        self.assertEqual(result["comparison"]["seconds"], 0)
        self.assertEqual(result["included_attempt_subspans"]["known_cost_usd"], 10)
        self.assertEqual(result["included_attempt_subspans"]["seconds"], 0)
        self.assertEqual(len(result["included_attempt_subspans"]["event_ids"]), 5)
        rows[0]["cost_usd"] = None
        result = self.summarize(rows)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["comparison"]["cost_usd"], 0)
        self.assertIsNone(result["included_attempt_subspans"]["cost_usd"])

    def test_standalone_source_fees_add_only_price_with_incurring_arm(self):
        fee = event_fixture(kind="source_tool_calls", duration_role="cost_only",
                            allocation={"source": 1, "workspace": 0}, cost_usd=0.75)
        result = self.summarize([fee])
        self.assertEqual(result["comparison"]["cost_usd"], 0.75)
        self.assertEqual(result["per_batch"]["batch-1"]["source"]["seconds"], 0)
        fee["allocation"] = {"source": 0.5, "workspace": 0.5}
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.summarize([fee])

    def test_duplicate_ids_and_complete_receipts_fail_closed(self):
        for key in ("event_id", "evidence_sha256"):
            first, second = event_fixture("first"), at(event_fixture("second"), 20, 30)
            second[key] = first[key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Duplicate"):
                self.summarize([first, second])

    def test_exact_event_fields_reject_unknown_or_missing_spend(self):
        for change in (lambda row: row.update(extra_tool_usd=100), lambda row: row.pop("cost_usd"),
                       lambda row: row.pop("attempt_id"), lambda row: row.update(included_in_attempt=True)):
            row = event_fixture()
            change(row)
            with self.assertRaisesRegex(ValueError, "exactly the declared"):
                self.summarize([row])

    def test_allocation_matches_frozen_ownership_and_rejects_bool_nan_or_missing(self):
        for allocation in ({"source": 1, "workspace": 0}, {"source": 0.25, "workspace": 0.75},
                           {"source": 0, "workspace": True}, {"source": 0, "workspace": float("nan")},
                           {"source": 0, "workspace": 0.9}, {"source": 0, "workspace": 1, "evaluator": 0}):
            with self.subTest(allocation=allocation), self.assertRaises(ValueError):
                self.summarize([event_fixture(allocation=allocation)])
        event = event_fixture(kind="evaluator_validation", allocation={"source": 0.3, "workspace": 0.7})
        self.assertAlmostEqual(self.summarize([event])["comparison"]["by_arm"]["source"]["cost_usd"], 0.6)

    def test_invalid_costs_and_duration_are_never_treated_as_zero(self):
        for key, value in (("cost_usd", True), ("cost_usd", -1), ("cost_usd", float("inf")),
                           ("elapsed_seconds", None), ("elapsed_seconds", True), ("elapsed_seconds", -1),
                           ("elapsed_seconds", 11)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.summarize([event_fixture(**{key: value})])
        self.summarize([event_fixture(elapsed_seconds=10.0005)])

    def test_real_utc_chronology_applies_even_to_cost_only(self):
        event = event_fixture(kind="runtime_license", duration_role="cost_only")
        for changes in ({"finished_at": (EPOCH + timedelta(seconds=1)).isoformat()},
                        {"started_at": "2026-01-02T01:00:10+01:00"},
                        {"started_at": "2026-01-02T00:00:10"},
                        {"started_at": "2999-01-02T00:00:10Z", "finished_at": "2999-01-02T00:00:20Z"},
                        {"elapsed_seconds": 0}):
            altered = deepcopy(event); altered.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.summarize([altered])

    def test_exclusive_overhead_cannot_duplicate_time_in_same_arm_or_global_scope(self):
        first = event_fixture("first")
        for second in (event_fixture("second"),
                       event_fixture("second", kind="context_review", batch_id=None, phase_id=None,
                                     allocation={"source": 0.5, "workspace": 0.5})):
            with self.assertRaisesRegex(ValueError, "overlaps"):
                self.summarize([first, second])
        self.summarize([first, at(event_fixture("adjacent"), 20, 30)])
        self.summarize([first, event_fixture("parallel-source", kind="source_mcp_startup",
                                           allocation={"source": 1, "workspace": 0})])
        self.summarize([first, event_fixture("parallel-batch", batch_id="batch-2")])
        self.summarize([first, event_fixture("fee", kind="runtime_license", duration_role="cost_only")])

    def test_history_is_exact_ordered_and_hash_bound_without_timestamp_rewrite(self):
        first = at(event_fixture("creation", kind="context_creation", batch_id=None, phase_id=None,
                                 allocation={"source": 0.5, "workspace": 0.5}), -100, -90)
        second = at(event_fixture("review", kind="context_review", batch_id=None, phase_id=None,
                                  allocation={"source": 0.5, "workspace": 0.5}), -90, -80)
        self.historical([first, second])
        result = self.summarize([first, second])
        self.assertEqual(result["normalized_events"][0]["started_at"], first["started_at"])
        for rows in ([second, first], [first], []):
            with self.assertRaisesRegex(ValueError, "historical context"):
                self.summarize(rows)
        self.protocol["historical_context_events_sha256"] = _hash("different")
        with self.assertRaisesRegex(ValueError, "historical context ledger"):
            self.summarize([first, second])

    def test_unfrozen_history_and_noncontext_prefreeze_work_are_rejected(self):
        context = at(event_fixture(kind="context_creation", allocation={"source": 0.5, "workspace": 0.5}), -10, -5)
        with self.assertRaisesRegex(ValueError, "historical context"):
            self.summarize([context])
        for row in (at(event_fixture(), -10, -5), at(deepcopy(context), -5, 5)):
            with self.assertRaisesRegex(ValueError, "Pre-freeze"):
                self.summarize([row])

    def test_attempt_ownership_cannot_move_across_batch_phase_or_arm(self):
        first = event_fixture("first", kind="model_calls", duration_role="within_attempt",
                              cost_role="included_in_attempt", attempt_id="attempt-1")
        for changes in ({"batch_id": "batch-2"}, {"phase_id": "refreshed"},
                        {"allocation": {"source": 1, "workspace": 0}}):
            second = deepcopy(first); second.update(event_id="second", evidence_sha256=_hash("second"), **changes)
            with self.assertRaisesRegex(ValueError, "Attempt subspans disagree"):
                self.summarize([first, second])

    def test_scope_and_role_substitution_cannot_hide_additive_work(self):
        bad = (
            {"batch_id": "missing"}, {"phase_id": "missing"}, {"batch_id": None},
            {"kind": "refresh", "phase_id": None}, {"kind": "phase_transitions", "phase_id": "baseline"},
            {"kind": "model_calls"}, {"kind": "retries_failures"}, {"kind": "runtime_license"},
            {"duration_role": "within_attempt", "cost_role": "included_in_attempt", "attempt_id": "a"},
            {"duration_role": "cost_only"}, {"cost_role": "included_in_attempt"}, {"attempt_id": "a"},
            {"duration_role": "unknown"},
            {"kind": "model_calls", "duration_role": "within_attempt", "cost_role": "included_in_attempt", "attempt_id": "a", "phase_id": None},
            {"kind": "model_calls", "duration_role": "within_attempt", "cost_role": "incremental", "attempt_id": "a"},
        )
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.summarize([event_fixture(**changes)])

    def test_fixture_records_corroborate_protocol_apparatus_without_comparison_charge(self):
        provisioning = self.protocol["intents"][0]["provisioning"]
        fixture = event_fixture("fixture", kind="fixture_generation", phase_id=None,
            started_at=provisioning["started_at"], finished_at=provisioning["finished_at"], elapsed_seconds=30,
            evidence_sha256=provisioning["evidence_sha256"], cost_usd=0.25,
            allocation={"source": 0.5, "workspace": 0.5})
        result = self.summarize([fixture])
        self.assertEqual(result["apparatus"]["cost_usd"], 0.25)
        self.assertEqual(result["apparatus"]["protocol_provisioning_spend_usd"], 1.5)
        self.assertTrue(result["apparatus"]["protocol_owned"])
        self.assertEqual(result["apparatus"]["missing_batch_ids"], ["batch-2"])
        self.assertEqual(result["comparison"]["cost_usd"], 0)
        for changes in ({"cost_usd": 0.75}, {"cost_usd": None}, {"phase_id": "baseline"},
                        {"evidence_sha256": _hash("new-receipt")},
                        {"started_at": (EPOCH - timedelta(seconds=999)).isoformat(), "elapsed_seconds": 29}):
            altered = deepcopy(fixture); altered.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.summarize([altered])
        result = self.summarize([])
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["apparatus"]["missing_batch_ids"], ["batch-1", "batch-2"])
        self.assertFalse(result["full_all_in_complete"])

    def test_frozen_rules_cannot_be_silently_changed_with_protocol_validation_bypassed(self):
        self.protocol["accounting_policy"]["event_rules"]["workspace_ingestion"]["allocation"] = "source"
        with self.assertRaisesRegex(ValueError, "Frozen accounting"):
            self.summarize([event_fixture()])

    def test_protocol_validation_bypass_requires_an_explicit_boolean(self):
        for value in (None, 0, "false"):
            with self.assertRaisesRegex(ValueError, "explicit boolean"):
                summarize_events(self.protocol, [], validate_protocol=value)

    def test_finite_event_costs_cannot_overflow_total_silently(self):
        rows = [event_fixture("first", cost_usd=1e308), at(event_fixture("second", cost_usd=1e308), 20, 30)]
        with self.assertRaisesRegex(ValueError, "finite"):
            self.summarize(rows)

    def test_default_api_revalidates_real_protocol_with_zero_external_calls(self):
        from test_acceptance_protocol import protocol_fixture
        with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No source")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            protocol = protocol_fixture()
            result = summarize_events(protocol, [])
            self.assertEqual(result["status"], "validated")
            self.assertEqual((result["model_calls"], result["source_calls"]), (0, 0))
            protocol["claim_eligible"] = True
            with self.assertRaises(ValueError):
                summarize_events(protocol, [])


if __name__ == "__main__":
    unittest.main()
