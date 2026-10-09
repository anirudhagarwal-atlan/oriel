"""Bounded development runs for one observed recurring-workload phase.

This uses the same agent loop and MCP factory as the warm pilot. It does not
apply source transitions, authorize a full study, or turn per-phase runs into
acceptance evidence. Planning is offline; execution requires explicit consent
and verifies current source state before and after the model trials.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import random
import tempfile

from .context import load_context
from .evaluation_context import context_view
from .refresh_state import refresh_status
from .rystic import SCHEMAS, local_url, snapshot_info
from .rystic_benchmark import ARMS, _budgets, _hash, _runtime, _write, workspace_tables
from .study_binding import bind_phase, _validation

SPEC = "contextbase.phase-pilot-plan/v1"


def _local_state(workspace, validation):
    receipt = validation["receipt"]
    status = refresh_status(workspace)
    latest = status.get("latest_attempt") or {}
    if latest.get("validation") != receipt:
        raise ValueError("The workspace must record this exact validation attempt; retain its sync capture.")
    blocked = receipt.get("validation_status") == "blocked"
    expected = "refused_invalid_source" if blocked else "materialized"
    if latest.get("outcome") != expected or status["snapshot_reads_allowed"] == blocked:
        raise ValueError("Workspace readiness does not match the declared phase.")
    # Even during refusal, reject unrelated data without bypassing the block to
    # expose historical rows through normal analytics.
    with workspace._connect() as connection:
        names = {schema + "." + table for schema, table in connection.execute(
            "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema <> '_contextbase'"
        ).fetchall()}
    if names != set(SCHEMAS):
        raise ValueError("Phase workspace must contain exactly the three Rystic source tables.")
    if not blocked and _hash(workspace_tables(workspace)) != receipt["data_sha256"]:
        raise ValueError("Workspace rows differ from the observed phase.")
    return status, None if blocked else snapshot_info(workspace)


def _binding(workspace, prepared, phase_id, capture, origin):
    status, snapshot = _local_state(workspace, capture)
    with tempfile.TemporaryDirectory(prefix="contextbase-phase-binding-") as directory:
        packet = bind_phase(prepared, phase_id, capture, Path(directory) / "binding.json",
                            snapshot=snapshot, evidence_origin=origin)
    return packet, status


def _tasks(packet):
    return [{"id": private["id"], **deepcopy(public), "expected": deepcopy(private["expected"])}
            for public, private in zip(packet["public_tasks"], packet["evaluator_tasks"])]


def freeze_phase_plan(workspace, prepared, phase_id, capture, output, *, evidence_origin="synthetic_test",
                      max_turns=8, max_calls=80, max_cost_usd=3.0, max_wall_seconds=900,
                      trial_seconds=180, seed=7):
    budgets = _budgets(dict(max_turns=max_turns, max_calls=max_calls, max_cost_usd=max_cost_usd,
                           max_wall_seconds=max_wall_seconds, trial_seconds=trial_seconds))
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("Pilot randomization seed must be an unsigned 32-bit integer.")
    view = context_view(workspace)
    packet, status = _binding(workspace, prepared, phase_id, capture, evidence_origin)
    tasks = _tasks(packet)
    rng = random.Random(seed)
    ordered = [task["id"] for task in tasks]
    rng.shuffle(ordered)
    first = rng.randrange(2)
    queue = [{"task": task, "arm": ARMS[(first + index + position) % 2]}
             for index, task in enumerate(ordered) for position in range(2)]
    plan = {"spec": SPEC, "kind": "development_phase_pilot", "held_out": False,
            "created_at": datetime.now(timezone.utc).isoformat(), "phase_id": phase_id,
            "evidence_origin": evidence_origin, "origin_authentication": "operator_assertion_only",
            "seed": seed, "prepared_plan": deepcopy(prepared), "validation_capture": deepcopy(capture),
            "binding_sha256": packet["binding_sha256"], "task_artifacts": packet["task_artifacts"],
            "tasks": tasks, "queue": queue, "arms": list(ARMS), "budgets": budgets,
            "context_sha256": _hash(load_context(workspace)), "evaluation_context_sha256": _hash(view),
            "refresh_status_sha256": _hash(status), "runtime": _runtime(),
            "model_selection": "Existing Claude CLI configuration; no model override",
            "accounting": {"scope": "One observed development phase; complete recurring accounting absent",
                           "source_setup_usd": None, "workspace_setup_usd": None, "context_review_usd": None,
                           "runtime_and_license_usd": None, "refresh_usd": None, "cold_setup_seconds": None},
            "limitations": ["Development tasks only; no held-out or full recurring-workload claim.",
                            "This run performs no source transitions and cannot substitute for a preregistered study.",
                            "Both arms share the exact policy, recorded refresh evidence and deterministic result calculator.",
                            "Live pre/post observations check quiescent state; no cross-service transaction is claimed.",
                            "Source receipt provenance is attribution; fresh answer evidence still requires trace review.",
                            "Unknown setup, review, source/runtime and refresh costs remain unknown."]}
    plan["plan_sha256"] = _hash(plan)
    _write(output, plan, exclusive=True)
    return plan


def _check_local(workspace, plan):
    if (_hash(load_context(workspace)) != plan["context_sha256"]
            or _hash(context_view(workspace)) != plan["evaluation_context_sha256"]):
        raise ValueError("Context changed after phase planning.")
    packet, status = _binding(workspace, plan["prepared_plan"], plan["phase_id"],
                              plan["validation_capture"], plan["evidence_origin"])
    if (_hash(status) != plan["refresh_status_sha256"] or packet["binding_sha256"] != plan["binding_sha256"]
            or packet["task_artifacts"] != plan["task_artifacts"] or _tasks(packet) != plan["tasks"]):
        raise ValueError("Phase evidence or task artifacts changed after planning.")


def _live_matches(workspace, pair, plan):
    from .rystic_validation import capture_validation
    _check_local(workspace, plan)
    original = plan["validation_capture"]["receipt"]
    observed = capture_validation(pair, original["namespace"])
    prepared = plan["prepared_plan"]
    phase = next(item for item in prepared["phases"] if item["id"] == plan["phase_id"])
    receipt, _, _, _ = _validation(observed, phase["intent_manifest"], prepared["source_issue_account_ids"])
    for key in ("validation_status", "data_sha256", "observed_source_sha256", "hash_kind", "simulators"):
        if receipt[key] != original[key]:
            raise ValueError("Current sources differ from the observed phase.")


def run_phase_pilot(workspace, pair, plan, output, *, approve, allow_model_data=False,
                    client=None, tools_factory=None):
    from .rystic_benchmark import _run_comparison
    if not allow_model_data:
        raise ValueError("Use --allow-model-data to authorize this synthetic source/context-to-model flow.")
    plan = deepcopy(plan)
    digest = plan.pop("plan_sha256", None)
    if (plan.get("spec") != SPEC or plan.get("kind") != "development_phase_pilot"
            or plan.get("held_out") is not False):
        raise ValueError("Unsupported development phase plan.")
    if not isinstance(approve, str) or approve != digest or _hash(plan) != digest:
        raise ValueError("Approve the exact frozen phase plan SHA-256.")
    plan["plan_sha256"] = digest
    _budgets(plan["budgets"])
    if plan.get("runtime") != _runtime():
        raise ValueError("Evaluation code or runtime changed; freeze and review a new plan.")
    if client is None and plan.get("evidence_origin") != "recorded_native":
        raise ValueError("Authored source doubles cannot authorize a configured-model phase run.")
    local_url(pair.stripe.url)
    local_url(pair.linear.url)
    # Rebuild from bound intent before model access, including refused refreshes.
    _check_local(workspace, plan)
    tasks = {task["id"]: task for task in plan["tasks"]}
    if (len(tasks) != len(plan["tasks"]) or plan.get("arms") != list(ARMS)
            or sorted((entry["task"], entry["arm"]) for entry in plan["queue"]) != sorted(
                (task, arm) for task in tasks for arm in ARMS)):
        raise ValueError("Phase pilot requires exactly one trial per task and arm.")
    return _run_comparison(workspace, pair, plan, output,
        verify=lambda: _live_matches(workspace, pair, plan), client=client,
        tools_factory=tools_factory, kind="rystic_development_phase_pilot")
