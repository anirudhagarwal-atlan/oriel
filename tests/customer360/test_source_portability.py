"""Portability acceptance with real local extraction and explicit mapping review.

Replacing a source preserves canonical customer UUIDs and business-definition
content. Mapping and context approvals are re-reviewed and linked to history;
an approval for the old physical source never silently transfers to a new one.
Authored SQLite/CSV sources establish local functionality, not live vendors.
"""
import ast
import copy
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, identity, profiles, snapshots
from contextbase.customer360.contracts import PROFILE_SPEC
from contextbase.workspace import Workspace
from tests.test_customer360_native_review import runtime, review


def csv_source(workspace, directory, connection, header, rows):
    path = directory / (connection + ".csv")
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    service.add(workspace, "csv", name=connection, path=path)
    return service.sync(workspace, connection, bootstrap_context=False)


def link(workspace, connection, entity, source_id, customer_id):
    candidate = identity.propose_links(workspace, [{"connection": connection, "entity": entity, "source_id": source_id,
                                                   "customer_id": customer_id, "basis": "Explicit synthetic replacement/enrichment reference"}])
    identity.approve_links(workspace, candidate["proposal_id"], candidate["approval_sha256"], "example-reviewer")


def approve_profile(workspace, profile):
    snapshot = snapshots.capture_snapshot(workspace, sorted({b["connection"] for b in profile["bindings"]}))["snapshot_id"]
    proposed = profiles.propose_profile(workspace, profile, snapshot)
    approved = profiles.approve_profile(workspace, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")
    materialized = profiles.materialize(workspace, profile["name"], snapshot)
    return snapshot, approved, materialized


def approve_context(workspace, profile, snapshot, definitions, gaps):
    proposed = contexts.propose_context(workspace, profile, snapshot, definitions, gaps=gaps)
    return contexts.approve_context(workspace, proposed["proposal_id"], proposed["approval_sha256"], "example-reviewer")


class SourcePortabilityAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # Failure, never skip, if approved runtime is absent.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.native = {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(cls.temp.name) / profile)
            extraction = runtime.load_profile(workspace, profile)
            reviewed = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
            cls.native[profile] = (workspace, extraction, reviewed)

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_case.cleanup)
        self.root = Path(self.temp_case.name)

    def single_source(self):
        workspace = Workspace(self.root / "generic-workspace")
        customer = identity.create_customer(workspace, "example-portable-customer")["id"]
        receipt = csv_source(workspace, self.root, "ledger_alpha",
                             ["Bill key", "Party handle", "Minor outstanding", "ISO", "State"],
                             [["bill-1", "payer-7", 4200, "USD", "open"], ["bill-2", "payer-7", 3800, "USD", "paid"],
                              ["bill-3", "payer-7", 1200, "EUR", "open"]])
        link(workspace, "ledger_alpha", "payer", "payer-7", customer)
        profile = {"spec_version": PROFILE_SPEC, "name": "portable_review", "description": "Explicitly reviewed generic source roles",
                   "bindings": [{"connection": "ledger_alpha", "entity": "bill", "table": "ledger_alpha.records", "kind": "billing",
                                 "record_id": "Bill key", "customer_ref": {"connection": "ledger_alpha", "entity": "payer", "column": "Party handle"},
                                 "fields": {"amount": "Minor outstanding", "currency": "ISO", "status": "State"}}]}
        snapshot, _, _ = approve_profile(workspace, profile)
        definitions = [{"name": "open_amount", "description": "Sum source open balances in minor currency units; no recurring-revenue meaning",
                        "kind": "billing", "operation": "sum", "field": "amount", "unit": "minor_currency", "currency_field": "currency",
                        "filters": [{"field": "status", "operator": "eq", "value": "open"}]}]
        context = approve_context(workspace, profile["name"], snapshot, definitions,
                                  [{"code": "recurrence_unknown", "description": "This source does not establish recurring revenue."}])
        return workspace, customer, receipt, profile, snapshot, context

    def test_materially_different_profiles_share_core_contracts(self):
        schemas, capabilities, returned = {}, {}, []
        for fixture, (workspace, extraction, reviewed) in self.native.items():
            with self.subTest(profile=fixture):
                context = contexts.current_context(workspace, reviewed["profile"], reviewed["snapshot_id"])
                customer = reviewed["customer_ids"]["customer_a"]
                definition = context["definitions"][0]["name"]
                metric = answers.execute_metric(workspace, reviewed["profile"], reviewed["snapshot_id"], customer, definition)
                brief = answers.brief(workspace, reviewed["profile"], reviewed["snapshot_id"], customer)
                final = answers.validate_answer(workspace, reviewed["profile"], reviewed["snapshot_id"], customer,
                                                {"metrics": [{"definition_name": definition, "result_id": metric["result_id"]}], "quotes": []})
                self.assertEqual(final["metrics"][0], metric)
                self.assertEqual(final["context_id"], brief["context_id"])
                self.assertEqual(final["customer_id"], customer)
                self.assertTrue(extraction["actual_dlt_executed"])
                self.assertFalse(extraction["vendor_adapter_verified"])
                returned.append(set(final))
                approved = profiles.load_profile(workspace, reviewed["profile"])
                schemas[fixture] = approved["candidate"]["schema_contract"]
                capabilities[fixture] = set(brief["capabilities"])
        self.assertEqual(returned[0], returned[1])
        self.assertNotEqual(schemas["salesforce-gong"], schemas["billing-support"])
        self.assertEqual(capabilities["salesforce-gong"], {"customer", "commercial", "conversation"})
        self.assertEqual(capabilities["billing-support"], {"customer", "billing", "support"})

    def test_no_crm_or_transcripts_required_for_second_profile(self):
        workspace, extraction, reviewed = self.native["billing-support"]
        self.assertEqual(set(extraction["source_label_mapping"]), {"stripe", "intercom"})
        self.assertTrue(all("salesforce" not in resource["resource"] and "gong" not in resource["resource"] for resource in extraction["resources"]))
        customer = reviewed["customer_ids"]["customer_a"]
        brief = answers.brief(workspace, reviewed["profile"], reviewed["snapshot_id"], customer)
        self.assertNotIn("commercial", brief["capabilities"])
        self.assertNotIn("conversation", brief["capabilities"])
        amounts = next(metric["result"] for metric in brief["metrics"] if metric["result"]["definition_name"] == "outstanding_invoices")
        self.assertEqual(amounts["values"], [{"currency": "EUR", "value": "30000"}, {"currency": "USD", "value": "125000"}])
        self.assertTrue(any("Invoice export is failing" in quote["text"] for quote in brief["quotes"]))
        abstained = answers.validate_answer(workspace, reviewed["profile"], reviewed["snapshot_id"], customer,
                                            {"abstain": {"code": "capability_unavailable"}})
        self.assertEqual(abstained["abstention"]["code"], "capability_unavailable")
        self.assertEqual(abstained["snapshot_id"], reviewed["snapshot_id"])

    def test_single_source_then_enrichment_reports_coverage(self):
        workspace, customer, receipt, profile, snapshot, context = self.single_source()
        before = answers.brief(workspace, profile["name"], snapshot, customer)
        self.assertEqual(receipt["source_type"], "csv")
        self.assertEqual(before["capabilities"], ["billing"])
        self.assertEqual(before["quotes"], [])
        self.assertTrue(any(gap.get("kind") == "support" for gap in before["gaps"]))
        note = "The renewal invoice needs a corrected reference."
        csv_source(workspace, self.root, "notes_omega", ["Case token", "Org alias", "Note body", "Resolution"],
                   [["case-1", "org-relinked", note, "open"]])
        link(workspace, "notes_omega", "organization", "org-relinked", customer)
        enriched = copy.deepcopy(profile)
        enriched["bindings"].append({"connection": "notes_omega", "entity": "case", "table": "notes_omega.records", "kind": "support",
                                     "record_id": "Case token", "customer_ref": {"connection": "notes_omega", "entity": "organization", "column": "Org alias"},
                                     "fields": {"text": "Note body", "status": "Resolution"}})
        refreshed, _, _ = approve_profile(workspace, enriched)
        with self.assertRaisesRegex(ValueError, "source mappings changed"):
            contexts.current_context(workspace, profile["name"], refreshed)
        revised = approve_context(workspace, profile["name"], refreshed, context["definitions"], context["gaps"])
        after = answers.brief(workspace, profile["name"], refreshed, customer)
        self.assertEqual(after["capabilities"], ["billing", "support"])
        self.assertFalse(any(gap.get("kind") == "support" for gap in after["gaps"]))
        self.assertEqual(after["metrics"][0]["result"], before["metrics"][0]["result"])
        self.assertEqual(after["quotes"][0]["text"], note)
        self.assertEqual(after["quotes"][0]["customer_id"], customer)
        self.assertEqual(revised["definition_sha256"], context["definition_sha256"])
        self.assertEqual(revised["previous_context_id"], context["context_id"])
        self.assertEqual(len(identity.list_customers(workspace)), 1)
        self.assertEqual(snapshots.load_snapshot(workspace, refreshed)["coverage"]["connections"], ["ledger_alpha", "notes_omega"])

    def test_source_replacement_preserves_customer_identity_and_context(self):
        original, _, reviewed = self.native["billing-support"]
        destination = self.root / "replacement-workspace"
        shutil.copytree(original.root, destination)
        workspace = Workspace(destination)
        old_context = contexts.current_context(workspace, reviewed["profile"], reviewed["snapshot_id"])
        old_profile = profiles.load_profile(workspace, reviewed["profile"])["candidate"]["profile"]
        old_customers = identity.list_customers(workspace)
        old_metrics = {alias: answers.execute_metric(workspace, reviewed["profile"], reviewed["snapshot_id"], customer, "outstanding_invoices")
                       for alias, customer in reviewed["customer_ids"].items()}
        inverse = {value: key for key, value in reviewed["customer_ids"].items()}
        bills = workspace.query("SELECT id,customer,amount_remaining,currency,status FROM fixture_stripe.invoices ORDER BY id")["rows"]
        replacement_bills = []
        for bill in bills:
            resolved = identity.resolve(workspace, "fixture_stripe", "customers", bill["customer"])
            alias = inverse[resolved["customer_id"]]
            replacement_bills.append(["new-" + bill["id"], "replacement-" + alias, bill["amount_remaining"], bill["currency"], bill["status"]])
        csv_source(workspace, self.root, "portable_parties", ["party_key", "label"],
                   [["replacement-" + alias, "Same synthetic label"] for alias in reviewed["customer_ids"]])
        csv_source(workspace, self.root, "portable_bills", ["bill_key", "party_key", "balance_minor", "currency_code", "phase"], replacement_bills)
        for alias, customer in reviewed["customer_ids"].items():
            link(workspace, "portable_parties", "party", "replacement-" + alias, customer)
        replacement = copy.deepcopy(old_profile)
        replacement["bindings"] = [binding for binding in replacement["bindings"] if binding["connection"] != "fixture_stripe"]
        replacement["bindings"] += [
            {"connection": "portable_parties", "entity": "party", "table": "portable_parties.records", "kind": "customer", "record_id": "party_key",
             "customer_ref": {"connection": "portable_parties", "entity": "party", "column": "party_key"}, "fields": {"label": "label"}},
            {"connection": "portable_bills", "entity": "bill", "table": "portable_bills.records", "kind": "billing", "record_id": "bill_key",
             "customer_ref": {"connection": "portable_parties", "entity": "party", "column": "party_key"},
             "fields": {"amount": "balance_minor", "currency": "currency_code", "status": "phase"}},
        ]
        snapshot, mapping, _ = approve_profile(workspace, replacement)
        self.assertNotEqual(mapping["approval_sha256"], reviewed["profile_approval"])
        with self.assertRaisesRegex(ValueError, "source mappings changed"):
            contexts.current_context(workspace, reviewed["profile"], snapshot)
        # Meaning is copied exactly and explicitly re-reviewed for the new
        # physical mapping. The old approval is retained, not relabeled.
        new_context = approve_context(workspace, reviewed["profile"], snapshot, old_context["definitions"], old_context["gaps"])
        self.assertEqual(new_context["definitions"], old_context["definitions"])
        self.assertEqual(new_context["definition_sha256"], old_context["definition_sha256"])
        self.assertEqual(new_context["previous_context_id"], old_context["context_id"])
        self.assertNotEqual(new_context["context_id"], old_context["context_id"])
        self.assertEqual(identity.list_customers(workspace), old_customers)
        for alias, customer in reviewed["customer_ids"].items():
            result = answers.execute_metric(workspace, reviewed["profile"], snapshot, customer, "outstanding_invoices")
            self.assertEqual(result["result"], old_metrics[alias]["result"])
            self.assertNotEqual(result["evidence_ids"], old_metrics[alias]["evidence_ids"])
            self.assertEqual({query["source_table"] for query in result["queries"]}, {"portable_bills.records"})
            self.assertEqual(result["customer_id"], customer)
            with self.assertRaises(ValueError):
                answers.validate_answer(workspace, reviewed["profile"], snapshot, customer,
                                        {"metrics": [{"definition_name": "outstanding_invoices", "result_id": old_metrics[alias]["result_id"]}], "quotes": []})
        self.assertNotIn("fixture_stripe", snapshots.load_snapshot(workspace, snapshot)["coverage"]["connections"])
        with workspace._connect() as conn:
            approvals = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM _contextbase.customer360_context_approvals ORDER BY version").fetchall()]
        self.assertEqual([value["context_id"] for value in approvals], [old_context["context_id"], new_context["context_id"]])

    def test_core_answer_logic_has_no_required_vendor_branch(self):
        # Unknown connection names and exact physical headers exercise the
        # production answer path; the code check is additional evidence only.
        workspace, customer, _, profile, snapshot, context = self.single_source()
        metric = answers.execute_metric(workspace, profile["name"], snapshot, customer, "open_amount")
        self.assertEqual(metric["result"]["values"], [{"currency": "EUR", "value": "1200"}, {"currency": "USD", "value": "4200"}])
        final = answers.validate_answer(workspace, profile["name"], snapshot, customer,
                                        {"metrics": [{"definition_name": "open_amount", "result_id": metric["result_id"]}], "quotes": []})
        self.assertEqual(final["context_id"], context["context_id"])
        self.assertEqual(metric["queries"][0]["source_table"], "ledger_alpha.records")
        self.assertIn('"Minor outstanding"', metric["queries"][0]["sql"])
        self.assertIn('"State"', metric["queries"][0]["sql"])
        known_vendors = {"salesforce", "gong", "stripe", "intercom", "linear", "mixpanel"}
        for module in (answers, contexts, profiles):
            literals = {node.value.lower() for node in ast.walk(ast.parse(Path(module.__file__).read_text()))
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            self.assertFalse(literals & known_vendors)


if __name__ == "__main__":
    unittest.main()
