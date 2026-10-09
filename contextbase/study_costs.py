"""Frozen, machine-readable prices and local source assignments for a study.

The controller validates the complete acceptance protocol before using this
helper. This module rechecks its content hash and the exact accounting-policy
commitment, but does not repeat expensive fixture reconstruction. Prices are
reviewed supplied billing terms, not authenticated invoices or default zeros.
No source, model or network calls are made here.

``rate`` means USD per operation for ``fixed_per_operation`` and USD per second
for ``usd_per_second``. ``declared_zero`` requires an explicit zero rate and a
billing reference. The same operation must not be billed again under another
rule. Model invocations/retries use their provider-reported costs; historical
context and provisioning retain their already frozen actual charges.

Reservations use the declared maximum duration. A quote always retains the
actual incurred price, even beyond that duration; the controller must settle
it and stop on an overshoot instead of truncating or discarding spend.
"""
from __future__ import annotations

from copy import deepcopy
import math
from urllib.parse import urlsplit

from .acceptance_protocol import EVENT_RULES
from .rystic import local_url
from .study_binding import _hash, _json, _same
from . import study_report as stats


SPEC = "contextbase.execution-cost-contract/v1"
EXCLUDED_KINDS = frozenset({"model_calls", "context_creation", "context_review",
                            "fixture_generation", "retries_failures"})
COST_KINDS = frozenset(EVENT_RULES) - EXCLUDED_KINDS
CONTRACT_FIELDS = {"spec", "currency", "rules", "assignments"}
RULE_FIELDS = {"method", "rate", "max_seconds", "allocation", "billing_evidence_sha256", "rationale"}
ASSIGNMENT_FIELDS = {"environment_id", "stripe_url", "linear_url"}
METHODS = {"declared_zero", "fixed_per_operation", "usd_per_second"}
ZERO_ONLY = {"source_tool_calls", "source_mcp_startup", "workspace_mcp_startup"}


def _rule(contract, kind):
    stats._check(type(kind) is str and kind in COST_KINDS, "Unsupported controller cost kind")
    rules = stats._object(contract.get("rules"), "cost rules")
    rule = stats._record(rules.get(kind), RULE_FIELDS, "cost rule " + kind)
    method = rule["method"]
    stats._check(type(method) is str and method in METHODS, "Unknown cost pricing method")
    rate = stats._number(rule["rate"], "cost rate")
    maximum = stats._number(rule["max_seconds"], "maximum operation seconds", positive=True)
    stats._sha(rule["billing_evidence_sha256"], "billing evidence reference")
    stats._text(rule["rationale"], "billing rationale")
    if method == "declared_zero":
        stats._check(rate == 0, "A declared-zero rule must have an explicit zero rate")
    else:
        stats._check(rate > 0, "A zero charge must use declared_zero with explicit billing evidence")
    if kind in ZERO_ONLY:
        stats._check(method == "declared_zero",
                     "Source tool fees and MCP startup support explicit zero charges only; separate native per-call billing is not instrumented")
    mode = EVENT_RULES[kind]["allocation"]
    if mode == "measured_shared":
        allocation = stats._record(rule["allocation"], {"source", "workspace"}, "shared cost allocation")
        fractions = [stats._number(allocation[arm], "shared allocation " + arm)
                     for arm in ("source", "workspace")]
        stats._check(all(value <= 1 for value in fractions)
                     and math.isclose(sum(fractions), 1.0, rel_tol=0, abs_tol=1e-12),
                     "Shared cost allocations must sum to one")
    else:
        _same(rule["allocation"], mode, "Frozen cost ownership mode")
    reservation = rate * maximum if method == "usd_per_second" else rate
    stats._number(reservation, "maximum reserved cost")
    return rule


def quote(contract, kind, elapsed_seconds):
    """Return full known USD for an operation, including a duration overshoot."""
    try:
        rule = _rule(contract, kind)
        elapsed = stats._number(elapsed_seconds, "actual operation seconds")
        amount = rule["rate"] * elapsed if rule["method"] == "usd_per_second" else rule["rate"]
        return stats._number(amount, "actual operation cost")
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed operation cost input; no price is assumed.") from exc


