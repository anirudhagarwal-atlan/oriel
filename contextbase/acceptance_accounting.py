"""Validate supplied acceptance accounting events without executing a study.

This ledger adds only exclusive overhead and incremental fees. Model/tool
subspans marked ``within_attempt`` belong to the attempt ledger and are never
added here. A collector must reconcile those subspans against actual attempts,
check overhead against trial windows, and require its complete event coverage.
This module cannot establish that omitted work was free or authenticate receipts.

Every event has exactly ``EVENT_FIELDS``. ``cost_role`` is ``incremental`` or
``included_in_attempt``; ``duration_role`` is ``exclusive_overhead``,
``within_attempt``, or ``cost_only``. The latter retains the actual billing/tool
interval but adds no elapsed time. Fractions allocate both incremental cost and
exclusive elapsed time. Global comparison events are spread equally across the
declared batches, without changing their frozen arm fractions.

Fixture-generation records, when supplied, corroborate the already protocol-
owned provisioning cost component; consumers must not add them to that apparatus
total again. The protocol's runtime provisioning component remains separately
visible in ``protocol_provisioning_spend_usd``. Missing corroborating fixture
events alone do not make comparative overhead incomplete.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from .acceptance_protocol import EVENT_RULES
from .study_binding import _hash, _json, _same
from . import study_report as stats

SPEC = "contextbase.acceptance-accounting/v1"
ARMS = ("source", "workspace")
EVENT_FIELDS = {"event_id", "kind", "batch_id", "phase_id", "started_at", "finished_at",
                "cost_usd", "elapsed_seconds", "allocation", "evidence_sha256",
                "duration_role", "cost_role", "attempt_id"}
WITHIN_ATTEMPT = {"model_calls", "source_tool_calls", "retries_failures",
                  "source_mcp_startup", "workspace_mcp_startup"}
COST_ONLY = {"runtime_license", "source_tool_calls"}
GLOBAL_KINDS = {"context_creation", "context_review", "runtime_license"}


def _utc(value, label):
    stamp = stats._timestamp(value, label)
    stats._check(stamp.utcoffset() == timedelta(0), label + " must be UTC")
    return stamp


def _total():
    return {"cost_usd": 0.0, "known_cost_usd": 0.0, "seconds": 0.0, "unknown_event_ids": []}


def _add(total, event, fraction=1.0, *, seconds=True):
    if not fraction:
        return
    if event["cost_usd"] is None:
        total["cost_usd"] = None
        total["unknown_event_ids"].append(event["event_id"])
    else:
        total["known_cost_usd"] += event["cost_usd"] * fraction
        if total["cost_usd"] is not None:
            total["cost_usd"] = total["known_cost_usd"]
    if seconds:
        total["seconds"] += event["elapsed_seconds"] * fraction


def _allocation(event, rule):
    value = stats._record(event["allocation"], set(ARMS), "event allocation")
    fractions = {arm: stats._number(value[arm], "allocation." + arm) for arm in ARMS}
    stats._check(all(part <= 1 for part in fractions.values())
                 and math.isclose(sum(fractions.values()), 1.0, rel_tol=0, abs_tol=1e-12),
                 "Event allocation fractions must sum to one")
    mode = rule["allocation"]
    if mode == "common_equal":
        valid = fractions == {"source": 0.5, "workspace": 0.5}
    elif mode in ARMS:
        valid = fractions[mode] == 1 and fractions[ARMS[1 - ARMS.index(mode)]] == 0
    elif mode == "incurring_arm":
        valid = sorted(fractions.values()) == [0, 1]
    else:
        valid = mode == "measured_shared"
    stats._check(valid, "Event allocation violates the frozen " + mode + " ownership rule")


def _roles(event):
    role, kind = event["duration_role"], event["kind"]
    if role == "within_attempt":
        stats._check(kind in WITHIN_ATTEMPT, "This event kind cannot be an attempt subspan")
        _same(event["cost_role"], "included_in_attempt", "Attempt subspan cost role")
        stats._text(event["attempt_id"], "attempt_id")
        stats._check(event["batch_id"] is not None and event["phase_id"] is not None,
                     "Attempt subspans require a batch and phase")
    else:
        stats._check(role in {"exclusive_overhead", "cost_only"}, "Unknown duration role")
        _same(event["cost_role"], "incremental", "Additive event cost role")
        stats._check(event["attempt_id"] is None, "Additive events must not name an attempt")
        if role == "cost_only":
            stats._check(kind in COST_ONLY, "Only runtime licensing and standalone source fees can be cost_only")
        else:
            stats._check(kind not in {"model_calls", "source_tool_calls", "retries_failures", "runtime_license"},
                         "Model/tool/retry totals belong to attempts; licensing adds no elapsed time")


def _fixture(event, provision, start, finish, frozen):
    stats._check(event["phase_id"] is None, "Provisioning apparatus must not be assigned a workload phase")
    stats._check(finish <= frozen, "Provisioning apparatus must precede the protocol freeze")
    stats._check(start == _utc(provision["started_at"], "provisioning.started_at")
                 and finish == _utc(provision["finished_at"], "provisioning.finished_at"),
                 "Fixture event must retain the actual protocol-owned provisioning interval")
    _same(event["evidence_sha256"], provision["evidence_sha256"], "Fixture provisioning receipt")
    _same(event["cost_usd"], provision["costs_usd"]["fixture_generation"], "Fixture generation cost component")


def _overlaps(first, second):
    a, a_start, a_finish = first
    b, b_start, b_finish = second
    if EVENT_RULES[a["kind"]]["scope"] != EVENT_RULES[b["kind"]]["scope"]:
        return False
    same_scope = a["batch_id"] is None or b["batch_id"] is None or a["batch_id"] == b["batch_id"]
    same_arm = any(a["allocation"][arm] > 0 and b["allocation"][arm] > 0 for arm in ARMS)
    return same_scope and same_arm and max(a_start, b_start) < min(a_finish, b_finish)


def _summarize(protocol, events, validate_protocol):
    stats._check(type(validate_protocol) is bool, "validate_protocol must be an explicit boolean")
    if validate_protocol:
        from .acceptance_protocol import validate_protocol as check_protocol
        check_protocol(protocol)
    _json(protocol); _json(events)
    stats._list(events, "events", nonempty=False)
    _same(protocol["accounting_policy"]["event_rules"], EVENT_RULES, "Frozen accounting event rules")
    _same(protocol["accounting_policy"]["unknown_cost_policy"], "inconclusive", "Frozen unknown-cost policy")
    frozen = _utc(protocol["frozen_at"], "protocol.frozen_at")
    now = datetime.now(timezone.utc)
    batch_ids = [stats._text(row["id"], "batch id") for row in stats._list(protocol["batches"], "batches")]
    stats._check(len(batch_ids) == len(set(batch_ids)), "Duplicate protocol batch IDs")
    phases = stats._list(protocol["recurring_profile"]["phase_order"], "phase_order")
    stats._check(len(phases) == len(set(phases)), "Duplicate protocol phase IDs")
    provisions = {row["batch_id"]: row["provisioning"] for row in protocol["intents"]}
    historical = protocol["historical_context_events"]
    stats._list(historical, "historical_context_events", nonempty=False)
    _same(protocol["historical_context_events_sha256"], _hash(historical), "Frozen historical context ledger")
    ids, receipts, attempts, fixtures, exclusive, actual_history = set(), set(), {}, set(), [], []
    normalized = deepcopy(events)
    unknown = []
    for event in normalized:
        stats._record(event, EVENT_FIELDS, "accounting event")
        identifier = stats._text(event["event_id"], "event_id")
        stats._check(identifier not in ids, "Duplicate event_id")
        ids.add(identifier)
        receipt = stats._sha(event["evidence_sha256"], "event evidence_sha256")
        stats._check(receipt not in receipts, "Duplicate complete event evidence receipt")
        receipts.add(receipt)
        kind = stats._text(event["kind"], "kind")
        stats._check(kind in EVENT_RULES, "Unknown accounting event kind")
        rule = EVENT_RULES[kind]
        batch, phase = event["batch_id"], event["phase_id"]
        stats._check(batch in batch_ids or (batch is None and kind in GLOBAL_KINDS),
                     "Event must reference a declared batch; only context or runtime may be global")
        stats._check(phase is None or phase in phases, "Event phase must be declared")
        stats._check(batch is not None or phase is None, "A global event cannot identify one batch's phase")
        if kind in {"refresh", "phase_transitions"}:
            stats._check(phase in phases[1:], "Refresh and transition events must name a non-baseline phase")
        _allocation(event, rule)
        _roles(event)
        elapsed = stats._number(event["elapsed_seconds"], "elapsed_seconds")
        start = _utc(event["started_at"], "event.started_at")
        finish = _utc(event["finished_at"], "event.finished_at")
        stats._check(start <= finish <= now, "Event timestamps must be ordered actual observations, not future work")
        stats._check(math.isclose(elapsed, (finish - start).total_seconds(), rel_tol=1e-6, abs_tol=0.001),
                     "Measured elapsed_seconds does not match the actual UTC interval")
        if event["cost_usd"] is not None:
            stats._number(event["cost_usd"], "cost_usd")
        else:
            unknown.append("Unknown cost_usd for event " + identifier)
        if kind in {"context_creation", "context_review"} and finish <= frozen:
            actual_history.append(event)
        elif kind == "fixture_generation":
            stats._check(batch not in fixtures, "Duplicate fixture-generation corroboration for a batch")
            fixtures.add(batch)
            _fixture(event, provisions[batch], start, finish, frozen)
        elif kind != "runtime_license":
            stats._check(start >= frozen, "Pre-freeze work must be frozen historical context or protocol-owned apparatus")
        if event["duration_role"] == "within_attempt":
            owner = (batch, phase, next(arm for arm in ARMS if event["allocation"][arm] == 1))
            previous = attempts.setdefault(event["attempt_id"], owner)
            stats._check(previous == owner, "Attempt subspans disagree on batch, phase, or owning arm")
        if event["duration_role"] == "exclusive_overhead":
            span = (event, start, finish)
            for other in exclusive:
                stats._check(not _overlaps(span, other), "Exclusive overhead overlaps another event in the same arm and scope")
            exclusive.append(span)
    _same(actual_history, historical, "Exact ordered pre-freeze historical context events")
    per_batch = {batch: {arm: _total() for arm in ARMS} for batch in batch_ids}
    by_arm = {arm: _total() for arm in ARMS}
    comparison, apparatus, subspans = _total(), _total(), _total()
    event_counts = {kind: 0 for kind in EVENT_RULES}
    subspan_ids = []
    for event in normalized:
        event_counts[event["kind"]] += 1
        if event["duration_role"] == "within_attempt":
            _add(subspans, event, seconds=False)
            subspan_ids.append(event["event_id"])
            continue
        seconds = event["duration_role"] == "exclusive_overhead"
        if EVENT_RULES[event["kind"]]["scope"] == "apparatus":
            _add(apparatus, event, seconds=seconds)
            continue
        _add(comparison, event, seconds=seconds)
        targets = batch_ids if event["batch_id"] is None else [event["batch_id"]]
        for arm in ARMS:
            _add(by_arm[arm], event, event["allocation"][arm], seconds=seconds)
            for batch in targets:
                _add(per_batch[batch][arm], event, event["allocation"][arm] / len(targets), seconds=seconds)
    # Arithmetic overflow must not turn a finite list of charges into an
    # unserializable or silently truncated total.
    for total in [comparison, apparatus, subspans, *by_arm.values(),
                  *(value for row in per_batch.values() for value in row.values())]:
        stats._number(total["known_cost_usd"], "total known cost")
        stats._number(total["seconds"], "total seconds")
    return {"spec": SPEC, "status": "inconclusive" if unknown else "validated",
            "scope": "supplied_overhead_and_apparatus_events_only", "events_sha256": _hash(events),
            "protocol_sha256": protocol["protocol_sha256"], "normalized_events": normalized,
            "per_batch": per_batch, "comparison": {**comparison, "by_arm": by_arm},
            "apparatus": {**apparatus, "protocol_owned": True,
                          "missing_batch_ids": [batch for batch in batch_ids if batch not in fixtures],
                          "protocol_provisioning_spend_usd": protocol["provisioning_spend_usd"],
                          "treatment": "corroborating_fixture_component_do_not_add_again"},
            "included_attempt_subspans": {**subspans, "event_ids": subspan_ids,
                                          "arithmetic": "excluded_requires_attempt_reconciliation"},
            "event_counts": event_counts, "unknown_reasons": unknown,
            "full_all_in_complete": False, "claim_eligible": False, "execution_authorized": False,
            "source_calls": 0, "model_calls": 0,
            "limitations": [
                "This validates only supplied events; missing work, full phase coverage and actual receipts require collector/reviewer reconciliation.",
                "Attempt subspan costs and times are excluded; reconcile them against complete model/tool/retry attempt receipts before adding trial totals.",
                "Exclusive overhead is checked against other events here; the collector must also reject overlap with trials in the same arm and scope.",
                "Hashes identify supplied complete allocation receipts, not authenticated billing or native origin.",
                "Global costs and exclusive time are spread equally across declared batches; runtime cost-only intervals add no elapsed seconds.",
                "Protocol-owned provisioning apparatus is reported separately and must not be counted again from corroborating fixture events.",
                "Declared arm allocations retain measured work and waiting; this summary does not invent capture ownership or reassign cross-arm waiting."]}


def summarize_events(protocol, events, *, validate_protocol=True):
    """Return additive overhead summaries; invalid input raises ``ValueError``.

    ``validate_protocol=False`` is for a collector that already validated the
    same immutable protocol, or isolated contract tests. It does not weaken
    event validation. A ``validated`` status means supplied events are internally
    consistent, never that coverage is complete or an acceptance result passed.
    """
    try:
        return _summarize(protocol, events, validate_protocol)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed acceptance accounting ledger.") from exc
