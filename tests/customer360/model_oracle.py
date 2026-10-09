"""Independent, test-only semantic review for the synthetic real-model smoke.

Never put this module, expected.json, or returned oracle decisions in model
inputs. Expected populations come from the stdlib fixture oracle and source JSON,
not product SQL. Content hashes establish consistency, not source authenticity or
human approval. This helper does not execute models, vendors, or product code.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import importlib.util
from itertools import combinations
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / "examples" / "customer-360"
PROFILES = {"billing-support", "salesforce-gong"}
PHASES = {"initial", "refreshed"}
BASIS = ("workspace_id", "profile", "customer_id", "snapshot_id", "snapshot_manifest_sha256",
         "profile_approval", "materialization_id", "materialization_receipt_sha256", "context_id",
         "definition_sha256", "identity_sha256")
DEF_FIELDS = {"name", "description", "kind", "operation", "field", "filters", "unit", "currency_field"}
# Domains observed across both authored fixture phases. Names/descriptions are
# provisional; only exact count/currency-sum shapes and these typed partitions
# receive synthetic semantic review. No-filter and full-domain filters remain
# distinct meanings, since future source states could distinguish them.
STATUS_DOMAINS = {
    "billing-support": {"billing": ("status", ("open", "paid", "void")),
                        "support": ("status", ("open", "closed"))},
    "salesforce-gong": {"commercial": ("is_closed", (False, True))},
}


def _meaning_rules():
    rules = {profile: {"all_customer_records": ("customer", "count", None, None, "records", False)}
             for profile in PROFILES}
    rules["salesforce-gong"]["all_conversation_records"] = ("conversation", "count", None, None, "records", False)
    for profile, kinds in STATUS_DOMAINS.items():
        for kind, (field, domain) in kinds.items():
            populations = [None] + [subset for size in range(1, len(domain) + 1) for subset in combinations(domain, size)]
            for subset in populations:
                label = "all" if subset is None else "_".join(
                    "closed" if value is True else "open" if value is False else value for value in subset)
                for operation in (("count",) if kind == "support" else ("count", "sum")):
                    noun = {"billing": "invoice", "commercial": "opportunity", "support": "support"}[kind]
                    if operation == "count":
                        semantic, unit = label + "_" + noun + "_records", "records"
                    elif kind == "billing":
                        semantic = "all_remaining_invoice_minor_units" if subset is None else "remaining_" + label + "_invoice_minor_units"
                        unit = "minor_currency"
                    else:
                        semantic, unit = label + "_opportunity_currency", "currency"
                    required = operation == "sum" and subset == (("open",) if kind == "billing" else (False,))
                    rules[profile][semantic] = (kind, operation, field if subset is not None else None, subset, unit, required)
    return rules


MEANINGS = _meaning_rules()
RESOURCES = {
    "billing-support": {"customer": "stripe.customers", "billing": "stripe.invoices", "support": "intercom.conversations"},
    "salesforce-gong": {"customer": "salesforce.account", "commercial": "salesforce.opportunity", "conversation": "gong.call_transcripts"},
}


class OracleMismatch(ValueError):
    """A fixed safe code, with no customer text, expected values, or model output."""

    def __init__(self, code, review_receipt=None):
        super().__init__(code)
        self.code = code
        self.review_receipt = review_receipt


def _require(condition, code):
    if not condition:
        raise OracleMismatch(code)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError):
        raise OracleMismatch("invalid_json_value") from None


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _seal(value, key):
    _require(isinstance(value, dict) and key in value, "missing_receipt")
    _require(value[key] == _hash({k: v for k, v in value.items() if k != key}), "receipt_hash_mismatch")


def _load(profile, phase, fixture_root):
    _require(profile in PROFILES and phase in PHASES, "unsupported_fixture_scope")
    root = Path(fixture_root) if fixture_root is not None else ROOT
    spec = importlib.util.spec_from_file_location("customer360_independent_smoke_fixture", ROOT / "oracle.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    computed = oracle.all_expected(root)
    saved = json.loads((root / "expected.json").read_text())
    _require(computed == saved, "independent_fixture_expectation_changed")
    oracle.verify_citations(computed, root)
    result = computed["profiles"][profile][phase]
    manifest = json.loads((root / "manifest.json").read_text())
    files = manifest["profiles"][profile]["phases"][phase]
    records = {resource: json.loads((root / file).read_text()) for resource, file in files.items()}
    return root, result, records, _hash({"expected": computed, "manifest": manifest, "records": records})


def _status_subset(definition, field, values):
    predicates = definition["filters"]
    if not isinstance(predicates, list) or len(predicates) != 1:
        return False
    predicate = predicates[0]
    if not isinstance(predicate, dict) or set(predicate) != {"field", "operator", "value"} or predicate["field"] != field:
        return False
    selected = predicate["value"]
    if predicate["operator"] == "eq":
        selected = [selected]
    elif predicate["operator"] != "in" or not isinstance(selected, list):
        return False
    if not selected or len(selected) != len(values) or len({_canonical(item) for item in selected}) != len(selected):
        return False
    return all(any(type(item) is type(value) and item == value for value in values) for item in selected)


def _definitions(profile, definitions):
    _require(profile in PROFILES, "unsupported_fixture_scope")
    _require(isinstance(definitions, list) and 1 <= len(definitions) <= 32, "invalid_definitions")
    reviewed, seen, extras = [], set(), []
    for definition in definitions:
        _require(isinstance(definition, dict) and set(definition) == DEF_FIELDS, "invalid_definition_shape")
        name = definition["name"]
        _require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) and name not in seen, "invalid_definition_name")
        seen.add(name)
        _require(isinstance(definition["description"], str) and 0 < len(definition["description"].strip()) <= 2000, "invalid_definition_description")
        matches = []
        for semantic, (kind, operation, field, value, unit, required) in MEANINGS[profile].items():
            if (definition["kind"] == kind and definition["operation"] == operation
                    and definition["field"] == ("amount" if operation == "sum" else None)
                    and definition["unit"] == unit
                    and definition["currency_field"] == ("currency" if operation == "sum" else None)
                    and (definition["filters"] == [] if field is None else _status_subset(definition, field, value))):
                matches.append((semantic, required))
        if not matches:
            extras.append({"name": name, "definition_sha256": _hash(definition)})
            continue
        _require(len(matches) == 1, "ambiguous_semantic_definition")
        semantic, required = matches[0]
        _require(semantic not in [item["semantic"] for item in reviewed], "duplicate_semantic_definition")
        reviewed.append({"name": name, "semantic": semantic, "definition_sha256": _hash(definition), "required": required})
    code = ("required_currency_sum_missing" if sum(item["required"] for item in reviewed) != 1
            else "extra_definition_needs_review" if extras else None)
    if code:
        raise OracleMismatch(code, {"decision": "needs_review", "definition_sha256": _hash(definitions),
                                   "extra_defs_need_review": extras})
    return reviewed


def review_definitions(profile, candidate, *, fixture_root=None):
    """Review an untouched full context candidate; reject all unchecked extras.

    Names/descriptions may vary. Only the finite MEANINGS above are reviewed;
    the profile's open currency sum remains mandatory even if extras are valid.
    This is a synthetic test decision, never authorization to approve real data.
    """
    _, _, _, fixture_hash = _load(profile, "initial", fixture_root)
    _require(isinstance(candidate, dict) and candidate.get("status") == "candidate"
             and candidate.get("spec_version") == "oriel.customer360.context/v1", "invalid_candidate")
    payload = {key: value for key, value in candidate.items() if key not in {"proposal_id", "approval_sha256", "status"}}
    _require(candidate.get("proposal_id") == candidate.get("approval_sha256") == _hash(payload), "candidate_hash_mismatch")
    _require(candidate.get("definition_sha256") == _hash(candidate.get("definitions")), "definition_hash_mismatch")
    reviewed = _definitions(profile, candidate["definitions"])
    receipt = {"spec": "oriel.customer360.model-smoke-definition-review/v1", "decision": "accepted_synthetic_semantics",
               "profile": profile, "candidate_sha256": _hash(candidate), "approval_sha256": candidate["approval_sha256"],
               "definition_sha256": candidate["definition_sha256"], "reviewed_definitions": reviewed,
               "extra_defs_need_review": [], "fixture_sha256": fixture_hash, "human_approval": False,
               "display_text_review": "provisional_synthetic_only"}
    return {**receipt, "receipt_sha256": _hash(receipt)}


def _record(profile, resource, row):
    """Independent mapping of declared sample fields, not a product projection."""
    if resource == "stripe.customers":
        return row["id"], row["id"], {"label": row["name"]}
    if resource == "salesforce.account":
        return row["Id"], row["Id"], {"label": row["Name"]}
    if resource == "stripe.invoices":
        return row["id"], row["customer"], {"amount": row["amount_remaining"], "currency": row["currency"], "status": row["status"]}
    if resource == "intercom.conversations":
        return row["id"], row["id"], {"text": _canonical(row["source"]), "status": row["state"],
                                     "updates": _canonical(row["conversation_parts"]),
                                     "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if resource == "salesforce.opportunity":
        return row["Id"], row["AccountId"], {"amount": row["Amount"], "currency": row["CurrencyIsoCode"], "is_closed": row["IsClosed"], "close_date": row["CloseDate"]}
    if resource == "gong.call_transcripts":
        return row["callId"], row["callId"], {"text": _canonical(row["transcript"])}
    raise OracleMismatch("unsupported_source_resource")


def _attachment(records, row, source_names, snapshot_id=None):
    """Reconstruct only independently attributed call metadata from raw JSON."""
    call_id = row["callId"]
    found = [item for item in records["gong.extensive_calls"] if item["metaData"]["id"] == call_id]
    _require(len(found) == 1, "independent_attachment_missing_or_duplicated")
    metadata = _canonical(found[0]["metaData"])
    source = source_names["gong"]
    return {"status": "matched", "connection": source, "entity": "extensive_calls",
            "source_table": source + ".extensive_calls", "snapshot_id": snapshot_id,
            "fields": {"metadata": metadata}, "source_id": call_id,
            "source_row_sha256": _hash({"fixture_record_id": call_id, "meta_data": metadata}),
            "local_key": {"column": "call_id", "type": "VARCHAR", "value": call_id},
            "remote_key": {"column": "fixture_record_id", "type": "VARCHAR", "value": call_id},
            "customer_ref": {"connection": source, "entity": "extensive_calls",
                             "column": "fixture_record_id", "value": call_id}}


def _evidence(profile, resource, row, workspace_id, source_names, records):
    source, entity = resource.split(".")
    record_id, customer_ref, fields = _record(profile, resource, row)
    key = _hash([workspace_id, source_names[source], entity, record_id])
    content = {"fields": fields, "source_id": record_id, "customer_ref": customer_ref}
    if resource == "gong.call_transcripts":
        attachment = _attachment(records, row, source_names)
        content["attachments"] = {"call_metadata": {k: v for k, v in attachment.items() if k != "snapshot_id"}}
        content["attachment_keys_sha256"] = {"call_metadata": _hash({"type": "VARCHAR", "value": row["callId"]})}
    evidence = _hash([key, _hash(content)])
    return record_id, key, evidence, fields


def _source_time(value, encoding, meaning):
    """Independent fixture chronology: no product temporal/projection imports."""
    if encoding == "unix_seconds":
        _require(type(value) in (int, Decimal, str), "invalid_independent_source_time")
        raw = str(value)
        _require(re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?", raw) is not None,
                 "invalid_independent_source_time")
        exact = Decimal(raw)
    else:
        _require(encoding == "rfc3339" and type(value) is str, "invalid_independent_source_time")
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
        _require(match is not None and match[3] != "-00:00", "invalid_independent_source_time")
        try:
            parsed = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
            whole = calendar.timegm(parsed.astimezone(timezone.utc).utctimetuple())
        except (ValueError, OverflowError):
            raise OracleMismatch("invalid_independent_source_time") from None
        with localcontext() as ctx:
            ctx.prec = 64
            exact = Decimal(whole) + Decimal("0." + (match[2] or "0"))
    with localcontext() as ctx:
        ctx.prec = 64
        nanos = int(exact * 1_000_000_000)
    whole, fraction = divmod(nanos, 1_000_000_000)
    try:
        utc = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=whole)
    except (ValueError, OverflowError):
        raise OracleMismatch("invalid_independent_source_time") from None
    stamp = f"{utc.year:04}-{utc.month:02}-{utc.day:02}T{utc.hour:02}:{utc.minute:02}:{utc.second:02}"
    stamp += ("." + f"{fraction:09}".rstrip("0") if fraction else "") + "Z"
    number = format(exact, "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    if exact == 0:
        number = "0"
    return {"status": "known", "unix_seconds": number, "utc": stamp, "reason": None,
            "encoding": encoding, "meaning": meaning}


def _passages(profile, records, resolved, basis, source_names):
    """Enumerate full independently attributed fixture leaves, in proof order."""
    resource = RESOURCES[profile]["support" if profile == "billing-support" else "conversation"]
    attribution = resource if profile == "billing-support" else "gong.extensive_calls"
    output, ordered = [], []
    for index, row in enumerate(records[resource]):
        record_id = _record(profile, resource, row)[0]
        if (attribution, record_id) not in resolved:
            continue
        _, key, evidence, fields = _evidence(profile, resource, row, basis["workspace_id"], source_names, records)
        ordered.append((key, index, row, evidence, fields))
    for key, index, row, evidence, fields in sorted(ordered):
        record_id = _record(profile, resource, row)[0]
        source, entity = resource.split(".")
        base = {"connection": source_names[source], "entity": entity, "source_id": record_id, "evidence_id": evidence}
        if profile == "billing-support":
            leaves = [("original_message", "text", "/body", row["source"]["body"],
                       f"/{index}/source/body", row["created_at"], "record_created",
                       {**base, "source": "field", "path": "", "field": "created_at", "field_sha256": _hash(row["created_at"])})]
            for part_index, part in enumerate(row["conversation_parts"]["conversation_parts"]):
                pointer = f"/conversation_parts/{part_index}/body"
                lineage = {**base, "source": "item", "path": "/created_at", "field": "updates",
                           "field_sha256": _hash(fields["updates"]), "passage_json_pointer": pointer,
                           "json_pointer": f"/conversation_parts/{part_index}/created_at"}
                leaves.append(("support_update", "updates", pointer, part["body"],
                               f"/{index}/conversation_parts/conversation_parts/{part_index}/body",
                               part["created_at"], "passage_created", lineage))
            encoding = "unix_seconds"
        else:
            attachment = _attachment(records, row, source_names, basis["snapshot_id"])
            started = json.loads(attachment["fields"]["metadata"])["started"]
            lineage = {**base, "source": "attachment", "path": "/started", "attachment": "call_metadata",
                       "attachment_lineage": {k: v for k, v in attachment.items() if k != "fields"},
                       "field": "metadata", "field_sha256": _hash(attachment["fields"]["metadata"])}
            leaves = []
            for segment_index, segment in enumerate(row["transcript"]):
                for sentence_index, sentence in enumerate(segment["sentences"]):
                    pointer = f"/{segment_index}/sentences/{sentence_index}/text"
                    leaves.append(("call_statement", "text", pointer, sentence["text"],
                                   f"/{index}/transcript{pointer}", started, "event_started", lineage))
            encoding = "rfc3339"
        for passage_name, field, pointer, value, fixture_pointer, timestamp, meaning, lineage in leaves:
            _require(type(value) is str, "invalid_independent_passage")
            if not value.strip():
                continue
            output.append({"resource": resource, "record_id": record_id, "record_key": key, "evidence_id": evidence,
                           "passage_name": passage_name, "field": field, "json_pointer": pointer,
                           "value": value, "fixture_pointer": fixture_pointer, "raw": fields[field],
                           "time": _source_time(timestamp, encoding, meaning), "time_lineage": lineage})
    return output


def _check_temporal(answer, basis, passages, *, required):
    selections = answer.get("temporal_selections", [])
    _require(type(selections) is list and len(selections) <= 1, "invalid_latest_selection")
    _require(not required or selections, "latest_selection_required")
    expected_kind = "support" if passages[0]["resource"] == "intercom.conversations" else "conversation"
    candidate_proof = [{"evidence_id": row["evidence_id"], "passage_name": row["passage_name"],
                        "json_pointer": row["json_pointer"], "passage_sha256": _hash(row["value"]),
                        "time": row["time"], "time_lineage_sha256": _hash(row["time_lineage"])} for row in passages]
    maximum = max(Decimal(row["time"]["unix_seconds"]) for row in passages)
    latest = sorted((row for row in passages if Decimal(row["time"]["unix_seconds"]) == maximum),
                    key=lambda row: (row["evidence_id"], row["passage_name"], row["json_pointer"]))
    expected_ties = [{key: row[key] for key in ("evidence_id", "passage_name", "json_pointer")} for row in latest]
    known_quotes = {quote["quote_id"]: quote for quote in answer["quotes"]}
    for selection in selections:
        _same_basis(selection, basis, "oriel.customer360.latest-selection/v1")
        _seal(selection, "selection_id")
        _require(set(selection) == set(BASIS) | {"spec_version", "kind", "limit", "status", "coverage",
                 "candidates_sha256", "latest_time", "ties", "ties_count", "quotes", "selection_id"}, "invalid_latest_selection")
        _require(type(selection["limit"]) is int and 1 <= selection["limit"] <= 20
                 and selection["limit"] >= len(latest) and selection["kind"] == expected_kind,
                 "latest_selection_scope_mismatch")
        coverage = {"scope": "all_attributed_records_in_pinned_snapshot", "complete": True,
                    "upstream_completeness": "not_verified", "issues": [], "issues_truncated": False, "truncated": False}
        _require(_canonical(selection["coverage"]) == _canonical(coverage), "latest_coverage_mismatch")
        _require(selection["status"] == ("latest_tied" if len(latest) > 1 else "latest_unique")
                 and type(selection["ties_count"]) is int and selection["ties_count"] == len(latest)
                 and _canonical(selection["ties"]) == _canonical(expected_ties), "latest_population_mismatch")
        _require(selection["candidates_sha256"] == _hash(candidate_proof), "latest_candidate_proof_mismatch")
        _require(_canonical(selection["latest_time"]) == _canonical({key: latest[0]["time"][key] for key in ("unix_seconds", "utc")}),
                 "latest_time_mismatch")
        quoted = selection["quotes"]
        _require(type(quoted) is list and len(quoted) == len(latest), "latest_quotes_missing")
        for quote, expected in zip(quoted, latest):
            _require(type(quote) is dict and quote.get("quote_id") in known_quotes
                     and _canonical(quote) == _canonical(known_quotes[quote["quote_id"]]), "latest_quote_reference_mismatch")
            _require(all(quote.get(key) == expected[key] for key in ("evidence_id", "passage_name", "json_pointer")),
                     "latest_quote_reference_mismatch")
    return bool(selections)


def _same_basis(item, basis, spec):
    _require(isinstance(item, dict) and item.get("spec_version") == spec, "invalid_result_shape")
    _require(all(key in item and item[key] == basis[key] for key in BASIS), "result_scope_mismatch")


def _number(value):
    _require(isinstance(value, str) and len(value) <= 100 and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value), "inexact_numeric_result")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise OracleMismatch("inexact_numeric_result") from None


def check_answer(profile, phase, answer, *, definitions, customer_id, snapshot_id, context_id,
                 source_names, fixture_root=None, profile_name="customer_review", require_temporal=False):
    """Check actual customer_a answer against independently attributed raw JSON.

    Callers pin customer UUID, snapshot/context IDs and connection names from
    their local setup. All selected metrics/quotes are checked, and at least one
    quote must contain the phase's latest full substantive customer_a passage.
    ``require_temporal`` additionally requires a independently checked latest
    selection proof, not merely a quote containing the latest known passage.
    Returned receipts contain decisions/hashes, never expected totals or text.
    """
    root, expected, records, fixture_hash = _load(profile, phase, fixture_root)
    reviewed = _definitions(profile, definitions)
    _require(type(require_temporal) is bool, "invalid_temporal_requirement")
    _require(isinstance(source_names, dict) and set(source_names) == ({"stripe", "intercom"} if profile == "billing-support" else {"salesforce", "gong"})
             and all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) for value in source_names.values())
             and len(set(source_names.values())) == len(source_names), "invalid_source_bindings")
    _require(isinstance(answer, dict) and len(_canonical(answer)) <= 2_000_000, "invalid_answer")
    _require(answer.get("spec_version") == "oriel.customer360.grounded-answer/v1" and answer.get("scope") == "reviewed_metrics_and_exact_attributed_quotes"
             and answer.get("abstention") is None and answer.get("model_generated_factual_prose") is False, "answer_not_grounded")
    _require(all(isinstance(answer.get(key), str) and answer[key] for key in BASIS), "missing_answer_basis")
    _require(answer["customer_id"] == customer_id and answer["snapshot_id"] == snapshot_id and answer["context_id"] == context_id
             and answer["profile"] == profile_name and answer["definition_sha256"] == _hash(definitions), "answer_scope_mismatch")
    _seal(answer, "answer_id")
    basis = {key: answer[key] for key in BASIS}
    metrics, quotes = answer.get("metrics"), answer.get("quotes")
    _require(isinstance(metrics, list) and 1 <= len(metrics) <= len(definitions) and isinstance(quotes, list) and 1 <= len(quotes) <= 20, "required_evidence_missing")
    expected_customer = next(row for row in expected["customers"] if row["customer_id"] == "customer_a")
    resolved = {(row["resource"], row["record_id"]) for row in expected["resolved_identities"] if row["customer_id"] == "customer_a"}
    selected_names = set()
    for metric in metrics:
        _same_basis(metric, basis, "oriel.customer360.metric-result/v1")
        _seal(metric, "result_id")
        definition = metric.get("definition")
        _require(any(_canonical(definition) == _canonical(item) for item in definitions), "unreviewed_metric_definition")
        name = definition["name"]
        _require(name not in selected_names, "duplicate_metric")
        selected_names.add(name)
        semantic = next(item["semantic"] for item in reviewed if item["name"] == name)
        kind, operation, filter_field, filter_value, _, required = MEANINGS[profile][semantic]
        resource = RESOURCES[profile][kind]
        attribution_resource = "gong.extensive_calls" if resource == "gong.call_transcripts" else resource
        selected, totals = [], {}
        for row in records[resource]:
            record_id, _, fields = _record(profile, resource, row)
            eligible = filter_field is None or any(type(fields[filter_field]) is type(value) and fields[filter_field] == value for value in filter_value)
            if (attribution_resource, record_id) in resolved and eligible:
                _, _, evidence_id, _ = _evidence(profile, resource, row, basis["workspace_id"], source_names, records)
                selected.append(evidence_id)
                if operation == "sum":
                    # These authored fixtures contain integer source amounts,
                    # never numeric text/floats or a product-derived aggregate.
                    amount, currency = fields["amount"], fields["currency"]
                    _require(type(amount) is int and isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency), "invalid_raw_currency_amount")
                    currency = currency.upper()
                    totals[currency] = totals.get(currency, Decimal(0)) + Decimal(amount)
        if required:
            _require(totals == {key: Decimal(str(value)) for key, value in expected_customer["amounts_by_currency"].items()}, "independent_fixture_expectation_changed")
        result = metric.get("result")
        _require(isinstance(result, dict) and result.get("definition_name") == name and result.get("operation") == definition["operation"]
                 and result.get("unit") == definition["unit"] and result.get("status") == ("computed" if selected or operation == "count" else "no_matching_records")
                 and type(result.get("matched_records")) is int and result["matched_records"] == len(selected), "metric_population_mismatch")
        _require(metric.get("result_sha256") == _hash(result) and metric.get("evidence_ids") == sorted(selected)
                 and metric.get("evidence_sha256") == _hash(sorted(selected)), "metric_evidence_mismatch")
        values = result.get("values")
        _require(isinstance(values, list) and all(isinstance(value, dict) and set(value) == {"currency", "value"} for value in values), "invalid_currency_groups")
        if operation == "count":
            _require(len(values) == 1 and values[0]["currency"] is None and type(values[0]["value"]) is int
                     and values[0]["value"] == len(selected), "count_mismatch")
        else:
            _require(all(isinstance(value["currency"], str) and re.fullmatch(r"[A-Z]{3}", value["currency"]) for value in values), "invalid_currency_groups")
            observed = {value["currency"]: _number(value["value"]) for value in values}
            _require(len(observed) == len(values) and observed == totals, "currency_totals_mismatch")
    required = next(item["name"] for item in reviewed if item["required"])
    _require(required in selected_names, "required_currency_sum_missing")
    seen, matches = set(), []
    latest = expected["latest_attributed_text"]["customer_a"]
    passages = _passages(profile, records, resolved, basis, source_names)
    _require(bool(passages), "independent_passages_missing")
    for quote in quotes:
        _same_basis(quote, basis, "oriel.customer360.quote/v1")
        _seal(quote, "quote_id")
        common_fields = set(BASIS) | {"spec_version", "evidence_id", "record_key", "kind", "connection", "entity",
                        "source_id", "field", "start", "end", "text", "text_sha256", "field_sha256",
                        "field_characters", "is_partial_quote", "quote_id"}
        passage_fields = {"passage_name", "json_pointer", "passage_sha256", "passage_characters", "time", "time_lineage"}
        is_passage = "passage_name" in quote
        _require(set(quote) == common_fields | (passage_fields if is_passage else set()), "invalid_quote_shape")
        _require(quote["quote_id"] not in seen, "duplicate_quote")
        seen.add(quote["quote_id"])
        resource = "intercom.conversations" if profile == "billing-support" else "gong.call_transcripts"
        source, entity = resource.split(".")
        _require(quote.get("connection") == source_names[source] and quote.get("entity") == entity
                 and quote.get("kind") == ("support" if profile == "billing-support" else "conversation"), "quote_attribution_mismatch")
        row = next((row for row in records[resource] if _record(profile, resource, row)[0] == quote.get("source_id")), None)
        _require(row is not None, "quote_source_missing")
        record_id = _record(profile, resource, row)[0]
        attribution_resource = resource if profile == "billing-support" else "gong.extensive_calls"
        _require((attribution_resource, record_id) in resolved, "quote_customer_mismatch")
        record_id, key, evidence, fields = _evidence(profile, resource, row, basis["workspace_id"], source_names, records)
        _require(quote.get("record_key") == key and quote.get("evidence_id") == evidence, "quote_evidence_mismatch")
        field = quote.get("field")
        _require(field in ({"text", "updates"} if profile == "billing-support" else {"text"}), "quote_field_mismatch")
        raw, start, end = fields[field], quote.get("start"), quote.get("end")
        selected_passage = None
        if is_passage:
            selected_passage = next((item for item in passages if item["evidence_id"] == evidence
                                    and item["field"] == field and item["passage_name"] == quote["passage_name"]
                                    and item["json_pointer"] == quote["json_pointer"]), None)
            _require(selected_passage is not None, "passage_locator_mismatch")
            full = selected_passage["value"]
            _require(_canonical(quote["time"]) == _canonical(selected_passage["time"]), "passage_time_mismatch")
            _require(_canonical(quote["time_lineage"]) == _canonical(selected_passage["time_lineage"]), "passage_lineage_mismatch")
            _require(quote["passage_sha256"] == _hash(full) and type(quote["passage_characters"]) is int
                     and quote["passage_characters"] == len(full), "passage_content_mismatch")
        else:
            full = raw
        _require(type(start) is int and type(end) is int and 0 <= start < end <= len(full) and end - start <= 2000, "quote_offsets_mismatch")
        excerpt = full[start:end]
        _require(quote.get("text") == excerpt and quote.get("text_sha256") == _hash(excerpt)
                 and quote.get("field_sha256") == _hash(raw) and type(quote.get("field_characters")) is int
                 and quote.get("field_characters") == len(raw)
                 and quote.get("is_partial_quote") is (start != 0 or end != len(full)), "quote_text_mismatch")
        for citation in expected["attributed_text"]:
            if citation["customer_id"] != "customer_a" or citation["resource"] != resource or citation["record_id"] != record_id:
                continue
            value = json.loads((root / citation["file"]).read_text())
            for token in citation["json_pointer"].split("/")[1:]:
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
            _require(value == citation["text"], "source_pointer_mismatch")
            encoded = value if is_passage else _canonical(value)[1:-1]
            if encoded in excerpt and (not is_passage or citation["json_pointer"] == selected_passage["fixture_pointer"]):
                matches.append(citation)
    _require(latest in matches, "latest_substantive_passage_missing")
    temporal_verified = _check_temporal(answer, basis, passages, required=require_temporal)
    receipt = {"spec": "oriel.customer360.model-smoke-answer-check/v1", "decision": "accepted_synthetic_answer",
               "profile": profile, "phase": phase, "answer_sha256": _hash(answer), "definition_sha256": _hash(definitions),
               "fixture_sha256": fixture_hash, "checked_metrics": len(metrics), "checked_quotes": len(quotes),
               "exact_currency_totals": True, "exact_source_attribution": True, "latest_substantive_passage": True,
               "latest_selection_verified": temporal_verified,
               "source_pointer_sha256": _hash([{key: value[key] for key in ("file", "json_pointer")} for value in matches])}
    return {**receipt, "receipt_sha256": _hash(receipt)}
