"""Synthetic pricing contracts only; no actual billing, source or model evidence."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from contextbase.acceptance_protocol import EVENT_RULES
from contextbase.study_binding import _hash
from contextbase.study_costs import (COST_KINDS, SPEC, maximum_reservation,
                                     quote, validate_cost_contract)


def cost_fixture():
    rules = {}
    for kind in sorted(COST_KINDS):
        mode = EVENT_RULES[kind]["allocation"]
        rules[kind] = {"method": "declared_zero", "rate": 0, "max_seconds": 60,
                       "allocation": {"source": 0.5, "workspace": 0.5} if mode == "measured_shared" else mode,
                       "billing_evidence_sha256": _hash({"synthetic price": kind}),
                       "rationale": "Fabricated zero-fee unit-test contract; not an observed invoice."}
    rules["workspace_ingestion"].update(method="usd_per_second", rate=0.02)
    rules["phase_transitions"].update(method="fixed_per_operation", rate=0.5)
    contract = {"spec": SPEC, "currency": "USD", "rules": rules, "assignments": {
        "batch-1": {"environment_id": "synthetic-pair-1", "stripe_url": "http://127.0.0.1:9001",
                    "linear_url": "http://127.0.0.1:9002"},
        "batch-2": {"environment_id": "synthetic-pair-2", "stripe_url": "http://127.0.0.1:9003",
                    "linear_url": "http://[::1]:9004"}}}
    protocol = {"batches": [{"id": "batch-1"}, {"id": "batch-2"}],
                "intents": [{"batch_id": key, "provisioning": {"environment_id": row["environment_id"]}}
                            for key, row in contract["assignments"].items()],
                "accounting_policy": {"event_rules": deepcopy(EVENT_RULES), "unknown_cost_policy": "inconclusive",
                                      "evidence_sha256": _hash(contract)},
                "budgets": {"study_seconds": 3600, "comparison_usd": 10}}
    reseal(protocol, contract)
    return protocol, contract


def reseal(protocol, contract):
    protocol["accounting_policy"]["evidence_sha256"] = _hash(contract)
    protocol["protocol_sha256"] = _hash({key: value for key, value in protocol.items() if key != "protocol_sha256"})


class StudyCostsTests(unittest.TestCase):
    def setUp(self):
        self.protocol, self.contract = cost_fixture()

    def test_validation_is_pure_and_returns_an_independent_copy(self):
        before = deepcopy((self.protocol, self.contract))
        with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native calls")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model calls")):
            result = validate_cost_contract(self.protocol, self.contract)
        self.assertEqual(result, self.contract)
        result["rules"]["refresh"]["rate"] = 999
        self.assertEqual((self.protocol, self.contract), before)

    def test_actual_prices_and_reservations_do_not_discard_overshoot(self):
        self.assertEqual(quote(self.contract, "source_tool_calls", 500), 0)
        self.assertEqual(quote(self.contract, "workspace_ingestion", 10), 0.2)
        self.assertEqual(maximum_reservation(self.contract, "workspace_ingestion"), 1.2)
        self.assertEqual(quote(self.contract, "workspace_ingestion", 100), 2)
        self.assertEqual(quote(self.contract, "phase_transitions", 0), 0.5)
        self.assertEqual(quote(self.contract, "phase_transitions", 100), 0.5)
        self.assertEqual(maximum_reservation(self.contract, "phase_transitions"), 0.5)

    def test_global_runtime_invoice_may_be_zero_fixed_or_metered(self):
        for method, rate, expected in (("declared_zero", 0, 0), ("fixed_per_operation", 3, 3),
                                       ("usd_per_second", 0.1, 0.2)):
            contract = deepcopy(self.contract)
            contract["rules"]["runtime_license"].update(method=method, rate=rate,
                                                        allocation={"source": 0.25, "workspace": 0.75})
            reseal(self.protocol, contract)
            validate_cost_contract(self.protocol, contract)
            self.assertEqual(quote(contract, "runtime_license", 2), expected)

    def test_modified_contract_or_protocol_without_original_hash_is_rejected(self):
        for target in ("contract", "protocol"):
            protocol, contract = deepcopy((self.protocol, self.contract))
            if target == "contract": contract["rules"]["refresh"]["rationale"] = "Changed after freeze"
            else: protocol["budgets"]["comparison_usd"] = 100
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_cost_contract(protocol, contract)

    def test_rules_are_complete_and_cannot_override_model_or_historical_charges(self):
        for kind in list(COST_KINDS) + ["model_calls", "context_review", "fixture_generation", "retries_failures"]:
            contract = deepcopy(self.contract)
            if kind in COST_KINDS: del contract["rules"][kind]
            else: contract["rules"][kind] = deepcopy(contract["rules"]["refresh"])
            reseal(self.protocol, contract)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_cost_contract(self.protocol, contract)
        for kind in ("model_calls", "context_creation", "nonexistent", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                quote(self.contract, kind, 2)

    def test_unknown_nonfinite_bool_and_negative_prices_or_durations_are_rejected(self):
        for field, value in (("rate", None), ("rate", True), ("rate", -1), ("rate", "0"),
                             ("rate", float("nan")), ("rate", float("inf")),
                             ("max_seconds", 0), ("max_seconds", True), ("max_seconds", None),
                             ("max_seconds", -1), ("method", "unknown")):
            contract = deepcopy(self.contract)
            contract["rules"]["workspace_ingestion"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                maximum_reservation(contract, "workspace_ingestion")
        for elapsed in (None, True, -1, "2", float("nan"), float("inf")):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                quote(self.contract, "workspace_ingestion", elapsed)
        self.contract["rules"]["workspace_ingestion"].update(rate=1e308, max_seconds=1)
        with self.assertRaises(ValueError):
            quote(self.contract, "workspace_ingestion", 10)

    def test_positive_uninstrumented_native_and_startup_fees_are_rejected(self):
        for kind in ("source_tool_calls", "source_mcp_startup", "workspace_mcp_startup"):
            for method in ("fixed_per_operation", "usd_per_second"):
                contract = deepcopy(self.contract)
                contract["rules"][kind].update(method=method, rate=0.01)
                reseal(self.protocol, contract)
                with self.subTest(kind=kind, method=method), self.assertRaises(ValueError):
                    validate_cost_contract(self.protocol, contract)

    def test_declared_zero_requires_explicit_receipt_and_exact_zero(self):
        for changes in ({"rate": 1}, {"billing_evidence_sha256": None}, {"rationale": ""},
                        {"method": "fixed_per_operation", "rate": 0}):
            contract = deepcopy(self.contract)
            contract["rules"]["refresh"].update(changes)
            reseal(self.protocol, contract)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_cost_contract(self.protocol, contract)

    def test_allocations_must_match_frozen_modes_and_explicit_shared_fractions(self):
        for kind, allocation in (("refresh", "workspace"), ("workflow_configuration", {"source": 1, "workspace": 0}),
                                  ("workspace_ingestion", "source"), ("phase_transitions", "incurring_arm"),
                                  ("runtime_license", "common_equal"),
                                  ("runtime_license", {"source": 1, "workspace": 1}),
                                  ("runtime_license", {"source": -0.1, "workspace": 1.1}),
                                  ("evaluator_validation", {"source": True, "workspace": 0})):
            contract = deepcopy(self.contract)
            contract["rules"][kind]["allocation"] = allocation
            reseal(self.protocol, contract)
            with self.subTest(kind=kind, allocation=allocation), self.assertRaises(ValueError):
                validate_cost_contract(self.protocol, contract)

    def test_operation_duration_and_maximum_reservation_fit_protocol_caps(self):
        for changes in ({"max_seconds": 3601}, {"rate": 1, "max_seconds": 11},
                        {"rate": 1e308, "max_seconds": 2}):
            contract = deepcopy(self.contract)
            contract["rules"]["workspace_ingestion"].update(changes)
            reseal(self.protocol, contract)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_cost_contract(self.protocol, contract)
        self.contract["rules"]["workspace_ingestion"].update(rate=0.1, max_seconds=100)
        reseal(self.protocol, self.contract)
        validate_cost_contract(self.protocol, self.contract)
        self.assertEqual(maximum_reservation(self.contract, "workspace_ingestion"), 10)

    def test_assignments_require_exact_batch_environment_and_distinct_services(self):
        for case in ("missing", "extra", "wrong_environment", "duplicate_environment", "duplicate_source",
                     "duplicate_batch_port", "ipv6_alias", "extra_field"):
            protocol, contract = deepcopy((self.protocol, self.contract))
            assignments = contract["assignments"]
            if case == "missing": del assignments["batch-2"]
            elif case == "extra": assignments["batch-3"] = deepcopy(assignments["batch-2"])
            elif case == "wrong_environment": assignments["batch-1"]["environment_id"] = "another-pair"
            elif case == "duplicate_environment":
                assignments["batch-2"]["environment_id"] = "synthetic-pair-1"
                protocol["intents"][1]["provisioning"]["environment_id"] = "synthetic-pair-1"
            elif case == "duplicate_source": assignments["batch-1"]["linear_url"] = assignments["batch-1"]["stripe_url"]
            elif case == "duplicate_batch_port": assignments["batch-2"]["stripe_url"] = assignments["batch-1"]["stripe_url"]
            elif case == "ipv6_alias": assignments["batch-2"]["linear_url"] = "http://[::1]:9001"
            else: assignments["batch-1"]["token"] = "synthetic-unexpected-field"
            reseal(protocol, contract)
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_cost_contract(protocol, contract)

    def test_remote_credentials_paths_names_and_invalid_ports_are_refused(self):
        for url in ("https://127.0.0.1:9001", "http://example.invalid:9001", "http://localhost:9001",
                    "http://127.0.0.2:9001", "http://user:password@127.0.0.1:9001",
                    "http://127.0.0.1:9001/path", "http://127.0.0.1:9001?x=1",
                    "http://127.0.0.1:9001#fragment", "http://127.0.0.1", "http://127.0.0.1:0",
                    "http://127.0.0.1:65536", "http://127.0.0.1:9001/", "\nhttp://127.0.0.1:9001",
                    "http://127.0.0.1:\t9001", "HTTP://127.0.0.1:9001", "http://127.0.0.1:09001"):
            contract = deepcopy(self.contract)
            contract["assignments"]["batch-1"]["stripe_url"] = url
            reseal(self.protocol, contract)
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_cost_contract(self.protocol, contract)

    def test_unknown_fields_currency_and_policy_changes_are_rejected(self):
        for case in ("contract_field", "rule_field", "currency", "event_rules", "unknown_policy"):
            protocol, contract = deepcopy((self.protocol, self.contract))
            if case == "contract_field": contract["override_provider_cost"] = 0
            elif case == "rule_field": contract["rules"]["refresh"]["default"] = 0
            elif case == "currency": contract["currency"] = "EUR"
            elif case == "event_rules": protocol["accounting_policy"]["event_rules"]["refresh"]["allocation"] = "common_equal"
            else: protocol["accounting_policy"]["unknown_cost_policy"] = "assume_zero"
            reseal(protocol, contract)
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_cost_contract(protocol, contract)


if __name__ == "__main__":
    unittest.main()
