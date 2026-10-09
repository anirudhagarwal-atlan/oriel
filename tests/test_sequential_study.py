"""Fabricated source/ledger evidence tests contracts, never agent performance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase import study_report as v1
from contextbase.rystic_validation import capture_validation
from contextbase.study_binding import bind_phase, _hash
from contextbase.sequential_study import SPEC, PROVENANCE_SLOT, task_commitment, evaluate_sequential_study
from test_study_binding import binding_fixture, source_rows, FakePair, fake_snapshot


def rehash(record, field="binding_sha256"):
    record[field] = _hash({key: value for key, value in record.items() if key != field})


def relock(protocol, observations):
    protocol["protocol_sha256"] = v1._digest({key: value for key, value in protocol.items() if key != "protocol_sha256"})
    previous = None
    for wrapper in observations["phase_bindings"]:
        rehash(wrapper["packet"])
        wrapper["protocol_sha256"] = protocol["protocol_sha256"]
        wrapper["previous_binding_sha256"] = previous
        rehash(wrapper)
        previous = wrapper["binding_sha256"]
        for run in observations["runs"]:
            if run["task_id"] in wrapper["packet"]["task_artifacts"]:
                run["provenance"].update(protocol_sha256=protocol["protocol_sha256"],
                                          phase_binding_sha256=wrapper["binding_sha256"])


def sequential_fixture():
    """Actual binder over an in-memory fake source; all timing/costs are invented."""
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base = frozen + timedelta(days=1)
    stamp = lambda seconds: (base + timedelta(seconds=seconds)).isoformat()
    observations = {"runs": [], "batches": [], "phases": [], "transitions": [], "first_use": [], "phase_bindings": []}
    with tempfile.TemporaryDirectory() as directory:
        plan, _, _ = binding_fixture(directory, "baseline")
        order = plan["recurring_profile"]["phase_order"]
        kinds = plan["recurring_profile"]["phase_kinds"]
        protocol = {"spec": SPEC, "kind": "acceptance", "held_out": True, "evidence_origin": "observed",
                    "frozen_at": frozen.isoformat(), "model_id": "fabricated-test-model", "runtime_sha256": _hash("runtime"),
                    "context_sha256": _hash("context"), "tasks": deepcopy(plan["tasks"]), "planned": [], "repeats": 1,
                    "recurring_profile": deepcopy(plan["recurring_profile"]),
                    "caps": {"run_seconds": 60, "total_cost_usd": 10000},
                    "bootstrap": {"seed": 7, "replicates": 2000, "confidence": 0.95},
                    "power": {"minimum_independent_batches": 30, "target_power": 0.8, "analysis_sha256": _hash("test-power"), "reviewed": True}}
        batch = {"id": "batch_0", "fixture_sha256": plan["seed_manifest_sha256"], "plan_sha256": plan["plan_sha256"],
                 "phase_intents": {phase["id"]: phase["intent_sha256"] for phase in plan["phases"]}, "task_commitments": {}}
        protocol["batches"] = [batch]
        previous_source = None
        for index, phase in enumerate(order):
            offset = index * 500
            capture = capture_validation(FakePair(source_rows(plan, phase)), plan["phases"][index]["intent_manifest"]["namespace"])
            capture["receipt"].update(observed_at=stamp(offset + 2), source_read_seconds=1)
            snapshot = fake_snapshot(capture)
            if snapshot is not None:
                # Match the actual sync_snapshot receipt's additional fields.
                snapshot.update(scope="Only the explicitly marked recipe cohort", consistency=capture["receipt"]["consistency"],
                                table_sha256={name: _hash([phase, name]) for name in snapshot["rows"]},
                                previous_snapshot_id=None if index == 0 else "rystic-" + previous_source[:16], data_changed=True)
            packet = bind_phase(plan, phase, capture, Path(directory) / (phase + "-packet.json"),
                                snapshot=snapshot, evidence_origin="recorded_native")
            source_hash = capture["receipt"]["data_sha256"]
            transition_hash = None
            if index:
                transition_hash = _hash(["transition", phase])
                observations["transitions"].append({"batch_id": "batch_0", "from_phase_id": order[index-1], "to_phase_id": phase,
                    "before_sha256": previous_source, "after_sha256": source_hash, "started_at": stamp(offset-5),
                    "finished_at": stamp(offset), "evidence_sha256": transition_hash})
            previous_source = source_hash
            observations["phase_bindings"].append({"batch_id": "batch_0", "phase_id": phase, "protocol_sha256": "pending",
                "previous_binding_sha256": None, "transition_receipt_sha256": transition_hash, "bound_at": stamp(offset+6),
                "capture_observed_at": stamp(offset+2), "packet": packet})
            for pub, private in zip(packet["public_tasks"], packet["evaluator_tasks"]):
                batch["task_commitments"][private["id"]] = task_commitment(pub, private["expected"], plan["scorer_sha256"])
            for arm in v1.ARMS:
                source = arm == "source"
                for task_index, private in enumerate(packet["evaluator_tasks"]):
                    start = offset + (10 + task_index*30 if source else 200 + task_index*10)
                    duration = 20 if source else 5
                    correct = not source or task_index % 2 == 0
                    key = {"batch_id": "batch_0", "task_id": private["id"], "repeat": 0, "arm": arm}
                    protocol["planned"].append(key)
                    run = {**key, "correct": correct, "started_at": stamp(start), "finished_at": stamp(start+duration+1),
                        "overhead_seconds": 1, "scored_evidence_sha256": _hash(["scored", phase, arm, task_index]),
                        "provenance": {"model_id": protocol["model_id"], "runtime_sha256": protocol["runtime_sha256"],
                                       "context_sha256": protocol["context_sha256"], "fixture_sha256": source_hash,
                                       **packet["task_artifacts"][private["id"]]},
                        "attempts": [{"correct": correct, "started_at": stamp(start), "finished_at": stamp(start+duration),
                            "elapsed_seconds": duration, "model_cost_usd": 2 if source else 0.25, "tool_cost_usd": 0.1 if source else 0.05,
                            "evidence_sha256": _hash(["attempt", phase, arm, task_index])}]}
                    observations["runs"].append(run)
                    if index == task_index == 0:
                        observations["first_use"].append({**key, "cold_started_at": stamp(0), "finished_at": run["finished_at"],
                                                         "evidence_sha256": _hash(["first-use", arm])})
                overhead = 3 if source else 6
                observations["phases"].append({"batch_id": "batch_0", "arm": arm, "phase_id": phase, "source_sha256": source_hash,
                    "started_at": stamp(offset), "ready_at": stamp(offset+overhead), "finished_at": stamp(offset+400),
                    "evidence_sha256": _hash(["phase", phase, arm]),
                    "receipt_kind": "source_state_validation" if source or kinds[phase] == "guarded" else "materialized_snapshot",
                    "readiness_mode": "refused_invalid_source" if not source and kinds[phase] == "guarded" else "ready",
                    "costs": {"setup": 0.4 if index == 0 else 0, "refresh": 0 if index == 0 else 0.1, "common_review": 0, "runtime": 0},
                    "seconds": {"setup": overhead if index == 0 else 0, "refresh": 0 if index == 0 else overhead, "common_review": 0, "runtime": 0}})
        for arm in v1.ARMS:
            phases = [row for row in observations["phases"] if row["arm"] == arm]
            observations["batches"].append({"batch_id": "batch_0", "arm": arm, "started_at": stamp(0), "finished_at": stamp(1900),
                "evidence_sha256": _hash(["batch-accounting", arm]),
                **{field: {component: sum(row[field][component] for row in phases) for component in v1.COMPONENTS} for field in ("costs", "seconds")}})
        relock(protocol, observations)
    return protocol, observations


class SequentialStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol, cls.observations = sequential_fixture()

    def fresh(self):
        return deepcopy(self.protocol), deepcopy(self.observations)

    def invalid(self, protocol, observations, reason=None):
        report = evaluate_sequential_study(protocol, observations)
        self.assertEqual(report["statistical_acceptance"], "inconclusive")
        self.assertFalse(report["claim_eligible"])
        self.assertFalse(report["charter_ready"])
        if reason:
            self.assertIn(reason, " ".join(report["reasons"]))
        return report

    def test_real_binder_shape_preserves_original_freeze_and_all_v1_accounting(self):
        protocol, observations = self.fresh()
        originals = deepcopy((protocol, observations))
        report = self.invalid(protocol, observations, "Development-only")
        self.assertTrue(report["sequential_binding_validated"], report)
        self.assertEqual(report["protocol_sha256"], protocol["protocol_sha256"])
        self.assertEqual(report["protocol_spec"], SPEC)
        self.assertNotIn("manifest_sha256", report)
        self.assertFalse(report["statistical_projection"]["is_preregistration"])
        self.assertEqual(report["statistical_projection"]["frozen_at"], protocol["frozen_at"])
        self.assertEqual(report["metrics"]["source"]["planned_runs"], 12)
        self.assertEqual(report["metrics"]["workspace"]["planned_runs"], 12)
        self.assertAlmostEqual(report["metrics"]["source"]["total_cost_usd"], 12*2.1+0.7)
        self.assertAlmostEqual(report["metrics"]["workspace"]["total_cost_usd"], 12*0.3+0.7)
        self.assertEqual(report["first_use"]["by_arm"]["workspace"]["observed_seconds"]["p50"], 206)
        self.assertLess(observations["phases"][0]["ready_at"], observations["phase_bindings"][0]["bound_at"])
        self.assertTrue(any("allocation" in item for item in report["limitations"]))
        self.assertEqual((protocol, observations), originals)

    def test_only_exact_provenance_substitution_is_late_bound(self):
        packet = self.observations["phase_bindings"][0]["packet"]
        public, expected = deepcopy(packet["public_tasks"][0]), deepcopy(packet["evaluator_tasks"][0]["expected"])
        scorer = packet["artifact_hashes"]["scorer_sha256"]
        before = task_commitment(public, expected, scorer)
        old = json.dumps(expected["provenance"], sort_keys=True)
        expected["provenance"].update(data_sha256="a"*64, snapshot_id="rystic-"+"a"*16)
        public["question"] = public["question"].replace(old, json.dumps(expected["provenance"], sort_keys=True))
        self.assertEqual(task_commitment(public, expected, scorer), before)
        for case in ("missing", "duplicate", "placeholder", "extra_provenance", "bad_alias"):
            pub, gold = deepcopy(public), deepcopy(expected)
            block = json.dumps(gold["provenance"], sort_keys=True)
            if case == "missing": pub["question"] = pub["question"].replace(block, "")
            if case == "duplicate": pub["question"] += block
            if case == "placeholder": pub["question"] += PROVENANCE_SLOT
            if case == "extra_provenance": gold["provenance"]["expected_result"] = 1
            if case == "bad_alias": gold["provenance"]["snapshot_id"] = "arbitrary"
            with self.subTest(case=case), self.assertRaises(ValueError):
                task_commitment(pub, gold, scorer)
        expected["nested"] = {"provenance": {"value": "semantic"}}
        self.assertNotEqual(task_commitment(public, expected, scorer)["semantic_expected_sha256"], before["semantic_expected_sha256"])

    def test_semantic_gold_question_schema_and_scorer_cannot_change_after_freeze(self):
        for case in ("gold", "question", "schema", "scorer"):
            protocol, observations = self.fresh()
            packet = observations["phase_bindings"][0]["packet"]
            if case == "gold": packet["evaluator_tasks"][0]["expected"]["extra"] = 1
            if case == "question": packet["public_tasks"][0]["question"] += " Use a different policy."
            if case == "schema": packet["public_tasks"][0]["answer_schema"]["extra"] = True
            if case == "scorer": packet["artifact_hashes"]["scorer_sha256"] = _hash("changed")
            packet["artifact_hashes"].update(public_tasks_sha256=_hash(packet["public_tasks"]), evaluator_tasks_sha256=_hash(packet["evaluator_tasks"]))
            relock(protocol, observations)
            with self.subTest(case=case):
                self.invalid(protocol, observations, "Task semantic gold")

    def test_missing_duplicate_replaced_or_reordered_bindings_and_tasks_fail(self):
        for case in ("missing", "duplicate", "reordered", "chain", "packet", "intent", "task"):
            protocol, observations = self.fresh()
            wrappers = observations["phase_bindings"]
            if case == "missing": wrappers.pop()
            if case == "duplicate": wrappers.append(deepcopy(wrappers[0]))
            if case == "reordered": wrappers[1], wrappers[2] = wrappers[2], wrappers[1]
            if case == "chain": wrappers[1]["previous_binding_sha256"] = _hash("wrong"); rehash(wrappers[1])
            if case == "packet": wrappers[0]["packet"]["binding_sha256"] = _hash("wrong"); rehash(wrappers[0])
            if case == "intent": wrappers[0]["packet"]["artifact_hashes"]["phase_intent_sha256"] = _hash("wrong"); relock(protocol, observations)
            if case == "task": wrappers[0]["packet"]["evaluator_tasks"].pop(); relock(protocol, observations)
            with self.subTest(case=case):
                self.assertNotIn("sequential_binding_validated", self.invalid(protocol, observations))

    def test_capture_and_binding_chronology_precede_both_arms(self):
        for case in ("before_freeze", "capture_before_transition", "bind_before_capture", "late_binding", "different_observed_time", "transition_reference"):
            protocol, observations = self.fresh()
            wrapper = observations["phase_bindings"][1]
            if case == "before_freeze": wrapper["capture_observed_at"] = protocol["frozen_at"]
            if case == "capture_before_transition": wrapper["packet"]["validation_receipt"]["source_read_seconds"] = 10
            if case == "bind_before_capture": wrapper["bound_at"] = observations["transitions"][0]["finished_at"]
            if case == "late_binding": wrapper["bound_at"] = observations["phases"][2]["finished_at"]
            if case == "different_observed_time": wrapper["capture_observed_at"] = wrapper["bound_at"]
            if case == "transition_reference": wrapper["transition_receipt_sha256"] = _hash("other")
            if case == "capture_before_transition":
                packet = wrapper["packet"]
                packet["snapshot_receipt"]["source_read_seconds"] = 10
                packet["snapshot_receipt"]["source_validation_sha256"] = _hash(packet["validation_receipt"])
                packet["artifact_hashes"]["snapshot_receipt_sha256"] = _hash(packet["snapshot_receipt"])
            relock(protocol, observations)
            with self.subTest(case=case):
                self.assertNotIn("sequential_binding_validated", self.invalid(protocol, observations))

    def test_run_identity_cannot_be_hidden_by_internal_projection(self):
        for field in ("protocol_sha256", "phase_binding_sha256", "model_id", "runtime_sha256", "context_sha256",
                      "fixture_sha256", "question_sha256", "response_schema_sha256", "expected_sha256", "scorer_sha256"):
            protocol, observations = self.fresh()
            observations["runs"][1]["provenance"][field] = _hash("wrong")
            with self.subTest(field=field):
                self.invalid(protocol, observations, "Run provenance differs")

    def test_original_protocol_freeze_and_distinct_measurement_roles_are_enforced(self):
        protocol, observations = self.fresh()
        protocol["caps"]["total_cost_usd"] += 1
        self.invalid(protocol, observations, "Protocol changed after freezing")
        for location in ("scored", "attempt", "accounting"):
            protocol, observations = self.fresh()
            reused = observations["phase_bindings"][0]["binding_sha256"]
            if location == "scored": observations["runs"][0]["scored_evidence_sha256"] = reused
            if location == "attempt": observations["runs"][0]["attempts"][0]["evidence_sha256"] = reused
            if location == "accounting": observations["batches"][0]["evidence_sha256"] = reused
            with self.subTest(location=location):
                self.invalid(protocol, observations, "Measurement receipt reused")

    def test_unknown_wrapper_packet_and_accounting_fields_cannot_hide_spend(self):
        for location in ("wrapper", "packet", "batch", "phase", "attempt", "first_use"):
            protocol, observations = self.fresh()
            target = {"wrapper": observations["phase_bindings"][0],
                      "packet": observations["phase_bindings"][0]["packet"],
                      "batch": observations["batches"][0], "phase": observations["phases"][0],
                      "attempt": observations["runs"][0]["attempts"][0], "first_use": observations["first_use"][0]}[location]
            target["omitted_model_cost_usd"] = 7
            relock(protocol, observations)
            with self.subTest(location=location):
                report = self.invalid(protocol, observations)
                self.assertTrue(any("fields" in reason for reason in report["reasons"]), report)

    def test_native_label_or_modified_flags_cannot_promote_development_packets(self):
        self.invalid(*self.fresh(), "Development-only")
        for field in ("held_out", "execution_authorized", "claim_eligible"):
            protocol, observations = self.fresh()
            observations["phase_bindings"][0]["packet"][field] = True
            relock(protocol, observations)
            self.invalid(protocol, observations, "Packet cannot assert")
        protocol, observations = self.fresh()
        observations["phase_bindings"][0]["packet"]["evidence_origin"] = "synthetic_test"
        relock(protocol, observations)
        self.invalid(protocol, observations, "Observed studies cannot use synthetic")

    def test_unknown_spend_missing_cost_and_duplicate_attempts_retain_v1_refusals(self):
        for case in ("unknown", "missing", "duplicate", "schedule", "accounting", "first_use"):
            protocol, observations = self.fresh()
            if case == "unknown": observations["runs"][0]["additional_cost"] = 3
            if case == "missing": del observations["runs"][0]["attempts"][0]["model_cost_usd"]
            if case == "duplicate": observations["runs"][1]["attempts"][0]["evidence_sha256"] = observations["runs"][0]["attempts"][0]["evidence_sha256"]
            if case == "schedule": observations["runs"].pop()
            if case == "accounting": observations["phases"][0]["seconds"]["setup"] = 0
            if case == "first_use": observations["first_use"][0]["cold_started_at"] = protocol["frozen_at"]
            with self.subTest(case=case):
                report = self.invalid(protocol, observations)
                self.assertTrue(any("Development-only" not in reason for reason in report["reasons"]), report)

    def test_receipt_semantics_and_hash_kinds_cannot_contradict_tasks(self):
        for case in ("pins", "counts", "blockers", "hash_kind", "stale_snapshot"):
            protocol, observations = self.fresh()
            wrapper = observations["phase_bindings"][2]
            receipt = wrapper["packet"]["validation_receipt"]
            if case == "pins": next(iter(receipt["simulators"].values()))["version"] = "wrong"
            if case == "counts": receipt["source_rows"]["stripe.customers"] = True
            if case == "blockers": receipt["blockers"][0]["record_id"] = "unrelated-record"
            if case == "hash_kind": receipt["hash_kind"] = "normalized_tables/v1"
            if case == "stale_snapshot": wrapper["packet"]["snapshot_receipt"] = deepcopy(observations["phase_bindings"][0]["packet"]["snapshot_receipt"])
            relock(protocol, observations)
            with self.subTest(case=case):
                self.assertNotIn("sequential_binding_validated", self.invalid(protocol, observations))

    def test_cli_dispatches_v2_using_only_supplied_local_files(self):
        from contextbase.cli import execute, parser
        protocol, observations = self.fresh()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol_path, observations_path, output = (root / name for name in ("protocol.json", "observations.json", "report.json"))
            protocol_path.write_text(json.dumps(protocol))
            observations_path.write_text(json.dumps(observations))
            args = parser().parse_args(["--workspace", str(root / "unused-workspace"), "rystic", "study-report",
                "--manifest", str(protocol_path), "--observations", str(observations_path), "--output", str(output)])
            with patch("contextbase.rystic.RysticPair", side_effect=AssertionError("No source calls")) as source, \
                 patch("contextbase.model.ClaudeCLI", side_effect=AssertionError("No model calls")) as model:
                summary = execute(args)
                source.assert_not_called()
                model.assert_not_called()
            report = json.loads(output.read_text())
            self.assertEqual(report["spec"], "contextbase.sequential-study-report/v2")
            self.assertEqual(report["protocol_sha256"], protocol["protocol_sha256"])
            self.assertTrue(report["sequential_binding_validated"])
            self.assertEqual(summary["statistical_acceptance"], "inconclusive")
            self.assertFalse(summary["claim_eligible"])
            self.assertFalse((root / "unused-workspace").exists())


if __name__ == "__main__":
    unittest.main()
