"""Offline allocation of unseen fixture seeds; not an acceptance manifest.

The allocation commits the cases before any fixture outcomes are inspected.
Provisioned baseline identities, semantic task commitments, reviewed power and
accounting belong to a separately frozen protocol before model execution.
Later observed source states must be bound before their phase's queries;
their unpredictable hashes are not invented in the original protocol.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random

from .workspace import canonical

TASKS = ("qualifying_accounts", "exposure_summary", "payment_controls", "single_account")


def allocate_study(output=None, *, master_seed, development_seeds, batches=30, repeats=3,
                   account_counts=(12, 36, 84), workload="warm_v1"):
    if type(workload) is not str or workload not in {"warm_v1", "recurring_v1"}:
        raise ValueError("workload must be warm_v1 or recurring_v1.")
    if type(master_seed) is not int or not 0 <= master_seed < 2**32:
        raise ValueError("master_seed must be an unsigned 32-bit integer.")
    if (not isinstance(development_seeds, (list, tuple)) or not development_seeds
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in development_seeds)
            or len(development_seeds) != len(set(development_seeds))):
        raise ValueError("Declare unique development seeds before allocating held-out seeds.")
    if type(batches) is not int or not 30 <= batches <= 120:
        raise ValueError("Use 30–120 independent fixture batches; power still needs review.")
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError("Use 1–5 repeats per task and arm.")
    if (not isinstance(account_counts, (list, tuple)) or not account_counts
            or any(type(count) is not int or not 12 <= count <= 100 for count in account_counts)
            or len(account_counts) != len(set(account_counts))):
        raise ValueError("Declare unique fixture sizes from 12 through 100 accounts.")
    rng = random.Random(master_seed)
    used = set(development_seeds)
    fixtures = []
    sizes = [account_counts[i % len(account_counts)] for i in range(batches)]
    rng.shuffle(sizes)
    for index, count in enumerate(sizes):
        fixture_seed = rng.randrange(2**32)
        while fixture_seed in used:
            fixture_seed = rng.randrange(2**32)
        used.add(fixture_seed)
        fixtures.append({"batch_id": f"batch_{index + 1:03d}", "seed": fixture_seed,
            "accounts": count, "variant": "shuffled_cases_v1", "state": "not_materialized",
            "fixture_sha256": None})
    from .study_phases import PHASE_ORDER, PHASE_KINDS, PROFILE_SPEC, recurring_tasks
    task_definitions = recurring_tasks() if workload == "recurring_v1" else [{"id": task, "weight": 1} for task in TASKS]
    phase_order = PHASE_ORDER if workload == "recurring_v1" else (None,)
    schedule = []
    for fixture in fixtures:
        for phase in phase_order:
            pairs = [(task["id"], repeat) for repeat in range(repeats) for task in task_definitions if task.get("phase_id") == phase]
            rng.shuffle(pairs)
            first_arm = rng.randrange(2)
            for index, (task, repeat) in enumerate(pairs):
                arms = ("source", "workspace") if (index + first_arm) % 2 == 0 else ("workspace", "source")
                schedule.extend({"batch_id": fixture["batch_id"], "task_id": task, "repeat": repeat, "arm": arm}
                                for arm in arms)
    allocation = {"spec": "contextbase.study-allocation/v1", "kind": "offline_preparation",
        "created_at": datetime.now(timezone.utc).isoformat(), "master_seed": master_seed,
        "development_seeds": sorted(development_seeds), "account_counts": list(account_counts), "fixture_batches": fixtures,
        "tasks": task_definitions, "repeats": repeats, "workload": workload,
        "schedule": schedule, "planned_trials": len(schedule),
        "model_calls": 0, "model_budget_usd": None, "execution_authorized": False,
        "release_claim_eligible": False,
        "holdout_policy": "Unseen source states within declared public task families. Keep allocated outcomes out of product tuning; if inspected for tuning, retire those batches and allocate fresh ones.",
        "required_before_acceptance": [
            "Complete and publish the development pilot, including source capability limitations and observed budget needs.",
            "Review a power analysis for accuracy/cost/time and freeze the required independent batch count.",
            "Provision isolated native baseline fixtures and retain complete read-back and physical identities; verify allocated seed/size/variant independently.",
            "Freeze the same model, context, runtime, question templates, schemas, semantic gold, scorer and budgets before transitions or model measurements.",
            "Declare and measure cold setup, context review, refresh, model, source/runtime/license costs and retries.",
            ("Execute phases sequentially; bind each observed source state before dependent queries, verify transitions/readiness and reject stale answers after failed refreshes."
             if workload == "recurring_v1" else "Add the predeclared refresh and abstention cases; these four warm-query families are not the complete release workload."),
            "Obtain an explicit bounded execution budget; this allocation does not authorize inference or runtime charges.",
        ]}
    if workload == "recurring_v1":
        allocation["recurring_profile"] = {"spec": PROFILE_SPEC, "phase_order": list(PHASE_ORDER), "phase_kinds": PHASE_KINDS.copy()}
        for fixture in fixtures:
            fixture["phase_snapshots"] = {phase: None for phase in PHASE_ORDER}
            fixture["task_artifacts"] = {task["id"]: None for task in task_definitions}
    allocation["allocation_sha256"] = hashlib.sha256(canonical(allocation).encode()).hexdigest()
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as handle:
            json.dump(allocation, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    return allocation
