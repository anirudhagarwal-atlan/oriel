"""Independent Customer360 fixture oracle: stdlib records/sets/Decimal only.

No Oriel, DuckDB, connector, model or generator imports. Explicit mappings and
source references determine identity; names, domains and text never do.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _read(path):
    return json.loads(path.read_text())


def _unique(rows, key):
    indexed = {}
    for row in rows:
        value = key(row)
        if value in indexed:
            raise ValueError("Duplicate fixture record identity: " + str(value))
        indexed[value] = row
    return indexed


def evaluate(profile, phase, root=ROOT):
    root = Path(root)
    manifest = _read(root / "manifest.json")
    definition = manifest["profiles"][profile]
    files = definition["phases"][phase]
    records = {resource: _read(root / path) for resource, path in files.items()}
    mapping = _read(root / definition["mapping_decisions"])
    customers = {row["customer_id"] for row in mapping["customers"]}
    if len(customers) != len(mapping["customers"]):
        raise ValueError("Duplicate stable customer identity")
    bindings = defaultdict(set)
    for row in mapping["bindings"]:
        if phase in row["phases"] and row["status"] == "approved_example":
            if row["customer_id"] not in customers:
                raise ValueError("Mapping references an unknown stable customer")
            bindings[(row["source"], row["resource"], row["source_id"])].add(row["customer_id"])
    def linked(source, resource, source_id):
        return set(bindings.get((source, resource, source_id), set()))
    issues, identities, citations = [], [], []
    totals = {customer: defaultdict(Decimal) for customer in customers}
    counts = {customer: defaultdict(int) for customer in customers}
    def resolve(resource, record_id, candidates, missing=False):
        if len(candidates) != 1 or missing:
            code = "conflicting_reference" if len(candidates) > 1 else "missing_reference" if not candidates else "incomplete_reference"
            issues.append({"resource": resource, "record_id": record_id, "issue": code,
                           "candidate_customers": sorted(candidates)})
            return None
        customer = next(iter(candidates))
        identities.append({"resource": resource, "record_id": record_id, "customer_id": customer})
        return customer
    def cite(customer, resource, record_id, pointer, text, observed_at, source_url=None, speaker=None):
        citation = {"customer_id": customer, "resource": resource, "record_id": record_id,
                    "file": files[resource], "json_pointer": pointer, "text": text, "observed_at": observed_at}
        if source_url:
            citation["source_url"] = source_url
        if speaker:
            citation["speaker_id"] = speaker
        citations.append(citation)

    if profile == "salesforce-gong":
        accounts = _unique(records["salesforce.account"], lambda row: row["Id"])
        opportunities = _unique(records["salesforce.opportunity"], lambda row: row["Id"])
        calls = _unique(records["gong.extensive_calls"], lambda row: row["metaData"]["id"])
        _unique(records["gong.call_transcripts"], lambda row: row["callId"])
        for account_id in accounts:
            resolve("salesforce.account", account_id, linked("salesforce", "account", account_id))
        for row in opportunities.values():
            customer = resolve("salesforce.opportunity", row["Id"], linked("salesforce", "account", row["AccountId"]), row["AccountId"] not in accounts)
            if customer and not row["IsClosed"]:
                totals[customer][row["CurrencyIsoCode"]] += Decimal(str(row["Amount"]))
                counts[customer]["open_opportunities"] += 1
        for row in records["salesforce.contact"]:
            customer = resolve("salesforce.contact", row["Id"], linked("salesforce", "account", row["AccountId"]), row["AccountId"] not in accounts)
            if customer:
                counts[customer]["contacts"] += 1
        call_customers = {}
        for call_id, row in calls.items():
            candidates = linked("gong", "extensive_calls", call_id)
            missing = False
            for context in row["context"]:
                if context["system"] != "Salesforce":
                    missing = True
                    continue
                for obj in context["objects"]:
                    if obj["objectType"] == "Account":
                        found = linked("salesforce", "account", obj["objectId"])
                    elif obj["objectType"] == "Opportunity" and obj["objectId"] in opportunities:
                        found = linked("salesforce", "account", opportunities[obj["objectId"]]["AccountId"])
                    else:
                        found = set()
                    missing |= not found
                    candidates.update(found)
            customer = resolve("gong.extensive_calls", call_id, candidates, missing)
            if customer:
                call_customers[call_id] = customer
                counts[customer]["calls"] += 1
        for i, transcript in enumerate(records["gong.call_transcripts"]):
            call_id = transcript["callId"]
            if call_id not in calls:
                raise ValueError("Transcript references an unknown call")
            if call_id not in call_customers:
                continue
            call = calls[call_id]
            for j, segment in enumerate(transcript["transcript"]):
                for k, sentence in enumerate(segment["sentences"]):
                    cite(call_customers[call_id], "gong.call_transcripts", call_id,
                         f"/{i}/transcript/{j}/sentences/{k}/text", sentence["text"], call["metaData"]["started"],
                         call["metaData"]["url"], segment["speakerId"])
                    citations[-1]["start_ms"] = sentence["start"]
                    citations[-1]["end_ms"] = sentence["end"]
        metric = {"name": "open_pipeline", "unit": "currency_major_units", "inclusion": "IsClosed == false"}
        count_names = ("open_opportunities", "contacts", "calls")
        sets = {"with_open_opportunities": sorted(c for c in customers if counts[c]["open_opportunities"]),
                "with_attributed_calls": sorted(c for c in customers if counts[c]["calls"]),
                "without_attributed_calls": sorted(c for c in customers if not counts[c]["calls"])}
    elif profile == "billing-support":
        billing_customers = _unique(records["stripe.customers"], lambda row: row["id"])
        companies = _unique(records["intercom.companies"], lambda row: row["id"])
        contacts = _unique(records["intercom.contacts"], lambda row: row["id"])
        _unique(records["stripe.invoices"], lambda row: row["id"])
        _unique(records["intercom.conversations"], lambda row: row["id"])
        for customer_id in billing_customers:
            resolve("stripe.customers", customer_id, linked("stripe", "customers", customer_id))
        for invoice in records["stripe.invoices"]:
            customer = resolve("stripe.invoices", invoice["id"], linked("stripe", "customers", invoice["customer"]), invoice["customer"] not in billing_customers)
            if customer and invoice["status"] == "open" and invoice["amount_remaining"] > 0:
                if type(invoice["amount_remaining"]) is not int:
                    raise ValueError("Expected integral invoice minor units")
                totals[customer][invoice["currency"].upper()] += invoice["amount_remaining"]
                counts[customer]["open_invoices"] += 1
        for i, conversation in enumerate(records["intercom.conversations"]):
            candidates, missing = set(), False
            for contact_ref in conversation["contacts"]["contacts"]:
                contact_id = contact_ref["id"]
                found = linked("intercom", "contacts", contact_id)
                contact = contacts.get(contact_id)
                if not contact:
                    missing = True
                    continue
                for company_ref in contact["companies"]["data"]:
                    company = companies.get(company_ref["id"])
                    target = company.get("custom_attributes", {}).get("billing_customer_id") if company else None
                    company_targets = linked("stripe", "customers", target) if target else set()
                    missing |= not company_targets
                    found.update(company_targets)
                missing |= not found
                candidates.update(found)
            customer = resolve("intercom.conversations", conversation["id"], candidates, missing)
            if not customer:
                continue
            counts[customer]["conversations"] += 1
            if conversation["state"] == "open":
                counts[customer]["open_conversations"] += 1
            cite(customer, "intercom.conversations", conversation["id"], f"/{i}/source/body",
                 conversation["source"]["body"], conversation["created_at"])
            for j, part in enumerate(conversation["conversation_parts"]["conversation_parts"]):
                cite(customer, "intercom.conversations", conversation["id"],
                     f"/{i}/conversation_parts/conversation_parts/{j}/body", part["body"], part["created_at"])
                citations[-1]["part_id"] = part["id"]
        metric = {"name": "outstanding_open_invoices", "unit": "currency_minor_units", "inclusion": "status == open and amount_remaining > 0"}
        count_names = ("open_invoices", "conversations", "open_conversations")
        sets = {"with_outstanding_invoices": sorted(c for c in customers if counts[c]["open_invoices"]),
                "with_open_support": sorted(c for c in customers if counts[c]["open_conversations"]),
                "with_attributed_support": sorted(c for c in customers if counts[c]["conversations"])}
    else:
        raise ValueError("Unknown fixture profile")
    def number(value):
        return int(value) if metric["unit"] == "currency_minor_units" else format(value, ".2f")
    global_totals = defaultdict(Decimal)
    per_customer = []
    for customer in sorted(customers):
        for currency, value in totals[customer].items():
            global_totals[currency] += value
        per_customer.append({"customer_id": customer,
                             "amounts_by_currency": {key: number(value) for key, value in sorted(totals[customer].items())},
                             **{name: counts[customer][name] for name in count_names}})
    citations.sort(key=lambda row: (row["customer_id"], row["observed_at"], row["record_id"], row["json_pointer"]))
    latest = {}
    for row in citations:
        latest[row["customer_id"]] = row
    return {"synthetic": True, "profile": profile, "phase": phase,
            "capabilities": definition["capabilities"], "metric": metric,
            "totals_by_currency": {key: number(value) for key, value in sorted(global_totals.items())},
            "customers": per_customer, "customer_sets": sets,
            "resolved_identities": sorted(identities, key=lambda row: (row["resource"], row["record_id"])),
            "identity_issues": sorted(issues, key=lambda row: (row["resource"], row["record_id"])),
            "attributed_text": citations, "latest_attributed_text": latest,
            "source_record_counts": {key: len(value) for key, value in sorted(records.items())}}


def all_expected(root=ROOT):
    return {"spec": "oriel.customer360.independent-oracle/v1", "synthetic": True,
            "method": "Python records, explicit reference sets and Decimal; no product SQL or model inference",
            "profiles": {profile: {phase: evaluate(profile, phase, root) for phase in ("initial", "refreshed")}
                         for profile in ("salesforce-gong", "billing-support")}}


def verify_citations(expected, root):
    for phases in expected["profiles"].values():
        for result in phases.values():
            for citation in result["attributed_text"]:
                value = _read(Path(root) / citation["file"])
                for token in citation["json_pointer"].split("/")[1:]:
                    value = value[int(token)] if isinstance(value, list) else value[token]
                if value != citation["text"]:
                    raise ValueError("Citation does not resolve to its exact source text")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="Compare with saved expected.json and resolve every text citation.")
    args = parser.parse_args()
    expected = all_expected(args.root)
    verify_citations(expected, args.root)
    if args.check:
        if expected != _read(args.root / "expected.json"):
            raise SystemExit("Fixture oracle differs from expected.json")
        print(json.dumps({"status": "passed", "scope": "synthetic_fixture_oracle_only", "profiles": 2,
                          "phases_per_profile": 2, "authenticated_vendor_calls": 0, "model_calls": 0,
                          "native_connector_execution": False}, sort_keys=True))
    else:
        print(json.dumps(expected, indent=2, sort_keys=True))
