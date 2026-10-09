"""Retained review integration over actual local CSV publications and DuckDB.

No model/vendor calls. These seals detect inconsistent local artifacts, not a
workspace owner rewriting every artifact and its expected hashes.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, identity, profiles, reviews, snapshots
from contextbase.customer360.contracts import canonical, sha256
from tests import test_customer360_contexts as fixtures


class CustomerReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.make_fixture()
        self.w, self.profile, self.snapshot = self.fixture.w, self.fixture.profile, self.fixture.snapshot
        self.customer = self.fixture.fixture.customer["id"]
        self.approved = self.fixture.approve()

    def make_fixture(self):
        fixture = fixtures.ContextTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def save(self, **options):
        return reviews.save_review(self.w, self.profile, self.snapshot, self.customer, **options)

    def refresh(self, *, billing=None, support=None):
        for name, content in (("billing", billing), ("support", support)):
            if content is not None:
                getattr(self.fixture.fixture, name).write_text(content)
                service.sync(self.w, name, bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.profile, self.snapshot)

    def test_saved_brief_is_computed_scoped_and_reused_in_fresh_process(self):
        expected = answers.brief(self.w, self.profile, self.snapshot, self.customer, quote_limit=1)
        saved = self.save(quote_limit=1)
        self.assertEqual(saved["brief"], expected)
        self.assertEqual(saved["brief"]["metrics"][0]["result"]["values"], [{"currency": "USD", "value": "400"}])
        self.assertEqual(len(saved["brief"]["quotes"]), 1)
        self.assertEqual(len(saved["records"]), 4)  # Full scoped index despite bounded quotations.
        self.assertNotIn("inv-c", {row["source_id"] for row in saved["records"]})
        self.assertEqual(saved["status"], "retained_review_not_current_answer")
        self.assertEqual(saved["retention_scope"], "private_local_operator_workspace")
        self.assertEqual(saved["review_id"], sha256({key: value for key, value in saved.items() if key != "review_id"}))
        code = """
