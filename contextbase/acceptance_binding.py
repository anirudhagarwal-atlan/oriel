"""Late-bind observed phase provenance to an original frozen acceptance protocol.

This is an offline consistency check, not source authentication, execution
authorization, a completed study, or proof of held-out independence. Only the
public task question/schema pairs may enter agent prompts. All other fields,
including the recursive evidence chain and gold, belong to the evaluator.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re

from .rystic import local_url
from .rystic_eval import build_guarded_task, build_phase_tasks
from .sequential_study import SNAPSHOT_FIELDS, STORED_SNAPSHOT_FIELDS, VALIDATION_FIELDS, task_commitment
from .study_binding import _check, _duration, _hash, _json, _same, _snapshot, _timestamp, _validation
from .study_phases import PHASE_ORDER
from .study_transitions import SPEC as TRANSITION_SPEC, _operations

SPEC = "contextbase.acceptance-phase-binding/v1"
LIMITATIONS = [
    "Hashes establish consistency of supplied evidence, not authenticated native execution or complete pagination.",
    "Declared origin and holdout reviews do not prove held-out independence or authorize execution.",
    "The collector/report must verify that both arms completed the preceding phase before its transition, and that this binding preceded both current-phase queries.",
    "Only public_tasks question/schema entries may enter agent prompts; all remaining evidence, identifiers and gold are evaluator-only.",
    "Successful import is a supplied receipt assertion; this binder performs no workspace or source access.",
    "This binding alone is not a completed acceptance study or a public superiority claim.",
]
BINDING_FIELDS = {
    "spec", "protocol_sha256", "allocation_sha256", "batch_id", "phase_id", "intent_sha256",
    "fixture_sha256", "plan_sha256", "phase_intent_sha256", "provisioning_sha256",
    "previous_binding_sha256", "transition_sha256", "previous_binding", "transition", "capture", "snapshot",
    "public_tasks", "evaluator_tasks", "task_artifacts", "task_commitments", "observed_at", "bound_at",
    "evidence_origin", "origin_authentication", "heldout_independence", "execution_authorized",
    "claim_eligible", "model_calls", "source_calls", "limitations", "binding_sha256",
}
TRANSITION_FIELDS = {
    "spec", "outcome", "plan_sha256", "action_sha256", "from_phase", "to_phase", "before_intent_sha256",
    "after_intent_sha256", "started_at", "finished_at", "elapsed_seconds", "source_endpoints", "simulators",
    "before_capture", "after_capture", "before_state_matches", "after_state_matches", "operations",
    "write_attempts", "write_responses_confirmed", "model_calls", "workspace_imported", "claim_eligible",
    "held_out", "evidence_origin", "origin_authentication", "limitations", "receipt_sha256",
}


def _stamp(value, label):
    _timestamp(value, label)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _capture_interval(capture):
    receipt = capture["receipt"]
    observed = _stamp(receipt["observed_at"], "Source observation time")
    _duration(receipt["source_read_seconds"], "Source read duration")
    return observed - timedelta(seconds=receipt["source_read_seconds"]), observed


def _capture(capture, phase, mapping):
    _check(type(capture) is dict and type(capture.get("receipt")) is dict
           and set(capture["receipt"]) == VALIDATION_FIELDS, "Requires the complete canonical source validation receipt.")
    return _validation(capture, phase["intent_manifest"], mapping)


def _snapshot_receipt(snapshot, receipt, tables):
    _check(type(snapshot) is dict and set(snapshot) == SNAPSHOT_FIELDS | STORED_SNAPSHOT_FIELDS,
           "Requires the complete stored workspace snapshot receipt.")
    _snapshot(snapshot, receipt, tables)
    if "scope" in snapshot:
        _same(snapshot["scope"], "Only the explicitly marked recipe cohort", "Workspace snapshot scope")
        _same(snapshot["consistency"], receipt["consistency"], "Workspace snapshot consistency")
        hashes = snapshot["table_sha256"]
        _check(type(hashes) is dict and set(hashes) == set(tables)
               and all(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()),
               "Workspace snapshot requires complete table hashes.")
        previous = snapshot["previous_snapshot_id"]
        _check(previous is None or type(previous) is str and re.fullmatch(r"rystic-[0-9a-f]{16}", previous),
               "Workspace snapshot previous identity is invalid.")
        _check(type(snapshot["data_changed"]) is bool, "Workspace snapshot requires its change declaration.")


def _same_state(first, second, label):
    """Compare complete observed state, permitting only observation-time changes."""
    _same(first["observed_source"], second["observed_source"], label + " raw projection")
    for key in ("namespace", "simulators", "hash_kind", "data_sha256", "observed_source_sha256",
                "validation_status", "source_rows", "blockers"):
        _same(first["receipt"].get(key), second["receipt"].get(key), label + " " + key)


def _selected(protocol, batch_id, phase_id):
    _check(type(batch_id) is str and type(phase_id) is str and phase_id in PHASE_ORDER,
           "Select a frozen batch and phase.")
    intents = [item for item in protocol["intents"] if item["batch_id"] == batch_id]
    batches = [item for item in protocol["batches"] if item["id"] == batch_id]
    _check(len(intents) == len(batches) == 1, "Batch is not uniquely frozen in the original protocol.")
    intent, batch = intents[0], batches[0]
    phases = {item["id"]: item for item in intent["phase_plan"]["phases"]}
    return intent, batch, phases[phase_id]


def _transition_matches(protocol, intent, phase, previous, transition, capture, bound_at):
    _check(type(transition) is dict and set(transition) == TRANSITION_FIELDS,
           "A complete successful transition receipt is required.")
    _json(transition)
    _same(transition["receipt_sha256"], _hash({k: v for k, v in transition.items() if k != "receipt_sha256"}),
          "Transition receipt hash")
    phase_plan = intent["phase_plan"]
    declared = next(item for item in phase_plan["transitions"]
                    if item["from_phase"] == previous["phase_id"] and item["to_phase"] == phase["id"])
    expected = {"spec": TRANSITION_SPEC, "outcome": "applied", "plan_sha256": phase_plan["plan_sha256"],
                "action_sha256": declared["action_sha256"], "from_phase": previous["phase_id"], "to_phase": phase["id"],
                "before_intent_sha256": declared["before_intent_sha256"],
                "after_intent_sha256": declared["after_intent_sha256"],
                "before_state_matches": True, "after_state_matches": True,
                "write_attempts": len(declared["changes"]), "write_responses_confirmed": len(declared["changes"]),
                "model_calls": 0, "workspace_imported": False, "claim_eligible": False, "held_out": False,
                "evidence_origin": "supplied_pair_observation", "origin_authentication": "not_authenticated"}
    for key, value in expected.items():
        _same(transition[key], value, "Transition " + key)
    operations = _operations(declared["changes"])
    for operation in operations:
        operation["status"] = "response_confirmed"
    _same(transition["operations"], operations, "Exact confirmed transition requests")
    endpoints = transition["source_endpoints"]
    _check(type(endpoints) is dict and set(endpoints) == {"stripe", "linear"}, "Transition source endpoints are required.")
    _check(local_url(endpoints["stripe"]) != local_url(endpoints["linear"]), "Transition sources require separate local endpoints.")
    if previous["transition"] is not None:
        _same(endpoints, previous["transition"]["source_endpoints"], "Batch transition source endpoints")
    before_phase = next(item for item in phase_plan["phases"] if item["id"] == previous["phase_id"])
    for observed, authored in ((transition["before_capture"], before_phase), (transition["after_capture"], phase)):
        _capture(observed, authored, phase_plan["source_issue_account_ids"])
        _same(observed["receipt"]["simulators"], transition["simulators"], "Transition simulator identity")
    _same_state(transition["before_capture"], previous["capture"], "Previous bound source state")
    _same_state(transition["after_capture"], capture, "Current bound source state")
    started = _stamp(transition["started_at"], "Transition start time")
    finished = _stamp(transition["finished_at"], "Transition finish time")
    before_start, before_end = _capture_interval(transition["before_capture"])
    after_start, after_end = _capture_interval(transition["after_capture"])
    current_start, _ = _capture_interval(capture)
    _duration(transition["elapsed_seconds"], "Transition elapsed duration")
    _check(math.isclose((finished - started).total_seconds(), transition["elapsed_seconds"],
                        rel_tol=1e-6, abs_tol=0.001), "Transition elapsed time does not match its timestamps.")
    _check(_stamp(previous["bound_at"], "Previous binding time") <= started <= before_start <= before_end
           <= after_start <= after_end <= finished <= current_start <= bound_at,
           "Phase transition and capture chronology is inconsistent.")


def _build(protocol, batch_id, phase_id, capture, *, snapshot, previous_binding, transition, bound_at, depth=0):
    _check(depth < len(PHASE_ORDER), "Acceptance phase chain is too deep.")
    intent, batch, phase = _selected(protocol, batch_id, phase_id)
    phase_plan = intent["phase_plan"]
    bound = _stamp(bound_at, "Phase binding time")
    frozen = _stamp(protocol["frozen_at"], "Protocol freeze time")
    receipt, tables, _, _ = _capture(capture, phase, phase_plan["source_issue_account_ids"])
    capture_start, observed = _capture_interval(capture)
    _check(frozen <= capture_start <= observed <= bound, "Phase capture must start after protocol freeze and finish before binding.")
    if tables is None:
        _check(snapshot is None, "A guarded phase cannot bind an imported or stale snapshot.")
        tasks = []
    else:
        _snapshot_receipt(snapshot, receipt, tables)
        tasks = build_phase_tasks(phase["intent_manifest"], snapshot, phase_id=phase_id)
    tasks.append(build_guarded_task(phase["intent_manifest"], receipt, phase_id=phase_id))
    _same([item["id"] for item in tasks], phase["task_ids"], "Frozen phase task coverage")
    index = PHASE_ORDER.index(phase_id)
    if index == 0:
        _check(previous_binding is None and transition is None, "Baseline cannot supply a prior binding or transition.")
        _same_state(intent["baseline_capture"], capture, "Provisioned baseline source state")
        _same(snapshot["previous_snapshot_id"], None, "Acceptance baseline requires a fresh workspace")
        _same(snapshot["data_changed"], True, "Acceptance baseline requires its first materialization")
    else:
        _check(type(previous_binding) is dict, "A validated adjacent previous phase binding is required.")
        _same(previous_binding.get("protocol_sha256"), protocol["protocol_sha256"], "Previous original protocol")
        _same(previous_binding.get("batch_id"), batch_id, "Previous fixture batch")
        _same(previous_binding.get("phase_id"), PHASE_ORDER[index - 1], "Previous adjacent phase")
        _validate_stored(protocol, previous_binding, depth + 1)
        _transition_matches(protocol, intent, phase, previous_binding, transition, capture, bound)
        if phase_id == "refreshed":
            _same(snapshot["previous_snapshot_id"], previous_binding["snapshot"]["snapshot_id"],
                  "Refreshed workspace must follow its bound baseline snapshot")
            _same(snapshot["data_changed"], True, "Refreshed workspace must materialize changed source state")
    public, private, artifacts, commitments = [], [], {}, {}
    for task in tasks:
        entry = {key: deepcopy(task[key]) for key in ("question", "answer_schema")}
        commitment = task_commitment(entry, task["expected"], phase_plan["scorer_sha256"])
        _same(commitment, batch["task_commitments"][task["id"]], "Original protocol semantic task commitment")
        _same(commitment, intent["templates"]["task_commitments"][task["id"]], "Original intent semantic task commitment")
        public.append(entry)
        private.append({"id": task["id"], "phase_id": phase_id, "expected": deepcopy(task["expected"]),
                        "expected_sha256": _hash(task["expected"])})
        commitments[task["id"]] = commitment
        artifacts[task["id"]] = {"question_sha256": _hash(task["question"]),
                                  "response_schema_sha256": _hash(task["answer_schema"]),
                                  "scorer_sha256": phase_plan["scorer_sha256"], "expected_sha256": _hash(task["expected"])}
    result = {"spec": SPEC, "protocol_sha256": protocol["protocol_sha256"],
              "allocation_sha256": protocol["allocation_sha256"], "batch_id": batch_id, "phase_id": phase_id,
              "intent_sha256": intent["intent_sha256"], "fixture_sha256": batch["fixture_sha256"],
              "plan_sha256": phase_plan["plan_sha256"], "phase_intent_sha256": phase["intent_sha256"],
              "provisioning_sha256": intent["provisioning_sha256"],
              "previous_binding_sha256": previous_binding["binding_sha256"] if previous_binding is not None else None,
              "transition_sha256": transition["receipt_sha256"] if transition is not None else None,
              "previous_binding": deepcopy(previous_binding), "transition": deepcopy(transition),
              "capture": deepcopy(capture), "snapshot": deepcopy(snapshot), "public_tasks": public,
              "evaluator_tasks": private, "task_artifacts": artifacts, "task_commitments": commitments,
              "observed_at": receipt["observed_at"], "bound_at": bound_at,
              "evidence_origin": protocol["evidence_origin"], "origin_authentication": "operator_assertion_only",
              "heldout_independence": "not_authenticated", "execution_authorized": False, "claim_eligible": False,
              "model_calls": 0, "source_calls": 0, "limitations": deepcopy(LIMITATIONS)}
    result["binding_sha256"] = _hash(result)
    return result


def _validate_stored(protocol, binding, depth=0):
    _check(type(binding) is dict and set(binding) == BINDING_FIELDS, "Requires an original acceptance phase binding.")
    _json(binding)
    _same(binding["spec"], SPEC, "Acceptance binding spec")
    _same(binding["binding_sha256"], _hash({k: v for k, v in binding.items() if k != "binding_sha256"}), "Acceptance binding hash")
    rebuilt = _build(protocol, binding["batch_id"], binding["phase_id"], binding["capture"], snapshot=binding["snapshot"],
                     previous_binding=binding["previous_binding"], transition=binding["transition"],
                     bound_at=binding["bound_at"], depth=depth)
    _same(binding, rebuilt, "Rebuilt original acceptance binding")


def _validate_binding(protocol, binding):
    """Revalidate the original protocol and entire chain at stored binding times."""
    from .acceptance_protocol import validate_protocol
    validate_protocol(protocol)
    try:
        _validate_stored(protocol, binding)
    except (KeyError, TypeError, AttributeError, OverflowError, StopIteration, RecursionError) as exc:
        raise ValueError("Malformed acceptance binding or evidence chain.") from exc


def bind_acceptance_phase(protocol, batch_id, phase_id, capture, output=None, *, snapshot=None,
                          previous_binding=None, transition=None):
    """Bind only late source provenance; preserve every frozen semantic commitment."""
    from .acceptance_protocol import validate_protocol
    protocol = deepcopy(protocol)
    validate_protocol(protocol)
    try:
        result = _build(protocol, batch_id, phase_id, capture, snapshot=snapshot,
                        previous_binding=previous_binding, transition=transition,
                        bound_at=datetime.now(timezone.utc).isoformat())
    except (KeyError, TypeError, AttributeError, OverflowError, StopIteration, RecursionError) as exc:
        raise ValueError("Malformed acceptance phase evidence; no binding was written.") from exc
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    return result
