"""Customer CLI foundation over real local CSV publication; no model/vendor calls."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contextbase.cli import execute, parser
from contextbase.customer360.contracts import PROFILE_SPEC, sha256
from contextbase.workspace import Workspace


class Customer360CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.billing = self.root / "billing.csv"
        self.billing.write_text("invoice_id,party_ref,amount,currency\ninv-a,party-a,150,USD\ninv-b,party-a,250,USD\ninv-c,unmapped,80,EUR\n")
        self.support = self.root / "support.csv"
        self.support.write_text("case_id,organization,body,status\ncase-a,org-a,Example follow-up,open\ncase-b,org-a,Example response,open\n")

    def cli(self, *arguments):
        return execute(parser().parse_args(["--workspace", str(self.workspace), *arguments]))

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)

    def connected(self):
        customer = self.cli("customer", "create", "--label", "Example Customer")
        for connection, path in (("billing", self.billing), ("support", self.support)):
            self.cli("source", "add", "csv", "--name", connection, "--path", str(path))
            self.cli("source", "sync", connection, "--no-bootstrap")
        links = [{"connection": connection, "entity": entity, "source_id": key,
                  "customer_id": customer["id"], "basis": "Reviewed explicit synthetic customer reference"}
                 for connection, entity, key in (("billing", "party", "party-a"), ("support", "organization", "org-a"))]
        proposal = self.cli("customer", "links", "propose", "--file", self.write_json("links.json", links))
        profile = {"spec_version": PROFILE_SPEC, "name": "customer_review", "description": "Reviewed billing and support mappings",
                   "bindings": [
                       {"connection": "billing", "entity": "invoice", "table": "billing.records", "kind": "billing",
                        "record_id": "invoice_id", "customer_ref": {"connection": "billing", "entity": "party", "column": "party_ref"},
                        "fields": {"amount": "amount", "currency": "currency"}},
                       {"connection": "support", "entity": "case", "table": "support.records", "kind": "support",
                        "record_id": "case_id", "customer_ref": {"connection": "support", "entity": "organization", "column": "organization"},
                        "fields": {"text": "body", "status": "status"}}]}
        return customer, proposal, self.write_json("profile.json", profile)

    def approve(self, kind, proposal, *, approval=None):
        return self.cli("customer", kind, "approve", proposal["proposal_id"],
                        "--approve", proposal["approval_sha256"] if approval is None else approval,
                        "--reviewer", "example-reviewer")

    def reviewed(self):
        customer, links, profile_file = self.connected()
        self.approve("links", links)
        snapshot = self.cli("customer", "snapshot", "--source", "support", "--source", "billing")
        candidate = self.cli("customer", "profile", "propose", "--file", profile_file,
                             "--snapshot", snapshot["snapshot_id"])
        approved = self.approve("profile", candidate)
        return customer, snapshot, approved

    def materialize(self, snapshot):
        return self.cli("customer", "materialize", "--profile", "customer_review", "--snapshot", snapshot["snapshot_id"])

    def records(self, customer, snapshot):
        return self.cli("customer", "records", "--profile", "customer_review", "--customer", customer["id"],
                        "--snapshot", snapshot["snapshot_id"])

    def test_full_csv_review_projection_and_refresh_flow(self):
        customer, snapshot, approved = self.reviewed()
        self.assertEqual(self.cli("customer", "list"), [customer])
        self.assertEqual(snapshot["coverage"]["connections"], ["billing", "support"])
        self.assertEqual(snapshot["freshness"]["max_age_seconds"], 86400)
        self.assertEqual(snapshot["freshness"]["max_skew_seconds"], 3600)
        self.assertEqual(self.cli("customer", "profile", "show", "customer_review"), approved)
        first = self.materialize(snapshot)
        self.assertEqual(first["row_count"], 5)
        evidence = self.records(customer, snapshot)
        self.assertEqual(evidence["customer_id"], customer["id"])
        self.assertEqual(evidence["capabilities"], ["billing", "support"])
        self.assertEqual({record["source_id"] for record in evidence["records"]}, {"inv-a", "inv-b", "case-a", "case-b"})
        self.assertEqual(sum(record["fields"]["amount"] for record in evidence["records"] if record["kind"] == "billing"), 400)
        self.assertEqual(first["coverage"][0]["identity"], {"resolved": 2, "unresolved": 1})
        self.billing.write_text(self.billing.read_text().replace(",150,", ",175,"))
        self.cli("source", "sync", "billing", "--no-bootstrap")
        with self.assertRaises(ValueError):
            self.records(customer, snapshot)
        refreshed = self.cli("customer", "snapshot", "--source", "billing", "--source", "support")
        self.assertNotEqual(refreshed["snapshot_id"], snapshot["snapshot_id"])
        second = self.materialize(refreshed)
        self.assertEqual(second["profile_approval"], first["profile_approval"])
        self.assertEqual(self.cli("customer", "profile", "show", "customer_review"), approved)
        records = self.records(customer, refreshed)["records"]
        self.assertEqual(sum(record["fields"]["amount"] for record in records if record["kind"] == "billing"), 425)
        self.assertEqual(self.cli("customer", "list"), [customer])
        self.assertEqual(Workspace(self.workspace).query("SELECT sum(amount) AS total FROM billing.records")["rows"], [{"total": 505}])

    def test_exact_link_and_profile_hashes_are_required(self):
        customer, links, profile_file = self.connected()
        with self.assertRaises(ValueError):
            self.approve("links", links, approval="0" * 64)
        self.assertEqual(Workspace(self.workspace).query("SELECT count(*) AS n FROM _contextbase.customer360_links")["rows"], [{"n": 0}])
        self.approve("links", links)
        snapshot = self.cli("customer", "snapshot", "--source", "billing", "--source", "support")
        candidate = self.cli("customer", "profile", "propose", "--file", profile_file, "--snapshot", snapshot["snapshot_id"])
        with self.assertRaises(ValueError):
            self.approve("profile", candidate, approval="0" * 64)
        with self.assertRaises(ValueError):
            self.materialize(snapshot)
        self.approve("profile", candidate)
        self.materialize(snapshot)
        self.assertEqual(len(self.records(customer, snapshot)["records"]), 4)

    def test_unknown_customer_is_rejected(self):
        _, snapshot, _ = self.reviewed()
        self.materialize(snapshot)
        with self.assertRaisesRegex(ValueError, "belonging"):
            self.records({"id": "other-workspace-customer"}, snapshot)

    def test_business_review_brief_metric_evidence_and_template_commands(self):
        from tests.test_customer360_contexts import definitions
        customer, snapshot, _ = self.reviewed()
        self.materialize(snapshot)
        scope = ("--profile", "customer_review", "--snapshot", snapshot["snapshot_id"])
        customer_scope = (*scope, "--customer", customer["id"])
        with self.assertRaisesRegex(ValueError, "Review business context"):
            self.cli("customer", "brief", *customer_scope)
        rules = self.write_json("definitions.json", {"definitions": definitions(), "gaps": []})
        proposal = self.cli("customer", "context", "propose", *scope, "--file", rules)
        approved = self.approve("context", proposal)
        self.assertEqual(self.cli("customer", "context", "show", *scope)["context_id"], approved["context_id"])
        metric = self.cli("customer", "metric", *customer_scope, "invoice_value")
        self.assertEqual(metric["result"]["values"], [{"currency": "USD", "value": "400"}])
        brief = self.cli("customer", "brief", *customer_scope)
        self.assertEqual(len(brief["metrics"]), 2)
        self.assertEqual(len(brief["quotes"]), 2)
        quote = brief["quotes"][0]
        self.assertEqual(self.cli("customer", "evidence", *customer_scope, quote["evidence_id"])["text"], quote["text"])
        target = self.root / "template.json"
        self.cli("customer", "context", "export-template", *scope, "--output", str(target))
        self.assertEqual(json.loads(target.read_text())["status"], "requires_local_binding_and_review")
        saved = self.cli("customer", "review", "save", *customer_scope)
        self.assertEqual(saved["brief"], brief)
        self.assertEqual(self.cli("customer", "review", "show", saved["review_id"]), saved)
        listed = self.cli("customer", "review", "list", "--profile", "customer_review", "--customer", customer["id"])
        self.assertEqual([item["review_id"] for item in listed], [saved["review_id"]])
        self.billing.write_text(self.billing.read_text().replace(",150,", ",175,"))
        self.cli("source", "sync", "billing", "--no-bootstrap")
        refreshed = self.cli("customer", "snapshot", "--source", "billing", "--source", "support")
        self.materialize(refreshed)
        compared = self.cli("customer", "review", "compare", saved["review_id"], "--snapshot", refreshed["snapshot_id"])
        self.assertEqual(compared["status"], "same_reviewed_basis")
        self.assertEqual(compared["metric_changes"], [{"definition_name": "invoice_value", "unit": "currency",
                                                       "currency": "USD", "before": "400", "after": "425", "delta": "25"}])

    def test_snapshot_freshness_options_are_enforced(self):
        self.connected()
        snapshot = self.cli("customer", "snapshot", "--source", "support", "--source", "billing",
                            "--max-age-seconds", "600", "--max-skew-seconds", "300")
        self.assertEqual(snapshot["freshness"]["max_age_seconds"], 600)
        self.assertEqual(snapshot["freshness"]["max_skew_seconds"], 300)
        with self.assertRaises(ValueError):
            self.cli("customer", "snapshot", "--source", "billing", "--max-age-seconds", "-1")
        with self.assertRaises(ValueError):
            self.cli("customer", "snapshot", "--source", "billing", "--source", "billing")

    def test_latest_and_temporal_evidence_cli_preserve_scope_time_and_quote(self):
        from tests.test_customer360_contexts import definitions

        with self.support.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["case_id", "organization", "body", "status"])
            for key, body, timestamp in (("case-a", "Original request.", 1768305600),
                                         ("case-b", "The export issue is resolved.", 1768392000)):
                writer.writerow([key, "org-a", json.dumps({"messages": [{"body": body, "created_at": timestamp}]}), "open"])
        customer, links, profile_file = self.connected()
        self.approve("links", links)
        profile = json.loads(Path(profile_file).read_text())
        profile["bindings"][1]["passages"] = [{"name": "message", "field": "text", "text_path": "/messages/*/body",
            "time": {"source": "item", "path": "/created_at", "encoding": "unix_seconds", "meaning": "passage_created"}}]
        profile_file = self.write_json("profile.json", profile)
        snapshot = self.cli("customer", "snapshot", "--source", "billing", "--source", "support")
        proposal = self.cli("customer", "profile", "propose", "--file", profile_file, "--snapshot", snapshot["snapshot_id"])
        mapping = self.approve("profile", proposal)
        materialized = self.materialize(snapshot)
        scope = ("--profile", "customer_review", "--snapshot", snapshot["snapshot_id"])
        customer_scope = (*scope, "--customer", customer["id"])
        rules = self.write_json("definitions.json", {"definitions": definitions(), "gaps": []})
        proposal = self.cli("customer", "context", "propose", *scope, "--file", rules)
        context = self.approve("context", proposal)
        selected = self.cli("customer", "latest", *customer_scope, "--kind", "support", "--limit", "1")
        self.assertEqual(selected["status"], "latest_unique")
        self.assertEqual(selected["kind"], "support")
        self.assertEqual(selected["limit"], 1)
        self.assertTrue(selected["coverage"]["complete"])
        self.assertFalse(selected["coverage"]["truncated"])
        self.assertEqual(selected["latest_time"], {"unix_seconds": "1768392000", "utc": "2026-01-14T12:00:00Z"})
        quote, = selected["quotes"]
        self.assertEqual(quote["text"], "The export issue is resolved.")
        self.assertEqual(quote["source_id"], "case-b")
        self.assertEqual(quote["connection"], "support")
        self.assertEqual(quote["entity"], "case")
        self.assertEqual(quote["passage_name"], "message")
        self.assertEqual(quote["json_pointer"], "/messages/0/body")
        self.assertEqual(quote["time_lineage"]["json_pointer"], "/messages/0/created_at")
        self.assertEqual(quote["time"]["meaning"], "passage_created")
        for receipt, hash_key in ((selected, "selection_id"), (quote, "quote_id")):
            self.assertEqual(receipt["customer_id"], customer["id"])
            self.assertEqual(receipt["profile"], "customer_review")
            self.assertEqual(receipt["snapshot_id"], snapshot["snapshot_id"])
            self.assertEqual(receipt["profile_approval"], mapping["approval_sha256"])
            self.assertEqual(receipt["context_id"], context["context_id"])
            self.assertEqual(receipt["materialization_id"], materialized["materialization_id"])
            self.assertEqual(receipt[hash_key], sha256({key: value for key, value in receipt.items() if key != hash_key}))
        locator = ("--field", quote["field"], "--passage-name", quote["passage_name"], "--json-pointer", quote["json_pointer"],
                   "--start", str(quote["start"]), "--end", str(quote["end"]))
        reopened = self.cli("customer", "evidence", *customer_scope, quote["evidence_id"], *locator)
        self.assertEqual(reopened, quote)
        for unpaired in (("--passage-name", "message"), ("--json-pointer", "/messages/0/body")):
            with self.subTest(unpaired=unpaired), self.assertRaisesRegex(ValueError, "both passage_name and json_pointer"):
                self.cli("customer", "evidence", *customer_scope, quote["evidence_id"], *unpaired)
        # A committed source refresh invalidates both CLI views of old scope;
        # the quote's valid historical hash does not make it current evidence.
        self.support.write_text(self.support.read_text().replace("The export issue is resolved.", "A new source update."))
        self.cli("source", "sync", "support", "--no-bootstrap")
        with self.assertRaises(ValueError):
            self.cli("customer", "latest", *customer_scope, "--kind", "support")
        with self.assertRaises(ValueError):
            self.cli("customer", "evidence", *customer_scope, quote["evidence_id"], *locator)

    def test_new_process_lists_persisted_customer_as_json(self):
        customer = self.cli("customer", "create", "--label", "Example Customer")
        completed = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(self.workspace),
                                    "customer", "list"], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [customer])
        self.assertEqual(completed.stderr, "")

    def test_bootstrap_optional_intent_is_forwarded_verbatim_without_an_inference(self):
        # Transport coverage only: agent tests independently check the staged
        # candidate contract. This stub must not be called model evidence.
        scope = ("customer", "context", "bootstrap", "--profile", "customer_review",
                 "--snapshot", "synthetic-snapshot", "--allow-model-data",
                 "--max-cost-usd", "0.25", "--max-turns", "4")
        intent = '  Compare the reviewed amount for status "open" with supporting passages.  '
        with patch("contextbase.customer360.agent.bootstrap", return_value={"transport_test": True}) as bootstrap:
            self.assertEqual(self.cli(*scope, "--intent", intent), {"transport_test": True})
            self.assertEqual(bootstrap.call_args.args[1:], ("customer_review", "synthetic-snapshot"))
            self.assertEqual(bootstrap.call_args.kwargs, {"allow_model_data": True, "max_turns": 4,
                                                        "max_cost_usd": 0.25, "intent": intent})
            self.cli(*scope)
            self.assertIsNone(bootstrap.call_args.kwargs["intent"])
        self.assertFalse(Workspace(self.workspace).db_path.exists())
        completed = subprocess.run([sys.executable, "-m", "contextbase", "customer", "context", "bootstrap", "--help"],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--intent", completed.stdout)

    def test_ambiguous_nonfinite_and_oversized_json_inputs_are_refused(self):
        invalid = [
            '[{"source_id":"synthetic-private-detail","source_id":"duplicate"}]',
            '[NaN]',
            '{"padding":"' + "x" * 1_100_000 + '"}',
        ]
        for raw in invalid:
            path = self.root / "invalid.json"
            path.write_text(raw)
            for command in (("customer", "links", "propose", "--file", str(path)),
                            ("customer", "profile", "propose", "--file", str(path), "--snapshot", "not-read")):
                with self.subTest(command=command, bytes=len(raw)):
                    with self.assertRaises(ValueError) as error:
                        self.cli(*command)
                    self.assertNotIn("synthetic-private-detail", str(error.exception))
        self.assertFalse(Workspace(self.workspace).db_path.exists())


if __name__ == "__main__":
    unittest.main()
