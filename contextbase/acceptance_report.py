"""Collect frozen acceptance execution receipts without changing their clocks.

This module is entirely offline. It consumes the common agent engine's original
receipts, recomputes answers, validates the complete sequential schedule and
adds reviewed accounting events. It cannot authenticate provider receipts or
authorize execution. A synthetic collection is never acceptance evidence.
"""
from __future__ import annotations

from copy import deepcopy
import math
import random

from . import study_report as stats
from .acceptance_binding import _validate_stored, _capture_interval
from .acceptance_protocol import validate_protocol
from .model import reported_cost
from .rystic_benchmark import parse_structured_action, _hash as engine_hash
from .rystic_eval import score_answer
from .study_binding import _hash, _same

SPEC = "contextbase.acceptance-collection/v1"
PLAN_SPEC = "contextbase.acceptance-execution-plan/v1"
BILLING_RECONCILIATION_REASON = "Full billing reconciliation is required before charter readiness"
ENGINE_ARMS = {"source": "rystic_source", "workspace": "sql_context"}
EXECUTION_FIELDS = {"kind", "plan_sha256", "started_at", "finished_at", "plan", "trials", "invocations",
    "known_cost_usd", "cost_complete", "transport", "source_stability", "stop_reason", "reported_model_ids",
    "model_identity_consistent", "model_backend", "expected_model_id", "accounting", "limitations",
    "preflight_seconds", "preflight_started_at", "preflight_finished_at", "postflight_seconds",
    "postflight_started_at", "postflight_finished_at", "elapsed_seconds", "execution_complete",
    "answered_trials", "summary", "release_gates"}
TRIAL_FIELDS = {"batch_id", "task", "repeat", "arm", "status", "correct", "answer", "known_cost_usd",
    "cost_complete", "inferences", "trace", "elapsed_seconds", "capped_seconds", "queue_index",
    "queue_entry", "trial_id", "started_at", "finished_at", "tool_invocations", "score", "messages", "error_type"}
INFERENCE_FIELDS = {"elapsed_seconds", "cost_usd", "cost_known", "spend_unknown", "usage", "model_usage",
    "duration_ms", "duration_api_ms", "num_turns", "subtype", "is_error", "exit_code", "error_type", "parse_error",
    "response", "inference_index", "started_at", "finished_at", "measured_elapsed_seconds"}
TOOL_FIELDS = {"tool_index", "inference_index", "tool", "arguments_sha256", "started_at", "finished_at",
               "elapsed_seconds", "status", "error_type"}


def _known_fields(value, fields, label):
    stats._check(type(value) is dict and set(value) <= fields,
                 label + " contains unknown fields that could hide unaccounted work/spend")


def _interval(row, label, elapsed_key="elapsed_seconds"):
    start = stats._timestamp(row.get("started_at"), label + " start")
    end = stats._timestamp(row.get("finished_at"), label + " finish")
    elapsed = stats._number(row.get(elapsed_key), label + " elapsed")
    stats._check(end >= start and math.isclose((end - start).total_seconds(), elapsed,
                 rel_tol=1e-6, abs_tol=0.001), label + " duration differs from actual timestamps")
    return start, end, elapsed


def _plan(protocol, binding, limits):
    caps = protocol["budgets"]
    maximum = {"max_turns": caps["max_model_turns_per_run"], "max_calls": caps["max_model_calls"],
               "max_cost_usd": caps["comparison_usd"], "max_wall_seconds": caps["study_seconds"],
               "trial_seconds": caps["run_seconds"]}
    limits = deepcopy(maximum if limits is None else limits)
    stats._record(limits, set(maximum), "phase execution limits")
    for key, value in limits.items():
        stats._check(0 < stats._number(value, key) <= maximum[key], "Phase limit exceeds original protocol")
    stats._check(type(limits["max_turns"]) is int and type(limits["max_calls"]) is int,
                 "Turn and call limits must be integers")
    _same(limits["trial_seconds"], maximum["trial_seconds"], "Original task latency cap")
    tasks = [{"id": private["id"], **deepcopy(public), "expected": deepcopy(private["expected"])}
             for public, private in zip(binding["public_tasks"], binding["evaluator_tasks"])]
    ids = {task["id"] for task in tasks}
    queue = [{"batch_id": row["batch_id"], "task": row["task_id"], "repeat": row["repeat"],
              "arm": ENGINE_ARMS[row["arm"]]}
             for row in protocol["planned"] if row["batch_id"] == binding["batch_id"] and row["task_id"] in ids]
    value = {"spec": PLAN_SPEC, "protocol_sha256": protocol["protocol_sha256"],
             "binding_sha256": binding["binding_sha256"], "batch_id": binding["batch_id"],
             "phase_id": binding["phase_id"], "context_sha256": protocol["context_sha256"],
             "runtime_sha256": protocol["runtime_sha256"], "expected_model_id": protocol["model_id"],
             "tasks": tasks, "queue": queue, "budgets": limits, "execution_authorized": False,
             "accounting": {"scope": "Full acceptance event ledger required separately"},
             "limitations": ["This offline plan does not authorize source writes, model calls or spending.",
                             "Per-phase limits do not replace a controller enforcing the full-study budget."]}
    value["plan_sha256"] = _hash(value)
    return value


