"""Local identity acceptance: real DuckDB with independent synthetic relations."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from contextbase.customer360.identity import (
    approve_links, create_customer, ensure_identity_schema, identity_head,
    list_customers, propose_links, resolve, resolve_in_connection,
)
from contextbase.workspace import Workspace


class CustomerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / "first")
        self.first = create_customer(self.workspace, "Example Customer")
        self.second = create_customer(self.workspace, "Other Example Customer")

    def link(self, source_id="00017", customer=None, connection="example-crm", entity="accounts", basis="Reviewed explicit source reference"):
        return {"connection": connection, "entity": entity, "source_id": source_id,
                "customer_id": (customer or self.first)["id"], "basis": basis}

    def approve(self, links):
        proposal = propose_links(self.workspace, links)
        return approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")

    def test_canonical_identity_is_not_a_vendor_primary_key(self):
        self.approve([self.link()])
        canonical_id = self.first["id"]
        self.assertEqual(uuid.UUID(canonical_id).version, 4)
        self.assertNotEqual(canonical_id, "00017")
        self.assertNotEqual(canonical_id, self.first["workspace_id"])
        reopened = Workspace(self.workspace.root)
        self.assertEqual(list_customers(reopened), list_customers(self.workspace))
        self.assertEqual(resolve(reopened, "example-crm", "accounts", "00017")["customer_id"], canonical_id)
        other = create_customer(Workspace(Path(self.temp.name) / "second"), "Example Customer")
        self.assertNotEqual(other["id"], canonical_id)
        self.assertNotEqual(other["workspace_id"], self.first["workspace_id"])

    def test_explicit_links_and_reviewed_mapping_resolve(self):
        links = [self.link(), self.link("org-92", connection="example-support", entity="organizations")]
        proposal = propose_links(self.workspace, links)
        self.assertEqual(resolve(self.workspace, "example-crm", "accounts", "00017")["status"], "unresolved")
        with self.assertRaises(ValueError):
            approve_links(self.workspace, proposal["proposal_id"], "0" * 64, "example-reviewer")
        receipt = approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")
        self.assertEqual(receipt["links_added"], 2)
        self.assertEqual(receipt["mapping_version"], 1)
        for link in links:
            resolved = resolve(self.workspace, link["connection"], link["entity"], link["source_id"])
            self.assertEqual((resolved["status"], resolved["customer_id"]), ("resolved", self.first["id"]))
            self.assertEqual(resolved["mapping_sha256"], receipt["mapping_sha256"])
        with self.assertRaises(ValueError):
            approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")

    def test_duplicate_names_and_shared_domains_do_not_auto_merge(self):
        duplicate = create_customer(self.workspace, "Example Customer")
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE SCHEMA imported")
            conn.execute("CREATE TABLE imported.accounts (id VARCHAR, name VARCHAR, domain VARCHAR)")
            conn.executemany("INSERT INTO imported.accounts VALUES (?, ?, ?)", [
                ["00017", "Example Customer", "shared.example.invalid"],
                ["00018", "Example Customer", "shared.example.invalid"],
            ])
        self.assertNotEqual(duplicate["id"], self.first["id"])
        for source_id in ("00017", "00018"):
            self.assertEqual(resolve(self.workspace, "example-crm", "accounts", source_id)["status"], "unresolved")
        self.approve([self.link(), self.link("00018", customer=duplicate)])
        customers = {resolve(self.workspace, "example-crm", "accounts", source_id)["customer_id"] for source_id in ("00017", "00018")}
        self.assertEqual(customers, {self.first["id"], duplicate["id"]})

    def test_missing_and_conflicting_links_remain_unresolved(self):
        missing = resolve(self.workspace, "example-crm", "accounts", "missing")
        self.assertEqual((missing["status"], missing["customer_id"]), ("unresolved", None))
        proposal = propose_links(self.workspace, [self.link(), self.link(customer=self.second)])
        self.assertEqual(proposal["status"], "conflict")
        resolved = resolve(self.workspace, "example-crm", "accounts", "00017")
        self.assertEqual((resolved["status"], resolved["customer_id"]), ("conflict", None))
        with self.assertRaises(ValueError):
            approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_links")["rows"], [{"n": 0}])
        self.approve([self.link()])
        contradictory = propose_links(self.workspace, [self.link(customer=self.second)])
        self.assertEqual(contradictory["conflicts"][0]["reason"], "existing_assignment")
        with self.assertRaises(ValueError):
            approve_links(self.workspace, contradictory["proposal_id"], contradictory["approval_sha256"], "example-reviewer")
        self.assertEqual(resolve(self.workspace, "example-crm", "accounts", "00017")["customer_id"], self.first["id"])

    def test_one_to_many_evidence_does_not_multiply_value(self):
        # Independent fixture: three invoices worth 400, four support records.
        invoices = [("invoice-1", "billing-1", 100), ("invoice-2", "billing-1", 50), ("invoice-3", "billing-2", 250)]
        tickets = [("ticket-1", "support-1"), ("ticket-2", "support-1"), ("ticket-3", "support-1"), ("ticket-4", "support-2")]
        links = [self.link("billing-1", connection="billing", entity="customers"),
                 self.link("billing-2", customer=self.second, connection="billing", entity="customers"),
                 self.link("support-1", connection="support", entity="organizations"),
                 self.link("support-2", customer=self.second, connection="support", entity="organizations")]
        self.approve(links)
        # Re-reviewing the same links must not append duplicate join keys.
        self.assertEqual(self.approve(links)["links_added"], 0)
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE SCHEMA imported")
            conn.execute("CREATE TABLE imported.invoices (id VARCHAR, customer_source_id VARCHAR, value INTEGER)")
            conn.execute("CREATE TABLE imported.tickets (id VARCHAR, organization_source_id VARCHAR)")
            conn.executemany("INSERT INTO imported.invoices VALUES (?, ?, ?)", invoices)
            conn.executemany("INSERT INTO imported.tickets VALUES (?, ?)", tickets)
        result = self.workspace.query("""
            WITH supported_customers AS (
                SELECT DISTINCT l.customer_id
                FROM imported.tickets t JOIN _contextbase.customer360_links l
                  ON l.connection='support' AND l.entity='organizations'
                 AND l.source_id=t.organization_source_id
            )
            SELECT sum(i.value) AS total, count(*) AS invoice_count,
                   count(DISTINCT l.customer_id) AS customer_count
            FROM imported.invoices i JOIN _contextbase.customer360_links l
              ON l.connection='billing' AND l.entity='customers'
             AND l.source_id=i.customer_source_id
            WHERE l.customer_id IN (SELECT customer_id FROM supported_customers)
        """)
        self.assertEqual(result["rows"], [{"total": 400, "invoice_count": 3, "customer_count": 2}])
        self.assertEqual(sum(invoice[2] for invoice in invoices), 400)
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_links")["rows"], [{"n": 4}])

    def test_changed_source_identifier_preserves_reviewed_customer_identity(self):
        old = self.approve([self.link()])
        alias = self.link("replaced-00017", connection="replacement-crm")
        pending = propose_links(self.workspace, [alias])
        self.assertEqual(resolve(self.workspace, "replacement-crm", "accounts", "replaced-00017")["status"], "unresolved")
        new = approve_links(self.workspace, pending["proposal_id"], pending["approval_sha256"], "example-reviewer")
        self.assertEqual(new["previous_mapping_sha256"], old["mapping_sha256"])
        self.assertEqual(new["mapping_version"], 2)
        for connection, source_id in (("example-crm", "00017"), ("replacement-crm", "replaced-00017")):
            self.assertEqual(resolve(self.workspace, connection, "accounts", source_id)["customer_id"], self.first["id"])
        rows = self.workspace.query("SELECT version FROM _contextbase.customer360_links ORDER BY version")["rows"]
        self.assertEqual(rows, [{"version": 1}, {"version": 2}])

    def test_source_key_scope_and_string_values_are_preserved(self):
        links = [self.link("00017"), self.link("17", customer=self.second),
                 self.link("00017", customer=self.second, entity="contacts"),
                 self.link("00017", customer=self.second, connection="another-crm"),
                 self.link("x'; DROP TABLE any_table; --", basis="SELECT is descriptive, never executed")]
        self.approve(links)
        for link in links:
            resolved = resolve(self.workspace, link["connection"], link["entity"], link["source_id"])
            self.assertEqual(resolved["customer_id"], link["customer_id"])
            self.assertEqual(resolved["source_key"]["source_id"], link["source_id"])
        with self.assertRaises(ValueError):
            resolve(self.workspace, "example-crm", "accounts", 17)

    def test_stale_review_rolls_back_all_new_links(self):
        stale = propose_links(self.workspace, [self.link("a"), self.link("b")])
        self.approve([self.link("c")])
        with self.assertRaises(ValueError):
            approve_links(self.workspace, stale["proposal_id"], stale["approval_sha256"], "example-reviewer")
        self.assertEqual(self.workspace.query("SELECT source_id FROM _contextbase.customer360_links")["rows"], [{"source_id": "c"}])
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_link_reviews")["rows"], [{"n": 1}])

    def test_invalid_batch_and_foreign_customer_are_atomic(self):
        foreign = create_customer(Workspace(Path(self.temp.name) / "foreign"), "Foreign Synthetic")
        invalid = [[], [self.link(source_id=17)], [self.link(basis="")],
                   [self.link(), self.link("foreign", customer=foreign)],
                   [{**self.link(), "extra": "unexpected"}], [self.link(source_id="x" * 1025)]]
        for links in invalid:
            with self.subTest(links=links), self.assertRaises(ValueError):
                propose_links(self.workspace, links)
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_link_proposals")["rows"], [{"n": 0}])

    def test_caller_transaction_lookup_observes_same_head(self):
        receipt = self.approve([self.link()])
        with self.workspace._connect(writable=True) as conn:
            conn.begin()
            ensure_identity_schema(conn)
            head = identity_head(conn)
            result = resolve_in_connection(conn, "example-crm", "accounts", "00017")
            self.assertEqual(head, {"workspace_id": self.first["workspace_id"], "version": 1, "sha256": receipt["mapping_sha256"]})
            self.assertEqual(result["customer_id"], self.first["id"])
            conn.rollback()

    def test_concurrent_reviews_cannot_commit_two_proposals_at_same_head(self):
        proposals = [propose_links(self.workspace, [self.link(source_id)]) for source_id in ("a", "b")]
        barrier = threading.Barrier(2)
        def review(proposal):
            barrier.wait(timeout=5)
            try:
                return approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")["status"]
            except ValueError:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(review, proposals))
        self.assertEqual(sorted(results), ["approved", "refused"])
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_links")["rows"], [{"n": 1}])
        self.assertEqual(self.workspace.query("SELECT version FROM _contextbase.customer360_identity_state")["rows"], [{"version": 1}])

    def test_duplicate_exact_key_and_pending_disagreement_are_visible(self):
        duplicate = propose_links(self.workspace, [self.link(), self.link()])
        self.assertEqual(duplicate["status"], "conflict")
        with self.assertRaises(ValueError):
            approve_links(self.workspace, duplicate["proposal_id"], duplicate["approval_sha256"], "example-reviewer")
        propose_links(self.workspace, [self.link("another")])
        propose_links(self.workspace, [self.link("another", customer=self.second)])
        result = resolve(self.workspace, "example-crm", "accounts", "another")
        self.assertEqual((result["status"], result["customer_id"], result["candidate_count"]), ("conflict", None, 2))

    def test_modified_proposal_cannot_reuse_prior_approval(self):
        proposal = propose_links(self.workspace, [self.link()])
        with self.workspace._connect(writable=True) as conn:
            stored = json.loads(conn.execute("SELECT payload FROM _contextbase.customer360_link_proposals").fetchone()[0])
            stored["links"][0]["customer_id"] = self.second["id"]
            conn.execute("UPDATE _contextbase.customer360_link_proposals SET payload=?", [json.dumps(stored)])
        with self.assertRaises(ValueError):
            approve_links(self.workspace, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM _contextbase.customer360_links")["rows"], [{"n": 0}])


if __name__ == "__main__":
    unittest.main()
