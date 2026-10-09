"""Business context lifecycle over real file-connector publications."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from contextbase.customer360 import contexts, profiles, snapshots
from contextbase.connectors import service
from tests import test_customer360_profiles as fixtures


def definitions():
    return [
        {"name": "invoice_value", "description": "Sum of the mapped invoice amounts; no recurring-revenue claim.",
         "kind": "billing", "operation": "sum", "field": "amount", "filters": [],
         "unit": "currency", "currency_field": "currency"},
        {"name": "open_cases", "description": "Count source case records whose status is open.",
         "kind": "support", "operation": "count", "field": None,
         "filters": [{"field": "status", "operator": "eq", "value": "open"}],
         "unit": "records", "currency_field": None},
    ]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.w
        self.profile = self.fixture.profile["name"]
        self.snapshot = self.fixture.snapshot["snapshot_id"]
        self.fixture.reviewed()
        profiles.materialize(self.w, self.profile, self.snapshot)

    def propose(self, rules=None, **kwargs):
        return contexts.propose_context(self.w, self.profile, self.snapshot, rules or definitions(), **kwargs)

    def approve(self, proposal=None):
        proposal = proposal or self.propose()
        return contexts.approve_context(self.w, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")

    def test_candidates_are_not_authoritative_and_review_is_exact(self):
        proposal = self.propose(gaps=[{"code": "unknown_recurring", "description": "Invoices do not establish recurring revenue."}])
        self.assertEqual(proposal["status"], "candidate")
        with self.assertRaisesRegex(ValueError, "Review business context"):
            contexts.current_context(self.w, self.profile, self.snapshot)
        with self.assertRaises(ValueError):
            contexts.approve_context(self.w, proposal["proposal_id"], "0" * 64, "example-reviewer")
        approved = self.approve(proposal)
        current = contexts.current_context(self.w, self.profile, self.snapshot)
        self.assertEqual(current["context_id"], approved["context_id"])
        self.assertEqual(current["gaps"][0]["code"], "unknown_recurring")
        self.assertEqual(current["origin"], "operator_authored")

    def test_source_capabilities_and_unresolved_evidence_are_inspected(self):
        inspected = contexts.inspect_context(self.w, self.profile, self.snapshot)
        self.assertEqual({b["kind"] for b in inspected["profile"]["bindings"]}, {"billing", "support"})
        self.assertEqual(inspected["upstream_completeness"], "not_verified")
        self.assertEqual(inspected["coverage"][0]["identity"]["unresolved"], 1)
        self.assertTrue(any(r["link_status"] == "unresolved" for r in inspected["samples"]))
        self.assertNotIn("customer_id", inspected["samples"][0])
        with self.assertRaises(ValueError):
            contexts.inspect_context(self.w, self.profile, self.snapshot, max_samples=6)

    def test_definitions_require_observed_fields_and_explicit_units(self):
        approved = profiles.load_profile(self.w, self.profile)
        invalid = []
        for key, value in (("field", "invented_arr"), ("currency_field", None), ("kind", "commercial"),
                           ("operation", "eval"), ("unit", "guessed")):
            item = definitions()
            item[0][key] = value
            invalid.append(item)
        item = definitions()
        item[1]["filters"][0]["value"] = {"sql": "DROP TABLE customer360.records"}
        invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                contexts.validate_definitions(item, approved)

    def test_approved_context_reused_by_a_fresh_process(self):
        approved = self.approve()
        code = "from contextbase.workspace import Workspace; from contextbase.customer360.contexts import current_context; import json,sys; print(json.dumps(current_context(Workspace(sys.argv[1]),sys.argv[2],sys.argv[3])))"
        result = subprocess.run([sys.executable, "-c", code, str(self.w.root), self.profile, self.snapshot],
                                text=True, capture_output=True, timeout=15, check=True)
        reused = json.loads(result.stdout)
        self.assertEqual(reused["context_id"], approved["context_id"])
        self.assertEqual(reused["definition_sha256"], approved["definition_sha256"])

    def test_changed_facts_reuse_definitions_but_mapping_revision_invalidates(self):
        approved = self.approve()
        self.fixture.billing.write_text(self.fixture.billing.read_text().replace(",150,", ",175,"))
        service.sync(self.w, "billing", bootstrap_context=False)
        refreshed = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.profile, refreshed)
        current = contexts.current_context(self.w, self.profile, refreshed)
        self.assertEqual(current["definition_sha256"], approved["definition_sha256"])
        self.assertEqual(current["context_id"], approved["context_id"])
        self.assertEqual(current["approved_snapshot_id"], self.snapshot)
        self.assertEqual(current["snapshot_id"], refreshed)
        changed = copy.deepcopy(self.fixture.profile)
        changed["description"] = "Explicitly revised field mapping review"
        proposal = profiles.propose_profile(self.w, changed, refreshed)
        profiles.approve_profile(self.w, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")
        profiles.materialize(self.w, self.profile, refreshed)
        with self.assertRaisesRegex(ValueError, "source mappings changed"):
            contexts.current_context(self.w, self.profile, refreshed)

    def test_context_revision_preserves_history_and_rejects_stale_review(self):
        first = self.approve()
        a = self.propose()
        rules = definitions()
        rules[1]["filters"][0]["value"] = "closed"
        b = self.propose(rules)
        second = self.approve(b)
        self.assertNotEqual(first["definition_sha256"], second["definition_sha256"])
        self.assertEqual(second["previous_context_id"], first["context_id"])
        self.assertEqual(second["version"], 2)
        with self.assertRaisesRegex(ValueError, "changed after proposal"):
            self.approve(a)
        with self.w._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM _contextbase.customer360_context_approvals").fetchone()[0], 2)

    def test_unpublished_and_altered_proposals_cannot_approve(self):
        proposal = self.propose()
        with self.w._connect(writable=True) as conn:
            row = conn.execute("SELECT payload FROM _contextbase.customer360_context_candidates").fetchone()
            changed = json.loads(row[0])
            changed["definitions"][0]["description"] = "Altered after review"
            conn.execute("UPDATE _contextbase.customer360_context_candidates SET payload=?", [json.dumps(changed)])
        with self.assertRaisesRegex(ValueError, "proposal changed"):
            self.approve(proposal)

    def test_export_template_excludes_rows_text_and_rule_literals(self):
        rules = definitions()
        rules[0]["description"] = "private-source-sentence"
        rules[1]["filters"][0]["value"] = "private-status-literal"
        self.approve(self.propose(rules, gaps=[{"code": "unresolved", "description": "private-gap-text"}],
                                   stages=[{"private_stage": "private-agent-text"}]))
        target = self.fixture.root / "template.json"
        contexts.export_template(self.w, self.profile, self.snapshot, target)
        raw = target.read_text()
        for forbidden in ("private-", "Needs follow-up", self.fixture.customer["id"], "party-a", "snapshot_id"):
            self.assertNotIn(forbidden, raw)
        artifact = json.loads(raw)
        self.assertEqual(artifact["status"], "requires_local_binding_and_review")
        self.assertTrue(artifact["definitions"][1]["filters"][0]["requires_local_value"])
        with self.assertRaises(FileExistsError):
            contexts.export_template(self.w, self.profile, self.snapshot, target)


if __name__ == "__main__":
    unittest.main()