def prepare_execution_plan(protocol, binding, *, limits=None):
    """Prepare exact engine input offline; only explicit lower limits may vary."""
    validate_protocol(protocol)
    _validate_stored(protocol, binding)
    return _plan(protocol, binding, limits)


def store_collection(store, protocol, collection):
    """Store each immutable receipt once; replace recursive binding copies with refs."""
    stats._record(collection, {"spec", "protocol_sha256", "phases", "events", "first_use"}, "collection")
    _same(collection["protocol_sha256"], protocol["protocol_sha256"], "Collection protocol")

    def binding_ref(binding, depth=0):
        stats._check(depth < 4, "Binding chain exceeds four phases")
        compact = deepcopy(binding)
        prior = compact.pop("previous_binding")
        return store.put({"spec": "contextbase.stored-binding/v1", "binding": compact,
                          "previous_binding_ref": binding_ref(prior, depth + 1) if prior is not None else None})

    return {"spec": "contextbase.acceptance-collection-index/v1", "protocol_ref": store.put(protocol),
            "collection_spec": collection["spec"], "protocol_sha256": collection["protocol_sha256"],
            "phases": [{"binding_ref": binding_ref(item["binding"]), "execution_ref": store.put(item["execution"])}
                       for item in collection["phases"]],
            "events_ref": store.put(collection["events"]), "first_use_ref": store.put(collection["first_use"])}


def load_collection(store, index):
    """Resolve a local digest-only index, with bounded phases and binding depth."""
    stats._record(index, {"spec", "protocol_ref", "collection_spec", "protocol_sha256", "phases", "events_ref", "first_use_ref"}, "collection index")
    _same(index["spec"], "contextbase.acceptance-collection-index/v1", "Collection index spec")
    stats._check(type(index["phases"]) is list and len(index["phases"]) <= 480, "Invalid indexed phase count")
    cache, read_bytes = {}, 0

    def read(digest):
        nonlocal read_bytes
        if digest not in cache:
            value = store.get(digest)
            read_bytes += len(stats._canonical(value).encode())
            stats._check(read_bytes <= 128 * 1024 * 1024, "Collection exceeds the 128 MiB distinct-artifact read budget")
            cache[digest] = value
        return cache[digest]

    def binding(digest, depth=0):
        stats._check(depth < 4, "Binding reference chain exceeds four phases")
        wrapper = read(digest)
        stats._record(wrapper, {"spec", "binding", "previous_binding_ref"}, "stored binding")
        _same(wrapper["spec"], "contextbase.stored-binding/v1", "Stored binding spec")
        value = deepcopy(wrapper["binding"])
        stats._check("previous_binding" not in value, "Stored binding includes duplicate history")
        value["previous_binding"] = binding(wrapper["previous_binding_ref"], depth + 1) if wrapper["previous_binding_ref"] is not None else None
        return value

    phases = []
    for entry in index["phases"]:
        stats._record(entry, {"binding_ref", "execution_ref"}, "phase references")
        phases.append({"binding": binding(entry["binding_ref"]), "execution": read(entry["execution_ref"])})
    protocol = read(index["protocol_ref"])
    _same(index["protocol_sha256"], protocol["protocol_sha256"], "Indexed original protocol")
    return protocol, {"spec": index["collection_spec"], "protocol_sha256": index["protocol_sha256"],
                      "phases": phases, "events": read(index["events_ref"]), "first_use": read(index["first_use_ref"])}


