"""Grounded-answer acceptance over real local extraction and an independent oracle.

Both authored source-shaped profiles pass through the actual dlt SQLite worker,
reviewed identity/profile/context and shared answer tools. This proves local
functional behavior, not vendor compatibility, actual model execution, or
semantic accuracy of model-written prose (which the answer contract refuses).
"""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from contextbase.customer360 import answers, contexts, identity, profiles, samples, snapshots
from contextbase.customer360.contracts import sha256
from contextbase.workspace import Workspace
from tests.test_customer360_native_review import runtime, review, oracle


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "customer-360"


def select_metric(receipt):
    return {"definition_name": receipt["result"]["definition_name"], "result_id": receipt["result_id"]}


def select_quote(receipt):
    return {key: receipt[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")}


class GroundedAnswersAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # Runtime absence is a failure, never a skipped gate.
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.runs = {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(cls.temp.name) / (profile + "-current"))
            for phase in ("initial", "refreshed"):
                extraction = runtime.load_profile(workspace, profile, phase)
                reviewed = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
                expected = oracle.evaluate(profile, phase)
                # Retain the initial *committed workspace*, so all acceptance
                # cases re-execute real tools for each phase, not cached answers.
                selected = workspace
                if phase == "initial":
                    target = Path(cls.temp.name) / (profile + "-initial")
                    shutil.copytree(workspace.root, target)
                    selected = Workspace(target)
                cls.runs[(profile, phase)] = {"workspace": selected, "extraction": extraction,
                                             "reviewed": reviewed, "expected": expected}

    def each_run(self):
        for (profile, phase), run in self.runs.items():
            yield profile, phase, run

    def metric(self, run, alias, definition=None):
        reviewed = run["reviewed"]
        definition = definition or ("open_pipeline" if run["expected"]["profile"] == "salesforce-gong" else "outstanding_invoices")
        return answers.execute_metric(run["workspace"], reviewed["profile"], reviewed["snapshot_id"],
                                      reviewed["customer_ids"][alias], definition)

    def records(self, run, alias):
        reviewed = run["reviewed"]
        return profiles.records_for_customer(run["workspace"], reviewed["profile"], reviewed["customer_ids"][alias],
                                             reviewed["snapshot_id"])["records"]

    def quote(self, run, citation):
        row = next(row for row in self.records(run, citation["customer_id"])
                   if row["source_id"] == citation["record_id"] and row["entity"] == citation["resource"].split(".")[1])
        field = "updates" if "/conversation_parts/" in citation["json_pointer"] else "text"
        start = row["fields"][field].index(citation["text"])
        reviewed = run["reviewed"]
        return answers.read_evidence(run["workspace"], reviewed["profile"], reviewed["snapshot_id"],
                                     reviewed["customer_ids"][citation["customer_id"]], row["evidence_id"],
                                     field=field, start=start, end=start + len(citation["text"]))

    def validate(self, run, alias, selection):
        reviewed = run["reviewed"]
        return answers.validate_answer(run["workspace"], reviewed["profile"], reviewed["snapshot_id"],
                                       reviewed["customer_ids"][alias], selection)

    def test_numeric_results_match_independent_oracle(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                totals = {}
                for expected in run["expected"]["customers"]:
                    receipt = self.metric(run, expected["customer_id"])
                    result = receipt["result"]
                    scope = receipt["calculation_scope"]
                    self.assertEqual(scope["population"], "selected_customer_resolved_records")
                    self.assertEqual(scope["customer_id"], run["reviewed"]["customer_ids"][expected["customer_id"]])
                    self.assertEqual(scope["snapshot_id"], run["reviewed"]["snapshot_id"])
                    self.assertEqual((scope["operation"], scope["field"], scope["currency_field"]), ("sum", "amount", "currency"))
                    self.assertEqual(scope["filters"], [{"field": "is_closed", "operator": "eq", "value": False}]
                                     if profile == "salesforce-gong" else [{"field": "status", "operator": "eq", "value": "open"}])
                    self.assertEqual(scope["unit"], "currency" if profile == "salesforce-gong" else "minor_currency")
                    self.assertNotIn("description", scope)
                    self.assertNotIn("name", scope)
                    self.assertIn("Unresolved and conflicting records and other customers are excluded.", scope["text"])
                    actual = {item["currency"]: Decimal(item["value"]) for item in result["values"]}
                    self.assertEqual(actual, {currency: Decimal(str(value)) for currency, value in expected["amounts_by_currency"].items()})
                    self.assertTrue(all(isinstance(item["value"], str) for item in result["values"]))
                    self.assertEqual(result["unit"], "currency" if profile == "salesforce-gong" else "minor_currency")
                    self.assertEqual(result["matched_records"], expected["open_opportunities" if profile == "salesforce-gong" else "open_invoices"])
                    for currency, value in actual.items():
                        totals[currency] = totals.get(currency, Decimal(0)) + value
                    if profile == "billing-support":
                        count = self.metric(run, expected["customer_id"], "open_support_cases")["result"]
                        self.assertEqual(count["values"], [{"currency": None, "value": expected["open_conversations"]}])
                        self.assertIs(type(count["values"][0]["value"]), int)
                self.assertEqual(totals, {currency: Decimal(str(value)) for currency, value in run["expected"]["totals_by_currency"].items()})
                self.assertEqual(set(totals), {"USD", "EUR"})
        for profile in ("salesforce-gong", "billing-support"):
            before, after = (self.runs[(profile, phase)] for phase in ("initial", "refreshed"))
            self.assertNotEqual(self.metric(before, "customer_a")["result"]["values"], self.metric(after, "customer_a")["result"]["values"])

    def test_customer_sets_match_independent_oracle(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                aliases = sorted(run["reviewed"]["customer_ids"])
                with_money = sorted(alias for alias in aliases if self.metric(run, alias)["result"]["matched_records"] > 0)
                attributed = sorted(alias for alias in aliases if any(row["kind"] == ("conversation" if profile == "salesforce-gong" else "support")
                                                                      for row in self.records(run, alias)))
                if profile == "salesforce-gong":
                    actual = {"with_open_opportunities": with_money, "with_attributed_calls": attributed,
                              "without_attributed_calls": sorted(set(aliases) - set(attributed))}
                else:
                    open_support = sorted(alias for alias in aliases if self.metric(run, alias, "open_support_cases")["result"]["values"][0]["value"] > 0)
                    actual = {"with_outstanding_invoices": with_money, "with_attributed_support": attributed, "with_open_support": open_support}
                self.assertEqual(actual, run["expected"]["customer_sets"])
                # Membership must match source references, not just a global
                # numeric total that could conceal a wrong customer join.
                inverse = {value: key for key, value in run["reviewed"]["customer_ids"].items()}
                source_names = {value: key for key, value in run["extraction"]["source_label_mapping"].items()}
                with profiles.readable_profile(run["workspace"], run["reviewed"]["profile"], run["reviewed"]["snapshot_id"]) as scope:
                    records = scope[-1]["records"]
                expected_members = {(row["resource"], row["record_id"]): row["customer_id"] for row in run["expected"]["resolved_identities"]}
                for record in records:
                    resource = source_names[record["connection"]] + "." + record["entity"]
                    resource = "gong.extensive_calls" if resource == "gong.call_transcripts" else resource
                    expected = expected_members.get((resource, record["source_id"]))
                    self.assertEqual(inverse.get(record["customer_id"]), expected)
                self.assertEqual(sum(record["link_status"] == "conflict" for record in records), 1)
                self.assertEqual(sum(record["link_status"] == "unresolved" for record in records), 1)
        before = self.runs[("billing-support", "initial")]["expected"]["customer_sets"]["with_open_support"]
        after = self.runs[("billing-support", "refreshed")]["expected"]["customer_sets"]["with_open_support"]
        self.assertNotEqual(before, after)

    def test_factual_claims_resolve_to_supporting_evidence(self):
        updates = 0
        for profile, phase, run in self.each_run():
            for citation in run["expected"]["attributed_text"]:
                with self.subTest(profile=profile, phase=phase, source=citation["record_id"], pointer=citation["json_pointer"]):
                    # Resolve the independent pointer in the original authored
                    # source JSON, rather than accepting a valid citation ID.
                    value = json.loads((FIXTURE_ROOT / citation["file"]).read_text())
                    for token in citation["json_pointer"].split("/")[1:]:
                        value = value[int(token)] if isinstance(value, list) else value[token]
                    quote = self.quote(run, citation)
                    self.assertEqual(quote["text"], value)
                    self.assertEqual(quote["source_id"], citation["record_id"])
                    self.assertEqual(quote["customer_id"], run["reviewed"]["customer_ids"][citation["customer_id"]])
                    final = self.validate(run, citation["customer_id"], {"metrics": [], "quotes": [select_quote(quote)]})
                    self.assertEqual(final["quotes"], [quote])
                    self.assertIn(repr(value), final["rendered"])
                    self.assertFalse(final["model_generated_factual_prose"])
                    self.assertEqual(final["scope"], "reviewed_metrics_and_exact_attributed_quotes")
                    updates += quote["field"] == "updates"
        self.assertGreater(updates, 0)
        for profile in ("salesforce-gong", "billing-support"):
            run = self.runs[(profile, "refreshed")]
            latest = run["expected"]["latest_attributed_text"]["customer_a"]
            self.assertEqual(self.quote(run, latest)["text"], latest["text"])

    def test_latest_passage_matches_reviewed_source_time_across_profiles(self):
        """Actual staged extraction and approvals; raw-source time is the oracle.

        Authored synthetic sources run through dlt SQLite. This is neither a
        configured-model smoke nor evidence of native vendor completeness.
        Equal event timestamps remain complete sets of tied passages.
        """
        manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

        def seconds(value):
            if type(value) is int:
                return Decimal(value)
            delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - epoch
            return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1_000_000)

        def raw_citation(profile, phase, citation):
            """Resolve authored rows independently of product pointer/time code."""
            rows = json.loads((FIXTURE_ROOT / citation["file"]).read_text())
            tokens = citation["json_pointer"].split("/")[1:]
            row = rows[int(tokens[0])]
            passage = row
            for token in tokens[1:]:
                passage = passage[int(token)] if isinstance(passage, list) else passage[token]
            self.assertEqual(passage, citation["text"])
            if profile == "salesforce-gong":
                path = manifest["profiles"][profile]["phases"][phase]["gong.extensive_calls"]
                call = next(item for item in json.loads((FIXTURE_ROOT / path).read_text())
                            if item["metaData"]["id"] == citation["record_id"])
                observed = call["metaData"]["started"]
            elif tokens[1] == "source":
                observed = row["created_at"]
            else:
                parent = row
                for token in tokens[1:-1]:
                    parent = parent[int(token)] if isinstance(parent, list) else parent[token]
                observed = parent["created_at"]
            self.assertEqual(observed, citation["observed_at"])
            return passage, seconds(observed), "/" + "/".join(tokens[2:])

        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(self.temp.name) / ("staged-latest-" + profile))
            kind = "conversation" if profile == "salesforce-gong" else "support"
            initial_plan = initial_mapping = initial_context = previous_selection = previous_answer = None
            for phase in ("initial", "refreshed"):
                with self.subTest(profile=profile, phase=phase):
                    extraction = samples.connect(workspace, profile, phase)
                    self.assertTrue(extraction["actual_dlt_executed"])
                    self.assertFalse(extraction["vendor_adapter_verified"])
                    self.assertEqual(extraction["model_calls"], 0)
                    if initial_plan is not None:
                        with self.assertRaises(ValueError):
                            answers.latest_evidence(workspace, initial_plan["profile"], initial_plan["snapshot_id"],
                                                    initial_plan["customer_ids"]["customer_a"], kind)
                    plan = samples.prepare(workspace, profile)
                    self.assertFalse(plan["approvals_performed"])
                    for proposal in plan["links"]:
                        if proposal["status"] == "proposed":
                            identity.approve_links(workspace, proposal["proposal_id"], proposal["approval_sha256"],
                                                   "synthetic-temporal-acceptance-review")
                    if initial_plan is None:
                        proposed = profiles.propose_profile(workspace, plan["mapping"], plan["snapshot_id"])
                        initial_mapping = profiles.approve_profile(workspace, proposed["proposal_id"], proposed["approval_sha256"],
                                                                   "synthetic-temporal-acceptance-review")
                    else:
                        self.assertEqual(plan["mapping"], initial_plan["mapping"])
                        self.assertEqual(plan["definitions"], initial_plan["definitions"])
                        self.assertEqual(plan["customer_ids"], initial_plan["customer_ids"])
                    receipt = profiles.materialize(workspace, plan["profile"], plan["snapshot_id"])
                    if initial_plan is None:
                        proposed = contexts.propose_context(workspace, plan["profile"], plan["snapshot_id"], plan["definitions"],
                                                            gaps=plan["gaps"], origin="scripted_agent_test")
                        initial_context = contexts.approve_context(workspace, proposed["proposal_id"], proposed["approval_sha256"],
                                                                  "synthetic-temporal-acceptance-review")
                        initial_plan = plan
                    current = contexts.current_context(workspace, plan["profile"], plan["snapshot_id"])
                    self.assertEqual(current["context_id"], initial_context["context_id"])
                    self.assertEqual(current["definition_sha256"], initial_context["definition_sha256"])
                    self.assertEqual(current["definitions"], initial_plan["definitions"])
                    self.assertEqual(receipt["profile_approval"], initial_mapping["approval_sha256"])
                    expected = oracle.evaluate(profile, phase)
                    resource = "gong.call_transcripts" if kind == "conversation" else "intercom.conversations"
                    self.assertEqual(next(row["records"] for row in receipt["coverage"] if row["kind"] == kind),
                                     expected["source_record_counts"][resource])
                    for customer in expected["customers"]:
                        alias = customer["customer_id"]
                        customer_id = plan["customer_ids"][alias]
                        records = profiles.records_for_customer(workspace, plan["profile"], customer_id, plan["snapshot_id"])["records"]
                        self.assertEqual(sum(row["kind"] == kind for row in records),
                                         customer["calls" if kind == "conversation" else "conversations"])
                        selection = answers.latest_evidence(workspace, plan["profile"], plan["snapshot_id"], customer_id, kind, limit=5)
                        candidates = [(citation, *raw_citation(profile, phase, citation))
                                      for citation in expected["attributed_text"] if citation["customer_id"] == alias]
                        if not candidates:
                            self.assertEqual(selection["status"], "no_passages")
                            self.assertEqual(selection["quotes"], [])
                            continue
                        latest_time = max(item[2] for item in candidates)
                        latest = [item for item in candidates if item[2] == latest_time]
                        self.assertEqual(selection["status"], "latest_unique" if len(latest) == 1 else "latest_tied")
                        self.assertTrue(selection["coverage"]["complete"])
                        self.assertFalse(selection["coverage"]["truncated"])
                        self.assertEqual(Decimal(selection["latest_time"]["unix_seconds"]), latest_time)
                        self.assertEqual({(quote["source_id"], quote["text"], quote["json_pointer"]) for quote in selection["quotes"]},
                                         {(item[0]["record_id"], item[1], item[3]) for item in latest})
                        self.assertIn(expected["latest_attributed_text"][alias]["text"], {quote["text"] for quote in selection["quotes"]})
                        selected_quotes = []
                        for quote in selection["quotes"]:
                            self.assertEqual(quote["customer_id"], customer_id)
                            self.assertEqual(quote["connection"], extraction["source_label_mapping"][resource.split(".")[0]])
                            self.assertEqual(quote["entity"], resource.split(".")[1])
                            self.assertEqual(Decimal(quote["time"]["unix_seconds"]), latest_time)
                            self.assertEqual(quote["time"]["status"], "known")
                            self.assertEqual(quote["snapshot_id"], plan["snapshot_id"])
                            self.assertEqual(quote["context_id"], current["context_id"])
                            locator = {key: quote[key] for key in ("field", "start", "end", "passage_name", "json_pointer")}
                            reopened = answers.read_evidence(workspace, plan["profile"], plan["snapshot_id"], customer_id,
                                                             quote["evidence_id"], **locator)
                            self.assertEqual(reopened, quote)
                            selected_quotes.append({**select_quote(quote), "passage_name": quote["passage_name"],
                                                    "json_pointer": quote["json_pointer"]})
                        self.assertEqual(selection["selection_id"], sha256({key: value for key, value in selection.items() if key != "selection_id"}))
                        self.assertEqual(answers.latest_evidence(workspace, plan["profile"], plan["snapshot_id"], customer_id, kind, limit=5), selection)
                        answer = {"metrics": [], "quotes": selected_quotes,
                                  "latest": [{key: selection[key] for key in ("kind", "limit", "selection_id")}]}
                        final = answers.validate_answer(workspace, plan["profile"], plan["snapshot_id"], customer_id, answer)
                        self.assertEqual(final["quotes"], selection["quotes"])
                        self.assertEqual(final["temporal_selections"], [selection])
                        self.assertFalse(final["model_generated_factual_prose"])
                        if alias == "customer_a":
                            if previous_selection is not None:
                                self.assertNotEqual(previous_selection["selection_id"], selection["selection_id"])
                                self.assertNotEqual({quote["text"] for quote in previous_selection["quotes"]},
                                                    {quote["text"] for quote in selection["quotes"]})
                                self.assertGreater(latest_time, Decimal(previous_selection["latest_time"]["unix_seconds"]))
                                with self.assertRaises(ValueError):
                                    answers.validate_answer(workspace, plan["profile"], plan["snapshot_id"], customer_id, previous_answer)
                            previous_selection, previous_answer = selection, answer

    def test_fabricated_citation_and_unsupported_claim_are_rejected(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                quote = self.quote(run, run["expected"]["attributed_text"][0])
                original = {"metrics": [select_metric(self.metric(run, "customer_a"))], "quotes": [select_quote(quote)]}
                invalid = []
                forged = copy.deepcopy(original)
                forged["quotes"][0]["text"] = "The customer has committed to renew."
                invalid.append(forged)
                forged = copy.deepcopy(original)
                forged["quotes"][0]["evidence_id"] = "0" * 64
                invalid.append(forged)
                forged_quote = {**quote, "text": "Unsupported factual claim."}
                forged_quote["text_sha256"] = sha256(forged_quote["text"])
                forged_quote["quote_id"] = sha256({k: v for k, v in forged_quote.items() if k != "quote_id"})
                invalid.append({"metrics": [], "quotes": [select_quote(forged_quote)]})
                invalid.append({**original, "summary": "Customer health is excellent."})
                invalid.append({"metrics": [select_metric(self.metric(run, "customer_b"))], "quotes": []})
                for value in invalid:
                    with self.assertRaises(ValueError):
                        self.validate(run, "customer_a", value)
                with self.assertRaises(ValueError):
                    self.validate(run, "customer_b", {"metrics": [], "quotes": [select_quote(quote)]})
                self.assertEqual(self.validate(run, "customer_a", original)["quotes"][0], quote)

    def test_missing_capability_yields_gap_or_abstention(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                missing = "billing" if profile == "salesforce-gong" else "conversation"
                reviewed = run["reviewed"]
                brief = answers.brief(run["workspace"], reviewed["profile"], reviewed["snapshot_id"], reviewed["customer_ids"]["customer_a"])
                self.assertNotIn(missing, brief["capabilities"])
                self.assertTrue(any(gap.get("kind") == missing and gap["code"] == "capability_unavailable" for gap in brief["gaps"]))
                self.assertTrue(any(gap["code"] == "profile_unresolved_identity" for gap in brief["gaps"]))
                abstained = self.validate(run, "customer_a", {"abstain": {"code": "capability_unavailable"}})
                self.assertEqual(abstained["abstention"]["code"], "capability_unavailable")
                self.assertEqual(abstained["metrics"], [])
                self.assertEqual(abstained["quotes"], [])
                self.assertEqual(abstained["snapshot_id"], reviewed["snapshot_id"])
                self.assertEqual(abstained["context_id"], reviewed["context_id"])
                self.assertIn("No factual answer is returned", abstained["rendered"])
                with self.assertRaises(ValueError):
                    self.validate(run, "customer_a", {"abstain": {"code": "capability_unavailable", "reason": "The customer has no calls."}})
                with self.assertRaises(ValueError):
                    self.metric(run, "customer_a", "unreviewed_arr")

    def test_answer_receipt_pins_snapshot_context_queries_and_evidence(self):
        for profile, phase, run in self.each_run():
            with self.subTest(profile=profile, phase=phase):
                reviewed, workspace = run["reviewed"], run["workspace"]
                metric = self.metric(run, "customer_a")
                quote = self.quote(run, run["expected"]["attributed_text"][0])
                final = self.validate(run, "customer_a", {"metrics": [select_metric(metric)], "quotes": [select_quote(quote)]})
                snapshot = snapshots.assert_snapshot_current(workspace, reviewed["snapshot_id"])
                context = contexts.current_context(workspace, reviewed["profile"], reviewed["snapshot_id"])
                with profiles.readable_customer(workspace, reviewed["profile"], reviewed["customer_ids"]["customer_a"], reviewed["snapshot_id"]) as scope:
                    conn, _, _, materialized, payload = scope
                    evidence = {record["evidence_id"] for record in payload["records"]}
                    for receipt in (final, metric, quote):
                        self.assertEqual(receipt["snapshot_manifest_sha256"], snapshot["manifest_sha256"])
                        self.assertEqual(receipt["materialization_receipt_sha256"], materialized["receipt_sha256"])
                        self.assertEqual(receipt["profile_approval"], reviewed["profile_approval"])
                        self.assertEqual(receipt["identity_sha256"], materialized["identity"]["sha256"])
                        self.assertEqual(receipt["context_id"], context["context_id"])
                        self.assertEqual(receipt["definition_sha256"], sha256(context["definitions"]))
                    self.assertLessEqual(set(metric["evidence_ids"]), evidence)
                    self.assertIn(quote["evidence_id"], evidence)
                    observed_ids = []
                    for query in metric["queries"]:
                        self.assertEqual(query["query_sha256"], sha256({k: v for k, v in query.items() if k != "query_sha256"}))
                        actual = conn.execute(query["sql"], query["parameters"]).fetchall()
                        self.assertEqual({currency: Decimal(value) for currency, value in actual},
                                         {row["currency"]: Decimal(row["value"]) for row in metric["result"]["values"]})
                        selected = conn.execute(query["selection_sql"], query["selection_parameters"]).fetchall()
                        self.assertEqual(len(selected), query["matched_records"])
                        observed_ids.extend(row[0] for row in selected)
                        self.assertIn(query["source_table"], snapshot["coverage"]["tables"])
                    self.assertEqual(sorted(observed_ids), metric["evidence_ids"])
                for value, key in ((metric, "result_id"), (quote, "quote_id"), (final, "answer_id")):
                    self.assertEqual(value[key], sha256({k: v for k, v in value.items() if k != key}))
                self.assertEqual(metric["result_sha256"], sha256(metric["result"]))
                self.assertEqual(metric["evidence_sha256"], sha256(metric["evidence_ids"]))
                self.assertEqual(quote["text_sha256"], sha256(quote["text"]))
                self.assertEqual(final["source_observed_at"], payload["source_observed_at"])
                self.assertFalse(final["model_generated_factual_prose"])


if __name__ == "__main__":
    unittest.main()
