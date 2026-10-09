"""Refresh acceptance using actual local dlt SQLite publications and an oracle.

All source mutations affect authored local fixtures. No vendor, model, patched
extraction, mocked answer, or historical snapshot relabeled as current.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from importlib import util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, profiles, snapshots
from contextbase.customer360.contracts import canonical, sha256
from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[2] / "examples" / "customer-360"


def _module(name):
    spec = util.spec_from_file_location("refresh_acceptance_" + name, ROOT / (name + ".py"))
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime, review, oracle = (_module(name) for name in ("runtime", "review", "oracle"))


class RefreshAndHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # A missing real dependency fails this gate.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.runs = {}
        for profile in ("salesforce-gong", "billing-support"):
            original = Workspace(cls.directory / (profile + "-initial"))
            extraction = runtime.load_profile(original, profile)
            reviewed = review.setup_review(original, extraction, approve_synthetic_fixture=True)
            initial = cls.observe(original, profile, "initial", extraction, reviewed)
            refreshed = Workspace(cls.directory / (profile + "-refreshed"))
            shutil.copytree(original.root, refreshed.root)
            next_extraction = runtime.load_profile(refreshed, profile, "refreshed")
            next_review = review.setup_review(refreshed, next_extraction, approve_synthetic_fixture=True)
            cls.runs[profile] = {"initial": initial,
                                 "refreshed": cls.observe(refreshed, profile, "refreshed", next_extraction, next_review)}

    @staticmethod
    def observe(workspace, profile, phase, extraction, reviewed):
        with profiles.readable_profile(workspace, reviewed["profile"], reviewed["snapshot_id"]) as scope:
            records = scope[-1]["records"]
        fields = ("text", "updates") if profile == "billing-support" else ("text",)
        return {"workspace": workspace, "extraction": extraction, "review": reviewed,
                "snapshot": snapshots.load_snapshot(workspace, reviewed["snapshot_id"]),
                "context": contexts.current_context(workspace, reviewed["profile"], reviewed["snapshot_id"]),
                "records": records, "expected": oracle.evaluate(profile, phase),
                "briefs": {alias: answers.brief(workspace, reviewed["profile"], reviewed["snapshot_id"], customer,
                                                 fields=fields, quote_limit=20)
                           for alias, customer in reviewed["customer_ids"].items()}}

    def copy(self, profile, phase, suffix):
        source = self.runs[profile][phase]["workspace"]
        workspace = Workspace(self.directory / (self._testMethodName + "-" + suffix))
        shutil.copytree(source.root, workspace.root)
        return workspace

    @staticmethod
    def database(workspace, source):
        return workspace.root / "customer360-fixture-runtime" / (source + ".sqlite")

    @classmethod
    def sync(cls, workspace, source):
        return runtime._sync(workspace, "fixture_" + source,
                             "ORIEL_CUSTOMER360_FIXTURE_" + source.upper() + "_SQLITE_URL", cls.database(workspace, source))

    @staticmethod
    def history(workspace, reviewed):
        with workspace._connect() as connection:
            receipt = json.loads(connection.execute("SELECT payload FROM _contextbase.customer360_materializations "
                                                    "WHERE materialization_id=?", [reviewed["materialization_id"]]).fetchone()[0])
            rows = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM _contextbase.customer360_record_history WHERE materialization_id=? ORDER BY record_key",
                [reviewed["materialization_id"]]).fetchall()]
        return receipt, rows

    def assert_history(self, workspace, before):
        receipt, rows = self.history(workspace, before["review"])
        self.assertEqual(receipt["records_sha256"], sha256(rows))
        self.assertEqual(receipt["receipt_sha256"], sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"}))
        self.assertEqual(receipt["snapshot_id"], before["review"]["snapshot_id"])
        self.assertEqual(rows, before["records"])
        self.assertEqual(snapshots.load_snapshot(workspace, before["review"]["snapshot_id"]), before["snapshot"])

    def assert_briefs(self, briefs, expected):
        metric = "open_pipeline" if expected["profile"] == "salesforce-gong" else "outstanding_invoices"
        for customer in expected["customers"]:
            result = next(value["result"] for value in briefs[customer["customer_id"]]["metrics"] if value["result"]["definition_name"] == metric)
            self.assertEqual({value["currency"]: Decimal(value["value"]) for value in result["values"]},
                             {currency: Decimal(str(value)) for currency, value in customer["amounts_by_currency"].items()})
            if expected["profile"] == "billing-support":
                cases = next(value["result"] for value in briefs[customer["customer_id"]]["metrics"] if value["result"]["definition_name"] == "open_support_cases")
                self.assertEqual(cases["values"], [{"currency": None, "value": customer["open_conversations"]}])
        for citation in expected["attributed_text"]:
            candidates = [quote for quote in briefs[citation["customer_id"]]["quotes"] if quote["source_id"] == citation["record_id"]]
            self.assertTrue(any(citation["text"] in quote["text"] for quote in candidates))

    def test_changed_event_updates_brief_and_preserves_definition(self):
        for profile, phases in self.runs.items():
            with self.subTest(profile=profile):
                before, after = phases["initial"], phases["refreshed"]
                self.assert_briefs(before["briefs"], before["expected"])
                self.assert_briefs(after["briefs"], after["expected"])
                self.assertNotEqual(before["expected"]["totals_by_currency"], after["expected"]["totals_by_currency"])
                for field in ("definition_sha256", "context_id", "profile_approval", "customer_ids"):
                    self.assertEqual(before["review"][field], after["review"][field])
                self.assertEqual(before["context"]["definitions"], after["context"]["definitions"])
                self.assertNotEqual(before["review"]["snapshot_id"], after["review"]["snapshot_id"])
                self.assertNotEqual(before["briefs"]["customer_a"]["answer_id"], after["briefs"]["customer_a"]["answer_id"])
                self.assert_history(after["workspace"], before)
                with self.assertRaisesRegex(ValueError, "no longer matches"):
                    answers.brief(after["workspace"], before["review"]["profile"], before["review"]["snapshot_id"], before["review"]["customer_ids"]["customer_a"])

    def test_repeated_refresh_does_not_duplicate_evidence(self):
        for profile, phases in self.runs.items():
            with self.subTest(profile=profile):
                before = phases["refreshed"]
                workspace = self.copy(profile, "refreshed", profile)
                extraction = runtime.load_profile(workspace, profile, "refreshed")
                reviewed = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
                after = self.observe(workspace, profile, "refreshed", extraction, reviewed)
                self.assert_briefs(after["briefs"], before["expected"])
                self.assertNotEqual(reviewed["snapshot_id"], before["review"]["snapshot_id"])
                self.assertEqual({row["evidence_id"] for row in after["records"]}, {row["evidence_id"] for row in before["records"]})
                self.assertEqual(len(after["records"]), len({row["record_key"] for row in after["records"]}))
                self.assertEqual(len(after["records"]), len(before["records"]))
                with workspace._connect() as connection:
                    old_count = connection.execute("SELECT COUNT(*) FROM _contextbase.customer360_record_history").fetchone()[0]
                first = profiles.materialize(workspace, reviewed["profile"], reviewed["snapshot_id"])
                second = profiles.materialize(workspace, reviewed["profile"], reviewed["snapshot_id"])
                self.assertEqual(first, second)
                with workspace._connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM _contextbase.customer360_record_history").fetchone()[0], old_count)
                    self.assertEqual(connection.execute("SELECT COUNT(*),COUNT(DISTINCT evidence_id) FROM customer360.records").fetchone(),
                                     (len(after["records"]), len(after["records"])))
                self.assert_history(workspace, before)

    def test_failed_refresh_preserves_snapshot_and_exposes_staleness(self):
        from contextbase.customer360 import reviews
        before = self.runs["billing-support"]["initial"]
        workspace = self.copy("billing-support", "initial", "failed")
        saved = reviews.save_review(workspace, before["review"]["profile"], before["review"]["snapshot_id"],
                                    before["review"]["customer_ids"]["customer_a"], fields=("text", "updates"), quote_limit=20)
        raw_before = workspace.query("SELECT * FROM fixture_stripe.invoices ORDER BY fixture_record_id")["rows"]
        last = next(source for source in service.configured(workspace)["sources"] if source["name"] == "fixture_stripe")["last_snapshot"]
        with closing(sqlite3.connect(self.database(workspace, "stripe"))) as database, database:
            database.execute("ALTER TABLE invoices RENAME TO unavailable_invoices")
        with self.assertRaises(RuntimeError):
            self.sync(workspace, "stripe")
        source = next(source for source in service.configured(workspace)["sources"] if source["name"] == "fixture_stripe")
        self.assertEqual(source["last_sync"]["status"], "failed")
        self.assertNotEqual(source["last_sync"]["attempt"], last["attempt"])
        self.assertEqual(source["last_snapshot"], last)
        self.assertEqual(workspace.query("SELECT * FROM fixture_stripe.invoices ORDER BY fixture_record_id")["rows"], raw_before)
        self.assertEqual(reviews.load_review(Workspace(workspace.root), saved["review_id"]), saved)
        for operation in (
                lambda: snapshots.capture_snapshot(workspace, before["extraction"]["source_names"]),
                lambda: contexts.current_context(workspace, before["review"]["profile"], before["review"]["snapshot_id"]),
                lambda: answers.brief(workspace, before["review"]["profile"], before["review"]["snapshot_id"], before["review"]["customer_ids"]["customer_a"]),
                lambda: reviews.compare_review(workspace, saved["review_id"], before["review"]["snapshot_id"])):
            with self.assertRaisesRegex(ValueError, "failed latest sync"):
                operation()
        self.assert_history(workspace, before)

    def test_current_question_refused_when_required_coverage_is_stale(self):
        before = self.runs["billing-support"]["initial"]
        workspace = self.copy("billing-support", "initial", "freshness")
        sources, reviewed = before["extraction"]["source_names"], before["review"]
        # Real sequential extraction creates a nonzero publication skew.
        self.assertGreater(before["snapshot"]["freshness"]["publication_skew_seconds"], 0)
        with self.assertRaisesRegex(ValueError, "skew"):
            snapshots.capture_snapshot(workspace, sources, max_skew_seconds=0)
        oldest = datetime.fromisoformat(before["snapshot"]["freshness"]["oldest_extraction_started_at"])
        age_limit = (datetime.now(timezone.utc) - oldest).total_seconds() + 1.5
        bounded = snapshots.capture_snapshot(workspace, sources, max_age_seconds=age_limit)
        profiles.materialize(workspace, reviewed["profile"], bounded["snapshot_id"])
        current = answers.brief(workspace, reviewed["profile"], bounded["snapshot_id"], reviewed["customer_ids"]["customer_a"])
        self.assertEqual(current["definition_sha256"], reviewed["definition_sha256"])
        remaining = age_limit - (datetime.now(timezone.utc) - oldest).total_seconds()
        time.sleep(max(0, remaining) + 0.03)
        with self.assertRaisesRegex(ValueError, "stale"):
            answers.brief(workspace, reviewed["profile"], bounded["snapshot_id"], reviewed["customer_ids"]["customer_a"])
        with self.assertRaisesRegex(ValueError, "stale"):
            snapshots.capture_snapshot(workspace, sources, max_age_seconds=age_limit)
        self.assertEqual(snapshots.load_snapshot(workspace, bounded["snapshot_id"]), bounded)
        self.assert_history(workspace, before)

    def test_schema_drift_invalidates_affected_context(self):
        before = self.runs["billing-support"]["initial"]
        workspace = self.copy("billing-support", "initial", "schema")
        with closing(sqlite3.connect(self.database(workspace, "stripe"))) as database, database:
            columns = [row[1] for row in database.execute("PRAGMA table_info(invoices)")]
            selections = [('CAST("amount_remaining" AS TEXT) AS "amount_remaining"' if name == "amount_remaining" else runtime._quoted(name)) for name in columns]
            database.execute("ALTER TABLE invoices RENAME TO old_invoice_shape")
            database.execute("CREATE TABLE invoices AS SELECT " + ",".join(selections) + " FROM old_invoice_shape")
        self.assertEqual(self.sync(workspace, "stripe")["status"], "synced")
        snapshot = snapshots.capture_snapshot(workspace, before["extraction"]["source_names"])
        tables = [table for source in snapshot["sources"] for table in source["tables"]]
        table = next(table for table in tables if table["table"] == "fixture_stripe.invoices")
        self.assertEqual(next(column["type"] for column in table["columns"] if column["name"] == "amount_remaining"), "VARCHAR")
        with self.assertRaisesRegex(ValueError, "Mapped column types changed"):
            profiles.materialize(workspace, before["review"]["profile"], snapshot["snapshot_id"])
        for snapshot_id in (snapshot["snapshot_id"], before["review"]["snapshot_id"]):
            with self.assertRaises(ValueError):
                answers.brief(workspace, before["review"]["profile"], snapshot_id, before["review"]["customer_ids"]["customer_a"])
            with self.assertRaises(ValueError):
                contexts.current_context(workspace, before["review"]["profile"], snapshot_id)
        self.assertEqual(profiles.load_profile(workspace, before["review"]["profile"])["approval_sha256"], before["review"]["profile_approval"])
        self.assert_history(workspace, before)

    def test_deleted_or_retracted_evidence_updates_current_answer(self):
        for profile in ("salesforce-gong", "billing-support"):
            with self.subTest(profile=profile):
                before = self.runs[profile]["initial"]
                workspace = self.copy(profile, "initial", profile)
                fixture = self.directory / (self._testMethodName + "-oracle-" + profile)
                shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns("__pycache__"))
                reviewed = before["review"]
                customer = reviewed["customer_ids"]["customer_a"]
                original_quote = before["briefs"]["customer_a"]["quotes"][0]
                source, table = ("gong", "call_transcripts") if profile == "salesforce-gong" else ("intercom", "conversations")
                record_id = original_quote["source_id"]
                with closing(sqlite3.connect(self.database(workspace, source))) as database, database:
                    database.execute("DELETE FROM " + runtime._quoted(table) + " WHERE fixture_record_id=?", [record_id])
                path = fixture / "profiles" / profile / "initial" / (source + "." + table + ".json")
                key = "callId" if profile == "salesforce-gong" else "id"
                path.write_text(json.dumps([row for row in json.loads(path.read_text()) if row[key] != record_id]))
                self.sync(workspace, source)
                if profile == "billing-support":
                    path = fixture / "profiles" / profile / "initial" / "stripe.invoices.json"
                    invoices = json.loads(path.read_text())
                    invoice = next(row for row in invoices if row["customer"] == "cus_synthetic_a" and row["status"] == "open" and row["amount_remaining"] > 0)
                    invoice["status"] = "void"
                    with closing(sqlite3.connect(self.database(workspace, "stripe"))) as database, database:
                        database.execute("UPDATE invoices SET status=?,record_json=? WHERE fixture_record_id=?", ["void", canonical(invoice), invoice["id"]])
                    path.write_text(json.dumps(invoices))
                    self.sync(workspace, "stripe")
                snapshot = snapshots.capture_snapshot(workspace, before["extraction"]["source_names"])
                profiles.materialize(workspace, reviewed["profile"], snapshot["snapshot_id"])
                expected = oracle.evaluate(profile, "initial", root=fixture)
                fields = ("text", "updates") if profile == "billing-support" else ("text",)
                briefs = {alias: answers.brief(workspace, reviewed["profile"], snapshot["snapshot_id"], value, fields=fields, quote_limit=20)
                          for alias, value in reviewed["customer_ids"].items()}
                self.assert_briefs(briefs, expected)
                self.assertNotIn(record_id, {quote["source_id"] for brief in briefs.values() for quote in brief["quotes"]})
                self.assertEqual(briefs["customer_a"]["definition_sha256"], reviewed["definition_sha256"])
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    answers.read_evidence(workspace, reviewed["profile"], snapshot["snapshot_id"], customer, original_quote["evidence_id"])
                self.assert_history(workspace, before)

    def test_changed_facts_compare_against_retained_review(self):
        from contextbase.customer360 import reviews
        for profile in ("salesforce-gong", "billing-support"):
            with self.subTest(profile=profile):
                before = self.runs[profile]["initial"]
                workspace = self.copy(profile, "initial", profile)
                reviewed = before["review"]
                fields = ("text", "updates") if profile == "billing-support" else ("text",)
                saved = reviews.save_review(workspace, reviewed["profile"], reviewed["snapshot_id"], reviewed["customer_ids"]["customer_a"],
                                             fields=fields, quote_limit=20)
                self.assertEqual(saved["brief"], before["briefs"]["customer_a"])
                self.assertEqual(saved["status"], "retained_review_not_current_answer")
                self.assertEqual(saved["review_id"], sha256({key: value for key, value in saved.items() if key != "review_id"}))
                extraction = runtime.load_profile(workspace, profile, "refreshed")
                after = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
                retained = reviews.load_review(Workspace(workspace.root), saved["review_id"])
                self.assertEqual(retained, saved)
                listed = reviews.list_reviews(workspace, reviewed["profile"], reviewed["customer_ids"]["customer_a"])
                self.assertEqual([value["review_id"] for value in listed], [saved["review_id"]])
                self.assertEqual(reviews.list_reviews(workspace, reviewed["profile"], reviewed["customer_ids"]["customer_b"]), [])
                comparison = reviews.compare_review(workspace, saved["review_id"], after["snapshot_id"])
                self.assertEqual(comparison["saved_review_id"], saved["review_id"])
                self.assertEqual(comparison["previous_snapshot_id"], reviewed["snapshot_id"])
                self.assertEqual(comparison["snapshot_id"], after["snapshot_id"])
                self.assertEqual(comparison["customer_id"], reviewed["customer_ids"]["customer_a"])
                self.assertEqual(comparison["profile"], reviewed["profile"])
                self.assertEqual(comparison["definition_sha256"], saved["definition_sha256"])
                self.assertEqual(comparison["context_id"], saved["context_id"])
                self.assertEqual(comparison["profile_approval"], saved["profile_approval"])
                # The refresh explicitly reviews new source-ID aliases. The
                # source facts may be compared, but a changed identity basis
                # must be visible and must suppress causal arithmetic deltas.
                self.assertEqual(comparison["changed_basis"], ["identity_sha256"])
                self.assertEqual(comparison["status"], "review_basis_changed")
                expected_after = oracle.evaluate(profile, "refreshed")
                metric_name = "open_pipeline" if profile == "salesforce-gong" else "outstanding_invoices"
                unit = "currency" if profile == "salesforce-gong" else "minor_currency"
                initial_customer = next(value for value in before["expected"]["customers"] if value["customer_id"] == "customer_a")
                current_customer = next(value for value in expected_after["customers"] if value["customer_id"] == "customer_a")
                expected_changes = []
                for currency in sorted(set(initial_customer["amounts_by_currency"]) | set(current_customer["amounts_by_currency"])):
                    left, right = initial_customer["amounts_by_currency"].get(currency), current_customer["amounts_by_currency"].get(currency)
                    if left != right:
                        expected_changes.append((metric_name, unit, currency, None if left is None else Decimal(str(left)),
                                                 None if right is None else Decimal(str(right)), None))
                if profile == "billing-support" and initial_customer["open_conversations"] != current_customer["open_conversations"]:
                    expected_changes.append(("open_support_cases", "records", None, Decimal(initial_customer["open_conversations"]),
                                             Decimal(current_customer["open_conversations"]), None))
                actual_changes = [(value["definition_name"], value["unit"], value["currency"],
                                   None if value["before"] is None else Decimal(value["before"]),
                                   None if value["after"] is None else Decimal(value["after"]), value["delta"])
                                  for value in comparison["metric_changes"]]
                self.assertCountEqual(actual_changes, expected_changes)
                actual_amounts = next(value["result"]["values"] for value in comparison["current_brief"]["metrics"]
                                      if value["result"]["definition_name"] == metric_name)
                self.assertEqual({value["currency"]: Decimal(value["value"]) for value in actual_amounts},
                                 {currency: Decimal(str(value)) for currency, value in current_customer["amounts_by_currency"].items()})
                for citation in expected_after["attributed_text"]:
                    if citation["customer_id"] == "customer_a":
                        self.assertTrue(any(quote["source_id"] == citation["record_id"] and citation["text"] in quote["text"]
                                            for quote in comparison["current_brief"]["quotes"]))

                def expected_records(phase, expected):
                    # Direct fixture values for the reviewed roles. This code
                    # does not read product SQL, projections or result hashes.
                    selected = ({"salesforce.account": ("Name",), "salesforce.opportunity": ("AccountId", "Amount", "CurrencyIsoCode", "IsClosed", "CloseDate"),
                                 "gong.call_transcripts": ("transcript",)} if profile == "salesforce-gong" else
                                {"stripe.customers": ("name",), "stripe.invoices": ("customer", "amount_remaining", "currency", "status"),
                                 "intercom.conversations": ("source", "state", "conversation_parts")})
                    result = {}
                    for record in expected["resolved_identities"]:
                        resource = "gong.call_transcripts" if record["resource"] == "gong.extensive_calls" else record["resource"]
                        if record["customer_id"] != "customer_a" or resource not in selected:
                            continue
                        raw = json.loads((ROOT / "profiles" / profile / phase / (resource + ".json")).read_text())
                        source_id = "callId" if resource == "gong.call_transcripts" else "Id" if resource.startswith("salesforce.") else "id"
                        row = next(value for value in raw if value[source_id] == record["record_id"])
                        result[(resource, record["record_id"])] = {key: row[key] for key in selected[resource]}
                    return result

                old_records = expected_records("initial", before["expected"])
                new_records = expected_records("refreshed", expected_after)
                labels = {value: key for key, value in extraction["source_label_mapping"].items()}
                def key(record):
                    return labels[record["connection"]] + "." + record["entity"], record["source_id"]
                self.assertEqual({key(value) for value in comparison["record_changes"]["added"]}, set(new_records) - set(old_records))
                self.assertEqual({key(value) for value in comparison["record_changes"]["removed"]}, set(old_records) - set(new_records))
                self.assertEqual({key(value["after"]) for value in comparison["record_changes"]["changed"]},
                                 {value for value in set(old_records) & set(new_records) if old_records[value] != new_records[value]})
                self.assertEqual(comparison["comparison_id"], sha256({key: value for key, value in comparison.items() if key != "comparison_id"}))
                with self.assertRaisesRegex(ValueError, "no longer matches"):
                    reviews.compare_review(workspace, saved["review_id"], reviewed["snapshot_id"])
                self.assert_history(workspace, before)


if __name__ == "__main__":
    unittest.main()