def _phase(protocol, packet, reasons):
    stats._record(packet, {"binding", "execution"}, "phase collection")
    binding, execution = packet["binding"], packet["execution"]
    _known_fields(execution, EXECUTION_FIELDS, "Engine execution")
    _validate_stored(protocol, binding)
    plan = _plan(protocol, binding, execution["plan"]["budgets"])
    _same(execution["plan"], plan, "Original acceptance execution plan")
    _same(execution["plan_sha256"], plan["plan_sha256"], "Execution plan hash")
    _same(execution["kind"], "rystic_acceptance_phase", "Acceptance engine receipt kind")
    _same(execution["expected_model_id"], protocol["model_id"], "Frozen model enforcement")
    started, finished, phase_elapsed = _interval(execution, "phase execution")
    if phase_elapsed > plan["budgets"]["max_wall_seconds"]:
        reasons.append("A phase exceeded its elapsed deadline")
    stats._check(stats._timestamp(binding["bound_at"], "binding time") <= started,
                 "Binding must precede both agents' phase execution")
    if execution.get("source_stability") != "pre_and_post_match":
        reasons.append("Source/context verification did not pass before and after every phase")
    if execution.get("model_backend") != "configured_claude_cli":
        reasons.append("Injected model responses are test evidence only")
    if execution.get("execution_complete") is not True or execution.get("stop_reason") is not None:
        reasons.append("A phase stopped or retained unexecuted trials; partial execution is not acceptance")
    trials = stats._list(execution.get("trials"), "phase trials")
    stats._check(len(trials) == len(plan["queue"]), "Every planned trial must be retained, including skipped trials")
    tasks = {task["id"]: task for task in plan["tasks"]}
    details, calls, spend, identities = [], 0, 0.0, set()
    last = started
    for index, (entry, trial) in enumerate(zip(plan["queue"], trials)):
        _known_fields(trial, TRIAL_FIELDS, "Trial")
        _same(trial["queue_index"], index, "Trial index")
        _same(trial["queue_entry"], entry, "Trial queue entry")
        for key, value in entry.items():
            _same(trial[key], value, "Trial " + key)
        identity = _hash({"plan_sha256": plan["plan_sha256"], "queue_index": index, "queue_entry": entry})
        _same(trial["trial_id"], identity, "Stable trial identity")
        arm = next(key for key, value in ENGINE_ARMS.items() if value == entry["arm"])
        key = (entry["batch_id"], entry["task"], entry["repeat"], arm)
        inferences = stats._list(trial["inferences"], "inferences", nonempty=False)
        stats._check(len(inferences) <= plan["budgets"]["max_turns"], "Per-run model-turn cap exceeded")
        calls += len(inferences)
        if trial["started_at"] is None:
            stats._check(trial["finished_at"] is None and not inferences and not trial["tool_invocations"]
                         and trial["status"].startswith("skipped_") and trial["answer"] is None
                         and trial["known_cost_usd"] == 0 and trial["elapsed_seconds"] == 0,
                         "Skipped trial contains hidden execution")
            reasons.append("The full frozen schedule was not executed")
            continue
        run_start, run_finish, elapsed = _interval(trial, "trial")
        stats._check(last <= run_start <= run_finish <= finished, "Trials overlap or depart from frozen order")
        last = run_finish
        model_cost, last_inference, spans, last_answer = 0.0, run_start, [], None
        requested_calls = []
        unknown = False
        for number, inference in enumerate(inferences):
            _known_fields(inference, INFERENCE_FIELDS, "Inference")
            _same(inference["inference_index"], number, "Inference index")
            begin, end, duration = _interval(inference, "inference", "measured_elapsed_seconds")
            stats._check(last_inference <= begin <= end <= run_finish, "Inference outside its trial or out of order")
            last_inference = end
            spans.append((begin, end))
            price = reported_cost(inference)
            if price is None or inference.get("spend_unknown") or inference.get("cost_known") is False:
                unknown = True
                reasons.append("A provider invocation has unknown spend")
            if price is not None:
                model_cost += price
            usage = inference.get("model_usage")
            if not isinstance(usage, dict) or set(usage) != {protocol["model_id"]}:
                reasons.append("Invocation model identity is missing or differs from the frozen model")
            else:
                identities.update(usage)
            try:
                action = parse_structured_action(inference.get("response"))
            except (ValueError, TypeError, RecursionError):
                action = {}
            if "answer" in action and not inference.get("is_error"):
                stats._check(number == len(inferences) - 1, "Model execution continued after a final answer")
                last_answer = action["answer"]
            if not inference.get("is_error"):
                requested_calls.extend((number, item["tool"], engine_hash(item["arguments"]))
                                       for item in action.get("tools", []))
        observed_calls = [(call["inference_index"], call["tool"], call["arguments_sha256"])
                          for call in trial["tool_invocations"]]
        stats._check(observed_calls == requested_calls[:len(observed_calls)], "Tool invocation order/arguments differ from model requests")
        if trial["status"] == "answered":
            stats._check(observed_calls == requested_calls, "Answered trial omits requested tool execution")
        for number, call in enumerate(trial["tool_invocations"]):
            _known_fields(call, TOOL_FIELDS, "Tool invocation")
            _same(call["tool_index"], number, "Tool invocation index")
            begin, end, _ = _interval(call, "tool invocation")
            ref = call["inference_index"]
            stats._check(type(ref) is int and 0 <= ref < len(inferences), "Tool lacks its model request")
            request = parse_structured_action(inferences[ref].get("response"))
            stats._check(call["tool"] in [item["tool"] for item in request.get("tools", [])],
                         "Tool invocation differs from model request")
            stats._check(spans[ref][1] <= begin <= end <= run_finish, "Tool timing outside its trial/request")
            if ref + 1 < len(inferences):
                stats._check(end <= stats._timestamp(inferences[ref + 1]["started_at"], "next inference"),
                             "Tool invocation must finish before the next model turn")
            stats._check(call["status"] in {"returned", "raised"}, "Tool invocation is unfinished")
            spans.append((begin, end))
        ordered = sorted(spans)
        stats._check(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), "Model/tool spans overlap")
        terminal = trial["status"]
        supported = {"answered", "turn_limit", "trial_deadline", "context_limit", "tool_or_transport_error",
                     "inference_error", "invalid_model_response", "invocation_budget", "cost_budget",
                     "wall_clock_budget", "inference_cost_unknown", "reported_cost_exceeded_budget",
                     "model_identity_unavailable", "model_identity_mismatch", "model_identity_changed", "global_budget_refused"}
        stats._check(terminal in supported, "Unsupported trial terminal status")
        final_inference = inferences[-1] if inferences else {}
        if terminal == "trial_deadline":
            stats._check(elapsed >= plan["budgets"]["trial_seconds"], "Claimed deadline failure preceded its actual deadline")
        if terminal == "turn_limit":
            stats._check(len(inferences) == plan["budgets"]["max_turns"] and last_answer is None,
                         "Claimed turn limit differs from retained invocations")
        if terminal == "inference_error":
            stats._check(final_inference.get("is_error") is True, "Claimed provider error lacks an error receipt")
        if terminal == "invalid_model_response":
            invalid = False
            try:
                parse_structured_action(final_inference.get("response"))
            except (ValueError, TypeError, RecursionError):
                invalid = True
            stats._check(invalid, "Claimed invalid response parses successfully")
        if terminal == "tool_or_transport_error":
            stats._text(trial.get("error_type"), "Recorded transport exception type")
        if terminal == "context_limit":
            reasons.append("Context-limit failure requires independent retained prompt-size evidence")
        if (last_answer is not None and not unknown and not final_inference.get("is_error")
                and set(final_inference.get("model_usage", {})) == {protocol["model_id"]}
                and elapsed <= plan["budgets"]["trial_seconds"]
                and phase_elapsed <= plan["budgets"]["max_wall_seconds"]
                and execution.get("stop_reason") is None):
            stats._check(terminal == "answered", "An in-budget final answer was downgraded to a failure")
        _same(trial["answer"], last_answer if trial["status"] == "answered" else trial["answer"], "Final paid response answer")
        correct = (trial["status"] == "answered" and last_answer is not None
                   and score_answer(last_answer, tasks[entry["task"]]["expected"])["correct"]
                   and elapsed <= protocol["budgets"]["run_seconds"]
                   and execution.get("source_stability") == "pre_and_post_match")
        _same(trial["correct"], correct, "Independently rescored correctness")
        stats._check(math.isclose(trial["known_cost_usd"], model_cost, abs_tol=1e-12), "Trial model costs do not reconcile")
        _same(trial["cost_complete"], not unknown, "Trial known cost declaration")
        spend += model_cost
        details.append((key, {"trial_id": identity, "started": run_start, "finished": run_finish,
            "elapsed": elapsed, "effective_seconds": elapsed if correct else protocol["budgets"]["run_seconds"],
            "correct": correct, "phase_id": binding["phase_id"], "query_cost": model_cost,
            "attempts": 1, "model_calls": len(inferences), "late_answer": elapsed > protocol["budgets"]["run_seconds"]}))
    _same(execution["invocations"], calls, "Retained model invocation count")
    stats._check(math.isclose(execution["known_cost_usd"], spend, abs_tol=1e-12), "Phase model spend does not reconcile")
    stats._check(calls <= plan["budgets"]["max_calls"], "Phase model call cap exceeded")
    if spend > plan["budgets"]["max_cost_usd"]:
        reasons.append("A phase exceeded its model spend cap")
    pre_start, pre_finish, _ = _interval(verification_evidence(execution, "preflight"), "preflight")
    if execution.get("source_stability") == "pre_and_post_match":
        stats._check(execution["preflight_seconds"] > 0 and execution.get("postflight_seconds", 0) > 0,
                     "Successful source verification requires measured positive work")
    stats._check(started <= pre_start <= pre_finish <= finished, "Preflight lies outside phase execution")
    if details:
        stats._check(pre_finish <= details[0][1]["started"], "Preflight must finish before either agent queries")
    if execution.get("postflight_started_at") is not None:
        post_start, post_finish, _ = _interval(verification_evidence(execution, "postflight"), "postflight")
        stats._check(pre_finish <= post_start <= post_finish <= finished, "Postflight lies outside phase execution")
        if details:
            stats._check(details[-1][1]["finished"] <= post_start, "Postflight must follow both agents' final query")
    else:
        stats._check(execution.get("source_stability") != "pre_and_post_match", "Successful source verification is missing postflight evidence")
    return binding, execution, details, calls


