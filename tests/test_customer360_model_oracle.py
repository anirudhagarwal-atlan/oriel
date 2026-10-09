"""Oracle regressions using actual local fixture extraction; no model/vendor calls."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from contextbase.customer360 import answers, contexts, identity, profiles, samples
from contextbase.workspace import Workspace
from tests.customer360 import model_oracle as oracle


def seal(value, key):
    value[key] = oracle._hash({name: item for name, item in value.items() if name != key})
    return value


def reseal_candidate(candidate):
    candidate["definition_sha256"] = oracle._hash(candidate["definitions"])
    payload = {key: value for key, value in candidate.items() if key not in {"proposal_id", "approval_sha256", "status"}}
    candidate["proposal_id"] = candidate["approval_sha256"] = oracle._hash(payload)
    return candidate


def reseal_answer(answer):
    for metric in answer["metrics"]:
        metric["result_sha256"] = oracle._hash(metric["result"])
        metric["evidence_sha256"] = oracle._hash(metric["evidence_ids"])
        seal(metric, "result_id")
    for quote in answer["quotes"]:
        quote["text_sha256"] = oracle._hash(quote["text"])
        seal(quote, "quote_id")
    for selection in answer.get("temporal_selections", []):
        seal(selection, "selection_id")
    return seal(answer, "answer_id")


def select_quote(quote):
    keys = {"evidence_id", "field", "start", "end", "text", "quote_id"}
    if "passage_name" in quote:
        keys.update(("passage_name", "json_pointer"))
    return {key: quote[key] for key in keys}


def extra_definitions(profile):
    """Explicit test inputs, independent of the oracle's semantic allowlist."""
    entries = [("customer", "count", [], "records")]
    if profile == "billing-support":
        entries += [("billing", "count", [], "records"),
                    ("billing", "count", [{"field": "status", "operator": "eq", "value": "open"}], "records"),
                    ("billing", "sum", [], "minor_currency"),
                    ("support", "count", [], "records")]
    else:
        entries += [("commercial", "count", [], "records"),
                    ("commercial", "count", [{"field": "is_closed", "operator": "eq", "value": False}], "records"),
                    ("commercial", "sum", [], "currency"),
                    ("conversation", "count", [], "records")]
    if profile == "billing-support":
        for subset in (("paid",), ("void",), ("open", "paid"), ("open", "void"), ("paid", "void"), ("open", "paid", "void")):
            for operation, unit in (("count", "records"), ("sum", "minor_currency")):
                entries.append(("billing", operation, [{"field": "status", "operator": "in", "value": list(subset)}], unit))
        for subset in (("closed",), ("open", "closed")):
            entries.append(("support", "count", [{"field": "status", "operator": "in", "value": list(subset)}], "records"))
    else:
        for subset in ((True,), (False, True)):
            for operation, unit in (("count", "records"), ("sum", "currency")):
                entries.append(("commercial", operation, [{"field": "is_closed", "operator": "in", "value": list(subset)}], unit))
    return [{"name": "extra_measure_" + str(index), "description": "Additional synthetic meaning for independent review.",
             "kind": kind, "operation": operation, "field": "amount" if operation == "sum" else None,
             "filters": filters, "unit": unit, "currency_field": "currency" if operation == "sum" else None}
            for index, (kind, operation, filters, unit) in enumerate(entries)]


class ModelOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.runs = {}
        for profile in sorted(oracle.PROFILES):
            workspace = Workspace(Path(cls.temp.name) / profile)
            for phase in ("initial", "refreshed"):
                extraction = samples.connect(workspace, profile, phase)
                plan = samples.prepare(workspace, profile)
                for proposal in plan["links"]:
                    if proposal["status"] != "conflict":
                        identity.approve_links(workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-oracle-test")
                scope = (workspace, plan["profile"], plan["snapshot_id"])
                if phase == "initial":
                    mapping = profiles.propose_profile(workspace, plan["mapping"], plan["snapshot_id"])
                    profiles.approve_profile(workspace, mapping["proposal_id"], mapping["approval_sha256"], "synthetic-oracle-test")
                profiles.materialize(*scope)
                if phase == "initial":
                    definitions = copy.deepcopy(plan["definitions"]) + extra_definitions(profile)
                    for index, definition in enumerate(definitions):
                        definition["name"] = "proposed_measure_" + str(index)
                        definition["description"] = "Synthetic independently checked business definition."
                        if definition["filters"] and definition["filters"][0]["operator"] == "eq":
                            predicate = definition["filters"][0]
                            predicate.update(operator="in", value=[predicate["value"]])
                    candidate = contexts.propose_context(*scope, definitions)
                    context = contexts.approve_context(workspace, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-oracle-test")
                else:
                    context = contexts.current_context(*scope)
                customer = plan["customer_ids"]["customer_a"]
                customer_scope = (*scope, customer)
                metrics = [answers.execute_metric(*customer_scope, item["name"]) for item in definitions]
                # The deterministic brief selects actual projected source text,
                # while the independent oracle decides whether it is substantive.
                brief = answers.brief(*customer_scope, fields=("text", "updates"), quote_limit=20)
                selected = {"metrics": [{"definition_name": item["result"]["definition_name"], "result_id": item["result_id"]} for item in metrics],
                            "quotes": [{key: quote[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")} for quote in brief["quotes"]]}
                answer = answers.validate_answer(*customer_scope, selected)
                kind = "support" if profile == "billing-support" else "conversation"
                latest = answers.latest_evidence(*customer_scope, kind, limit=20)
                latest_answer = answers.validate_answer(*customer_scope, {
                    "metrics": selected["metrics"], "quotes": [select_quote(quote) for quote in latest["quotes"]],
                    "latest": [{"kind": kind, "limit": latest["limit"], "selection_id": latest["selection_id"]}]})
                # Independent raw-file enumeration chooses read locators only;
                # actual receipts are produced by the public scoped product API.
                _, expected, raw_records, _ = oracle._load(profile, phase, None)
                resolved = {(r["resource"], r["record_id"]) for r in expected["resolved_identities"]
                            if r["customer_id"] == "customer_a"}
                all_passages = oracle._passages(profile, raw_records, resolved, answer, extraction["source_label_mapping"])
                leaf_quotes = [answers.read_evidence(*customer_scope, p["evidence_id"], field=p["field"],
                               passage_name=p["passage_name"], json_pointer=p["json_pointer"]) for p in all_passages]
                leaf_answer = answers.validate_answer(*customer_scope, {
                    "metrics": selected["metrics"], "quotes": [select_quote(quote) for quote in leaf_quotes]})
                arguments = {"definitions": definitions, "customer_id": customer, "snapshot_id": plan["snapshot_id"],
                             "context_id": context["context_id"], "source_names": extraction["source_label_mapping"]}
                cls.runs[(profile, phase)] = {"candidate": copy.deepcopy(candidate), "answer": answer,
                    "latest_answer": latest_answer, "leaf_answer": leaf_answer,
                    "arguments": copy.deepcopy(arguments)}

    def run_data(self, profile="billing-support", phase="initial"):
        return copy.deepcopy(self.runs[(profile, phase)])

    def check(self, run, profile="billing-support", phase="initial"):
        return oracle.check_answer(profile, phase, run["answer"], **run["arguments"])

    def latest_run(self, profile="billing-support", phase="initial"):
        run = self.run_data(profile, phase)
        run["answer"] = run["latest_answer"]
        run["arguments"]["require_temporal"] = True
        return run

    def test_actual_answers_pass_both_profiles_and_phases_without_provider(self):
        for (profile, phase), run in self.runs.items():
            with self.subTest(profile=profile, phase=phase):
                review = oracle.review_definitions(profile, run["candidate"])
                receipt = self.check(run, profile, phase)
                self.assertEqual(review["decision"], "accepted_synthetic_semantics")
                self.assertEqual(review["extra_defs_need_review"], [])
                self.assertFalse(review["human_approval"])
                self.assertEqual(review["display_text_review"], "provisional_synthetic_only")
                self.assertEqual(receipt["decision"], "accepted_synthetic_answer")
                self.assertTrue(receipt["exact_currency_totals"] and receipt["latest_substantive_passage"])
                self.assertFalse(receipt["latest_selection_verified"])
                self.assertNotIn("125000", json.dumps(receipt))
                self.assertNotIn("Invoice export", json.dumps(receipt))
                self.assertRegex(receipt["receipt_sha256"], r"^[a-f0-9]{64}$")

    def test_actual_leaf_and_latest_answers_pass_both_profiles_phases(self):
        for profile, phase in self.runs:
            with self.subTest(profile=profile, phase=phase):
                run = self.latest_run(profile, phase)
                receipt = self.check(run, profile, phase)
                self.assertTrue(receipt["latest_selection_verified"])
                self.assertTrue(receipt["exact_currency_totals"])
                self.assertTrue(all("passage_name" in quote for quote in run["answer"]["quotes"]))
                run = self.run_data(profile, phase)
                run["answer"] = run["leaf_answer"]
                self.assertTrue(self.check(run, profile, phase)["latest_substantive_passage"])
                self.assertFalse(self.check(run, profile, phase)["latest_selection_verified"])

    def test_latest_requirement_cannot_be_satisfied_by_raw_or_leaf_quote_alone(self):
        for profile, phase in self.runs:
            for answer_key in ("answer", "leaf_answer"):
                run = self.run_data(profile, phase)
                run["answer"] = run[answer_key]
                run["arguments"]["require_temporal"] = True
                with self.subTest(profile=profile, phase=phase, answer=answer_key), self.assertRaisesRegex(
                        oracle.OracleMismatch, "latest_selection_required"):
                    self.check(run, profile, phase)

    def test_leaf_pointer_content_time_and_lineage_tampering_rejected_even_resealed(self):
        for profile in oracle.PROFILES:
            for phase in oracle.PHASES:
                original = self.latest_run(profile, phase)
                changes = (("passage_name", "wrong_passage"), ("json_pointer", "/wrong"),
                           ("passage_sha256", "f" * 64), ("passage_characters", 1),
                           ("field_sha256", "e" * 64))
                for key, value in changes:
                    with self.subTest(profile=profile, phase=phase, field=key):
                        run = copy.deepcopy(original)
                        run["answer"]["quotes"][0][key] = value
                        with self.assertRaises(oracle.OracleMismatch):
                            self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)
                for field, value in (("unix_seconds", "1"), ("utc", "1970-01-01T00:00:01Z"),
                                     ("encoding", "milliseconds"), ("meaning", "record_updated"),
                                     ("status", "unknown")):
                    run = copy.deepcopy(original)
                    run["answer"]["quotes"][0]["time"][field] = value
                    with self.subTest(profile=profile, phase=phase, time=field), self.assertRaisesRegex(
                            oracle.OracleMismatch, "passage_time_mismatch"):
                        self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)
                for field, value in (("source_id", "other-source"), ("path", "/updated_at"),
                                     ("field_sha256", "f" * 64), ("source", "field" if profile == "salesforce-gong" else "attachment")):
                    run = copy.deepcopy(original)
                    run["answer"]["quotes"][0]["time_lineage"][field] = value
                    with self.subTest(profile=profile, phase=phase, lineage=field), self.assertRaisesRegex(
                            oracle.OracleMismatch, "passage_lineage_mismatch"):
                        self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)

    def test_support_parent_updated_time_cannot_replace_original_or_part_time(self):
        run = self.run_data("billing-support", "refreshed")
        run["answer"] = run["leaf_answer"]
        original = next(q for q in run["answer"]["quotes"] if q["passage_name"] == "original_message")
        update = next(q for q in run["answer"]["quotes"] if q["passage_name"] == "support_update"
                      and q["source_id"] == original["source_id"])
        self.assertNotEqual(original["time"]["unix_seconds"], update["time"]["unix_seconds"])
        self.assertEqual(update["time_lineage"]["json_pointer"], "/conversation_parts/0/created_at")
        self.assertEqual(update["time_lineage"]["passage_json_pointer"], "/conversation_parts/0/body")
        changed = copy.deepcopy(run)
        quote = next(q for q in changed["answer"]["quotes"] if q["quote_id"] == original["quote_id"])
        quote["time"]["unix_seconds"] = update["time"]["unix_seconds"]
        quote["time"]["utc"] = update["time"]["utc"]
        with self.assertRaisesRegex(oracle.OracleMismatch, "passage_time_mismatch"):
            self.check({**changed, "answer": reseal_answer(changed["answer"])}, phase="refreshed")
        changed = copy.deepcopy(run)
        quote = next(q for q in changed["answer"]["quotes"] if q["quote_id"] == update["quote_id"])
        quote["time_lineage"]["json_pointer"] = quote["time_lineage"]["passage_json_pointer"]
        with self.assertRaisesRegex(oracle.OracleMismatch, "passage_lineage_mismatch"):
            self.check({**changed, "answer": reseal_answer(changed["answer"])}, phase="refreshed")

    def test_call_metadata_attachment_is_bound_without_duplicate_conversation_count(self):
        for phase in oracle.PHASES:
            original = self.latest_run("salesforce-gong", phase)
            metric = next(m for m in original["answer"]["metrics"] if m["definition"]["kind"] == "conversation")
            _, expected, _, _ = oracle._load("salesforce-gong", phase, None)
            independent_count = next(c["calls"] for c in expected["customers"] if c["customer_id"] == "customer_a")
            self.assertEqual(metric["result"]["values"], [{"currency": None, "value": independent_count}])
            for key, value in (("source_row_sha256", "f" * 64), ("source_table", "other.extensive_calls"),
                               ("source_id", "wrong-call"), ("snapshot_id", "wrong-snapshot"), ("status", "unknown")):
                run = copy.deepcopy(original)
                run["answer"]["quotes"][0]["time_lineage"]["attachment_lineage"][key] = value
                with self.subTest(phase=phase, key=key), self.assertRaisesRegex(oracle.OracleMismatch, "passage_lineage_mismatch"):
                    self.check({**run, "answer": reseal_answer(run["answer"])}, "salesforce-gong", phase)
            for key in ("local_key", "remote_key", "customer_ref"):
                run = copy.deepcopy(original)
                run["answer"]["quotes"][0]["time_lineage"]["attachment_lineage"][key]["value"] = "another-call"
                with self.subTest(phase=phase, key=key), self.assertRaisesRegex(oracle.OracleMismatch, "passage_lineage_mismatch"):
                    self.check({**run, "answer": reseal_answer(run["answer"])}, "salesforce-gong", phase)

    def test_latest_proof_population_clock_coverage_and_nested_quote_checked(self):
        for profile, phase in self.runs:
            for kind in ("candidates", "time", "ties", "ties_count", "coverage", "scope", "quote", "duplicate"):
                run = self.latest_run(profile, phase)
                selected = run["answer"]["temporal_selections"][0]
                if kind == "candidates": selected["candidates_sha256"] = oracle._hash([])
                elif kind == "time": selected["latest_time"] = {"unix_seconds": "0", "utc": "1970-01-01T00:00:00Z"}
                elif kind == "ties": selected["ties"] = []
                elif kind == "ties_count": selected["ties_count"] = True
                elif kind == "coverage": selected["coverage"]["complete"] = False
                elif kind == "scope": selected["customer_id"] = "another-customer"
                elif kind == "quote": selected["quotes"][0]["text"] = "fabricated nested evidence"
                else: run["answer"]["temporal_selections"].append(copy.deepcopy(selected))
                with self.subTest(profile=profile, phase=phase, mutation=kind), self.assertRaises(oracle.OracleMismatch):
                    self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)

    def test_resealed_old_selection_cannot_pass_new_snapshot(self):
        for profile in oracle.PROFILES:
            current = self.latest_run(profile, "refreshed")
            old = self.latest_run(profile, "initial")["answer"]["temporal_selections"][0]
            for key in oracle.BASIS:
                old[key] = current["answer"][key]
            current["answer"]["temporal_selections"] = [old]
            with self.assertRaises(oracle.OracleMismatch):
                self.check({**current, "answer": reseal_answer(current["answer"])}, profile, "refreshed")

    def test_independent_source_clock_preserves_offsets_and_subseconds(self):
        got = oracle._source_time("1970-01-01T01:00:00.123456789+01:00", "rfc3339", "event_started")
        self.assertEqual(got["unix_seconds"], "0.123456789")
        self.assertEqual(got["utc"], "1970-01-01T00:00:00.123456789Z")
        self.assertEqual(oracle._source_time("-0.000000001", "unix_seconds", "passage_created")["utc"],
                         "1969-12-31T23:59:59.999999999Z")

    def test_every_explicit_extra_is_reviewed_and_checked_against_raw_records(self):
        expected = {
            "billing-support": {"all_customer_records", "all_invoice_records", "open_invoice_records",
                                "all_remaining_invoice_minor_units", "remaining_open_invoice_minor_units",
                                "all_support_records", "open_support_records"},
            "salesforce-gong": {"all_customer_records", "all_opportunity_records", "open_opportunity_records",
                                "all_opportunity_currency", "open_opportunity_currency", "all_conversation_records"},
        }
        for (profile, phase), run in self.runs.items():
            with self.subTest(profile=profile, phase=phase):
                review = oracle.review_definitions(profile, run["candidate"])
                self.assertTrue(expected[profile].issubset({item["semantic"] for item in review["reviewed_definitions"]}))
                self.assertEqual(sum(item["required"] for item in review["reviewed_definitions"]), 1)
                self.assertEqual(self.check(run, profile, phase)["checked_metrics"], 21 if profile == "billing-support" else 10)

    def test_names_and_equivalent_singleton_filters_may_vary(self):
        for profile in oracle.PROFILES:
            candidate = self.run_data(profile)["candidate"]
            original = copy.deepcopy(candidate)
            for index, definition in enumerate(candidate["definitions"]):
                definition["name"] = "renamed_measure_" + str(index)
                definition["description"] = "Another synthetic display description."
                if definition["filters"]:
                    predicate = definition["filters"][0]
                    if len(predicate["value"]) == 1:
                        predicate.update(operator="eq", value=predicate["value"][0])
                    else:
                        predicate["value"].reverse()
            reseal_candidate(candidate)
            receipt = oracle.review_definitions(profile, candidate)
            self.assertEqual(receipt["candidate_sha256"], oracle._hash(candidate))
            self.assertNotEqual(receipt["candidate_sha256"], oracle._hash(original))

    def test_untouched_candidate_and_definition_hashes_are_required(self):
        for key in ("approval_sha256", "definition_sha256"):
            candidate = self.run_data()["candidate"]
            candidate[key] = "f" * 64
            with self.assertRaises(oracle.OracleMismatch):
                oracle.review_definitions("billing-support", candidate)

    def test_unknown_extra_definition_is_not_silently_approved(self):
        candidate = self.run_data()["candidate"]
        extra = copy.deepcopy(candidate["definitions"][0])
        extra.update(name="unchecked_extra", description="Unreviewed synthetic description",
                     filters=[{"field": "currency", "operator": "eq", "value": "USD"}])
        candidate["definitions"].append(extra)
        reseal_candidate(candidate)
        with self.assertRaises(oracle.OracleMismatch) as caught:
            oracle.review_definitions("billing-support", candidate)
        self.assertEqual(caught.exception.code, "extra_definition_needs_review")
        self.assertEqual(caught.exception.review_receipt["extra_defs_need_review"], [{"name": "unchecked_extra", "definition_sha256": oracle._hash(extra)}])
        self.assertNotIn(extra["description"], json.dumps(caught.exception.review_receipt))

    def test_wrong_population_unit_currency_and_typed_boolean_refused(self):
        for profile in oracle.PROFILES:
            for change in ({"filters": []}, {"unit": "number", "currency_field": None}, {"currency_field": "status"},
                           {"field": "amount_due"}, {"operation": "count"}, {"filters": [{"field": "currency", "operator": "eq", "value": "USD"}]}):
                with self.subTest(profile=profile, change=change):
                    candidate = self.run_data(profile)["candidate"]
                    candidate["definitions"] = candidate["definitions"][:1]
                    candidate["definitions"][0].update(change)
                    reseal_candidate(candidate)
                    with self.assertRaises(oracle.OracleMismatch) as caught:
                        oracle.review_definitions(profile, candidate)
                    self.assertEqual(caught.exception.code, "required_currency_sum_missing")
        for value in (0, "false", [False, True]):
            candidate = self.run_data("salesforce-gong")["candidate"]
            candidate["definitions"][0]["filters"] = [{"field": "is_closed", "operator": "in" if isinstance(value, list) else "eq", "value": value}]
            with self.assertRaises(oracle.OracleMismatch):
                oracle.review_definitions("salesforce-gong", reseal_candidate(candidate))

    def test_extra_definitions_cannot_broaden_typed_filters_units_or_currency(self):
        for profile in oracle.PROFILES:
            original = self.run_data(profile)["candidate"]
            for index, definition in enumerate(original["definitions"]):
                if index == 0:
                    continue
                changes = [{"unit": "number"}, {"currency_field": "label"}, {"field": "label"},
                           {"filters": [{"field": "label", "operator": "eq", "value": "customer_a"}]}]
                if definition["filters"]:
                    field = definition["filters"][0]["field"]
                    changes += [{"filters": [{"field": field, "operator": "eq", "value": 0}]}]
                for change in changes:
                    with self.subTest(profile=profile, index=index, change=change):
                        candidate = copy.deepcopy(original)
                        candidate["definitions"][index].update(change)
                        with self.assertRaisesRegex(oracle.OracleMismatch, "extra_definition_needs_review"):
                            oracle.review_definitions(profile, reseal_candidate(candidate))

    def test_duplicate_meaning_and_missing_required_selected_metric_refused(self):
        for profile in oracle.PROFILES:
            candidate = self.run_data(profile)["candidate"]
            duplicate = copy.deepcopy(candidate["definitions"][-1])
            duplicate["name"] = "duplicate_meaning"
            candidate["definitions"].append(duplicate)
            with self.assertRaisesRegex(oracle.OracleMismatch, "duplicate_semantic_definition"):
                oracle.review_definitions(profile, reseal_candidate(candidate))
            run = self.run_data(profile)
            run["answer"]["metrics"] = run["answer"]["metrics"][1:]
            with self.assertRaisesRegex(oracle.OracleMismatch, "required_currency_sum_missing"):
                self.check({**run, "answer": reseal_answer(run["answer"])}, profile)

    def test_selected_metric_definition_preserves_boolean_filter_type(self):
        run = self.run_data("salesforce-gong")
        # Python dict equality treats False and 0 as equal; the typed reviewed
        # definition and its result must still differ under that substitution.
        run["answer"]["metrics"][0]["definition"]["filters"][0]["value"] = [0]
        with self.assertRaisesRegex(oracle.OracleMismatch, "unreviewed_metric_definition"):
            self.check({**run, "answer": reseal_answer(run["answer"])}, "salesforce-gong")

    def test_status_subsets_reject_unknown_duplicate_empty_and_mixed_typed_values(self):
        cases = {
            "billing-support": ("billing", "status", [[], ["paid", "paid"], ["open", "archived"], ["open", 0], [None], [True]]),
            "salesforce-gong": ("commercial", "is_closed", [[], [False, False], [False, 0], [True, 1], ["false"], [None]]),
        }
        for profile, (kind, field, values) in cases.items():
            for subset in values:
                with self.subTest(profile=profile, subset=subset):
                    candidate = self.run_data(profile)["candidate"]
                    candidate["definitions"] = candidate["definitions"][:1]
                    candidate["definitions"].append({"name": "unchecked_partition", "description": "Synthetic bounded partition.",
                        "kind": kind, "operation": "count", "field": None, "unit": "records", "currency_field": None,
                        "filters": [{"field": field, "operator": "in", "value": subset}]})
                    with self.assertRaisesRegex(oracle.OracleMismatch, "extra_definition_needs_review"):
                        oracle.review_definitions(profile, reseal_candidate(candidate))

    def test_status_union_cannot_replace_required_open_only_sum(self):
        for profile, values in (("billing-support", ["open", "paid"]), ("salesforce-gong", [False, True])):
            candidate = self.run_data(profile)["candidate"]
            candidate["definitions"] = candidate["definitions"][:1]
            candidate["definitions"][0]["filters"][0]["value"] = values
            with self.assertRaisesRegex(oracle.OracleMismatch, "required_currency_sum_missing"):
                oracle.review_definitions(profile, reseal_candidate(candidate))

    def test_empty_partition_and_matched_zero_sum_have_distinct_receipts(self):
        initial, refreshed = self.run_data(), self.run_data(phase="refreshed")
        empty = next(metric for metric in initial["answer"]["metrics"]
                     if metric["definition"]["operation"] == "sum" and not metric["result"]["matched_records"])
        zero = next(metric for metric in refreshed["answer"]["metrics"]
                    if metric["definition"]["name"] == empty["definition"]["name"])
        self.assertEqual(empty["result"]["status"], "no_matching_records")
        self.assertEqual(empty["result"]["values"], [])
        self.assertEqual(empty["evidence_ids"], [])
        self.assertEqual(zero["result"]["status"], "computed")
        self.assertTrue(zero["evidence_ids"])
        self.assertEqual(zero["result"]["values"], [{"currency": "USD", "value": "0"}])
        for phase, run, status in (("initial", initial, "computed"), ("refreshed", refreshed, "no_matching_records")):
            self.assertEqual(self.check(run, phase=phase)["decision"], "accepted_synthetic_answer")
            changed = copy.deepcopy(run)
            metric = next(item for item in changed["answer"]["metrics"] if item["definition"]["name"] == empty["definition"]["name"])
            metric["result"]["status"] = status
            with self.assertRaisesRegex(oracle.OracleMismatch, "metric_population_mismatch"):
                self.check({**changed, "answer": reseal_answer(changed["answer"])}, phase=phase)

    def test_all_optional_metric_values_and_populations_are_checked(self):
        for profile in oracle.PROFILES:
            for phase in ("initial", "refreshed"):
                original = self.run_data(profile, phase)
                for index in range(1, len(original["answer"]["metrics"])):
                    with self.subTest(profile=profile, phase=phase, index=index):
                        for mutation in ("value", "population", "evidence"):
                            run = copy.deepcopy(original)
                            metric = run["answer"]["metrics"][index]
                            if mutation == "value":
                                if metric["result"]["values"]:
                                    value = metric["result"]["values"][0]
                                    value["value"] = value["value"] + 1 if type(value["value"]) is int else "999999999"
                                else:
                                    metric["result"]["values"] = [{"currency": "USD", "value": "0"}]
                            elif mutation == "population":
                                metric["result"]["matched_records"] += 1
                            else:
                                metric["evidence_ids"] = sorted([*metric["evidence_ids"], "f" * 64])
                            with self.assertRaises(oracle.OracleMismatch):
                                self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)
                run = copy.deepcopy(original)
                count = next(metric for metric in run["answer"]["metrics"] if metric["definition"]["kind"] == "customer")
                count["result"]["values"][0]["value"] = True
                with self.assertRaisesRegex(oracle.OracleMismatch, "count_mismatch"):
                    self.check({**run, "answer": reseal_answer(run["answer"])}, profile, phase)

    def test_same_totals_with_wrong_evidence_population_refused(self):
        for key, value in (("matched_records", 999), ("unit", "currency")):
            run = self.run_data()
            run["answer"]["metrics"][0]["result"][key] = value
            with self.assertRaisesRegex(oracle.OracleMismatch, "metric_population_mismatch"):
                self.check({**run, "answer": reseal_answer(run["answer"])})
        run = self.run_data()
        run["answer"]["metrics"][0]["evidence_ids"][0] = "f" * 64
        with self.assertRaisesRegex(oracle.OracleMismatch, "metric_evidence_mismatch"):
            self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_wrong_missing_duplicate_and_inexact_currency_totals_refused(self):
        values = [[{"currency": "USD", "value": "155000"}],
                  [{"currency": "USD", "value": "125000"}, {"currency": "EUR", "value": "30001"}],
                  [{"currency": "USD", "value": "125000"}, {"currency": "EUR", "value": "30000"}, {"currency": "EUR", "value": "30000"}],
                  [{"currency": "USD", "value": 125000.0}, {"currency": "EUR", "value": "30000"}],
                  [{"currency": "USD", "value": "NaN"}, {"currency": "EUR", "value": "30000"}]]
        for rows in values:
            run = self.run_data()
            run["answer"]["metrics"][0]["result"]["values"] = rows
            with self.assertRaises(oracle.OracleMismatch):
                self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_quote_source_customer_field_and_receipt_attribution_refused(self):
        for key, value in (("source_id", "ic_conversation_2"), ("connection", "wrong_source"),
                           ("entity", "companies"), ("kind", "customer"), ("record_key", "f" * 64),
                           ("evidence_id", "f" * 64), ("field", "status"), ("customer_id", "wrong_customer")):
            with self.subTest(key=key):
                run = self.run_data()
                run["answer"]["quotes"][0][key] = value
                with self.assertRaises(oracle.OracleMismatch):
                    self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_quote_exact_text_offsets_and_hashes_checked(self):
        for key, value in (("text", "A fabricated synthetic passage."), ("start", 1), ("field_sha256", "f" * 64),
                           ("field_characters", 1), ("is_partial_quote", True)):
            run = self.run_data()
            run["answer"]["quotes"][0][key] = value
            with self.assertRaises(oracle.OracleMismatch):
                self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_punctuation_is_not_a_substantive_passage(self):
        run = self.run_data()
        quote = run["answer"]["quotes"][0]
        quote.update(end=quote["start"] + 1, text=quote["text"][:1], is_partial_quote=True)
        run["answer"]["quotes"] = [quote]
        with self.assertRaisesRegex(oracle.OracleMismatch, "latest_substantive_passage_missing"):
            self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_refresh_requires_changed_substantive_evidence(self):
        for profile in oracle.PROFILES:
            run = self.run_data(profile, "refreshed")
            # Keep genuine current-snapshot quotes of unchanged old text. They
            # are valid product evidence but do not satisfy the refreshed task.
            run["answer"]["quotes"] = [quote for quote in run["answer"]["quotes"]
                                       if "Invoice export is failing" in quote["text"] or "SSO rollout is blocked" in quote["text"]]
            self.assertTrue(run["answer"]["quotes"])
            with self.assertRaisesRegex(oracle.OracleMismatch, "latest_substantive_passage_missing"):
                self.check({**run, "answer": reseal_answer(run["answer"])}, profile, "refreshed")

    def test_old_scope_and_old_answer_cannot_pass_as_refreshed(self):
        for profile in oracle.PROFILES:
            old, fresh = self.run_data(profile), self.run_data(profile, "refreshed")
            fresh["answer"] = old["answer"]
            with self.assertRaisesRegex(oracle.OracleMismatch, "answer_scope_mismatch"):
                self.check(fresh, profile, "refreshed")
            fresh = self.run_data(profile, "refreshed")
            fresh["answer"]["metrics"][0]["snapshot_id"] = old["arguments"]["snapshot_id"]
            with self.assertRaisesRegex(oracle.OracleMismatch, "result_scope_mismatch"):
                self.check({**fresh, "answer": reseal_answer(fresh["answer"])}, profile, "refreshed")

    def test_missing_required_metric_quotes_and_abstention_do_not_pass(self):
        for key, value in (("metrics", []), ("quotes", []), ("abstention", {"code": "insufficient_evidence"}),
                           ("model_generated_factual_prose", True)):
            run = self.run_data()
            run["answer"][key] = value
            with self.assertRaises(oracle.OracleMismatch):
                self.check({**run, "answer": reseal_answer(run["answer"])})

    def test_saved_expected_values_must_match_independent_raw_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture"
            shutil.copytree(oracle.ROOT, fixture)
            expected = json.loads((fixture / "expected.json").read_text())
            expected["profiles"]["billing-support"]["initial"]["customers"][0]["amounts_by_currency"]["USD"] += 1
            (fixture / "expected.json").write_text(json.dumps(expected))
            with self.assertRaisesRegex(oracle.OracleMismatch, "independent_fixture_expectation_changed"):
                oracle.review_definitions("billing-support", self.run_data()["candidate"], fixture_root=fixture)


if __name__ == "__main__":
    unittest.main()
