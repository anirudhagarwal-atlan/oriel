from copy import deepcopy
import json
import unittest

from contextbase.rystic_eval import build_tasks, score_answer
from contextbase.rystic_recipe import FIXTURE_NAMESPACE, RECIPE_SPEC


def fixture():
    def charge(number, amount, refund=0, **changes):
        return {"charge_id": f"ch_{number}", "amount_cents": amount, "refund_cents": refund,
                "currency": "usd", "paid": True, "captured": True,
                "status": "succeeded", "disputed": False, **changes}

    def issue(number, priority=1, state="started", owner="Synthetic Owner"):
        return {"issue_id": f"issue_{number}", "identifier": f"SYN-{number}",
                "priority": priority, "state_type": state,
                "owner_id": "user_1" if owner else None, "owner_name": owner}

    def account(number, charges, issues, internal=False):
        return {"account_id": f"customer_{number:03d}", "customer_id": f"cus_{number}",
                "name": f"Example Company {number:03d}", "is_internal": internal,
                "charges": charges, "issues": issues}

    accounts = [
        account(1, [charge(1, 10001, 3333), charge(2, 999999, currency="eur"),
                    charge(3, 100000, paid=False), charge(4, 200000, captured=False),
                    charge(5, 300000, status="failed"), charge(6, 400000, disputed=True)],
                [issue(1), issue(2, 2, "unstarted", None), issue(3, 1, "completed"),
                 issue(4, 1, "canceled"), issue(5, 0), issue(6, 3)]),
        account(2, [charge(7, 1000, 1000)], [issue(7)]),
        account(3, [charge(8, 500)], [issue(8)], internal=True),
        account(4, [charge(9, 150)], [issue(9, 4)]),
    ]
    return {"spec": RECIPE_SPEC, "namespace": FIXTURE_NAMESPACE, "status": "complete",
            "synthetic": True, "account_count": len(accounts), "accounts": accounts}


SNAPSHOT = {"snapshot_id": "rystic-aaaaaaaaaaaaaaaa", "data_sha256": "a" * 64,
            "namespace": FIXTURE_NAMESPACE}