def _statistics(grouped, bootstrap):
    sample = sorted(grouped)
    metrics = stats._metrics(grouped, sample)
    source, workspace = metrics["source"], metrics["workspace"]
    stats._check(source["correct_answers"] and workspace["correct_answers"], "Zero correct answers makes relative costs undefined")
    stats._check(source["total_cost_usd"] > 0 and source["mean_capped_seconds"] > 0, "Zero baseline cost/time makes improvement undefined")
    draws = {key: [] for key in stats._differences(metrics)}
    rng = random.Random(bootstrap["seed"])
    for _ in range(bootstrap["replicates"]):
        selected = [sample[rng.randrange(len(sample))] for _ in sample]
        for key, value in stats._differences(stats._metrics(grouped, selected)).items():
            stats._check(value is not None, "A paired bootstrap draw has zero correct answers; do not discard undefined draws")
            draws[key].append(value)
    intervals = {key: {"lower": stats._percentile(sorted(values), .025), "upper": stats._percentile(sorted(values), .975)}
                 for key, values in draws.items()}
    targets = stats.TARGETS
    gates = {
        "workspace_accuracy": workspace["accuracy"] + 1e-12 >= targets["workspace_accuracy_minimum"],
        "accuracy_lift": workspace["accuracy"] - source["accuracy"] + 1e-12 >= targets["accuracy_lift_minimum"],
        "accuracy_lift_supported": intervals["accuracy_lift"]["lower"] > 0,
        "cost_per_correct": workspace["cost_per_correct_usd"] <= source["cost_per_correct_usd"] * targets["cost_per_correct_ratio_maximum"] + 1e-12,
        "lower_total_cost": workspace["total_cost_usd"] < source["total_cost_usd"],
        "cost_per_correct_supported": intervals["cost_per_correct_difference_usd"]["upper"] < 0,
        "total_cost_supported": intervals["total_cost_difference_usd"]["upper"] < 0,
        "speedup": workspace["mean_capped_seconds"] <= source["mean_capped_seconds"] / targets["speedup_minimum"] + 1e-12,
        "time_supported": intervals["mean_capped_seconds_difference"]["upper"] < 0}
    return metrics, intervals, gates