def maximum_reservation(contract, kind):
    """Return the pre-operation USD reservation; unknown prices are rejected."""
    try:
        rule = _rule(contract, kind)
        return quote(contract, kind, rule["max_seconds"])
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed operation cost input; no reservation is assumed.") from exc


def validate_cost_contract(protocol, contract):
    """Validate a cost artifact bound to an already validated frozen protocol.

    Returns an independent copy. Source assignments cover every declared batch,
    name its provisioned environment and use separate numeric loopback ports.
    No endpoint is contacted and no declared review is treated as authentication.
    """
    try:
        _json(protocol); _json(contract)
        stats._object(protocol, "protocol")
        digest = stats._sha(protocol.get("protocol_sha256"), "protocol hash")
        _same(digest, _hash({key: value for key, value in protocol.items() if key != "protocol_sha256"}),
              "Unchanged original protocol")
        stats._record(contract, CONTRACT_FIELDS, "execution cost contract")
        _same(contract["spec"], SPEC, "Execution cost contract specification")
        _same(contract["currency"], "USD", "Execution billing currency")
        policy = stats._object(protocol.get("accounting_policy"), "frozen accounting policy")
        _same(policy.get("event_rules"), EVENT_RULES, "Original accounting event rules")
        _same(policy.get("unknown_cost_policy"), "inconclusive", "Unknown cost treatment")
        _same(stats._sha(policy.get("evidence_sha256"), "cost-contract commitment"), _hash(contract),
              "Cost contract differs from the frozen accounting evidence")
        budgets = stats._object(protocol.get("budgets"), "study budgets")
        duration_cap = stats._number(budgets.get("study_seconds"), "study duration", positive=True)
        comparison_cap = stats._number(budgets.get("comparison_usd"), "comparison budget", positive=True)
        stats._check(set(stats._object(contract["rules"], "cost rules")) == set(COST_KINDS), "Exact controller cost coverage differs")
        for kind in sorted(COST_KINDS):
            rule = _rule(contract, kind)
            stats._check(rule["max_seconds"] <= duration_cap,
                         "Operation duration limit exceeds the original study deadline")
            stats._check(maximum_reservation(contract, kind) <= comparison_cap,
                         "An operation reservation exceeds the original comparison budget")
        batches = stats._list(protocol.get("batches"), "declared batches")
        batch_ids = [stats._text(row.get("id"), "batch id") for row in batches]
        stats._check(len(batch_ids) == len(set(batch_ids)), "Duplicate declared batch")
        intents = stats._list(protocol.get("intents"), "provisioned intents")
        environments = {}
        for intent in intents:
            batch = stats._text(intent.get("batch_id"), "intent batch")
            stats._check(batch not in environments, "Duplicate provisioned intent")
            environments[batch] = stats._text(intent["provisioning"]["environment_id"], "provisioned environment")
        stats._check(set(environments) == set(batch_ids), "Provisioned batch coverage differs")
        stats._check(len(set(environments.values())) == len(environments), "Provisioned environments cannot be reused")
        assignments = stats._object(contract["assignments"], "source assignments")
        stats._check(set(assignments) == set(batch_ids), "Exact source assignment coverage differs")
        ports = set()
        for batch in batch_ids:
            assignment = stats._record(assignments[batch], ASSIGNMENT_FIELDS, "source assignment")
            _same(assignment["environment_id"], environments[batch], "Provisioned environment assignment")
            for name in ("stripe_url", "linear_url"):
                value = stats._text(assignment[name], "numeric loopback source URL")
                normalized = local_url(value)
                _same(value, normalized, "Canonical source URL without a trailing slash")
                parsed = urlsplit(normalized)
                host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
                _same(value, "http://" + host + ":" + str(parsed.port), "Exact numeric loopback URL")
                # Treat IPv4/IPv6 aliases on one port conservatively as one
                # service, so aliases cannot silently reuse a native pair.
                stats._check(parsed.port not in ports, "Each source and independent batch requires a distinct loopback port")
                ports.add(parsed.port)
        return deepcopy(contract)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed execution cost contract; unknown charges or assignments are not assumed.") from exc
