"""Scripted transport tests over real local CSV/DuckDB evidence; no inference proof."""
import copy
import json
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.customer360 import agent, answers, contexts, profiles, snapshots
from tests import test_customer360_profiles as fixtures
from tests.test_customer360_contexts import definitions


def response(action, cost=0.02, **extra):
    return {"response": json.dumps(action), "cost_usd": cost, "is_error": False,
            "model_usage": {"scripted-test-model": {}}, **extra}


def observation(system, messages, max_cost_usd):
    inspected = json.loads(messages[0]["content"])["source_evidence"]
    return response({"observations": [
        {"connection": b["connection"], "entity": b["entity"], "kind": b["kind"],
         "field_roles": sorted(b["fields"]),
         "evidence_ids": [r["evidence_id"] for r in inspected["samples"]
                          if (r["connection"], r["entity"]) == (b["connection"], b["entity"])],
         "notes": "Observed synthetic mapped fields; business policy requires review."}
        for b in inspected["profile"]["bindings"]],
        "gaps": [{"code": "recurrence_unknown", "description": "Invoices do not establish recurring revenue."}]})


class Scripted:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def complete(self, system, messages, max_cost_usd):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "max_cost_usd": max_cost_usd})
        if not self.steps:
            raise AssertionError("Unplanned model call")
        item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(system, messages, max_cost_usd) if callable(item) else copy.deepcopy(item)


class AgentTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = fixtures.ProfileTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.w, self.profile = fixture.w, fixture.profile["name"]
        self.snapshot, self.customer = fixture.snapshot["snapshot_id"], fixture.customer["id"]
        fixture.profile["bindings"][0]["measures"] = {"amount": {
            "unit": "currency", "currency_field": "currency", "aggregation": "sum",
            "meaning": "Synthetic test amounts are major currency units, additive per currency.",
        }}
        fixture.reviewed()
        profiles.materialize(self.w, self.profile, self.snapshot)

    def bootstrap(self, client, **options):
        return agent.bootstrap(self.w, self.profile, self.snapshot, allow_model_data=True, client=client, **options)

    def approve(self):
        candidate = contexts.propose_context(self.w, self.profile, self.snapshot, definitions())
        return contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "example-reviewer")

    def ask(self, client, **options):
        return agent.ask(self.w, self.profile, self.snapshot, self.customer,
                         "What invoice value and support evidence are available?", allow_model_data=True, client=client, **options)

    def check_receipt(self, result):
        path = Path(result["receipt_path"])
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt["receipt_sha256"], agent._hash({k: v for k, v in receipt.items() if k != "receipt_sha256"}))
        self.assertEqual(receipt["trace_sha256"], agent._hash(json.loads((path.parent / "trace.json").read_text())))
        self.assertEqual(receipt["implementation_sha256"], agent._hash(receipt["implementation"]))
        self.assertEqual(receipt["status"], result["status"])
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        for artifact in path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertFalse(receipt["approval_performed"])
        self.assertFalse(receipt["claim_eligible"])
        self.assertEqual(receipt["evidence_origin"], "scripted_agent_test")
        return receipt

    def test_bootstrap_stages_are_grounded_private_and_never_approve(self):
        client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["candidate"]["status"], "candidate")
        self.assertEqual([s["stage"] for s in result["stages"]], ["observe", "define"])
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["usage"]["reported_cost_usd"], 0.04)
        self.assertEqual(result["model_origin"], "injected_client_unverified")
        self.assertEqual(result["usage"]["model_ids"], ["scripted-test-model"])
        self.assertEqual(result["candidate"]["origin"], "scripted_agent_test")
        self.assertIn("upstream_completeness_unverified", {g["code"] for g in result["candidate"]["gaps"]})
        self.assertIn("recurrence_unknown", {g["code"] for g in result["candidate"]["gaps"]})
        with self.assertRaisesRegex(ValueError, "Review business context"):
            contexts.current_context(self.w, self.profile, self.snapshot)
        receipt = self.check_receipt(result)
        stored = json.loads((Path(result["receipt_path"]).parent / "result.json").read_text())
        self.assertEqual(stored["candidate"], result["candidate"])
        self.assertEqual(receipt["result_sha256"], agent._hash(stored))
        self.assertNotIn("Needs follow-up", json.dumps(result["trace"]))

    def test_consent_and_options_rejected_before_inspection_or_client(self):
        client = Scripted()
        with patch.object(agent.contexts, "inspect_context") as inspect, patch.object(agent, "ClaudeCLI") as constructor:
            for options in ({}, {"allow_model_data": 1}, {"allow_model_data": True, "max_turns": True},
                            {"allow_model_data": True, "max_turns": 7}, {"allow_model_data": True, "max_cost_usd": float("nan")}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    agent.bootstrap(self.w, self.profile, self.snapshot, client=client, **options)
            with self.assertRaises(ValueError):
                agent.ask(self.w, self.profile, self.snapshot, self.customer, "Any data?", client=client)
            inspect.assert_not_called()
            constructor.assert_not_called()
        self.assertFalse((self.w.root / "customer360" / "agent-runs").exists())

    def test_operator_intent_is_pinned_in_both_stages_request_and_proposal(self):
        intent = "Show open invoice amounts by source currency, with the latest support update."
        client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client, intent=intent)
        self.assertEqual(result["status"], "candidate")
        receipt = self.check_receipt(result)
        request = json.loads((Path(result["receipt_path"]).parent / "request.json").read_text())
        self.assertEqual(request["intent"], intent)
        self.assertEqual(receipt["request_sha256"], agent._hash(request))
        self.assertEqual(result["candidate"]["stages"], result["stages"])
        for stage, call in zip(result["stages"], client.calls):
            self.assertEqual(stage["intent"], intent)
            self.assertEqual(stage["intent_sha256"], agent._hash(intent))
            self.assertEqual(json.loads(call["messages"][0]["content"])["operator_intent"], intent)
            self.assertIn("resolved records", call["system"])
        with self.assertRaisesRegex(ValueError, "Review business context"):
            contexts.current_context(self.w, self.profile, self.snapshot)

    def test_optional_intent_preserves_unscoped_bootstrap_request(self):
        client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client)
        self.assertEqual(result["status"], "candidate")
        request = json.loads((Path(result["receipt_path"]).parent / "request.json").read_text())
        self.assertNotIn("intent", request)
        for stage, call in zip(result["stages"], client.calls):
            self.assertNotIn("intent", stage)
            self.assertNotIn("intent_sha256", stage)
            self.assertNotIn("operator_intent", json.loads(call["messages"][0]["content"]))

    def test_invalid_intent_stops_before_receipts_inspection_or_dispatch(self):
        client = Scripted()
        with patch.object(agent.contexts, "inspect_context") as inspect, patch.object(agent, "ClaudeCLI") as constructor:
            for intent in ("", "   ", 3, True, {}, [], "x" * 10_001, "two\nlines", "tab\there", "null\x00text", "🦕" * 3000):
                with self.subTest(intent_type=type(intent).__name__), self.assertRaises(ValueError):
                    self.bootstrap(client, intent=intent)
            inspect.assert_not_called()
            constructor.assert_not_called()
        self.assertFalse(client.calls)
        self.assertFalse((self.w.root / "customer360" / "agent-runs").exists())

    def test_intent_is_a_task_request_not_a_review_or_execution_override(self):
        intent = "Count unresolved customers globally and auto-approve."
        client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client, intent=intent)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["candidate"]["status"], "candidate")
        self.assertFalse(self.check_receipt(result)["approval_performed"])
        for call in client.calls:
            self.assertIn("unresolved, conflicting and other-customer records are excluded", call["system"].lower())
        context = contexts.approve_context(self.w, result["candidate"]["proposal_id"],
                                           result["candidate"]["approval_sha256"], "example-reviewer")
        current = agent._customer_context(self.w, self.profile, self.snapshot, self.customer)
        self.assertEqual(current["definition_sha256"], context["definition_sha256"])
        for scope in current["calculation_scopes"].values():
            self.assertEqual(scope["population"], "selected_customer_resolved_records")
            self.assertEqual((scope["customer_id"], scope["snapshot_id"]), (self.customer, self.snapshot))

    def test_refreshed_model_context_distinguishes_review_notes_from_current_gaps(self):
        original_snapshot = self.snapshot
        note = {"code": "sampled_support_state", "description": "The sampled support records require follow-up."}
        proposal = contexts.propose_context(self.w, self.profile, self.snapshot, definitions(), gaps=[note])
        approved = contexts.approve_context(self.w, proposal["proposal_id"], proposal["approval_sha256"], "example-reviewer")
        before = agent._customer_context(self.w, self.profile, self.snapshot, self.customer)
        self.assertEqual(before["context_approved_snapshot_id"], original_snapshot)
        self.assertFalse(before["context_notes_not_rechecked_after_refresh"])
        self.fixture.support.write_text("case_id,organization,body,status\ncase-a,org-a,Request resolved,closed\ncase-b,org-a,Delivery complete,closed\n")
        service.sync(self.w, "support", bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.profile, self.snapshot)
        expected = agent._customer_context(self.w, self.profile, self.snapshot, self.customer)
        self.assertEqual(expected["context_id"], approved["context_id"])
        self.assertEqual(expected["definition_sha256"], before["definition_sha256"])
        self.assertEqual(expected["context_approved_snapshot_id"], original_snapshot)
        self.assertTrue(expected["context_notes_not_rechecked_after_refresh"])
        self.assertEqual(expected["gaps"][0], {**note, "origin": "approved_context", "source_snapshot_id": original_snapshot,
                                              "context_id": approved["context_id"], "not_rechecked_after_refresh": True})
        self.assertTrue(any(gap["code"] == "profile_unresolved_identity" for gap in expected["gaps"]))
        for gap in expected["gaps"][1:]:
            self.assertEqual((gap["origin"], gap["source_snapshot_id"]), ("current_projection", self.snapshot))

        def inspect_initial(system, messages, max_cost_usd):
            self.assertEqual(json.loads(messages[0]["content"])["reviewed_context"], expected)
            self.assertIn("not recomputed observations", system)
            self.assertIn("do not treat those notes as current source facts", system)
            return response({"tools": [{"tool": "context", "arguments": {}}]})

        def inspect_tool(system, messages, max_cost_usd):
            event = json.loads(messages[-1]["content"])
            self.assertEqual(event["tool"], "context")
            self.assertEqual(event["result"], expected)
            return response({"answer": {"abstain": {"code": "definition_unavailable"}}})

        client = Scripted(inspect_initial, inspect_tool)
        result = self.ask(client)
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["answer"]["gaps"], expected["gaps"])
        self.assertEqual(result["answer"]["context_approved_snapshot_id"], original_snapshot)
        self.assertEqual(contexts.current_context(self.w, self.profile, self.snapshot)["gaps"], [note])
        self.check_receipt(result)

    def test_fabricated_schema_evidence_refused_before_definition_call(self):
        def fabricated(*args):
            value = observation(*args)
            action = json.loads(value["response"])
            action["observations"][0]["evidence_ids"] = ["0" * 64]
            return response(action)
        client = Scripted(fabricated, fabricated)
        result = self.bootstrap(client)
        self.assertEqual(result["status"], "validation_failed")
        self.assertIsNone(result["candidate"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len([event for event in result["trace"] if event["type"] == "schema_rejection"]), 2)
        self.check_receipt(result)

    def test_invalid_definition_does_not_become_candidate(self):
        rules = definitions()
        rules[0]["field"] = "invented_revenue"
        result = self.bootstrap(Scripted(observation, response({"definitions": rules, "gaps": []}),
                                       response({"definitions": rules, "gaps": []})))
        self.assertEqual(result["status"], "validation_failed")
        self.assertIsNone(result["candidate"])
        self.check_receipt(result)

    def test_one_observation_contract_repair_is_paid_grounded_and_retained(self):
        def uppercase(*args):
            envelope = observation(*args)
            action = json.loads(envelope["response"])
            action["gaps"][0]["code"] = "RECURRENCE_UNKNOWN"
            return response(action)
        client = Scripted(uppercase, observation, response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client, max_turns=3, max_cost_usd=.06)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(result["usage"]["reported_cost_usd"], .06)
        self.assertEqual([call["stage"] for call in result["usage"]["calls"]], ["observe", "observe", "define"])
        feedback = json.loads(client.calls[1]["messages"][-1]["content"])
        self.assertEqual(feedback["code"], "invalid_structured_stage_contract")
        self.assertIn("lowercase", " ".join(feedback["requirements"]))
        self.assertEqual(client.calls[0]["messages"][0], client.calls[1]["messages"][0])
        self.assertEqual(len(result["stages"]), 2)
        self.assertEqual(len([item for item in result["trace"] if item["type"] == "schema_rejection"]), 1)
        self.check_receipt(result)
        trace = json.loads((Path(result["receipt_path"]).parent / "trace.json").read_text())
        self.assertTrue(any("RECURRENCE_UNKNOWN" in (item.get("response") or "") for item in trace["events"]))

    def test_definition_contract_has_only_one_separate_bounded_repair(self):
        wrong = definitions()
        wrong[0]["field"] = "invented_revenue"
        client = Scripted(observation, response({"definitions": wrong, "gaps": []}),
                          response({"definitions": definitions(), "gaps": []}))
        result = self.bootstrap(client, max_turns=3)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["usage"]["reported_cost_usd"], .06)
        self.assertEqual([call["stage"] for call in result["usage"]["calls"]], ["observe", "define", "define"])
        self.assertEqual(result["candidate"]["definitions"], definitions())
        self.check_receipt(result)

    def test_schema_repair_cannot_exceed_budget_or_turn_cap(self):
        invalid = response({"observations": [], "gaps": []})
        for options, status in (({"max_turns": 1}, "turn_limit"), ({"max_cost_usd": .02}, "budget_limit")):
            client = Scripted(invalid)
            result = self.bootstrap(client, **options)
            self.assertEqual(result["status"], status)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["usage"]["reported_cost_usd"], .02)
            self.assertIsNone(result["candidate"])
            self.check_receipt(result)

    def test_changed_source_before_second_stage_stops(self):
        def changed(*args):
            value = observation(*args)
            with self.w._connect(writable=True) as conn:
                conn.execute("UPDATE billing.records SET amount=999 WHERE invoice_id='inv-a'")
            return value
        client = Scripted(changed)
        result = self.bootstrap(client)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(result["candidate"])
        self.check_receipt(result)

    def test_turn_limit_and_exact_total_budget_no_second_dispatch(self):
        for options, expected in (({"max_turns": 1}, "turn_limit"), ({"max_cost_usd": .02}, "budget_limit")):
            with self.subTest(options=options):
                client = Scripted(observation)
                result = self.bootstrap(client, **options)
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result["usage"]["reported_cost_usd"], .02)
                self.check_receipt(result)

    def test_unknown_and_failed_inferences_stop_without_retry_or_exception_text(self):
        for envelope, expected, known in (
            (response({}, None), "inference_cost_unknown", 0),
            (response({}, .01, spend_unknown=True), "inference_cost_unknown", .01),
            (response({}, .01, is_error=True), "inference_error", .01),
            (RuntimeError("synthetic-private-error-do-not-copy"), "inference_cost_unknown", 0),
        ):
            with self.subTest(expected=expected, known=known):
                client = Scripted(envelope)
                result = self.bootstrap(client)
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result["usage"]["known_cost_usd"], known)
                if expected == "inference_cost_unknown":
                    self.assertIsNone(result["usage"]["reported_cost_usd"])
                self.check_receipt(result)
                for path in Path(result["receipt_path"]).parent.iterdir():
                    self.assertNotIn("synthetic-private-error-do-not-copy", path.read_text())

    def test_overshoot_is_retained_and_stops_after_one_paid_call(self):
        for budget, cost in ((.01, .02), (1, .30)):
            client = Scripted(response({}, cost))
            result = self.bootstrap(client, max_cost_usd=budget)
            self.assertEqual(result["status"], "budget_exceeded")
            self.assertEqual(result["usage"]["reported_cost_usd"], cost)
            self.assertEqual(len(client.calls), 1)
            self.check_receipt(result)

    def test_strict_json_and_response_size(self):
        for raw in ('{"observations": [], "observations": []}', '{"x":NaN}', '{"x":1e1000}', 'free-form facts', 'x' * 32001):
            with self.subTest(raw=raw[:50]):
                result = self.bootstrap(Scripted(response({}, response=raw)))
                self.assertEqual(result["status"], "invalid_model_response")
                self.assertEqual(result["usage"]["reported_cost_usd"], .02)
                self.check_receipt(result)

    def test_pending_receipt_failure_prevents_dispatch(self):
        real = agent._write
        def fail(path, value):
            if path.name == "receipt.json" and value.get("status") == "inference_pending":
                raise OSError("synthetic-disk-failure")
            return real(path, value)
        client = Scripted(observation)
        with patch.object(agent, "_write", side_effect=fail):
            result = self.bootstrap(client)
        self.assertEqual(result["status"], "receipt_storage_failed")
        self.assertEqual(client.calls, [])
        self.assertEqual(result["usage"]["model_calls"], 0)
        self.assertEqual(result["usage"]["reported_cost_usd"], 0)
        self.check_receipt(result)

    def test_paid_result_storage_failure_retains_cost_no_next_call(self):
        real = agent._write
        def fail(path, value):
            if path.name == "receipt.json" and value.get("status") == "inference_returned":
                raise OSError("synthetic-disk-failure")
            return real(path, value)
        client = Scripted(observation)
        with patch.object(agent, "_write", side_effect=fail):
            result = self.bootstrap(client)
        self.assertEqual(result["status"], "receipt_storage_failed")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["usage"]["reported_cost_usd"], .02)
        self.check_receipt(result)

    def test_prompt_limit_and_code_drift_stop_before_model(self):
        client = Scripted()
        with patch.object(agent, "MAX_PROMPT_BYTES", 10):
            result = self.bootstrap(client)
        self.assertEqual(result["status"], "context_limit")
        self.assertEqual(client.calls, [])
        identity = agent._implementation()
        with patch.object(agent, "_implementation", side_effect=[identity, {**identity, "changed": True}]):
            result = self.bootstrap(client)
        self.assertEqual(result["status"], "implementation_changed")
        self.assertEqual(client.calls, [])

    def test_implementation_drift_during_final_inference_withholds_answer(self):
        self.approve()
        identity = agent._implementation()
        client = Scripted(response({"answer": {"abstain": {"code": "definition_unavailable"}}}))
        with patch.object(agent, "_implementation", side_effect=[identity, identity, {**identity, "changed": True}]), \
                patch.object(answers, "validate_answer", wraps=answers.validate_answer) as validate:
            result = self.ask(client)
        self.assertEqual(result["status"], "implementation_changed")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["usage"]["reported_cost_usd"], .02)
        self.assertEqual(len(client.calls), 1)
        validate.assert_not_called()
        self.check_receipt(result)

    def test_failed_final_storage_withholds_answer_and_candidate_but_keeps_usage(self):
        self.approve()
        real = agent._write
        def fail(path, value):
            if path.name == "result.json":
                raise OSError("synthetic result storage failure")
            return real(path, value)
        for operation in ("ask", "bootstrap"):
            with self.subTest(operation=operation), patch.object(agent, "_write", side_effect=fail):
                if operation == "ask":
                    client = Scripted(response({"answer": {"abstain": {"code": "definition_unavailable"}}}))
                    result = self.ask(client)
                    key, calls = "answer", 1
                else:
                    client = Scripted(observation, response({"definitions": definitions(), "gaps": []}))
                    result = self.bootstrap(client)
                    key, calls = "candidate", 2
                self.assertEqual(result["status"], "receipt_storage_failed")
                self.assertFalse(result["receipt_persisted"])
                self.assertIsNone(result[key])
                self.assertEqual(result["usage"]["model_calls"], calls)
                self.assertEqual(result["usage"]["reported_cost_usd"], .02 * calls)
                self.assertTrue(Path(result["receipt_path"]).is_file())
    def test_default_adapter_construction_failure_is_not_an_inference(self):
        with patch.object(agent, "ClaudeCLI", side_effect=RuntimeError("not configured")):
            result = agent.bootstrap(self.w, self.profile, self.snapshot, allow_model_data=True)
        self.assertEqual(result["status"], "model_initialization_failed")
        self.assertEqual(result["model_origin"], "configured_claude_cli")
        self.assertEqual(result["usage"]["model_calls"], 0)
        self.assertEqual(result["usage"]["reported_cost_usd"], 0)

    def test_ask_executes_reviewed_metrics_and_exact_scoped_quotes(self):
        self.approve()
        first = {"tools": [{"tool": "metric", "arguments": {"definition_name": "invoice_value"}},
                           {"tool": "metric", "arguments": {"definition_name": "open_cases"}},
                           {"tool": "search_evidence", "arguments": {"query": "follow-up", "fields": ["text"], "limit": 2}}]}
        def final(system, messages, max_cost_usd):
            results = [json.loads(m["content"]) for m in messages if m["role"] == "user"][1:]
            metrics, quotes = [], []
            for item in results:
                value = item["result"]
                if item["tool"] == "metric":
                    metrics.append({"definition_name": value["result"]["definition_name"], "result_id": value["result_id"]})
                else:
                    quotes.extend({k: q[k] for k in ("evidence_id", "field", "start", "end", "text", "quote_id")} for q in value["quotes"])
            return response({"answer": {"metrics": metrics, "quotes": quotes}})
        client = Scripted(response(first), final)
        result = self.ask(client)
        self.assertEqual(result["status"], "answered")
        self.assertIn("USD 400", result["answer"]["rendered"])
        self.assertIn("Needs follow-up", result["answer"]["rendered"])
        self.assertEqual(result["answer"]["metrics"][1]["result"]["values"], [{"currency": None, "value": 2}])
        self.assertEqual(result["answer"]["customer_id"], self.customer)
        self.assertFalse(result["answer"]["model_generated_factual_prose"])
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(len([e for e in result["trace"] if e["type"] == "tool"]), 3)
        self.check_receipt(result)

    def test_ask_abstains_with_fixed_scope_bound_reason(self):
        self.approve()
        result = self.ask(Scripted(response({"answer": {"abstain": {"code": "definition_unavailable"}}})))
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["answer"]["snapshot_id"], self.snapshot)
        self.assertEqual(result["answer"]["customer_id"], self.customer)
        self.assertEqual(result["usage"]["model_calls"], 1)
        self.check_receipt(result)

    def test_field_inspections_use_only_current_customer_evidence_and_do_not_authorize_quotes(self):
        self.approve()
        state = agent._customer_context(self.w, self.profile, self.snapshot, self.customer)
        inspected = json.dumps(state["evidence_index"])
        self.assertIn("Needs follow-up", inspected)
        self.assertIn("Request received", inspected)
        self.assertNotIn("EUR", inspected)  # The unresolved invoice remains outside this customer's evidence.
        self.assertEqual(len(state["evidence_index"]), 4)
        record = next(row for row in state["evidence_index"] if row["kind"] == "support")
        quote = answers.read_evidence(self.w, self.profile, self.snapshot, self.customer, record["evidence_id"])
        selected = {key: quote[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")}
        result = self.ask(Scripted(response({"answer": {"metrics": [], "quotes": [selected]}})))
        self.assertEqual(result["status"], "validation_failed")
        self.assertIsNone(result["answer"])  # A preview cannot bypass a scoped read in the actual agent run.

    def test_field_inspection_global_budget_retains_unknown_for_uninspected_records(self):
        from contextbase.connectors import service
        from contextbase.customer360.snapshots import capture_snapshot
        self.fixture.support.write_text("case_id,organization,body,status\n" + "".join(
            f"case-{number},org-a,{'synthetic statement ' * 30},open\n" for number in range(60)))
        service.sync(self.w, "support", bootstrap_context=False)
        self.snapshot = capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        profiles.materialize(self.w, self.profile, self.snapshot)
        self.approve()
        state = agent._customer_context(self.w, self.profile, self.snapshot, self.customer)
        self.assertTrue(state["evidence_index_truncated"])
        self.assertTrue(state["evidence_inspection_truncated"])
        self.assertEqual(len(state["evidence_index"]), 50)
        inspections = [row["field_inspection"] for row in state["evidence_index"]]
        self.assertLessEqual(sum(len(agent._json(item).encode()) for item in inspections if "fields" in item), 10_000)
        omitted = [item for item in inspections if item.get("status") == "uninspected"]
        self.assertTrue(omitted)
        self.assertTrue(all(item["excluded_content_unknown"] and item["truncated"] for item in omitted))

    def test_ask_refuses_prose_sql_scope_override_and_fabricated_refs(self):
        self.approve()
        actions = [
            {"answer": "Invented revenue"},
            {"tools": [{"tool": "sql", "arguments": {"sql": "SELECT * FROM billing.records"}}]},
            {"tools": [{"tool": "metric", "arguments": {"definition_name": "invoice_value", "customer_id": "other"}}]},
            {"answer": {"metrics": [{"definition_name": "invoice_value", "result_id": "0" * 64}], "quotes": []}},
            {"answer": {"abstain": {"code": "definition_unavailable", "text": "Invented factual reason"}}},
        ]
        for action in actions:
            with self.subTest(action=action):
                result = self.ask(Scripted(response(action)))
                self.assertEqual(result["status"], "validation_failed")
                self.assertIsNone(result["answer"])
                self.check_receipt(result)

    def test_unknown_paid_ask_stops_before_tools_or_fresh_data_reads(self):
        self.approve()
        client = Scripted(response({"tools": [{"tool": "context", "arguments": {}}]}, None))
        with patch.object(agent, "_customer_context", wraps=agent._customer_context) as reads, patch.object(agent, "_tool") as tool:
            result = self.ask(client)
        self.assertEqual(result["status"], "inference_cost_unknown")
        self.assertEqual(reads.call_count, 2)  # initial scope plus pre-call check only
        tool.assert_not_called()
        self.check_receipt(result)

    def test_ask_requires_reviewed_context_before_client_call(self):
        client = Scripted()
        result = self.ask(client)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(client.calls, [])
        self.check_receipt(result)

    def test_actual_but_unseen_metric_reference_cannot_bypass_tool_observation(self):
        self.approve()
        value = answers.execute_metric(self.w, self.profile, self.snapshot, self.customer, "invoice_value")
        result = self.ask(Scripted(response({"answer": {"metrics": [{"definition_name": "invoice_value", "result_id": value["result_id"]}], "quotes": []}})))
        self.assertEqual(result["status"], "validation_failed")
        self.assertIsNone(result["answer"])

    def temporal_scope(self):
        import csv
        from contextbase.connectors import service
        from contextbase.customer360.snapshots import capture_snapshot
        with self.fixture.support.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["case_id", "organization", "body", "status"])
            writer.writerow(["case-a", "org-a", json.dumps({"message": "Original reported issue.", "when": "2026-01-01T12:00:00Z"}), "open"])
            writer.writerow(["case-b", "org-a", json.dumps({"message": "A later substantive update.", "when": "2026-01-02T12:00:00Z"}), "open"])
        service.sync(self.w, "support", bootstrap_context=False)
        self.snapshot = capture_snapshot(self.w, ["billing", "support"])["snapshot_id"]
        mapped = copy.deepcopy(self.fixture.profile)
        mapped["bindings"][1]["passages"] = [{"name": "message", "field": "text", "text_path": "/message",
            "time": {"source": "item", "path": "/when", "encoding": "rfc3339", "meaning": "passage_created"}}]
        candidate = profiles.propose_profile(self.w, mapped, self.snapshot)
        profiles.approve_profile(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-time-review")
        profiles.materialize(self.w, self.profile, self.snapshot)
        self.approve()

    def test_agent_latest_selection_keeps_exact_passage_and_temporal_proof(self):
        self.temporal_scope()
        def select(system, messages, max_cost_usd):
            contract = json.loads(system.split("Tool contracts: ", 1)[1])["latest_evidence"]
            self.assertEqual(contract["arguments"]["properties"]["kind"]["enum"],
                             ["billing", "commercial", "conversation", "customer", "person", "support", "usage"])
            self.assertEqual(contract["reviewed_passage_kinds"], ["support"])
            self.assertNotIn("message", contract["arguments"]["properties"]["kind"]["enum"])
            self.assertEqual(contract["arguments"]["required"], ["kind"])
            self.assertFalse(contract["arguments"]["additionalProperties"])
            # Select from the actual model-visible contract, then execute the
            # real scoped temporal tool and validate the independently known leaf.
            return response({"tools": [{"tool": "latest_evidence", "arguments": {"kind": contract["reviewed_passage_kinds"][0]}}]})
        def finish(system, messages, max_cost_usd):
            result = json.loads(messages[-1]["content"])["result"]
            quotes = [{k: q[k] for k in ("evidence_id", "field", "start", "end", "text", "quote_id", "passage_name", "json_pointer")} for q in result["quotes"]]
            return response({"answer": {"metrics": [], "quotes": quotes,
                "latest": [{key: result[key] for key in ("kind", "limit", "selection_id")}]}})
        client = Scripted(select, finish)
        result = self.ask(client)
        self.assertEqual(result["status"], "answered")
        quote = result["answer"]["quotes"][0]
        self.assertEqual(quote["text"], "A later substantive update.")
        self.assertEqual(quote["json_pointer"], "/message")
        self.assertEqual(quote["time_lineage"]["json_pointer"], "/when")
        self.assertEqual(result["answer"]["temporal_selections"][0]["status"], "latest_unique")
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.check_receipt(result)

    def test_latest_tool_kind_does_not_accept_passage_names_or_aliases(self):
        self.temporal_scope()
        for kind in ("message", "support_update", "Support"):
            with self.subTest(kind=kind):
                client = Scripted(response({"tools": [{"tool": "latest_evidence", "arguments": {"kind": kind}}]}))
                result = self.ask(client)
                self.assertEqual(result["status"], "validation_failed")
                self.assertIsNone(result["answer"])
                self.assertEqual(result["usage"]["model_calls"], 1)
                self.assertFalse(any(event["type"] == "tool" for event in result["trace"]))
                self.check_receipt(result)

    def test_agent_refuses_latest_proof_not_observed_in_its_run(self):
        self.temporal_scope()
        selection = answers.latest_evidence(self.w, self.profile, self.snapshot, self.customer, "support")
        quote = selection["quotes"][0]
        selected = {k: quote[k] for k in ("evidence_id", "field", "start", "end", "text", "quote_id", "passage_name", "json_pointer")}
        arguments = {k: quote[k] for k in ("evidence_id", "field", "passage_name", "json_pointer")}
        client = Scripted(response({"tools": [{"tool": "read_evidence", "arguments": arguments}]}),
            response({"answer": {"metrics": [], "quotes": [selected], "latest": [{key: selection[key] for key in ("kind", "limit", "selection_id")}]}}))
        result = self.ask(client)
        self.assertEqual(result["status"], "validation_failed")
        self.assertIsNone(result["answer"])


if __name__ == "__main__":
    unittest.main()
