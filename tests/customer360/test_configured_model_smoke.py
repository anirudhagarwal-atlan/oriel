"""Fresh opt-in actual model acceptance; never replay a prior successful run."""
import unittest

from tests.customer360.configured_model_runtime import QUESTIONS, digest, execute_smoke


class ConfiguredModelSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_smoke()
        cls.profiles = {p["profile"]: p for p in cls.result["profiles"]}

    def _profile(self, name):
        profile = self.profiles[name]
        receipt = profile["receipt"]
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["interaction"], "chrome_cdp_actual_dom_forms_and_clicks")
        self.assertTrue(receipt["configured_model_enabled"])
        self.assertFalse(receipt["scripted_model_enabled"])
        self.assertFalse(receipt["vendor_adapter_verified"])
        self.assertEqual(receipt["console_errors"], [])
        self.assertEqual(receipt["blocked_requests"], [])
        self.assertEqual(receipt["browser_cleanup"], {"main_process_exited": True, "isolated_profile_removed": True})
        self.assertEqual(len(profile["reviews"]), 3)
        bootstrap = next(j["result"] for j in profile["jobs"] if j["operation"] == "context.bootstrap")
        mapping = next(j["result"] for j in profile["jobs"] if j["operation"] == "profile.approve")
        policy = next(b["measures"]["amount"] for b in mapping["candidate"]["profile"]["bindings"]
                      if "amount" in b.get("measures", {}))
        self.assertEqual(policy["unit"], "minor_currency" if name == "billing-support" else "currency")
        self.assertEqual(policy["currency_field"], "currency")
        self.assertEqual(policy["aggregation"], "sum")
        self.assertEqual(bootstrap["candidate"]["profile_approval"], mapping["approval_sha256"])
        self.assertIn("explicit_measure_policy_form_submission_and_review", receipt["verified"])
        self.assertEqual(bootstrap["model_origin"], "configured_claude_cli")
        self.assertFalse(bootstrap["approval_performed"])
        self.assertEqual(receipt["bootstrap_intent"], QUESTIONS[name])
        self.assertEqual([stage["intent"] for stage in bootstrap["candidate"]["stages"]], [QUESTIONS[name]] * 2)
        self.assertEqual([stage["intent_sha256"] for stage in bootstrap["candidate"]["stages"]], [digest(QUESTIONS[name])] * 2)
        self.assertEqual(next(op["intent"] for op in receipt["operations"] if op["operation"] == "context.bootstrap"), QUESTIONS[name])
        self.assertIn("operator_bootstrap_intent_entered_and_retained", receipt["verified"])
        self.assertIn("deterministic_calculation_scope_separate_from_provisional_notes", receipt["verified"])
        self.assertEqual(profile["reviews"][0]["decision"]["extra_defs_need_review"], [])
        self.assertEqual(profile["reviews"][0]["definitions"], bootstrap["candidate"]["definitions"])
        for review in profile["reviews"][1:]:
            self.assertTrue(review["decision"]["exact_currency_totals"])
            self.assertTrue(review["decision"]["exact_source_attribution"])
            self.assertTrue(review["decision"]["latest_substantive_passage"])
            self.assertTrue(review["decision"]["latest_selection_verified"])
        for job in profile["jobs"]:
            if job["operation"] != "ask":
                continue
            answer = job["result"]["answer"]
            for metric in answer["metrics"]:
                scope = metric["calculation_scope"]
                self.assertEqual(scope["population"], "selected_customer_resolved_records")
                for key in ("operation", "kind", "field", "filters", "unit", "currency_field"):
                    self.assertEqual(scope[key], metric["definition"][key])
                self.assertEqual(scope["snapshot_id"], answer["snapshot_id"])
                self.assertEqual(scope["customer_id"], answer["customer_id"])
                self.assertNotIn("description", scope)
        return profile

    def test_real_model_bootstraps_and_answers_crm_conversation_profile(self):
        self._profile("salesforce-gong")

    def test_real_model_bootstraps_and_answers_non_crm_profile(self):
        self._profile("billing-support")

    def test_real_model_refresh_answer_matches_changed_evidence(self):
        for profile in self.profiles.values():
            asks = [j["result"]["answer"] for j in profile["jobs"] if j["operation"] == "ask"]
            self.assertEqual(len(asks), 2)
            self.assertNotEqual(asks[0]["snapshot_id"], asks[1]["snapshot_id"])
            self.assertEqual(asks[0]["context_id"], asks[1]["context_id"])
            self.assertEqual(asks[0]["definition_sha256"], asks[1]["definition_sha256"])
            self.assertNotEqual(asks[0]["metrics"], asks[1]["metrics"])
            self.assertNotEqual(asks[0]["quotes"], asks[1]["quotes"])
            for index, answer in enumerate(asks):
                self.assertEqual(answer["context_approved_snapshot_id"], asks[0]["snapshot_id"])
                self.assertEqual(answer["context_notes_not_rechecked_after_refresh"], bool(index))
                for gap in answer["gaps"]:
                    self.assertIn(gap["origin"], ("approved_context", "current_projection"))
                    if gap["origin"] == "approved_context":
                        self.assertEqual(gap["source_snapshot_id"], asks[0]["snapshot_id"])
                        self.assertEqual(gap["not_rechecked_after_refresh"], bool(index))
                        self.assertEqual(gap["context_id"], answer["context_id"])
                    else:
                        self.assertEqual(gap["source_snapshot_id"], answer["snapshot_id"])
            self.assertEqual([r["phase"] for r in profile["reviews"][1:]], ["initial", "refreshed"])

    def test_real_usage_and_all_stage_costs_respect_declared_budget(self):
        ledger = self.result["ledger"]
        self.assertEqual(ledger["status"], "complete")
        self.assertEqual(len(ledger["entries"]), 6)
        self.assertEqual(ledger["total_reserved_usd"], 3.0)
        self.assertFalse(ledger["spend_unknown"])
        self.assertLessEqual(ledger["known_cost_usd"], 3.0)
        self.assertGreater(ledger["known_cost_usd"], 0)
        self.assertEqual(len(ledger["model_ids"]), 1)
        for entry in ledger["entries"]:
            self.assertEqual(entry["status"], "reviewed")
            self.assertLessEqual(entry["reported_cost_usd"], .5)
            self.assertTrue(entry["usage"]["cost_complete"])
            self.assertGreater(entry["usage"]["model_calls"], 0)
            self.assertLessEqual(entry["usage"]["model_calls"], 4)
            if entry["operation"] == "context.bootstrap":
                self.assertEqual(entry["intent"], QUESTIONS[entry["profile"]])


if __name__ == "__main__":
    unittest.main()
