"""Benchmark context with equal business policy and no fixture answer oracle."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from contextbase.context import approve_metric, load_context
from contextbase.evaluation_context import CONTEXT_DESCRIPTION, EvaluationContextToolkit, context_view
from contextbase.evaluation_tools import ComputedTools, StdioMCPClient
from contextbase.rystic import load_example
from contextbase.rystic_recipe import NET_EXPOSED_SQL
from contextbase.rystic_recipe import NAMESPACE
from contextbase.rystic_tools import RysticToolkit
from contextbase.rystic_workflow import RULES, propose_context
from contextbase.tools import Toolkit
from contextbase.workspace import Workspace


class EvaluationContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="contextbase-context-view-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(Path(temporary.name) / "workspace")
        load_example(self.workspace)
        proposal = propose_context(self.workspace)["proposal"]
        approve_metric(self.workspace, proposal["name"], proposal["approval_sha256"], "synthetic_test_reviewer")
        self.path = self.workspace.root / "context" / "approved.json"

    def test_view_retains_policy_and_versions_without_observations_or_free_text(self):
        original_bytes = self.path.read_bytes()
        full = load_context(self.workspace)
        view = context_view(self.workspace)
        self.assertEqual(view["context_version"], full["context_version"])
        self.assertEqual(view["metrics"][0]["version"], full["metrics"][0]["version"])
        self.assertEqual(view["metrics"][0]["sql"], NET_EXPOSED_SQL)
        self.assertEqual(view["metrics"][0]["rules"], RULES)
        self.assertEqual(set(view["metrics"][0]), {"name", "version", "status", "synthetic", "sql", "rules"})
        rendered = json.dumps(view)
        self.assertNotIn(full["metrics"][0]["execution_evidence"]["result_sha256"], rendered)
        self.assertNotIn("execution_evidence", rendered)
        self.assertNotIn("1857.69", rendered)
        self.assertNotIn("synthetic_test_reviewer", rendered)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        view["metrics"][0]["rules"].append("mutation of caller copy")
        self.assertEqual(context_view(self.workspace)["metrics"][0]["rules"], RULES)

    def test_extra_context_payload_never_reaches_the_view(self):
        full = load_context(self.workspace)
        marker = "SYNTHETIC_OBSERVATION_NOT_FOR_AGENT"
        full.update(description=marker, source_snapshot={"observed_answer": marker}, unexpected={"gold": marker})
        metric = full["metrics"][0]
        metric.update(description=marker, owner=marker, execution_evidence={"result_sha256": marker})
        metric["definition"] = {"examples": [{"result": {"rows": [{"answer": marker}]}}], "provenance": marker}
        self.path.write_text(json.dumps(full))
        view = context_view(self.workspace)
        self.assertNotIn(marker, json.dumps(view))
        self.assertEqual(load_context(self.workspace), full)

    def test_legacy_container_label_does_not_change_exact_synthetic_policy(self):
        full = load_context(self.workspace)
        original_view = context_view(self.workspace)
        full["synthetic"] = False
        self.path.write_text(json.dumps(full))
        self.assertEqual(context_view(self.workspace), original_view)
        self.assertTrue(context_view(self.workspace)["synthetic"])
        self.assertFalse(load_context(self.workspace)["synthetic"])
        self.assertFalse(self.workspace.is_synthetic())

    def test_both_toolkits_expose_identical_context_and_preserve_other_handlers(self):
        pair = Mock()
        source_base = RysticToolkit(self.workspace, pair)
        sql_base = Toolkit(self.workspace, "sql_context")
        source = EvaluationContextToolkit(self.workspace, source_base)
        sql = EvaluationContextToolkit(self.workspace, sql_base)
        self.assertEqual(source.call("context"), sql.call("context", {}))
        source_definition = next(item for item in source.definitions() if item["name"] == "context")
        sql_definition = next(item for item in sql.definitions() if item["name"] == "context")
        self.assertEqual(source_definition, sql_definition)
        self.assertEqual(sql_definition["description"], CONTEXT_DESCRIPTION)
        self.assertEqual(sql.call("query", {"sql": "SELECT 1 AS n"})["rows"], [{"n": 1}])
        self.assertEqual(sql.call("catalog"), sql_base.call("catalog"))
        pair.verify.assert_not_called()
        for arguments in ({"raw": True}, [], "", False):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                source.call("context", arguments)
        self.assertNotEqual(sql_base.call("context"), sql.call("context"))

    def test_blocked_workspace_exposes_equal_context_and_complete_refresh_status(self):
        pair = Mock()
        source = EvaluationContextToolkit(self.workspace, RysticToolkit(self.workspace, pair))
        sql = EvaluationContextToolkit(self.workspace, Toolkit(self.workspace, "sql_context"))
        receipt = {"status": "blocked", "snapshot_reads_allowed": False,
                   "currentness": "latest_observed_state_only",
                   "latest_attempt": {"status": "refused", "observed_at": "2026-10-09T01:00:00Z",
                                      "blockers": [{"source": "stripe.customers", "record_id": "cus_example",
                                                    "field": "metadata.is_internal", "reason": "missing_required_input"}]},
                   "last_successful_snapshot": {"snapshot_id": "previous_synthetic_snapshot", "observed_at": "2026-10-09T00:00:00Z"}}
        with patch("contextbase.refresh_state.refresh_status", return_value=receipt), \
                patch("contextbase.refresh_state.require_readable_snapshot", side_effect=ValueError("Required source refresh is blocked")):
            self.assertEqual(source.call("refresh_status"), sql.call("refresh_status"))
            self.assertEqual(sql.call("refresh_status"), receipt)
            self.assertEqual(source.call("context"), sql.call("context"))
            for tool in ("catalog", "query", "list_records", "metric"):
                with self.subTest(tool=tool), patch.object(sql.base_toolkit, "call") as execute:
                    with self.assertRaisesRegex(ValueError, "refresh is blocked"):
                        sql.call(tool, {})
                    execute.assert_not_called()
            pair.verify.return_value = {"stripe": {"version": "0.0.8"}, "linear": {"version": "0.0.1"}}
            pair.stripe.request.return_value = {"data": [{"id": "cus_current"}], "has_more": False}
            self.assertEqual(source.call("stripe_list", {"resource": "customers"}), pair.stripe.request.return_value)
            pair.stripe.request.assert_called_once()
        for toolkit in (source, sql):
            definition = [item for item in toolkit.definitions() if item["name"] == "refresh_status"]
            self.assertEqual(len(definition), 1)
            self.assertEqual(definition, [item for item in sql.definitions() if item["name"] == "refresh_status"])
            with self.assertRaises(ValueError):
                toolkit.call("refresh_status", {"include_stale_data": True})

    def test_invalid_approval_metric_and_policy_are_rejected(self):
        original = load_context(self.workspace)
        modifications = [
            lambda value: value.update(synthetic=None),
            lambda value: value.update(status="draft"),
            lambda value: value.update(metrics=[]),
            lambda value: value["metrics"].append({**deepcopy(value["metrics"][0]), "name": "extra_metric"}),
            lambda value: value["metrics"][0].update(sql="SELECT 1857.69 AS net_exposed_collected_usd"),
            lambda value: value["metrics"][0].update(rules=["Unreviewed rule"]),
            lambda value: value["metrics"][0].update(status="candidate"),
            lambda value: value["metrics"][0].update(synthetic=False),
            lambda value: value["metrics"][0].update(approval_kind="curated_synthetic_demo"),
            lambda value: value["metrics"][0].update(version=True),
            lambda value: value["metrics"][0].update(version=0),
            lambda value: value.update(context_version="copied-answer-1857.69"),
        ]
        for change in modifications:
            with self.subTest(change=change):
                context = deepcopy(original)
                change(context)
                self.path.write_text(json.dumps(context))
                with self.assertRaises(ValueError):
                    context_view(self.workspace)
        self.path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "already exposes context"):
            EvaluationContextToolkit(self.workspace, Toolkit(self.workspace, "sql"))

    def test_actual_stdio_context_view_matches_for_both_arms_without_source_calls(self):
        actual = []
        for arm in ("sql_context", "rystic_source"):
            with self.subTest(arm=arm), ComputedTools(StdioMCPClient(self.workspace, arm=arm, evaluation_context=True)) as tools:
                result = tools.call("context")
                actual.append(result["result"])
                self.assertEqual(result["stored_call_id"], 1)
                self.assertEqual(result["result"], context_view(self.workspace))
                stored = tools.call("compute", {"sql": "SELECT result_json FROM scratch.tool_results WHERE call_id=1"})
                self.assertNotIn("result_sha256", stored["result"]["rows"][0]["result_json"])
        self.assertEqual(actual[0], actual[1])

    def test_actual_stdio_failed_refresh_keeps_status_context_and_history_accessible(self):
        from contextbase.refresh_state import begin_refresh, finish_failed_refresh, refresh_status
        previous = refresh_status(self.workspace)["last_successful_snapshot"]
        attempt = begin_refresh(self.workspace, NAMESPACE)
        finish_failed_refresh(self.workspace, attempt["attempt_id"], outcome="unverified")
        expected = refresh_status(self.workspace)
        self.assertEqual(expected["status"], "blocked")
        self.assertEqual(expected["last_successful_snapshot"], previous)
        self.assertEqual(expected["latest_attempt"]["outcome"], "unverified")
        self.assertFalse(expected["source_contacted"])
        returned_status = []
        for arm in ("sql_context", "rystic_source"):
            with self.subTest(arm=arm), ComputedTools(StdioMCPClient(self.workspace, arm=arm, evaluation_context=True)) as tools:
                status = tools.call("refresh_status")
                returned_status.append(status["result"])
                self.assertEqual(status["result"], expected)
                self.assertEqual(tools.call("context")["result"], context_view(self.workspace))
                stored = tools.call("compute", {"sql": "SELECT result_json->>'status' AS status FROM scratch.tool_results WHERE call_id=1"})
                self.assertEqual(stored["result"]["rows"], [{"status": "blocked"}])
                if arm == "sql_context":
                    self.assertIn("error", tools.call("query", {"sql": "SELECT count(*) FROM stripe.customers"})["result"])
                    self.assertIn("error", tools.call("catalog")["result"])
        self.assertEqual(returned_status[0], returned_status[1])
        with self.assertRaisesRegex(ValueError, "required refresh"):
            self.workspace.query("SELECT count(*) FROM stripe.customers")
        # Historical data is still present for explicit operator recovery; the
        # model tool boundary never exposes this internal connection method.
        with self.workspace._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM stripe.customers").fetchone()[0], 12)


if __name__ == "__main__":
    unittest.main()