def require_billing_reconciliation(report):
    """Keep scoped statistics separate from unverified all-in cost readiness.

    The current evidence contract has no authentic whole-job reconciliation
    input or verifier. Apply this after statistical calculation, including on
    stored replay; a declared native origin or matching hash cannot lift it.
    """
    report["charter_ready"] = False
    report["full_billing_reconciliation_verified"] = False
    report["reasons"] = list(dict.fromkeys([*report["reasons"], BILLING_RECONCILIATION_REASON]))


def build_report(protocol, collection):
    """Validate supplied execution/accounting; never contact a source or model."""
    result = {"spec": "contextbase.acceptance-report/v1", "statistical_acceptance": "inconclusive",
              "claim_eligible": False, "charter_ready": False, "reasons": [], "metrics": None,
              "paired_intervals": None, "gates": None, "accounting": None, "first_use": None,
              "query_latency": None, "task_scorecard": None,
              "limitations": ["Supplied hashes and review declarations do not authenticate native/provider evidence or held-out independence.",
                              "A public claim additionally requires independent provenance and source-baseline review.",
                              "Marginal 95% paired batch intervals do not imply simultaneous confidence coverage.",
                              "This collector is offline; a controller enforcing global budgets during live execution remains separate."]}
    reasons = result["reasons"]
    try:
        validate_protocol(protocol)
        stats._record(collection, {"spec", "protocol_sha256", "phases", "events", "first_use"}, "acceptance collection")
        _same(collection["spec"], SPEC, "Acceptance collection specification")
        _same(collection["protocol_sha256"], protocol["protocol_sha256"], "Original acceptance protocol")
        result["protocol_sha256"] = protocol["protocol_sha256"]
        result["collection_sha256"] = _hash(collection)
        from .acceptance_accounting import summarize_events
        accounting = summarize_events(protocol, collection["events"], validate_protocol=False)
        result["accounting"] = accounting
        reasons.extend(accounting["unknown_reasons"])
        if protocol["evidence_origin"] != "recorded_native":
            reasons.append("Synthetic source fixtures cannot support observed acceptance results")
        history = protocol["historical_context_events"]
        stats._check({event["kind"] for event in history} == {"context_creation", "context_review"},
                     "Freeze complete actual context authoring/review costs before execution")
        tasks = {task["id"]: task for task in protocol["tasks"]}
        expected_phases = []
        for row in protocol["planned"]:
            key = row["batch_id"], tasks[row["task_id"]]["phase_id"]
            if not expected_phases or key != expected_phases[-1]:
                expected_phases.append(key)
        packets = stats._list(collection["phases"], "phase collections")
        stats._check([(p["binding"]["batch_id"], p["binding"]["phase_id"]) for p in packets] == expected_phases,
                     "Complete original batch and phase order does not match")
        grouped = {batch["id"]: {arm: {"cost": 0.0, "weighted_correct": 0.0, "weight": 0.0,
                   "correct": 0, "runs": 0, "weighted_seconds": 0.0, "overhead_seconds": 0.0,
                   "attempts": 0, "late_answers": 0} for arm in stats.ARMS} for batch in protocol["batches"]}
        details, previous, executions, bindings, model_calls = {}, {}, {}, {}, 0
        last_finished = stats._timestamp(protocol["frozen_at"], "freeze")
        for packet in packets:
            binding, execution, rows, calls = _phase(protocol, packet, reasons)
            batch, phase = binding["batch_id"], binding["phase_id"]
            phase_start, phase_finish, _ = _interval(execution, "phase execution")
            stats._check(last_finished <= phase_start, "Phase executions overlap or depart from original order")
            last_finished = phase_finish
            if batch in previous:
                prior_binding, prior_finish = previous[batch]
                _same(binding["previous_binding_sha256"], prior_binding["binding_sha256"], "Executed previous phase binding")
                stats._check(prior_finish <= stats._timestamp(binding["transition"]["started_at"], "transition start"),
                             "Both arms must finish the preceding phase before transition")
            previous[batch] = binding, phase_finish
            executions[(batch, phase)] = execution
            bindings[(batch, phase)] = binding
            model_calls += calls
            for key, row in rows:
                stats._check(key not in details, "Duplicate trial; best-of selection is forbidden")
                details[key] = row
                values, weight = grouped[key[0]][key[3]], tasks[key[1]]["weight"]
                values["cost"] += row["query_cost"]
                values["correct"] += int(row["correct"])
                values["weighted_correct"] += weight * int(row["correct"])
                values["weight"] += weight
                values["runs"] += 1
                values["weighted_seconds"] += weight * row["effective_seconds"]
                values["attempts"] += 1
                values["late_answers"] += int(row["late_answer"])
        for batch, arms in grouped.items():
            for arm, values in arms.items():
                ledger = accounting["per_batch"][batch][arm]
                values["cost"] += ledger["known_cost_usd"]
                values["overhead_seconds"] += ledger["seconds"]
        comparison_spend = sum(values["cost"] for arms in grouped.values() for values in arms.values())
        total_spend = comparison_spend + protocol["provisioning_spend_usd"]
        result["known_comparison_spend_usd"] = comparison_spend
        result["known_total_study_spend_usd"] = total_spend
        result["model_calls"] = model_calls
        if len(details) != len(protocol["planned"]):
            reasons.append("Missing executed trials; no artificial start times or zero-cost attempts were supplied")
            return result
        stats._check(list(details) == [stats._key(row) for row in protocol["planned"]], "Full execution schedule differs")
        _check_events(protocol, collection["events"], bindings, details, executions)
        result["first_use"] = _first_use(collection["first_use"], collection["events"], details, grouped)
        if model_calls > protocol["budgets"]["max_model_calls"]:
            reasons.append("Global model-call budget exceeded")
        if comparison_spend > protocol["budgets"]["comparison_usd"] or total_spend > protocol["budgets"]["total_study_usd"]:
            reasons.append("Global comparison or all-in study spend cap exceeded")
        workload_finished = max([last_finished] + [stats._timestamp(event["finished_at"], "work event finish")
            for event in collection["events"] if event["duration_role"] != "cost_only"
            and event["kind"] != "fixture_generation"])
        if (workload_finished - stats._timestamp(protocol["frozen_at"], "freeze")).total_seconds() > protocol["budgets"]["study_seconds"]:
            reasons.append("Post-freeze study elapsed deadline exceeded")
        result["query_latency"] = stats._query_latency(details)
        accounting_rows = [{"batch_id": batch, "arm": arm,
                            "costs": {"all_events": accounting["per_batch"][batch][arm]["known_cost_usd"]}}
                           for batch in grouped for arm in stats.ARMS]
        if not reasons:
            result["task_scorecard"] = stats._task_scorecard(protocol, {"batches": accounting_rows}, details, grouped)
            metrics, intervals, gates = _statistics(grouped, protocol["bootstrap"])
            result.update(metrics=metrics, paired_intervals=intervals, gates=gates,
                          statistical_acceptance="passed" if all(gates.values()) else "failed")
            reasons.extend("Gate not met: " + name for name, passed in gates.items() if not passed)
        return result
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError, StopIteration) as exc:
        reasons.append(str(exc) if isinstance(exc, ValueError) else "Malformed acceptance evidence")
        return result
    finally:
        require_billing_reconciliation(result)


