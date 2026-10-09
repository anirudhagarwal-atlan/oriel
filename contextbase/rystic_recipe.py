"""Synthetic Stripe + Linear recipe, seeded through ordinary simulator APIs.

This is a functional fixture and an independent arithmetic oracle, not a model
benchmark. The metric is net collected USD attached to unresolved urgent/high
issues. It is neither ARR nor a prediction of churn or lost revenue.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import random
from typing import Any


RECIPE_SPEC = "contextbase.rystic-recipe/v1"
FIXTURE_NAMESPACE = "contextbase_stripe_linear_v1"
NAMESPACE = FIXTURE_NAMESPACE
UNRESOLVED_STATE_TYPES = ("backlog", "unstarted", "started")
RECIPE_VARIANTS = ("demo_v1", "shuffled_cases_v1")

ACCOUNT_REPORT_SQL = """WITH collected AS (
  SELECT c.account_id, c.name,
    SUM(ch.amount_cents - ch.amount_refunded_cents) AS net_collected_usd_cents
  FROM stripe.customers c
  JOIN stripe.charges ch ON ch.customer_id = c.customer_id
  WHERE c.is_internal = FALSE
    AND ch.currency = 'usd'
    AND ch.paid = TRUE AND ch.captured = TRUE
    AND ch.status = 'succeeded' AND ch.disputed = FALSE
  GROUP BY c.account_id, c.name
), unresolved AS (
  SELECT account_id, COUNT(*) AS open_high_priority_issue_count,
    list_sort(list(DISTINCT COALESCE(NULLIF(owner_name, ''), 'Unassigned'))) AS owners
  FROM linear.issues
  WHERE priority IN (1, 2)
    AND state_type IN ('backlog', 'unstarted', 'started')
  GROUP BY account_id
)
SELECT c.account_id, c.name, c.net_collected_usd_cents,
  u.open_high_priority_issue_count, u.owners
