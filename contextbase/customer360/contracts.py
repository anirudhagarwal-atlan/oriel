"""Bounded, declarative source-to-customer mappings; no executable expressions."""
from __future__ import annotations

import hashlib
import json
import re

from ..workspace import serializable

PROFILE_SPEC = "oriel.customer360.profile/v1"
KINDS = frozenset({"customer", "person", "commercial", "billing", "support", "usage", "conversation"})


def canonical(value):
    return json.dumps(serializable(value, lossless=True), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value, label, *, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"Supply bounded {label} text without control characters.")
    return value


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("Names must use lowercase letters, digits and underscores.")
    return value


def source_id(value):
    # Integers retain exact decimal form. Floats/bools must never become IDs.
    if type(value) is int:
        value = str(value)
    return text(value, "source identifier", maximum=1024)


def _source_mapping(binding):
    connection, entity = name(binding["connection"]), name(binding["entity"])
    table = binding["table"]
    if not isinstance(table, str) or len(table.split(".")) != 2:
        raise ValueError("Source tables use connection.table names.")
    schema, relation = table.split(".")
    if schema != connection or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", relation):
        raise ValueError("Bind a base table owned by the declared connection.")
    text(binding["record_id"], "record ID column")
    ref = binding["customer_ref"]
    if not isinstance(ref, dict) or set(ref) != {"connection", "entity", "column"}:
        raise ValueError("Customer references require connection, entity and column.")
    name(ref["connection"])
    name(ref["entity"])
    text(ref["column"], "customer reference column")
    fields = binding["fields"]
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 64:
        raise ValueError("Map 1–64 observed fields per binding.")
    for role, column in fields.items():
        name(role)
        text(column, "source column")
    return connection, entity, table


def _temporal_mapping(binding):
    attachments = binding.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 8:
        raise ValueError("A binding supports at most eight reviewed attachments.")
    by_name = {}
    for attachment in attachments:
        if not isinstance(attachment, dict) or set(attachment) != {
            "name", "connection", "entity", "table", "record_id", "local_key", "remote_key", "customer_ref", "fields"
        }:
            raise ValueError("Attachments require exact source, key, customer and field mappings.")
        label = name(attachment["name"])
        if label in by_name:
            raise ValueError("Attachment names must be unique within a binding.")
        by_name[label] = attachment
        _source_mapping(attachment)
        text(attachment["local_key"], "local join column")
        text(attachment["remote_key"], "remote join column")
    passages = binding.get("passages", [])
    if not isinstance(passages, list) or len(passages) > 16:
        raise ValueError("A binding supports at most sixteen reviewed passage mappings.")
    names = set()
    for passage in passages:
        from .temporal import validate_pointer
        if not isinstance(passage, dict) or set(passage) != {"name", "field", "text_path", "time"}:
            raise ValueError("Passages require name, field, text_path and time.")
        label = name(passage["name"])
        if label in names:
            raise ValueError("Passage names must be unique within a binding.")
        names.add(label)
        if name(passage["field"]) not in binding["fields"]:
            raise ValueError("A passage must reference an existing mapped field.")
        validate_pointer(passage["text_path"], wildcard=True)
        timestamp = passage["time"]
        if not isinstance(timestamp, dict):
            raise ValueError("A passage time requires a reviewed mapping.")
        source = timestamp.get("source")
        expected = {"source", "path", "encoding", "meaning"}
        if source == "field":
            expected.add("field")
        elif source == "attachment":
            expected.update({"field", "attachment"})
        elif source != "item":
            raise ValueError("Unknown passage time source.")
        if set(timestamp) != expected:
            raise ValueError("A passage time requires the exact fields for its source.")
        if source == "field" and name(timestamp["field"]) not in binding["fields"]:
            raise ValueError("Passage time references an unmapped field.")
        if source == "attachment":
            attachment = by_name.get(name(timestamp["attachment"]))
            if attachment is None or name(timestamp["field"]) not in attachment["fields"]:
                raise ValueError("Passage time references an unmapped attachment field.")
        validate_pointer(timestamp["path"])
        if timestamp["encoding"] not in ("rfc3339", "unix_seconds"):
            raise ValueError("Unknown passage time encoding.")
        if timestamp["meaning"] not in ("event_started", "record_created", "record_updated", "passage_created"):
            raise ValueError("Unknown reviewed passage time meaning.")


