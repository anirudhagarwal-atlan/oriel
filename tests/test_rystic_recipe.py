from copy import deepcopy
from collections import Counter
from decimal import Decimal
import hashlib
import json
import unittest

import duckdb

from contextbase.rystic_recipe import (
    ACCOUNT_REPORT_SQL, FIXTURE_NAMESPACE, NET_EXPOSED_SQL, RECIPE_SPEC,
    RecipeSeedError, expected_answer, seed_recipe,
)


class FakeStripe:
    def __init__(self):
        self.customers = []
        self.charges = []
        self.refunds = []
        self.calls = []
        self.fail_on_refund = False

    def request(self, method, path, query=None, form=None, json_body=None):
        self.calls.append((method, path, deepcopy(query), deepcopy(form)))
        if method == "GET" and path == "/v1/customers":
            offset = 0
            if query.get("starting_after"):
                offset = next(i for i, c in enumerate(self.customers)
                              if c["id"] == query["starting_after"]) + 1
            rows = self.customers[offset:offset + 100]
            return {"data": deepcopy(rows), "has_more": offset + len(rows) < len(self.customers)}
        if method == "POST" and path == "/v1/customers":
            result = {"id": f"cus_{len(self.customers) + 1:03d}", "name": form["name"],
                      "metadata": {key[len("metadata["):-1]: value for key, value in form.items()
                                   if key.startswith("metadata[")}}
            self.customers.append(result)
            return deepcopy(result)
        if method == "POST" and path == "/v1/charges":
            result = {"id": f"ch_{len(self.charges) + 1:03d}", **form}
            self.charges.append(result)
            return deepcopy(result)
        if method == "POST" and path == "/v1/refunds":
            if self.fail_on_refund:
                raise ValueError("simulated source failure")
            result = {"id": f"re_{len(self.refunds) + 1:03d}", **form}
            self.refunds.append(result)
            return deepcopy(result)
        raise AssertionError(f"Unexpected request {method} {path}")


class FakeLinear:
    def __init__(self):
        self.issues = []
        self.calls = []
        self.states = [
            {"id": f"state_{state}", "type": state, "name": state.title()}
            for state in ("backlog", "unstarted", "started", "completed", "canceled")
        ]
        self.users = [{"id": "user_1", "name": "Synthetic Operator One"},
                      {"id": "user_2", "name": "Synthetic Operator Two"}]
        self.envelope = False

    def graphql(self, query, variables=None):
        self.calls.append((query, deepcopy(variables)))
        if "FixtureIssues" in query:
            offset = int((variables or {}).get("after") or 0)
            rows = self.issues[offset:offset + 100]
            data = {"issues": {"nodes": deepcopy(rows), "pageInfo": {
                "hasNextPage": offset + len(rows) < len(self.issues),
                "endCursor": str(offset + len(rows)),
            }}}
        elif "FixtureSetup" in query:
            data = {"teams": {"nodes": [{"id": "team_1", "name": "Synthetic Team",
                                         "states": {"nodes": deepcopy(self.states)}}]},
                    "users": {"nodes": deepcopy(self.users)}}
        elif "FixtureIssueCreate" in query:
            number = len(self.issues) + 1
            issue = {"id": f"issue_{number:03d}", "identifier": f"SYN-{number}",
                     **deepcopy(variables["input"])}
            self.issues.append(issue)
            data = {"issueCreate": {"success": True, "issue": issue}}
        else:
            raise AssertionError(f"Unexpected GraphQL operation {query}")
        return {"data": data} if self.envelope else data


class FakePair:
    def __init__(self):
        self.stripe = FakeStripe()
        self.linear = FakeLinear()
        self.verifications = 0

    def verify(self):
        self.verifications += 1
        return {"stripe": {"version": "0.0.8"}, "linear": {"version": "0.0.1"}}


def normalized_database(manifest):
    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA stripe")
    conn.execute("CREATE SCHEMA linear")
    conn.execute("""CREATE TABLE stripe.customers (
        customer_id VARCHAR, account_id VARCHAR, name VARCHAR, is_internal BOOLEAN)""")
    conn.execute("""CREATE TABLE stripe.charges (
        charge_id VARCHAR, customer_id VARCHAR, amount_cents BIGINT,
        amount_refunded_cents BIGINT, currency VARCHAR, paid BOOLEAN, captured BOOLEAN,
        status VARCHAR, disputed BOOLEAN, created_at VARCHAR)""")
    conn.execute("""CREATE TABLE linear.issues (
        issue_id VARCHAR, identifier VARCHAR, account_id VARCHAR, title VARCHAR,
        priority INTEGER, state_type VARCHAR, state_name VARCHAR, owner_id VARCHAR,
        owner_name VARCHAR, updated_at VARCHAR)""")
    for account in manifest["accounts"]:
        conn.execute("INSERT INTO stripe.customers VALUES (?, ?, ?, ?)", [
            account["customer_id"], account["account_id"], account["name"], account["is_internal"],
        ])
        for charge in account["charges"]:
            conn.execute("INSERT INTO stripe.charges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                charge["charge_id"], account["customer_id"], charge["amount_cents"],
                charge["refund_cents"], charge["currency"], charge["paid"], charge["captured"],
                charge["status"], charge["disputed"], "2026-10-09T00:00:00Z",
            ])
        for issue in account["issues"]:
            conn.execute("INSERT INTO linear.issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                issue["issue_id"], issue["identifier"], account["account_id"], issue["title"],
                issue["priority"], issue["state_type"], issue["state_name"], issue["owner_id"],
                issue["owner_name"], "2026-10-09T00:00:00Z",
            ])
    return conn


