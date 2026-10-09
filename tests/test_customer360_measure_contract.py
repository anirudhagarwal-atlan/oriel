"""Reviewed measure policy contracts; synthetic records, no model/vendor calls."""
import unittest

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, profiles, snapshots
from contextbase.customer360.contracts import PROFILE_SPEC, sha256, validate_profile
from tests import test_customer360_profiles as fixtures


def policy(unit="minor_currency", currency_field="denomination", aggregation="sum"):
    return {"unit": unit, "currency_field": currency_field, "aggregation": aggregation,
            "meaning": "Source quantity retained in its explicitly reviewed unit."}


def binding(connection="source_a"):
    return {"connection": connection, "entity": "entries", "table": connection + ".records",
            "kind": "usage", "record_id": "entry_id",
            "customer_ref": {"connection": connection, "entity": "party", "column": "party_id"},
            "fields": {"quantity": "raw_q", "denomination": "raw_d", "category": "raw_c"}}


def profile(*bindings):
    return {"spec_version": PROFILE_SPEC, "name": "measure_review", "description": "Synthetic source policy",
            "bindings": list(bindings) or [binding()]}


def definition(unit="minor_currency", currency_field="denomination", *, kind="usage", operation="sum"):
    return {"name": "reviewed_quantity", "description": "Aggregate the reviewed source quantity.",
            "kind": kind, "operation": operation, "field": "quantity" if operation == "sum" else None,
            "filters": [], "unit": unit if operation == "sum" else "records",
            "currency_field": currency_field if operation == "sum" else None}


class MeasureContractTests(unittest.TestCase):
    def test_optional_legacy_mapping_and_detached_explicit_measure_policy(self):
        original = profile()
        self.assertNotIn("measures", validate_profile(original)["bindings"][0])
        original["bindings"][0]["measures"] = {"quantity": policy()}
        result = validate_profile(original)
        self.assertEqual(result, original)
        result["bindings"][0]["measures"]["quantity"]["meaning"] = "Changed detached description"
        self.assertNotEqual(result, original)

    def test_units_and_aggregation_declarations_are_explicit_without_vendor_names(self):
        for declared in (policy(), policy("currency"), policy("number", None),
                         policy("unknown", None, "unknown"), policy("unknown", "denomination", "none"),
                         policy("currency", "denomination", "none")):
            value = profile()
            value["bindings"][0]["measures"] = {"quantity": declared}
            with self.subTest(declaration=declared):
                self.assertEqual(validate_profile(value)["bindings"][0]["measures"]["quantity"], declared)

    def test_measure_and_policy_shapes_reject_partial_and_unrecognized_fields(self):
        invalid = [None, [], "quantity", {"quantity": None}, {"quantity": []},
                   {"quantity": {**policy(), "expression": "SUM(raw_q)"}}]
        invalid.extend({"quantity": {k: v for k, v in policy().items() if k != missing}} for missing in policy())
        for measures in invalid:
            value = profile()
            value["bindings"][0]["measures"] = measures
            with self.subTest(measures=measures), self.assertRaises(ValueError):
                validate_profile(value)

    def test_measure_roles_must_be_mapped_and_named(self):
        for role in ("missing", "raw_q", "SUM(quantity)", "../quantity", "", 1):
            value = profile()
            value["bindings"][0]["measures"] = {role: policy()}
            with self.subTest(role=role), self.assertRaises(ValueError):
                validate_profile(value)

    def test_currency_roles_cannot_be_missing_self_or_attached_to_number(self):
        invalid = [policy("currency", None), policy("minor_currency", None),
                   policy("currency", "missing"), policy("minor_currency", "quantity"),
                   policy("unknown", "quantity"), policy("unknown", "missing"),
                   policy("number", "denomination"), policy("currency", False)]
        for declared in invalid:
            value = profile()
            value["bindings"][0]["measures"] = {"quantity": declared}
            with self.subTest(declaration=declared), self.assertRaises(ValueError):
                validate_profile(value)

    def test_bounded_meaning_and_exact_enum_types(self):
        invalid = [("unit", "dollars"), ("unit", None), ("unit", True), ("unit", []),
                   ("aggregation", "avg"), ("aggregation", None), ("aggregation", True), ("aggregation", []),
                   ("meaning", ""), ("meaning", "   "), ("meaning", "x" * 2001),
                   ("meaning", "two\nlines"), ("meaning", None)]
        for key, replacement in invalid:
            value = profile()
            value["bindings"][0]["measures"] = {"quantity": {**policy(), key: replacement}}
            with self.subTest(field=key, replacement=replacement), self.assertRaises(ValueError):
                validate_profile(value)

    def test_measure_count_bound(self):
        value = profile()
        selected = value["bindings"][0]
        selected["fields"] = {"value_" + str(i): "column_" + str(i) for i in range(64)}
        selected["measures"] = {key: policy("number", None) for key in selected["fields"]}
        self.assertEqual(len(validate_profile(value)["bindings"][0]["measures"]), 64)
        selected["measures"]["excess"] = policy("number", None)
        with self.assertRaises(ValueError):
            validate_profile(value)



class MeasureApprovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.w
        self.snapshot = self.fixture.snapshot["snapshot_id"]
        self.name = self.fixture.profile["name"]
        self.fixture.profile["bindings"][0]["fields"] = {"quantity": "amount", "denomination": "currency"}

    def materialize(self):
        return profiles.materialize(self.w, self.name, self.snapshot)

    def test_policy_changes_approval_and_materialization_without_rewriting_source_or_projected_values(self):
        self.fixture.reviewed()
        before_receipt = self.materialize()
        before = self.fixture.read()["records"]
        before_source = self.w.query("SELECT * FROM billing.records ORDER BY invoice_id")["rows"]
        self.fixture.profile["bindings"][0]["measures"] = {"quantity": policy()}
        proposal = profiles.propose_profile(self.w, self.fixture.profile, self.snapshot)
        self.assertEqual(proposal["profile"]["bindings"][0]["measures"], {"quantity": policy()})
        self.assertEqual(sha256({k: v for k, v in proposal.items() if k not in {"proposal_id", "approval_sha256", "status"}}), proposal["approval_sha256"])
        with self.assertRaises(ValueError):
            profiles.approve_profile(self.w, proposal["proposal_id"], before_receipt["profile_approval"], "synthetic-reviewer")
        approved = profiles.approve_profile(self.w, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-reviewer")
        with self.assertRaises(ValueError):
            self.fixture.read()
        after_receipt = self.materialize()
        after = self.fixture.read()["records"]
        self.assertNotEqual(before_receipt["profile_approval"], approved["approval_sha256"])
        self.assertNotEqual(before_receipt["materialization_id"], after_receipt["materialization_id"])
        self.assertEqual(before, after)
        self.assertEqual(before_source, self.w.query("SELECT * FROM billing.records ORDER BY invoice_id")["rows"])

    def test_changed_measure_policy_invalidates_context_and_pending_definition_approval(self):
        self.fixture.profile["bindings"][0]["measures"] = {"quantity": policy()}
        self.fixture.reviewed()
        self.materialize()
        rule = definition(kind="billing")
        candidate = contexts.propose_context(self.w, self.name, self.snapshot, [rule])
        approved = contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")
        pending = contexts.propose_context(self.w, self.name, self.snapshot, [rule])
        self.fixture.profile["bindings"][0]["measures"]["quantity"] = policy("currency")
        self.fixture.reviewed()
        self.materialize()
        with self.assertRaises(ValueError):
            contexts.current_context(self.w, self.name, self.snapshot)
        with self.assertRaises(ValueError):
            contexts.approve_context(self.w, pending["proposal_id"], pending["approval_sha256"], "synthetic-reviewer")
        with self.assertRaises(ValueError):
            contexts.propose_context(self.w, self.name, self.snapshot, [rule])
        fresh = contexts.propose_context(self.w, self.name, self.snapshot, [definition("currency", kind="billing")])
        self.assertNotEqual(approved["profile_approval"], fresh["profile_approval"])

    def test_refresh_new_currency_keeps_exact_minor_units_and_unchanged_definition(self):
        self.fixture.profile["bindings"][0]["measures"] = {"quantity": policy()}
        self.fixture.reviewed()
        self.materialize()
        candidate = contexts.propose_context(self.w, self.name, self.snapshot, [definition(kind="billing")])
        context = contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")
        initial = answers.execute_metric(self.w, self.name, self.snapshot, self.fixture.customer["id"], "reviewed_quantity")
        self.assertEqual(initial["result"]["values"], [{"currency": "USD", "value": "400"}])

        # A new source currency is a refreshed fact, not a change of unit policy.
        self.fixture.billing.write_text("invoice_id,party_ref,amount,currency\ninv-a,party-a,150,USD\ninv-b,party-a,250,ZAR\ninv-c,missing,80,EUR\n")
        service.sync(self.w, "billing", bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        self.materialize()
        current = contexts.current_context(self.w, self.name, self.snapshot)
        self.assertEqual(current["context_id"], context["context_id"])
        self.assertEqual(current["definition_sha256"], context["definition_sha256"])
        result = answers.execute_metric(self.w, self.name, self.snapshot, self.fixture.customer["id"], "reviewed_quantity")
        self.assertEqual(result["result"]["unit"], "minor_currency")
        self.assertEqual(result["result"]["values"], [{"currency": "USD", "value": "150"}, {"currency": "ZAR", "value": "250"}])
        self.assertEqual(result["result"]["matched_records"], 2)


if __name__ == "__main__":
    unittest.main()
