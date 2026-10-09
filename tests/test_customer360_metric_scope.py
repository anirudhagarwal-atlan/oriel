"""Calculation descriptions checked against real local CSV-to-DuckDB results.

The prose tests do not replace the existing independent numeric/source oracles.
No model, vendor or network calls are made.
"""
import copy
import json
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, reviews
from contextbase.customer360.contracts import canonical, sha256
from contextbase.customer360.metric_scope import calculation_scope
from tests import test_customer360_answers as fixtures


class CalculationScopeTests(unittest.TestCase):
    def test_large_valid_scope_is_retained_while_summary_omits_only_complete_lines(self):
        values = [("status_" + str(index) + "_").ljust(1000, "x") for index in range(50)]
        definition = fixtures.definition("large_population", operation="count", field=None,
                                         unit="records", currency_field=None,
                                         filters=[{"field": "status", "operator": "in", "value": values}])
        approved = {"candidate": {"profile": {"bindings": [{"kind": "billing", "fields": {"status": "status"}}]}}}
        definition = contexts.validate_definitions([definition], approved)[0]
        self.assertLess(len(canonical([definition]).encode()), 64_000)
        metric = {"definition": definition, "calculation_scope": calculation_scope(definition),
                  "result": {"definition_name": definition["name"], "unit": "records",
                             "values": [{"currency": None, "value": 0}]}}
        before = copy.deepcopy(metric)
        self.assertGreater(len(metric["calculation_scope"]["text"]), answers.MAX_ANSWER_CHARACTERS)
        scope = {"context": {"approved_snapshot_id": "synthetic-snapshot", "snapshot_id": "synthetic-snapshot"},
                 "payload": {"capabilities": ["billing"], "source_observed_at": []}}
        gaps = [{"origin": "current_projection", "code": "note_" + str(index),
                 "description": ("Complete synthetic note " + str(index) + " ").ljust(1900, "x")}
                for index in range(12)]
        with patch.object(answers, "_basis", return_value={}), patch.object(answers, "_gaps", return_value=gaps):
            answer = answers._render(scope, [metric], [])
        self.assertTrue(answer["rendered_truncated"])
        self.assertLessEqual(len(answer["rendered"]), answers.MAX_ANSWER_CHARACTERS)
        self.assertIn("metrics[0].calculation_scope", answer["rendered"])
        self.assertIn("Result: 0 (records).", answer["rendered"])
        self.assertIn("Some details are omitted", answer["rendered"])
        self.assertNotIn("Filters (all must match)", answer["rendered"])
        self.assertNotIn(values[0], answer["rendered"])
        self.assertEqual(answer["metrics"], [before])
        self.assertEqual(metric, before)
        self.assertEqual(answer["gaps"], gaps)
        # Every retained note is a whole line; omitted notes remain structured.
        expected = {"Current snapshot gap " + gap["code"] + ": " + gap["description"] for gap in gaps}
        displayed = [line for line in answer["rendered"].splitlines() if line.startswith("Current snapshot gap ")]
        self.assertTrue(0 < len(displayed) < len(gaps))
        self.assertTrue(all(line in expected for line in displayed))
        self.assertEqual(answer["answer_id"], sha256({key: value for key, value in answer.items() if key != "answer_id"}))

    def test_scope_excludes_business_prose_and_does_not_mutate_definition(self):
        definition = fixtures.definition("all_global_revenue", filters=[
            {"field": "status", "operator": "eq", "value": "paid"}])
        definition["description"] = "Every customer, every invoice, confirmed recurring revenue."
        original = copy.deepcopy(definition)
        scope = calculation_scope(definition)
        changed = {**definition, "name": "other_name", "description": "A completely different unverified claim."}
        self.assertEqual(scope, calculation_scope(changed))
        self.assertEqual(definition, original)
        self.assertNotIn("name", scope)
        self.assertNotIn("description", scope)
        self.assertNotIn("recurring", scope["text"])
        self.assertEqual(scope["population"], "selected_customer_resolved_records")
        self.assertIn("other customers are excluded", scope["text"])
        scope["filters"][0]["value"] = "void"
        self.assertEqual(definition, original)

    def test_filter_literals_remain_exact_typed_escaped_and_conjunctive(self):
        literals = [True, 1, 1.0, "1", None, 9007199254740993, 'quoted "value" \\ é']
        definition = fixtures.definition("typed_population", filters=[
            {"field": "status", "operator": "in", "value": literals},
            {"field": "currency", "operator": "eq", "value": "USD"}])
        scope = calculation_scope(definition)
        actual = scope["filters"][0]["value"]
        self.assertEqual([type(value) for value in actual], [type(value) for value in literals])
        self.assertEqual(actual, literals)
        for expected in ('boolean true', 'integer 1', 'number 1.0', 'string "1"', 'null',
                         'integer 9007199254740993', 'string "quoted \\"value\\" \\\\ \\u00e9"',
                         '"currency" equals string "USD"', 'Filters (all must match)'):
            self.assertIn(expected, scope["text"])
        self.assertEqual(scope["filters"], definition["filters"])
        self.assertNotEqual(calculation_scope({**definition, "filters": [{"field": "status", "operator": "eq", "value": 1}]}),
                            calculation_scope({**definition, "filters": [{"field": "status", "operator": "eq", "value": "1"}]}))

    def test_count_and_sum_units_are_explicit_without_inventing_business_meaning(self):
        count = fixtures.definition("anything", operation="count", field=None, unit="records", currency_field=None)
        result = calculation_scope(count, snapshot_id="synthetic-snapshot", customer_id="synthetic-customer")
        self.assertIn("Count records", result["text"])
        self.assertIn("Filters: none", result["text"])
        self.assertIn("No currency grouping", result["text"])
        self.assertEqual(result["snapshot_id"], "synthetic-snapshot")
        self.assertEqual(result["customer_id"], "synthetic-customer")
        for unit in ("currency", "minor_currency"):
            scope = calculation_scope(fixtures.definition("anything", unit=unit))
            self.assertIn("Unit: " + unit + ".", scope["text"])
            self.assertIn('Group separately by uppercase currency codes from field "currency".', scope["text"])
        with self.assertRaises(ValueError):
            calculation_scope({**count, "unit": "minor_currency"})
        for value in (float("nan"), {"untyped": "expression"}):
            with self.assertRaises(ValueError):
                calculation_scope(fixtures.definition("anything", filters=[{"field": "status", "operator": "eq", "value": value}]))


class ExecutedCalculationScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AnswerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.w

    def test_misleading_label_cannot_change_actual_population_or_authoritative_rendering(self):
        fixture = self.fixture
        before = fixture.metric()
        fixture.rules[0]["name"] = "global_recurring_revenue"
        description = "All customers, all invoices, and unresolved customer records are included."
        fixture.rules[0]["description"] = description
        approved = fixture.review_context()
        metric = fixture.metric("global_recurring_revenue")
        # Independent raw fixture selection; other-customer and unresolved amounts
        # and the void invoice must not enter the stated resolved/paid population.
        expected = {}
        for _, party, amount, currency, status in fixture.bills:
            if party == "party-a" and status == "paid":
                expected[currency.upper()] = expected.get(currency.upper(), 0) + amount
        self.assertEqual(metric["result"]["values"], [{"currency": code, "value": str(amount)}
                                                    for code, amount in sorted(expected.items())])
        self.assertEqual(metric["evidence_ids"], before["evidence_ids"])
        self.assertEqual(metric["queries"], before["queries"])
        self.assertEqual(metric["calculation_scope"], before["calculation_scope"])
        self.assertEqual(metric["result"]["matched_records"], 3)
        answer = fixture.validate(fixture.selection(metric))
        self.assertNotIn("rendered_truncated", answer)
        self.assertIn("Definition identifier 'global_recurring_revenue'. Calculation:", answer["rendered"])
        self.assertIn('"status" equals string "paid"', answer["rendered"])
        self.assertNotIn(description, answer["rendered"])
        self.assertIn("USD 9007199254741000", answer["rendered"])
        current = contexts.current_context(self.w, fixture.profile["name"], fixture.snapshot)
        self.assertEqual(current["definitions"], approved["definitions"])
        self.assertEqual(current["definition_sha256"], approved["definition_sha256"])
        self.assertNotIn("calculation_scope", current["definitions"][0])
        self.assertEqual(metric["result_id"], sha256({k: v for k, v in metric.items() if k != "result_id"}))

    def test_scope_tracks_snapshot_and_customer_while_reviewed_definition_stays_fixed(self):
        fixture = self.fixture
        old = fixture.metric()
        other = fixture.metric(customer=fixture.b)
        self.assertEqual(other["result"]["values"], [{"currency": "USD", "value": "2000"}])
        self.assertEqual(other["calculation_scope"]["customer_id"], fixture.b)
        self.assertEqual(old["calculation_scope"]["customer_id"], fixture.a)
        fixture.bills[1][2] = 11
        fixture.write(fixture.billing, ["invoice_id", "party_ref", "amount", "currency", "status"], fixture.bills)
        service.sync(self.w, "billing", bootstrap_context=False)
        fixture.publish()
        new = fixture.metric()
        self.assertNotEqual(old["snapshot_id"], new["snapshot_id"])
        self.assertEqual(new["calculation_scope"]["snapshot_id"], new["snapshot_id"])
        self.assertEqual(new["calculation_scope"]["customer_id"], fixture.a)
        self.assertEqual(new["definition"], old["definition"])
        self.assertEqual(new["definition_sha256"], old["definition_sha256"])
        self.assertEqual(new["context_id"], old["context_id"])
        self.assertEqual(new["calculation_scope"]["text"], old["calculation_scope"]["text"])
        self.assertEqual(new["result"]["values"], [{"currency": "EUR", "value": "13"},
                                                  {"currency": "USD", "value": "9007199254741004"}])

    def test_legacy_saved_metric_and_answer_bytes_remain_readable_after_refresh(self):
        fixture = self.fixture
        old = answers.brief(self.w, fixture.profile["name"], fixture.snapshot, fixture.a)
        legacy = copy.deepcopy(old)
        for metric in legacy["metrics"]:
            metric.pop("calculation_scope")
            metric["result_id"] = sha256({key: value for key, value in metric.items() if key != "result_id"})
        # Recreate the old renderer exactly; historical reads preserve its bytes
        # and never upgrade old labels into the new authoritative scope format.
        lines = []
        for metric in legacy["metrics"]:
            result = metric["result"]
            values = "; ".join((row["currency"] + " " if row["currency"] else "") + str(row["value"])
                               for row in result["values"])
            lines.append(f"{result['definition_name']}: {values or 'no matching records'} ({result['unit']}).")
        lines.extend(line for line in old["rendered"].splitlines() if not line.startswith("Definition identifier "))
        legacy["rendered"] = "\n".join(lines)
        legacy["answer_id"] = sha256({key: value for key, value in legacy.items() if key != "answer_id"})
        with patch.object(answers, "_brief", return_value=legacy):
            saved = reviews.save_review(self.w, fixture.profile["name"], fixture.snapshot, fixture.a)
        with self.w._connect() as conn:
            stored = conn.execute("SELECT payload FROM _contextbase.customer360_saved_reviews WHERE review_id=?",
                                  [saved["review_id"]]).fetchone()[0]
        fixture.bills[1][2] = 11
        fixture.write(fixture.billing, ["invoice_id", "party_ref", "amount", "currency", "status"], fixture.bills)
        service.sync(self.w, "billing", bootstrap_context=False)
        fixture.publish()
        with patch.object(answers, "_metric", side_effect=AssertionError("Historical reads must not recalculate")):
            loaded = reviews.load_review(self.w, saved["review_id"])
        self.assertEqual(loaded, saved)
        self.assertTrue(all("calculation_scope" not in item for item in loaded["brief"]["metrics"]))
        self.assertEqual(json.loads(stored), loaded)
        with self.w._connect() as conn:
            self.assertEqual(conn.execute("SELECT payload FROM _contextbase.customer360_saved_reviews WHERE review_id=?",
                                          [saved["review_id"]]).fetchone()[0], stored)
        compared = reviews.compare_review(self.w, saved["review_id"], fixture.snapshot)
        self.assertEqual(compared["status"], "same_reviewed_basis")
        self.assertEqual(compared["metric_changes"], [{"definition_name": "paid_value", "unit": "minor_currency",
                                                     "currency": "USD", "before": "9007199254741000",
                                                     "after": "9007199254741004", "delta": "4"}])
        self.assertIn("calculation_scope", compared["current_brief"]["metrics"][0])


if __name__ == "__main__":
    unittest.main()
