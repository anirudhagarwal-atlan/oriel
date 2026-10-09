"""Phase-aware development tasks and exact scoring for the synthetic recipe.

Gold is calculated only from authored seed intent, with integer arithmetic and
no database, product SQL, source calls, or model. These public development tasks
are not held-out acceptance evidence. Pass only ``question`` and
``answer_schema`` to an agent; ``expected`` belongs exclusively to the scorer.
The supplied snapshot receipt identifies the evaluation state. Requiring its
echo in an answer tests provenance attribution, not independent drift detection.
Phase tasks calculate gold from each phase's authored state. Guarded tasks
also inspect explicit customer-identity and policy defects; they never treat
an invalid cohort as a successfully imported workspace snapshot. None of these
builders calls Rystic or verifies that the authored state was materialized.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re

from .rystic_recipe import FIXTURE_NAMESPACE, RECIPE_SPEC


_POLICY = """Use only the synthetic cohort marked contextbase_fixture={namespace}.
Join Stripe customer metadata.account_id to the exact contextbase_account_id
line in Linear issue descriptions. Ignore unrelated simulator records. For
payments, include only USD charges with paid=true, captured=true,
status=succeeded, and disputed=false. Subtract each charge's refunded amount in
integer cents; do not convert currencies, annualize, or call payments ARR.
Qualifying issues have priority 1 or 2 and workflow state type backlog,
unstarted, or started. Aggregate payments and issues separately by account so
multiple issues cannot multiply payments. A qualifying account is external
(metadata.is_internal=false), has positive eligible net USD payments, and has
at least one qualifying issue. Owners belong to individual issues: return
owner_id and owner_name as null when unassigned. Return complete sets without
duplicates, in any order, and return only the specified JSON object. Monetary
values and counts must be JSON integers, never decimal dollars or strings.
All history in the frozen source state is in scope; do not apply date filters.
"""


def _json_value(value, path="value", depth=0):
    if depth > 40:
        raise ValueError(f"{path}: JSON nesting exceeds 40 levels")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]", depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, item in value.items():
            _json_value(item, f"{path}.{key}", depth + 1)
        return
    raise ValueError(f"{path}: requires a finite JSON value")


def _string(value, field, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value:
        raise ValueError(f"{field}: requires a nonempty string")


def _integer(value, field, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{field}: requires an integer in the supported range")


def _oracle(manifest):
    _json_value(manifest, "manifest")
    if (type(manifest) is not dict or manifest.get("spec") != RECIPE_SPEC or
            manifest.get("namespace") != FIXTURE_NAMESPACE or
            manifest.get("status") != "complete" or manifest.get("synthetic") is not True):
        raise ValueError("Requires a complete synthetic Rystic recipe manifest")
    accounts = manifest.get("accounts")
    if type(accounts) is not list or not accounts:
        raise ValueError("Manifest requires a nonempty accounts list")
    if type(manifest.get("account_count")) is not int or manifest["account_count"] != len(accounts):
        raise ValueError("Manifest account_count does not match its accounts")
    seen = {key: set() for key in ("account_id", "customer_id", "charge_id", "issue_id", "identifier")}

    def unique(record, key):
        value = record.get(key)
        _string(value, key)
        if value in seen[key]:
            raise ValueError(f"Duplicate manifest {key}")
        seen[key].add(value)

    records, controls = [], []
    for account in accounts:
        if type(account) is not dict:
            raise ValueError("Manifest accounts must be objects")
        unique(account, "account_id")
        unique(account, "customer_id")
        _string(account.get("name"), "account.name")
        if type(account.get("is_internal")) is not bool:
            raise ValueError("is_internal requires a boolean")
        if type(account.get("charges")) is not list or type(account.get("issues")) is not list:
            raise ValueError("Manifest requires charge and issue lists")
        gross, refunds = 0, 0
        for charge in account["charges"]:
            if type(charge) is not dict:
                raise ValueError("Manifest charges must be objects")
            unique(charge, "charge_id")
            _integer(charge.get("amount_cents"), "amount_cents")
            _integer(charge.get("refund_cents"), "refund_cents", maximum=charge["amount_cents"])
            for flag in ("paid", "captured", "disputed"):
                if type(charge.get(flag)) is not bool:
                    raise ValueError(f"{flag} requires a boolean")
            _string(charge.get("currency"), "currency")
            _string(charge.get("status"), "status")
            if (charge["currency"] == "usd" and charge["paid"] and charge["captured"] and
                    charge["status"] == "succeeded" and not charge["disputed"]):
                gross += charge["amount_cents"]
                refunds += charge["refund_cents"]
        issues = []
        for issue in account["issues"]:
            if type(issue) is not dict:
                raise ValueError("Manifest issues must be objects")
            unique(issue, "issue_id")
            unique(issue, "identifier")
            _integer(issue.get("priority"), "priority", maximum=4)
            _string(issue.get("state_type"), "state_type")
            for key in ("owner_id", "owner_name"):
                if key not in issue:
                    raise ValueError(f"Manifest issue is missing {key}")
                _string(issue[key], key, nullable=True)
            if issue["priority"] in (1, 2) and issue["state_type"] in ("backlog", "unstarted", "started"):
                issues.append({key: issue[key] for key in ("issue_id", "identifier", "owner_id", "owner_name")})
        issues.sort(key=lambda item: item["issue_id"])
        reasons = []
        if account["is_internal"]:
            reasons.append("internal_account")
        if gross - refunds <= 0:
            reasons.append("no_positive_net_usd")
        if not issues:
            reasons.append("no_unresolved_high_priority_issue")
        records.append({"account_id": account["account_id"], "name": account["name"],
                        "net_collected_usd_cents": gross - refunds, "issues": issues})
        controls.append({"account_id": account["account_id"], "eligible_gross_usd_cents": gross,
                         "eligible_refunded_usd_cents": refunds, "eligible_net_usd_cents": gross - refunds,
                         "qualifying_issue_count": len(issues), "qualifies": not reasons,
                         "exclusion_reasons": reasons})
    records.sort(key=lambda item: item["account_id"])
    controls.sort(key=lambda item: item["account_id"])
    return records, controls


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items):
    return {"type": "array", "items": items, "uniqueItems": True}


def build_tasks(manifest: dict, snapshot: dict) -> list[dict]:
    """Build four development tasks; keep each task's ``expected`` private.

    ``snapshot_id`` and ``data_sha256`` are required in answers when provided
    in ``snapshot``. They are supplied equally to both arms as frozen-state
    metadata, not information agents must derive from arbitrary source IDs.
    """
    records, controls = _oracle(manifest)
    _json_value(snapshot, "snapshot")
    if type(snapshot) is not dict:
        raise ValueError("snapshot must be an object")
    if "namespace" in snapshot and snapshot["namespace"] != manifest["namespace"]:
        raise ValueError("Snapshot namespace differs from manifest")
    provenance = {}
    for key in ("snapshot_id", "data_sha256"):
        if key in snapshot:
            _string(snapshot[key], key)
            provenance[key] = snapshot[key]
    if "data_sha256" in provenance and not re.fullmatch(r"[a-f0-9]{64}", provenance["data_sha256"]):
        raise ValueError("data_sha256 must be a lowercase SHA-256 digest")
    prompt = _POLICY.format(namespace=manifest["namespace"])
    prompt += "\nFrozen-state provenance supplied to both comparison arms: " + json.dumps(provenance, sort_keys=True)
    prompt += ". Echo these exact fields in provenance; do not imply an independent freshness check.\n"
    string, integer = {"type": "string"}, {"type": "integer", "minimum": 0}
    issue_schema = _object({"issue_id": string, "identifier": string,
                            "owner_id": {"type": ["string", "null"]}, "owner_name": {"type": ["string", "null"]}})
    account_fields = {"account_id": string, "name": string, "net_collected_usd_cents": integer,
                      "issues": _array(issue_schema)}
    control_fields = {"account_id": string, "eligible_gross_usd_cents": integer,
                      "eligible_refunded_usd_cents": integer, "eligible_net_usd_cents": integer,
                      "qualifying_issue_count": integer, "qualifies": {"type": "boolean"},
                      "exclusion_reasons": _array({"type": "string", "enum": [
                          "internal_account", "no_positive_net_usd", "no_unresolved_high_priority_issue"]})}
    provenance_schema = _object({key: string for key in provenance})
    eligible_ids = {item["account_id"] for item in controls if item["qualifies"]}
    qualifying = [item for item in records if item["account_id"] in eligible_ids]
    # The canonical demo's eighth case exercises unassigned owners. Shuffled
    # variants need not preserve that case. Select by position, not qualification,
    # so the question does not reveal the answer in either variant.
    selected = records[7] if len(records) >= 8 else records[0]
    tasks = []

    def add(task_id, question, fields, expected):
        tasks.append({"id": task_id, "question": prompt + question,
                      "answer_schema": deepcopy(_object({**fields, "provenance": provenance_schema})),
                      "expected": deepcopy({**expected, "provenance": provenance})})

    add("qualifying_accounts", "List every qualifying external account with its name, net collected USD cents, "
        "and every qualifying issue's issue_id, identifier, owner_id, and owner_name.",
        {"accounts": _array(_object(account_fields))}, {"accounts": qualifying})
    add("exposure_summary", "Return the number of qualifying external accounts, the total number of their "
        "qualifying issues, and their total net collected USD cents. Count each account's payments once.",
        {"account_count": integer, "qualifying_issue_count": integer, "net_collected_usd_cents": integer},
        {"account_count": len(qualifying), "qualifying_issue_count": sum(len(item["issues"]) for item in qualifying),
         "net_collected_usd_cents": sum(item["net_collected_usd_cents"] for item in qualifying)})
    add("payment_controls", "For every account in the cohort, including excluded accounts, return eligible "
        "gross, refunded, and net USD cents, qualifying_issue_count, qualifies, and all applicable "
        "exclusion_reasons. Compute payments even for internal accounts. Reasons are internal_account, "
        "no_positive_net_usd, and no_unresolved_high_priority_issue; return [] when none apply.",
        {"accounts": _array(_object(control_fields))}, {"accounts": controls})
    add("single_account", f"For account_id={selected['account_id']}, return its name, net collected USD cents, "
        "all qualifying issue records with owners, and whether the account qualifies. Return null owners explicitly.",
        {"account": _object({**account_fields, "qualifies": {"type": "boolean"}})},
        {"account": {**selected, "qualifies": selected["account_id"] in eligible_ids}})
    return tasks


def _phase_id(value):
    if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("phase_id must be a lowercase identifier of at most 64 characters")
    return value


def _phase_receipt(receipt, *, guarded=False):
    _json_value(receipt, "source state receipt")
    if type(receipt) is not dict:
        raise ValueError("A phase requires a source state receipt")
    for key in ("snapshot_id", "data_sha256"):
        _string(receipt.get(key), key)
    if not re.fullmatch(r"[a-f0-9]{64}", receipt["data_sha256"]):
        raise ValueError("data_sha256 must be a lowercase SHA-256 digest")
    if "namespace" in receipt and receipt["namespace"] != FIXTURE_NAMESPACE:
        raise ValueError("Receipt namespace differs from the recipe")
    if guarded:
        if receipt.get("receipt_kind") != "source_state_validation":
            raise ValueError("Guarded tasks require a source_state_validation receipt, not an imported workspace snapshot")
        if "refresh_mode" in receipt:
            raise ValueError("A source-validation receipt must not claim a workspace refresh mode")
    elif "receipt_kind" in receipt and receipt["receipt_kind"] != "workspace_snapshot":
        raise ValueError("Ordinary phase tasks require a valid workspace snapshot receipt")


def build_phase_tasks(manifest: dict, snapshot: dict, *, phase_id: str) -> list[dict]:
    """Build ordinary tasks from a baseline or refreshed authored source state.

    This adds phase metadata and prefixes IDs without changing ``build_tasks``.
    Required snapshot IDs/hashes bind the phase but do not prove freshness.
    Freshness correctness requires the actual answers to match the changed
    manifest: copying a new hash onto an old answer is insufficient. The caller
    must separately materialize and verify each phase before any live study.
    """
    phase_id = _phase_id(phase_id)
    _phase_receipt(snapshot)
    tasks = build_tasks(manifest, snapshot)
    for task in tasks:
        task["id"] = phase_id + "/" + task["id"]
        task["phase_id"] = phase_id
    return tasks


def _guarded_state(manifest):
    """Recognize two declared source defects; all other corruption is invalid.

    The scratch copy only substitutes the two guarded fields to let the normal
    oracle validate every other required field and physical source identity.
    Its hypothetical numeric answers are never used when any blocker exists.
    Original state and the caller's authored manifest remain untouched.
    """
    _json_value(manifest, "manifest")
    if (type(manifest) is not dict or manifest.get("spec") != RECIPE_SPEC or
            manifest.get("namespace") != FIXTURE_NAMESPACE or manifest.get("status") != "complete"
            or manifest.get("synthetic") is not True or type(manifest.get("accounts")) is not list):
        raise ValueError("Requires a complete synthetic Rystic recipe manifest")
    validation_copy = deepcopy(manifest)
    by_account = {}
    blockers = []
    for index, account in enumerate(manifest["accounts"]):
        if type(account) is not dict:
            raise ValueError("Manifest accounts must be objects")
        _string(account.get("account_id"), "account_id")
        _string(account.get("customer_id"), "customer_id")
        by_account.setdefault(account["account_id"], []).append(account["customer_id"])
        if "is_internal" not in account or account["is_internal"] is None:
            blockers.append({"source": "stripe.customers", "record_id": account["customer_id"],
                             "field": "metadata.is_internal", "reason": "missing_required_input"})
            validation_copy["accounts"][index]["is_internal"] = False
        # Distinct placeholders isolate validation of physical source IDs and
        # all other fields from the intentionally ambiguous logical mapping.
        validation_copy["accounts"][index]["account_id"] = f"guard_validation_{index}"
    for customer_ids in by_account.values():
        if len(customer_ids) > 1:
            blockers.extend({"source": "stripe.customers", "record_id": customer_id,
                             "field": "metadata.account_id", "reason": "ambiguous_identity"}
                            for customer_id in customer_ids)
    _oracle(validation_copy)
    if blockers:
        blockers.sort(key=lambda item: (item["source"], item["record_id"], item["field"], item["reason"]))
        return blockers, None
    records, controls = _oracle(manifest)
    eligible_ids = {item["account_id"] for item in controls if item["qualifies"]}
    qualifying = [record for record in records if record["account_id"] in eligible_ids]
    return [], {"account_count": len(qualifying),
                "qualifying_issue_count": sum(len(record["issues"]) for record in qualifying),
                "net_collected_usd_cents": sum(record["net_collected_usd_cents"] for record in qualifying)}


def build_guarded_task(manifest: dict, receipt: dict, *, phase_id: str) -> dict:
    """Build an outcome-neutral answer-or-abstain task from authored state.

    A valid state must produce the independent exposure summary. Duplicate
    logical account IDs across distinct Stripe customers, or absent/null
    ``is_internal`` policy, require abstention with every exact affected record
    and field. Unassigned owners, zero payments and closed issues are valid
    business data, not reasons to abstain. All other malformed manifest fields
    raise ValueError rather than becoming an automatic abstention success.

    ``receipt`` requires receipt_kind='source_state_validation', snapshot_id,
    data_sha256, and optionally the recipe namespace. It must not contain a
    workspace refresh_mode. These are receipts of source state/validation,
    never claims that invalid data entered DuckDB. Live receipt delivery and
    stale-workspace rejection still require a separately verified runtime path.
    Public snapshot IDs use the same hash-derived alias for every status: the
    native receipt's ID prefix must not reveal whether analysis should succeed.

    Send only question and answer_schema to the agent. Phase/receipt metadata
    and expected gold stay with the evaluator; the same public contract is
    used for valid controls and both guarded defect types.
    """
    phase_id = _phase_id(phase_id)
    _phase_receipt(receipt, guarded=True)
    blockers, summary = _guarded_state(manifest)
    provenance = {key: receipt[key] for key in ("data_sha256", "receipt_kind")}
    provenance["snapshot_id"] = "source-validation-" + receipt["data_sha256"][:16]
    integer = {"type": "integer", "minimum": 0}
    summary_schema = _object({"account_count": integer, "qualifying_issue_count": integer,
                              "net_collected_usd_cents": integer})
    blocker_schema = _object({
        "source": {"type": "string", "enum": ["stripe.customers"]},
        "record_id": {"type": "string"},
        "field": {"type": "string", "enum": ["metadata.account_id", "metadata.is_internal"]},
        "reason": {"type": "string", "enum": ["ambiguous_identity", "missing_required_input"]},
    })
    provenance_schema = _object({key: {"type": "string"} for key in provenance})
    answer_schema = {"oneOf": [
        _object({"status": {"const": "answered"}, "result": summary_schema,
                 "blockers": {**_array(blocker_schema), "maxItems": 0}, "provenance": provenance_schema}),
        _object({"status": {"const": "abstained"}, "result": {"type": "null"},
                 "blockers": {**_array(blocker_schema), "minItems": 1}, "provenance": provenance_schema}),
    ]}
    question = _POLICY.format(namespace=FIXTURE_NAMESPACE) + "\n" + (
        "Return the exposure summary (account_count, qualifying_issue_count, and net_collected_usd_cents), "
        "subject to these mandatory input checks. Each Stripe customer must map to a unique account_id; "
        "multiple distinct customer IDs carrying the same account_id are ambiguous under this recipe policy. "
        "Every cohort customer must explicitly declare is_internal as true or false. A missing/null policy "
        "must not default to external. Inspect the current source state or verified validation evidence. "
        "If either check fails anywhere in the cohort, return status=abstained and result=null; report every "
        "offending source record and field in blockers. For duplicate mappings cite every colliding customer "
        "with reason=ambiguous_identity and field=metadata.account_id. For missing policy cite each affected "
        "customer with reason=missing_required_input and field=metadata.is_internal. Use actual customer IDs "
        "as record_id and stripe.customers as source. If both checks pass, return status=answered with the "
        "complete numeric summary and blockers=[]. A zero result or unassigned owner alone does not justify "
        "abstention. Do not omit bad records to obtain a partial answer, infer identities from names, guess "
        "policy, or answer from an older imported workspace.\n"
        "The following provenance identifies source state and validation using a hash-derived observation "
        "alias; it does not certify a successful workspace import. Echo its exact fields in provenance. "
        "Echoing the provenance alone is not evidence "
        "for an answer or abstention: " + json.dumps(provenance, sort_keys=True)
    )
    return {"id": phase_id + "/guarded_exposure", "phase_id": phase_id,
            "receipt_kind": "source_state_validation", "question": question, "answer_schema": answer_schema,
            "expected": {"status": "abstained" if blockers else "answered", "result": summary,
                         "blockers": blockers, "provenance": provenance}}


def score_answer(answer, expected) -> dict:
    """Exact typed scoring with unordered complete sets and no partial credit.

    Parse model JSON with duplicate-key rejection before calling: duplicate
    object keys cannot be recovered from an already-parsed Python dictionary.
    Errors describe locations, not hidden expected values, and are capped at 32.
    """
    _json_value(expected, "expected")
    if type(expected) is not dict:
        raise ValueError("expected must be an object")
    try:
        _json_value(answer, "answer")
    except ValueError as exc:
        return {"correct": False, "errors": [str(exc)]}
    errors = []

    def error(message):
        if len(errors) < 32:
            errors.append(message)

    def compare(actual, gold, path):
        if type(actual) is not type(gold):
            error(f"{path}: incorrect JSON type")
        elif type(gold) is dict:
            if set(actual) != set(gold):
                error(f"{path}: missing or unexpected fields")
            for key in gold.keys() & actual.keys():
                compare(actual[key], gold[key], f"{path}.{key}")
        elif type(gold) is list:
            canonical = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
            actual_items = [canonical(item) for item in actual]
            if len(actual_items) != len(set(actual_items)):
                error(f"{path}: duplicate records or values")
            for identity in ("account_id", "issue_id", "charge_id"):
                if actual and all(type(item) is dict and identity in item for item in actual):
                    keys = [canonical(item[identity]) for item in actual]
                    if len(keys) != len(set(keys)):
                        error(f"{path}: duplicate {identity}")
            # Recurse into matching identities so nested arrays are also sets.
            identity = next((key for key in ("account_id", "issue_id", "charge_id")
                             if gold and all(type(item) is dict and key in item for item in gold + actual)), None)
            if identity:
                gold_map = {canonical(item[identity]): item for item in gold}
                actual_map = {canonical(item[identity]): item for item in actual}
                if set(gold_map) != set(actual_map) or len(actual) != len(gold):
                    error(f"{path}: incomplete or extra records")
                for key in gold_map.keys() & actual_map.keys():
                    compare(actual_map[key], gold_map[key], f"{path}[{identity}={key}]")
            elif sorted(actual_items) != sorted(canonical(item) for item in gold):
                error(f"{path}: incorrect set contents")
        elif actual != gold:
            error(f"{path}: incorrect value")

    compare(answer, expected, "answer")
    return {"correct": not errors, "errors": errors}