def _check_events(protocol, events, bindings, details, executions):
    attempts = {row["trial_id"]: (key, row) for key, row in details.items()}
    for event in events:
        start, finish, _ = _interval(event, "accounting event")
        if event["duration_role"] == "within_attempt":
            stats._check(event["attempt_id"] in attempts, "Accounting refers to an unknown attempt")
            key, run = attempts[event["attempt_id"]]
            stats._check(event["batch_id"] == key[0] and event["phase_id"] == run["phase_id"]
                         and run["started"] <= start <= finish <= run["finished"], "Subspan is outside its actual trial")
            stats._check(event["allocation"][key[3]] == 1, "Attempt subspan belongs to a different arm")
            if event["cost_usd"] is not None:
                stats._check(event["cost_usd"] <= run["query_cost"] + 1e-12, "Included subspan exceeds measured attempt spend")
            if event["kind"] == "source_tool_calls":
                stats._check(event["cost_usd"] == 0,
                             "Separate source fees cannot be hidden in model spend; retain incremental cost_only billing evidence")
        if event["duration_role"] == "exclusive_overhead":
            for key, run in details.items():
                if event["batch_id"] not in (None, key[0]) or not event["allocation"][key[3]]:
                    continue
                stats._check(finish <= run["started"] or start >= run["finished"],
                             "Overhead duplicates an already measured trial interval")
    for (batch, phase), binding in bindings.items():
        matching = [event for event in events if event["batch_id"] == batch and event["phase_id"] == phase]
        execution = executions[(batch, phase)]
        for stage in ("preflight", "postflight"):
            measurement = verification_evidence(execution, stage)
            validations = [event for event in matching if event["kind"] == "evaluator_validation"
                           and event["evidence_sha256"] == _hash(measurement)]
            stats._check(len(validations) == 1, "Each actual pre/postflight needs a separate complete accounting event")
            for name in ("started_at", "finished_at", "elapsed_seconds"):
                _same(validations[0][name], measurement[name], "Original verification " + name)
            _same(validations[0]["duration_role"], "exclusive_overhead", "Verification overhead")
        kind = "workspace_ingestion" if phase == "baseline" else "refresh"
        imports = [event for event in matching if event["kind"] == kind and event["allocation"]["workspace"] == 1]
        stats._check(len(imports) == 1, "Every phase needs one complete measured workspace import/refusal event")
        capture_start, capture_finish = _capture_interval(binding["capture"])
        event = imports[0]
        _same(event["evidence_sha256"], _hash(binding["capture"]), "Retained exact sync capture")
        start, finish, _ = _interval(event, "workspace preparation")
        stats._check(event["duration_role"] == "exclusive_overhead" and start <= capture_start <= capture_finish <= finish
                     <= stats._timestamp(binding["bound_at"], "binding"), "Workspace preparation must retain its actual capture and readiness times")
        transition = binding["transition"]
        transitions = [event for event in matching if event["kind"] == "phase_transitions"]
        if transition is not None:
            stats._check(len(transitions) == 1, "Every transition needs complete measured cost/time")
            _same(transitions[0]["evidence_sha256"], transition["receipt_sha256"], "Applied transition receipt")
            for key in ("started_at", "finished_at", "elapsed_seconds"):
                _same(transitions[0][key], transition[key], "Original transition " + key)
        else:
            stats._check(not transitions, "Baseline has no transition")
    # Explicit zero-fee receipts are necessary; absence is not zero.
    for batch in {key[0] for key in bindings}:
        for kind in ("runtime_license", "workflow_configuration"):
            stats._check(any(event["kind"] == kind and event["batch_id"] in (None, batch) for event in events),
                         "Missing explicit cost/time coverage for " + kind + " in " + batch)
        for arm in stats.ARMS:
            rows = [row for key, row in details.items() if key[0] == batch and key[3] == arm]
            begin, end = min(row["started"] for row in rows), max(row["finished"] for row in rows)
            fees = [event for event in events if event["kind"] == "source_tool_calls"
                    and event["duration_role"] == "cost_only" and event["batch_id"] == batch
                    and event["allocation"][arm] == 1]
            stats._check(any(stats._timestamp(event["started_at"], "tool billing start") <= begin
                            and stats._timestamp(event["finished_at"], "tool billing finish") >= end for event in fees),
                         "Retain complete per-arm tool-fee coverage, including explicit zero fees")
            runtime = [event for event in events if event["kind"] == "runtime_license"
                       and event["batch_id"] in (None, batch)]
            stats._check(any(stats._timestamp(event["started_at"], "runtime start") <= begin
                            and stats._timestamp(event["finished_at"], "runtime finish") >= end for event in runtime),
                         "Runtime/license accounting does not cover the observed workload")


