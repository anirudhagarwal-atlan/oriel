"""Fail-closed statistics for a future paired recurring-workload study.

This module makes no model calls. It cannot authenticate supplied run receipts,
the held-out declaration, or an independently reviewed power analysis. Its
``statistical_acceptance`` is conditional on those declarations; public claims
always require a separate evidence review. Synthetic test inputs never qualify.

Input schema (all fields below are required unless explicitly optional):

manifest = {
  spec: "contextbase.paired-study/v1", kind: "acceptance", held_out: true,
  evidence_origin: "observed", frozen_at: <timezone-aware ISO timestamp>,
  model_id: <nonempty str>, runtime_sha256: <hex>, context_sha256: <hex>,
  tasks: [{id: <str>, weight: <positive finite number>, phase_id: <optional str>}],
  batches: [{id: <str>, fixture_sha256: <hex>,
             phase_snapshots: <optional {phase_id: source-state SHA-256}>}],
  repeats: <positive int>,
  planned: [{batch_id, task_id, repeat: <zero-based int>, arm: "source"|"workspace"}],
  caps: {run_seconds: <positive>, total_cost_usd: <positive>},
  bootstrap: {seed: <uint32>, replicates: <int >= 2000>, confidence: 0.95},
  power: {minimum_independent_batches: <int >= 30>, target_power: <0.8..1>,
          analysis_sha256: <hex>, reviewed: true},
  manifest_sha256: <SHA-256 of canonical JSON without this field>
}

observations = {
  runs: [{batch_id, task_id, repeat, arm, correct: <bool>,
          started_at, finished_at: <timezone-aware ISO timestamps after frozen_at>,
          overhead_seconds: <nonnegative finite>, scored_evidence_sha256: <hex>,
          provenance: {manifest_sha256, model_id, runtime_sha256,
                       context_sha256, fixture_sha256},
          attempts: [{correct: <bool>, started_at, finished_at,
                      elapsed_seconds: <nonnegative finite>,
                      model_cost_usd: <nonnegative finite>,
                      tool_cost_usd: <nonnegative finite>, evidence_sha256: <hex>}]}],
  batches: [{batch_id, arm, started_at, finished_at, evidence_sha256: <hex>,
             costs: {setup, refresh, common_review, runtime},
             seconds: {setup, refresh, common_review, runtime}}]
}

Every cost/second component is explicit and nonnegative. Null or missing is
unknown, never zero. Batch overhead is counted once, separately from attempts;
``runtime`` seconds mean additional overhead, not duplicated query duration.
Common review receives equal allocation in the two arms. Scored run receipts
must be distinct, and both arms must execute the complete predeclared Cartesian
schedule in its locked start order. All failed attempts cost money/time;
attempts stop at first success.

Attempt records use exactly the declared fields. Each attempt digest identifies
a complete invocation receipt (identity, timings and spend), not merely its
response body; each accounting digest identifies one complete batch/arm
allocation receipt. Both kinds are unique across observations. Accounting phase
seconds represent additional, non-overlapping wall time, so their sum must fit
inside the declared accounting period. Shared evidence may inform multiple
allocations, but each allocation needs its own distinct receipt.

Task phases default to ``baseline``. If any task declares ``phase_id``, every
batch must declare ``phase_snapshots`` with exactly the full set of task phase
IDs. Each run's provenance.fixture_sha256 then identifies its task's declared
source state, including refreshed states. The batch fixture hash continues to
identify the independent fixture, not necessarily any particular source state.
Without explicit task phases, absent phase_snapshots retains the original
batch fixture hash binding; an optional baseline mapping is still checked.
Refresh costs alone do not establish that an answer used refreshed data.

The optional recurring_profile object has spec
``contextbase.recurring-workload/v1``, a phase_order list, and phase_kinds mapping.
It requires one baseline, then one refreshed phase, then one or more guarded
phases. Tasks declare response_kind (answer or abstain). Each batch.task_artifacts
maps every task ID to question_sha256, response_schema_sha256, scorer_sha256 and
expected_sha256; profiled run provenance includes the same four hashes.
Profiled tasks also declare control_kind: analytic, single_record, or guarded
(a positive answer control) for valid phases, guarded for abstention phases.
At least one single_record control and one positive guarded answer control are
required. A descriptive per-task scorecard publishes both arms, including losses;
it makes no per-task statistical significance claim.

Profile observations additionally require:
* transitions: one {batch_id, from_phase_id, to_phase_id, before_sha256,
  after_sha256, started_at, finished_at, evidence_sha256} per consecutive phase.
* phases: one {batch_id, arm, phase_id, source_sha256, started_at, ready_at,
  finished_at, evidence_sha256, receipt_kind, readiness_mode, costs, seconds}
  per batch/arm/phase. Costs/seconds use COMPONENTS and reconcile to batch totals.
  Source and guarded receipts are source_state_validation; valid workspace
  receipts are materialized_snapshot. Guarded workspace readiness is
  refused_invalid_source, which attests rejection, not materialization.
* first_use: one {batch_id, arm, task_id, repeat, cold_started_at, finished_at,
  evidence_sha256} naming that arm's first planned baseline run.
Transitions follow completion of both prior-phase arms; preparation follows the
transition and readiness precedes dependent runs. Source hashes attest the
declared source state, including invalid states, never a stale workspace state.
Phase overhead must include its complete observed preparation span ready-start.
First-use timing must stay inside its batch accounting window. Phase, transition
and first-use record fields are exact; undocumented cost fields are rejected.
Complete measurement receipts are unique across all roles in profiled studies;
task artifacts and source/context identity hashes may legitimately be shared.

Generic reports remain conditional statistical reports, with charter_ready=False
and missing recurring-coverage details. charter_ready additionally requires a
valid recurring contract, explicit first-use receipts, eligible observed inputs,
and passing statistical gates. It still does not authenticate external evidence
or authorize a public claim. First-use is unknown without explicit receipts.
Query-latency quantiles are unweighted empirical distributions, separately scoped
from the weighted primary mean and without allocated batch overhead.

Accuracy uses declared task weights. Cost uses actual unweighted workload spend
and raw correct-answer counts. Time is weighted capped task latency plus equal
per-run allocation of batch overhead. All repeated tasks, setup and refresh in
one independent batch are resampled together with their paired comparison arm.
Intervals are marginal 95% percentile intervals, not simultaneous coverage.

Canonical JSON is UTF-8, sorted keys, compact separators, ensure_ascii=False,
allow_nan=False. Only hashes bind records here; hashes do not authenticate them.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import random
import re


SPEC = "contextbase.paired-study/v1"
RECURRING_SPEC = "contextbase.recurring-workload/v1"
ARMS = ("source", "workspace")
COMPONENTS = ("setup", "refresh", "common_review", "runtime")
ARTIFACT_FIELDS = {"question_sha256", "response_schema_sha256", "scorer_sha256", "expected_sha256"}
ATTEMPT_FIELDS = {"correct", "started_at", "finished_at", "elapsed_seconds",
                  "model_cost_usd", "tool_cost_usd", "evidence_sha256"}
PHASE_FIELDS = {"batch_id", "arm", "phase_id", "source_sha256", "started_at", "ready_at",
                "finished_at", "evidence_sha256", "receipt_kind", "readiness_mode", "costs", "seconds"}
TRANSITION_FIELDS = {"batch_id", "from_phase_id", "to_phase_id", "before_sha256", "after_sha256",
                     "started_at", "finished_at", "evidence_sha256"}
FIRST_USE_FIELDS = {"batch_id", "arm", "task_id", "repeat", "cold_started_at", "finished_at", "evidence_sha256"}
TARGETS = {"workspace_accuracy_minimum": 0.90, "accuracy_lift_minimum": 0.10,
           "cost_per_correct_ratio_maximum": 0.70, "speedup_minimum": 2.0}


class InvalidStudy(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _check(condition, message):
    if not condition:
        raise InvalidStudy(message)


def _number(value, field, positive=False):
    _check(type(value) in (int, float), f"{field}: missing, unknown, or nonnumeric")
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    _check(valid, f"{field}: requires a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def _text(value, field):
    _check(type(value) is str and bool(value.strip()), f"{field}: requires a nonempty string")
    return value


def _sha(value, field):
    _check(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, f"{field}: requires a SHA-256 digest")
    return value


def _timestamp(value, field):
    _text(value, field)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidStudy(f"{field}: requires an ISO timestamp") from None
    _check(stamp.tzinfo is not None and stamp.utcoffset() is not None, f"{field}: timezone is required")
    return stamp


def _object(value, field):
    _check(type(value) is dict, f"{field}: requires an object")
    return value


def _record(value, fields, name):
    _object(value, name)
    _check(set(value) == fields, f"{name}: supply exactly the declared fields; unknown fields may hide unaccounted spend")
    return value


def _list(value, field, nonempty=True):
    _check(type(value) is list and (bool(value) or not nonempty), f"{field}: requires a {'nonempty ' if nonempty else ''}list")
    return value


def _key(row):
    _object(row, "run/schedule row")
    _text(row.get("batch_id"), "batch_id")
    _text(row.get("task_id"), "task_id")
    _check(type(row.get("repeat")) is int and row["repeat"] >= 0, "repeat: requires a zero-based integer")
    _check(row.get("arm") in ARMS, "arm: requires source or workspace")
    return row["batch_id"], row["task_id"], row["repeat"], row["arm"]


def _profile(manifest, task_phases):
    profile = manifest.get("recurring_profile")
    if profile is None:
        return None
    _object(profile, "recurring_profile")
    _check(profile.get("spec") == RECURRING_SPEC, "Unsupported recurring profile")
    order = [_text(item, "recurring phase ID") for item in _list(profile.get("phase_order"), "phase_order")]
    kinds = _object(profile.get("phase_kinds"), "phase_kinds")
    _check(len(order) == len(set(order)) and set(order) == set(kinds) == set(task_phases.values()),
           "Recurring phases must cover every task phase exactly once")
    _check(len(order) >= 3 and [kinds[phase] for phase in order[:2]] == ["baseline", "refreshed"]
           and all(kinds[phase] == "guarded" for phase in order[2:]),
           "Recurring coverage requires baseline, refreshed, then one or more guarded phases")
    for task in manifest["tasks"]:
        expected = "abstain" if kinds[task_phases[task["id"]]] == "guarded" else "answer"
        _check(task.get("response_kind") == expected, "Task response_kind differs from its recurring phase kind")
        allowed = {"guarded"} if expected == "abstain" else {"analytic", "single_record", "guarded"}
        _check(task.get("control_kind") in allowed, "Task control_kind differs from its recurring phase kind")
    _check(any(task.get("control_kind") == "single_record" for task in manifest["tasks"]),
           "Recurring charter requires a declared single_record control task")
    _check(any(task.get("control_kind") == "guarded" and task.get("response_kind") == "answer" for task in manifest["tasks"]),
           "Recurring charter requires a positive guarded answer control task")
    return profile


def _validate(manifest, observations):
    _object(manifest, "manifest")
    _object(observations, "observations")
    _check(manifest.get("spec") == SPEC, "Unsupported study specification")
    locked = _sha(manifest.get("manifest_sha256"), "manifest_sha256")
    _check(_digest({key: value for key, value in manifest.items() if key != "manifest_sha256"}) == locked,
           "Manifest changed after locking")
    frozen = _timestamp(manifest.get("frozen_at"), "frozen_at")
    for key in ("runtime_sha256", "context_sha256"):
        _sha(manifest.get(key), key)
    _text(manifest.get("model_id"), "model_id")
    task_weights, task_phases, batches, fixture_ids = {}, {}, {}, set()
    explicit_phases = False
    for task in _list(manifest.get("tasks"), "tasks"):
        _object(task, "task")
        identity = _text(task.get("id"), "task.id")
        _check(identity not in task_weights, "Duplicate declared task")
        task_weights[identity] = _number(task.get("weight"), "task.weight", positive=True)
        task_phases[identity] = _text(task.get("phase_id", "baseline"), "task.phase_id")
        explicit_phases = explicit_phases or "phase_id" in task
    phase_ids = set(task_phases.values())
    profile = _profile(manifest, task_phases)
    phase_snapshots = {}
    task_artifacts = {}
    for batch in _list(manifest.get("batches"), "batches"):
        _object(batch, "batch")
        identity = _text(batch.get("id"), "batch.id")
        fixture = _sha(batch.get("fixture_sha256"), "batch.fixture_sha256")
        _check(identity not in batches, "Duplicate declared batch")
        _check(fixture not in fixture_ids, "Independent batches cannot reuse the same fixture hash")
        batches[identity] = fixture
        fixture_ids.add(fixture)
        if explicit_phases or "phase_snapshots" in batch:
            snapshots = _object(batch.get("phase_snapshots"), "batch.phase_snapshots")
            _check(set(snapshots) == phase_ids,
                   "Batch phase_snapshots must include exactly all declared task phases")
            phase_snapshots[identity] = {phase: _sha(snapshots[phase], "batch.phase_snapshots value")
                                         for phase in phase_ids}
        else:
            phase_snapshots[identity] = {"baseline": fixture}
        if profile:
            artifacts = _object(batch.get("task_artifacts"), "batch.task_artifacts")
            _check(set(artifacts) == set(task_weights), "Task artifact bindings must cover every batch/task")
            task_artifacts[identity] = {}
            for task, artifact in artifacts.items():
                _object(artifact, "task artifact")
                _check(set(artifact) == ARTIFACT_FIELDS, "Supply every immutable task artifact hash exactly once")
                task_artifacts[identity][task] = {key: _sha(artifact[key], "task artifact." + key) for key in ARTIFACT_FIELDS}
    repeats = manifest.get("repeats")
    _check(type(repeats) is int and 1 <= repeats <= 100, "repeats must be an integer from 1 to 100")
    _check(len(batches) * len(task_weights) * repeats * 2 <= 100000, "Study exceeds the supported schedule size")
    expected_keys = {(batch, task, repeat, arm) for batch in batches for task in task_weights
                     for repeat in range(repeats) for arm in ARMS}
    planned = [_key(row) for row in _list(manifest.get("planned"), "planned")]
    _check(len(planned) == len(set(planned)), "Duplicate planned run")
    _check(set(planned) == expected_keys, "Plan must include the complete batch/task/repeat/arm Cartesian schedule")
    if profile:
        positions = {phase: index for index, phase in enumerate(profile["phase_order"])}
        for batch in batches:
            ordered = [positions[task_phases[key[1]]] for key in planned if key[0] == batch]
            _check(ordered == sorted(ordered), "Recurring schedule must preserve declared phase order")
    caps = _object(manifest.get("caps"), "caps")
    run_cap = _number(caps.get("run_seconds"), "caps.run_seconds", positive=True)
    cost_cap = _number(caps.get("total_cost_usd"), "caps.total_cost_usd", positive=True)
    bootstrap = _object(manifest.get("bootstrap"), "bootstrap")
    replicates = bootstrap.get("replicates")
    _check(type(replicates) is int and 2000 <= replicates <= 100000, "At least 2000 and at most 100000 bootstrap replicates are required")
    _check(replicates * len(batches) <= 5000000, "Bootstrap exceeds the supported five million batch draws")
    seed = bootstrap.get("seed")
    _check(type(seed) is int and 0 <= seed < 2**32, "Bootstrap seed must be a uint32")
    _check(type(bootstrap.get("confidence")) in (int, float) and bootstrap["confidence"] == 0.95,
           "Predeclare 0.95 bootstrap confidence")
    power = _object(manifest.get("power"), "power")
    minimum = power.get("minimum_independent_batches")
    _check(type(minimum) is int and minimum >= 30, "Power plan requires at least 30 independent batches")
    target_power = _number(power.get("target_power"), "power.target_power", positive=True)
    _check(0.8 <= target_power < 1, "Predeclared target power must be at least 0.8 and below 1")
    _sha(power.get("analysis_sha256"), "power.analysis_sha256")

    rows = _list(observations.get("runs"), "observations.runs")
    observed_keys = [_key(row) for row in rows]
    _check(len(observed_keys) == len(set(observed_keys)), "Duplicate observed run; no best-of selection is allowed")
    _check(set(observed_keys) == expected_keys, "Observed runs are incomplete or include unplanned runs")
    grouped = {identity: {arm: {"cost": 0.0, "weighted_correct": 0.0, "weight": 0.0,
                "correct": 0, "runs": 0, "weighted_seconds": 0.0, "overhead_seconds": 0.0,
                "attempts": 0, "late_answers": 0} for arm in ARMS} for identity in batches}
    seen_receipts, seen_attempt_receipts, run_starts, details = set(), set(), {}, {}
    for row, key in zip(rows, observed_keys):
        batch, task, _, arm = key
        _check(type(row.get("correct")) is bool, "Run correctness must be an exact boolean")
        run_started = _timestamp(row.get("started_at"), "run.started_at")
        run_finished = _timestamp(row.get("finished_at"), "run.finished_at")
        _check(run_started >= frozen and run_finished >= run_started, "A run predates the frozen plan or has reversed timestamps")
        run_starts[key] = run_started
        receipt = _sha(row.get("scored_evidence_sha256"), "run.scored_evidence_sha256")
        _check(receipt not in seen_receipts, "A scored run receipt was reused across observations")
        seen_receipts.add(receipt)
        provenance = _object(row.get("provenance"), "run.provenance")
        expected_provenance = {"manifest_sha256": locked, "model_id": manifest["model_id"],
            "runtime_sha256": manifest["runtime_sha256"], "context_sha256": manifest["context_sha256"],
            "fixture_sha256": phase_snapshots[batch][task_phases[task]]}
        if profile:
            expected_provenance.update(task_artifacts[batch][task])
        _check(provenance == expected_provenance,
               "Run model, context, runtime, phase source state or frozen-plan identity differs")
        elapsed = _number(row.get("overhead_seconds"), "run.overhead_seconds")
        cost = 0.0
        attempts = _list(row.get("attempts"), "run.attempts")
        last_finished = run_started
        for index, attempt in enumerate(attempts):
            _object(attempt, "attempt")
            _check(set(attempt) == ATTEMPT_FIELDS,
                   "Supply every attempt field exactly once; unknown fields may hide unaccounted spend")
            _check(type(attempt.get("correct")) is bool, "Attempt correctness must be an exact boolean")
            _check(not attempt["correct"] or index == len(attempts) - 1, "Attempts continued after a correct answer")
            attempt_receipt = _sha(attempt.get("evidence_sha256"), "attempt.evidence_sha256")
            _check(attempt_receipt not in seen_attempt_receipts, "An attempt receipt was reused across observations")
            seen_attempt_receipts.add(attempt_receipt)
            attempt_elapsed = _number(attempt.get("elapsed_seconds"), "attempt.elapsed_seconds")
            attempt_started = _timestamp(attempt.get("started_at"), "attempt.started_at")
            attempt_finished = _timestamp(attempt.get("finished_at"), "attempt.finished_at")
            _check(last_finished <= attempt_started <= attempt_finished <= run_finished,
                   "Attempt timestamps overlap, precede the plan, or fall outside their run")
            _check(math.isclose((attempt_finished - attempt_started).total_seconds(), attempt_elapsed,
                                rel_tol=1e-6, abs_tol=0.001), "Attempt elapsed time does not match its timestamps")
            last_finished = attempt_finished
            elapsed += attempt_elapsed
            cost += _number(attempt.get("model_cost_usd"), "attempt.model_cost_usd")
            cost += _number(attempt.get("tool_cost_usd"), "attempt.tool_cost_usd")
        _check(attempts[-1]["correct"] is row["correct"], "Final attempt and scored run correctness disagree")
        _check(math.isclose((run_finished - run_started).total_seconds(), elapsed,
                            rel_tol=1e-6, abs_tol=0.001), "Run time is not completely accounted for by attempts and overhead")
        correct = row["correct"] and elapsed <= run_cap
        effective_seconds = elapsed if correct else run_cap
        details[key] = {"started": run_started, "finished": run_finished, "elapsed": elapsed,
                        "effective_seconds": effective_seconds, "correct": correct, "phase_id": task_phases[task],
                        "query_cost": cost, "attempts": len(attempts), "late_answer": row["correct"] and not correct}
        result = grouped[batch][arm]
        result["cost"] += cost
        result["weighted_correct"] += task_weights[task] * int(correct)
        result["weight"] += task_weights[task]
        result["correct"] += int(correct)
        result["runs"] += 1
        result["weighted_seconds"] += task_weights[task] * effective_seconds
        result["attempts"] += len(attempts)
        result["late_answers"] += int(row["correct"] and not correct)

    _check(all(run_starts[earlier] <= run_starts[later] for earlier, later in zip(planned, planned[1:])),
           "Observed start order differs from the frozen execution schedule")

    accounting = _list(observations.get("batches"), "observations.batches")
    account_keys, accounting_receipts, review = set(), set(), {}
    for row in accounting:
        _object(row, "batch accounting")
        key = (row.get("batch_id"), row.get("arm"))
        _check(key[0] in batches and key[1] in ARMS, "Unplanned batch accounting row")
        _check(key not in account_keys, "Duplicate batch accounting row")
        account_keys.add(key)
        accounting_receipt = _sha(row.get("evidence_sha256"), "batch accounting evidence_sha256")
        _check(accounting_receipt not in accounting_receipts, "A batch accounting receipt was reused across allocations")
        accounting_receipts.add(accounting_receipt)
        account_started = _timestamp(row.get("started_at"), "batch accounting.started_at")
        account_finished = _timestamp(row.get("finished_at"), "batch accounting.finished_at")
        _check(account_started >= frozen and account_finished >= account_started,
               "Batch accounting includes phases before the frozen plan or reversed timestamps")
        costs, seconds = _object(row.get("costs"), "batch costs"), _object(row.get("seconds"), "batch seconds")
        _check(set(costs) == set(COMPONENTS) and set(seconds) == set(COMPONENTS), "Supply every batch cost/time component exactly once")
        allocated_seconds = 0.0
        for component in COMPONENTS:
            grouped[key[0]][key[1]]["cost"] += _number(costs[component], f"batch costs.{component}")
            allocated_seconds += _number(seconds[component], f"batch seconds.{component}")
        _check(allocated_seconds <= (account_finished - account_started).total_seconds() + 0.001,
               "Allocated phase times exceed the declared accounting period")
        grouped[key[0]][key[1]]["overhead_seconds"] += allocated_seconds
        review[key] = (costs["common_review"], seconds["common_review"])
    _check(account_keys == {(batch, arm) for batch in batches for arm in ARMS}, "Missing batch setup/refresh/review/runtime accounting")
    for batch in batches:
        _check(review[(batch, "source")] == review[(batch, "workspace")], "Common review must receive equal allocations in both arms")
    for batch in grouped.values():
        for arm in ARMS:
            values = batch[arm]
            _check(all(math.isfinite(value) for value in values.values()), "Accounting totals overflowed")
    eligibility = []
    if manifest.get("kind") != "acceptance":
        eligibility.append("Pilot/development studies cannot satisfy release acceptance")
    if manifest.get("held_out") is not True:
        eligibility.append("Held-out evaluation is required")
    if manifest.get("evidence_origin") != "observed":
        eligibility.append("Synthetic test arrays are not observed benchmark evidence")
    if power.get("reviewed") is not True:
        eligibility.append("An independently reviewed predeclared power analysis is required")
    if len(batches) < minimum:
        eligibility.append("Independent batch count is below the predeclared power requirement")
    return grouped, eligibility, cost_cap, replicates, seed, task_phases, details, profile


def _metrics(grouped, sample):
    result = {}
    for arm in ARMS:
        totals = {key: sum(grouped[batch][arm][key] for batch in sample)
                  for key in ("cost", "weighted_correct", "weight", "correct", "runs", "attempts", "late_answers")}
        # Each batch has the same declared tasks/repeats. Its overhead is shared
        # equally over actual runs; weights cannot make setup or failures vanish.
        total_time = sum(grouped[batch][arm]["weighted_seconds"] / grouped[batch][arm]["weight"]
                         + grouped[batch][arm]["overhead_seconds"] / grouped[batch][arm]["runs"]
                         for batch in sample)
        _check(all(math.isfinite(value) for value in totals.values()) and math.isfinite(total_time),
               "Study accounting totals overflowed")
        result[arm] = {"accuracy": totals["weighted_correct"] / totals["weight"],
                       "correct_answers": totals["correct"], "planned_runs": totals["runs"],
                       "attempts": totals["attempts"], "late_answers": totals["late_answers"],
                       "total_cost_usd": totals["cost"],
                       "cost_per_correct_usd": totals["cost"] / totals["correct"] if totals["correct"] else None,
                       "mean_capped_seconds": total_time / len(sample)}
    return result


def _differences(metrics):
    source, workspace = metrics["source"], metrics["workspace"]
    return {"accuracy_lift": workspace["accuracy"] - source["accuracy"],
            "total_cost_difference_usd": workspace["total_cost_usd"] - source["total_cost_usd"],
            "cost_per_correct_difference_usd": None if None in (workspace["cost_per_correct_usd"], source["cost_per_correct_usd"])
                else workspace["cost_per_correct_usd"] - source["cost_per_correct_usd"],
            "mean_capped_seconds_difference": workspace["mean_capped_seconds"] - source["mean_capped_seconds"]}


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _distribution(values):
    ordered = sorted(values)
    return {"count": len(ordered), "p50": _percentile(ordered, 0.5) if ordered else None,
            "p95": _percentile(ordered, 0.95) if ordered else None}


def _query_latency(details):
    def summaries(rows):
        return {"observed_seconds": _distribution([row["elapsed"] for row in rows]),
                "capped_seconds": _distribution([row["effective_seconds"] for row in rows]),
                "correct_within_cap_seconds": _distribution([row["elapsed"] for row in rows if row["correct"]]),
                "failed_or_late_count": sum(not row["correct"] for row in rows)}
    phases = sorted({row["phase_id"] for row in details.values()})
    return {"scope": "Unweighted planned-query distributions; observed includes every attempt and run overhead, capped penalizes wrong/late answers. Batch setup/review/refresh/runtime allocations are excluded here and included in the primary mean.",
            "quantile_method": "Linear interpolation at (n-1)*p over sorted observations",
            "by_arm": {arm: summaries([row for key, row in details.items() if key[3] == arm]) for arm in ARMS},
            "by_phase": {phase: {arm: summaries([row for key, row in details.items()
                                                  if key[3] == arm and row["phase_id"] == phase]) for arm in ARMS}
                         for phase in phases}}


def _task_scorecard(manifest, observations, details, grouped):
    accounting = {(row["batch_id"], row["arm"]): row for row in observations["batches"]}
    selected_by_task = {task["id"]: {arm: [] for arm in ARMS} for task in manifest["tasks"]}
    for key, row in details.items():
        selected_by_task[key[1]][key[3]].append((key, row))
    tasks = {}
    for task in manifest["tasks"]:
        arms = {}
        for arm in ARMS:
            selected = selected_by_task[task["id"]][arm]
            count = len(selected)
            correct = sum(row["correct"] for _, row in selected)
            query_cost = sum(row["query_cost"] for _, row in selected)
            allocated_cost = sum(sum(accounting[(key[0], arm)]["costs"].values()) / grouped[key[0]][arm]["runs"]
                                 for key, _ in selected)
            allocated_seconds = sum(grouped[key[0]][arm]["overhead_seconds"] / grouped[key[0]][arm]["runs"]
                                    for key, _ in selected)
            total_cost = query_cost + allocated_cost
            arms[arm] = {"planned_runs": count, "correct_answers": correct, "accuracy": correct / count,
                         "attempts": sum(row["attempts"] for _, row in selected),
                         "late_answers": sum(row["late_answer"] for _, row in selected),
                         "query_cost_usd": query_cost, "allocated_batch_cost_usd": allocated_cost,
                         "total_cost_usd": total_cost, "cost_per_correct_usd": total_cost / correct if correct else None,
                         "mean_observed_query_seconds": sum(row["elapsed"] for _, row in selected) / count,
                         "mean_capped_query_seconds": sum(row["effective_seconds"] for _, row in selected) / count,
                         "mean_capped_seconds": (sum(row["effective_seconds"] for _, row in selected) + allocated_seconds) / count}
            _check(all(value is None or math.isfinite(value) for value in arms[arm].values()),
                   "Per-task accounting totals overflowed")
        tasks[task["id"]] = {"phase_id": task.get("phase_id", "baseline"),
                              "control_kind": task.get("control_kind", "undeclared"),
                              "weight": task["weight"], "by_arm": arms,
                              "workspace_minus_source": _differences(arms)}
    return {"scope": "Descriptive per-task counts and point estimates for all planned runs; no per-task significance claim. Batch costs/time are allocated equally over actual scheduled runs, including failures. Primary aggregate gates retain declared task weights.",
            "by_task": tasks}


def _profile_receipt_uniqueness(observations):
    receipts = set()

    def add(value):
        receipt = _sha(value, "complete measurement receipt")
        _check(receipt not in receipts, "A complete measurement receipt was reused across evidence roles or observations")
        receipts.add(receipt)

    for run in observations["runs"]:
        add(run["scored_evidence_sha256"])
        for attempt in run["attempts"]:
            add(attempt["evidence_sha256"])
    for field in ("batches", "phases", "transitions", "first_use"):
        if observations.get(field) is None:
            continue
        for row in _list(observations[field], "observations." + field):
            _object(row, field + " receipt")
            add(row.get("evidence_sha256"))


def _recurring_evidence(manifest, observations, details, profile):
    if not profile:
        return None
    order, kinds = profile["phase_order"], profile["phase_kinds"]
    batches = {row["id"]: row for row in manifest["batches"]}
    frozen = _timestamp(manifest["frozen_at"], "frozen_at")
    required = {(batch, arm, phase) for batch in batches for arm in ARMS for phase in order}
    phases, receipts = {}, set()
    for row in _list(observations.get("phases"), "observations.phases"):
        _record(row, PHASE_FIELDS, "phase evidence")
        key = row.get("batch_id"), row.get("arm"), row.get("phase_id")
        _check(key in required and key not in phases, "Duplicate or unplanned recurring phase evidence")
        batch, arm, phase = key
        receipt = _sha(row.get("evidence_sha256"), "phase.evidence_sha256")
        _check(receipt not in receipts, "A recurring phase receipt was reused")
        receipts.add(receipt)
        _check(_sha(row.get("source_sha256"), "phase.source_sha256") == batches[batch]["phase_snapshots"][phase],
               "Recurring phase source state differs from its frozen snapshot hash")
        guarded = kinds[phase] == "guarded"
        expected_receipt = "source_state_validation" if arm == "source" or guarded else "materialized_snapshot"
        expected_mode = "refused_invalid_source" if arm == "workspace" and guarded else "ready"
        _check(row.get("receipt_kind") == expected_receipt and row.get("readiness_mode") == expected_mode,
               "Recurring readiness must distinguish materialization from refusing an invalid source")
        started = _timestamp(row.get("started_at"), "phase.started_at")
        ready = _timestamp(row.get("ready_at"), "phase.ready_at")
        finished = _timestamp(row.get("finished_at"), "phase.finished_at")
        _check(frozen <= started <= ready <= finished, "Invalid recurring preparation/readiness timestamps")
        runs = sorted((value for run_key, value in details.items()
                       if run_key[0] == batch and run_key[3] == arm and value["phase_id"] == phase),
                      key=lambda value: value["started"])
        _check(runs and ready <= runs[0]["started"] and runs[-1]["finished"] <= finished,
               "A recurring task ran before phase readiness or outside its phase evidence window")
        _check(all(left["finished"] <= right["started"] for left, right in zip(runs, runs[1:])),
               "Recurring tasks overlap within one arm and phase")
        allocations = {}
        for field in ("costs", "seconds"):
            values = _object(row.get(field), "phase." + field)
            _check(set(values) == set(COMPONENTS), "Supply every phase cost/time allocation exactly once")
            allocations[field] = {component: _number(values[component], "phase." + field + "." + component)
                                  for component in COMPONENTS}
        # Allocated overhead is additional to query durations, not a second
        # charge for the same time inside a broad phase window.
        available = (finished - started).total_seconds() - sum(value["elapsed"] for value in runs)
        overhead = sum(allocations["seconds"].values())
        _check(overhead + 0.001 >= (ready - started).total_seconds(),
               "Recurring phase accounting omits observed preparation time")
        _check(overhead <= available + 0.001,
               "Recurring phase overhead overlaps accounted query time or exceeds its window")
        phases[key] = {"started": started, "ready": ready, "finished": finished, **allocations}
    _check(set(phases) == required, "Missing recurring readiness/accounting evidence")
    expected_transitions = {(batch, before, after) for batch in batches for before, after in zip(order, order[1:])}
    transitions, transition_receipts = set(), set()
    for row in _list(observations.get("transitions"), "observations.transitions"):
        _record(row, TRANSITION_FIELDS, "transition evidence")
        key = row.get("batch_id"), row.get("from_phase_id"), row.get("to_phase_id")
        _check(key in expected_transitions and key not in transitions, "Duplicate or unplanned recurring transition")
        transitions.add(key)
        batch, before, after = key
        receipt = _sha(row.get("evidence_sha256"), "transition.evidence_sha256")
        _check(receipt not in transition_receipts, "A recurring transition receipt was reused")
        transition_receipts.add(receipt)
        before_hash = _sha(row.get("before_sha256"), "transition.before_sha256")
        after_hash = _sha(row.get("after_sha256"), "transition.after_sha256")
        _check(before_hash == batches[batch]["phase_snapshots"][before]
               and after_hash == batches[batch]["phase_snapshots"][after] and before_hash != after_hash,
               "Recurring transition must bind the distinct frozen before/after source states")
        started = _timestamp(row.get("started_at"), "transition.started_at")
        finished = _timestamp(row.get("finished_at"), "transition.finished_at")
        _check(started <= finished and all(phases[(batch, arm, before)]["finished"] <= started
                                          and finished <= phases[(batch, arm, after)]["started"] for arm in ARMS),
               "Recurring transition must follow both previous arms and precede next-phase preparation")
    _check(transitions == expected_transitions, "Missing recurring source transition evidence")
    for accounting in observations["batches"]:
        batch, arm = accounting["batch_id"], accounting["arm"]
        start = _timestamp(accounting["started_at"], "batch.started_at")
        finish = _timestamp(accounting["finished_at"], "batch.finished_at")
        _check(start <= phases[(batch, arm, order[0])]["started"]
               and phases[(batch, arm, order[-1])]["finished"] <= finish,
               "Recurring phase evidence falls outside its batch accounting window")
        for field in ("costs", "seconds"):
            for component in COMPONENTS:
                allocated = sum(phases[(batch, arm, phase)][field][component] for phase in order)
                _check(math.isclose(allocated, accounting[field][component], rel_tol=1e-9,
                                    abs_tol=0.001 if field == "seconds" else 1e-9),
                       "Recurring phase allocations do not reconcile to authoritative batch totals")
    return phases


def _first_use(manifest, observations, details, phases, profile):
    rows = observations.get("first_use")
    if rows is None:
        return {"status": "unknown", "reason": "Explicit cold-start/first-run receipts were not supplied.", "by_arm": None}
    required = {(batch["id"], arm) for batch in manifest["batches"] for arm in ARMS}
    first_keys = {}
    for row in manifest["planned"]:
        key = _key(row)
        first_keys.setdefault((key[0], key[3]), key)
    recorded, receipts = {}, set()
    accounting_windows = {(row["batch_id"], row["arm"]):
                          (_timestamp(row["started_at"], "batch.started_at"), _timestamp(row["finished_at"], "batch.finished_at"))
                          for row in observations["batches"]}
    frozen = _timestamp(manifest["frozen_at"], "frozen_at")
    for row in _list(rows, "observations.first_use"):
        if profile:
            _record(row, FIRST_USE_FIELDS, "first-use evidence")
        key = _key(row)
        group = key[0], key[3]
        _check(group in required and group not in recorded and key == first_keys[group],
               "First-use receipt must identify exactly the first planned run for each batch/arm")
        receipt = _sha(row.get("evidence_sha256"), "first_use.evidence_sha256")
        _check(receipt not in receipts, "A cold-start/first-run receipt was reused")
        receipts.add(receipt)
        cold = _timestamp(row.get("cold_started_at"), "first_use.cold_started_at")
        finished = _timestamp(row.get("finished_at"), "first_use.finished_at")
        run = details[key]
        _check(frozen <= cold <= run["started"] and finished == run["finished"],
               "First-use cold start or finish disagrees with its recorded first run")
        _check(accounting_windows[group][0] <= cold and finished <= accounting_windows[group][1],
               "First-use cold span falls outside its batch accounting window")
        if profile:
            baseline = profile["phase_order"][0]
            _check(run["phase_id"] == baseline and cold <= phases[(key[0], key[3], baseline)]["started"],
                   "First-use timing must include baseline phase preparation")
        overhead = (run["started"] - cold).total_seconds()
        recorded[group] = {"observed": (finished - cold).total_seconds(),
                           "capped": overhead + run["effective_seconds"], "correct": run["correct"]}
    _check(set(recorded) == required, "Missing explicit first-use receipts for planned batch/arm pairs")
    return {"status": "recorded", "scope": "One predeclared first query per batch/arm, including the explicit cold-start span. Capped time replaces failed/late query duration with the query cap and retains cold overhead; no batch-average overhead is added again.",
            "quantile_method": "Linear interpolation at (n-1)*p over sorted observations",
            "by_arm": {arm: {"observed_seconds": _distribution([row["observed"] for key, row in recorded.items() if key[1] == arm]),
                             "capped_seconds": _distribution([row["capped"] for key, row in recorded.items() if key[1] == arm]),
                             "correct_first_answers": sum(row["correct"] for key, row in recorded.items() if key[1] == arm)}
                       for arm in ARMS}}


def evaluate_study(manifest, observations) -> dict:
    """Return conditional statistical acceptance or a fail-closed explanation.

    Incomplete/unknown/inconsistent records yield ``inconclusive``. Completely
    observed, eligible studies that miss any superiority gate yield ``failed``.
    The report never by itself authorizes a public superiority claim.
    """
    report = {"spec": "contextbase.paired-study-report/v1", "statistical_acceptance": "inconclusive",
              "claim_eligible": False, "charter_ready": False,
              "charter_ready_scope": "Conditional recurring-benchmark evidence contract and statistical gates only; not authenticated provenance, Registry integration, user adoption or public-claim approval.",
              "charter_missing": ["Recurring baseline/refreshed/guarded workload evidence has not been validated."],
              "targets": dict(TARGETS), "reasons": [],
              "metrics": None, "paired_intervals": None, "gates": None,
              "phase_ids": None, "task_phases": None,
              "query_latency": None, "task_scorecard": None, "first_use": {"status": "unknown", "by_arm": None},
              "limitations": [
                  "This report checks supplied records; hashes do not authenticate original model/provider receipts.",
                  "Held-out independence and the reviewed power analysis require separate evidence review.",
                  "95% percentile intervals are marginal; no simultaneous-coverage claim is made.",
                  "A public superiority claim additionally requires verified provenance and protocol review."]}
    try:
        # Serialization also rejects NaN/infinity and unsupported Python objects.
        _canonical(manifest)
        report["observations_sha256"] = _digest(observations)
        grouped, reasons, cost_cap, replicates, seed, task_phases, details, profile = _validate(manifest, observations)
        report["manifest_sha256"] = manifest["manifest_sha256"]
        report["independent_batches"] = len(grouped)
        report["phase_ids"] = sorted(set(task_phases.values()))
        report["task_phases"] = task_phases
        phase_evidence = _recurring_evidence(manifest, observations, details, profile)
        report["first_use"] = _first_use(manifest, observations, details, phase_evidence, profile)
        if profile:
            _profile_receipt_uniqueness(observations)
        report["query_latency"] = _query_latency(details)
        report["task_scorecard"] = _task_scorecard(manifest, observations, details, grouped)
        report["charter_missing"] = [] if profile else ["Missing recurring profile and baseline/refreshed/guarded coverage, immutable task artifacts, transitions and phase accounting."]
        if report["first_use"]["status"] != "recorded":
            report["charter_missing"].append("Explicit cold-start/first-run receipts are missing.")
            if profile:
                reasons.append("Recurring charter requires explicit first-use receipts")
        sample = sorted(grouped)
        metrics = _metrics(grouped, sample)
        report["metrics"] = metrics
        source, workspace = metrics["source"], metrics["workspace"]
        if source["correct_answers"] == 0 or workspace["correct_answers"] == 0:
            reasons.append("Zero correct answers makes cost-per-correct comparison undefined")
        if source["total_cost_usd"] <= 0 or source["mean_capped_seconds"] <= 0:
            reasons.append("Zero baseline cost or time makes relative improvement undefined")
        if sum(metrics[arm]["total_cost_usd"] for arm in ARMS) > cost_cap:
            reasons.append("Observed all-in spend exceeded the predeclared study cap")
        report["reasons"] = reasons
        if reasons:
            report["charter_missing"].extend(reasons)
            return report
        samples = {name: [] for name in _differences(metrics)}
        rng = random.Random(seed)
        for _ in range(replicates):
            drawn = [sample[rng.randrange(len(sample))] for _ in sample]
            for name, value in _differences(_metrics(grouped, drawn)).items():
                if value is None:
                    reason = "A paired bootstrap draw has zero correct answers; undefined draws cannot be discarded"
                    report["reasons"].append(reason)
                    report["charter_missing"].append(reason)
                    return report
                samples[name].append(value)
        intervals = {}
        for name, values in samples.items():
            ordered = sorted(values)
            intervals[name] = {"lower": _percentile(ordered, 0.025), "upper": _percentile(ordered, 0.975)}
        report["paired_intervals"] = intervals
        report["resampling"] = {"unit": "whole paired independent batch", "replicates": replicates,
                                 "seed": seed, "confidence": 0.95, "method": "percentile"}
        report["gates"] = {
            "workspace_accuracy": workspace["accuracy"] + 1e-12 >= TARGETS["workspace_accuracy_minimum"],
            "accuracy_lift": workspace["accuracy"] - source["accuracy"] + 1e-12 >= TARGETS["accuracy_lift_minimum"],
            "accuracy_lift_supported": intervals["accuracy_lift"]["lower"] > 0,
            "cost_per_correct": (workspace["cost_per_correct_usd"] <= source["cost_per_correct_usd"] * TARGETS["cost_per_correct_ratio_maximum"]
                or math.isclose(workspace["cost_per_correct_usd"], source["cost_per_correct_usd"] * TARGETS["cost_per_correct_ratio_maximum"], rel_tol=1e-12, abs_tol=0)),
            "lower_total_cost": workspace["total_cost_usd"] < source["total_cost_usd"],
            "cost_per_correct_supported": intervals["cost_per_correct_difference_usd"]["upper"] < 0,
            "total_cost_supported": intervals["total_cost_difference_usd"]["upper"] < 0,
            "speedup": (workspace["mean_capped_seconds"] <= source["mean_capped_seconds"] / TARGETS["speedup_minimum"]
                or math.isclose(workspace["mean_capped_seconds"], source["mean_capped_seconds"] / TARGETS["speedup_minimum"], rel_tol=1e-12, abs_tol=0)),
            "time_supported": intervals["mean_capped_seconds_difference"]["upper"] < 0,
        }
        report["statistical_acceptance"] = "passed" if all(report["gates"].values()) else "failed"
        report["reasons"] = [f"Gate not met: {key}" for key, passed in report["gates"].items() if not passed]
        report["charter_missing"].extend(report["reasons"])
        report["charter_ready"] = bool(profile) and not report["charter_missing"]
        return report
    except (InvalidStudy, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        report["reasons"] = [str(exc) if isinstance(exc, InvalidStudy) else "Malformed or non-finite study input"]
        report["charter_missing"].extend(report["reasons"])
        return report
