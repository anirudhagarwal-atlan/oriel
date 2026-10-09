"""Independent fixture oracle: CSV/JSON parsing and Python sets, no SQL or DuckDB."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(events="product-events-initial.jsonl", mapping="workspace-map-reviewed.csv"):
    with (FIXTURES / "crm-seats.csv").open(newline="") as handle:
        accounts = list(csv.DictReader(handle))
    with (FIXTURES / mapping).open(newline="") as handle:
        links = list(csv.DictReader(handle))
    activity = [json.loads(line) for line in (FIXTURES / events).read_text().splitlines()]
    return accounts, links, activity


def mapping_issues(accounts, links, activity):
    organizations = {}
    for account in accounts:
        organizations.setdefault(account["organization_id"], []).append(account)
    enabled = {}
    for link in links:
        if str(link["enabled"]).lower() == "true":
            enabled.setdefault(link["workspace_ref"], []).append(link["customer_key"])
    issues = {("ambiguous_workspace", key) for key, targets in enabled.items() if len(targets) > 1}
    issues.update(("missing_workspace", row["workspace_ref"]) for row in activity if row["workspace_ref"] not in enabled)
    issues.update(("unknown_customer", target) for targets in enabled.values() for target in targets if target not in organizations)
    issues.update(("duplicate_customer", key) for key, rows in organizations.items() if len(rows) > 1)
    targets = {target for values in enabled.values() for target in values}
    issues.update(("unmapped_customer", row["organization_id"]) for row in accounts
                  if row["contract_state"] == "active" and row["entity_kind"] == "customer"
                  and row["organization_id"] not in targets)
    return [{"issue": issue, "source_key": key} for issue, key in sorted(issues)]


def expected(accounts, links, activity):
    issues = mapping_issues(accounts, links, activity)
    if issues:
        raise ValueError("Resolve identity mapping before computing activation.")
    targets = {row["workspace_ref"]: row["customer_key"] for row in links if str(row["enabled"]).lower() == "true"}
    start = datetime(2026, 9, 24, tzinfo=timezone.utc)
    end = datetime(2026, 10, 8, tzinfo=timezone.utc)
    people = {}
    for row in activity:
        observed = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        if row["feature_key"] == "publish_report" and row["member_ref"] is not None and start <= observed < end:
            people.setdefault(targets[row["workspace_ref"]], set()).add(row["member_ref"])
    eligible = []
    for row in accounts:
        seats = int(row["licensed_seats"])
        activated = len(people.get(row["organization_id"], set()))
        if row["contract_state"] == "active" and row["entity_kind"] == "customer" and seats > 0 and activated * 2 < seats:
            eligible.append({"organization_id": row["organization_id"], "licensed_seats": seats,
                             "activated_members": activated, "unused_seats": seats - activated})
    return {"accounts": sorted(eligible, key=lambda row: row["organization_id"]),
            "unused_seats_at_low_activation": sum(row["unused_seats"] for row in eligible)}


if __name__ == "__main__":
    print(json.dumps({"synthetic": True, "initial": expected(*read_fixture()),
                      "refreshed": expected(*read_fixture("product-events-refreshed.jsonl"))}, indent=2))