def verification_evidence(execution, stage):
    """The exact measured engine verification envelope for event receipt binding."""
    stats._check(stage in {"preflight", "postflight"}, "Unknown verification stage")
    return {"plan_sha256": execution["plan_sha256"], "stage": stage,
            "started_at": execution[stage + "_started_at"], "finished_at": execution[stage + "_finished_at"],
            "elapsed_seconds": execution[stage + "_seconds"], "source_stability": execution["source_stability"]}


def _first_use(records, events, details, grouped):
    stats._list(records, "first_use")
    seen, receipts, output = set(), {event["evidence_sha256"] for event in events}, {arm: [] for arm in stats.ARMS}
    for record in records:
        stats._record(record, {"batch_id", "arm", "task_id", "repeat", "cold_started_at", "finished_at", "evidence_sha256"}, "first_use")
        identity = record["batch_id"], record["arm"]
        stats._check(identity not in seen and identity[0] in grouped and identity[1] in stats.ARMS, "Invalid or duplicate first-use receipt")
        seen.add(identity)
        key = next(key for key in details if (key[0], key[3]) == identity)
        stats._check((record["task_id"], record["repeat"]) == (key[1], key[2]), "First scheduled query differs")
        row = details[key]
        start = stats._timestamp(record["cold_started_at"], "cold start")
        finish = stats._timestamp(record["finished_at"], "cold finish")
        stats._check(start <= row["started"] and finish == row["finished"], "Cold clock must include its entire first query")
        stats._sha(record["evidence_sha256"], "cold receipt")
        stats._check(record["evidence_sha256"] not in receipts, "Cold-start receipt reused across arms or batches")
        receipts.add(record["evidence_sha256"])
        candidates = [event for event in events if event["batch_id"] == key[0] and event["phase_id"] == "baseline"
                      and event["allocation"][key[3]] > 0 and event["kind"] in
                      ({"workspace_ingestion", "workflow_configuration", "workspace_mcp_startup"} if key[3] == "workspace"
                       else {"workflow_configuration", "source_mcp_startup"})]
        stats._check(candidates and start == min(stats._timestamp(event["started_at"], "cold setup") for event in candidates),
                     "Cold start must equal the earliest recorded product setup, not a later binding time")
        output[key[3]].append((finish - start).total_seconds())
    stats._check(seen == {(batch, arm) for batch in grouped for arm in stats.ARMS}, "Complete paired first-use coverage differs")
    return {"status": "recorded", "scope": "Observed cold wall time including shared-barrier waiting; separate from allocated service time.",
            "by_arm": {arm: stats._distribution(values) for arm, values in output.items()}}
