"""Offline contracts for a future explicitly authorized live collector.

This module performs no I/O and grants no authority. A structurally consistent
operator declaration is not authenticated vendor evidence. In particular, this
helper never completes the live acceptance gate. Callers must independently
review proof artifacts and run the real product/connector workflow.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit


SPEC = "oriel.customer360.live-access/v1"
CAPABILITY_SPEC = "oriel.customer360.live-capability/v1"
RECEIPT_SPEC = "oriel.customer360.vendor-extraction/v1"
REFERENCE_SPEC = "oriel.customer360.vendor-reference/v1"
CONTROLS = frozenset({"endpoint", "principal", "read_only", "resources", "fields",
                      "history", "record_ids", "requests", "pages", "rows", "seconds"})
LIMITATIONS = [
    "Origin, authorization and artifact authenticity require independent human/collector verification; hashes do not authenticate them.",
    "The caller supplies the separately reviewed adapter allowlist; the access contract cannot approve its own adapter.",
    "Matching scoped rows does not establish source completeness outside the declared permissions, fields and history window.",
    "This checker does not execute extraction, approve model disclosure, or prove Customer360 mapping/context/answer and third-connector integration.",
]


class LiveContractError(ValueError):
    """Fail closed without echoing submitted records, references or credentials."""


def _require(condition, code):
    if not condition:
        raise LiveContractError(code)


def _shape(value, fields, code="unexpected_or_missing_fields"):
    _require(type(value) is dict and set(value) == set(fields.split()), code)


def _checked(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LiveContractError:
            raise
        except (TypeError, KeyError, ValueError, OverflowError, InvalidOperation):
            raise LiveContractError("malformed_contract_or_evidence") from None
    return call


def _text(value):
    _require(type(value) is str and 0 < len(value) <= 512 and value.strip() == value
             and all(ord(c) >= 32 for c in value), "invalid_bounded_text")
    return value


def _name(value):
    _require(type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value), "invalid_identifier")
    return value


def _hash(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def _integer(value, minimum=0, maximum=1_000_000):
    _require(type(value) is int and minimum <= value <= maximum, "invalid_integer_bound")
    return value


def _time(value):
    _text(value)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LiveContractError("invalid_utc_timestamp") from None
    _require(result.tzinfo is not None and result.utcoffset().total_seconds() == 0,
             "utc_timestamp_required")
    return result.astimezone(timezone.utc)


def _unique(values, *, allow_empty=False, validator=_text):
    _require(type(values) is list and (allow_empty or bool(values)) and len(values) <= 100_000,
             "invalid_list")
    for value in values:
        validator(value)
    _require(len(values) == len(set(values)), "duplicate_list_value")


def _json(value, depth=0, budget=None):
    # Floats lose exact decimals. Normalize finite source decimals to strings
    # in both independently acquired projections before calling this checker.
    _require(depth <= 12, "json_depth_exceeded")
    budget = budget if budget is not None else {"nodes": 0, "bytes": 0}
    budget["nodes"] += 1
    _require(budget["nodes"] <= 200_000, "json_aggregate_node_bound_exceeded")
    if value is None or type(value) in (str, bool, int):
        if type(value) is str:
            _require(len(value) <= 200_000, "cell_size_exceeded")
            budget["bytes"] += len(value.encode("utf-8"))
        elif type(value) is int:
            _require(len(str(value)) <= 200, "integer_precision_bound_exceeded")
            budget["bytes"] += len(str(value))
        else:
            budget["bytes"] += 5
        _require(budget["bytes"] <= 16 * 1024 * 1024, "json_aggregate_byte_bound_exceeded")
        return
    if type(value) is list:
        _require(len(value) <= 100_000, "json_size_exceeded")
        for item in value:
            _json(item, depth + 1, budget)
        return
    if type(value) is dict:
        _require(len(value) <= 1000 and all(type(k) is str for k in value), "invalid_json_object")
        for key, item in value.items():
            _json(key, depth + 1, budget)
            _json(item, depth + 1, budget)
        return
    raise LiveContractError("json_requires_exact_values_no_floats")


def digest(value):
    """Canonical ASCII JSON SHA256; no float coercion or custom encoders."""
    _json(value)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def subject(value, *excluded):
    """Digest a known record before its proof/self-hash fields are attached."""
    return digest({key: item for key, item in value.items() if key not in excluded})


def _sealed(value, key):
    _hash(value[key])
    _require(value[key] == subject(value, key), "artifact_hash_mismatch")


def _endpoint(value):
    _text(value)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise LiveContractError("invalid_vendor_origin") from None
    _require(parsed.scheme == "https" and parsed.hostname and parsed.username is None
             and parsed.password is None and not parsed.query and not parsed.fragment
             and not parsed.path and parsed.hostname not in {"localhost", "localhost.localdomain"},
             "approved_https_origin_required")
    _require(not parsed.hostname.endswith((".invalid", ".test", ".example", ".localhost"))
             and not any(parsed.hostname == name or parsed.hostname.endswith("." + name)
                         for name in ("example.com", "example.org", "example.net")), "fixture_origin_not_vendor_proof")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    _require(address is None or address.is_global, "fixture_or_private_ip_not_vendor_proof")


def _proof(evidence, key, *, kind, origin, expected_subject):
    _hash(key)
    _require(type(evidence) is dict and key in evidence, "proof_artifact_missing")
    item = evidence[key]
    _shape(item, "kind origin subject_sha256 recorded_at producer_ref artifact_sha256 derived_from_sha256")
    _require(digest(item) == key, "proof_hash_mismatch")
    _require(item["kind"] == kind and item["origin"] == origin, "proof_kind_or_origin_mismatch")
    _require(item["subject_sha256"] == expected_subject, "proof_subject_mismatch")
    _hash(item["artifact_sha256"])
    _text(item["producer_ref"])
    _unique(item["derived_from_sha256"], allow_empty=True, validator=_hash)
    _time(item["recorded_at"])
    return item


def _resource(resource):
    _shape(resource, "name fields id_field history record_ids numeric_fields currency_field text_fields")
    _name(resource["name"])
    _unique(resource["fields"])
    _text(resource["id_field"])
    _require(resource["id_field"] in resource["fields"], "id_field_not_selected")
    _shape(resource["history"], "field start end")
    history = resource["history"]
    _require(history["field"] in resource["fields"] and _time(history["start"]) < _time(history["end"]),
             "invalid_history_scope")
    if resource["record_ids"] is not None:
        _unique(resource["record_ids"])
    for key in ("numeric_fields", "text_fields"):
        _unique(resource[key], allow_empty=True)
        _require(set(resource[key]) <= set(resource["fields"]), "reconciliation_field_not_selected")
    currency = resource["currency_field"]
    _require(currency is None or (currency in resource["fields"] and resource["numeric_fields"]),
             "invalid_currency_field")
    _require(not set(resource["numeric_fields"]) & set(resource["text_fields"]), "conflicting_field_roles")


def _permissions(value, resources):
    _shape(value, "resources fields exclusions")
    _unique(value["resources"], validator=_name)
    expected = {row["name"]: row["fields"] for row in resources}
    _require(set(value["resources"]) == set(expected) and type(value["fields"]) is dict
             and set(value["fields"]) == set(expected), "permission_resource_mismatch")
    for key, fields in value["fields"].items():
        _unique(fields)
        _require(set(fields) == set(expected[key]), "permission_field_mismatch")
    exclusions = value["exclusions"]
    _require(type(exclusions) is list and len(exclusions) <= 1000, "invalid_exclusions")
    seen = set()
    for item in exclusions:
        _shape(item, "resource field reason")
        _name(item["resource"])
        _text(item["reason"])
        _require(item["field"] is None or type(item["field"]) is str, "invalid_exclusion_field")
        if item["field"] is not None:
            _text(item["field"])
        identity = (item["resource"], item["field"])
        _require(identity not in seen, "duplicate_exclusion")
        seen.add(identity)
        _require(item["resource"] not in expected or (item["field"] is not None
                 and item["field"] not in expected[item["resource"]]), "selected_scope_is_excluded")


@_checked
def validate_contract(contract, *, adapter_capabilities, evidence):
    """Validate declarations/proofs, without executing or authorizing any call.

    ``adapter_capabilities`` is a separately reviewed allowlist keyed by its
    canonical capability hash, never an adapter chosen/approved by the contract.
    ``evidence`` maps proof-envelope hashes to the exact envelopes. Their
    artifact_sha256 identifies independently reviewed private artifacts, whose
    actual existence/authenticity this pure helper cannot establish.
    """
    _shape(contract, "spec created_at authorization initial_sources additional_source sources max_evidence_age_seconds max_reference_skew_seconds contract_sha256")
    _require(contract["spec"] == SPEC, "unsupported_contract_spec")
    created = _time(contract["created_at"])
    _integer(contract["max_evidence_age_seconds"], 1, 604800)
    _integer(contract["max_reference_skew_seconds"], 0, 3600)
    auth = contract["authorization"]
    _shape(auth, "reference approved_by approved_at expires_at source_ids source_scope_sha256 read_only model_data_allowed evidence_sha256")
    for key in ("reference", "approved_by"):
        _text(auth[key])
    _require(auth["read_only"] is True and auth["model_data_allowed"] is False,
             "read_only_separate_model_consent_required")
    _require(_time(auth["approved_at"]) <= created < _time(auth["expires_at"]), "authorization_not_current")
    _unique(auth["source_ids"], validator=_name)
    _require(type(auth["source_scope_sha256"]) is dict and set(auth["source_scope_sha256"]) == set(auth["source_ids"]),
             "authorization_scope_commitment_missing")
    for value in auth["source_scope_sha256"].values():
        _hash(value)
    authorization = _proof(evidence, auth["evidence_sha256"], kind="authorization", origin="operator_assertion",
                           expected_subject=subject(auth, "evidence_sha256"))
    _require(_time(auth["approved_at"]) <= _time(authorization["recorded_at"]) <= created,
             "authorization_proof_chronology")
    _unique(contract["initial_sources"], validator=_name)
    _require(len(contract["initial_sources"]) == 2, "exactly_two_initial_sources_required")
    _name(contract["additional_source"])
    _require(contract["additional_source"] not in contract["initial_sources"], "additional_source_not_distinct")
    sources = contract["sources"]
    _require(type(sources) is list and len(sources) == 3, "exactly_three_sources_required")
    _require(type(adapter_capabilities) is dict, "reviewed_adapter_allowlist_required")
    source_ids, connectors = [], []
    for source in sources:
        _shape(source, "source_id connector endpoint tenant_ref principal_ref capability_sha256 resources permissions limits refresh_mode enforcement_sha256")
        source_ids.append(_name(source["source_id"]))
        _require(auth["source_scope_sha256"].get(source["source_id"]) == subject(source, "enforcement_sha256"),
                 "authorization_scope_mismatch")
        connectors.append(_name(source["connector"]))
        _endpoint(source["endpoint"])
        for key in ("tenant_ref", "principal_ref"):
            _text(source[key])
        _require(source["refresh_mode"] == "fresh_replace", "durable_incremental_not_supported_by_this_contract")
        _shape(source["limits"], "max_requests max_pages max_rows max_seconds")
        for key, limit in source["limits"].items():
            _integer(limit, 1, 3600 if key == "max_seconds" else 100_000)
        resources = source["resources"]
        _require(type(resources) is list and 1 <= len(resources) <= 64, "invalid_resource_scope")
        for resource in resources:
            _resource(resource)
        _require(len({r["name"] for r in resources}) == len(resources), "duplicate_resource_scope")
        _permissions(source["permissions"], resources)
        capability_key = _hash(source["capability_sha256"])
        _require(capability_key in adapter_capabilities, "adapter_not_allowlisted")
        capability = adapter_capabilities[capability_key]
        _shape(capability, "spec connector implementation_sha256 origins resources controls refresh_modes reviewed_by reviewed_at evidence_sha256")
        _require(digest(capability) == capability_key and capability["spec"] == CAPABILITY_SPEC
                 and capability["connector"] == source["connector"], "adapter_capability_mismatch")
        _hash(capability["implementation_sha256"])
        _unique(capability["origins"], validator=_endpoint)
        _require(source["endpoint"] in capability["origins"], "origin_not_allowlisted")
        _require(type(capability["resources"]) is dict, "capability_resources_required")
        for name, fields in capability["resources"].items():
            _name(name)
            _unique(fields)
        for resource in resources:
            _require(resource["name"] in capability["resources"] and set(resource["fields"]) <=
                     set(capability["resources"][resource["name"]]), "resource_or_field_not_supported")
        _require(type(capability["controls"]) is dict and set(capability["controls"]) == CONTROLS
                 and all(v in {"adapter_enforced", "vendor_permission_enforced"}
                         for v in capability["controls"].values()), "scope_enforcement_missing")
        _require(all(capability["controls"][key] == "adapter_enforced" for key in
                     {"endpoint", "resources", "history", "record_ids", "requests", "pages", "rows", "seconds"}),
                 "runtime_bounds_require_adapter_enforcement")
        _unique(capability["refresh_modes"])
        _require(capability["refresh_modes"] == ["fresh_replace"], "unproven_incremental_capability")
        _text(capability["reviewed_by"])
        _require(_time(capability["reviewed_at"]) <= created, "adapter_review_after_freeze")
        proof = _proof(evidence, capability["evidence_sha256"], kind="capability_review",
                       origin="controlled_runtime_verification", expected_subject=subject(capability, "evidence_sha256"))
        _require(_time(proof["recorded_at"]) <= _time(capability["reviewed_at"]), "capability_proof_chronology")
        enforcement = _proof(evidence, source["enforcement_sha256"], kind="scope_enforcement",
                             origin="authenticated_vendor", expected_subject=subject(source, "enforcement_sha256"))
        _require(_time(auth["approved_at"]) <= _time(enforcement["recorded_at"]) <= created,
                 "scope_enforcement_chronology")
    expected_ids = set(contract["initial_sources"]) | {contract["additional_source"]}
    _require(len(set(source_ids)) == 3 and set(source_ids) == expected_ids == set(auth["source_ids"]),
             "authorization_source_coverage_mismatch")
    _require(len(set(connectors)) == 3, "three_distinct_connector_types_required")
    _sealed(contract, "contract_sha256")
    return copy.deepcopy(contract)


def _decimal(value):
    _require(type(value) in (int, str), "numeric_value_missing_or_inexact")
    _require(len(str(value)) <= 200, "numeric_precision_bound_exceeded")
    if type(value) is str:
        _require(re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) and len(value) <= 200,
                 "invalid_exact_decimal")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise LiveContractError("invalid_exact_decimal") from None
    _require(number.is_finite(), "invalid_exact_decimal")
    return number


def _rows(rows, source):
    _require(type(rows) is list and len(rows) <= source["limits"]["max_rows"], "row_limit_exceeded")
    scopes = {resource["name"]: resource for resource in source["resources"]}
    grouped = {name: {} for name in scopes}
    totals = {name: {} for name in scopes}
    for row in rows:
        _shape(row, "resource id values")
        _require(row["resource"] in scopes, "undeclared_resource")
        scope = scopes[row["resource"]]
        identifier = _text(row["id"])
        _require(identifier not in grouped[row["resource"]], "duplicate_record_id")
        values = row["values"]
        _require(type(values) is dict and set(values) == set(scope["fields"]), "observed_field_coverage_mismatch")
        _json(values)
        _require(values[scope["id_field"]] == identifier, "source_id_mismatch")
        _require(scope["record_ids"] is None or identifier in scope["record_ids"], "record_outside_authorized_scope")
        history = scope["history"]
        _require(_time(history["start"]) <= _time(values[history["field"]]) < _time(history["end"]),
                 "record_outside_history_scope")
        for field in scope["text_fields"]:
            _require(type(values[field]) is str, "selected_text_unavailable")
        currency = None
        if scope["currency_field"] is not None:
            currency = values[scope["currency_field"]]
            _require(type(currency) is str and re.fullmatch(r"[A-Z]{3}", currency), "currency_missing_or_invalid")
        for field in scope["numeric_fields"]:
            key = (field, currency)
            # 200 input digits, up to 200 fractional places and <=100000 rows
            # require at most 406 digits. Never use the process's default 28.
            with localcontext() as context:
                context.prec = 500
                totals[row["resource"]][key] = totals[row["resource"]].get(key, Decimal(0)) + _decimal(values[field])
        grouped[row["resource"]][identifier] = digest(values)
    summary = {name: {"records": len(grouped[name]), "totals": [
        {"field": field, "currency": currency, "value": format(value, "f")}
        for (field, currency), value in sorted(totals[name].items(), key=lambda pair: repr(pair[0]))]}
        for name in scopes}
    return grouped, summary


def _coverage(value, source, groups, started, finished, observed):
    _require(type(value) is list and len(value) == len(source["resources"]), "resource_coverage_missing")
    scopes = {row["name"]: row for row in source["resources"]}
    seen = set()
    times = []
    for row in value:
        _shape(row, "resource fields row_count complete source_observed_at")
        _require(row["resource"] in scopes and row["resource"] not in seen, "resource_coverage_mismatch")
        seen.add(row["resource"])
        _unique(row["fields"])
        _require(set(row["fields"]) == set(scopes[row["resource"]]["fields"])
                 and row["complete"] is True, "partial_resource_coverage")
        _integer(row["row_count"])
        _require(row["row_count"] == len(groups[row["resource"]]), "resource_count_mismatch")
        when = _time(row["source_observed_at"])
        _require(started <= when <= finished, "resource_observation_chronology")
        times.append(when)
    # Overall freshness uses the oldest resource observation, not the newest.
    _require(observed == min(times), "oldest_resource_observation_required")


@_checked
def reconcile_receipts(contract, receipts, references, *, adapter_capabilities, evidence, checked_at):
    """Reconcile six declared live extracts to independently acquired rows.

    Returns a sanitized conditional report. Does not establish source origin,
    consent, completeness, adapter behavior or product success by assertion.
    Missing, synthetic, partial, expired or inconsistent evidence raises.
    """
    contract = validate_contract(contract, adapter_capabilities=adapter_capabilities, evidence=evidence)
    checked = _time(checked_at)
    _require(_time(contract["created_at"]) <= checked <= _time(contract["authorization"]["expires_at"]),
             "verification_outside_authorization")
    sources = {source["source_id"]: source for source in contract["sources"]}
    _require(type(receipts) is list and type(references) is list and len(receipts) == len(references) == 6,
             "initial_and_refreshed_receipts_required_for_each_source")
    expected = {(source, phase) for source in sources for phase in ("initial", "refreshed")}
    extracts, refs = {}, {}
    reserved = {contract["authorization"]["evidence_sha256"]}
    for source in sources.values():
        reserved.add(source["enforcement_sha256"])
        reserved.add(adapter_capabilities[source["capability_sha256"]]["evidence_sha256"])
    used_proofs = set(reserved)
    used_artifacts = {evidence[key]["artifact_sha256"] for key in reserved}
    for row, extraction in [(row, True) for row in receipts] + [(row, False) for row in references]:
        common = "spec source_id phase contract_sha256 scope_sha256 origin started_at finished_at source_observed_at permissions resource_coverage rows evidence_sha256"
        extra = ("capability_sha256 adapter_sha256 endpoint tenant_ref principal_ref refresh_mode previous_receipt_sha256 "
                 "request_count page_count complete status receipt_sha256") if extraction else (
                 "method observer_ref independent_of_extractor derived_from_sha256 reference_sha256")
        _shape(row, common + " " + extra)
        _require(row["spec"] == (RECEIPT_SPEC if extraction else REFERENCE_SPEC)
                 and row["origin"] == "authenticated_vendor", "actual_vendor_receipt_required")
        _name(row["source_id"])
        _require(row["source_id"] in sources and row["phase"] in {"initial", "refreshed"}, "undeclared_source_or_phase")
        key = (row["source_id"], row["phase"])
        target = extracts if extraction else refs
        _require(key not in target, "duplicate_source_phase")
        source = sources[row["source_id"]]
        _require(row["contract_sha256"] == contract["contract_sha256"]
                 and row["scope_sha256"] == subject(source, "enforcement_sha256"), "receipt_scope_mismatch")
        started, finished, observed = (_time(row[name]) for name in ("started_at", "finished_at", "source_observed_at"))
        _require(_time(contract["created_at"]) <= started <= observed <= finished <= checked,
                 "observation_chronology_invalid")
        _require((checked - observed).total_seconds() <= contract["max_evidence_age_seconds"], "source_observation_stale")
        _permissions(row["permissions"], source["resources"])
        _require(row["permissions"] == source["permissions"], "permission_or_exclusion_drift")
        groups, summary = _rows(row["rows"], source)
        _coverage(row["resource_coverage"], source, groups, started, finished, observed)
        hash_key = "receipt_sha256" if extraction else "reference_sha256"
        proof = _proof(evidence, row["evidence_sha256"], kind="extraction" if extraction else "reference",
                       origin="authenticated_vendor", expected_subject=subject(row, "evidence_sha256", hash_key))
        _require(finished <= _time(proof["recorded_at"]) <= checked, "run_proof_chronology")
        _require(row["evidence_sha256"] not in used_proofs and proof["artifact_sha256"] not in used_artifacts,
                 "invocation_or_reference_artifact_reused")
        used_proofs.add(row["evidence_sha256"])
        used_artifacts.add(proof["artifact_sha256"])
        if extraction:
            capability = adapter_capabilities[source["capability_sha256"]]
            _require(row["capability_sha256"] == source["capability_sha256"]
                     and row["adapter_sha256"] == capability["implementation_sha256"], "executed_adapter_mismatch")
            for field in ("endpoint", "tenant_ref", "principal_ref", "refresh_mode"):
                _require(row[field] == source[field], "execution_authority_mismatch")
            _require(row["complete"] is True and row["status"] == "completed", "partial_or_failed_extraction")
            _integer(row["request_count"], len(source["resources"]), source["limits"]["max_requests"])
            _integer(row["page_count"], len(source["resources"]), source["limits"]["max_pages"])
            _require(row["page_count"] <= row["request_count"], "invalid_request_accounting")
            _require(0 < (finished - started).total_seconds() <= source["limits"]["max_seconds"], "extraction_time_bound_exceeded")
        else:
            _require(row["method"] in {"vendor_export", "vendor_readonly_query", "operator_source_readback"}
                     and row["independent_of_extractor"] is True, "independent_source_reference_required")
            _text(row["observer_ref"])
            _unique(row["derived_from_sha256"], allow_empty=True, validator=_hash)
            # v1 accepts only directly acquired references. Derived references
            # need a future explicit lineage verifier; an asserted independence
            # flag does not override provenance pointing back to an extractor.
            _require(not row["derived_from_sha256"] and not proof["derived_from_sha256"], "derived_reference_not_independent")
        _sealed(row, hash_key)
        target[key] = (row, proof, groups, summary)
    _require(set(extracts) == set(refs) == expected, "source_phase_coverage_incomplete")
    outcomes, change_count = [], 0
    for source_id, phase in sorted(expected):
        row, proof, groups, summary = extracts[(source_id, phase)]
        ref, ref_proof, ref_groups, _ = refs[(source_id, phase)]
        _require(proof["producer_ref"] != ref_proof["producer_ref"] and row["evidence_sha256"] != ref["evidence_sha256"],
                 "reference_reuses_extractor")
        _require(ref["observer_ref"] == ref_proof["producer_ref"], "reference_observer_mismatch")
        _require(abs((_time(row["source_observed_at"]) - _time(ref["source_observed_at"])).total_seconds())
                 <= contract["max_reference_skew_seconds"], "reference_observation_skew")
        reference_times = {item["resource"]: _time(item["source_observed_at"]) for item in ref["resource_coverage"]}
        _require(all(abs((_time(item["source_observed_at"]) - reference_times[item["resource"]]).total_seconds())
                     <= contract["max_reference_skew_seconds"] for item in row["resource_coverage"]),
                 "resource_reference_observation_skew")
        _require(groups == ref_groups, "independent_record_reconciliation_failed")
        changes = None
        if phase == "initial":
            _require(row["previous_receipt_sha256"] is None, "initial_receipt_has_predecessor")
        else:
            before, _, previous_groups, _ = extracts[(source_id, "initial")]
            previous_ref = refs[(source_id, "initial")][0]
            _require(row["previous_receipt_sha256"] == before["receipt_sha256"], "refresh_chain_broken")
            _require(max(_time(before["finished_at"]), _time(previous_ref["finished_at"]))
                     < min(_time(row["started_at"]), _time(ref["started_at"])), "refresh_precedes_completed_initial_observation")
            changes = {}
            for resource, values in groups.items():
                old = previous_groups[resource]
                change = {"added": len(set(values) - set(old)), "deleted": len(set(old) - set(values)),
                          "updated": sum(values[key] != old[key] for key in set(values) & set(old))}
                changes[resource] = change
                history_field = next(item["history"]["field"] for item in sources[source_id]["resources"] if item["name"] == resource)
                def facts(records):
                    return {item["id"]: digest({key: value for key, value in item["values"].items() if key != history_field})
                            for item in records if item["resource"] == resource}
                old_facts, new_facts = facts(before["rows"]), facts(row["rows"])
                # A changed polling/update timestamp alone is not the changed
                # customer fact required by the refresh demonstration.
                change_count += change["added"] + change["deleted"] + sum(
                    new_facts[key] != old_facts[key] for key in set(new_facts) & set(old_facts))
        outcomes.append({"source_id": source_id, "phase": phase, "receipt_sha256": row["receipt_sha256"],
                         "reference_sha256": ref["reference_sha256"], "source_observed_at": row["source_observed_at"],
                         "resources": summary, "changes": changes})
    _require(change_count > 0, "changed_source_fact_not_demonstrated")
    return {"spec": "oriel.customer360.live-reconciliation-report/v1", "contract_sha256": contract["contract_sha256"],
            "checked_at": checked_at, "conditional_reconciliation_passed": True, "live_gate_passed": False,
            "execution_authorized": False, "origin_authentication": "operator_assertion_only",
            "network_calls": 0, "model_calls": 0, "outcomes": outcomes, "limitations": list(LIMITATIONS)}