FROM collected c
JOIN unresolved u ON u.account_id = c.account_id
WHERE c.net_collected_usd_cents > 0
ORDER BY c.account_id"""

NET_EXPOSED_SQL = (
    "SELECT CAST(COALESCE(SUM(net_collected_usd_cents), 0) / 100.0 "
    "AS DECIMAL(18, 2)) AS net_exposed_collected_usd\n"
    "FROM (\n" + ACCOUNT_REPORT_SQL + "\n) AS exposed_accounts"
)


class RecipeSeedError(RuntimeError):
    """Seeding stopped; already-created source objects are intentionally retained."""

    def __init__(self, partial_manifest: dict):
        super().__init__(
            "Synthetic seeding stopped after a source API error. "
            "Existing objects were retained; inspect the partial manifest before continuing."
        )
        self.partial_manifest = deepcopy(partial_manifest)


def _graphql_data(response: dict) -> dict:
    if not isinstance(response, dict) or response.get("errors"):
        raise ValueError("Linear GraphQL returned an error or an invalid response")
    data = response.get("data", response)
    if not isinstance(data, dict):
        raise ValueError("Linear GraphQL returned no data")
    return data


def _stripe_list(pair: Any, path: str) -> list[dict]:
    result, seen_cursors = [], set()
    query: dict = {"limit": 100}
    for _ in range(1000):
        page = pair.stripe.request("GET", path, query=query)
        rows = page.get("data") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Stripe list response has no data array")
        result.extend(rows)
        if not page.get("has_more"):
            return result
        cursor = rows[-1].get("id") if rows else None
        if not cursor or cursor in seen_cursors:
            raise ValueError("Stripe pagination did not advance")
        seen_cursors.add(cursor)
        query = {"limit": 100, "starting_after": cursor}
    raise ValueError("Stripe list exceeded the bounded fixture scan")


def _linear_issues(pair: Any) -> list[dict]:
    query = """query FixtureIssues($after: String) {
      issues(first: 100, after: $after) {
        nodes { id description }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    result, seen_cursors = [], set()
    cursor = None
    for _ in range(1000):
        data = _graphql_data(pair.linear.graphql(query, {"after": cursor}))
        connection = data.get("issues", {})
        rows = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(rows, list) or not isinstance(page_info, dict):
            raise ValueError("Linear issue scan requires nodes and pagination metadata")
        result.extend(rows)
        if not page_info.get("hasNextPage"):
            return result
        cursor = page_info.get("endCursor")
        if not cursor or cursor in seen_cursors:
            raise ValueError("Linear pagination did not advance")
        seen_cursors.add(cursor)
    raise ValueError("Linear issues exceeded the bounded fixture scan")


def _discover_linear(pair: Any) -> tuple[dict, dict[str, dict], list[dict]]:
    data = _graphql_data(pair.linear.graphql("""query FixtureSetup {
      teams(first: 100) {
        nodes { id name states { nodes { id name type } } }
      }
      users(first: 100) { nodes { id name } }
    }"""))
    teams = data.get("teams", {}).get("nodes", [])
    users = data.get("users", {}).get("nodes", [])
    for team in teams:
        states: dict[str, dict] = {}
        for state in team.get("states", {}).get("nodes", []):
            if state.get("id") and state.get("type"):
                states.setdefault(state["type"], state)
        if team.get("id") and {"started", "completed", "canceled"} <= states.keys():
            return team, states, [u for u in users if u.get("id") and u.get("name")]
    raise ValueError("Recipe needs an existing Linear team with started, completed, and canceled states")


def _validate_variant(variant: str) -> None:
    if not isinstance(variant, str) or variant not in RECIPE_VARIANTS:
        raise ValueError("variant must be demo_v1 or shuffled_cases_v1")


def _plan_accounts(account_count: int, seed: int, states: dict, users: list[dict], *,
                   variant: str = "demo_v1") -> list[dict]:
    _validate_variant(variant)
    rng = random.Random(seed)
    # A separate generator leaves the recorded demo's monetary draw sequence
    # unchanged. The opt-in variant randomizes layout, not simulator behavior.
    layout_rng = random.Random(seed ^ 0x53485546)
    cases = [index % 12 for index in range(account_count)]
    labels = list(range(1, account_count + 1))
    if variant == "shuffled_cases_v1":
        layout_rng.shuffle(cases)
        labels = layout_rng.sample(range(1, 1_000_000_000), account_count)
    accounts = []
    for index in range(account_count):
        kind = cases[index]
        label = f"{labels[index]:03d}" if variant == "demo_v1" else f"{labels[index]:09d}"
        account_id = f"customer_{label}"
        charges = []
        for charge_index in range(2):
            amount = rng.randint(40, 500) * 100 + rng.randint(0, 99)
            currency = "eur" if kind == 8 or (kind == 1 and charge_index == 1) else "usd"
            refund = amount if kind == 5 else (
                amount // 3 if (kind == 0 and charge_index == 1) or
                (kind == 9 and charge_index == 0) else 0
            )
            charges.append({
                "charge_id": None, "amount_cents": amount, "refund_cents": refund,
                "currency": currency, "paid": True, "captured": True,
                "status": "succeeded", "disputed": False, "refund_ids": [],
            })
        state_type = "completed" if kind == 2 else "canceled" if kind == 6 else "started"
        priorities = [1, 2] if kind in (0, 11) else [3, 4] if kind == 4 else [0] if kind == 10 else [2]
        if kind in (7, 9):
            priorities.append(3 if kind == 7 else 1)
        issues = []
        for issue_index, priority in enumerate(priorities):
            issue_state_type = "completed" if kind == 9 and issue_index == 1 else state_type
            state = states[issue_state_type]
            owner = users[(index + issue_index) % len(users)] if users and kind != 7 else {}
            issues.append({
                "issue_id": None, "identifier": None,
                "title": f"Synthetic {account_id} investigation {issue_index + 1}",
                "priority": priority, "state_type": issue_state_type,
                "state_name": state["name"], "state_id": state["id"],
                "owner_id": owner.get("id"), "owner_name": owner.get("name"),
            })
        if variant == "shuffled_cases_v1":
            layout_rng.shuffle(charges)
            layout_rng.shuffle(issues)
        accounts.append({
            "account_id": account_id, "name": f"Example Company {label}",
            "is_internal": kind == 3, "customer_id": None,
            "charges": charges, "issues": issues,
        })
    if variant == "shuffled_cases_v1":
        # seed_recipe iterates this sequence, so source-generated identifiers and
        # pagination order no longer correspond to the fixed demo case index.
        layout_rng.shuffle(accounts)
    return accounts


def seed_recipe(pair: Any, account_count: int = 12, seed: int = 7, *,
                variant: str = "demo_v1") -> dict:
    """Create a bounded fixture without resets, premium seeds, or duplicate runs.

    The returned manifest records authored business intent as well as server IDs.
    Callers should persist ``RecipeSeedError.partial_manifest`` on source failure.
    A failed partial seed deliberately prevents a silent retry into the same pair.
    ``shuffled_cases_v1`` varies case assignment, labels, and insertion order.
    It does not establish a held-out split or additional production API fidelity;
    an evaluation must separately freeze disjoint development/held-out manifests.
    """
    _validate_variant(variant)
    if type(account_count) is not int or not 4 <= account_count <= 100:
        raise ValueError("account_count must be an integer from 4 through 100")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer from 0 through 4294967295")
    versions = pair.verify()
    existing_customers = _stripe_list(pair, "/v1/customers")
    existing_issues = _linear_issues(pair)
    marker = f"contextbase_fixture={FIXTURE_NAMESPACE}"
    if any((c.get("metadata") or {}).get("contextbase_fixture") == FIXTURE_NAMESPACE
           for c in existing_customers) or any(
               marker in (i.get("description") or "").splitlines() for i in existing_issues
           ):
        raise ValueError("This Rystic pair already contains the Oriel fixture; refusing duplicates")
    team, states, users = _discover_linear(pair)
    manifest = {
        "spec": RECIPE_SPEC, "namespace": FIXTURE_NAMESPACE,
        "seed": seed, "account_count": account_count,
        "simulator_versions": versions, "team_id": team["id"],
        "status": "seeding", "synthetic": True,
        "owner_provenance": "Existing synthetic users from the Rystic Linear simulator",
        "metric": "net_exposed_collected_usd",
        "accounts": _plan_accounts(account_count, seed, states, users, variant=variant),
    }
    if variant != "demo_v1":
        # Absence means the original demo, preserving existing recorded manifests.
        manifest["variant"] = variant
        manifest["variation"] = {
            "semantic_case_assignment": "seeded_permutation_of_demo_cases",
            "account_labels": "seeded_unique_synthetic_labels",
            "source_insertion_order": "seeded_account_charge_and_issue_permutations",
            "split_assignment": "not_assigned",
        }
    try:
        for account in manifest["accounts"]:
            metadata = {
                "metadata[contextbase_fixture]": FIXTURE_NAMESPACE,
                "metadata[account_id]": account["account_id"],
                "metadata[is_internal]": str(account["is_internal"]).lower(),
            }
            customer = pair.stripe.request("POST", "/v1/customers", form={
                "name": account["name"], **metadata,
            })
            account["customer_id"] = customer["id"]
            for charge in account["charges"]:
                created = pair.stripe.request("POST", "/v1/charges", form={
                    "amount": charge["amount_cents"], "currency": charge["currency"],
                    "customer": account["customer_id"], "source": "tok_visa",
                    "capture": "true", **metadata,
                })
                charge["charge_id"] = created["id"]
                if charge["refund_cents"]:
                    refund = pair.stripe.request("POST", "/v1/refunds", form={
                        "charge": charge["charge_id"], "amount": charge["refund_cents"],
                        "metadata[contextbase_fixture]": FIXTURE_NAMESPACE,
                    })
                    charge["refund_ids"].append(refund["id"])
            for issue in account["issues"]:
                variables = {"input": {
                    "teamId": team["id"], "title": issue["title"],
                    "description": f"contextbase_account_id={account['account_id']}\n{marker}\n"
                        "Synthetic fixture. Revenue association is not a forecast of loss.",
                    "priority": issue["priority"], "stateId": issue["state_id"],
                }}
                if issue["owner_id"]:
                    variables["input"]["assigneeId"] = issue["owner_id"]
                data = _graphql_data(pair.linear.graphql("""mutation FixtureIssueCreate($input: IssueCreateInput!) {
                  issueCreate(input: $input) { success issue { id identifier } }
                }""", variables))
                payload = data.get("issueCreate", {})
                if payload.get("success") is not True:
                    raise ValueError("Linear did not confirm issue creation")
                issue["issue_id"] = payload["issue"]["id"]
                issue["identifier"] = payload["issue"]["identifier"]
    except Exception as exc:
        manifest["status"] = "incomplete"
        raise RecipeSeedError(manifest) from exc
    manifest["status"] = "complete"
    return manifest


def expected_answer(manifest: dict) -> dict:
    """Compute the answer from authored intent, independently of source reads/SQL."""
    if manifest.get("spec") != RECIPE_SPEC or manifest.get("namespace") != FIXTURE_NAMESPACE:
        raise ValueError("Not a supported Oriel Stripe + Linear fixture manifest")
    accounts = []
    for account in manifest["accounts"]:
        if account["is_internal"]:
            continue
        net_cents = 0
        for charge in account["charges"]:
            if (charge["currency"] == "usd" and charge["paid"] is True and
                    charge["captured"] is True and charge["status"] == "succeeded" and
                    charge["disputed"] is False):
                amount, refund = charge["amount_cents"], charge["refund_cents"]
                if type(amount) is not int or type(refund) is not int or not 0 <= refund <= amount:
                    raise ValueError("Fixture monetary intent requires valid integer cents")
                net_cents += amount - refund
        issues = [issue for issue in account["issues"]
                  if issue["priority"] in (1, 2) and issue["state_type"] in UNRESOLVED_STATE_TYPES]
        if net_cents > 0 and issues:
            accounts.append({
                "account_id": account["account_id"], "name": account["name"],
                "net_collected_usd_cents": net_cents,
                "open_high_priority_issue_count": len(issues),
                "owners": sorted({issue["owner_name"] or "Unassigned" for issue in issues}),
            })
    accounts.sort(key=lambda account: account["account_id"])
    total = sum(account["net_collected_usd_cents"] for account in accounts)
    return {
        "net_exposed_collected_usd_cents": total,
        "net_exposed_collected_usd": format(Decimal(total) / Decimal(100), ".2f"),
        "account_count": len(accounts), "accounts": accounts,
    }
