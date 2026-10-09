"""Authored full-ledger scenarios, not native performance observations.

Protocol and binding validators are stubbed ONLY in this arithmetic/collector
suite; their separate integration tests exercise real fixture reconstruction.
Real common-engine receipts are covered by test_acceptance_execution.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_protocol import EVENT_RULES
from contextbase.acceptance_report import (_plan, build_report, store_collection, load_collection,
    verification_evidence, BILLING_RECONCILIATION_REASON, SPEC)
from contextbase.evidence_store import EvidenceStore
from contextbase.study_binding import _hash


def fixture(batches=30):
    origin = datetime(2026, 1, 2, tzinfo=timezone.utc)
    stamp = lambda seconds: (origin + timedelta(seconds=seconds)).isoformat()
    phases = ["baseline", "refreshed", "guard_a", "guard_b"]
    tasks = [{"id": phase + "/task", "phase_id": phase, "weight": 1,
              "control_kind": "analytic"} for phase in phases]
    protocol = {"protocol_sha256": "1" * 64, "runtime_sha256": "2" * 64, "context_sha256": "3" * 64,
        "model_id": "scripted-model", "frozen_at": stamp(0), "evidence_origin": "recorded_native",
        "budgets": {"max_model_turns_per_run": 2, "max_model_calls": batches * 20,
                    "comparison_usd": 100, "total_study_usd": 200, "study_seconds": 100000, "run_seconds": 8},
        "batches": [{"id": f"batch_{i:03d}"} for i in range(batches)], "tasks": tasks,
        "recurring_profile": {"phase_order": phases}, "provisioning_spend_usd": 1.0,
        "accounting_policy": {"event_rules": deepcopy(EVENT_RULES), "unknown_cost_policy": "inconclusive"},
        "intents": [{"batch_id": f"batch_{i:03d}", "provisioning": {}} for i in range(batches)],
        "bootstrap": {"seed": 7, "replicates": 2000, "confidence": .95},
        "planned": [{"batch_id": f"batch_{i:03d}", "task_id": task["id"], "repeat": 0, "arm": arm}
                    for i in range(batches) for task in tasks for arm in ("source", "workspace")]}
    collection = {"spec": SPEC, "protocol_sha256": protocol["protocol_sha256"], "phases": [], "events": [], "first_use": []}

    def event(kind, batch, phase, start, duration, *, arm=None, cost=0., evidence=None, role="exclusive_overhead"):
        identity = f"event-{len(collection['events'])}"
        value = {"event_id": identity, "kind": kind, "batch_id": batch, "phase_id": phase,
            "started_at": stamp(start), "finished_at": stamp(start + duration), "elapsed_seconds": duration,
            "allocation": {"source": .5, "workspace": .5} if arm is None else {"source": int(arm == "source"), "workspace": int(arm == "workspace")},
            "cost_usd": cost, "evidence_sha256": evidence or _hash(identity),
            "duration_role": role, "cost_role": "incremental", "attempt_id": None}
        collection["events"].append(value)
        return value

    event("context_creation", None, None, -10, 1., cost=.01)
    event("context_review", None, None, -9, 1., cost=.01)
    protocol["historical_context_events"] = deepcopy(collection["events"])
    protocol["historical_context_events_sha256"] = _hash(protocol["historical_context_events"])
    for batch_index in range(batches):
        batch = f"batch_{batch_index:03d}"
        base = 1 + batch_index * 100
        previous = None
        first_runs = {}
        for phase_index, phase in enumerate(phases):
            start = base + phase_index * 20
            transition = None
            if phase_index:
                transition = {"started_at": stamp(start), "finished_at": stamp(start + .1),
                              "elapsed_seconds": .1, "receipt_sha256": _hash([batch, phase, "transition"])}
                event("phase_transitions", batch, phase, start, .1, evidence=transition["receipt_sha256"])
            elif phase == "baseline":
                event("workflow_configuration", batch, phase, start, .1, arm="source", cost=.001)
                event("workflow_configuration", batch, phase, start, .1, arm="workspace", cost=.001)
            capture = {"receipt": {"observed_at": stamp(start + .25), "source_read_seconds": .02}}
            event("workspace_ingestion" if phase_index == 0 else "refresh", batch, phase,
                  start + .2, .1, arm="workspace", cost=.001, evidence=_hash(capture))
            binding = {"batch_id": batch, "phase_id": phase, "bound_at": stamp(start + .4), "capture": capture,
                "previous_binding": previous, "previous_binding_sha256": previous["binding_sha256"] if previous else None,
                "transition": transition, "public_tasks": [{"question": "Authored test question", "answer_schema": {"type": "object"}}],
                "evaluator_tasks": [{"id": phase + "/task", "expected": {"value": 1}}]}
            binding["binding_sha256"] = _hash(binding)
            plan = _plan(protocol, binding, None)
            execution = {"kind": "rystic_acceptance_phase", "plan": plan, "plan_sha256": plan["plan_sha256"],
                "expected_model_id": protocol["model_id"], "source_stability": "pre_and_post_match",
                "model_backend": "configured_claude_cli", "execution_complete": True, "stop_reason": None,
                "started_at": stamp(start + .5), "finished_at": stamp(start + 5.7), "elapsed_seconds": 5.2,
                "preflight_started_at": stamp(start + .5), "preflight_finished_at": stamp(start + .6), "preflight_seconds": .1,
                "postflight_started_at": stamp(start + 5.6), "postflight_finished_at": stamp(start + 5.7), "postflight_seconds": .1,
                "trials": [], "invocations": 2, "known_cost_usd": .032}
            for index, entry in enumerate(plan["queue"]):
                workspace = entry["arm"] == "sql_context"
                begin = start + (4.6 if workspace else .6)
                duration, cost = (1., .002) if workspace else (4., .03)
                answer = {"value": 0 if phase_index == 3 and not workspace else 1}
                trial = {**entry, "queue_index": index, "queue_entry": deepcopy(entry),
                    "trial_id": _hash({"plan_sha256": plan["plan_sha256"], "queue_index": index, "queue_entry": entry}),
                    "started_at": stamp(begin), "finished_at": stamp(begin + duration), "elapsed_seconds": duration,
                    "status": "answered", "answer": answer, "correct": answer == {"value": 1},
                    "known_cost_usd": cost, "cost_complete": True, "tool_invocations": [], "trace": [],
                    "inferences": [{"inference_index": 0, "started_at": stamp(begin), "finished_at": stamp(begin + duration),
                                    "measured_elapsed_seconds": duration, "cost_usd": cost,
                                    "model_usage": {protocol["model_id"]: {}}, "response": json.dumps({"answer": answer})}]}
                execution["trials"].append(trial)
                if phase == "baseline":
                    arm = "workspace" if workspace else "source"
                    first_runs[arm] = trial
                    collection["first_use"].append({"batch_id": batch, "arm": arm, "task_id": entry["task"], "repeat": 0,
                        "cold_started_at": stamp(base), "finished_at": trial["finished_at"], "evidence_sha256": _hash([batch, arm, "cold"])})
            for stage, offset in (("preflight", .5), ("postflight", 5.6)):
                event("evaluator_validation", batch, phase, start + offset, .1, evidence=_hash(verification_evidence(execution, stage)))
            collection["phases"].append({"binding": binding, "execution": execution})
            previous = binding
        event("runtime_license", batch, None, base, 80., cost=.001, role="cost_only")
        for arm in ("source", "workspace"):
            event("source_tool_calls", batch, None, base, 80., arm=arm, cost=0., role="cost_only")
    return protocol, collection


class AcceptanceReportTests(unittest.TestCase):
    def setUp(self):
        # Other suites validate the complete native-shape protocols/bindings.
        for name in ("validate_protocol", "_validate_stored"):
            patcher = patch("contextbase.acceptance_report." + name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_complete_fabricated_ledger_reconciles_original_clocks_and_paired_gates(self):
        protocol, collection = fixture()
        original = deepcopy((protocol, collection))
        report = build_report(protocol, collection)
        self.assertEqual(report["statistical_acceptance"], "passed", report["reasons"])
        self.assertFalse(report["charter_ready"])
        self.assertFalse(report["full_billing_reconciliation_verified"])
        self.assertTrue(all(report["gates"].values()))
        self.assertEqual(report["reasons"], [BILLING_RECONCILIATION_REASON])
        self.assertFalse(report["claim_eligible"])
        self.assertAlmostEqual(report["metrics"]["workspace"]["accuracy"], 1.)
        self.assertAlmostEqual(report["metrics"]["source"]["accuracy"], .75)
        self.assertEqual(report["model_calls"], 240)
        self.assertEqual((protocol, collection), original)
        self.assertEqual(report["first_use"]["status"], "recorded")
        self.assertEqual(len(report["task_scorecard"]["by_task"]), 4)
        self.assertAlmostEqual(report["known_total_study_spend_usd"] - report["known_comparison_spend_usd"], 1.)

    def test_missing_billing_does_not_suppress_scoped_failed_statistics(self):
        protocol, collection = fixture(1)
        source = next(trial for trial in collection["phases"][-1]["execution"]["trials"]
                      if trial["arm"] == "rystic_source")
        source.update(answer={"value": 1}, correct=True)
        source["inferences"][0]["response"] = json.dumps({"answer": source["answer"]})
        report = build_report(protocol, collection)
        self.assertEqual(report["statistical_acceptance"], "failed", report["reasons"])
        self.assertFalse(report["gates"]["accuracy_lift"])
        self.assertIsNotNone(report["metrics"])
        self.assertIsNotNone(report["paired_intervals"])
        self.assertFalse(report["charter_ready"])
        self.assertFalse(report["full_billing_reconciliation_verified"])
        self.assertEqual(report["reasons"].count(BILLING_RECONCILIATION_REASON), 1)

    def test_synthetic_sources_or_scripted_models_never_become_acceptance(self):
        for mutation in (lambda p, c: p.update(evidence_origin="synthetic_test"),
                         lambda p, c: c["phases"][0]["execution"].update(model_backend="injected_test_client")):
            protocol, collection = fixture(1)
            mutation(protocol, collection)
            report = build_report(protocol, collection)
            self.assertEqual(report["statistical_acceptance"], "inconclusive")
            self.assertFalse(report["charter_ready"])
            self.assertFalse(report["claim_eligible"])

    def test_fabricated_status_cannot_downgrade_a_correct_baseline_answer(self):
        protocol, collection = fixture(1)
        trial = collection["phases"][0]["execution"]["trials"][0]
        trial.update(status="trial_deadline", correct=False)
        report = build_report(protocol, collection)
        self.assertIn("actual deadline", " ".join(report["reasons"]))

    def test_reordered_missing_repeated_and_early_transition_evidence_is_rejected(self):
        mutations = [lambda c: c["phases"].reverse(), lambda c: c["phases"].pop(),
                     lambda c: c["phases"][1]["binding"]["transition"].update(started_at=c["phases"][0]["execution"]["started_at"]),
                     lambda c: c["phases"][0]["execution"]["trials"][0].update(correct=False),
                     lambda c: c["first_use"][1].update(evidence_sha256=c["first_use"][0]["evidence_sha256"])]
        for mutation in mutations:
            protocol, collection = fixture(1)
            mutation(collection)
            report = build_report(protocol, collection)
            self.assertEqual(report["statistical_acceptance"], "inconclusive", report)
            self.assertTrue(report["reasons"])

    def test_missing_unknown_retimed_or_uncovered_costs_stay_inconclusive(self):
        mutations = [lambda c: c["events"].pop(),
                     lambda c: c["events"][-1].update(cost_usd=None),
                     lambda c: c["events"][0].update(cost_usd=0),
                     lambda c: c["events"][-1].update(finished_at=c["events"][-1]["started_at"], elapsed_seconds=0),
                     lambda c: next(e for e in c["events"] if e["kind"] == "evaluator_validation").update(elapsed_seconds=0)]
        for mutation in mutations:
            protocol, collection = fixture(1)
            mutation(collection)
            report = build_report(protocol, collection)
            self.assertEqual(report["statistical_acceptance"], "inconclusive", report)

    def test_global_spend_calls_and_postrun_work_deadline_are_enforced(self):
        for key, value in (("comparison_usd", .01), ("total_study_usd", .01), ("max_model_calls", 7), ("study_seconds", 10)):
            protocol, collection = fixture(1)
            protocol["budgets"][key] = value
            # Preserve internally coherent local caps when testing global totals.
            for packet in collection["phases"]:
                packet["execution"]["plan"] = _plan(protocol, packet["binding"], None)
                plan = packet["execution"]["plan"]
                packet["execution"]["plan_sha256"] = plan["plan_sha256"]
                for i, trial in enumerate(packet["execution"]["trials"]):
                    trial["trial_id"] = _hash({"plan_sha256": plan["plan_sha256"], "queue_index": i, "queue_entry": trial["queue_entry"]})
                for stage in ("preflight", "postflight"):
                    for event in collection["events"]:
                        if event["kind"] == "evaluator_validation" and event["started_at"] == packet["execution"][stage + "_started_at"]:
                            event["evidence_sha256"] = _hash(verification_evidence(packet["execution"], stage))
            report = build_report(protocol, collection)
            self.assertEqual(report["statistical_acceptance"], "inconclusive", report)

    def test_content_addressed_index_roundtrip_deduplicates_binding_history(self):
        protocol, collection = fixture(1)
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "receipts")
            index = store_collection(store, protocol, collection)
            self.assertEqual(load_collection(store, index), (protocol, collection))
            count = len(list(store.directory.glob("*.json"))) if hasattr(store, "directory") else len(list((Path(directory) / "receipts").glob("*.json")))
            self.assertEqual(store_collection(store, protocol, collection), index)
            self.assertEqual(len(list((Path(directory) / "receipts").glob("*.json"))), count)
            report = build_report(*load_collection(store, index))
            self.assertEqual(report["statistical_acceptance"], "passed", report["reasons"])
            self.assertFalse(report["charter_ready"])
            self.assertFalse(report["full_billing_reconciliation_verified"])
            self.assertEqual(report["reasons"], [BILLING_RECONCILIATION_REASON])

    def test_normal_cli_replay_cannot_lift_missing_billing_gate(self):
        from contextbase.cli import execute, parser
        # This authored ledger carries a native label to exercise the exact
        # statistical-pass branch. It is fabricated test data, not native proof.
        protocol, collection = fixture(1)
        direct = build_report(protocol, collection)
        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native sources")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            root = Path(directory)
            store = EvidenceStore(root / "receipts")
            index = store_collection(store, protocol, collection)
            (root / "index.json").write_text(json.dumps(index))
            summary = execute(parser().parse_args(["--workspace", str(root / "unused"), "rystic",
                "acceptance-report", "--index", str(root / "index.json"), "--store", str(root / "receipts"),
                "--output", str(root / "report.json")]))
            replayed = json.loads((root / "report.json").read_text())
            self.assertEqual(replayed, direct)
            self.assertEqual(summary["statistical_acceptance"], "passed")
            self.assertFalse(summary["charter_ready"])
            self.assertFalse(summary["claim_eligible"])
            self.assertEqual(summary["reasons"], [BILLING_RECONCILIATION_REASON])
            self.assertFalse((root / "unused").exists())

    def test_offline_cli_pack_and_report_create_private_outputs_without_model_or_source(self):
        from contextbase.cli import execute, parser
        protocol, collection = fixture(1)
        protocol["evidence_origin"] = "synthetic_test"
        with tempfile.TemporaryDirectory() as directory, \
             patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No native sources")), \
             patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model")):
            root = Path(directory)
            for name, value in (("protocol", protocol), ("collection", collection)):
                (root / (name + ".json")).write_text(json.dumps(value))
            def command(*args):
                return execute(parser().parse_args(["--workspace", str(root / "unused"), "rystic", *args]))
            index, output, store = root / "index.json", root / "report.json", root / "receipts"
            summary = command("acceptance-pack", "--protocol", str(root / "protocol.json"),
                "--collection", str(root / "collection.json"), "--store", str(store), "--output", str(index))
            self.assertEqual(summary["model_calls"], 0)
            self.assertEqual(summary["source_calls"], 0)
            result = command("acceptance-report", "--index", str(index), "--store", str(store), "--output", str(output))
            self.assertEqual(result["statistical_acceptance"], "inconclusive")
            self.assertFalse(result["claim_eligible"])
            for path in (index, output, *store.glob("*.json")):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "unused").exists())
            with self.assertRaises(FileExistsError):
                command("acceptance-report", "--index", str(index), "--store", str(store), "--output", str(output))


if __name__ == "__main__":
    unittest.main()
