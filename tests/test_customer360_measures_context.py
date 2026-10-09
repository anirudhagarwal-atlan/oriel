"""Reviewed measure meaning over local CSV publications; no model/vendor calls.

Injected model replies exercise orchestration only. Historical rows constructed
below represent the prior receipt format, not permission to weaken new proposals.
"""
import copy
import json
import unittest

from contextbase.connectors import service
from contextbase.customer360 import agent, contexts, profiles, snapshots
from contextbase.customer360.contracts import canonical, sha256, validate_profile
from tests import test_customer360_profiles as fixtures
from tests.test_customer360_agent import Scripted, observation, response
from tests.test_customer360_contexts import definitions


def policy(unit="currency", *, aggregation="sum", currency_field="currency"):
    return {"unit": unit, "currency_field": None if unit == "number" else currency_field,
            "aggregation": aggregation, "meaning": "Explicit synthetic unit and aggregation review."}


def approved_shape(*policies):
    bindings = []
    for index, value in enumerate(policies):
        binding = {"connection": "source_" + str(index), "entity": "invoice", "table": "source_" + str(index) + ".records",
                   "kind": "billing", "record_id": "id", "customer_ref": {"connection": "source_" + str(index), "entity": "party", "column": "party"},
                   "fields": {"amount": "amount", "currency": "currency", "other_currency": "other_currency"}}
        if value is not None:
            binding["measures"] = {"amount": value}
        bindings.append(binding)
    return {"candidate": {"profile": {"bindings": bindings}}}


class MeasureDefinitionTests(unittest.TestCase):
    def test_absent_policy_is_legacy_only_and_count_needs_no_policy(self):
        summed = definitions()[:1]
        self.assertEqual(contexts.validate_definitions(summed, approved_shape(None)), summed)
        with self.assertRaisesRegex(ValueError, "reviewed measure policy"):
            contexts.validate_definitions(summed, approved_shape(None), require_measure_policy=True)
        counted = {**summed[0], "operation": "count", "field": None, "unit": "records", "currency_field": None}
        for value in (None, policy("unknown", aggregation="unknown"), policy(aggregation="none")):
            self.assertEqual(contexts.validate_definitions([counted], approved_shape(value), require_measure_policy=True), [counted])
        with self.assertRaises(ValueError):
            contexts.validate_definitions(summed, approved_shape(None), require_measure_policy=1)

    def test_known_major_minor_and_nonmonetary_units_match_exactly(self):
        for unit in ("currency", "minor_currency", "number"):
            declared = policy(unit)
            definition = {**definitions()[0], "unit": unit, "currency_field": declared["currency_field"]}
            with self.subTest(unit=unit):
                self.assertEqual(contexts.validate_definitions([definition], approved_shape(declared, copy.deepcopy(declared)),
                                                              require_measure_policy=True), [definition])
                other_unit = "number" if unit != "number" else "currency"
                changed = {**definition, "unit": other_unit, "currency_field": None if other_unit == "number" else "currency"}
                with self.assertRaisesRegex(ValueError, "match every reviewed"):
                    contexts.validate_definitions([changed], approved_shape(declared))

    def test_unknown_units_and_unknown_or_disallowed_aggregation_refuse_sums(self):
        for value in (policy("unknown"), policy(aggregation="unknown"), policy(aggregation="none")):
            for required in (False, True):
                with self.subTest(policy=value, required=required), self.assertRaisesRegex(ValueError, "must be confirmed"):
                    contexts.validate_definitions(definitions()[:1], approved_shape(value), require_measure_policy=required)

    def test_partial_or_conflicting_same_kind_bindings_refuse_even_operator_sums(self):
        for values in ((policy(), None), (policy(), policy("minor_currency")),
                       (policy(), policy(currency_field="other_currency")), (policy(), policy(aggregation="none"))):
            for required in (False, True):
                with self.subTest(policies=values, required=required), self.assertRaises(ValueError):
                    contexts.validate_definitions(definitions()[:1], approved_shape(*values), require_measure_policy=required)

    def test_currency_filter_cannot_turn_monetary_measure_into_unlabelled_number(self):
        definition = {**definitions()[0], "unit": "number", "currency_field": None,
                      "filters": [{"field": "currency", "operator": "eq", "value": "USD"}]}
        for required in (False, True):
            with self.subTest(required=required), self.assertRaisesRegex(ValueError, "match every reviewed"):
                contexts.validate_definitions([definition], approved_shape(policy()), require_measure_policy=required)

    def test_bootstrap_definition_stage_requires_reviewed_policy_before_storage(self):
        inspected = {"profile": approved_shape(None)["candidate"]["profile"]}
        action = {"definitions": definitions()[:1], "gaps": []}
        with self.assertRaisesRegex(ValueError, "reviewed measure policy"):
            agent._definitions(action, inspected)
        inspected["profile"] = approved_shape(policy())["candidate"]["profile"]
        self.assertIs(agent._definitions(action, inspected), action)


class MeasureContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w, self.snapshot = self.fixture.w, self.fixture.snapshot["snapshot_id"]
        self.profile = copy.deepcopy(self.fixture.profile)
        for binding in self.profile["bindings"]:
            binding.pop("measures", None)
        self.name = self.profile["name"]

    def install(self, value=None):
        self.profile["bindings"][0].pop("measures", None)
        if value is not None:
            self.profile["bindings"][0]["measures"] = {"amount": copy.deepcopy(value)}
        validate_profile(self.profile)
        candidate = profiles.propose_profile(self.w, self.profile, self.snapshot)
        approved = profiles.approve_profile(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-measure-reviewer")
        profiles.materialize(self.w, self.name, self.snapshot)
        return approved

    def approve(self, candidate):
        return contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-context-reviewer")

    def test_inspection_policy_is_approved_lineage_not_sql_type_inference(self):
        approved = self.install(policy("unknown", aggregation="none"))
        inspected = contexts.inspect_context(self.w, self.name, self.snapshot)
        self.assertEqual(inspected["profile"]["bindings"][0]["measures"]["amount"], policy("unknown", aggregation="none"))
        expected_codes = {"measure_unit_unknown", "measure_aggregation_disallowed"}
        metadata = inspected["measure_policy"]
        self.assertEqual({gap["code"] for gap in metadata["gaps"]}, expected_codes)
        self.assertEqual(metadata["profile_approval"], approved["approval_sha256"])
        self.assertEqual(metadata["approved_snapshot_id"], self.snapshot)
        self.assertEqual(metadata["origin"], "approved_profile")
        self.assertTrue(metadata["model_sums_require_policy"])
        self.assertNotIn("measures", inspected["profile"]["bindings"][1])  # Text fields do not acquire units.
        self.assertLessEqual(len(metadata["gaps"]), 32)
        self.assertTrue(all(len(gap["description"]) <= 2000 for gap in metadata["gaps"]))
        self.fixture.billing.write_text(self.fixture.billing.read_text().replace(",150,", ",175,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        refreshed = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.name, refreshed)
        current = contexts.inspect_context(self.w, self.name, refreshed)
        self.assertEqual(current["measure_policy"], metadata)
        self.assertEqual(current["snapshot_id"], refreshed)

    def test_new_model_and_scripted_sums_require_policy_but_operator_legacy_is_explicit(self):
        self.install()
        for origin in ("model_agent", "scripted_agent_test"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "reviewed measure policy"):
                contexts.propose_context(self.w, self.name, self.snapshot, definitions(), origin=origin)
        candidate = contexts.propose_context(self.w, self.name, self.snapshot, definitions())
        self.assertEqual(self.approve(candidate)["origin"], "operator_authored")

    def test_model_policy_lineage_survives_proposal_and_exact_approval(self):
        profile = self.install(policy())
        client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
        result = agent.bootstrap(self.w, self.name, self.snapshot, allow_model_data=True, client=client)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertIn("reviewed", call["system"])
            self.assertIn("never infer", call["system"].lower())
        self.assertIn("Currency is a result dimension", client.calls[1]["system"])
        inspected = json.loads(client.calls[1]["messages"][0]["content"])["source_evidence"]
        self.assertEqual(inspected["measure_policy"]["profile_approval"], profile["approval_sha256"])
        approved = self.approve(result["candidate"])
        self.assertEqual(approved["measure_policy"], inspected["measure_policy"])
        self.assertEqual(approved["definitions"], definitions())

    def test_unknown_policy_produces_counts_and_retains_gap_even_if_model_omits_it(self):
        self.install(policy("unknown", aggregation="unknown"))
        client = Scripted(observation, response({"definitions": definitions()[1:], "gaps": []}))
        result = agent.bootstrap(self.w, self.name, self.snapshot, allow_model_data=True, client=client)
        self.assertEqual(result["status"], "candidate")
        candidate = result["candidate"]
        self.assertEqual(candidate["definitions"], definitions()[1:])
        self.assertTrue({"measure_unit_unknown", "measure_aggregation_unknown"} <= {g["code"] for g in candidate["gaps"]})
        self.approve(candidate)

    def test_approval_rechecks_policy_for_prior_unapproved_model_candidate(self):
        self.install()
        candidate = contexts.propose_context(self.w, self.name, self.snapshot, definitions())
        legacy = {k: v for k, v in candidate.items() if k not in {"proposal_id", "approval_sha256", "status", "measure_policy"}}
        legacy["origin"] = "model_agent"
        legacy_id = sha256(legacy)
        with self.w._connect(writable=True) as conn:
            conn.execute("INSERT INTO _contextbase.customer360_context_candidates VALUES (?,?)", [legacy_id, canonical(legacy)])
        with self.assertRaisesRegex(ValueError, "reviewed measure policy"):
            contexts.approve_context(self.w, legacy_id, legacy_id, "synthetic-context-reviewer")

    def test_pre_policy_approved_model_context_remains_readable_without_rewriting(self):
        self.install()
        current = self.approve(contexts.propose_context(self.w, self.name, self.snapshot, definitions()))
        legacy = {k: v for k, v in current.items() if k not in {"context_id", "measure_policy"}}
        legacy["origin"] = "model_agent"
        legacy["context_id"] = sha256(legacy)
        raw = canonical(legacy)
        with self.w._connect(writable=True) as conn:
            conn.execute("UPDATE _contextbase.customer360_context_approvals SET context_id=?,payload=? WHERE context_id=?",
                         [legacy["context_id"], raw, current["context_id"]])
        before = contexts.current_context(self.w, self.name, self.snapshot)
        self.assertEqual(before["context_id"], legacy["context_id"])
        self.fixture.billing.write_text(self.fixture.billing.read_text().replace(",150,", ",175,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        refreshed = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.name, refreshed)
        after = contexts.current_context(self.w, self.name, refreshed)
        self.assertEqual(after["context_id"], legacy["context_id"])
        self.assertEqual(after["definition_sha256"], before["definition_sha256"])
        self.assertNotIn("measure_policy", after)
        with self.w._connect() as conn:
            self.assertEqual(conn.execute("SELECT payload FROM _contextbase.customer360_context_approvals WHERE context_id=?",
                                          [legacy["context_id"]]).fetchone()[0], raw)

    def test_policy_review_change_invalidates_context_and_pending_approval(self):
        self.install(policy())
        approved = self.approve(contexts.propose_context(self.w, self.name, self.snapshot, definitions(), origin="scripted_agent_test"))
        pending = contexts.propose_context(self.w, self.name, self.snapshot, definitions(), origin="scripted_agent_test")
        changed = policy()
        changed["meaning"] = "Revised explicit synthetic business meaning; same unit requires review again."
        self.install(changed)
        with self.assertRaisesRegex(ValueError, "source mappings changed"):
            contexts.current_context(self.w, self.name, self.snapshot)
        with self.assertRaisesRegex(ValueError, "Source mappings or customer evidence changed"):
            self.approve(pending)
        new = self.approve(contexts.propose_context(self.w, self.name, self.snapshot, definitions(), origin="scripted_agent_test"))
        self.assertNotEqual(new["context_id"], approved["context_id"])
        self.assertEqual(new["definition_sha256"], approved["definition_sha256"])
        self.assertNotEqual(new["measure_policy"]["profile_approval"], approved["measure_policy"]["profile_approval"])


if __name__ == "__main__":
    unittest.main()