import json, socket, sys
from unittest.mock import patch
from contextbase.workspace import Workspace
from contextbase.customer360 import answers, reviews
with patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')), patch.object(answers, '_scope', side_effect=AssertionError('Historical load must not claim fresh evidence')):
    print(json.dumps(reviews.load_review(Workspace(sys.argv[1]), sys.argv[2])))
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.w.root), saved["review_id"]],
                                 text=True, capture_output=True, check=True, timeout=20)
        self.assertEqual(json.loads(process.stdout), saved)
        listed = reviews.list_reviews(self.w, self.profile, self.customer)
        self.assertEqual(len(listed), 1)
        self.assertEqual(set(listed[0]), {"review_id", "profile", "customer_id", "created_at", "snapshot_id", "context_id", "status"})
        self.assertNotIn("Needs follow-up", json.dumps(listed))

    def test_refresh_compares_exact_changes_without_rewriting_saved_evidence(self):
        saved = self.save(quote_limit=1)
        unchanged = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.assertEqual(unchanged["status"], "same_reviewed_basis")
        self.assertEqual(unchanged["metric_changes"], [])
        self.assertEqual(unchanged["record_changes"], {"added": [], "removed": [], "changed": []})
        self.refresh(billing="invoice_id,party_ref,amount,currency\ninv-a,party-a,200,USD\ninv-d,party-a,90,USD\ninv-c,missing,80,EUR\n",
                     support="case_id,organization,body,status\ncase-b,org-a,Request received,open\ncase-c,org-a,Resolved by source update,closed\n")
        compared = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.assertEqual(compared["status"], "same_reviewed_basis")
        self.assertEqual(compared["changed_basis"], [])
        self.assertEqual(compared["metric_changes"], [
            {"definition_name": "invoice_value", "unit": "currency", "currency": "USD", "before": "400", "after": "290", "delta": "-110"},
            {"definition_name": "open_cases", "unit": "records", "currency": None, "before": "2", "after": "1", "delta": "-1"}])
        changes = compared["record_changes"]
        self.assertEqual({row["source_id"] for row in changes["added"]}, {"inv-d", "case-c"})
        self.assertEqual({row["source_id"] for row in changes["removed"]}, {"inv-b", "case-a"})
        self.assertEqual(len(changes["changed"]), 1)
        self.assertEqual(changes["changed"][0]["before"]["source_id"], "inv-a")
        self.assertEqual(changes["changed"][0]["before"]["fields"]["amount"], 150)
        self.assertEqual(changes["changed"][0]["after"]["fields"]["amount"], 200)
        self.assertEqual(compared["comparison_id"], sha256({key: value for key, value in compared.items() if key != "comparison_id"}))
        self.assertEqual(compared["previous_snapshot_id"], saved["snapshot_id"])
        self.assertEqual(compared["snapshot_id"], self.snapshot)
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_missing_currency_group_is_not_fabricated_zero(self):
        saved = self.save()
        self.refresh(billing="invoice_id,party_ref,amount,currency\ninv-a,party-a,150,EUR\ninv-b,party-a,250,EUR\ninv-c,missing,80,EUR\n")
        compared = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        values = {row["currency"]: row for row in compared["metric_changes"]}
        self.assertEqual(values["USD"]["before"], "400")
        self.assertIsNone(values["USD"]["after"])
        self.assertIsNone(values["USD"]["delta"])
        self.assertIsNone(values["EUR"]["before"])
        self.assertEqual(values["EUR"]["after"], "400")
        self.assertIsNone(values["EUR"]["delta"])

    def test_failed_refresh_retains_history_but_refuses_current_comparison(self):
        saved = self.save()
        self.fixture.fixture.billing.write_bytes(b"invoice_id,party_ref,amount,currency\ninv-a,party-a,1,\xff\xfe\n")
        with self.assertRaises(Exception):
            service.sync(self.w, "billing", bootstrap_context=False)
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)
        self.assertEqual(reviews.list_reviews(self.w, self.profile, self.customer)[0]["review_id"], saved["review_id"])
        with self.assertRaises(ValueError):
            reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        with self.assertRaises(ValueError):
            self.save()

    def test_definition_change_suppresses_delta_but_keeps_original_review(self):
        saved = self.save()
        definitions = fixtures.definitions()
        definitions[1]["filters"][0]["value"] = "closed"
        proposed = contexts.propose_context(self.w, self.profile, self.snapshot, definitions)
        reviewed = contexts.approve_context(self.w, proposed["proposal_id"], proposed["approval_sha256"], "synthetic-definition-reviewer")
        compared = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.assertEqual(compared["status"], "review_basis_changed")
        self.assertEqual(compared["changed_basis"], ["definition_sha256"])
        self.assertEqual(compared["metric_changes"], [{"definition_name": "open_cases", "unit": "records", "currency": None,
                                                       "before": "2", "after": "0", "delta": None}])
        self.assertEqual(compared["record_changes"], {"added": [], "removed": [], "changed": []})
        self.assertEqual(compared["context_id"], reviewed["context_id"])
        self.assertEqual(reviews.load_review(self.w, saved["review_id"])["context_id"], self.approved["context_id"])

    def test_mapping_change_suppresses_numerical_delta_after_re_review(self):
        saved = self.save()
        changed = copy.deepcopy(self.fixture.fixture.profile)
        changed["description"] = "Explicit revised source mapping review"
        candidate = profiles.propose_profile(self.w, changed, self.snapshot)
        profiles.approve_profile(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-mapping-reviewer")
        self.refresh(billing=self.fixture.fixture.billing.read_text().replace(",150,", ",175,"))
        with self.assertRaises(ValueError):
            reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.fixture.snapshot = self.snapshot
        self.fixture.approve()
        compared = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.assertEqual(compared["changed_basis"], ["profile_approval"])
        self.assertEqual(compared["metric_changes"], [{"definition_name": "invoice_value", "unit": "currency", "currency": "USD",
                                                       "before": "400", "after": "425", "delta": None}])
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_identity_change_suppresses_delta_and_preserves_prior_scope(self):
        saved = self.save()
        candidate = identity.propose_links(self.w, [{"connection": "billing", "entity": "party", "source_id": "new-alias",
                                                    "customer_id": self.customer, "basis": "Explicit synthetic alias review"}])
        identity.approve_links(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-identity-reviewer")
        self.refresh(billing=self.fixture.fixture.billing.read_text().replace(",150,", ",175,"))
        compared = reviews.compare_review(self.w, saved["review_id"], self.snapshot)
        self.assertEqual(compared["changed_basis"], ["identity_sha256"])
        self.assertEqual(compared["metric_changes"][0]["after"], "425")
        self.assertIsNone(compared["metric_changes"][0]["delta"])
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_save_never_accepts_caller_authored_brief_or_stale_source_rows(self):
        for keyword in ("brief", "answer", "records"):
            with self.assertRaises(TypeError):
                reviews.save_review(self.w, self.profile, self.snapshot, self.customer, **{keyword: {"fabricated": True}})
        self.assertEqual(reviews.list_reviews(self.w, self.profile, self.customer), [])
        with self.w._connect(writable=True) as connection:
            connection.execute("UPDATE billing.records SET amount=999 WHERE invoice_id='inv-a'")
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(reviews.list_reviews(self.w, self.profile, self.customer), [])

    def test_cross_workspace_review_ids_and_copied_payloads_are_rejected(self):
        saved = self.save()
        other = self.make_fixture()
        other.approve()
        other_customer = other.fixture.customer["id"]
        reviews.save_review(other.w, other.profile, other.snapshot, other_customer)
        with self.assertRaises(ValueError):
            reviews.load_review(other.w, saved["review_id"])
        with other.w._connect(writable=True) as connection:
            connection.execute("INSERT INTO _contextbase.customer360_saved_reviews VALUES (?,?,?,?,?)",
                               [saved["review_id"], saved["profile"], saved["customer_id"], saved["created_at"], canonical(saved)])
        with self.assertRaises(ValueError):
            reviews.load_review(other.w, saved["review_id"])
        with self.assertRaises(ValueError):
            reviews.compare_review(other.w, saved["review_id"], other.snapshot)
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_review_and_all_retained_dependency_tampering_are_rejected(self):
        saved = self.save()
        targets = [
            ("customer360_saved_reviews", "review_id", saved["review_id"], lambda v: v["brief"].update(rendered="Invented saved answer")),
            ("customer360_materializations", "materialization_id", saved["materialization_id"], lambda v: v.update(row_count=999)),
            ("customer360_context_approvals", "context_id", saved["context_id"], lambda v: v["definitions"][0].update(description="Changed retained meaning")),
            ("customer360_snapshots", "snapshot_id", saved["snapshot_id"], lambda v: v["coverage"].update(total_rows=999)),
        ]
        # The profile table is indexed by name/version, with its approval in JSON.
        with self.w._connect() as connection:
            retained_profile = connection.execute("SELECT payload FROM _contextbase.customer360_profile_approvals WHERE name=?", [self.profile]).fetchone()[0]
            record_key, retained_row = connection.execute("SELECT record_key,payload FROM _contextbase.customer360_record_history WHERE materialization_id=? LIMIT 1", [saved["materialization_id"]]).fetchone()
        for table, key, identifier, mutate in targets:
            with self.subTest(table=table):
                with self.w._connect(writable=True) as connection:
                    original = connection.execute(f"SELECT payload FROM _contextbase.{table} WHERE {key}=?", [identifier]).fetchone()[0]
                    changed = json.loads(original)
                    mutate(changed)
                    connection.execute(f"UPDATE _contextbase.{table} SET payload=? WHERE {key}=?", [json.dumps(changed), identifier])
                try:
                    with self.assertRaises(ValueError):
                        reviews.load_review(self.w, saved["review_id"])
                finally:
                    with self.w._connect(writable=True) as connection:
                        connection.execute(f"UPDATE _contextbase.{table} SET payload=? WHERE {key}=?", [original, identifier])
        for table, original, where, parameters, mutate in (
                ("customer360_profile_approvals", retained_profile, "name=?", [self.profile], lambda v: v["candidate"]["profile"].update(description="Altered mapping")),
                ("customer360_record_history", retained_row, "materialization_id=? AND record_key=?", [saved["materialization_id"], record_key], lambda v: v.update(source_id="altered-record"))):
            with self.subTest(table=table):
                value = json.loads(original)
                mutate(value)
                with self.w._connect(writable=True) as connection:
                    connection.execute(f"UPDATE _contextbase.{table} SET payload=? WHERE {where}", [json.dumps(value), *parameters])
                try:
                    with self.assertRaises(ValueError):
                        reviews.load_review(self.w, saved["review_id"])
                finally:
                    with self.w._connect(writable=True) as connection:
                        connection.execute(f"UPDATE _contextbase.{table} SET payload=? WHERE {where}", [original, *parameters])
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_resealed_record_type_drift_cannot_equal_retained_integer_history(self):
        saved = self.save()
        changed = copy.deepcopy(saved)
        record = next(row for row in changed["records"] if row["source_id"] == "inv-a")
        record["fields"]["amount"] = 150.0  # Python equality says True; canonical evidence must differ.
        changed["review_id"] = sha256({key: value for key, value in changed.items() if key != "review_id"})
        with self.w._connect(writable=True) as connection:
            connection.execute("INSERT INTO _contextbase.customer360_saved_reviews VALUES (?,?,?,?,?)",
                               [changed["review_id"], changed["profile"], changed["customer_id"], changed["created_at"], canonical(changed)])
        with self.assertRaises(ValueError):
            reviews.load_review(self.w, changed["review_id"])
        self.assertEqual(reviews.load_review(self.w, saved["review_id"]), saved)

    def test_index_tampering_does_not_silently_reorder_or_rescope_saved_review(self):
        saved = self.save()
        with self.w._connect(writable=True) as connection:
            connection.execute("UPDATE _contextbase.customer360_saved_reviews SET created_at=? WHERE review_id=?",
                               ["2099-01-01T00:00:00+00:00", saved["review_id"]])
        with self.assertRaises(ValueError):
            reviews.load_review(self.w, saved["review_id"])
        with self.assertRaises(ValueError):
            reviews.list_reviews(self.w, self.profile, self.customer)

    def test_failed_save_rolls_back_the_shared_source_transaction(self):
        first = self.save()
        original = answers._brief
        def fail(scope, fields, limit):
            original(scope, fields, limit)
            scope["conn"].execute("CREATE TABLE customer360.synthetic_rollback_marker (value INTEGER)")
            raise ValueError("Synthetic failure after evidence validation")
        with patch.object(answers, "_brief", side_effect=fail), self.assertRaises(ValueError):
            self.save()
        with self.w._connect() as connection:
            exists = connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='customer360' AND table_name='synthetic_rollback_marker'").fetchone()
        self.assertIsNone(exists)
        self.assertEqual([row["review_id"] for row in reviews.list_reviews(self.w, self.profile, self.customer)], [first["review_id"]])


if __name__ == "__main__":
    unittest.main()
