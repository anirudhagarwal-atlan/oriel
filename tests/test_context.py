"""Changed-data tests for business semantics and portable context evidence."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from contextbase.context import (
    approve_metric, bootstrap, demo_context, export_bundle, install_demo_context,
    load_context, propose_metric, run_metric,
)
from contextbase.workspace import Workspace


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = Workspace(self.root / "workspace")
        with self.w._connect(writable=True) as db:
            for schema in ("crm", "billing", "product", "support"):
                db.execute(f"CREATE SCHEMA {schema}")
            db.execute("CREATE TABLE crm.accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR, health VARCHAR)")
            db.execute("CREATE TABLE crm.opportunities (opportunity_id VARCHAR, account_id VARCHAR, stage VARCHAR, arr DOUBLE, tcv DOUBLE, close_date DATE)")
            db.execute("CREATE TABLE billing.subscriptions (subscription_id VARCHAR, account_id VARCHAR, status VARCHAR, mrr DOUBLE)")
            db.execute("CREATE TABLE product.usage (account_id VARCHAR, period VARCHAR, active_users BIGINT)")
            db.execute("CREATE TABLE support.tickets (ticket_id VARCHAR, account_id VARCHAR, status VARCHAR, priority VARCHAR)")
            db.execute("INSERT INTO crm.accounts VALUES ('customer_1','Example account','small','red'), ('customer_2','Another example','large','green')")
            db.execute("INSERT INTO crm.opportunities VALUES "
                       "('opp_1','customer_1','S2',100,900,'2026-08-01'),"
                       "('opp_2','customer_1','S1',800,900,'2026-09-01'),"
                       "('opp_3','customer_2','S3',200,900,'2026-10-31'),"
                       "('opp_4','customer_2','S4',400,900,'2026-11-01'),"
                       "('opp_5','customer_2','S5',1600,900,'2026-09-01')")
            db.execute("INSERT INTO billing.subscriptions VALUES "
                       "('sub_1','customer_1','active',10),('sub_2','customer_1','active',20),"
                       "('sub_3','customer_2','active',40),('sub_4','customer_2','inactive',100),"
                       "('sub_5','customer_2','active',0)")
            db.execute("INSERT INTO product.usage VALUES "
                       "('customer_1','previous',50),('customer_1','previous',50),"
                       "('customer_1','current',35),('customer_1','current',35),"
                       "('customer_2','previous',100),('customer_2','current',90)")
            db.execute("INSERT INTO support.tickets VALUES "
                       "('ticket_1','customer_1','open','high'),('ticket_2','customer_1','open','urgent'),"
                       "('ticket_3','customer_2','open','high')")

    def scalar(self, name):
        return run_metric(self.w, name)["rows"][0][name]

    def test_bootstrap_is_a_draft_and_profiles_cardinality_without_rows(self):
        draft = bootstrap(self.w)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(len(draft["schema"]), 5)
        self.assertEqual(draft["metrics"], [])
        self.assertTrue(draft["unresolved_questions"])
        joins = [j for j in draft["candidate_joins"] if j["from"] == "billing.subscriptions" and j["to"] == "crm.accounts"]
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0]["observed_cardinality"], "many_to_one")
        self.assertTrue(joins[0]["evidence"]["complete_non_null_match"])
        self.assertNotIn("Example account", json.dumps(draft))
        self.assertFalse((self.w.root / "context/approved.json").exists())
        with self.assertRaisesRegex(ValueError, "No approved context"):
            run_metric(self.w, "active_arr")

    def test_join_evidence_rejects_duplicate_parent_and_reports_orphans(self):
        with self.w._connect(writable=True) as db:
            db.execute("INSERT INTO billing.subscriptions VALUES ('orphan','missing','active',1)")
        draft = bootstrap(self.w)
        join = next(j for j in draft["candidate_joins"] if j["from"] == "billing.subscriptions")
        self.assertEqual(join["evidence"]["unmatched_rows"], 1)
        self.assertFalse(join["evidence"]["complete_non_null_match"])
        with self.w._connect(writable=True) as db:
            db.execute("INSERT INTO crm.accounts VALUES ('customer_1','Duplicate','small','red')")
        self.assertFalse(bootstrap(self.w)["candidate_joins"])

    def test_curated_policy_handles_fiscal_boundary_arr_and_fanout(self):
        installed = install_demo_context(self.w)
        self.assertTrue(installed["synthetic"])
        self.assertEqual(self.scalar("eligible_pipeline"), 300)
        self.assertEqual(self.scalar("active_arr"), 840)
        self.assertEqual(self.scalar("active_accounts"), 2)
        # Two subscriptions, two rows per usage period, two severe tickets:
        # only the account's 30 MRR is annualized, with no join fanout.
        self.assertEqual(self.scalar("at_risk_arr"), 360)
        self.assertEqual(load_context(self.w)["status"], "approved")
        with self.assertRaisesRegex(ValueError, "No approved metric"):
            run_metric(self.w, "future_bookings")

    def test_metrics_reexecute_after_source_data_changes(self):
        install_demo_context(self.w)
        first = run_metric(self.w, "active_arr")
        with self.w._connect(writable=True) as db:
            db.execute("UPDATE billing.subscriptions SET mrr=50 WHERE subscription_id='sub_1'")
            db.execute("UPDATE crm.opportunities SET arr=150 WHERE opportunity_id='opp_1'")
        second = run_metric(self.w, "active_arr")
        self.assertEqual(self.scalar("at_risk_arr"), 840)
        self.assertEqual(self.scalar("eligible_pipeline"), 350)
        self.assertNotEqual(first["execution_evidence"]["result_sha256"], second["execution_evidence"]["result_sha256"])

    def test_missing_usage_is_unknown_and_zero_observed_usage_is_valid(self):
        install_demo_context(self.w)
        with self.w._connect(writable=True) as db:
            db.execute("DELETE FROM product.usage WHERE account_id='customer_1' AND period='current'")
        self.assertEqual(self.scalar("at_risk_arr"), 0)
        with self.w._connect(writable=True) as db:
            db.execute("INSERT INTO product.usage VALUES ('customer_1','current',NULL)")
        self.assertEqual(self.scalar("at_risk_arr"), 0)
        with self.w._connect(writable=True) as db:
            db.execute("INSERT INTO product.usage VALUES ('customer_1','current',0)")
        self.assertEqual(self.scalar("at_risk_arr"), 360)

    def test_bundle_is_portable_hashed_and_preserves_native_metric_semantics(self):
        install_demo_context(self.w)
        destination = self.root / "bundle"
        manifest = export_bundle(self.w, destination)
        self.assertFalse(manifest["contains_source_rows"])
        self.assertEqual(manifest["network_upload"], "not_performed")
        for entry in manifest["files"]:
            raw = (destination / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
            self.assertEqual(len(raw), entry["bytes"])
        index = json.loads((destination / "skill_index.json").read_text())
        self.assertEqual(len(index["models"]), 5)
        self.assertEqual(len(index["metrics"]), 4)
        self.assertEqual(len(index["metric_versions"]), 4)
        handoff = json.loads((destination / index["registry_handoff"]).read_text())
        self.assertFalse(handoff["execution_authorized"])
        self.assertEqual(handoff["network_upload"], "not_performed")
        pipeline = json.loads((destination / "metrics/eligible_pipeline.json").read_text())
        self.assertEqual(pipeline["source_id"], "contextbase.metrics/eligible_pipeline")
        self.assertEqual(pipeline["definition"]["spec_version"], "1.0-draft.1")
        self.assertEqual(pipeline["definition"]["status"], "candidate")
        self.assertTrue(pipeline["definition"]["mandatory_filters"])
        for artifact in handoff["artifacts"]:
            create = json.loads((destination / artifact["create_body"]).read_text())
            append = json.loads((destination / artifact["append_version_body"]).read_text())
            fields = {"definition", "description", "source_uri", "source_version", "source_metadata"}
            self.assertEqual(set(append), fields)
            self.assertEqual(append, {key: create[key] for key in fields})
            self.assertEqual(append["definition"]["status"], "candidate")
            self.assertEqual(artifact["source_id"], create["source_id"])
            self.assertEqual(artifact["workspace_id"], create["workspace_id"])
            self.assertIsNone(artifact["artifact_id"])
        for path in index["metrics"]:
            definition = json.loads((destination / path).read_text())["definition"]
            source = definition["source"]
            relation = source.get("table") or f"({source['sql']})"
            filters = definition.get("mandatory_filters", [])
            where = " WHERE " + " AND ".join(filters) if filters else ""
            sql = f"SELECT {definition['formula']} AS answer FROM {relation} s{where}"
            actual = self.w.query(sql)["rows"][0]["answer"]
            expected = self.scalar(definition["output"]["alias"])
            self.assertEqual(actual, expected)
        again = export_bundle(self.w, self.root / "second-bundle")
        self.assertEqual(manifest, again)
        with self.assertRaisesRegex(ValueError, "new or empty"):
            export_bundle(self.w, destination)

    def test_data_refresh_preserves_exported_definition_until_reviewed_revision(self):
        candidate = self.proposal()
        approve_metric(self.w, "custom_arr", candidate["approval_sha256"], "reviewer")
        before = run_metric(self.w, "custom_arr")
        first = self.root / "before-refresh"
        export_bundle(self.w, first)
        with self.w._connect(writable=True) as db:
            db.execute("UPDATE billing.subscriptions SET mrr=50 WHERE subscription_id='sub_1'")
        after = run_metric(self.w, "custom_arr")
        second = self.root / "after-refresh"
        export_bundle(self.w, second)
        self.assertNotEqual(before["execution_evidence"]["result_sha256"], after["execution_evidence"]["result_sha256"])
        old = json.loads((first / "metric_versions/custom_arr.json").read_text())
        new = json.loads((second / "metric_versions/custom_arr.json").read_text())
        # Native admission fingerprints extensions as execution inputs. A fresh
        # query receipt must not rewrite them under the same semantic version.
        self.assertEqual(old["definition"], new["definition"])
        self.assertEqual(old["definition"]["version"], new["definition"]["version"])

    def test_exports_reject_path_traversal_and_context_symlinks(self):
        install_demo_context(self.w)
        path = self.w.root / "context/approved.json"
        context = load_context(self.w)
        context["metrics"][0]["name"] = "../../escape"
        path.write_text(json.dumps(context))
        with self.assertRaisesRegex(ValueError, "Metric export names"):
            export_bundle(self.w, self.root / "bundle")
        path.unlink()
        path.symlink_to(self.root / "elsewhere.json")
        with self.assertRaisesRegex(ValueError, "regular file"):
            load_context(self.w)

    def test_embedded_demo_template_is_not_mutated(self):
        original = deepcopy(demo_context())
        install_demo_context(self.w)
        self.assertEqual(original, demo_context())

    def proposal(self, name="custom_arr", **overrides):
        options = {
            "sql": "SELECT SUM(mrr)*12 AS custom_arr FROM billing.subscriptions WHERE status='active' AND mrr>0;",
            "description": "Paid active annual recurring revenue.",
            "unit": "USD/year", "grain": "One subscription.",
            "rules": ["Only active subscriptions with positive MRR; annualize by 12."],
            "owner": "analytics_owner",
        }
        options.update(overrides)
        return propose_metric(self.w, name, **options)

    def test_custom_metric_requires_explicit_business_review(self):
        proposal = propose_metric(self.w, "custom_arr", "SELECT 1 AS custom_arr", "")
        self.assertEqual(proposal["status"], "candidate")
        self.assertTrue(proposal["unresolved_questions"])
        self.assertFalse((self.w.root / "context/approved.json").exists())
        with self.assertRaisesRegex(ValueError, "unresolved business"):
            approve_metric(self.w, "custom_arr", proposal["approval_sha256"], "reviewer")
        proposal = self.proposal()
        with self.assertRaisesRegex(ValueError, "current SHA-256"):
            approve_metric(self.w, "custom_arr", "0" * 64, "reviewer")
        with self.assertRaisesRegex(ValueError, "reviewer's"):
            approve_metric(self.w, "custom_arr", proposal["approval_sha256"], "")
        approved = approve_metric(self.w, "custom_arr", proposal["approval_sha256"], "finance_reviewer")
        self.assertEqual(approved["version"], 1)
        self.assertEqual(approved["approval_kind"], "local_operator_review")
        self.assertEqual(self.scalar("custom_arr"), 840)
        with self.assertRaisesRegex(ValueError, "Approved context changed"):
            approve_metric(self.w, "custom_arr", proposal["approval_sha256"], "finance_reviewer")

    def test_custom_metric_checks_scalar_paths_and_tampered_proposals(self):
        for sql in ("SELECT account_id FROM crm.accounts", "SELECT 1 AS a,2 AS b", "SELECT NULL AS a", "SELECT 'data' AS a", "SELECT TRUE AS a", "SELECT CAST('Infinity' AS DOUBLE) AS a"):
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "numeric|finite"):
                self.proposal(sql=sql)
        with self.assertRaisesRegex(ValueError, "Metric names"):
            self.proposal(name="../../escape")
        candidate = self.proposal()
        path = self.w.root / "context/candidates/custom_arr.json"
        tampered = json.loads(path.read_text())
        tampered["rules"] = ["Changed after the review."]
        path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "proposal changed"):
            approve_metric(self.w, "custom_arr", candidate["approval_sha256"], "reviewer")

    def test_custom_metric_rejects_stale_results_and_schema(self):
        proposal = self.proposal()
        with self.w._connect(writable=True) as db:
            db.execute("UPDATE billing.subscriptions SET mrr=50 WHERE subscription_id='sub_1'")
        with self.assertRaisesRegex(ValueError, "witnessed metric result changed"):
            approve_metric(self.w, "custom_arr", proposal["approval_sha256"], "reviewer")
        fresh = self.proposal()
        with self.w._connect(writable=True) as db:
            db.execute("ALTER TABLE crm.accounts ADD COLUMN reviewed BOOLEAN")
        with self.assertRaisesRegex(ValueError, "Schema or row counts changed"):
            approve_metric(self.w, "custom_arr", fresh["approval_sha256"], "reviewer")

    def test_two_custom_revisions_preserve_other_metrics_and_history(self):
        first = self.proposal()
        approve_metric(self.w, "custom_arr", first["approval_sha256"], "reviewer")
        other = self.proposal(name="other_arr", sql="SELECT 2 AS other_arr")
        approve_metric(self.w, "other_arr", other["approval_sha256"], "reviewer")
        second = self.proposal(sql="SELECT SUM(mrr)*12 AS custom_arr FROM billing.subscriptions WHERE status='active' AND mrr>=20")
        self.assertEqual(second["version"], 2)
        updated = approve_metric(self.w, "custom_arr", second["approval_sha256"], "reviewer_two")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.scalar("custom_arr"), 720)
        self.assertEqual(self.scalar("other_arr"), 2)
        self.assertEqual(len(list((self.w.root / "context/history").glob("*.json"))), 2)
        dest = self.root / "custom-bundle"
        export_bundle(self.w, dest)
        payload = json.loads((dest / "metrics/custom_arr.json").read_text())
        definition = payload["definition"]
        self.assertEqual(definition["status"], "approved")
        self.assertEqual(definition["owner"], "analytics_owner")
        self.assertEqual(definition["grain"], "One subscription.")
        self.assertEqual(definition["output"]["unit"], "USD/year")
        self.assertTrue(definition["extensions"]["contextbase"]["rules"])
        sql = f"SELECT {definition['formula']} AS result FROM ({definition['source']['sql']}) s"
        self.assertEqual(self.w.query(sql)["rows"][0]["result"], 720)
        self.assertEqual(definition["examples"][0]["result"]["rows"], [])
        append = json.loads((dest / "metric_versions/custom_arr.json").read_text())
        self.assertEqual(append["definition"], definition)
        self.assertEqual(append["definition"]["version"], 2)
        self.assertEqual(append["source_version"], payload["source_version"])
        self.assertEqual(append["source_metadata"], payload["source_metadata"])

    def test_candidate_based_on_old_context_cannot_clobber_new_approval(self):
        earlier = self.proposal()
        another = self.proposal(name="another_arr", sql="SELECT 7 AS another_arr")
        approve_metric(self.w, "another_arr", another["approval_sha256"], "reviewer")
        with self.assertRaisesRegex(ValueError, "Approved context changed"):
            approve_metric(self.w, "custom_arr", earlier["approval_sha256"], "reviewer")
        self.assertEqual(self.scalar("another_arr"), 7)


if __name__ == "__main__":
    unittest.main()
