"""Phase execution with authored responses and source doubles, not performance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.cli import execute, parser
from contextbase.phase_benchmark import freeze_phase_plan, run_phase_pilot
from contextbase.rystic import sync_snapshot
from contextbase.rystic_benchmark import _hash
from contextbase.study_phases import prepare_phases
from contextbase.workspace import Workspace
from test_rystic_benchmark import AuthoredModel, EmptyTools, make_fixture
from test_study_binding import FakePair, source_rows


class PhaseBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root / "workspace")
        self.manifest, _ = make_fixture(self.workspace)
        self.prepared = prepare_phases(self.manifest, self.root / "prepared.json")

    def observe(self, phase):
        pair = FakePair(source_rows(self.prepared, phase))
        pair.url = "http://127.0.0.1:18981"
        path = self.root / (phase + "-capture.json")
        if phase in {"guard_a", "guard_b"}:
            with self.assertRaises(ValueError):
                sync_snapshot(self.workspace, pair, self.manifest["namespace"], capture_output=path)
        else:
            sync_snapshot(self.workspace, pair, self.manifest["namespace"], capture_output=path)
        return pair, json.loads(path.read_text())

    def freeze(self, phase="baseline", **kwargs):
        pair, capture = self.observe(phase)
        plan = freeze_phase_plan(self.workspace, self.prepared, phase, capture,
                                 self.root / (phase + "-plan.json"), **kwargs)
        return pair, capture, plan

    def run_plan(self, pair, plan, model=None, **kwargs):
        return run_phase_pilot(self.workspace, pair, plan, self.root / (plan["phase_id"] + "-run.json"),
            approve=plan["plan_sha256"], allow_model_data=True,
            client=model or AuthoredModel(plan), tools_factory=lambda arm: EmptyTools(), **kwargs)

    def test_all_phases_use_common_loop_and_keep_gold_and_labels_out_of_prompts(self):
        observed = []
        for phase, trials in (("baseline", 10), ("refreshed", 10), ("guard_a", 2), ("guard_b", 2)):
            pair, capture, plan = self.freeze(phase)
            self.assertEqual(plan["evidence_origin"], "synthetic_test")
            self.assertFalse(plan["held_out"])
            self.assertEqual(len(plan["queue"]), trials)
            for index in range(0, trials, 2):
                left, right = plan["queue"][index:index + 2]
                self.assertEqual(left["task"], right["task"])
                self.assertNotEqual(left["arm"], right["arm"])
            model = AuthoredModel(plan)
            result = self.run_plan(pair, plan, model)
            self.assertEqual(result["kind"], "rystic_development_phase_pilot")
            self.assertTrue(result["execution_complete"])
            self.assertEqual(result["invocations"], trials)
            self.assertTrue(all(trial["correct"] for trial in result["trials"]))
            self.assertFalse(result["release_gates"]["claim_eligible"])
            self.assertIsNone(result["summary"][0]["total_workload_cost_usd"])
            self.assertEqual(result["source_stability"], "pre_and_post_match")
            for system, messages in model.prompts:
                for hidden in ('"expected"', "guard_a/", "guard_b/", '"phase_id"', "binding_sha256"):
                    self.assertNotIn(hidden, system + messages[0]["content"])
            observed.append(result)
        self.assertEqual(sum(result["invocations"] for result in observed), 24)

    def test_planning_cli_uses_saved_evidence_and_makes_no_source_or_model_calls(self):
        _, _ = self.observe("baseline")
        args = parser().parse_args(["--workspace", str(self.workspace.root), "rystic", "phase-pilot-plan",
            "--prepared-plan", str(self.root / "prepared.json"), "--phase", "baseline",
            "--capture", str(self.root / "baseline-capture.json"), "--output", str(self.root / "cli-plan.json")])
        with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No live sources")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            result = execute(args)
        self.assertEqual(result["planned_trials"], 10)
        self.assertEqual(result["source_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["release_evidence"])

    def test_synthetic_plan_cannot_start_configured_model(self):
        pair, _, plan = self.freeze()
        with patch("contextbase.rystic_benchmark.ClaudeCLI", side_effect=AssertionError("No model")), \
             self.assertRaisesRegex(ValueError, "Authored source doubles"):
            run_phase_pilot(self.workspace, pair, plan, self.root / "no-run.json",
                            approve=plan["plan_sha256"], allow_model_data=True)
        self.assertFalse((self.root / "no-run.json").exists())

    def test_consent_approval_and_rehashed_gold_change_rejected_before_model(self):
        pair, _, plan = self.freeze()
        for consent, approval in ((False, plan["plan_sha256"]), (True, "wrong")):
            with self.assertRaises(ValueError):
                run_phase_pilot(self.workspace, pair, plan, self.root / "no-run.json", approve=approval,
                                allow_model_data=consent, client=AuthoredModel(plan))
        altered = deepcopy(plan)
        altered["tasks"][0]["expected"] = {"fabricated": True}
        altered["plan_sha256"] = _hash({key: value for key, value in altered.items() if key != "plan_sha256"})
        with self.assertRaisesRegex(ValueError, "task artifacts"):
            self.run_plan(pair, altered)
        self.assertFalse((self.root / "baseline-run.json").exists())

    def test_source_drift_keeps_all_trials_as_capped_failures_without_inference(self):
        pair, _, plan = self.freeze()
        pair.rows["stripe.charges"][0]["amount"] += 1
        model = AuthoredModel(plan)
        result = self.run_plan(pair, plan, model)
        self.assertEqual(result["invocations"], 0)
        self.assertEqual(result["stop_reason"], "preflight_failed")
        self.assertEqual(len(result["trials"]), 10)
        self.assertTrue(all(not trial["correct"] and trial["capped_seconds"] == 180 for trial in result["trials"]))
        self.assertEqual(model.prompts, [])

    def test_changed_refresh_and_unrelated_tables_prevent_plan_reuse(self):
        pair, capture, plan = self.freeze()
        self.observe("guard_a")
        with self.assertRaisesRegex(ValueError, "exact validation attempt"):
            self.run_plan(pair, plan)
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE stripe.unrelated (id INTEGER)")
        current_capture = json.loads((self.root / "guard_a-capture.json").read_text())
        with self.assertRaisesRegex(ValueError, "exactly the three"):
            freeze_phase_plan(self.workspace, self.prepared, "guard_a", current_capture, self.root / "bad.json")

    def test_postflight_drift_invalidates_previously_correct_answers(self):
        pair, _, plan = self.freeze("guard_a")
        class ChangeAfterAnswer(AuthoredModel):
            def complete(model, *args, **kwargs):
                response = super().complete(*args, **kwargs)
                if len(model.prompts) == len(plan["queue"]):
                    pair.rows["stripe.charges"][0]["amount"] += 1
                return response
        result = self.run_plan(pair, plan, ChangeAfterAnswer(plan))
        self.assertFalse(result["execution_complete"])
        self.assertEqual(result["source_stability"], "postflight_failed")
        self.assertFalse(any(trial["correct"] for trial in result["trials"]))

    def test_plan_creation_is_exclusive_and_requires_exact_saved_capture(self):
        pair, capture, _ = self.freeze()
        with self.assertRaises(FileExistsError):
            freeze_phase_plan(self.workspace, self.prepared, "baseline", capture, self.root / "baseline-plan.json")
        changed = deepcopy(capture)
        changed["receipt"]["observed_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "exact validation attempt"):
            freeze_phase_plan(self.workspace, self.prepared, "baseline", changed, self.root / "bad.json")

    def test_guarded_run_reads_evidence_and_refuses_stale_rows_over_actual_local_mcp(self):
        from contextbase.evaluation_context import EvaluationContextToolkit
        from contextbase.evaluation_tools import ComputedTools, StdioMCPClient
        from contextbase.rystic_tools import RysticToolkit
        pair, _, plan = self.freeze("guard_b")
        source = EvaluationContextToolkit(self.workspace, RysticToolkit(self.workspace, pair))

        class LocalSourceClient(EmptyTools):
            # In-memory source double; workspace arm below uses real stdio MCP.
            def definitions(self): return source.definitions()
            def call(self, name, arguments): return source.call(name, arguments)

        class EvidenceModel:
            def __init__(model): model.saw_refusal = False
            def complete(model, system, messages, **kwargs):
                result = {"cost_usd": 0.01, "model_usage": {"authored-evidence-reader": {}}}
                if len(messages) == 1:
                    tools = [{"tool": "refresh_status", "arguments": {}}]
                    definitions = json.loads(system.rsplit("\n", 1)[1])["tools"]
                    if any(item["name"] == "query" for item in definitions):
                        tools.append({"tool": "query", "arguments": {"sql": "SELECT * FROM stripe.customers"}})
                    return {**result, "response": json.dumps({"tools": tools})}
                responses = [json.loads(item["content"]) for item in messages[2:] if item["role"] == "user"]
                status = next(item["result"]["result"] for item in responses if item["tool"] == "refresh_status")
                assert status["status"] == "blocked" and not status["snapshot_reads_allowed"]
                for response in responses:
                    if response["tool"] == "query":
                        assert "error" in response["result"]["result"]
                        model.saw_refusal = True
                provenance = json.loads(messages[0]["content"].split("for an answer or abstention: ", 1)[1])
                answer = {"status": "abstained", "result": None,
                          "blockers": status["latest_attempt"]["validation"]["blockers"], "provenance": provenance}
                return {**result, "response": json.dumps({"answer": answer})}

        def factory(arm):
            client = (StdioMCPClient(self.workspace, arm="sql_context", evaluation_context=True)
                      if arm == "sql_context" else LocalSourceClient())
            return ComputedTools(client)

        model = EvidenceModel()
        result = run_phase_pilot(self.workspace, pair, plan, self.root / "mcp-run.json",
            approve=plan["plan_sha256"], allow_model_data=True, client=model, tools_factory=factory)
        self.assertTrue(result["execution_complete"])
        self.assertTrue(all(trial["correct"] for trial in result["trials"]))
        self.assertEqual(result["invocations"], 4)
        self.assertTrue(model.saw_refusal)
