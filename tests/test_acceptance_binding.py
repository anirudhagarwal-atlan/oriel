"""Focused binder checks with authored observations, never acceptance evidence.

These tests replace only the expensive full protocol validator with a monitored
stub; the protocol/intent integration suite independently exercises that gate.
All phase captures and transitions use in-memory source doubles, not a runtime.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from contextbase.acceptance_binding import bind_acceptance_phase, _validate_binding, SPEC
from contextbase.rystic_validation import capture_validation
from contextbase.study_binding import _hash, bind_phase
from contextbase.study_templates import compile_templates
from contextbase.study_transitions import apply_transition
from test_study_binding import binding_fixture, fake_snapshot as minimal_snapshot
from test_study_transitions import MutationPair


def rehash(value, field):
    value[field] = _hash({key: item for key, item in value.items() if key != field})


def fake_snapshot(capture, *, previous=None):
    """Complete-shape fabricated metadata, not a native import or table-hash claim."""
    result = minimal_snapshot(capture)
    if result is not None:
        result.update(scope="Only the explicitly marked recipe cohort", consistency=capture["receipt"]["consistency"],
                      table_sha256={name: "a" * 64 for name in capture["tables"]},
                      previous_snapshot_id=previous["snapshot_id"] if previous is not None else None, data_changed=True)
    return result


class AcceptanceBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan, baseline, _ = binding_fixture(self.root, "baseline")
        self.pair = MutationPair(self.plan)
        templates = compile_templates(self.plan)
        intent = {"batch_id": "batch_001", "intent_sha256": "1" * 64, "phase_plan": self.plan,
                  "templates": templates, "baseline_capture": baseline, "provisioning_sha256": "2" * 64}
        self.protocol = {"spec": "contextbase.acceptance-protocol/v1", "protocol_sha256": "3" * 64,
                         "allocation_sha256": "4" * 64, "frozen_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                         "evidence_origin": "synthetic_test", "intents": [intent],
                         "batches": [{"id": "batch_001", "fixture_sha256": "5" * 64,
                                      "task_commitments": templates["task_commitments"]}]}
        validator = patch("contextbase.acceptance_protocol.validate_protocol")
        self.validator = validator.start()
        self.addCleanup(validator.stop)

    def capture(self):
        return capture_validation(self.pair, self.plan["phases"][0]["intent_manifest"]["namespace"])

    def baseline(self, **kwargs):
        capture = self.capture()
        return bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture,
                                     snapshot=fake_snapshot(capture), **kwargs)

    def advance(self, index):
        declared = self.plan["transitions"][index]
        return apply_transition(self.pair, self.plan, declared["from_phase"], declared["to_phase"],
                                self.root / ("transition-" + str(index) + ".json"),
                                approve_plan=self.plan["plan_sha256"], approve_action=declared["action_sha256"])

    def test_all_phases_rebuild_exact_frozen_commitments_and_private_evidence_chain(self):
        binding = self.baseline()
        for index, phase in enumerate(("baseline", "refreshed", "guard_a", "guard_b")):
            if index:
                transition = self.advance(index - 1)
                capture = self.capture()
                binding = bind_acceptance_phase(self.protocol, "batch_001", phase, capture,
                    snapshot=fake_snapshot(capture, previous=binding["snapshot"]), previous_binding=binding, transition=transition)
            _validate_binding(self.protocol, binding)
            self.assertEqual(binding["spec"], SPEC)
            self.assertEqual(len(binding["public_tasks"]), 5 if index < 2 else 1)
            self.assertEqual(binding["protocol_sha256"], self.protocol["protocol_sha256"])
            self.assertEqual(binding["allocation_sha256"], self.protocol["allocation_sha256"])
            self.assertEqual(binding["binding_sha256"], _hash({k: v for k, v in binding.items() if k != "binding_sha256"}))
            self.assertFalse(binding["execution_authorized"])
            self.assertFalse(binding["claim_eligible"])
            self.assertEqual(binding["heldout_independence"], "not_authenticated")
            self.assertEqual(binding["source_calls"], 0)
            self.assertEqual(binding["model_calls"], 0)
            self.assertTrue(all(set(item) == {"question", "answer_schema"} for item in binding["public_tasks"]))
            for task, commitment in binding["task_commitments"].items():
                self.assertEqual(commitment, self.protocol["batches"][0]["task_commitments"][task])
        self.assertGreaterEqual(self.validator.call_count, 8)

    def test_protocol_gate_runs_before_output_or_binding_work(self):
        self.validator.side_effect = ValueError("Original protocol rejected")
        output = self.root / "rejected.json"
        with self.assertRaisesRegex(ValueError, "Original protocol rejected"):
            self.baseline(output=output)
        self.assertFalse(output.exists())

    def test_only_new_private_output_is_written_after_checks(self):
        output = self.root / "private.json"
        binding = self.baseline(output=output)
        original = output.read_bytes()
        self.assertEqual(json.loads(original), binding)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            self.baseline(output=output)
        self.assertEqual(output.read_bytes(), original)

    def test_baseline_cannot_upgrade_a_development_packet_or_accept_a_prior_chain(self):
        capture = self.capture()
        development = bind_phase(self.plan, "baseline", capture, self.root / "development.json", snapshot=fake_snapshot(capture))
        development["spec"] = SPEC
        rehash(development, "binding_sha256")
        with self.assertRaisesRegex(ValueError, "original acceptance"):
            _validate_binding(self.protocol, development)
        with self.assertRaises(ValueError):
            self.baseline(previous_binding=development)
        with self.assertRaises(ValueError):
            bind_acceptance_phase(self.protocol, "batch_001", "baseline", development)

    def test_observation_end_after_freeze_is_insufficient_when_read_started_before_freeze(self):
        capture = self.capture()
        frozen = datetime.fromisoformat(self.protocol["frozen_at"])
        capture["receipt"]["observed_at"] = (frozen + timedelta(milliseconds=100)).isoformat()
        capture["receipt"]["source_read_seconds"] = 0.5
        with self.assertRaisesRegex(ValueError, "start after protocol freeze"):
            bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture, snapshot=fake_snapshot(capture))

    def test_semantic_task_commitments_cannot_change_with_provenance_binding(self):
        self.protocol["batches"][0]["task_commitments"]["baseline/exposure_summary"]["semantic_expected_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "semantic task commitment"):
            self.baseline()

    def test_previous_binding_cannot_be_rehashed_after_gold_or_origin_edits(self):
        binding = self.baseline()
        for mutate in (lambda value: value.update(evidence_origin="recorded_native"),
                       lambda value: value["evaluator_tasks"][0].update(expected={"invented": "gold"}),
                       lambda value: value.update(claim_eligible=True)):
            altered = deepcopy(binding)
            mutate(altered)
            rehash(altered, "binding_sha256")
            with self.assertRaises(ValueError):
                _validate_binding(self.protocol, altered)

    def test_later_phase_requires_exact_adjacent_prior_binding_and_applied_transition(self):
        prior = self.baseline()
        transition = self.advance(0)
        capture = self.capture()
        args = dict(snapshot=fake_snapshot(capture, previous=prior["snapshot"]), previous_binding=prior, transition=transition)
        for changed in (dict(previous_binding=None), dict(transition=None),
                        dict(previous_binding={**prior, "batch_id": "batch_999"})):
            with self.assertRaises(ValueError):
                bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture, **{**args, **changed})
        for mutate in (lambda value: value.update(outcome="write_uncertain"),
                       lambda value: value.update(write_attempts=100),
                       lambda value: value.update(elapsed_seconds=0),
                       lambda value: value.update(elapsed_seconds=value["elapsed_seconds"] + 10),
                       lambda value: value.update(action_sha256="0" * 64),
                       lambda value: value["operations"][0].update(status="effect_uncertain"),
                       lambda value: value["operations"][0]["request"]["variables"]["input"].update(stateId="wrong")):
            altered = deepcopy(transition)
            mutate(altered)
            rehash(altered, "receipt_sha256")
            with self.assertRaises(ValueError):
                bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture, **{**args, "transition": altered})

    def test_transition_embedded_capture_is_too_early_for_current_phase_binding(self):
        prior = self.baseline()
        transition = self.advance(0)
        capture = transition["after_capture"]
        with self.assertRaisesRegex(ValueError, "chronology"):
            bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture, snapshot=fake_snapshot(capture, previous=prior["snapshot"]),
                                  previous_binding=prior, transition=transition)

    def test_full_before_and_after_states_must_match_beyond_authored_semantics(self):
        prior = self.baseline()
        transition = self.advance(0)
        capture = self.capture()
        args = dict(snapshot=fake_snapshot(capture, previous=prior["snapshot"]), previous_binding=prior, transition=transition)
        altered = deepcopy(transition)
        before = altered["before_capture"]
        before["observed_source"]["rows"]["stripe.customers"][0]["extra_observed_field"] = "changed"
        before["receipt"]["observed_source_sha256"] = _hash(before["observed_source"])
        rehash(altered, "receipt_sha256")
        with self.assertRaisesRegex(ValueError, "Previous bound source state raw projection"):
            bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture, **{**args, "transition": altered})
        capture["observed_source"]["rows"]["stripe.customers"][0]["extra_observed_field"] = "changed"
        capture["receipt"]["observed_source_sha256"] = _hash(capture["observed_source"])
        with self.assertRaisesRegex(ValueError, "Current bound source state raw projection"):
            bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture,
                                  **{**args, "snapshot": fake_snapshot(capture, previous=prior["snapshot"])})

    def test_baseline_refuses_reused_workspace_and_refresh_requires_baseline_predecessor(self):
        capture = self.capture()
        for mutate in (lambda value: value.update(previous_snapshot_id="rystic-" + "a" * 16),
                       lambda value: value.update(data_changed=False)):
            snapshot = fake_snapshot(capture)
            mutate(snapshot)
            with self.assertRaises(ValueError):
                bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture, snapshot=snapshot)
        prior = self.baseline()
        transition = self.advance(0)
        capture = self.capture()
        for mutate in (lambda value: value.update(previous_snapshot_id=None),
                       lambda value: value.update(previous_snapshot_id="rystic-" + "f" * 16),
                       lambda value: value.update(data_changed=False)):
            snapshot = fake_snapshot(capture, previous=prior["snapshot"])
            mutate(snapshot)
            with self.assertRaises(ValueError):
                bind_acceptance_phase(self.protocol, "batch_001", "refreshed", capture, snapshot=snapshot,
                                      previous_binding=prior, transition=transition)

    def test_canonical_capture_and_snapshot_shapes_refuse_extra_claim_fields(self):
        capture = self.capture()
        with self.assertRaisesRegex(ValueError, "complete stored"):
            bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture, snapshot=minimal_snapshot(capture))
        snapshot = fake_snapshot(capture)
        snapshot["workspace_imported"] = False
        with self.assertRaises(ValueError):
            bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture, snapshot=snapshot)
        capture["receipt"]["claim_eligible"] = True
        with self.assertRaises(ValueError):
            bind_acceptance_phase(self.protocol, "batch_001", "baseline", capture, snapshot=fake_snapshot(capture))