def _measure_mapping(binding):
    """Validate operator assertions; SQL types never establish a measure unit."""
    measures = binding.get("measures", {})
    if not isinstance(measures, dict) or len(measures) > 64:
        raise ValueError("A binding supports at most 64 reviewed measure policies.")
    for role, policy in measures.items():
        if name(role) not in binding["fields"]:
            raise ValueError("A measure policy must reference an existing mapped value field.")
        if not isinstance(policy, dict) or set(policy) != {"unit", "currency_field", "aggregation", "meaning"}:
            raise ValueError("Measure policies require unit, currency_field, aggregation and meaning.")
        unit, currency = policy["unit"], policy["currency_field"]
        if not isinstance(unit, str) or unit not in ("number", "currency", "minor_currency", "unknown"):
            raise ValueError("Choose a known measure unit or explicitly leave it unknown.")
        if not isinstance(policy["aggregation"], str) or policy["aggregation"] not in ("sum", "none", "unknown"):
            raise ValueError("Choose whether summing this measure is allowed, disallowed or unknown.")
        text(policy["meaning"], "measure meaning and review basis", maximum=2000)
        if currency is not None:
            if name(currency) not in binding["fields"] or currency == role:
                raise ValueError("A measure currency must reference a different mapped field.")
        if unit in ("currency", "minor_currency") and currency is None:
            raise ValueError("Monetary measure policies require a mapped currency field.")
        if unit == "number" and currency is not None:
            raise ValueError("Nonmonetary measure policies have no currency field.")


def validate_profile(value):
    """Return a detached canonical profile or reject ambiguous configuration.

    Columns are exact observed column names, not SQL or template expressions.
    Customer links reference the identity map independently of record identity.
    Meanings in `fields` are reviewed source mappings, not metric certification.
    """
    if not isinstance(value, dict) or set(value) != {"spec_version", "name", "description", "bindings"}:
        raise ValueError("Profile requires spec_version, name, description and bindings.")
    if value["spec_version"] != PROFILE_SPEC:
        raise ValueError("Unsupported Customer 360 profile version.")
    name(value["name"])
    text(value["description"], "profile description", maximum=2000)
    bindings = value["bindings"]
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
        raise ValueError("A profile requires 1–64 source bindings.")
    seen, seen_tables = set(), set()
    for binding in bindings:
        required = {"connection", "entity", "table", "kind", "record_id", "customer_ref", "fields"}
        if (not isinstance(binding, dict) or not required <= set(binding)
                or not set(binding) <= required | {"attachments", "passages", "measures"}):
            raise ValueError("Each source binding requires the declared mapping fields.")
        connection, entity, table = _source_mapping(binding)
        if (connection, entity) in seen:
            raise ValueError("Each connection/entity has exactly one binding per profile.")
        seen.add((connection, entity))
        if table in seen_tables:
            raise ValueError("A source table can be bound only once; duplicate bindings would duplicate evidence.")
        seen_tables.add(table)
        if not isinstance(binding["kind"], str) or binding["kind"] not in KINDS:
            raise ValueError("Unknown customer source capability.")
        _temporal_mapping(binding)
        _measure_mapping(binding)
    if len(canonical(value).encode()) > 128_000:
        raise ValueError("Profile exceeds its size limit.")
    return json.loads(canonical(value))


def connections(profile):
    return sorted({source["connection"] for binding in profile["bindings"]
                   for source in [binding, *binding.get("attachments", [])]})


def observed_contract(profile, snapshot):
    """Bind required columns to observed types; unrelated columns can evolve."""
    observed = {table["table"]: table for source in snapshot["sources"] for table in source["tables"]}
    required_tables = {}
    for binding in profile["bindings"]:
        required = {binding["record_id"], binding["customer_ref"]["column"], *binding["fields"].values()}
        required.update(a["local_key"] for a in binding.get("attachments", []))
        required_tables.setdefault(binding["table"], set()).update(required)
        for attachment in binding.get("attachments", []):
            required_tables.setdefault(attachment["table"], set()).update({attachment["record_id"],
                attachment["remote_key"], attachment["customer_ref"]["column"], *attachment["fields"].values()})
    result = []
    for table, required in required_tables.items():
        if table not in observed:
            raise ValueError("A mapped table is absent from the committed source snapshot.")
        available = {c["name"]: c["type"] for c in observed[table]["columns"]}
        if not required <= available.keys():
            raise ValueError("A mapped column is absent from the committed source snapshot.")
        result.append({"table": table, "columns": [
            {"name": column, "type": available[column]} for column in sorted(required)]})
    for binding in profile["bindings"]:
        local_types = {c["name"]: c["type"] for c in observed[binding["table"]]["columns"]}
        for attachment in binding.get("attachments", []):
            remote_types = {c["name"]: c["type"] for c in observed[attachment["table"]]["columns"]}
            if local_types[attachment["local_key"]] != remote_types[attachment["remote_key"]]:
                raise ValueError("Attachment join keys require exactly matching observed SQL types.")
    return result
