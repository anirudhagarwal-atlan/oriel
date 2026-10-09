"""Deterministic logical evidence for a published DuckDB base table.

This observes local data only. It neither contacts a source nor establishes
source completeness. Rows are streamed in SQL value order with binary text
collation and representation tie-breaks. No rows are returned or logged.
Hashes bind column order/types/nullability and duplicate row counts.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import uuid

from ..context import _identifier


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _value(value):
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite table values cannot be captured as source evidence.")
        return ["decimal", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite table values cannot be captured as source evidence.")
        return ["float", value.hex()]
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat()]
    if isinstance(value, timedelta):
        return ["interval", str((value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds)]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, (list, tuple)):
        return ["list", [_value(item) for item in value]]
    if isinstance(value, dict):
        items = [[_value(key), _value(item)] for key, item in value.items()]
        # DuckDB preserves STRUCT field and MAP entry order. Map order remains
        # observable through map_entries/map_keys and is part of this evidence.
        return ["object", items]
    raise ValueError("Unsupported table value type for source snapshot evidence.")


def observe_table(conn, schema, table):
    """Return schema/content hashes, streaming rows without exporting data.

    The caller owns the connection and transaction. Temporary SQL ordering may
    use DuckDB's configured memory/storage; this helper creates no artifacts.
    """
    relation = f"{_identifier(schema)}.{_identifier(table)}"
    found = conn.execute("SELECT table_type FROM information_schema.tables WHERE "
                         "table_catalog=current_database() AND table_schema=? AND table_name=?", [schema, table]).fetchall()
    if found != [("BASE TABLE",)]:
        raise ValueError("Source snapshot evidence requires an existing owned base table.")
    columns = [{"name": name, "type": dtype, "nullable": nullable == "YES"} for name, dtype, nullable in
               conn.execute("SELECT column_name,data_type,is_nullable FROM information_schema.columns WHERE "
                            "table_catalog=current_database() AND table_schema=? AND table_name=? "
                            "ORDER BY ordinal_position", [schema, table]).fetchall()]
    if not columns:
        raise ValueError("Source snapshot evidence requires a nonempty table schema.")
    # Python fetch loses nanoseconds, interval month components and UNION tags.
    # Preserve the first two scalar forms as SQL text. Nested forms and unions
    # are refused rather than silently producing incomplete value evidence.
    for column in columns:
        dtype = column["type"]
        if ("UNION(" in dtype or ("TIMESTAMP_NS" in dtype and dtype != "TIMESTAMP_NS")
                or ("INTERVAL" in dtype and dtype != "INTERVAL")):
            raise ValueError("Unsupported precision or union table type for source snapshot evidence.")
    schema_sha256 = hashlib.sha256(_canonical(columns).encode()).hexdigest()
    expressions, order, representations = [], [], []
    for column in columns:
        name, dtype = _identifier(column["name"]), column["type"]
        expressions.append(f"CAST({name} AS VARCHAR)" if dtype in {"TIMESTAMP_NS", "INTERVAL"} else name)
        order.append(name + (' COLLATE "binary"' if dtype in {"VARCHAR", "CHAR", "TEXT", "JSON"} else "") + " ASC NULLS FIRST")
        if dtype == "INTERVAL" or any(token in dtype for token in ("TIME WITH TIME ZONE", "FLOAT", "DOUBLE")):
            # Equal SQL interval/time values can retain distinct month/offset
            # representations (including signed zero); use a deterministic tie.
            order.append(f'CAST({name} AS VARCHAR) COLLATE "binary" ASC NULLS FIRST')
        if any(token in dtype for token in ("FLOAT", "DOUBLE")):
            # DuckDB's sorter can canonicalize -0.0 in a returned sort key.
            # Retain its pre-sort SQL representation as additional evidence.
            representations.append(f"CAST({name} AS VARCHAR)")
    expressions.extend(representations)
    cursor = conn.execute(f"SELECT {','.join(expressions)} FROM {relation} ORDER BY {','.join(order)}")
    hasher = hashlib.sha256(b"contextbase.logical-table/v1\0" + schema_sha256.encode() + b"\0")
    count = 0
    while rows := cursor.fetchmany(512):
        for row in rows:
            encoded = _canonical([_value(item) for item in row]).encode()
            hasher.update(len(encoded).to_bytes(8, "big"))
            hasher.update(encoded)
            count += 1
    hasher.update(b"\0rows\0" + str(count).encode())
    return {"table": schema + "." + table, "rows": count, "columns": columns,
            "schema_sha256": schema_sha256, "content_sha256": hasher.hexdigest()}
