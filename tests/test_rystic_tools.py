"""Security boundaries and source-capability parity for simulator MCP tools."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from contextbase.context import bootstrap, install_demo_context
from contextbase.mcp import dispatch
from contextbase.rystic_tools import MAX_RESPONSE_BYTES, SOURCE_METADATA, RysticToolkit, validate_readonly_graphql
from contextbase.tools import Toolkit
from contextbase.workspace import Workspace, init_demo


class RysticToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root / "workspace")
        self.pair = Mock()
        self.pair.verify.return_value = {
            "stripe": {"product": "stripe", "version": "0.0.1", "model_hash": "synthetic"},
            "linear": {"product": "linear", "version": "0.0.1", "model_hash": "synthetic"},
        }
        self.pair.stripe.request.return_value = {"data": [{"id": "cus_example"}], "has_more": False}
        self.pair.linear.graphql.return_value = {"issues": {"nodes": [{"id": "example_issue"}]}}
        self.toolkit = RysticToolkit(self.workspace, self.pair)

    def test_sources_verify_and_disclose_simulator_fidelity(self):
        result = self.toolkit.call("sources")
        self.pair.verify.assert_called_once_with()
        self.assertEqual(result["sources"], self.pair.verify.return_value)
        self.assertEqual(result["source_writes"], "disabled")
        self.assertEqual(result["production_fidelity"], "not_established")
        self.assertIn("issues.filter as ignored", " ".join(result["limitations"]))
        self.assertEqual(result["identity_verification"]["policy"], "per_call")
        self.assertFalse(result["identity_verification"]["source_data_cached"])

    def test_refresh_status_reports_shared_evidence_without_contacting_sources(self):
        receipt = {"status": "blocked", "snapshot_reads_allowed": False,
                   "currentness": "latest_observed_state_only",
                   "latest_attempt": {"status": "refused", "observed_at": "2026-10-09T01:00:00Z",
                                      "blockers": [{"source": "stripe.customers", "record_id": "cus_example",
                                                    "field": "metadata.is_internal", "reason": "missing_required_input"}]},
                   "last_successful_snapshot": {"snapshot_id": "previous_synthetic_snapshot", "observed_at": "2026-10-09T00:00:00Z"}}
        with patch("contextbase.refresh_state.refresh_status", return_value=receipt) as status:
            self.assertEqual(self.toolkit.call("refresh_status"), receipt)
            status.assert_called_once_with(self.workspace)
        for arguments in ({"refresh": True}, [], False):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.toolkit.call("refresh_status", arguments)
        self.pair.verify.assert_not_called()
        self.pair.stripe.request.assert_not_called()
        self.pair.linear.graphql.assert_not_called()

    def test_source_metadata_exposes_native_field_shapes_and_working_projection_example(self):
        result = self.toolkit.call("sources")
        metadata = result["metadata"]
        self.assertEqual(metadata, SOURCE_METADATA)
        self.assertEqual(metadata["stripe"]["collections"]["customers"]["metadata.is_internal"], "string: true or false")
        self.assertEqual(metadata["stripe"]["collections"]["charges"]["amount_refunded"], "integer cents")
        self.assertIn("has_more", metadata["stripe"]["list_response"])
        self.assertIn("assignee", metadata["linear"]["issue_fields"])
        example = metadata["linear"]["example"]
        self.assertIn("includeArchived: true", example["arguments"]["query"])
        self.assertIn("hasNextPage endCursor", example["arguments"]["query"])
        self.toolkit.call(example["tool"], example["arguments"])
        self.pair.linear.graphql.assert_called_once_with(example["arguments"]["query"], variables={"after": None})
        metadata["linear"]["example"]["arguments"]["query"] = "changed caller copy"
        self.assertNotEqual(self.toolkit.call("sources")["metadata"], metadata)

    def test_evaluation_session_verifies_once_at_startup_without_caching_source_data(self):
        self.pair.verify.assert_not_called()
        toolkit = RysticToolkit(self.workspace, self.pair, verification_policy="evaluation_session")
        self.pair.verify.assert_called_once_with()
        self.pair.stripe.request.return_value = {"data": [{"id": "cus_first"}], "has_more": False}
        first = toolkit.call("stripe_list", {"resource": "customers"})
        self.pair.stripe.request.return_value = {"data": [{"id": "cus_second"}], "has_more": False}
        second = toolkit.call("stripe_list", {"resource": "customers"})
        self.assertNotEqual(first, second)
        self.assertEqual(self.pair.stripe.request.call_count, 2)
        metadata = toolkit.call("sources")
        self.assertEqual(metadata["identity_verification"]["policy"], "evaluation_session")
        self.assertIn("startup", metadata["identity_verification"]["scope"])
        self.assertIn("postflight", metadata["identity_verification"]["evaluation_requirement"])
        self.assertFalse(metadata["identity_verification"]["source_data_cached"])
        metadata["sources"]["stripe"]["version"] = "changed caller copy"
        self.assertNotEqual(toolkit.call("sources")["sources"], metadata["sources"])
        self.pair.verify.assert_called_once_with()

    def test_evaluation_session_startup_failure_and_invalid_policy_fail_closed(self):
        for policy in (None, "", "cached", [], True):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "verification_policy"):
                RysticToolkit(self.workspace, self.pair, verification_policy=policy)
        self.pair.verify.assert_not_called()
        self.pair.verify.side_effect = ValueError("private provider body /private/example")
        with self.assertRaisesRegex(ValueError, "Simulator request failed") as raised:
            RysticToolkit(self.workspace, self.pair, verification_policy="evaluation_session")
        self.assertNotIn("private", str(raised.exception))
        self.pair.stripe.request.assert_not_called()
        self.pair.linear.graphql.assert_not_called()

    def test_list_preserves_source_cursor_and_filter_fields(self):
        filters = {"customer": "cus_example", "created[gte]": 1_000, "expand[0]": "data.customer"}
        returned = self.toolkit.call("stripe_list", {
            "resource": "charges", "limit": 7, "starting_after": "ch_previous", "filters": filters,
        })
        self.assertEqual(returned, self.pair.stripe.request.return_value)
        self.pair.verify.assert_called_once_with()
        self.pair.stripe.request.assert_called_once_with("GET", "/v1/charges", query={
            **filters, "limit": 7, "starting_after": "ch_previous",
        })
        self.assertNotIn("limit", filters)
        for resource in ("customers", "refunds", "payment_intents"):
            self.toolkit.call("stripe_list", {"resource": resource})
        self.assertEqual(self.pair.verify.call_count, 4)

    def test_get_uses_only_allowed_resource_and_exact_safe_id(self):
        self.toolkit.call("stripe_get", {"resource": "payment_intents", "id": "pi_example-1"})
        self.pair.stripe.request.assert_called_once_with("GET", "/v1/payment_intents/pi_example-1")
        for object_id in ("", "../control", "id?secret=x", "/reset", "id%2freset", "id\nnext", 42, "x" * 201):
            with self.subTest(object_id=object_id), self.assertRaises(ValueError):
                self.toolkit.call("stripe_get", {"resource": "customers", "id": object_id})
        self.assertEqual(self.pair.stripe.request.call_count, 1)

    def test_stripe_bounds_and_malformed_filters_fail_before_source_access(self):
        invalid = [
            {"resource": "../reset"}, {"resource": "customers", "limit": True},
            {"resource": "customers", "limit": 0}, {"resource": "customers", "limit": 101},
            {"resource": "customers", "starting_after": None},
            {"resource": "charges", "filters": {"limit": 999}},
            {"resource": "charges", "filters": {"starting_after": "ch_id"}},
            {"resource": "charges", "filters": {"customer": {"id": "cus_example"}}},
            {"resource": "charges", "filters": {"created[gte]": float("nan")}},
            {"resource": "charges", "filters": {"customer&limit": "bad"}},
            {"resource": "charges", "filters": {"customer": "x" * 4097}},
            {"resource": "charges", "filters": {str(i): i for i in range(33)}},
            {"resource": "charges", "starting_after": "ch_a", "filters": {"ending_before": "ch_b"}},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.toolkit.call("stripe_list", arguments)
        self.pair.verify.assert_not_called()
        self.pair.stripe.request.assert_not_called()

    def test_full_readonly_graphql_passes_aliases_fragments_variables_unchanged(self):
        query = '''
            query Accounts($after: String, $filter: IssueFilter = {title: {contains: "mutation"}}) {
              important: issues(first: 50, after: $after, filter: $filter) {
                nodes { ...IssueFields }
                pageInfo { endCursor hasNextPage }
              }
            }
            fragment IssueFields on Issue { id title priority state { name } }
        '''
        variables = {"after": "cursor_example", "filter": {"priority": {"eq": 1}}}
        result = self.toolkit.call("linear_query", {"query": query, "variables": variables})
        self.assertEqual(result, self.pair.linear.graphql.return_value)
        self.pair.linear.graphql.assert_called_once_with(query, variables=variables)
        self.pair.verify.assert_called_once_with()

    def test_graphql_rejects_all_write_operations_including_mixed_documents(self):
        denied = [
            "mutation { issueCreate(input: {}) { success } }",
            "# comment\n mutation Create { issueCreate(input: {}) { success } }",
            "query Read { viewer { id } } mutation Write { issueCreate(input: {}) { success } }",
            'query Read { issues(filter: {title: {contains: "mutation"}}) { nodes { id } } }\nmutation Write { issueDelete(id: "example") { success } }',
            "fragment Safe on Issue { id } subscription Watch { issues { nodes { id } } }",
            "{ viewer { id } } subscription { viewer { id } }",
            'query Read { viewer { name(format: """# query fake { viewer }\n""") } } mutation { write }',
            'query Read { viewer { name(format: "\\\" # mutation") } } mutation { write }',
            'query Read($text: String = "mutation { x }") { viewer { id } } subscription { viewer { id } }',
        ]
        for query in denied:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.toolkit.call("linear_query", {"query": query})
        self.pair.verify.assert_not_called()
        self.pair.linear.graphql.assert_not_called()

    def test_graphql_keywords_in_comments_strings_fields_or_names_are_not_operations(self):
        allowed = [
            '# mutation { write }\n{ viewer { id } }',
            'query mutation { subscription: viewer { id } }',
            'query Read($text: String = "mutation { x }") { viewer { name(format: $text) } }',
            'query Read { viewer { name(format: "\\\" mutation { x }") } }',
            'query Read { viewer { name(format: """mutation { x }\nsubscription { y }""") } }',
            'query Read { viewer { name(format: """escaped \\""" mutation""") } }',
            'fragment F on User { id } query A @skip(if: false) { viewer { ...F } } query B { viewer { id } }',
        ]
        for query in allowed:
            with self.subTest(query=query):
                validate_readonly_graphql(query)

    def test_graphql_bounds_invalid_documents_and_nonfinite_variables(self):
        for query in (None, "", "# only comments", "fragment F on User { id }", "schema { query: Query }",
                      "query { viewer { id }", 'query { f(x: "unfinished) }', "query { f(x: [}) }",
                      "query { " + "f { " * 65 + "id" + "}" * 66, " " * 32769 + "{ viewer { id } }"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.toolkit.call("linear_query", {"query": query})
        for variables in ([], None, {"cursor": "x" * 65536}, {"value": float("inf")}, {1: "wrong key"}):
            with self.subTest(variables=variables), self.assertRaises(ValueError):
                self.toolkit.call("linear_query", {"query": "{ viewer { id } }", "variables": variables})
        self.pair.verify.assert_not_called()

    def test_context_matches_duckdb_for_missing_draft_and_approved_context(self):
        self.assertEqual(self.toolkit.call("context"), Toolkit(self.workspace).call("context"))
        workspace = init_demo(self.root / "demo", accounts=4)
        toolkit = RysticToolkit(workspace, self.pair)
        approved = workspace.root / "context" / "approved.json"
        # init_demo installs approved synthetic context; check exact parity first.
        self.assertEqual(toolkit.call("context"), Toolkit(workspace).call("context"))
        approved.unlink()
        bootstrap(workspace)
        result = toolkit.call("context")
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result, Toolkit(workspace).call("context"))
        install_demo_context(workspace)
        self.assertEqual(toolkit.call("context"), Toolkit(workspace).call("context"))
        self.pair.verify.assert_not_called()

    def test_mcp_dispatch_error_sanitization_and_response_size(self):
        listed = dispatch(self.toolkit, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertEqual(names, {"sources", "context", "stripe_list", "stripe_get", "linear_query", "refresh_status"})
        self.assertTrue(all(item["annotations"]["readOnlyHint"] for item in listed["result"]["tools"]))
        self.pair.linear.graphql.side_effect = ValueError("provider body with /private/example/path")
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "linear_query", "arguments": {"query": "{ viewer { id } }"},
        }}
        failed = dispatch(self.toolkit, request)
        self.assertTrue(failed["result"]["isError"])
        self.assertNotIn("private", json.dumps(failed))
        self.assertNotIn("provider body", json.dumps(failed))
        self.pair.linear.graphql.side_effect = None
        self.pair.linear.graphql.return_value = {"text": "x" * MAX_RESPONSE_BYTES}
        self.assertTrue(dispatch(self.toolkit, request)["result"]["isError"])
        self.pair.verify.side_effect = RuntimeError("verification body /private/example/path")
        with self.assertRaisesRegex(ValueError, "Simulator request failed") as caught:
            self.toolkit.call("sources")
        self.assertNotIn("private", str(caught.exception))

    def test_unknown_and_extra_arguments_are_rejected(self):
        for name, arguments in (("reset", {}), ([], {}), ("sources", {"url": "http://example.invalid"}),
                                ("linear_query", {}), ("stripe_list", []),
                                ("context", {"approved": False})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.toolkit.call(name, arguments)
        self.pair.verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