class RysticEvalTests(unittest.TestCase):
    def setUp(self):
        self.manifest = fixture()
        self.tasks = {task["id"]: task for task in build_tasks(self.manifest, SNAPSHOT)}

    def test_independent_cent_oracle_and_issue_owners(self):
        self.assertEqual(set(self.tasks), {"qualifying_accounts", "exposure_summary", "payment_controls", "single_account"})
        self.assertEqual(self.tasks["exposure_summary"]["expected"], {
            "account_count": 1, "qualifying_issue_count": 2, "net_collected_usd_cents": 6668,
            "provenance": {"snapshot_id": SNAPSHOT["snapshot_id"], "data_sha256": SNAPSHOT["data_sha256"]},
        })
        accounts = self.tasks["qualifying_accounts"]["expected"]["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0], {
            "account_id": "customer_001", "name": "Example Company 001", "net_collected_usd_cents": 6668,
            "issues": [{"issue_id": "issue_1", "identifier": "SYN-1", "owner_id": "user_1", "owner_name": "Synthetic Owner"},
                       {"issue_id": "issue_2", "identifier": "SYN-2", "owner_id": None, "owner_name": None}],
        })
        self.assertEqual(self.tasks["single_account"]["expected"]["account"], {**accounts[0], "qualifies": True})

    def test_control_task_covers_refunds_currency_internal_and_closed_states(self):
        controls = self.tasks["payment_controls"]["expected"]["accounts"]
        self.assertEqual(controls, [
            {"account_id": "customer_001", "eligible_gross_usd_cents": 10001,
             "eligible_refunded_usd_cents": 3333, "eligible_net_usd_cents": 6668,
             "qualifying_issue_count": 2, "qualifies": True, "exclusion_reasons": []},
            {"account_id": "customer_002", "eligible_gross_usd_cents": 1000,
             "eligible_refunded_usd_cents": 1000, "eligible_net_usd_cents": 0,
             "qualifying_issue_count": 1, "qualifies": False, "exclusion_reasons": ["no_positive_net_usd"]},
            {"account_id": "customer_003", "eligible_gross_usd_cents": 500,
             "eligible_refunded_usd_cents": 0, "eligible_net_usd_cents": 500,
             "qualifying_issue_count": 1, "qualifies": False, "exclusion_reasons": ["internal_account"]},
            {"account_id": "customer_004", "eligible_gross_usd_cents": 150,
             "eligible_refunded_usd_cents": 0, "eligible_net_usd_cents": 150,
             "qualifying_issue_count": 0, "qualifies": False,
             "exclusion_reasons": ["no_unresolved_high_priority_issue"]},
        ])

    def test_questions_and_schemas_exclude_gold_and_are_isolated(self):
        for task in self.tasks.values():
            public = json.dumps({key: task[key] for key in ("question", "answer_schema")})
            for hidden in ("6668", "3333", "10001", "Example Company", "Synthetic Owner", "issue_1"):
                self.assertNotIn(hidden, public)
            self.assertNotIn("const", public)
            self.assertIn(SNAPSHOT["data_sha256"], task["question"])
            self.assertEqual(set(task), {"id", "question", "answer_schema", "expected"})
            self.assertFalse(task["answer_schema"]["additionalProperties"])
        self.tasks["qualifying_accounts"]["expected"]["accounts"][0]["issues"][0]["owner_name"] = "Changed"
        self.assertEqual(self.manifest["accounts"][0]["issues"][0]["owner_name"], "Synthetic Owner")
        self.assertEqual(self.tasks["single_account"]["expected"]["account"]["issues"][0]["owner_name"], "Synthetic Owner")

    def test_all_gold_scores_correct_and_order_is_irrelevant(self):
        for task in self.tasks.values():
            self.assertEqual(score_answer(deepcopy(task["expected"]), task["expected"]), {"correct": True, "errors": []})
        expected = self.tasks["qualifying_accounts"]["expected"]
        actual = deepcopy(expected)
        actual["accounts"][0]["issues"].reverse()
        self.assertTrue(score_answer(actual, expected)["correct"])
        expected = self.tasks["payment_controls"]["expected"]
        actual = deepcopy(expected)
        actual["accounts"].reverse()
        self.assertTrue(score_answer(actual, expected)["correct"])

    def test_scoring_rejects_missing_extra_duplicate_or_misassigned_records(self):
        expected = self.tasks["qualifying_accounts"]["expected"]
        mutations = [
            lambda answer: answer["accounts"].clear(),
            lambda answer: answer["accounts"].append(deepcopy(answer["accounts"][0])),
            lambda answer: answer["accounts"][0]["issues"].pop(),
            lambda answer: answer["accounts"][0]["issues"].append(deepcopy(answer["accounts"][0]["issues"][0])),
            lambda answer: answer["accounts"][0]["issues"][0].update(owner_id=None, owner_name=None),
            lambda answer: answer["accounts"][0].update(explanation="An unrequested field"),
            lambda answer: answer["accounts"][0].update(net_collected_usd_cents=13336),
        ]
        for mutate in mutations:
            actual = deepcopy(expected)
            mutate(actual)
            result = score_answer(actual, expected)
            self.assertFalse(result["correct"])
            self.assertTrue(result["errors"])
        actual = deepcopy(expected)
        duplicate = deepcopy(actual["accounts"][0])
        duplicate["name"] = "Different value with the same account identity"
        actual["accounts"].append(duplicate)
        self.assertIn("duplicate account_id", " ".join(score_answer(actual, expected)["errors"]))

    def test_money_and_counts_require_integer_json_not_bool_float_or_string(self):
        expected = self.tasks["exposure_summary"]["expected"]
        for key, values in (("net_collected_usd_cents", [6668.0, "6668", 66.68, True]),
                            ("account_count", [True, 1.0, "1"])):
            for value in values:
                with self.subTest(key=key, value=value):
                    actual = deepcopy(expected)
                    actual[key] = value
                    self.assertFalse(score_answer(actual, expected)["correct"])

    def test_non_json_and_nonfinite_results_fail_without_coercion(self):
        expected = self.tasks["exposure_summary"]["expected"]
        for value in (float("nan"), float("inf"), float("-inf"), {1, 2}, (1, 2), object()):
            actual = deepcopy(expected)
            actual["net_collected_usd_cents"] = value
            self.assertFalse(score_answer(actual, expected)["correct"])
        actual = deepcopy(expected)
        actual[3] = "not a JSON object key"
        self.assertFalse(score_answer(actual, expected)["correct"])
        self.assertFalse(score_answer(json.dumps(expected), expected)["correct"])

    def test_freshness_attribution_requires_supplied_receipt_fields(self):
        expected = self.tasks["exposure_summary"]["expected"]
        for change in (lambda answer: answer.pop("provenance"),
                       lambda answer: answer["provenance"].pop("data_sha256"),
                       lambda answer: answer["provenance"].update(data_sha256="b" * 64),
                       lambda answer: answer["provenance"].update(snapshot_id="older")):
            actual = deepcopy(expected)
            change(actual)
            self.assertFalse(score_answer(actual, expected)["correct"])
        tasks = build_tasks(self.manifest, {"snapshot_id": "frozen-example"})
        self.assertEqual(tasks[0]["expected"]["provenance"], {"snapshot_id": "frozen-example"})
        tasks = build_tasks(self.manifest, {})
        self.assertEqual(tasks[0]["expected"]["provenance"], {})

    def test_invalid_manifests_fail_closed_before_generating_gold(self):
        mutations = [
            lambda manifest: manifest.update(status="incomplete"),
            lambda manifest: manifest.update(synthetic=False),
            lambda manifest: manifest.update(account_count=99),
            lambda manifest: manifest["accounts"][0].update(is_internal="false"),
            lambda manifest: manifest["accounts"][0]["charges"][0].update(amount_cents=True),
            lambda manifest: manifest["accounts"][0]["charges"][0].update(refund_cents=10002),
            lambda manifest: manifest["accounts"][0]["charges"][0].update(paid=1),
            lambda manifest: manifest["accounts"][0]["issues"][0].update(priority=True),
            lambda manifest: manifest["accounts"][0]["issues"][0].pop("owner_id"),
            lambda manifest: manifest["accounts"][1].update(account_id="customer_001"),
            lambda manifest: manifest["accounts"][1]["charges"][0].update(charge_id="ch_1"),
            lambda manifest: manifest["accounts"][1]["issues"][0].update(issue_id="issue_1"),
        ]
        for mutate in mutations:
            manifest = deepcopy(self.manifest)
            mutate(manifest)
            with self.assertRaises(ValueError):
                build_tasks(manifest, SNAPSHOT)
        for snapshot in ({"data_sha256": "invalid"}, {"namespace": "different"}, {"snapshot_id": None}):
            with self.assertRaises(ValueError):
                build_tasks(self.manifest, snapshot)

    def test_zero_qualifying_accounts_is_valid_and_single_lookup_still_exists(self):
        self.manifest["accounts"][0]["is_internal"] = True
        tasks = {task["id"]: task for task in build_tasks(self.manifest, SNAPSHOT)}
        self.assertEqual(tasks["qualifying_accounts"]["expected"]["accounts"], [])
        self.assertEqual(tasks["exposure_summary"]["expected"]["net_collected_usd_cents"], 0)
        self.assertFalse(tasks["single_account"]["expected"]["account"]["qualifies"])
        self.assertTrue(score_answer(tasks["qualifying_accounts"]["expected"], tasks["qualifying_accounts"]["expected"])["correct"])


if __name__ == "__main__":
    unittest.main()
