"""Budget mechanics with fabricated protocols and scripted, cost-free clients.

The full protocol compiler is stubbed here; its validation call and immutable
artifact identity are tested, while controller integration uses real protocols.
No source, native runtime or model process is invoked.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.study_budget import BudgetError, BudgetedModel, NoDispatch, StudyBudget, _hash


class ScriptedClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response if response is not None else {"cost_usd": .1, "response": "authored test"}
        self.error = error
        self.timeout_seconds = 90

    def complete(self, system, messages, max_cost_usd):
        self.calls.append({"max_cost_usd": max_cost_usd, "timeout_seconds": self.timeout_seconds})
        if self.error:
            raise self.error
        return self.response


class StudyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "budget"
        self.now = datetime.now(timezone.utc)
        self.protocol = {"protocol_sha256": "1" * 64,
                         "frozen_at": (self.now - timedelta(seconds=1)).isoformat(),
                         "budgets": {"comparison_usd": 2, "total_study_usd": 3,
                                     "max_model_calls": 4, "study_seconds": 300},
                         "provisioning_spend_usd": .5,
                         "historical_context_events": [{"event_id": "authored-context", "cost_usd": .25}],
                         "historical_context_events_sha256": "2" * 64}
        self.validation = self.enterContext(patch("contextbase.study_budget.validate_protocol"))

    def create(self, protocol=None):
        budget = StudyBudget.create(self.path, protocol or self.protocol)
        self.addCleanup(budget.close)
        return budget

    def reserve(self, budget, cost=.2, **kwargs):
        return budget.reserve("model_calls", max_cost_usd=cost, model_calls=1, **kwargs)

    def test_cumulative_across_phases_and_reopen_includes_owned_history(self):
        budget = self.create()
        for phase in ("baseline", "refreshed"):
            reservation = self.reserve(budget, phase_id=phase)
            budget.settle(reservation["reservation_id"], actual_cost_usd=.15)
        self.assertEqual(budget.status()["known_comparison_cost_usd"], .55)
        self.assertEqual(budget.status()["known_total_cost_usd"], 1.05)
        self.assertEqual(budget.status()["model_calls"], 2)
        first_deadline = budget.status()["deadline"]
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertEqual(reopened.status()["known_total_cost_usd"], 1.05)
            self.assertEqual(reopened.status()["deadline"], first_deadline)
            reservation = self.reserve(reopened)
            reopened.settle(reservation["reservation_id"], actual_cost_usd=.1)
            self.assertEqual(reopened.status()["model_calls"], 3)
            self.assertEqual(reopened.status()["known_comparison_cost_usd"], .65)
        self.assertEqual(self.validation.call_count, 2)

    def test_long_lived_runtime_reservation_restricts_nested_operations(self):
        budget = self.create()
        runtime = budget.reserve("runtime_license", max_cost_usd=1.5)
        model = self.reserve(budget, .2)
        with self.assertRaisesRegex(BudgetError, "comparison_cost_limit"):
            self.reserve(budget, .1)
        budget.settle(model["reservation_id"], actual_cost_usd=.05)
        source = budget.reserve("source_tool_calls", max_cost_usd=.1)
        self.assertEqual(len(budget.status()["pending"]), 2)
        budget.settle(source["reservation_id"], actual_cost_usd=.01)
        budget.settle(runtime["reservation_id"], actual_cost_usd=.5)
        self.assertEqual(budget.status()["known_comparison_cost_usd"], .81)
        self.assertEqual(budget.status()["model_calls"], 1)

    def test_pending_reopen_is_inspectable_and_cannot_resume_or_settle(self):
        budget = self.create()
        reservation = self.reserve(budget)
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertEqual(reopened.status()["model_calls"], 1)
            self.assertIn("interrupted_reservation", reopened.status()["blocked_reasons"])
            with self.assertRaisesRegex(BudgetError, "interrupted_reservation"):
                self.reserve(reopened)
            with self.assertRaisesRegex(BudgetError, "reservation_not_owned"):
                reopened.settle(reservation["reservation_id"], actual_cost_usd=0)
            self.assertEqual(reopened.status()["pending"][0]["reservation_id"], reservation["reservation_id"])

    def test_unknown_outcome_retains_known_partial_charge_and_blocks(self):
        budget = self.create()
        reservation = self.reserve(budget, .4)
        budget.settle(reservation["reservation_id"], actual_cost_usd=.1, outcome="unknown")
        state = budget.status()
        self.assertEqual(state["known_comparison_cost_usd"], .35)
        self.assertEqual(state["reserved_comparison_usd"], .3)
        self.assertIn("outcome_unknown", state["blocked_reasons"])
        with self.assertRaisesRegex(BudgetError, "outcome_unknown"):
            self.reserve(budget)
        with self.assertRaises(BudgetError):
            budget.settle(reservation["reservation_id"], actual_cost_usd=.2)
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertEqual(reopened.status()["known_total_cost_usd"], .85)
            self.assertIn("outcome_unknown", reopened.status()["blocked_reasons"])

    def test_known_overspend_is_retained_even_above_global_cap(self):
        budget = self.create()
        reservation = self.reserve(budget, .2)
        budget.settle(reservation["reservation_id"], actual_cost_usd=5)
        self.assertEqual(budget.status()["known_total_cost_usd"], 5.75)
        self.assertEqual(budget.status()["pending"], [])
        self.assertEqual(set(budget.status()["blocked_reasons"]),
                         {"comparison_cost_exceeded", "total_study_cost_exceeded", "reservation_exceeded"})
        with self.assertRaises(BudgetError):
            self.reserve(budget)
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertEqual(reopened.status()["known_total_cost_usd"], 5.75)

    def test_call_cap_counts_reserved_failed_calls_and_cost_caps_are_decimal(self):
        protocol = deepcopy(self.protocol)
        protocol["budgets"].update(max_model_calls=2, comparison_usd=.55)
        budget = self.create(protocol)
        for cost in (.1, .2):
            reservation = self.reserve(budget, cost)
            budget.settle(reservation["reservation_id"], actual_cost_usd=cost)
        self.assertEqual(budget.status()["known_comparison_cost_usd"], .55)
        with self.assertRaisesRegex(BudgetError, "model_call_limit"):
            self.reserve(budget, 0)
        with self.assertRaisesRegex(BudgetError, "comparison_cost_limit"):
            budget.reserve("source_tool_calls", max_cost_usd=.01)

    def test_total_cap_includes_apparatus_and_history(self):
        protocol = deepcopy(self.protocol)
        protocol["budgets"].update(total_study_usd=.8)
        budget = self.create(protocol)
        with self.assertRaisesRegex(BudgetError, "total_study_cost_limit"):
            budget.reserve("fixture_generation", max_cost_usd=.06)
        self.assertEqual(budget.status()["known_total_cost_usd"], .75)
        self.assertEqual(budget.status()["model_calls"], 0)

    def test_unknown_historical_context_prevents_any_new_paid_work(self):
        protocol = deepcopy(self.protocol)
        protocol["historical_context_events"].append({"event_id": "unknown-context", "cost_usd": None})
        budget = self.create(protocol)
        self.assertEqual(budget.status()["known_total_cost_usd"], .75)
        with self.assertRaisesRegex(BudgetError, "historical_cost_unknown"):
            self.reserve(budget)

    def test_deadline_is_from_freeze_and_stays_expired_after_reopen(self):
        protocol = deepcopy(self.protocol)
        protocol["frozen_at"] = (self.now - timedelta(seconds=301)).isoformat()
        budget = self.create(protocol)
        self.assertEqual(budget.remaining_seconds, 0)
        with self.assertRaisesRegex(BudgetError, "study_deadline"):
            self.reserve(budget)
        budget.close()
        with StudyBudget.open(self.path, protocol) as reopened:
            self.assertEqual(reopened.remaining_seconds, 0)

    def test_backward_clock_cannot_extend_live_deadline_or_dispatch(self):
        budget = self.create()
        with patch("contextbase.study_budget._now", return_value=self.now - timedelta(days=1)):
            self.assertLessEqual(budget.remaining_seconds, 300)
            with self.assertRaisesRegex(BudgetError, "clock_regression"):
                self.reserve(budget)

    def test_explicit_stop_is_durable_but_known_fees_can_still_settle(self):
        budget = self.create()
        runtime = budget.reserve("runtime_license", max_cost_usd=.3)
        budget.stop("source_state_changed")
        budget.settle(runtime["reservation_id"], actual_cost_usd=.1)
        self.assertEqual(budget.status()["pending"], [])
        self.assertEqual(budget.status()["known_comparison_cost_usd"], .35)
        self.assertNotIn("outcome_unknown", budget.status()["blocked_reasons"])
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            with self.assertRaisesRegex(BudgetError, "source_state_changed"):
                self.reserve(reopened)

    def test_exclusive_creation_single_writer_and_exact_protocol_identity(self):
        budget = self.create()
        original = (self.path / "000000000000.json").read_bytes()
        with self.assertRaises(FileExistsError):
            StudyBudget.create(self.path, self.protocol)
        with self.assertRaisesRegex(BudgetError, "writer_active"):
            StudyBudget.open(self.path, self.protocol)
        self.assertEqual((self.path / "000000000000.json").read_bytes(), original)
        budget.close()
        for key in ("protocol_sha256", "provisioning_spend_usd"):
            changed = deepcopy(self.protocol)
            changed[key] = "3" * 64 if key == "protocol_sha256" else .6
            with self.assertRaisesRegex(BudgetError, "protocol_or_limits_changed"):
                StudyBudget.open(self.path, changed)
        changed = deepcopy(self.protocol)
        changed["budgets"]["max_model_calls"] = 40
        with self.assertRaisesRegex(BudgetError, "protocol_or_limits_changed"):
            StudyBudget.open(self.path, changed)

    def test_private_actual_json_receipts_have_chains_and_no_prompt_values(self):
        budget = self.create()
        reservation = self.reserve(budget, batch_id="batch_001", phase_id="baseline")
        budget.settle(reservation["reservation_id"], actual_cost_usd=.1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        previous = None
        for index, path in enumerate(sorted(self.path.glob("*.json"))):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            row = json.loads(path.read_text())
            self.assertEqual(row["sequence"], index)
            self.assertEqual(row["previous_sha256"], previous)
            self.assertEqual(row["receipt_sha256"], _hash({key: value for key, value in row.items() if key != "receipt_sha256"}))
            previous = row["receipt_sha256"]
        self.assertEqual(previous, budget.status()["last_receipt_sha256"])
        self.assertEqual(len(budget.receipts()), 3)
        self.assertFalse(budget.status()["execution_authorized"])

    def test_tampered_receipt_and_unsafe_permissions_are_rejected(self):
        budget = self.create()
        budget.close()
        header = self.path / "000000000000.json"
        original = header.read_text()
        row = json.loads(original)
        row["payload"]["limits"]["comparison_usd"] = 999
        header.write_text(json.dumps(row))
        with self.assertRaises(BudgetError):
            StudyBudget.open(self.path, self.protocol)
        header.write_text(original)
        header.chmod(0o644)
        with self.assertRaisesRegex(BudgetError, "invalid_receipt"):
            StudyBudget.open(self.path, self.protocol)

    def test_interrupted_checkpoint_is_inspectable_and_blocks(self):
        budget = self.create()
        reservation = self.reserve(budget)
        budget.close()
        descriptor = os.open(self.path / ".pending-authored-interruption", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertIn("interrupted_checkpoint", reopened.status()["blocked_reasons"])
            self.assertEqual(reopened.status()["pending"][0]["reservation_id"], reservation["reservation_id"])

    def test_invalid_inputs_do_not_append_receipts_or_consume_calls(self):
        budget = self.create()
        for cost in (-1, True, float("nan"), float("inf"), "0.2"):
            with self.assertRaises(BudgetError):
                self.reserve(budget, cost)
        with self.assertRaises(BudgetError):
            budget.reserve("source_tool_calls", max_cost_usd=0, model_calls=1)
        with self.assertRaises(BudgetError):
            budget.stop("unsafe\nfreeform error")
        self.assertEqual(budget.status()["receipt_count"], 1)
        self.assertEqual(budget.status()["model_calls"], 0)

    def test_model_before_call_reserves_before_dispatch_and_forwards_timeout(self):
        budget = self.create()
        native = ScriptedClient()
        model = BudgetedModel(native, budget, batch_id="batch_001", phase_id="baseline")
        model.timeout_seconds = 2
        model.before_call(.2)
        self.assertEqual(native.calls, [])
        self.assertEqual(budget.status()["model_calls"], 1)
        self.assertEqual(len(budget.status()["pending"]), 1)
        returned = model.complete("authored system", [], max_cost_usd=.2)
        self.assertIs(returned, native.response)
        self.assertEqual(native.calls, [{"max_cost_usd": .2, "timeout_seconds": 2}])
        self.assertEqual(budget.status()["pending"], [])
        self.assertEqual(budget.status()["known_total_cost_usd"], .85)

    def test_model_refusal_never_dispatches_or_adds_phantom_call(self):
        budget = self.create()
        native = ScriptedClient()
        model = BudgetedModel(native, budget)
        with self.assertRaises(BudgetError):
            model.before_call(10)
        self.assertEqual(native.calls, [])
        self.assertEqual(budget.status()["model_calls"], 0)

    def test_model_unknown_and_raised_calls_are_not_retried(self):
        for label, native in (("unknown", ScriptedClient({"cost_usd": .1, "spend_unknown": True})),
                              ("raised", ScriptedClient(error=RuntimeError("scripted")))):
            with self.subTest(label=label), StudyBudget.create(self.path.parent / label, self.protocol) as budget:
                model = BudgetedModel(native, budget)
                if label == "raised":
                    with self.assertRaises(RuntimeError):
                        model.complete("authored", [], max_cost_usd=.2)
                else:
                    self.assertIs(model.complete("authored", [], max_cost_usd=.2), native.response)
                with self.assertRaises(BudgetError):
                    model.before_call(.2)
                self.assertEqual(len(native.calls), 1)
                self.assertEqual(budget.status()["model_calls"], 1)
                self.assertIn("outcome_unknown", budget.status()["blocked_reasons"])
                self.assertEqual(budget.status()["known_comparison_cost_usd"], .35 if label == "unknown" else .25)

    def test_paid_overspend_response_is_not_dropped(self):
        budget = self.create()
        native = ScriptedClient({"cost_usd": 4, "is_error": True})
        model = BudgetedModel(native, budget)
        self.assertIs(model.complete("authored", [], max_cost_usd=.2), native.response)
        self.assertEqual(budget.status()["known_total_cost_usd"], 4.75)
        with self.assertRaises(BudgetError):
            model.before_call(.2)

    def test_reserved_model_cannot_dispatch_after_another_unknown_action(self):
        budget = self.create()
        native = ScriptedClient()
        model = BudgetedModel(native, budget)
        model.before_call(.2)
        source = budget.reserve("source_tool_calls", max_cost_usd=.1)
        budget.settle(source["reservation_id"], actual_cost_usd=None, outcome="unknown")
        with self.assertRaisesRegex(NoDispatch, "outcome_unknown"):
            model.complete("authored", [], max_cost_usd=.2)
        self.assertEqual(native.calls, [])
        self.assertEqual(budget.status()["model_calls"], 1)
        self.assertEqual(budget.status()["known_total_cost_usd"], .75)
        self.assertEqual(len(budget.status()["pending"]), 1)

    def test_changed_reserved_limit_stops_without_dispatch_or_unknown_charge(self):
        budget = self.create()
        native = ScriptedClient()
        model = BudgetedModel(native, budget)
        model.before_call(.2)
        with self.assertRaisesRegex(NoDispatch, "model_reservation_limit_changed"):
            model.complete("authored", [], max_cost_usd=.3)
        self.assertEqual(native.calls, [])
        self.assertEqual(budget.status()["pending"], [])
        self.assertEqual(budget.status()["blocked_reasons"], ["model_reservation_limit_changed"])

    def test_backend_cannot_claim_its_raised_call_was_never_dispatched(self):
        budget = self.create()
        native = ScriptedClient(error=NoDispatch("authored_backend_error"))
        model = BudgetedModel(native, budget)
        with self.assertRaisesRegex(BudgetError, "model_call_outcome_unknown") as raised:
            model.complete("authored", [], max_cost_usd=.2)
        self.assertNotIsInstance(raised.exception, NoDispatch)
        self.assertEqual(len(native.calls), 1)
        self.assertIn("outcome_unknown", budget.status()["blocked_reasons"])

    def test_storage_failure_after_paid_response_retains_cost_and_stops(self):
        budget = self.create()
        native = ScriptedClient()
        model = BudgetedModel(native, budget)
        model.before_call(.2)
        with patch("contextbase.study_budget.os.link", side_effect=OSError("authored storage failure")):
            self.assertIs(model.complete("authored", [], max_cost_usd=.2), native.response)
        self.assertEqual(budget.status()["known_total_cost_usd"], .85)
        self.assertIn("receipt_storage_failed", budget.status()["blocked_reasons"])
        with self.assertRaises(BudgetError):
            model.before_call(.2)
        budget.close()
        with StudyBudget.open(self.path, self.protocol) as reopened:
            self.assertIn("interrupted_reservation", reopened.status()["blocked_reasons"])
            self.assertIn("interrupted_checkpoint", reopened.status()["blocked_reasons"])

    def test_source_known_failure_can_be_settled_without_unknown_cost(self):
        budget = self.create()
        source = budget.reserve("refresh", max_cost_usd=.1, phase_id="guard")
        budget.settle(source["reservation_id"], actual_cost_usd=.02)
        budget.stop("source_transport_failed")
        self.assertEqual(budget.status()["known_total_cost_usd"], .77)
        self.assertEqual(budget.status()["model_calls"], 0)
        self.assertNotIn("outcome_unknown", budget.status()["blocked_reasons"])


if __name__ == "__main__":
    unittest.main()