class RysticRecipeTests(unittest.TestCase):
    def test_default_demo_manifest_and_api_sequence_are_unchanged(self):
        def sha(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

        pair = FakePair()
        manifest = seed_recipe(pair)
        # These digests were recorded from the original implementation before
        # adding variants; they cover every authored field and ordinary API call.
        self.assertEqual(sha(manifest), "79c00136e4716cf77145a013b449452d6f8a8ff7eef7e766dc189c6b7d3583ec")
        self.assertEqual(sha([pair.stripe.calls, pair.linear.calls]),
                         "9d735091c3539d7c7f5954bab44eeb464bf8e15fa511104f8aec6d55c88dc5a0")
        self.assertEqual(manifest, seed_recipe(FakePair(), variant="demo_v1"))
        self.assertNotIn("variant", manifest)

    @staticmethod
    def semantic_case(account):
        """Classify actual policy edges without amount, IDs, names, or ordering."""
        return (
            account["is_internal"],
            tuple(sorted((charge["currency"], "full" if charge["refund_cents"] == charge["amount_cents"] else
                          "partial" if charge["refund_cents"] else "none") for charge in account["charges"])),
            tuple(sorted((issue["priority"], issue["state_type"], issue["owner_id"] is None) for issue in account["issues"])),
        )

    def test_variant_reproducibly_varies_cases_labels_and_insertion_order(self):
        first, second = FakePair(), FakePair()
        a = seed_recipe(first, seed=42, variant="shuffled_cases_v1")
        b = seed_recipe(second, seed=42, variant="shuffled_cases_v1")
        self.assertEqual(a, b)
        self.assertEqual(first.stripe.calls, second.stripe.calls)
        self.assertEqual(first.linear.calls, second.linear.calls)
        different = seed_recipe(FakePair(), seed=43, variant="shuffled_cases_v1")
        self.assertNotEqual(a, different)
        self.assertNotEqual([x["account_id"] for x in a["accounts"]], [x["account_id"] for x in different["accounts"]])
        self.assertNotEqual([self.semantic_case(x) for x in a["accounts"]], [self.semantic_case(x) for x in different["accounts"]])
        self.assertEqual(a["variant"], "shuffled_cases_v1")
        self.assertEqual(a["variation"]["split_assignment"], "not_assigned")
        self.assertEqual([customer["metadata"]["account_id"] for customer in first.stripe.customers],
                         [account["account_id"] for account in a["accounts"]])

    def test_no_fixed_customer_index_predicts_semantic_case_across_seeds(self):
        source_positions = [set() for _ in range(12)]
        sorted_label_positions = [set() for _ in range(12)]
        demo = seed_recipe(FakePair())
        expected_cases = Counter(self.semantic_case(x) for x in demo["accounts"])
        for seed in range(12):
            fixture = seed_recipe(FakePair(), seed=seed, variant="shuffled_cases_v1")
            accounts = fixture["accounts"]
            self.assertEqual(Counter(self.semantic_case(x) for x in accounts), expected_cases)
            for index, account in enumerate(accounts):
                self.assertEqual(account["customer_id"], f"cus_{index + 1:03d}")
                source_positions[index].add(self.semantic_case(account))
            for index, account in enumerate(sorted(accounts, key=lambda row: row["account_id"])):
                sorted_label_positions[index].add(self.semantic_case(account))
        self.assertTrue(all(len(cases) > 1 for cases in source_positions))
        self.assertTrue(all(len(cases) > 1 for cases in sorted_label_positions))

    def test_variant_preserves_unique_explicit_mappings_and_supported_api_shapes(self):
        pair = FakePair()
        manifest = seed_recipe(pair, account_count=100, seed=2**32 - 1, variant="shuffled_cases_v1")
        accounts = manifest["accounts"]
        ids = {account["account_id"] for account in accounts}
        self.assertEqual(len(ids), 100)
        self.assertEqual(len({account["name"] for account in accounts}), 100)
        self.assertTrue(all(account["account_id"].startswith("customer_") for account in accounts))
        source_customers = {customer["id"]: customer for customer in pair.stripe.customers}
        source_issues = {issue["id"]: issue for issue in pair.linear.issues}
        for account in accounts:
            self.assertEqual(source_customers[account["customer_id"]]["metadata"]["account_id"], account["account_id"])
            for issue in account["issues"]:
                source = source_issues[issue["issue_id"]]
                self.assertIn(f"contextbase_account_id={account['account_id']}", source["description"].splitlines())
                self.assertEqual(source["priority"], issue["priority"])
                self.assertEqual(source["stateId"], issue["state_id"])
        for method, path, _, form in pair.stripe.calls:
            if method == "POST" and path == "/v1/charges":
                self.assertEqual(form["source"], "tok_visa")
                self.assertEqual(form["capture"], "true")
                self.assertEqual(form["metadata[account_id]"], source_customers[form["customer"]]["metadata"]["account_id"])
                self.assertEqual(set(form), {"amount", "currency", "customer", "source", "capture",
                                             "metadata[contextbase_fixture]", "metadata[account_id]", "metadata[is_internal]"})
        self.assertEqual({path for method, path, _, _ in pair.stripe.calls if method == "POST"},
                         {"/v1/customers", "/v1/charges", "/v1/refunds"})

    def test_variant_oracle_agrees_with_sql_at_multiple_sizes_and_seeds(self):
        for count, seed in ((4, 0), (12, 7), (25, 42)):
            with self.subTest(account_count=count, seed=seed):
                manifest = seed_recipe(FakePair(), count, seed, variant="shuffled_cases_v1")
                expected = expected_answer(manifest)
                with normalized_database(manifest) as conn:
                    result = conn.execute(ACCOUNT_REPORT_SQL)
                    names = [column[0] for column in result.description]
                    actual = [dict(zip(names, row)) for row in result.fetchall()]
                    self.assertEqual(actual, expected["accounts"])
                    self.assertEqual(conn.execute(NET_EXPOSED_SQL).fetchone()[0], Decimal(expected["net_exposed_collected_usd"]))

    def test_invalid_variants_fail_before_any_source_call(self):
        for variant in (None, True, 1, [], {}, "", "shuffled_cases", "DEMO_V1", "demo_v1 "):
            with self.subTest(variant=variant):
                pair = FakePair()
                with self.assertRaisesRegex(ValueError, "variant must"):
                    seed_recipe(pair, variant=variant)
                self.assertEqual(pair.verifications, 0)
                self.assertEqual(pair.stripe.calls, [])
                self.assertEqual(pair.linear.calls, [])

    def test_seed_uses_ordinary_apis_and_persists_all_ids(self):
        pair = FakePair()
        manifest = seed_recipe(pair)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["account_count"], 12)
        self.assertEqual(len(pair.stripe.customers), 12)
        self.assertEqual(len(pair.stripe.charges), 24)
        self.assertEqual(pair.verifications, 1)
        self.assertTrue(manifest["synthetic"])
        self.assertIn("Rystic", manifest["owner_provenance"])
        for account in manifest["accounts"]:
            self.assertTrue(account["customer_id"])
            self.assertTrue(all(charge["charge_id"] for charge in account["charges"]))
            self.assertTrue(all(issue["issue_id"] and issue["identifier"] for issue in account["issues"]))
        for issue in pair.linear.issues:
            self.assertIn(f"contextbase_fixture={FIXTURE_NAMESPACE}", issue["description"].splitlines())
            self.assertTrue(any(line.startswith("contextbase_account_id=customer_")
                                for line in issue["description"].splitlines()))
        self.assertEqual({path for method, path, _, _ in pair.stripe.calls if method == "POST"},
                         {"/v1/customers", "/v1/charges", "/v1/refunds"})

    def test_arithmetic_and_sql_agree_for_fixture_and_common_traps(self):
        manifest = seed_recipe(FakePair())
        expected = expected_answer(manifest)
        self.assertEqual([account["account_id"] for account in expected["accounts"]],
                         ["customer_001", "customer_002", "customer_008", "customer_010", "customer_012"])
        self.assertEqual(expected["accounts"][0]["open_high_priority_issue_count"], 2)
        self.assertEqual(expected["accounts"][2]["owners"], ["Unassigned"])
        with normalized_database(manifest) as conn:
            result = conn.execute(ACCOUNT_REPORT_SQL)
            names = [column[0] for column in result.description]
            actual = [dict(zip(names, row)) for row in result.fetchall()]
            self.assertEqual(actual, expected["accounts"])
            self.assertEqual(conn.execute(NET_EXPOSED_SQL).fetchone()[0],
                             Decimal(expected["net_exposed_collected_usd"]))

    def test_oracle_uses_authored_cents_not_float_or_created_ids(self):
        base_charge = {"currency": "usd", "paid": True, "captured": True,
                       "status": "succeeded", "disputed": False,
                       "amount_cents": 10001, "refund_cents": 3333}
        manifest = {"spec": RECIPE_SPEC, "namespace": FIXTURE_NAMESPACE, "accounts": [{
            "account_id": "customer_001", "name": "Example Company 001", "is_internal": False,
            "charges": [base_charge, {**base_charge, "currency": "eur"},
                        {**base_charge, "paid": False}, {**base_charge, "disputed": True},
                        {**base_charge, "captured": False}, {**base_charge, "status": "failed"}],
            "issues": [{"priority": 1, "state_type": "started", "owner_name": "Synthetic Owner"},
                       {"priority": 2, "state_type": "unstarted", "owner_name": "Synthetic Owner"},
                       {"priority": 1, "state_type": "completed", "owner_name": "Other Synthetic Owner"},
                       {"priority": 1, "state_type": "unknown", "owner_name": "Other Synthetic Owner"}],
        }]}
        answer = expected_answer(manifest)
        self.assertEqual(answer["net_exposed_collected_usd_cents"], 6668)
        self.assertEqual(answer["net_exposed_collected_usd"], "66.68")
        self.assertEqual(answer["accounts"][0]["open_high_priority_issue_count"], 2)
        self.assertEqual(answer["accounts"][0]["owners"], ["Synthetic Owner"])
        invalid = deepcopy(manifest)
        invalid["accounts"][0]["charges"][0]["amount_cents"] = 10001.0
        with self.assertRaisesRegex(ValueError, "integer cents"):
            expected_answer(invalid)

    def test_duplicate_namespace_is_found_after_first_page_before_mutations(self):
        for duplicate_source in ("stripe", "linear"):
            with self.subTest(duplicate_source=duplicate_source):
                pair = FakePair()
                pair.stripe.customers = [{"id": f"old_cus_{i}", "metadata": {}} for i in range(101)]
                pair.linear.issues = [{"id": f"old_issue_{i}", "description": "Unrelated synthetic fixture"}
                                      for i in range(101)]
                if duplicate_source == "stripe":
                    pair.stripe.customers[-1]["metadata"]["contextbase_fixture"] = FIXTURE_NAMESPACE
                else:
                    pair.linear.issues[-1]["description"] = f"contextbase_fixture={FIXTURE_NAMESPACE}"
                with self.assertRaisesRegex(ValueError, "refusing duplicates"):
                    seed_recipe(pair)
                self.assertFalse(any(method != "GET" for method, *_ in pair.stripe.calls))
                self.assertFalse(any("mutation" in query for query, _ in pair.linear.calls))

    def test_same_pair_cannot_be_seeded_twice(self):
        pair = FakePair()
        seed_recipe(pair, 4)
        with self.assertRaisesRegex(ValueError, "refusing duplicates"):
            seed_recipe(pair, 4)
        self.assertEqual(len(pair.stripe.customers), 4)

    def test_enveloped_graphql_and_no_owner_are_supported(self):
        pair = FakePair()
        pair.linear.envelope = True
        pair.linear.users = []
        manifest = seed_recipe(pair, 4)
        self.assertEqual(expected_answer(manifest)["accounts"][0]["owners"], ["Unassigned"])
        self.assertTrue(all("assigneeId" not in issue for issue in pair.linear.issues))

    def test_missing_state_or_invalid_bounds_cannot_write(self):
        pair = FakePair()
        pair.linear.states = [state for state in pair.linear.states if state["type"] != "canceled"]
        with self.assertRaisesRegex(ValueError, "existing Linear team"):
            seed_recipe(pair)
        self.assertFalse(pair.stripe.customers)
        for count in (3, 101, True, 4.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                seed_recipe(FakePair(), count)
        for seed in (-1, True, 2**32, 1.5):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                seed_recipe(FakePair(), seed=seed)

    def test_partial_failure_retains_recovery_manifest_without_source_reset(self):
        pair = FakePair()
        pair.stripe.fail_on_refund = True
        with self.assertRaises(RecipeSeedError) as raised:
            seed_recipe(pair, 4)
        partial = raised.exception.partial_manifest
        self.assertEqual(partial["status"], "incomplete")
        self.assertEqual(len(pair.stripe.customers), 1)
        self.assertEqual(len(pair.stripe.charges), 2)
        self.assertTrue(partial["accounts"][0]["customer_id"])
        self.assertTrue(partial["accounts"][0]["charges"][1]["charge_id"])
        self.assertFalse(partial["accounts"][0]["charges"][1]["refund_ids"])
        self.assertIsNone(partial["accounts"][1]["customer_id"])
        with self.assertRaisesRegex(ValueError, "refusing duplicates"):
            seed_recipe(pair, 4)


if __name__ == "__main__":
    unittest.main()
