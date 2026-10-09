"""Deterministic descriptions of the typed calculation, never its business label.

This presentation is derived from a definition already checked against a reviewed
profile. It neither approves a definition nor infers business meaning from names,
descriptions, source values or model prose. It is not stored in approved context.
"""
from __future__ import annotations

import json
import math

from .contracts import KINDS, name, text

SPEC = "oriel.customer360.calculation-scope/v1"
_FIELDS = ("operation", "kind", "field", "filters", "unit", "currency_field")


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _literal(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean " + _json(value)
    if type(value) is int and -(10 ** 29) < value < 10 ** 29:
        return "integer " + _json(value)
    if type(value) is float and math.isfinite(value):
        return "number " + _json(value)
    if isinstance(value, str) and len(value) <= 1024 and not any(ord(char) < 32 for char in value):
        return "string " + _json(value)
    raise ValueError("Calculation filters require bounded typed scalar literals.")


def calculation_scope(definition, *, snapshot_id=None, customer_id=None):
    """Describe the exact operation and conjunctive population, excluding prose.

    The returned filters are detached from the input and preserve JSON scalar types.
    Optional identifiers bind this description to one execution; without them it is
    a definition preview, not evidence that a query has run. Profile/measure-policy
    authorization remains the responsibility of the context and execution APIs.
    """
    if not isinstance(definition, dict) or not set(_FIELDS) <= set(definition):
        raise ValueError("Calculation scope requires a typed metric definition.")
    scope = {key: definition[key] for key in _FIELDS}
    operation, kind, field = scope["operation"], scope["kind"], scope["field"]
    unit, currency = scope["unit"], scope["currency_field"]
    if not isinstance(kind, str) or kind not in KINDS or operation not in ("count", "sum"):
        raise ValueError("Calculation scope requires a known kind and operation.")
    if operation == "count":
        if field is not None or currency is not None or unit != "records":
            raise ValueError("Count scope must count records without value or currency fields.")
        expression = "Count records"
    else:
        name(field)
        if unit not in ("number", "currency", "minor_currency"):
            raise ValueError("Sum scope requires an explicit supported unit.")
        if unit == "number":
            if currency is not None:
                raise ValueError("Number scope must not declare currency grouping.")
        else:
            name(currency)
        expression = "Sum field " + _json(field)
    filters = scope["filters"]
    if not isinstance(filters, list) or len(filters) > 12:
        raise ValueError("Calculation scope supports at most 12 conjunctive filters.")
    predicates = []
    for predicate in filters:
        if not isinstance(predicate, dict) or set(predicate) != {"field", "operator", "value"}:
            raise ValueError("Calculation filters require field, operator and value.")
        role = name(predicate["field"])
        operator, value = predicate["operator"], predicate["value"]
        if operator == "eq":
            rendered = "equals " + _literal(value)
        elif operator == "in" and isinstance(value, list) and 1 <= len(value) <= 50:
            rendered = "is in [" + ", ".join(_literal(item) for item in value) + "]"
        else:
            raise ValueError("Calculation filters support eq or a bounded nonempty in list.")
        predicates.append(_json(role) + " " + rendered)
    encoded = _json(scope)
    if len(json.dumps(scope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 64_000:
        raise ValueError("Calculation scope exceeds the definition size limit.")
    scope = json.loads(encoded)
    scope.update(spec_version=SPEC, population="selected_customer_resolved_records")
    clauses = [expression + " for kind " + _json(kind) + " using only the selected customer's resolved records in the selected snapshot.",
               "Unresolved and conflicting records and other customers are excluded.",
               ("Filters (all must match): " + "; ".join(predicates) + ".") if predicates else "Filters: none.",
               "Unit: " + unit + ".",
               ("Group separately by uppercase currency codes from field " + _json(currency) + ".")
               if currency is not None else "No currency grouping."]
    scope["text"] = " ".join(clauses)
    for key, value in (("snapshot_id", snapshot_id), ("customer_id", customer_id)):
        if value is not None:
            scope[key] = text(value, key.replace("_", " "), maximum=128)
    return scope
