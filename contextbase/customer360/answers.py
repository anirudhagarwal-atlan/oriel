"""Grounded, read-only customer tools over reviewed context and current evidence.

There is no model loop here. A future model may select immutable metric results
and exact source quotations; it may not add factual prose through this API.
Receipts are returned in full for the caller to retain. Hashes identify content,
not authorship, and validation recomputes references inside the current scope.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, localcontext
import math
import re

from ..context import _identifier
from .contracts import KINDS, canonical, name, sha256, text
from .contexts import current_context_in_connection
from .metric_scope import calculation_scope
from .profiles import readable_customer

MAX_METRIC_RECORDS = 10_000
MAX_QUOTES = 20
MAX_QUOTE_CHARACTERS = 2_000
MAX_ANSWER_CHARACTERS = 20_000
MAX_PASSAGE_CANDIDATES = 10_000
MAX_PASSAGE_PROOF_BYTES = 1_000_000
MAX_LATEST_RECEIPT_BYTES = 2_000_000
_HASH = re.compile(r"[a-f0-9]{64}")
_INTEGER_TYPES = frozenset({"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                            "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"})
_ABSTENTIONS = {
    "definition_unavailable": "No factual answer is returned for the requested definition. Review the available definitions and context gaps.",
    "capability_unavailable": "No factual answer is returned for the requested capability. Review the available source capabilities and context gaps.",
    "insufficient_evidence": "No factual answer is returned. The selected evidence was insufficient to establish an answer.",
}


def _seal(value, key):
    return {**value, key: sha256(value)}


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Supply an exact evidence or result identifier.")
    return value


def _limit(value, maximum=MAX_QUOTES):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Limit must be an integer between 1 and {maximum}.")
    return value


@contextmanager
def _scope(workspace, profile_name, snapshot_id, customer_id):
    with readable_customer(workspace, profile_name, customer_id, snapshot_id) as scope:
        conn, snapshot, approved, materialization, payload = scope
        context = current_context_in_connection(conn, profile_name, snapshot_id, approved_profile=approved)
        if context["profile_approval"] != approved["approval_sha256"] or context["snapshot_id"] != snapshot_id:
            raise ValueError("Reviewed context does not match the current customer scope.")
        yield {"conn": conn, "snapshot": snapshot, "approved": approved,
               "materialization": materialization, "payload": payload, "context": context,
               "profile": profile_name, "customer_id": customer_id, "types": {}}


def _basis(scope):
    receipt, context = scope["materialization"], scope["context"]
    return {"workspace_id": receipt["identity"]["workspace_id"], "profile": scope["profile"],
            "customer_id": scope["customer_id"], "snapshot_id": receipt["snapshot_id"],
            "snapshot_manifest_sha256": scope["snapshot"]["manifest_sha256"],
            "profile_approval": receipt["profile_approval"], "materialization_id": receipt["materialization_id"],
            "materialization_receipt_sha256": receipt["receipt_sha256"],
            "context_id": context["context_id"], "definition_sha256": context["definition_sha256"],
            "identity_sha256": receipt["identity"]["sha256"]}


def _bindings(scope, kind=None):
    return [binding for binding in scope["approved"]["candidate"]["profile"]["bindings"]
            if kind is None or binding["kind"] == kind]


def _types(scope, binding):
    if binding["table"] in scope["types"]:
        return scope["types"][binding["table"]]
    schema, table = binding["table"].split(".")
    found = dict(scope["conn"].execute(
        "SELECT column_name,data_type FROM information_schema.columns "
        "WHERE table_catalog=current_database() AND table_schema=? AND table_name=?", [schema, table]).fetchall())
    scope["types"][binding["table"]] = found
    return found


def _parameter(value):
    # JSON numeric filter values must not become floating-point SQL operands.
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Filters must contain finite scalar values.")
        return Decimal(str(value))
    return value


def _predicate(binding, predicate):
    column = "s." + _identifier(binding["fields"][predicate["field"]])
    values = predicate["value"] if predicate["operator"] == "in" else [predicate["value"]]
    parameters = [_parameter(value) for value in values if value is not None]
    pieces = [column + " IS NULL"] if None in values else []
    if parameters:
        pieces.append(column + " IN (" + ",".join("?" for _ in parameters) + ")")
    return "(" + " OR ".join(pieces) + ")", parameters


def _metric(scope, definition_name):
    name(definition_name)
    definition = next((item for item in scope["context"]["definitions"] if item["name"] == definition_name), None)
    if definition is None:
        raise ValueError("Select an approved metric definition by name.")
    queries, evidence_ids, totals = [], [], {}
    matched_records = 0
    for binding in _bindings(scope, definition["kind"]):
        schema, table = binding["table"].split(".")
        types = _types(scope, binding)
        amount = "NULL"
        if definition["operation"] == "sum":
            column = binding["fields"][definition["field"]]
            dtype = types.get(column, "")
            if dtype not in _INTEGER_TYPES and not re.fullmatch(r"DECIMAL\([0-9]+,[0-9]+\)", dtype):
                raise ValueError("Exact sums require an integer or DECIMAL source column; floating-point, boolean and text values are refused.")
            amount = "s." + _identifier(column)
        currency = "NULL"
        if definition["currency_field"] is not None:
            column = binding["fields"][definition["currency_field"]]
            if types.get(column) != "VARCHAR":
                raise ValueError("A monetary metric requires a text currency column.")
            currency = "s." + _identifier(column)
        sql = (f"SELECT r.evidence_id,{amount} AS amount,{currency} AS currency "
               f"FROM {_identifier(schema)}.{_identifier(table)} s JOIN customer360.records r "
               f"ON CAST(s.{_identifier(binding['record_id'])} AS VARCHAR)=r.source_id "
               "WHERE r.profile=? AND r.customer_id=? AND r.snapshot_id=? AND r.materialization_id=? "
               "AND r.connection=? AND r.entity=? AND r.kind=? AND r.link_status='resolved'")
        parameters = [scope["profile"], scope["customer_id"], scope["snapshot"]["snapshot_id"],
                      scope["materialization"]["materialization_id"], binding["connection"], binding["entity"], binding["kind"]]
        for predicate in definition["filters"]:
            fragment, values = _predicate(binding, predicate)
            sql += " AND " + fragment
            parameters.extend(values)
        try:
            matched = scope["conn"].execute(sql + " ORDER BY r.evidence_id LIMIT ?",
                                             [*parameters, MAX_METRIC_RECORDS + 1]).fetchall()
        except Exception:
            raise ValueError("The approved metric could not be read with exact source types.") from None
        matched_records += len(matched)
        if matched_records > MAX_METRIC_RECORDS:
            raise ValueError("Metric evidence exceeds the bounded record limit.")
        for evidence_id, value, code in matched:
            if definition["operation"] == "sum":
                if type(value) is bool or not isinstance(value, (int, Decimal)) or not Decimal(value).is_finite():
                    raise ValueError("A selected numeric value is missing or not exact; no zero was substituted.")
                if definition["unit"] == "minor_currency" and Decimal(value) != Decimal(value).to_integral_value():
                    raise ValueError("Minor currency units must be integral.")
            if definition["currency_field"] is not None and (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{3}", code)):
                raise ValueError("A selected currency value is missing or invalid; no default was substituted.")
            evidence_ids.append(evidence_id)
        if definition["operation"] == "count":
            aggregate = f"SELECT NULL AS currency,count(*) AS value FROM ({sql}) selected"
        elif definition["currency_field"] is not None:
            aggregate = f"SELECT upper(currency) AS currency,sum(amount) AS value FROM ({sql}) selected GROUP BY upper(currency) ORDER BY currency"
        else:
            aggregate = f"SELECT NULL AS currency,sum(amount) AS value FROM ({sql}) selected"
        try:
            rows = scope["conn"].execute(aggregate, parameters).fetchall()
        except Exception:
            raise ValueError("The approved metric exceeds exact SQL aggregation limits.") from None
        for code, value in rows:
            if value is None:
                continue
            if type(value) is bool or not isinstance(value, (int, Decimal)):
                raise ValueError("SQL aggregation did not return an exact number.")
            with localcontext() as ctx:
                ctx.prec = 80  # Exact addition across at most 64 bounded DECIMAL(38,*) SQL groups.
                totals[code] = totals.get(code, Decimal(0)) + Decimal(value)
        query = {"sql": aggregate, "parameters": json_safe(parameters),
                 "selection_sql": sql + " ORDER BY r.evidence_id LIMIT ?",
                 "selection_parameters": json_safe([*parameters, MAX_METRIC_RECORDS + 1]),
                 "source_table": binding["table"], "matched_records": len(matched)}
        queries.append(_seal(query, "query_sha256"))
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Metric projection repeats source evidence; inspect the reviewed grain.")
    values = [{"currency": code, "value": int(value) if definition["operation"] == "count" else format(value, "f")}
              for code, value in sorted(totals.items(), key=lambda row: row[0] or "")]
    result = {"definition_name": definition_name, "operation": definition["operation"], "unit": definition["unit"],
              "values": values, "matched_records": matched_records,
              "status": "computed" if matched_records or definition["operation"] == "count" else "no_matching_records"}
    return _seal({"spec_version": "oriel.customer360.metric-result/v1", **_basis(scope),
                  "definition": definition, "queries": queries, "result": result,
                  "calculation_scope": calculation_scope(definition, snapshot_id=scope["snapshot"]["snapshot_id"],
                                                         customer_id=scope["customer_id"]),
                  "result_sha256": sha256(result), "evidence_ids": sorted(evidence_ids),
                  "evidence_sha256": sha256(sorted(evidence_ids))}, "result_id")


def json_safe(value):
    # Lossless canonical conversion; SQL still receives original typed parameters.
    import json
    return json.loads(canonical(value))


def execute_metric(workspace, profile_name, snapshot_id, customer_id, definition_name):
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        return _metric(scope, definition_name)


def _quote(scope, evidence_id, field, start=0, end=None):
    _hash(evidence_id)
    name(field)
    record = next((row for row in scope["payload"]["records"] if row["evidence_id"] == evidence_id), None)
    if record is None:
        raise ValueError("Evidence is unavailable for this customer and current snapshot.")
    binding = next(binding for binding in _bindings(scope)
                   if (binding["connection"], binding["entity"]) == (record["connection"], record["entity"]))
    if field not in binding["fields"] or _types(scope, binding).get(binding["fields"][field]) != "VARCHAR":
        raise ValueError("Quote an observed text field in the approved mapping.")
    value = record["fields"].get(field)
    if not isinstance(value, str) or not value:
        raise ValueError("The selected evidence field has no text to quote.")
    if type(start) is not int or start < 0:
        raise ValueError("Quote offsets must be nonnegative integer character positions.")
    end = min(len(value), start + MAX_QUOTE_CHARACTERS) if end is None else end
    if type(end) is not int or not start < end <= len(value) or end - start > MAX_QUOTE_CHARACTERS:
        raise ValueError("Select a nonempty exact quote within the character limit.")
    return _seal({"spec_version": "oriel.customer360.quote/v1", **_basis(scope),
                  "evidence_id": evidence_id, "record_key": record["record_key"], "kind": record["kind"],
                  "connection": record["connection"], "entity": record["entity"], "source_id": record["source_id"],
                  "field": field, "start": start, "end": end, "text": value[start:end],
                  "text_sha256": sha256(value[start:end]), "field_sha256": sha256(value),
                  "field_characters": len(value), "is_partial_quote": start != 0 or end != len(value)}, "quote_id")


def _passage_time(record, passage, match):
    from .temporal import parse_timestamp, resolve_pointer, select_leaves
    rule = passage["time"]
    lineage = {"connection": record["connection"], "entity": record["entity"],
               "source_id": record["source_id"], "evidence_id": record["evidence_id"],
               "source": rule["source"], "path": rule["path"]}
    value = None
    if rule["source"] == "item":
        found, value = resolve_pointer(match["parent"], rule["path"])
        lineage.update(field=passage["field"], json_pointer=match["path"].rsplit("/", 1)[0] + rule["path"],
                       passage_json_pointer=match["path"],
                       field_sha256=sha256(record["fields"].get(passage["field"])))
    else:
        fields = record["fields"]
        found = True
        if rule["source"] == "attachment":
            attachment = record.get("attachments", {}).get(rule["attachment"], {})
            lineage.update(attachment=rule["attachment"],
                           attachment_lineage={key: item for key, item in attachment.items() if key != "fields"})
            found = attachment.get("status") == "matched"
            fields = attachment.get("fields", {}) if found else {}
        found = found and rule["field"] in fields
        value = fields.get(rule["field"])
        lineage.update(field=rule["field"], field_sha256=sha256(value))
        if found and rule["path"]:
            selected = select_leaves(value if isinstance(value, str) else canonical(value), rule["path"], wildcard=False)
            found = selected["complete"] and len(selected["matches"]) == 1
            value = selected["matches"][0]["value"] if found else None
    parsed = parse_timestamp(value if found else None, rule["encoding"])
    return {**parsed, "encoding": rule["encoding"], "meaning": rule["meaning"]}, lineage


def _passage_quote(scope, record, binding, passage, match, start=0, end=None):
    field, value = passage["field"], match["value"]
    if (not isinstance(value, str) or not value.strip()
            or _types(scope, binding).get(binding["fields"][field]) != "VARCHAR"):
        raise ValueError("The selected passage is not observed nonempty text.")
    if type(start) is not int or start < 0:
        raise ValueError("Quote offsets must be nonnegative integer character positions.")
    end = min(len(value), start + MAX_QUOTE_CHARACTERS) if end is None else end
    if type(end) is not int or not start < end <= len(value) or end - start > MAX_QUOTE_CHARACTERS:
        raise ValueError("Select a nonempty exact passage within the character limit.")
    timestamp, lineage = _passage_time(record, passage, match)
    raw = record["fields"][field]
    return _seal({"spec_version": "oriel.customer360.quote/v1", **_basis(scope),
                  "evidence_id": record["evidence_id"], "record_key": record["record_key"], "kind": record["kind"],
                  "connection": record["connection"], "entity": record["entity"], "source_id": record["source_id"],
                  "field": field, "passage_name": passage["name"], "json_pointer": match["path"],
                  "start": start, "end": end, "text": value[start:end], "text_sha256": sha256(value[start:end]),
                  "field_sha256": sha256(raw), "field_characters": len(raw),
                  "passage_sha256": sha256(value), "passage_characters": len(value),
                  "is_partial_quote": start != 0 or end != len(value), "time": timestamp,
                  "time_lineage": lineage}, "quote_id")


def _read_passage(scope, evidence_id, field, passage_name, json_pointer, start=0, end=None):
    from .temporal import select_leaves, validate_pointer
    _hash(evidence_id)
    name(field)
    name(passage_name)
    validate_pointer(json_pointer)
    record = next((row for row in scope["payload"]["records"] if row["evidence_id"] == evidence_id), None)
    if record is None:
        raise ValueError("Evidence is unavailable for this customer and current snapshot.")
    binding = next(b for b in _bindings(scope) if (b["connection"], b["entity"]) == (record["connection"], record["entity"]))
    passage = next((p for p in binding.get("passages", []) if p["name"] == passage_name and p["field"] == field), None)
    if passage is None:
        raise ValueError("Select an approved passage mapping and logical text field.")
    selected = select_leaves(record["fields"].get(field), passage["text_path"])
    match = next((item for item in selected["matches"] if item["path"] == json_pointer), None)
    if not selected["complete"] or match is None:
        raise ValueError("The passage locator is missing or its extraction is incomplete.")
    return _passage_quote(scope, record, binding, passage, match, start, end)


def read_evidence(workspace, profile_name, snapshot_id, customer_id, evidence_id, *, field="text", start=0, end=None,
                  passage_name=None, json_pointer=None):
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        if passage_name is not None or json_pointer is not None:
            if passage_name is None or json_pointer is None:
                raise ValueError("Passage reads require both passage_name and json_pointer.")
            return _read_passage(scope, evidence_id, field, passage_name, json_pointer, start, end)
        return _quote(scope, evidence_id, field, start, end)


def _latest(scope, kind, limit):
    from .temporal import select_leaves
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Select a known customer capability for latest evidence.")
    limit = _limit(limit)
    bindings, candidates, issues, proof = _bindings(scope, kind), [], [], []
    issue_count, proof_bytes = 0, 2

    def issue(value):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 64:
            issues.append(value)

    if not bindings:
        issue({"reason": "capability_unavailable"})
    overflow = False
    for binding in bindings:
        if not binding.get("passages"):
            issue({"connection": binding["connection"], "entity": binding["entity"], "reason": "passage_mapping_missing"})
            continue
        for record in scope["payload"]["records"]:
            if (record["connection"], record["entity"]) != (binding["connection"], binding["entity"]):
                continue
            for passage in binding["passages"]:
                locator = {"evidence_id": record["evidence_id"], "passage_name": passage["name"]}
                field = passage["field"]
                if _types(scope, binding).get(binding["fields"][field]) != "VARCHAR":
                    issue({**locator, "reason": "passage_field_not_text"})
                    continue
                selected = select_leaves(record["fields"].get(field), passage["text_path"])
                if not selected["complete"]:
                    issue({**locator, "reason": "incomplete_passage_extraction"})
                for match in selected["matches"]:
                    if not isinstance(match["value"], str):
                        issue({**locator, "json_pointer": match["path"], "reason": "passage_not_text"})
                        continue
                    if not match["value"].strip():
                        continue
                    timestamp, lineage = _passage_time(record, passage, match)
                    if timestamp["status"] != "known":
                        issue({**locator, "json_pointer": match["path"], "reason": "passage_time_unknown"})
                    observed = {**locator, "json_pointer": match["path"], "passage_sha256": sha256(match["value"]),
                                "time": timestamp, "time_lineage_sha256": sha256(lineage)}
                    encoded_size = len(canonical(observed).encode("utf-8")) + 1
                    if proof_bytes + encoded_size > MAX_PASSAGE_PROOF_BYTES:
                        overflow = True
                        break
                    proof_bytes += encoded_size
                    proof.append(observed)
                    candidates.append((record, binding, passage, match, timestamp, lineage))
                    if len(candidates) >= MAX_PASSAGE_CANDIDATES:
                        overflow = True
                        break
                if overflow: break
            if overflow: break
        if overflow: break
    if overflow:
        issue({"reason": "passage_candidate_bound_reached"})
    status, latest_time, quotes, ties = "unknown", None, [], []
    ties_count = None
    truncated = False
    if not issue_count and not candidates:
        status = "no_passages"
        ties_count = 0
    elif not issue_count:
        maximum = max(Decimal(row[4]["unix_seconds"]) for row in candidates)
        selected = [row for row in candidates if Decimal(row[4]["unix_seconds"]) == maximum]
        selected.sort(key=lambda row: (row[0]["evidence_id"], row[2]["name"], row[3]["path"]))
        latest_time = {key: selected[0][4][key] for key in ("unix_seconds", "utc")}
        ties_count = len(selected)
        ties = [{"evidence_id": row[0]["evidence_id"], "passage_name": row[2]["name"], "json_pointer": row[3]["path"]} for row in selected[:limit]]
        truncated = len(selected) > limit
        status = "latest_ties_truncated" if truncated else "latest_tied" if len(selected) > 1 else "latest_unique"
        if not truncated:
            quote_bound = min(MAX_QUOTE_CHARACTERS, MAX_ANSWER_CHARACTERS // len(selected))
            quotes = [_passage_quote(scope, *row[:4], end=min(len(row[3]["value"]), quote_bound)) for row in selected]
    receipt = _seal({"spec_version": "oriel.customer360.latest-selection/v1", **_basis(scope),
                  "kind": kind, "limit": limit, "status": status,
                  "coverage": {"scope": "all_attributed_records_in_pinned_snapshot", "complete": not issue_count,
                               "upstream_completeness": scope["snapshot"]["coverage"]["upstream_completeness"],
                               "issues": issues, "issues_truncated": issue_count > 64, "truncated": truncated},
                  "candidates_sha256": sha256(proof), "latest_time": latest_time,
                  "ties": ties, "ties_count": ties_count, "quotes": quotes}, "selection_id")
    if len(canonical(receipt).encode("utf-8")) > MAX_LATEST_RECEIPT_BYTES:
        raise ValueError("Latest evidence exceeds its receipt size bound; narrow the reviewed scope.")
    return receipt


def latest_evidence(workspace, profile_name, snapshot_id, customer_id, kind, *, limit=5):
    """Select latest reviewed passages from the complete scoped projection.

    Unknown times, incomplete extraction or unmapped bindings prevent a latest
    claim. Empty arrays contain no passages; equal event times remain ties.
    """
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        return _latest(scope, kind, limit)


def _fields(fields):
    if (not isinstance(fields, (list, tuple)) or not 1 <= len(fields) <= 8
            or any(not isinstance(field, str) for field in fields) or len(set(fields)) != len(fields)):
        raise ValueError("Select one to eight distinct logical text fields.")
    return [name(field) for field in fields]


def _search(scope, query, fields, limit):
    quotes, matched, characters = [], 0, 0
    for record in scope["payload"]["records"]:
        for field in fields:
            value = record["fields"].get(field)
            if not isinstance(value, str) or not value or (query and query.casefold() not in value.casefold()):
                continue
            binding = next(b for b in _bindings(scope) if (b["connection"], b["entity"]) == (record["connection"], record["entity"]))
            if _types(scope, binding).get(binding["fields"][field]) != "VARCHAR":
                continue
            matched += 1
            if len(quotes) >= limit or characters >= MAX_ANSWER_CHARACTERS:
                continue
            # Unicode casefold can expand one character (e.g. ß -> ss).
            # Convert the match back to its original character position.
            folded_at = value.casefold().find(query.casefold()) if query else 0
            original_at, folded_length = 0, 0
            while folded_length < folded_at:
                folded_length += len(value[original_at].casefold())
                original_at += 1
            if folded_length > folded_at:
                original_at -= 1
            start = max(0, original_at - 200)
            end = min(len(value), start + MAX_QUOTE_CHARACTERS, start + MAX_ANSWER_CHARACTERS - characters)
            quote = _quote(scope, record["evidence_id"], field, start, end)
            quotes.append(quote)
            characters += len(quote["text"])
    return {"quotes": quotes, "matched_fields": matched, "truncated": matched > len(quotes)}


def search_evidence(workspace, profile_name, snapshot_id, customer_id, query, *, fields=("text",), limit=5):
    text(query, "search query", maximum=200)
    fields, limit = _fields(fields), _limit(limit)
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        available = {role for binding in _bindings(scope) for role, column in binding["fields"].items()
                     if _types(scope, binding).get(column) == "VARCHAR"}
        if set(fields) - available:
            raise ValueError("Search only observed text fields in the approved mapping.")
        return _seal({"spec_version": "oriel.customer360.evidence-search/v1", **_basis(scope),
                      "query": query, "fields": fields, "limit": limit,
                      **_search(scope, query, fields, limit)}, "search_id")


def scoped_gaps(context, payload):
    """Decorate immutable review notes separately from current projection gaps.

    The caller supplies validated context and customer payload in one read scope.
    A reused definition does not revalidate prose captured at its approval.
    """
    approved_snapshot = context["approved_snapshot_id"]
    snapshot = context["snapshot_id"]
    gaps = [{**gap, "origin": "approved_context", "source_snapshot_id": approved_snapshot,
             "context_id": context["context_id"],
             "not_rechecked_after_refresh": approved_snapshot != snapshot}
            for gap in context["gaps"]]
    current = []
    capabilities = set(payload["capabilities"])
    for kind in sorted(KINDS - capabilities):
        current.append({"code": "capability_unavailable", "kind": kind,
                        "description": "No reviewed source capability is available for this category."})
    unresolved = sum(count for item in payload["coverage"]
                     for status, count in item["identity"].items() if status != "resolved")
    if unresolved:
        current.append({"code": "profile_unresolved_identity", "records": unresolved,
                        "description": "Profile-wide unlinked or conflicting records are excluded; they are not attributed to this customer."})
    if not payload["records"]:
        current.append({"code": "no_attributed_evidence", "description": "No records are attributed to this customer in the current snapshot."})
    return gaps + [{**gap, "origin": "current_projection", "source_snapshot_id": snapshot} for gap in current]


def _gaps(scope):
    return scoped_gaps(scope["context"], scope["payload"])


def _bounded_render(lines, metric_fallbacks):
    """Keep complete lines; omitted scope is a reference, never a partial rule."""
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_ANSWER_CHARACTERS:
        return rendered, False
    notice = ("Some details are omitted from this bounded text summary. Inspect the full structured answer "
              "for every calculation scope, result, quotation and note.")
    remaining = MAX_ANSWER_CHARACTERS - len(notice) - 1
    kept = []
    for index, line in enumerate(lines):
        for candidate in (line, *metric_fallbacks.get(index, ())):
            required = len(candidate) + bool(kept)
            if required <= remaining:
                kept.append(candidate)
                remaining -= required
                break
    return "\n".join([*kept, notice]), True


def _render(scope, metrics, quotes, *, evidence_selection=None, abstain=None, temporal_selections=None):
    lines = [_ABSTENTIONS[abstain]] if abstain else []
    metric_fallbacks = {}
    for index, metric in enumerate(metrics):
        result = metric["result"]
        value = "; ".join((row["currency"] + " " if row["currency"] else "") + str(row["value"]) for row in result["values"])
        calculated = metric["calculation_scope"]
        reference = (f"Definition identifier {result['definition_name']!r}. Full calculation scope omitted from this text summary; "
                     f"inspect metrics[{index}].calculation_scope.")
        metric_fallbacks[len(lines)] = (f"{reference} Result: {value or 'no matching records'} ({result['unit']}).",
                                        f"{reference} Full result: metrics[{index}].result.")
        lines.append(f"Definition identifier {result['definition_name']!r}. Calculation: {calculated['text']} "
                     f"Result: {value or 'no matching records'} ({result['unit']}).")
    for quote in quotes:
        lines.append(f"Source {quote['connection']}/{quote['entity']}/{quote['source_id']} states: {quote['text']!r}")
    for selected in temporal_selections or []:
        label = "Latest observed passage" if selected["status"] == "latest_unique" else "Latest observed passages are tied"
        lines.append(f"{label} for {selected['kind']} at {selected['latest_time']['utc']} in the pinned snapshot.")
    gaps = _gaps(scope)
    for gap in gaps:
        if gap["origin"] == "approved_context":
            status = "; not rechecked after refresh" if gap["not_rechecked_after_refresh"] else ""
            lines.append(f"Retained context note {gap['code']} (approval snapshot {gap['source_snapshot_id']}{status}): {gap['description']}")
        else:
            lines.append(f"Current snapshot gap {gap['code']}: {gap['description']}")
    rendered, truncated = _bounded_render(lines, metric_fallbacks)
    return _seal({"spec_version": "oriel.customer360.grounded-answer/v1", **_basis(scope),
                  "context_approved_snapshot_id": scope["context"]["approved_snapshot_id"],
                  "context_notes_not_rechecked_after_refresh": scope["context"]["approved_snapshot_id"] != scope["context"]["snapshot_id"],
                  "metrics": metrics, "quotes": quotes, "gaps": gaps,
                  "evidence_selection": evidence_selection or {"mode": "explicit_reference_selection", "exhaustive": False},
                  "capabilities": scope["payload"]["capabilities"],
                  "source_observed_at": scope["payload"]["source_observed_at"],
                  "rendered": rendered, **({"rendered_truncated": True} if truncated else {}),
                  "scope": "reviewed_metrics_and_exact_attributed_quotes",
                  "abstention": {"code": abstain, "meaning": "withheld_answer_not_a_claim_of_source_absence"} if abstain else None,
                  "model_generated_factual_prose": False,
                  **({"temporal_selections": temporal_selections} if temporal_selections is not None else {})}, "answer_id")


def _brief(scope, fields, quote_limit):
    """Build a brief inside an already validated scope and source transaction."""
    metrics = [_metric(scope, definition["name"]) for definition in scope["context"]["definitions"]]
    selected = _search(scope, "", fields, quote_limit)
    return _render(scope, metrics, selected["quotes"], evidence_selection={
        "mode": "bounded_observed_text", "fields": fields, "matched_fields": selected["matched_fields"],
        "returned_quotes": len(selected["quotes"]), "truncated": selected["truncated"], "exhaustive": False})


def brief(workspace, profile_name, snapshot_id, customer_id, *, fields=("text",), quote_limit=5):
    fields, quote_limit = _fields(fields), _limit(quote_limit)
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        return _brief(scope, fields, quote_limit)


def validate_answer(workspace, profile_name, snapshot_id, customer_id, answer):
    """Recompute selected immutable references, rejecting unsupported factual prose.

    Input shape: {metrics: [{definition_name, result_id}], quotes:
    [{evidence_id, field, start, end, text, quote_id}]}, or {abstain: {code}}.
    The fixed abstention code withholds an answer; it does not assert a customer
    fact or prove upstream data absent. Extra fields and free reasons are refused.
    A matching hash alone never establishes trust: every reference is recreated
    from the currently approved, customer-scoped source evidence.
    """
    if isinstance(answer, dict) and set(answer) == {"abstain"}:
        selected = answer["abstain"]
        if (not isinstance(selected, dict) or set(selected) != {"code"}
                or not isinstance(selected["code"], str) or selected["code"] not in _ABSTENTIONS):
            raise ValueError("Select a declared abstention code without a factual reason.")
        with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
            return _render(scope, [], [], abstain=selected["code"])
    if not isinstance(answer, dict) or set(answer) not in ({"metrics", "quotes"}, {"metrics", "quotes", "latest"}):
        raise ValueError("Answers may select approved metric references and exact quotes only; free factual prose is unsupported.")
    metrics, quotes = answer["metrics"], answer["quotes"]
    latest = answer.get("latest", [])
    if (not isinstance(metrics, list) or len(metrics) > 32 or not isinstance(quotes, list)
            or len(quotes) > MAX_QUOTES or not (metrics or quotes)
            or not isinstance(latest, list) or len(latest) > len(KINDS)):
        raise ValueError("Select a bounded nonempty set of metric and quote references.")
    with _scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        approved_metrics, approved_quotes, seen = [], [], set()
        for selected in metrics:
            if not isinstance(selected, dict) or set(selected) != {"definition_name", "result_id"}:
                raise ValueError("Metric selections require definition_name and result_id only.")
            _hash(selected["result_id"])
            result = _metric(scope, selected["definition_name"])
            if result["result_id"] != selected["result_id"] or result["result_id"] in seen:
                raise ValueError("Metric reference changed, is duplicated, or belongs to another customer/context/snapshot.")
            seen.add(result["result_id"])
            approved_metrics.append(result)
        for selected in quotes:
            required = {"evidence_id", "field", "start", "end", "text", "quote_id"}
            if not isinstance(selected, dict) or set(selected) not in (required, required | {"passage_name", "json_pointer"}):
                raise ValueError("Quote selections require exact evidence, field, offsets, text and quote_id only.")
            if type(selected["start"]) is not int or type(selected["end"]) is not int:
                raise ValueError("Final quote selections require exact integer offsets.")
            _hash(selected["quote_id"])
            quote = (_read_passage(scope, selected["evidence_id"], selected["field"], selected["passage_name"],
                                   selected["json_pointer"], selected["start"], selected["end"])
                     if "passage_name" in selected else
                     _quote(scope, selected["evidence_id"], selected["field"], selected["start"], selected["end"]))
            if quote["quote_id"] != selected["quote_id"] or quote["text"] != selected["text"] or quote["quote_id"] in seen:
                raise ValueError("Quote text or reference is fabricated, duplicated, stale or outside this customer scope.")
            seen.add(quote["quote_id"])
            approved_quotes.append(quote)
        if sum(len(quote["text"]) for quote in approved_quotes) > MAX_ANSWER_CHARACTERS:
            raise ValueError("Selected quote text exceeds the answer character limit.")
        temporal, kinds = [], set()
        quote_ids = {quote["quote_id"] for quote in approved_quotes}
        for reference in latest:
            if not isinstance(reference, dict) or set(reference) != {"kind", "limit", "selection_id"}:
                raise ValueError("Latest references require kind, limit and selection_id only.")
            _hash(reference["selection_id"])
            selected = _latest(scope, reference["kind"], reference["limit"])
            if (selected["kind"] in kinds or selected["selection_id"] != reference["selection_id"]
                    or selected["status"] not in {"latest_unique", "latest_tied"}
                    or not {quote["quote_id"] for quote in selected["quotes"]}.issubset(quote_ids)):
                raise ValueError("Latest evidence is stale, incomplete, duplicated or missing tied passages.")
            kinds.add(selected["kind"])
            temporal.append(selected)
        return _render(scope, approved_metrics, approved_quotes,
                       temporal_selections=temporal if "latest" in answer else None)
