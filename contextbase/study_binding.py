"""Offline, fail-closed binding of authored phases to supplied source evidence.

``bind_phase`` performs no source, model or database calls. Input receipts and
origin labels are operator assertions, not authenticated native execution or
proof of complete pagination. The resulting development packet cannot authorize
execution or support performance claims. Send only each ``public_tasks`` entry
to an agent; the packet and parallel ``evaluator_tasks`` remain evaluator-only.

All artifact hashes use UTF-8 strict canonical JSON: sorted object keys, compact
separators, ASCII escaping, and finite JSON values only. A question is hashed as
a JSON string including its quotes, not as bare UTF-8. ``binding_sha256`` hashes
the complete packet before that field is added. Source hashes never derive from
intent manifests. Refund totals are checked; separate refund objects are not
among the three captured collections and are not independently verified here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from .rystic import PINS, SCHEMAS, _normalize_snapshot
from .rystic_eval import _guarded_state, build_guarded_task, build_phase_tasks
from .rystic_validation import (
    NORMALIZED_HASH_KIND, OBSERVED_HASH_KIND, _cohort, _customer_blockers,
)
from .study_phases import PHASE_KINDS, PHASE_ORDER, PROFILE_SPEC, recurring_tasks

SPEC = "contextbase.phase-binding/v1"


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _json(value):
    """Reject Python-only values and coercible keys before canonical encoding."""
    kind = type(value)
    if kind is dict:
        _check(all(type(key) is str for key in value), "JSON object keys must be strings.")
        for item in value.values():
            _json(item)
    elif kind is list:
        for item in value:
            _json(item)
    elif kind is float:
        _check(math.isfinite(value), "Nonfinite evidence is invalid.")
    else:
        _check(kind in (str, int, bool, type(None)), "Evidence must contain only JSON values.")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _same(actual, expected, label):
    # JSON comparison also distinguishes bool/int and int/float.
    _check(_json(actual) == _json(expected), label + " does not match.")


def _timestamp(value, label):
    _check(type(value) is str and bool(value), label + " is required.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(label + " must be an ISO timestamp.") from None
    _check(stamp.tzinfo is not None and stamp.utcoffset() is not None,
           label + " must include a timezone.")


def _duration(value, label):
    _check(type(value) in (int, float) and math.isfinite(value) and value >= 0,
           label + " must be a finite nonnegative number.")


def _identities(value):
    _check(type(value) is dict and set(value) == set(PINS), "Both pinned simulator identities are required.")
    for source, (product, version) in PINS.items():
        identity = value[source]
        _check(type(identity) is dict and identity.get("product") == product
               and identity.get("version") == version, "Unsupported simulator identity or version.")


def _index(rows, key, label):
    _check(type(rows) is list, label + " must be an array.")
    result = {}
    for row in rows:
        _check(type(row) is dict and type(row.get(key)) is str and bool(row[key]),
               label + " requires physical record IDs.")
        _check(row[key] not in result, label + " repeats a physical record ID.")
        result[row[key]] = row
    return result


def _transition(before, after, transition):
    """Verify the authored action chain without executing native operations."""
    _same(transition.get("before_intent_sha256"), before["intent_sha256"], "Transition before hash")
    _same(transition.get("after_intent_sha256"), after["intent_sha256"], "Transition after hash")
    _same(transition.get("action_sha256"), _hash(transition.get("changes")), "Transition action hash")
    _same(transition.get("native_execution"), "unverified", "Authored transition execution")
    candidate = deepcopy(before["intent_manifest"])
    customers = _index(candidate["accounts"], "customer_id", "Intent customers")
    issues = _index([issue for account in candidate["accounts"] for issue in account["issues"]],
                    "issue_id", "Intent issues")
    changes = transition.get("changes")
    _check(type(changes) is list and bool(changes), "Transition requires declared changes.")
    for change in changes:
        _check(type(change) is dict, "Malformed phase transition.")
        source, operation = change.get("source"), change.get("operation")
        if source == "linear.issues" and operation == "update_workflow_state":
            records, keys = issues, {"state_id", "state_name", "state_type"}
        elif source == "stripe.customers" and operation in ("update_metadata", "remove_metadata_key"):
            records = customers
            keys = {"account_id"} if operation == "update_metadata" else {"is_internal"}
        else:
            raise ValueError("Unsupported authored phase operation.")
        old, new = change.get("before"), change.get("after")
        _check(type(old) is dict and type(new) is dict and set(old) == keys and set(new) == keys,
               "Transition fields are incomplete or unsupported.")
        _check(change.get("record_id") in records, "Transition record is absent.")
        record = records[change["record_id"]]
        _same({key: record.get(key) for key in keys}, old, "Transition previous values")
        record.update(new)
    _same(candidate, after["intent_manifest"], "Authored transition result")


def _plan(plan, phase_id):
    _check(type(plan) is dict, "A prepared phase plan is required.")
    _json(plan)
    _same(plan.get("plan_sha256"), _hash({key: item for key, item in plan.items() if key != "plan_sha256"}),
          "Prepared plan hash")
    _same(plan.get("spec"), "contextbase.phase-preparation/v1", "Prepared plan spec")
    _same(plan.get("kind"), "offline_preparation", "Prepared plan kind")
    _same(plan.get("evidence_origin"), "authored_intent", "Prepared plan origin")
    for field in ("claim_eligible", "execution_authorized", "held_out"):
        _same(plan.get(field), False, "Prepared plan " + field)
    for field in ("model_calls", "source_calls"):
        _same(plan.get(field), 0, "Prepared plan " + field)
    _timestamp(plan.get("created_at"), "Plan creation time")
    scorer = hashlib.sha256(Path(__file__).with_name("rystic_eval.py").read_bytes()).hexdigest()
    _same(plan.get("scorer_sha256"), scorer, "Current scorer hash")
    _same(plan.get("recurring_profile"), {"spec": PROFILE_SPEC, "phase_order": list(PHASE_ORDER),
                                         "phase_kinds": PHASE_KINDS}, "Recurring profile")
    _same(plan.get("tasks"), recurring_tasks(), "Declared recurring tasks")
    phases = _index(plan.get("phases"), "id", "Prepared phases")
    _same(list(phases), list(PHASE_ORDER), "Prepared phase order")
    _check(type(phase_id) is str and phase_id in phases, "Unknown prepared phase ID.")
    for identity, phase in phases.items():
        _same(phase.get("kind"), PHASE_KINDS[identity], "Phase kind")
        _same(phase.get("intent_sha256"), _hash(phase.get("intent_manifest")), "Phase intent hash")
        _check(phase.get("observed_source_sha256") is None and phase.get("native_receipt_sha256") is None,
               "Authored intent must not claim observed evidence.")
        blockers, summary = _guarded_state(phase["intent_manifest"])
        _same(bool(blockers), PHASE_KINDS[identity] == "guarded", "Phase guard kind")
        _same(phase.get("expected_guard"), {"status": "abstained" if blockers else "answered",
                                           "result": summary, "blockers": blockers}, "Phase independent guard")
        _same(phase.get("expected_ingestion"), "refuse_invalid_source" if blockers else "materialize_full_cohort",
              "Phase ingestion intent")
        _same(phase.get("task_ids"), [task["id"] for task in plan["tasks"] if task["phase_id"] == identity],
              "Phase task IDs")
    _check(phases["baseline"]["expected_guard"]["result"] != phases["refreshed"]["expected_guard"]["result"],
           "Refresh must change the independent answer.")
    baseline = phases["baseline"]["intent_manifest"]
    _same(plan.get("seed_manifest_sha256"), _hash(baseline), "Seed manifest hash")
    mappings = {issue["issue_id"]: account["account_id"] for account in baseline["accounts"] for issue in account["issues"]}
    _same(plan.get("source_issue_account_ids"), mappings, "Physical issue mappings")
    transitions = plan.get("transitions")
    _check(type(transitions) is list and len(transitions) == len(PHASE_ORDER) - 1, "Phase action chain is incomplete.")
    for index, transition in enumerate(transitions):
        _check(type(transition) is dict, "Malformed phase transition.")
        before, after = PHASE_ORDER[index:index + 2]
        _same([transition.get("from_phase"), transition.get("to_phase")], [before, after], "Phase transition order")
        _transition(phases[before], phases[after], transition)
    return phases[phase_id]


def _intent_matches(manifest, issue_accounts, rows, tables):
    """Compare complete physical sets and all authored captured semantics."""
    accounts = _index(manifest["accounts"], "customer_id", "Intent customers")
    customers = _index(rows["stripe.customers"], "id", "Observed customers")
    _same(sorted(customers), sorted(accounts), "Customer physical set")
    charges, issues = {}, {}
    for customer_id, account in accounts.items():
        raw = customers[customer_id]
        _same(raw.get("name"), account["name"], "Customer name")
        meta = raw.get("metadata")
        _check(type(meta) is dict, "Customer metadata is required.")
        _same(meta.get("contextbase_fixture"), manifest["namespace"], "Customer fixture marker")
        _same(meta.get("account_id"), account["account_id"], "Customer logical account mapping")
        policy = account["is_internal"]
        _same(meta.get("is_internal"), None if policy is None else "true" if policy else "false",
              "Customer internal-account policy")
        for charge in account["charges"]:
            charges[charge["charge_id"]] = {**charge, "customer_id": customer_id}
        for issue in account["issues"]:
            issues[issue["issue_id"]] = issue
    actual_charges = _index(tables["stripe.charges"], "charge_id", "Observed charges")
    actual_issues = _index(tables["linear.issues"], "issue_id", "Observed issues")
    _same(sorted(actual_charges), sorted(charges), "Charge physical set")
    _same(sorted(actual_issues), sorted(issues), "Issue physical set")
    _same(sorted(issue_accounts), sorted(issues), "Fixed issue mapping physical set")
    charge_fields = {"customer_id": "customer_id", "amount_cents": "amount_cents",
                     "amount_refunded_cents": "refund_cents", "currency": "currency",
                     "paid": "paid", "captured": "captured", "status": "status", "disputed": "disputed"}
    for identity, intended in charges.items():
        actual = actual_charges[identity]
        for actual_key, intended_key in charge_fields.items():
            _same(actual[actual_key], intended[intended_key], "Charge " + actual_key)
    raw_issues = _index(rows["linear.issues"], "id", "Raw issues")
    for identity, intended in issues.items():
        actual = actual_issues[identity]
        for field in ("identifier", "title", "priority", "state_type", "state_name", "owner_id", "owner_name"):
            _check(field in intended, "Intent lacks a captured issue semantic field.")
            _same(actual[field], intended[field], "Issue " + field)
        _same(actual["account_id"], issue_accounts[identity], "Fixed physical issue account mapping")
        _check(type(intended.get("state_id")) is str and bool(intended["state_id"]), "Intent lacks workflow state identity.")
        _same(raw_issues[identity]["state"].get("id"), intended["state_id"], "Issue workflow state identity")


def _validation(validation, manifest, issue_accounts):
    _check(type(validation) is dict and set(validation) == {"receipt", "tables", "observed_source"},
           "Supply the full validation result exactly.")
    _json(validation)
    receipt, observed = validation["receipt"], validation["observed_source"]
    _check(type(receipt) is dict and type(observed) is dict, "Complete source validation is required.")
    namespace = manifest["namespace"]
    for key, expected in (("receipt_kind", "source_state_validation"), ("namespace", namespace),
                          ("complete", True), ("workspace_imported", False), ("synthetic", True)):
        _same(receipt.get(key), expected, "Validation " + key)
    _check("refresh_mode" not in receipt and "error_type" not in receipt,
           "A successful validation receipt cannot claim import or incomplete execution.")
    _timestamp(receipt.get("observed_at"), "Source observation time")
    _duration(receipt.get("source_read_seconds"), "Source read duration")
    _identities(receipt.get("simulators"))
    rows = observed.get("rows")
    _check(type(rows) is dict and set(rows) == set(SCHEMAS), "Complete source collections are required.")
    for name, records in rows.items():
        _index(records, "id", name)
    customers, charges, issues = (rows[name] for name in ("stripe.customers", "stripe.charges", "linear.issues"))
    _same(observed, _cohort(customers, charges, issues, namespace), "Complete scoped source projection")
    tables = _normalize_snapshot(customers, charges, issues, namespace, _customer_guard_mode=True)
    blockers = _customer_blockers(customers)
    _intent_matches(manifest, issue_accounts, rows, tables)
    raw_hash = _hash(observed)
    normalized_hash = _hash(tables) if not blockers else None
    data_hash = raw_hash if blockers else normalized_hash
    for key, expected in (("validation_status", "blocked" if blockers else "valid"),
                          ("blockers", blockers),
                          ("source_rows", {name: len(items) for name, items in rows.items()}),
                          ("observed_source_sha256", raw_hash),
                          ("hash_kind", OBSERVED_HASH_KIND if blockers else NORMALIZED_HASH_KIND),
                          ("data_sha256", data_hash),
                          ("snapshot_id", ("source-state-" if blockers else "rystic-") + data_hash[:16])):
        _same(receipt.get(key), expected, "Validation " + key)
    _same(validation["tables"], None if blockers else tables, "Validation tables")
    return receipt, tables if not blockers else None, raw_hash, normalized_hash


def _snapshot(snapshot, receipt, tables):
    _check(type(snapshot) is dict, "Valid phases require their successful workspace snapshot receipt.")
    _json(snapshot)
    _check(snapshot.get("receipt_kind", "workspace_snapshot") == "workspace_snapshot",
           "A source validation receipt is not a workspace snapshot.")
    for key, expected in (("namespace", receipt["namespace"]), ("simulators", receipt["simulators"]),
                          ("synthetic", True), ("refresh_mode", "full_cohort_replace"),
                          ("data_sha256", receipt["data_sha256"]), ("snapshot_id", receipt["snapshot_id"]),
                          ("rows", {name: len(items) for name, items in tables.items()}),
                          ("source_validation_sha256", _hash(receipt)),
                          ("observed_at", receipt["observed_at"]),
                          ("source_read_seconds", receipt["source_read_seconds"])):
        _same(snapshot.get(key), expected, "Snapshot " + key)


def bind_phase(plan, phase_id, validation, output, *, snapshot=None, evidence_origin="synthetic_test"):
    """Bind one prepared development phase; write once only after every check.

    ``validation`` must be a complete ``capture_validation`` result. Valid phases
    require the snapshot associated with that exact validation receipt. Blocked
    phases forbid a snapshot, including a stale previously successful one. This
    cannot authenticate the operator's origin assertion or inspect a database.

    Use ``sync_snapshot``'s optional ``capture_output`` to retain the full capture
    from that exact import attempt. A separate subsequent capture has a different
    receipt and cannot replace the one associated with the snapshot. This binding
    function itself makes no native calls.
    """
    _check(evidence_origin in ("synthetic_test", "recorded_native"), "Unsupported evidence origin.")
    try:
        phase = _plan(plan, phase_id)
        receipt, tables, raw_hash, normalized_hash = _validation(
            validation, phase["intent_manifest"], plan["source_issue_account_ids"])
        blocked = tables is None
        if blocked:
            _check(snapshot is None, "Blocked phases must not supply a materialized snapshot.")
            tasks = []
        else:
            _snapshot(snapshot, receipt, tables)
            tasks = build_phase_tasks(phase["intent_manifest"], snapshot, phase_id=phase_id)
        tasks.append(build_guarded_task(phase["intent_manifest"], receipt, phase_id=phase_id))
        _same([task["id"] for task in tasks], phase["task_ids"], "Bound phase task IDs")
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Malformed phase evidence; no binding was written.") from exc
    public = [{key: deepcopy(task[key]) for key in ("question", "answer_schema")} for task in tasks]
    private = [{"id": task["id"], "phase_id": phase_id, "expected": deepcopy(task["expected"]),
                "expected_sha256": _hash(task["expected"])} for task in tasks]
    task_artifacts = {task["id"]: {"question_sha256": _hash(task["question"]),
                                    "response_schema_sha256": _hash(task["answer_schema"]),
                                    "scorer_sha256": plan["scorer_sha256"],
                                    "expected_sha256": _hash(task["expected"])} for task in tasks}
    packet = {"spec": SPEC, "phase_id": phase_id, "public_tasks": public, "evaluator_tasks": private,
              "task_artifacts": task_artifacts,
              "artifact_hashes": {"plan_sha256": plan["plan_sha256"], "phase_intent_sha256": phase["intent_sha256"],
                                  "scorer_sha256": plan["scorer_sha256"], "validation_sha256": _hash(validation),
                                  "observed_source_sha256": raw_hash, "normalized_tables_sha256": normalized_hash,
                                  "snapshot_receipt_sha256": _hash(snapshot) if snapshot is not None else None,
                                  "public_tasks_sha256": _hash(public), "evaluator_tasks_sha256": _hash(private)},
              "validation_receipt": deepcopy(receipt), "snapshot_receipt": deepcopy(snapshot),
              "evidence_origin": evidence_origin, "origin_authentication": "operator_assertion_only",
              "held_out": False, "execution_authorized": False, "claim_eligible": False,
              "model_calls": 0, "source_calls": 0,
              "limitations": ["Offline hashes establish input consistency, not native execution or complete pagination.",
                              "This packet is development preparation, not model results or acceptance evidence.",
                              "Only public_tasks question/schema entries may enter agent prompts; keep all other fields evaluator-only.",
                              "Successful import is a supplied receipt assertion; no workspace database is inspected.",
                              "Refund amounts are compared; separate refund objects are not captured."]}
    packet["binding_sha256"] = _hash(packet)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(packet, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return packet
