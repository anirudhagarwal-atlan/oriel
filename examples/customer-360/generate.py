"""Author small synthetic source-shaped fixtures; never contact a source.

Run with a NEW output directory. The oracle does not import this module.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def decisions(profile, customers, bindings):
    return {"spec": "oriel.customer360.fixture-mapping/v1", "synthetic": True,
            "review_status": "authored_example_not_a_real_review", "profile": profile,
            "customers": [{"customer_id": key, "label": label} for key, label in customers],
            "bindings": [{"decision_id": f"mapping-{index:02}", "status": "approved_example",
                          "source": source, "resource": resource, "source_id": source_id,
                          "customer_id": customer, "phases": phases, "reason": reason}
                         for index, (source, resource, source_id, customer, phases, reason)
                         in enumerate(bindings, 1)]}


def crm_conversations():
    ids = {name: f"001SYN00000000000{index}" for index, name in enumerate("abcd", 1)}
    replacement = "001SYN000000000009"
    accounts = [{"Id": ids[name], "Name": "Example Shared" if name in "ab" else "Example " + name.upper(),
                 "Website": "https://shared.example.invalid" if name in "ab" else f"https://{name}.example.invalid",
                 "OwnerId": "005SYN000000000001", "LastModifiedDate": "2026-01-05T09:00:00Z"}
                for name in "abcd"]
    amounts = [("a", 10000, "USD", False), ("a", 2500, "USD", False),
               ("a", 3000, "EUR", False), ("b", 8000, "USD", False),
               ("b", 2000, "USD", True), ("c", 4000, "EUR", False),
               ("d", 6000, "USD", False)]
    opportunities = [{"Id": f"006SYN00000000000{i}", "AccountId": ids[name],
                      "Name": f"Example opportunity {i}", "Amount": amount,
                      "CurrencyIsoCode": currency, "IsClosed": closed,
                      "StageName": "Closed Won" if closed else "Proposal",
                      "CloseDate": "2026-02-28", "LastModifiedDate": "2026-01-05T09:00:00Z"}
                     for i, (name, amount, currency, closed) in enumerate(amounts, 1)]
    contacts = [{"Id": f"003SYN00000000000{i}", "AccountId": ids[name],
                 "FirstName": "Example", "LastName": f"Person {i}",
                 "Email": f"person{i}@shared.example.invalid"}
                for i, name in enumerate("aabc", 1)]
    def call(index, refs, text, day=6):
        call_id = str(100000000000000000 + index)
        record = {"metaData": {"id": call_id, "started": f"2026-01-{day:02}T10:00:00Z",
                               "title": f"Example call {index}", "url": f"https://gong.example.invalid/call/{call_id}",
                               "duration": 900, "isPrivate": False},
                  "context": [{"system": "Salesforce", "objects": [
                      {"objectType": kind, "objectId": ref, "timing": "TimeOfCall", "fields": []}
                      for kind, ref in refs]}] if refs else [],
                  "parties": [{"id": f"party-{index}-1", "speakerId": "speaker-1", "affiliation": "External",
                               "emailAddress": "attendee@shared.example.invalid"},
                              {"id": f"party-{index}-2", "speakerId": "speaker-2", "affiliation": "Internal",
                               "emailAddress": "operator@example.invalid"}]}
        transcript = {"callId": call_id, "transcript": [{"speakerId": "speaker-1", "topic": "Implementation",
                      "sentences": [{"start": 12000, "end": 16000, "text": text}]}]}
        return record, transcript
    paired = [call(1, [("Account", ids["a"]), ("Opportunity", opportunities[0]["Id"])], "SSO rollout is blocked by an identity-provider configuration issue."),
              call(2, [("Opportunity", opportunities[3]["Id"])], "The pilot is ready; budget approval is pending."),
              call(3, [], "This unlinked call must not become a claim about Example Shared."),
              call(4, [("Account", ids["a"]), ("Opportunity", opportunities[3]["Id"])], "This conflicting call must remain unattributed."),
              call(5, [], "The reporting rollout is scheduled for February.")]
    initial = {"salesforce.account": accounts, "salesforce.opportunity": opportunities,
               "salesforce.contact": contacts,
               "salesforce.sf_user": [{"Id": "005SYN000000000001", "Name": "Example Operator", "Email": "operator@example.invalid"}],
               "gong.extensive_calls": [p[0] for p in paired], "gong.call_transcripts": [p[1] for p in paired]}
    refreshed = deepcopy(initial)
    refreshed["salesforce.account"][0].update(Id=replacement, LastModifiedDate="2026-01-12T09:00:00Z")
    for row in refreshed["salesforce.opportunity"] + refreshed["salesforce.contact"]:
        if row["AccountId"] == ids["a"]:
            row["AccountId"] = replacement
    refreshed["salesforce.opportunity"][0].update(Amount=9000, LastModifiedDate="2026-01-12T09:00:00Z")
    refreshed["salesforce.opportunity"][1].update(IsClosed=True, StageName="Closed Won", LastModifiedDate="2026-01-12T09:00:00Z")
    new_call, new_transcript = call(6, [("Account", replacement)], "SSO rollout is unblocked; procurement approval is now pending.", 13)
    refreshed["gong.extensive_calls"].append(new_call)
    refreshed["gong.call_transcripts"].append(new_transcript)
    bindings = [("salesforce", "account", ids[name], "customer_" + name,
                 ["initial", "refreshed"], "Stable identity; retain the old source ID as a historical alias.") for name in "abcd"]
    bindings += [("salesforce", "account", replacement, "customer_a", ["refreshed"], "Reviewed synthetic source-ID replacement for customer_a."),
                 ("gong", "extensive_calls", paired[4][0]["metaData"]["id"], "customer_c", ["initial", "refreshed"],
                  "Explicit reviewed call association; no domain or name inference.")]
    bindings += [("gong", "extensive_calls", paired[3][0]["metaData"]["id"], "customer_" + name,
                  ["initial", "refreshed"], "Deliberately conflicting authored mapping; must be refused.") for name in "ab"]
    return initial, refreshed, decisions("salesforce-gong", [("customer_" + n, "Example " + n.upper()) for n in "abcd"], bindings)


def billing_support():
    customer_ids = {n: "cus_synthetic_" + n for n in "abc"}
    customers = [{"id": customer_ids[n], "object": "customer", "name": "Example Shared" if n in "ab" else "Example C",
                  "email": f"billing-{n}@shared.example.invalid", "metadata": {}} for n in "abc"]
    invoices = [{"id": f"in_synthetic_{i}", "object": "invoice", "customer": customer_ids[n], "currency": currency,
                 "status": status, "amount_due": amount, "amount_paid": amount - remaining,
                 "amount_remaining": remaining, "created": 1767603600, "metadata": {}}
                for i, (n, currency, amount, remaining, status) in enumerate([
                    ("a", "usd", 100000, 100000, "open"), ("a", "usd", 25000, 25000, "open"),
                    ("a", "eur", 30000, 30000, "open"), ("b", "usd", 80000, 80000, "open"),
                    ("b", "usd", 20000, 0, "paid"), ("c", "eur", 45000, 45000, "open"),
                    ("a", "usd", 99900, 99900, "void")], 1)]
    companies = [{"type": "company", "id": "ic_company_" + n, "company_id": "org_external_" + n,
                  "name": "Example Shared", "website": "https://shared.example.invalid",
                  "custom_attributes": {"billing_customer_id": customer_ids[n]}} for n in "ab"]
    companies.append({"type": "company", "id": "ic_company_unknown", "company_id": "org_unmapped",
                      "name": "Example Shared", "website": "https://shared.example.invalid", "custom_attributes": {}})
    contacts = [{"type": "contact", "id": "ic_contact_" + name, "external_id": "person_external_" + name,
                 "email": name + "@shared.example.invalid", "role": "user",
                 "companies": {"type": "list", "data": [{"type": "company", "id": "ic_company_" + n} for n in refs]}}
                for name, refs in [("a1", ["a"]), ("a2", ["a"]), ("b1", ["b"]),
                                   ("shared", ["a", "b"]), ("unmapped", ["unknown"]), ("manual", [])]]
    def conversation(index, people, body):
        return {"type": "conversation", "id": "ic_conversation_" + str(index), "state": "open",
                "created_at": 1767693600 + index, "updated_at": 1767693600 + index,
                "source": {"type": "email", "id": "ic_source_" + str(index), "body": body,
                           "author": {"type": "contact", "id": "ic_contact_" + people[0]}},
                "contacts": {"type": "contact.list", "contacts": [{"type": "contact", "id": "ic_contact_" + p} for p in people]},
                "conversation_parts": {"type": "conversation_part.list", "conversation_parts": []}}
    initial = {"stripe.customers": customers, "stripe.invoices": invoices,
               "intercom.companies": companies, "intercom.contacts": contacts,
               "intercom.conversations": [conversation(1, ["a1", "a2"], "Invoice export is failing for the billing team."),
                                          conversation(2, ["b1"], "The renewal invoice needs a purchase-order correction."),
                                          conversation(3, ["unmapped"], "This unmatched request must remain unattributed."),
                                          conversation(4, ["shared"], "This contact belongs to two companies; attribution is ambiguous."),
                                          conversation(5, ["manual"], "The billing address change is awaiting confirmation.")]}
    refreshed = deepcopy(initial)
    replacement = "cus_synthetic_a_reissued"
    refreshed["stripe.customers"][0]["id"] = replacement
    for row in refreshed["stripe.invoices"]:
        if row["customer"] == customer_ids["a"]:
            row["customer"] = replacement
    refreshed["stripe.invoices"][0].update(status="paid", amount_paid=100000, amount_remaining=0)
    refreshed["stripe.invoices"][1].update(amount_paid=15000, amount_remaining=10000)
    refreshed["intercom.conversations"][0].update(state="closed", updated_at=1768305600)
    refreshed["intercom.conversations"][0]["conversation_parts"]["conversation_parts"].append({
        "type": "conversation_part", "id": "ic_part_resolution_1", "part_type": "comment",
        "body": "Invoice export is fixed; CSV export now succeeds.", "created_at": 1768305600,
        "author": {"type": "contact", "id": "ic_contact_a1"}})
    bindings = [("stripe", "customers", customer_ids[n], "customer_" + n, ["initial", "refreshed"],
                 "Stable billing identity; historical IDs remain explicit aliases.") for n in "abc"]
    bindings += [("stripe", "customers", replacement, "customer_a", ["refreshed"], "Reviewed synthetic replacement for the billing customer ID."),
                 ("intercom", "contacts", "ic_contact_manual", "customer_c", ["initial", "refreshed"],
                  "Explicit reviewed support contact mapping, without CRM or email matching.")]
    return initial, refreshed, decisions("billing-support", [("customer_" + n, "Example " + n.upper()) for n in "abc"], bindings)


def generate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"spec": "oriel.customer360.synthetic-fixtures/v1", "synthetic": True,
                "evidence_kind": "authored_source_shaped_examples", "native_connector_executed": False,
                "authenticated_vendor_calls": 0, "model_calls": 0, "profiles": {}}
    for profile, builder, capabilities in [
        ("salesforce-gong", crm_conversations, {"crm": True, "transcripts": True, "billing": False, "support": False}),
        ("billing-support", billing_support, {"crm": False, "transcripts": False, "billing": True, "support": True}),
    ]:
        initial, refreshed, mapping = builder()
        base = Path("profiles") / profile
        write(output / base / "mapping-decisions.json", mapping)
        phase_files = {}
        for phase, data in [("initial", initial), ("refreshed", refreshed)]:
            phase_files[phase] = {}
            for resource, rows in data.items():
                relative = base / phase / (resource + ".json")
                write(output / relative, rows)
                phase_files[phase][resource] = str(relative)
        manifest["profiles"][profile] = {"capabilities": capabilities,
                                        "mapping_decisions": str(base / "mapping-decisions.json"), "phases": phase_files}
    write(output / "manifest.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="A new directory; existing directories are refused.")
    generate(parser.parse_args().output)
