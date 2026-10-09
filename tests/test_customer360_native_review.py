"""Native SQLite extraction through reviewed context and grounded answers.

The independent oracle is read only by this test, never by the fixture review
adapter. No model, vendor API, mocked connector or acceptance-gate substitution.
"""
import copy
from decimal import Decimal
from importlib import util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from contextbase.customer360 import answers, contexts, identity, profiles
from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1] / "examples" / "customer-360"


def _module(name):
    spec = util.spec_from_file_location("native_review_fixture_" + name, ROOT / (name + ".py"))
    value = util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runtime, review, oracle = (_module(name) for name in ("runtime", "review", "oracle"))


def observe(workspace, extracted, reviewed, expected):
    """Collect real results before refresh makes the old source snapshot stale."""
    profile, snapshot = reviewed["profile"], reviewed["snapshot_id"]
    with profiles.readable_profile(workspace, profile, snapshot) as scope:
        records = copy.deepcopy(scope[-1]["records"])
    source_labels = {value: key for key, value in extracted["source_label_mapping"].items()}
    indexed = {(source_labels[row["connection"]] + "." + row["entity"], row["source_id"]): row for row in records}
    metric_names = ["open_pipeline"] if extracted["profile"] == "salesforce-gong" else ["outstanding_invoices", "open_support_cases"]
    metrics, quotes, rendered = {}, [], {}
    for alias, customer_id in reviewed["customer_ids"].items():
        metrics[alias] = {name: answers.execute_metric(workspace, profile, snapshot, customer_id, name) for name in metric_names}
    for citation in expected["attributed_text"]:
        row = indexed[(citation["resource"], citation["record_id"])]
        field = "updates" if "/conversation_parts/" in citation["json_pointer"] else "text"
        start = row["fields"][field].index(citation["text"])
        quote = answers.read_evidence(workspace, profile, snapshot, reviewed["customer_ids"][citation["customer_id"]],
                                      row["evidence_id"], field=field, start=start, end=start + len(citation["text"]))
        quotes.append({"expected": citation, "quote": quote})
    for alias, customer_id in reviewed["customer_ids"].items():
        selected = {"metrics": [{"definition_name": name, "result_id": result["result_id"]}
                                for name, result in metrics[alias].items()],
                    "quotes": [{key: item["quote"][key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")}
                               for item in quotes if item["expected"]["customer_id"] == alias]}
        rendered[alias] = answers.validate_answer(workspace, profile, snapshot, customer_id, selected)
    return {"records": records, "metrics": metrics, "quotes": quotes, "answers": rendered,
            "context": contexts.current_context(workspace, profile, snapshot)}


class NativeCustomerReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # Missing installed runtime is a failure, never a skip.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.runs, cls.workspaces, cls.same_label_customers = {}, {}, {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(cls.temp.name) / profile)
            # Label equality must not silently adopt a preexisting customer.
            cls.same_label_customers[profile] = identity.create_customer(workspace, "synthetic-" + profile + "-customer_a")["id"]
            cls.workspaces[profile], cls.runs[profile] = workspace, {}
            for phase in ("initial", "refreshed"):
                extracted = runtime.load_profile(workspace, profile, phase)
                reviewed = review.setup_review(workspace, extracted, approve_synthetic_fixture=True)
                expected = oracle.evaluate(profile, phase)
                observed = observe(workspace, extracted, reviewed, expected)
                cls.runs[profile][phase] = {"extracted": extracted, "reviewed": reviewed, "expected": expected, "observed": observed}

    def each_run(self):
        for profile, phases in self.runs.items():
            for phase, run in phases.items():
                yield profile, phase, run

    def test_real_extraction_review_and_answers_have_honest_scope(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                extracted, reviewed = run["extracted"], run["reviewed"]
                self.assertTrue(extracted["actual_dlt_executed"])
                self.assertEqual(extracted["status"], "passed")
                self.assertEqual(extracted["runtime_versions"]["dlt"], "1.24.0")
                self.assertEqual(extracted["chunk_size"], 2)
                self.assertTrue(all(value["source_type"] == "sqlite" and value["status"] == "synced" for value in extracted["publications"]))
                self.assertEqual(reviewed["scope"], "authored_synthetic_review_after_native_sqlite_extraction")
                for value in (extracted, reviewed):
                    self.assertFalse(value["vendor_adapter_verified"])
                    self.assertEqual(value["model_calls"], 0)
                self.assertFalse(reviewed["human_business_review"])
                self.assertEqual(run["observed"]["context"]["origin"], "scripted_agent_test")
                self.assertEqual(run["observed"]["context"]["reviewer"], "synthetic-fixture-recipe")
                for answer in run["observed"]["answers"].values():
                    self.assertEqual(answer["scope"], "reviewed_metrics_and_exact_attributed_quotes")
                    self.assertFalse(answer["model_generated_factual_prose"])
                    self.assertIn("limited_source_coverage", {gap["code"] for gap in answer["gaps"]})

    def test_all_projected_identities_and_gaps_match_independent_oracle(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                expected, reviewed = run["expected"], run["reviewed"]
                resolved = {(row["resource"], row["record_id"]): row["customer_id"] for row in expected["resolved_identities"]}
                issues = {(row["resource"], row["record_id"]): row["issue"] for row in expected["identity_issues"]}
                labels = {v: k for k, v in run["extracted"]["source_label_mapping"].items()}
                observed_keys = []
                for row in run["observed"]["records"]:
                    resource = labels[row["connection"]] + "." + row["entity"]
                    oracle_resource = "gong.extensive_calls" if resource == "gong.call_transcripts" else resource
                    key = oracle_resource, row["source_id"]
                    observed_keys.append((resource, row["source_id"]))
                    if key in resolved:
                        self.assertEqual(row["link_status"], "resolved")
                        self.assertEqual(row["customer_id"], reviewed["customer_ids"][resolved[key]])
                    else:
                        self.assertIn(key, issues)
                        self.assertEqual(row["link_status"], "conflict" if issues[key] == "conflicting_reference" else "unresolved")
                        self.assertIsNone(row["customer_id"])
                self.assertEqual(len(observed_keys), len(set(observed_keys)))
                selected = {"salesforce.account", "salesforce.opportunity", "gong.call_transcripts"} if profile == "salesforce-gong" else {"stripe.customers", "stripe.invoices", "intercom.conversations"}
                self.assertEqual(len(observed_keys), sum(expected["source_record_counts"][key] for key in selected))
                self.assertEqual(sum(row["link_status"] == "conflict" for row in run["observed"]["records"]), 1)
                self.assertEqual(sum(row["link_status"] == "unresolved" for row in run["observed"]["records"]), 1)

    def test_each_customer_numeric_results_and_currency_match_independent_oracle(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                total = {}
                for expected in run["expected"]["customers"]:
                    results = run["observed"]["metrics"][expected["customer_id"]]
                    metric_name = "open_pipeline" if profile == "salesforce-gong" else "outstanding_invoices"
                    result = results[metric_name]["result"]
                    values = {row["currency"]: Decimal(row["value"]) for row in result["values"]}
                    self.assertEqual(values, {currency: Decimal(str(value)) for currency, value in expected["amounts_by_currency"].items()})
                    self.assertEqual(result["matched_records"], expected["open_opportunities" if profile == "salesforce-gong" else "open_invoices"])
                    self.assertEqual(result["unit"], "currency" if profile == "salesforce-gong" else "minor_currency")
                    for currency, value in values.items():
                        total[currency] = total.get(currency, Decimal(0)) + value
                    if profile == "billing-support":
                        self.assertEqual(results["open_support_cases"]["result"]["values"], [{"currency": None, "value": expected["open_conversations"]}])
                self.assertEqual(total, {currency: Decimal(str(value)) for currency, value in run["expected"]["totals_by_currency"].items()})

    def test_every_oracle_quote_is_exact_and_attributed_to_the_same_customer(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                self.assertEqual(len(run["observed"]["quotes"]), len(run["expected"]["attributed_text"]))
                for item in run["observed"]["quotes"]:
                    expected, quote = item["expected"], item["quote"]
                    value = json.loads((ROOT / expected["file"]).read_text())
                    for token in expected["json_pointer"].split("/")[1:]:
                        value = value[int(token)] if isinstance(value, list) else value[token]
                    self.assertEqual(quote["text"], value)
                    self.assertEqual(quote["customer_id"], run["reviewed"]["customer_ids"][expected["customer_id"]])
                    self.assertEqual(quote["source_id"], expected["record_id"])
                    self.assertEqual(quote["snapshot_id"], run["reviewed"]["snapshot_id"])
                    self.assertEqual(quote["definition_sha256"], run["reviewed"]["definition_sha256"])
                    self.assertIn(quote["quote_id"], {q["quote_id"] for q in run["observed"]["answers"][expected["customer_id"]]["quotes"]})

    def test_refresh_reuses_customer_context_and_profile_but_changes_evidence(self):
        for profile, phases in self.runs.items():
            before, after = phases["initial"], phases["refreshed"]
            for key in ("customer_ids", "profile_approval", "definition_sha256", "context_id"):
                self.assertEqual(before["reviewed"][key], after["reviewed"][key])
            for key in ("snapshot_id", "materialization_id"):
                self.assertNotEqual(before["reviewed"][key], after["reviewed"][key])
            self.assertNotEqual(before["expected"]["totals_by_currency"], after["expected"]["totals_by_currency"])
            self.assertNotEqual(before["observed"]["answers"]["customer_a"]["answer_id"], after["observed"]["answers"]["customer_a"]["answer_id"])
            newest = after["expected"]["latest_attributed_text"]["customer_a"]
            self.assertIn(newest["text"], {item["quote"]["text"] for item in after["observed"]["quotes"]})
            decisions = json.loads((ROOT / "profiles" / profile / "mapping-decisions.json").read_text())
            aliases = [item for item in decisions["bindings"] if item["customer_id"] == "customer_a"
                       and item["source"] in ("salesforce", "stripe")]
            self.assertGreaterEqual(len({item["source_id"] for item in aliases}), 2)
            for item in aliases:
                resolved = identity.resolve(self.workspaces[profile], after["extracted"]["source_label_mapping"][item["source"]], item["resource"], item["source_id"])
                self.assertEqual(resolved["customer_id"], after["reviewed"]["customer_ids"]["customer_a"])
        support = self.runs["billing-support"]["refreshed"]
        self.assertTrue(any(item["quote"]["field"] == "updates" for item in support["observed"]["quotes"]))

    def test_matching_display_labels_never_merge_existing_customers(self):
        for profile, phases in self.runs.items():
            self.assertNotIn(self.same_label_customers[profile], phases["initial"]["reviewed"]["customer_ids"].values())
            self.assertEqual(len(identity.list_customers(self.workspaces[profile])), len(phases["initial"]["reviewed"]["customer_ids"]) + 1)

    def test_explicit_synthetic_approval_is_required_before_any_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory) / "workspace")
            for value in (False, None, 1, "yes"):
                with self.assertRaisesRegex(ValueError, "explicit synthetic approval"):
                    review.setup_review(workspace, {}, approve_synthetic_fixture=value)
            self.assertFalse(workspace.db_path.exists())

    def test_wrong_or_stale_publication_receipts_cannot_drive_new_reviews(self):
        for profile, workspace in self.workspaces.items():
            with self.assertRaisesRegex(ValueError, "publication receipts"):
                review.setup_review(workspace, self.runs[profile]["initial"]["extracted"], approve_synthetic_fixture=True)
            for field in ("attempt", "snapshot_sha256", "configuration_sha256"):
                changed = copy.deepcopy(self.runs[profile]["refreshed"]["extracted"])
                changed["publications"][0][field] = "authored-mismatch"
                with self.assertRaisesRegex(ValueError, "publication receipts"):
                    review.setup_review(workspace, changed, approve_synthetic_fixture=True)
            changed = copy.deepcopy(self.runs[profile]["refreshed"]["extracted"])
            changed["resources"][0]["source_records_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "committed source records"):
                review.setup_review(workspace, changed, approve_synthetic_fixture=True)

    def test_unknown_and_conflicting_records_cannot_supply_customer_quotes(self):
        for profile, phases in self.runs.items():
            run, workspace = phases["refreshed"], self.workspaces[profile]
            reviewed = run["reviewed"]
            for row in run["observed"]["records"]:
                if row["link_status"] == "resolved":
                    continue
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    answers.read_evidence(workspace, reviewed["profile"], reviewed["snapshot_id"], reviewed["customer_ids"]["customer_a"], row["evidence_id"])
            quote = run["observed"]["quotes"][0]["quote"]
            other_customer = next(value for value in reviewed["customer_ids"].values() if value != quote["customer_id"])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                answers.read_evidence(workspace, reviewed["profile"], reviewed["snapshot_id"], other_customer, quote["evidence_id"])

    def test_previous_snapshot_answer_is_refused_after_refresh(self):
        for profile, phases in self.runs.items():
            previous = phases["initial"]["reviewed"]
            with self.assertRaises(ValueError):
                answers.execute_metric(self.workspaces[profile], previous["profile"], previous["snapshot_id"],
                                       previous["customer_ids"]["customer_a"], "open_pipeline" if profile == "salesforce-gong" else "outstanding_invoices")

    def test_partial_relationships_are_not_silently_attributed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture"
            shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns("__pycache__"))
            for profile, resource in (("salesforce-gong", "gong.extensive_calls"), ("billing-support", "intercom.conversations")):
                path = fixture / "profiles" / profile / "initial" / (resource + ".json")
                rows = json.loads(path.read_text())
                if profile == "salesforce-gong":
                    rows[0]["context"][0]["objects"].append({"objectType": "Account", "objectId": "missing-synthetic-account"})
                    record_id = rows[0]["metaData"]["id"]
                    entity = "call_transcripts"
                else:
                    rows[0]["contacts"]["contacts"].append({"id": "missing-synthetic-contact"})
                    record_id, entity = rows[0]["id"], "conversations"
                path.write_text(json.dumps(rows))
                expected = oracle.evaluate(profile, "initial", root=fixture)
                self.assertIn({"resource": resource, "record_id": record_id, "issue": "incomplete_reference", "candidate_customers": ["customer_a"]}, expected["identity_issues"])
                workspace = Workspace(Path(directory) / profile)
                extracted = runtime.load_profile(workspace, profile, fixture_root=fixture)
                reviewed = review.setup_review(workspace, extracted, approve_synthetic_fixture=True, fixture_root=fixture)
                with profiles.readable_profile(workspace, reviewed["profile"], reviewed["snapshot_id"]) as scope:
                    record = next(row for row in scope[-1]["records"] if row["entity"] == entity and row["source_id"] == record_id)
                self.assertEqual(record["link_status"], "unresolved")
                self.assertIsNone(record["customer_id"])


if __name__ == "__main__":
    unittest.main()
