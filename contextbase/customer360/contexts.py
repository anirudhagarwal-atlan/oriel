"""Versioned, reviewed business definitions over customer evidence capabilities.

Definitions use logical mapped fields, never vendor names or executable SQL.
Review certifies an operator's chosen meaning; schema checks do not infer that
an invoice is recurring revenue or that an open case signals churn.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

from .contracts import KINDS, canonical, name, sha256, text
from .profiles import readable_profile

SPEC = "oriel.customer360.context/v1"
DEFINITION_FIELDS = {"name", "description", "kind", "operation", "field", "filters", "unit", "currency_field"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_context_candidates "
                 "(proposal_id VARCHAR PRIMARY KEY, payload VARCHAR)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_context_approvals "
                 "(profile VARCHAR, version INTEGER, context_id VARCHAR UNIQUE, payload VARCHAR, PRIMARY KEY(profile,version))")


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='_contextbase' AND table_name=?",
                        [table]).fetchone() is not None


def _latest(conn, profile_name):
    if not _exists(conn, "customer360_context_approvals"):
        return None
    row = conn.execute("SELECT context_id,payload FROM _contextbase.customer360_context_approvals "
                       "WHERE profile=? ORDER BY version DESC LIMIT 1", [profile_name]).fetchone()
    if row is None:
        return None
    value = json.loads(row[1])
    if value.get("context_id") != row[0] or sha256({key: item for key, item in value.items() if key != "context_id"}) != row[0]:
        raise ValueError("Reviewed customer context changed; inspect the workspace.")
    return value


def _literal(value):
    if value is None or type(value) in (bool, int):
        if type(value) is int and not -(10 ** 29) < value < 10 ** 29:
            raise ValueError("Filter integers exceed the supported exact range.")
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= 1024 and not any(ord(c) < 32 for c in value):
        return
    raise ValueError("Filters require bounded scalar values, not expressions or nested data.")


def _validate_sum_policy(definition, selected, *, required):
    policies = [binding.get("measures", {}).get(definition["field"]) for binding in selected]
    if not required and all(policy is None for policy in policies):
        return  # Existing operator-authored definitions can predate measure policies.
    if any(policy is None for policy in policies):
        raise ValueError("Sum definitions require a reviewed measure policy for every selected binding.")
    if any(policy["aggregation"] != "sum" or policy["unit"] == "unknown" for policy in policies):
        raise ValueError("Reviewed measure units and sum permission must be confirmed before summing.")
    expected = (definition["unit"], definition["currency_field"])
    if any((policy["unit"], policy["currency_field"]) != expected for policy in policies):
        raise ValueError("Sum units and currency fields must match every reviewed measure policy.")


def validate_definitions(definitions, approved_profile, *, require_measure_policy=False):
    if type(require_measure_policy) is not bool:
        raise ValueError("Specify whether reviewed sum policies are required.")
    if not isinstance(definitions, list) or not 1 <= len(definitions) <= 32:
        raise ValueError("Propose between one and 32 business definitions.")
    bindings = approved_profile["candidate"]["profile"]["bindings"]
    seen = set()
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) != DEFINITION_FIELDS:
            raise ValueError("Each definition requires name, description, kind, operation, field, filters, unit and currency_field.")
        key = name(definition["name"])
        if key in seen:
            raise ValueError("Definition names must be unique.")
        seen.add(key)
        text(definition["description"], "business definition", maximum=2000)
        kind = definition["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("Definition requires a known customer capability.")
        selected = [binding for binding in bindings if binding["kind"] == kind]
        if not selected:
            raise ValueError("Definition requires a capability absent from this profile.")
        roles = set.intersection(*(set(binding["fields"]) for binding in selected))
        operation, unit = definition["operation"], definition["unit"]
        if operation not in ("sum", "count"):
            raise ValueError("This context version supports sum and record count definitions.")
        if operation == "count":
            if definition["field"] is not None or unit != "records" or definition["currency_field"] is not None:
                raise ValueError("Record counts have no value or currency field and use the records unit.")
        else:
            name(definition["field"])
            if definition["field"] not in roles or unit not in ("number", "currency", "minor_currency"):
                raise ValueError("Sum definitions require a mapped value field and an explicit unit.")
            if unit in ("currency", "minor_currency"):
                name(definition["currency_field"])
                if definition["currency_field"] not in roles:
                    raise ValueError("Monetary definitions require a mapped currency field.")
            elif definition["currency_field"] is not None:
                raise ValueError("A nonmonetary number has no currency field.")
            _validate_sum_policy(definition, selected, required=require_measure_policy)
        filters = definition["filters"]
        if not isinstance(filters, list) or len(filters) > 12:
            raise ValueError("Use at most 12 explicit conjunctive filters.")
        for predicate in filters:
            if not isinstance(predicate, dict) or set(predicate) != {"field", "operator", "value"}:
                raise ValueError("Filters require field, operator and value.")
            name(predicate["field"])
            if predicate["field"] not in roles or predicate["operator"] not in ("eq", "in"):
                raise ValueError("Filters must reference mapped fields and use eq or in.")
            values = predicate["value"] if predicate["operator"] == "in" else [predicate["value"]]
            if not isinstance(values, list) or not 1 <= len(values) <= 50:
                raise ValueError("An in filter requires one to 50 scalar values.")
            for value in values:
                _literal(value)
    if len(canonical(definitions).encode()) > 64_000:
        raise ValueError("Business definitions exceed their size limit.")
    return json.loads(canonical(definitions))


def _gaps(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Retain at most 32 explicit context gaps.")
    for gap in value:
        if not isinstance(gap, dict) or set(gap) != {"code", "description"}:
            raise ValueError("Each context gap requires code and description.")
        name(gap["code"])
        text(gap["description"], "unresolved context", maximum=2000)
    return json.loads(canonical(value))


def _measure_policy(approved):
    """Bound policy lineage and unresolved declared measures, never infer units."""
    bindings = approved["candidate"]["profile"]["bindings"]
    declared = [(binding, role, policy) for binding in bindings
                for role, policy in binding.get("measures", {}).items()]
    reasons = set()
    if not declared:
        reasons.add("measure_policies_not_declared")
    for _, _, policy in declared:
        if policy["unit"] == "unknown":
            reasons.add("measure_unit_unknown")
        if policy["aggregation"] == "unknown":
            reasons.add("measure_aggregation_unknown")
        elif policy["aggregation"] == "none":
            reasons.add("measure_aggregation_disallowed")
    for kind, role in {(binding["kind"], role) for binding, role, _ in declared}:
        selected = [binding for binding in bindings if binding["kind"] == kind]
        policies = [binding.get("measures", {}).get(role) for binding in selected]
        if any(policy is None for policy in policies):
            reasons.add("measure_policy_partial")
        known = [policy for policy in policies if policy is not None]
        if len({(p["unit"], p["currency_field"], p["aggregation"]) for p in known}) > 1:
            reasons.add("measure_policy_conflict")
    descriptions = {
        "measure_policies_not_declared": "No measure policies are declared in the approved source mapping. Model-proposed sums require explicit unit and aggregation review; record counts remain available.",
        "measure_unit_unknown": "At least one declared measure has an unknown unit. Confirm its unit before proposing a sum; source types and samples do not establish units.",
        "measure_aggregation_unknown": "At least one declared measure has unknown aggregation permission. Confirm whether it can be summed before proposing a sum.",
        "measure_aggregation_disallowed": "At least one declared measure explicitly disallows sums. Retain that restriction; record counts do not sum the measure.",
        "measure_policy_partial": "A declared measure role lacks a policy on another binding of the same kind. A sum requires a compatible reviewed policy on every selected binding.",
        "measure_policy_conflict": "Bindings of the same kind declare incompatible unit, currency or aggregation policies for a measure role. Resolve the policies before proposing a sum across them.",
    }
    return {"origin": "approved_profile", "profile_approval": approved["approval_sha256"],
            "approved_snapshot_id": approved["candidate"]["snapshot_id"],
            "model_sums_require_policy": True,
            "gaps": [{"code": reason, "description": descriptions[reason]} for reason in sorted(reasons)]}


def inspect_context(workspace, profile_name, snapshot_id, *, max_samples=3):
    """Bounded model input from validated committed DuckDB evidence only."""
    if type(max_samples) is not int or not 0 <= max_samples <= 5:
        raise ValueError("Use zero to five samples per binding.")
    with readable_profile(workspace, profile_name, snapshot_id) as (_, snapshot, approved, receipt, payload):
        result = {"profile": approved["candidate"]["profile"],
                  "schema_contract": approved["candidate"]["schema_contract"],
                  "profile_approval": approved["approval_sha256"], "snapshot_id": snapshot_id,
                  "measure_policy": _measure_policy(approved),
                  "materialization_id": receipt["materialization_id"],
                  "identity_policy": {"status": "reviewed_explicit_source_links", "version": receipt["identity"]["version"],
                                      "mapping_sha256": receipt["identity"]["sha256"],
                                      "interpretation": "customer_ref identifies a key in the reviewed workspace identity map. A record ID reference can be explicitly mapped to a customer; it is not a missing join. Resolved links are already reviewed. Conflicting and unresolved links remain unattributed. Names and domains never establish identity."},
                  "coverage": payload["coverage"], "source_observed_at": payload["source_observed_at"],
                  "upstream_completeness": snapshot["coverage"]["upstream_completeness"], "samples": []}
        counts = {}
        for record in payload["records"]:
            key = (record["connection"], record["entity"])
            if counts.get(key, 0) >= max_samples:
                continue
            # Long text remains in the workspace; explicitly mark omitted values.
            fields, omitted = {}, []
            for role, value in record["fields"].items():
                if len(canonical(value).encode()) <= 2000:
                    fields[role] = value
                else:
                    omitted.append(role)
            result["samples"].append({"connection": key[0], "entity": key[1], "kind": record["kind"],
                                      "evidence_id": record["evidence_id"], "link_status": record["link_status"],
                                      "fields": fields, "omitted_fields": omitted})
            counts[key] = counts.get(key, 0) + 1
        if len(canonical(result).encode()) > 80_000:
            raise ValueError("Context inspection exceeds its input budget; reduce samples or profile scope.")
        return result


def propose_context(workspace, profile_name, snapshot_id, definitions, *, gaps=None, origin="operator_authored", stages=None,
                    expected_profile_approval=None, expected_materialization_id=None):
    name(profile_name)
    if origin not in ("operator_authored", "model_agent", "scripted_agent_test"):
        raise ValueError("Unknown context proposal origin.")
    gaps = _gaps([] if gaps is None else gaps)
    stages = [] if stages is None else stages
    if not isinstance(stages, list) or len(stages) > 8 or len(canonical(stages).encode()) > 64_000:
        raise ValueError("Context stage evidence exceeds its limit.")
    with readable_profile(workspace, profile_name, snapshot_id) as (conn, _, approved, receipt, _):
        if ((expected_profile_approval is not None and expected_profile_approval != approved["approval_sha256"])
                or (expected_materialization_id is not None and expected_materialization_id != receipt["materialization_id"])):
            raise ValueError("Customer context inputs changed after inspection; bootstrap again.")
        _ensure(conn)
        definitions = validate_definitions(definitions, approved, require_measure_policy=origin != "operator_authored")
        previous = _latest(conn, profile_name)
        candidate = {"spec_version": SPEC, "profile": profile_name,
                     "profile_approval": approved["approval_sha256"], "snapshot_id": snapshot_id,
                     "measure_policy": _measure_policy(approved),
                     "identity": receipt["identity"], "materialization_id": receipt["materialization_id"],
                     "version": previous["version"] + 1 if previous else 1,
                     "previous_context_id": previous["context_id"] if previous else None,
                     "definitions": definitions, "definition_sha256": sha256(definitions),
                     "gaps": gaps, "origin": origin, "stages": stages, "created_at": _now()}
        proposal_id = sha256(candidate)
        conn.execute("INSERT INTO _contextbase.customer360_context_candidates VALUES (?,?)", [proposal_id, canonical(candidate)])
        return {"proposal_id": proposal_id, "approval_sha256": proposal_id, "status": "candidate", **candidate}


def approve_context(workspace, proposal_id, approval_sha256, reviewer):
    if not isinstance(proposal_id, str) or len(proposal_id) != 64 or proposal_id != approval_sha256:
        raise ValueError("Review the exact customer context proposal hash.")
    text(reviewer, "context reviewer", maximum=128)
    with workspace._connect() as conn:
        if not _exists(conn, "customer360_context_candidates"):
            raise ValueError("No customer context proposals exist.")
        row = conn.execute("SELECT payload FROM _contextbase.customer360_context_candidates WHERE proposal_id=?", [proposal_id]).fetchone()
    if row is None:
        raise ValueError("Unknown customer context proposal.")
    candidate = json.loads(row[0])
    if sha256(candidate) != proposal_id:
        raise ValueError("Customer context proposal changed; propose and review again.")
    with readable_profile(workspace, candidate["profile"], candidate["snapshot_id"]) as (conn, _, approved, receipt, _):
        previous = _latest(conn, candidate["profile"])
        if (previous["context_id"] if previous else None) != candidate["previous_context_id"]:
            raise ValueError("Customer context changed after proposal; review again.")
        if (approved["approval_sha256"] != candidate["profile_approval"] or receipt["identity"] != candidate["identity"]
                or receipt["materialization_id"] != candidate["materialization_id"]):
            raise ValueError("Source mappings or customer evidence changed after proposal; review again.")
        validate_definitions(candidate["definitions"], approved,
                             require_measure_policy=candidate["origin"] != "operator_authored")
        if "measure_policy" in candidate and candidate["measure_policy"] != _measure_policy(approved):
            raise ValueError("Reviewed measure policy lineage changed after proposal.")
        value = {**candidate, "proposal_id": proposal_id, "reviewer": reviewer, "reviewed_at": _now(),
                 "approval_kind": "local_operator_business_review"}
        value["context_id"] = sha256(value)
        conn.execute("INSERT INTO _contextbase.customer360_context_approvals VALUES (?,?,?,?)",
                     [candidate["profile"], candidate["version"], value["context_id"], canonical(value)])
        return value


def current_context_in_connection(conn, profile_name, snapshot_id, *, approved_profile):
    """Caller must hold a validated readable_profile/customer transaction."""
    name(profile_name)
    context = _latest(conn, profile_name)
    if context is None:
        raise ValueError("Review business context before asking customer questions.")
    if context["profile_approval"] != approved_profile["approval_sha256"]:
        raise ValueError("Reviewed source mappings changed; propose and review dependent business context again.")
    validate_definitions(context["definitions"], approved_profile)
    if context["definition_sha256"] != sha256(context["definitions"]):
        raise ValueError("Reviewed business definitions changed.")
    # Keep the immutable approval identity separate from the current source view.
    return {**context, "approved_snapshot_id": context["snapshot_id"], "snapshot_id": snapshot_id}


def current_context(workspace, profile_name, snapshot_id):
    with readable_profile(workspace, profile_name, snapshot_id) as (conn, _, approved, _, _):
        return current_context_in_connection(conn, profile_name, snapshot_id, approved_profile=approved)


def export_template(workspace, profile_name, snapshot_id, output):
    """Export structure only; literal rules need local rebinding and review.

Descriptions, samples, customer links, source text, rule literals, gaps and
agent traces never enter this template. It cannot impersonate an approval.
"""
    context = current_context(workspace, profile_name, snapshot_id)
    definitions = [{"name": d["name"], "kind": d["kind"], "operation": d["operation"],
                    "field": d["field"], "unit": d["unit"], "currency_field": d["currency_field"],
                    "filters": [{"field": f["field"], "operator": f["operator"], "requires_local_value": True}
                                for f in d["filters"]]} for d in context["definitions"]]
    template = {"spec_version": "oriel.customer360.context-template/v1", "status": "requires_local_binding_and_review",
                "source_definition_sha256": context["definition_sha256"], "definitions": definitions}
    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(template, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return {"template_sha256": sha256(template), "status": template["status"], "output": str(target)}
