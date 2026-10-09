"""Freeze allocated acceptance intent and reviewed execution conditions offline.

This compiler validates supplied evidence and review declarations; it does not
authenticate them or perform a power analysis. It makes no source/model calls
and never authorizes execution or public claims. Native provisioning occurs
before this freeze. Its measured apparatus costs remain in the total study
budget, separate from post-freeze comparative workload costs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform

from .study_binding import _hash, _json, _same
from . import study_report as stats

SPEC = "contextbase.acceptance-protocol/v1"
SETTINGS_FIELDS = {"model_id", "context_sha256", "power", "accounting_policy", "budgets", "bootstrap"}
ASSUMPTIONS = {"paired_batch_dependence", "accuracy_effect_and_variance", "cost_effect_and_variance",
               "time_effect_and_variance", "failure_rates", "basis"}
TARGETS = {**stats.TARGETS, "lower_total_spend_required": True, "paired_confidence": 0.95}
# Each role is fixed before observations. Runtime and evaluator validation need
# an explicit shared-allocation explanation in addition to these categories.
EVENT_RULES = {
    "fixture_generation": {"scope": "apparatus", "allocation": "common_equal", "ledger": "apparatus"},
    "workspace_ingestion": {"scope": "comparison", "allocation": "workspace", "ledger": "setup"},
    "source_mcp_startup": {"scope": "comparison", "allocation": "source", "ledger": "setup"},
    "workspace_mcp_startup": {"scope": "comparison", "allocation": "workspace", "ledger": "setup"},
    "refresh": {"scope": "comparison", "allocation": "incurring_arm", "ledger": "refresh"},
    "model_calls": {"scope": "comparison", "allocation": "incurring_arm", "ledger": "attempt"},
    "source_tool_calls": {"scope": "comparison", "allocation": "incurring_arm", "ledger": "attempt"},
    "context_creation": {"scope": "comparison", "allocation": "common_equal", "ledger": "common_review"},
    "context_review": {"scope": "comparison", "allocation": "common_equal", "ledger": "common_review"},
    "workflow_configuration": {"scope": "comparison", "allocation": "incurring_arm", "ledger": "setup"},
    "phase_transitions": {"scope": "comparison", "allocation": "common_equal", "ledger": "transition"},
    "evaluator_validation": {"scope": "comparison", "allocation": "measured_shared", "ledger": "runtime"},
    "runtime_license": {"scope": "comparison", "allocation": "measured_shared", "ledger": "runtime"},
    "retries_failures": {"scope": "comparison", "allocation": "incurring_arm", "ledger": "attempt"},
}
POWER_FIELDS = {"spec", "author", "reviewer", "reviewed_at", "allocation_sha256", "analysis_sha256",
                "analysis_method", "assumptions", "required_independent_batches", "target_power",
                "estimated_joint_power", "review_decision", "no_heldout_outcomes_used", "targets",
                "sensitivity_and_limits"}
ACCOUNTING_FIELDS = {"spec", "author", "reviewer", "reviewed_at", "allocation_sha256", "evidence_sha256",
                     "fixture_generation_treatment", "rate_basis", "event_rules", "shared_allocation",
                     "deduplication", "cold_clock", "cross_arm_waiting", "cache_freshness",
                     "capture_ownership", "unknown_cost_policy", "holdout_change_policy", "review_decision"}
BUDGET_FIELDS = {"total_study_usd", "comparison_usd", "max_model_calls", "max_model_turns_per_run",
                 "run_seconds", "study_seconds"}


def runtime_identity():
    """Freeze every package module used by collection, tools and evaluation."""
    import duckdb
    from . import __version__
    return {"python": platform.python_version(), "duckdb": duckdb.__version__, "contextbase": __version__,
            "code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(Path(__file__).parent.glob("*.py"))}}


def _review(value, spec, fields, allocation, frozen, label):
    stats._record(value, fields, label)
    _same(value["spec"], spec, label + " spec")
    author = stats._text(value["author"], label + " author")
    reviewer = stats._text(value["reviewer"], label + " reviewer")
    stats._check(author.strip().casefold() != reviewer.strip().casefold(), label + " requires a separate reviewer")
    reviewed = stats._timestamp(value["reviewed_at"], label + " reviewed_at")
    stats._check(stats._timestamp(allocation["created_at"], "allocation.created_at") <= reviewed <= frozen,
                 label + " review must follow allocation and precede protocol freeze")
    _same(value["allocation_sha256"], allocation["allocation_sha256"], label + " allocation")
    _same(value["review_decision"], "approved", label + " review decision")


def _settings(settings, allocation, frozen, provisioning_spend):
    stats._record(settings, SETTINGS_FIELDS, "protocol settings")
    stats._text(settings["model_id"], "model_id")
    stats._sha(settings["context_sha256"], "context_sha256")
    power = settings["power"]
    _review(power, "contextbase.power-review/v1", POWER_FIELDS, allocation, frozen, "power")
    stats._sha(power["analysis_sha256"], "power.analysis_sha256")
    stats._text(power["analysis_method"], "power.analysis_method")
    stats._record(power["assumptions"], ASSUMPTIONS, "power assumptions")
    for key in ASSUMPTIONS:
        stats._text(power["assumptions"][key], "power assumption " + key)
    stats._text(power["sensitivity_and_limits"], "power.sensitivity_and_limits")
    _same(power["no_heldout_outcomes_used"], True, "Power review must declare no held-out outcome use")
    _same(power["targets"], TARGETS, "Reviewed power targets")
    minimum = power["required_independent_batches"]
    stats._check(type(minimum) is int and 30 <= minimum <= len(allocation["fixture_batches"]),
                 "Allocation does not meet the reviewed independent batch requirement")
    target = stats._number(power["target_power"], "power.target_power", positive=True)
    estimate = stats._number(power["estimated_joint_power"], "power.estimated_joint_power")
    stats._check(0.8 <= target < 1 and target <= estimate <= 1,
                 "Reviewed joint power estimate must meet the declared target of at least 0.8")
    policy = settings["accounting_policy"]
    _review(policy, "contextbase.accounting-review/v1", ACCOUNTING_FIELDS, allocation, frozen, "accounting")
    stats._sha(policy["evidence_sha256"], "accounting.evidence_sha256")
    _same(policy["fixture_generation_treatment"], "reported_apparatus", "Supported provisioning cost treatment")
    _same(policy["event_rules"], EVENT_RULES, "Reviewed event ownership and ledger mapping")
    _same(policy["unknown_cost_policy"], "inconclusive", "Unknown cost policy")
    _same(policy["holdout_change_policy"], "retire_and_allocate_fresh", "Holdout change policy")
    for key in ("rate_basis", "shared_allocation", "deduplication", "cold_clock", "cross_arm_waiting",
                "cache_freshness", "capture_ownership"):
        stats._text(policy[key], "accounting." + key)
    budgets = stats._record(settings["budgets"], BUDGET_FIELDS, "budgets")
    for key in ("total_study_usd", "comparison_usd", "run_seconds", "study_seconds"):
        stats._number(budgets[key], "budgets." + key, positive=True)
    stats._check(budgets["run_seconds"] <= budgets["study_seconds"], "Run deadline exceeds study deadline")
    stats._check(budgets["total_study_usd"] >= budgets["comparison_usd"] + provisioning_spend,
                 "Total study budget must include the comparison cap and actual provisioning spend")
    calls, turns = budgets["max_model_calls"], budgets["max_model_turns_per_run"]
    stats._check(type(turns) is int and 1 <= turns <= 12, "Declare 1–12 model turns per run")
    stats._check(type(calls) is int and len(allocation["schedule"]) <= calls <= len(allocation["schedule"]) * turns,
                 "Model-call cap must cover the schedule and fit declared per-run turns")
    bootstrap = stats._record(settings["bootstrap"], {"seed", "replicates", "confidence"}, "bootstrap")
    stats._check(type(bootstrap["seed"]) is int and 0 <= bootstrap["seed"] < 2**32, "Bootstrap seed must be uint32")
    draws = bootstrap["replicates"]
    stats._check(type(draws) is int and 2000 <= draws <= 100000
                 and draws * len(allocation["fixture_batches"]) <= 5000000, "Invalid bootstrap draw budget")
    _same(bootstrap["confidence"], 0.95, "Bootstrap confidence")


def _compose(allocation, intents, settings, frozen_at, historical_context_events=None):
    from .acceptance_intent import validate_allocation, validate_intent
    _json(allocation); _json(intents); _json(settings)
    stats._record(settings, SETTINGS_FIELDS, "protocol settings")
    stats._record(settings["accounting_policy"], ACCOUNTING_FIELDS, "accounting policy")
    validate_allocation(allocation)
    frozen = stats._timestamp(frozen_at, "frozen_at")
    stats._check(stats._timestamp(allocation["created_at"], "allocation.created_at") <= frozen,
                 "Protocol freeze precedes allocation")
    # Refuse missing reviews and invalid limits before reconstructing every
    # fixture. The actual apparatus spend is checked again after validation.
    _settings(settings, allocation, frozen, 0)
    stats._list(intents, "intents")
    expected_ids = [item["batch_id"] for item in allocation["fixture_batches"]]
    stats._check([item.get("batch_id") for item in intents if isinstance(item, dict)] == expected_ids
                 and len(intents) == len(expected_ids), "Intents must cover every allocated batch in order")
    seen = {key: set() for key in ("intent", "manifest", "environment", "identity_map", "provisioning")}
    batches, origins, total_spend = [], set(), 0.0
    for intent in intents:
        validate_intent(intent, allocation)
        provisioning = intent["provisioning"]
        stats._check(stats._timestamp(settings["accounting_policy"]["reviewed_at"], "accounting.reviewed_at")
                     <= stats._timestamp(provisioning["started_at"], "provisioning.started_at"),
                     "Accounting policy must be reviewed before provisioning begins")
        for timestamp in (intent["created_at"], provisioning["finished_at"],
                          provisioning["holdout_review"]["reviewed_at"], provisioning["isolation_review"]["reviewed_at"]):
            stats._check(stats._timestamp(timestamp, "intent/provisioning timestamp") <= frozen,
                         "Protocol freeze precedes provisioning or its review")
        identities = {"intent": intent["intent_sha256"], "manifest": intent["manifest_sha256"],
                      "environment": provisioning["environment_id"], "identity_map": intent["identity_map_sha256"],
                      "provisioning": provisioning["evidence_sha256"]}
        for key, value in identities.items():
            stats._check(value not in seen[key], "Independent batches reuse " + key + " evidence")
            seen[key].add(value)
        for key, cost in provisioning["costs_usd"].items():
            total_spend += stats._number(cost, "provisioning costs_usd." + key)
        origins.add(intent["evidence_origin"])
        templates = intent["templates"]
        _same(templates["tasks"], allocation["tasks"], "Allocated task declarations")
        _same(templates["recurring_profile"], allocation["recurring_profile"], "Allocated phase profile")
        batches.append({"id": intent["batch_id"], "intent_sha256": intent["intent_sha256"],
                        "fixture_sha256": intent["manifest_sha256"], "plan_sha256": intent["phase_plan"]["plan_sha256"],
                        "phase_intents": deepcopy(templates["phase_intents"]),
                        "task_commitments": deepcopy(templates["task_commitments"])})
    stats._check(len(origins) == 1, "Do not mix synthetic and declared native intent origins")
    stats._check(settings["budgets"]["total_study_usd"] >= settings["budgets"]["comparison_usd"] + total_spend,
                 "Total study budget must include the comparison cap and actual provisioning spend")
    runtime = runtime_identity()
    historical = deepcopy(historical_context_events or [])
    stats._list(historical, "historical_context_events", nonempty=False)
    for event in historical:
        stats._check(event.get("kind") in {"context_creation", "context_review"},
                     "Historical ledger may contain only frozen context creation and review")
        stats._check(stats._timestamp(event.get("finished_at"), "historical event finish") <= frozen,
                     "Historical context evidence must precede protocol freeze")
    protocol = {"spec": SPEC, "kind": "acceptance_protocol", "frozen_at": frozen_at,
                "allocation": deepcopy(allocation), "allocation_sha256": allocation["allocation_sha256"],
                "intents": deepcopy(intents), "batches": batches, "evidence_origin": next(iter(origins)),
                "origin_authentication": "operator_assertion_only", "holdout_independence": "requires_external_review",
                "runtime": runtime, "runtime_sha256": _hash(runtime),
                **deepcopy(settings), "tasks": deepcopy(allocation["tasks"]),
                "planned": deepcopy(allocation["schedule"]), "repeats": allocation["repeats"],
                "recurring_profile": deepcopy(allocation["recurring_profile"]),
                "provisioning_spend_usd": total_spend,
                "historical_context_events": historical, "historical_context_events_sha256": _hash(historical),
                "execution_authorized": False, "claim_eligible": False,
                "source_calls": 0, "model_calls": 0,
                "limitations": [
                    "Hashes validate consistency; native origin, independent holdouts and review quality require separate evidence review.",
                    "This freeze makes no source/model calls and does not authorize execution or spending.",
                    "Synthetic-test intents cannot support observed acceptance results even with approved review declarations.",
                    "Provisioning is reported apparatus included in the total study budget, not fabricated post-freeze workload expense.",
                    "Future phase observations and complete measured accounting must be collected without changing these commitments.",
                    "Reviewed power estimates are supplied assertions, not an analysis performed by this compiler."]}
    protocol["protocol_sha256"] = _hash(protocol)
    from .acceptance_accounting import summarize_events
    summarize_events(protocol, historical, validate_protocol=False)
    return protocol


def freeze_protocol(allocation, intents, settings, output=None, *, historical_context_events=None):
    """Validate complete inputs and freeze once; an existing output is refused."""
    try:
        protocol = _compose(allocation, intents, settings, datetime.now(timezone.utc).isoformat(), historical_context_events)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed acceptance protocol inputs; no protocol was written.") from exc
    if output is not None:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(protocol, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    return protocol


def validate_protocol(protocol):
    """Reconstruct the entire freeze, including current code and reviewed inputs."""
    try:
        _json(protocol)
        settings = {key: protocol[key] for key in SETTINGS_FIELDS}
        expected = _compose(protocol["allocation"], protocol["intents"], settings, protocol["frozen_at"],
                            protocol["historical_context_events"])
        _same(protocol, expected, "Complete acceptance protocol")
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed acceptance protocol.") from exc
