"""Offline recurring-workload intent; never a native source-state receipt.

All phase mutations are declared for later native integration. This module
makes no API/model calls and emits no observed snapshot hashes. Expected values
come from the independent manifest oracle, not the product SQL.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .rystic_eval import build_tasks, _guarded_state
from .workspace import canonical

PROFILE_SPEC = "contextbase.recurring-workload/v1"
PHASE_ORDER = ("baseline", "refreshed", "guard_a", "guard_b")
PHASE_KINDS = dict(zip(PHASE_ORDER, ("baseline", "refreshed", "guarded", "guarded")))
FAMILIES = ("qualifying_accounts", "exposure_summary", "payment_controls", "single_account")


def recurring_tasks():
    tasks = []
    for phase in PHASE_ORDER:
        families = (*FAMILIES, "guarded_exposure") if phase in ("baseline", "refreshed") else ("guarded_exposure",)
        for family in families:
            tasks.append({"id": phase + "/" + family, "family": family, "phase_id": phase,
                          "control_kind": "single_record" if family == "single_account" else "guarded" if family == "guarded_exposure" else "analytic",
                          "response_kind": "abstain" if PHASE_KINDS[phase] == "guarded" else "answer",
                          "weight": 1})
    return tasks


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def prepare_phases(manifest, output):
    """Freeze four authored source states for a future recurring study.

    A source operator must apply/read back transitions through native APIs,
    compare them with intent, and bind the resulting observed state/validation
    receipts before generating final questions or acceptance evidence. The
    prepared records below are development artifacts, even if their seed was
    originally allocated for a future holdout.
    """
    original = deepcopy(manifest)
    baseline_tasks = {task["id"]: task for task in build_tasks(original, {})}
    # Guard mutations affect Stripe metadata only. Nested manifest ownership
    # must not imply that a Linear issue's description changed with it.
    source_issue_accounts = {issue["issue_id"]: account["account_id"]
                             for account in original["accounts"] for issue in account["issues"]}
    qualifying = baseline_tasks["qualifying_accounts"]["expected"]["accounts"]
    if not qualifying:
        raise ValueError("Phase preparation needs a qualifying account to exercise a changed answer.")
    ordered = sorted(original["accounts"], key=lambda item: item["account_id"])
    if len(ordered) < 2:
        raise ValueError("Phase preparation needs at least two distinct customer mappings.")
    completed_states = {(issue.get("state_id"), issue.get("state_name"))
                        for account in original["accounts"] for issue in account["issues"]
                        if issue["state_type"] == "completed"}
    if (len(completed_states) != 1 or any(not isinstance(value, str) or not value
                                        for pair in completed_states for value in pair)):
        raise ValueError("The seed manifest must identify one unambiguous completed Linear state.")
    completed_id, completed_name = next(iter(completed_states))
    selected = sorted(qualifying, key=lambda item: item["account_id"])[0]
    selected_issue_ids = {issue["issue_id"] for issue in selected["issues"]}
    refreshed = deepcopy(original)
    resolve = []
    for account in refreshed["accounts"]:
        for issue in account["issues"]:
            if issue["issue_id"] not in selected_issue_ids:
                continue
            previous = {key: issue.get(key) for key in ("state_id", "state_name", "state_type")}
            if any(not isinstance(value, str) or not value for value in previous.values()):
                raise ValueError("Selected issues require their observed workflow state identities.")
            after = {"state_id": completed_id, "state_name": completed_name, "state_type": "completed"}
            issue.update(after)
            resolve.append({"source": "linear.issues", "record_id": issue["issue_id"],
                            "operation": "update_workflow_state", "before": previous, "after": after,
                            "native_operation": "issueUpdate(input.stateId)"})
    first, second = ordered[:2]
    ambiguous = deepcopy(refreshed)
    next(account for account in ambiguous["accounts"] if account["customer_id"] == second["customer_id"])["account_id"] = first["account_id"]
    missing = deepcopy(refreshed)
    next(account for account in missing["accounts"] if account["customer_id"] == first["customer_id"])["is_internal"] = None
    duplicate = {"source": "stripe.customers", "record_id": second["customer_id"],
                 "operation": "update_metadata", "before": {"account_id": second["account_id"]},
                 "after": {"account_id": first["account_id"]},
                 "native_operation": "customer metadata update; native read-back required"}
    restore = {**deepcopy(duplicate), "before": deepcopy(duplicate["after"]), "after": deepcopy(duplicate["before"])}
    remove_policy = {"source": "stripe.customers", "record_id": first["customer_id"],
                     "operation": "remove_metadata_key", "before": {"is_internal": first["is_internal"]},
                     "after": {"is_internal": None},
                     "native_operation": "customer metadata key removal; native support/read-back unverified"}
    states = dict(zip(PHASE_ORDER, (original, refreshed, ambiguous, missing)))
    tasks = recurring_tasks()
    phases = []
    for phase in PHASE_ORDER:
        blockers, summary = _guarded_state(states[phase])
        guarded = bool(blockers)
        if guarded != (PHASE_KINDS[phase] == "guarded"):
            raise ValueError("Authored phase does not match its required response kind.")
        phases.append({"id": phase, "kind": PHASE_KINDS[phase], "intent_manifest": states[phase],
                       "intent_sha256": _hash(states[phase]), "observed_source_sha256": None,
                       "native_receipt_sha256": None, "task_ids": [task["id"] for task in tasks if task["phase_id"] == phase],
                       "expected_ingestion": "refuse_invalid_source" if guarded else "materialize_full_cohort",
                       "expected_guard": {"status": "abstained" if guarded else "answered",
                                          "result": summary, "blockers": blockers}})
    if phases[0]["expected_guard"]["result"] == phases[1]["expected_guard"]["result"]:
        raise ValueError("Refresh must change the independent answer.")
    transitions = []
    for before, after, changes in (("baseline", "refreshed", resolve),
                                   ("refreshed", "guard_a", [duplicate]),
                                   ("guard_a", "guard_b", [restore, remove_policy])):
        transitions.append({"from_phase": before, "to_phase": after,
                            "before_intent_sha256": _hash(states[before]), "after_intent_sha256": _hash(states[after]),
                            "changes": changes, "action_sha256": _hash(changes),
                            "native_execution": "unverified"})
    plan = {"spec": "contextbase.phase-preparation/v1", "kind": "offline_preparation", "evidence_origin": "authored_intent",
            "created_at": datetime.now(timezone.utc).isoformat(), "seed_manifest_sha256": _hash(original),
            "recurring_profile": {"spec": PROFILE_SPEC, "phase_order": list(PHASE_ORDER), "phase_kinds": PHASE_KINDS.copy()},
            "tasks": tasks, "phases": phases, "transitions": transitions,
            "source_issue_account_ids": source_issue_accounts,
            "issue_mapping_policy": "These explicit Linear description mappings stay unchanged in every phase. Do not infer them from the parent account's possibly mutated Stripe metadata; guarded validation must read raw source records.",
            "model_calls": 0, "source_calls": 0, "execution_authorized": False, "claim_eligible": False,
            "held_out": False,
            "scorer_sha256": hashlib.sha256(Path(__file__).with_name("rystic_eval.py").read_bytes()).hexdigest(),
            "selection_policy": "Resolve all qualifying issues of the lexically first qualifying account; use the first two account mappings for guard controls.",
            "remaining_before_execution": [
                "Verify ordinary native API support and read-back for each transition on a development fixture.",
                "Bind actual source-state and validation receipts; authored intent hashes are not observed snapshot hashes.",
                "Expose failed-refresh evidence to both agents and reject stale workspace answers during invalid-source phases.",
                "Freeze final task questions, schemas, gold hashes, runtime/model/context and a reviewed execution budget.",
                "Use fresh isolated held-out fixtures after development; this prepared fixture is development evidence only.",
            ]}
    plan["plan_sha256"] = _hash(plan)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(plan, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return plan
