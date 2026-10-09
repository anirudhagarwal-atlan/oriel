"""Review declarative customer mappings and project committed source evidence.

Mapping review is a local operator assertion, not authentication or business
metric certification. Projection cannot call a vendor or approve an identity.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json

from ..context import _identifier
from .contracts import canonical, sha256, validate_profile, observed_contract, source_id, text, name, connections
from .identity import ensure_identity_schema, identity_head, resolve_in_connection
from .snapshots import readable_snapshot

MAX_ROWS_PER_BINDING = 10_000
MAX_PROJECTED_BYTES = 20_000_000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure(conn):
    ensure_identity_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_profile_candidates "
                 "(proposal_id VARCHAR PRIMARY KEY, payload VARCHAR)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_profile_approvals "
                 "(name VARCHAR, version INTEGER, proposal_id VARCHAR UNIQUE, payload VARCHAR, PRIMARY KEY(name,version))")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_materializations "
                 "(materialization_id VARCHAR PRIMARY KEY, payload VARCHAR)")


def _latest(conn, profile_name):
    row = conn.execute("SELECT payload FROM _contextbase.customer360_profile_approvals "
                       "WHERE name=? ORDER BY version DESC LIMIT 1", [profile_name]).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    proof = value.pop("approval_sha256")
    if sha256(value) != proof:
        raise ValueError("Stored profile approval changed; inspect the workspace.")
    value["approval_sha256"] = proof
    validate_profile(value["candidate"]["profile"])
    return value


def _pin_identity(conn):
    head = identity_head(conn)
    # Touch the same singleton that identity review updates so a concurrent
    # mapping commit conflicts instead of approving a stale observed head.
    conn.execute("UPDATE _contextbase.customer360_identity_state SET head_sha256=head_sha256 WHERE singleton=1")
    return head


def _read_mapping_rows(conn, source_table, columns, remaining_bytes):
    columns = sorted(columns)
    schema, table = source_table.split(".")
    observed_types = dict(conn.execute("SELECT column_name,data_type FROM information_schema.columns WHERE "
                                       "table_catalog=current_database() AND table_schema=? AND table_name=?",
                                       [schema, table]).fetchall())
    if not set(columns) <= observed_types.keys():
        raise ValueError("A mapped column is absent from the committed source snapshot.")
    # Retain the original exact scalar representations used by legacy bindings.
    selected_columns = [f"CAST({_identifier(c)} AS VARCHAR) AS {_identifier(c)}"
                        if observed_types.get(c) in {"TIMESTAMP_NS", "INTERVAL"} else _identifier(c)
                        for c in columns]
    selected = f"SELECT {','.join(selected_columns)} FROM {_identifier(schema)}.{_identifier(table)}"
    n, maximum, total = conn.execute(
        "SELECT count(*), coalesce(max(n),0), coalesce(sum(n),0) FROM "
        f"(SELECT octet_length(encode(to_json(p))) n FROM ({selected}) p) sizes"
    ).fetchone()
    if n > MAX_ROWS_PER_BINDING or maximum > 64_000 or total > remaining_bytes:
        raise ValueError("Customer evidence exceeds the bounded projection size; select fewer fields or resources.")
    values = conn.execute(selected + " LIMIT ?", [MAX_ROWS_PER_BINDING + 1]).fetchall()
    if len(values) > MAX_ROWS_PER_BINDING:
        raise ValueError("Customer projection exceeds the configured per-binding row limit.")
    return [dict(zip(columns, row)) for row in values], observed_types, total


def _join_key(value, sql_type):
    if value is None:
        return None
    if type(value) not in (str, int):
        raise ValueError("Attachment keys require bounded exact string or integer values.")
    source_id(value)
    return sql_type, type(value).__name__, value


def _resolve_ref(conn, ref, row):
    value = row[ref["column"]]
    if value is None:
        return {"status": "unresolved", "customer_id": None}
    return resolve_in_connection(conn, ref["connection"], ref["entity"], source_id(value))


def _attachment_indexes(conn, binding, local_types, remaining_bytes):
    indexes, used = [], 0
    for attachment in binding.get("attachments", []):
        columns = {attachment["record_id"], attachment["remote_key"],
                   attachment["customer_ref"]["column"], *attachment["fields"].values()}
        rows, types, size = _read_mapping_rows(conn, attachment["table"], columns, remaining_bytes - used)
        used += size
        key_type = types[attachment["remote_key"]]
        if local_types[attachment["local_key"]] != key_type:
            raise ValueError("Attachment join keys require exactly matching observed SQL types.")
        by_key, record_ids = {}, set()
        for row in rows:
            identifier = source_id(row[attachment["record_id"]])
            if identifier in record_ids:
                raise ValueError("An attachment has duplicate record IDs; resolve its grain before projection.")
            record_ids.add(identifier)
            key = _join_key(row[attachment["remote_key"]], key_type)
            if key in by_key:
                raise ValueError("An attachment has duplicate remote keys; resolve its grain before projection.")
            by_key[key] = row
        indexes.append((attachment, key_type, by_key))
    return indexes, used


def _attached(conn, indexes, row, resolved, snapshot_id):
    attached, key_hashes = {}, {}
    for attachment, key_type, by_key in indexes:
        local_value = row[attachment["local_key"]]
        key = _join_key(local_value, key_type)
        key_hashes[attachment["name"]] = sha256({"type": key_type, "value": local_value})
        remote = by_key.get(key) if key is not None else None
        metadata = {"connection": attachment["connection"], "entity": attachment["entity"],
                    "source_table": attachment["table"], "snapshot_id": snapshot_id}
        reason = "missing_local_key" if key is None else "no_matching_row" if remote is None else None
        if remote is not None:
            remote_customer = _resolve_ref(conn, attachment["customer_ref"], remote)
            if resolved["status"] != "resolved" or remote_customer["status"] != "resolved":
                reason = "unresolved_customer"
            elif resolved["customer_id"] != remote_customer["customer_id"]:
                raise ValueError("Attachment customer does not match the independently resolved base customer.")
        if reason:
            # Do not expose even a selected foreign/unresolved row ID or value.
            attached[attachment["name"]] = {"status": "unknown", "reason": reason, **metadata}
            continue
        ref = attachment["customer_ref"]
        attached[attachment["name"]] = {
            "status": "matched", **metadata,
            "fields": {role: remote[column] for role, column in attachment["fields"].items()},
            "source_id": source_id(remote[attachment["record_id"]]),
            "source_row_sha256": sha256(remote),
            "local_key": {"column": attachment["local_key"], "type": key_type, "value": local_value},
            "remote_key": {"column": attachment["remote_key"], "type": key_type, "value": remote[attachment["remote_key"]]},
            "customer_ref": {**ref, "value": remote[ref["column"]]},
        }
    return attached, key_hashes


def _project(conn, profile, snapshot_id):
    records, byte_count, source_bytes, coverage = [], 0, 0, []
    head = identity_head(conn)
    for binding in profile["bindings"]:
        columns = {binding["record_id"], binding["customer_ref"]["column"], *binding["fields"].values()}
        columns.update(a["local_key"] for a in binding.get("attachments", []))
        rows, types, size = _read_mapping_rows(conn, binding["table"], columns, MAX_PROJECTED_BYTES - source_bytes)
        source_bytes += size
        indexes, size = _attachment_indexes(conn, binding, types, MAX_PROJECTED_BYTES - source_bytes)
        source_bytes += size
        seen, counts = set(), Counter()
        for row in rows:
            key = source_id(row[binding["record_id"]])
            if key in seen:
                raise ValueError("A source binding has duplicate record IDs; resolve its grain before projection.")
            seen.add(key)
            ref = binding["customer_ref"]
            ref_value = row[ref["column"]]
            resolved = _resolve_ref(conn, ref, row)
            counts[resolved["status"]] += 1
            fields = {role: row[column] for role, column in binding["fields"].items()}
            record_key = sha256([head["workspace_id"], binding["connection"], binding["entity"], key])
            content = {"fields": fields, "source_id": key, "customer_ref": ref_value}
            attachments, attachment_keys = _attached(conn, indexes, row, resolved, snapshot_id)
            if indexes:
                # Snapshot lineage remains on the enclosing record/receipt. A
                # fresh capture of unchanged source content keeps its evidence
                # identity, while selected attachment or join changes do not.
                content["attachments"] = {label: {k: v for k, v in value.items() if k != "snapshot_id"}
                                          for label, value in attachments.items()}
                content["attachment_keys_sha256"] = attachment_keys
            projected_size = len(canonical(content if indexes else fields).encode())
            byte_count += projected_size
            if byte_count > MAX_PROJECTED_BYTES or projected_size > 64_000:
                raise ValueError("Customer evidence exceeds the bounded projection size; select fewer fields or resources.")
            record = {"record_key": record_key, "evidence_id": sha256([record_key, sha256(content)]),
                      "connection": binding["connection"], "entity": binding["entity"], "source_id": key,
                      "kind": binding["kind"], "customer_id": resolved.get("customer_id"),
                      "link_status": resolved["status"], "fields": fields,
                      "source_row_sha256": sha256(content), "source_table": binding["table"],
                      "snapshot_id": snapshot_id}
            if indexes:
                record["attachments"] = attachments
            records.append(json.loads(canonical(record)))
        coverage.append({"connection": binding["connection"], "entity": binding["entity"],
                         "kind": binding["kind"], "records": len(rows), "identity": dict(sorted(counts.items()))})
    records.sort(key=lambda record: record["record_key"])
    return records, coverage


def propose_profile(workspace, profile, snapshot_id):
    profile = validate_profile(profile)
    with readable_snapshot(workspace, snapshot_id) as (conn, snapshot):
        if connections(profile) != sorted(source["name"] for source in snapshot["sources"]):
            raise ValueError("The snapshot must contain exactly the profile's selected connections.")
        _ensure(conn)
        _pin_identity(conn)
        contract = observed_contract(profile, snapshot)
        records, coverage = _project(conn, profile, snapshot_id)
        previous = _latest(conn, profile["name"])
        candidate = {"spec_version": "oriel.customer360.mapping-proposal/v1", "profile": profile,
                     "version": previous["candidate"]["version"] + 1 if previous else 1,
                     "snapshot_id": snapshot_id, "identity": identity_head(conn),
                     "schema_contract": contract, "coverage": coverage,
                     "projection_sha256": sha256(records),
                     "previous_approval": previous["approval_sha256"] if previous else None,
                     "created_at": _now()}
        proof = sha256(candidate)
        conn.execute("INSERT INTO _contextbase.customer360_profile_candidates VALUES (?,?)", [proof, canonical(candidate)])
    return {"proposal_id": proof, "approval_sha256": proof, "status": "candidate", **candidate}


def approve_profile(workspace, proposal_id, approval_sha256, reviewer):
    text(reviewer, "reviewer", maximum=128)
    if not isinstance(proposal_id, str) or len(proposal_id) != 64 or approval_sha256 != proposal_id:
        raise ValueError("Approval requires the exact reviewed proposal hash.")
    with workspace._connect() as conn:
        row = conn.execute("SELECT payload FROM _contextbase.customer360_profile_candidates WHERE proposal_id=?", [proposal_id]).fetchone()
    if row is None:
        raise ValueError("Unknown customer profile proposal.")
    candidate = json.loads(row[0])
    if sha256(candidate) != proposal_id:
        raise ValueError("Customer profile proposal changed; review a new proposal.")
    profile = validate_profile(candidate["profile"])
    with readable_snapshot(workspace, candidate["snapshot_id"]) as (conn, snapshot):
        _ensure(conn)
        _pin_identity(conn)
        previous = _latest(conn, profile["name"])
        if (previous["approval_sha256"] if previous else None) != candidate["previous_approval"]:
            raise ValueError("The approved profile changed; create a fresh proposal.")
        if identity_head(conn) != candidate["identity"] or observed_contract(profile, snapshot) != candidate["schema_contract"]:
            raise ValueError("Identity or schema changed after the proposed mapping; review again.")
        records, coverage = _project(conn, profile, candidate["snapshot_id"])
        if sha256(records) != candidate["projection_sha256"] or coverage != candidate["coverage"]:
            raise ValueError("The proposed customer projection changed; review again.")
        approved = {"spec_version": "oriel.customer360.mapping-approval/v1", "candidate": candidate,
                    "proposal_id": proposal_id, "reviewer": reviewer, "reviewed_at": _now(),
                    "approval_kind": "local_operator_mapping_review"}
        approved["approval_sha256"] = sha256(approved)
        conn.execute("INSERT INTO _contextbase.customer360_profile_approvals VALUES (?,?,?,?)",
                     [profile["name"], candidate["version"], proposal_id, canonical(approved)])
    return approved


def load_profile(workspace, profile_name):
    name(profile_name)
    with workspace._connect() as conn:
        value = _latest(conn, profile_name)
    if value is None:
        raise ValueError("No reviewed customer profile with this name.")
    return value


def _check_receipt_integrity(receipt):
    fields = {"spec_version", "materialization_id", "profile", "profile_approval", "snapshot_id", "identity",
              "records_sha256", "profile_version", "coverage", "capabilities", "row_count", "created_at",
              "meaning", "receipt_sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or receipt["receipt_sha256"] != sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})):
        raise ValueError("Customer materialization receipt changed or lacks its integrity hash; inspect the workspace.")


def _check_receipt_metadata(receipt, approved, basis, records):
    """Cross-check the sealed receipt against reviewed mapping and row evidence."""
    _check_receipt_integrity(receipt)
    profile = approved["candidate"]["profile"]
    bindings = {(binding["connection"], binding["entity"]): binding for binding in profile["bindings"]}
    counts = {key: Counter() for key in bindings}
    for record in records:
        key = (record["connection"], record["entity"])
        binding = bindings.get(key)
        if binding is None or record["kind"] != binding["kind"] or record["source_table"] != binding["table"]:
            raise ValueError("Customer evidence does not match its reviewed source mapping.")
        counts[key][record["link_status"]] += 1
    coverage = [{"connection": binding["connection"], "entity": binding["entity"], "kind": binding["kind"],
                 "records": sum(counts[(binding["connection"], binding["entity"])].values()),
                 "identity": dict(sorted(counts[(binding["connection"], binding["entity"])].items()))}
                for binding in profile["bindings"]]
    expected = {"spec_version": "oriel.customer360.materialization/v1", "materialization_id": sha256(basis),
                **basis, "profile_version": approved["candidate"]["version"], "coverage": coverage,
                "capabilities": sorted({binding["kind"] for binding in profile["bindings"]}),
                "row_count": len(records), "meaning": "reviewed_field_mappings; business_metrics_not_certified"}
    actual = {key: receipt[key] for key in expected}
    if canonical(actual) != canonical(expected):
        raise ValueError("Customer materialization receipt metadata disagrees with its approved profile or evidence records.")


def materialize(workspace, profile_name, snapshot_id):
    name(profile_name)
    with readable_snapshot(workspace, snapshot_id) as (conn, snapshot):
        _ensure(conn)
        _pin_identity(conn)
        approved = _latest(conn, profile_name)
        if approved is None:
            raise ValueError("Review the customer profile before materializing it.")
        candidate = approved["candidate"]
        profile = candidate["profile"]
        if connections(profile) != sorted(source["name"] for source in snapshot["sources"]):
            raise ValueError("The snapshot does not match this profile's connections.")
        if observed_contract(profile, snapshot) != candidate["schema_contract"]:
            raise ValueError("Mapped column types changed; create and review a revised profile.")
        records, coverage = _project(conn, profile, snapshot_id)
        basis = {"profile": profile_name, "profile_approval": approved["approval_sha256"],
                 "snapshot_id": snapshot_id, "identity": identity_head(conn), "records_sha256": sha256(records)}
        materialization_id = sha256(basis)
        receipt = {"spec_version": "oriel.customer360.materialization/v1", "materialization_id": materialization_id,
                   **basis, "profile_version": candidate["version"], "coverage": coverage,
                   "capabilities": sorted({b["kind"] for b in profile["bindings"]}),
                   "row_count": len(records), "created_at": _now(),
                   "meaning": "reviewed_field_mappings; business_metrics_not_certified"}
        receipt["receipt_sha256"] = sha256(receipt)
        found = conn.execute("SELECT payload FROM _contextbase.customer360_materializations WHERE materialization_id=?", [materialization_id]).fetchone()
        if found:
            receipt = json.loads(found[0])
            _check_receipt_metadata(receipt, approved, basis, records)
            history = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM _contextbase.customer360_record_history WHERE materialization_id=? ORDER BY record_key",
                [materialization_id]).fetchall()]
            if canonical(history) != canonical(records):
                raise ValueError("Customer evidence history no longer matches its receipt.")
        else:
            _check_receipt_metadata(receipt, approved, basis, records)
        conn.execute("CREATE SCHEMA IF NOT EXISTS customer360")
        conn.execute("CREATE TABLE IF NOT EXISTS customer360.records "
                     "(profile VARCHAR,record_key VARCHAR,customer_id VARCHAR,kind VARCHAR,connection VARCHAR,"
                     "entity VARCHAR,source_id VARCHAR,evidence_id VARCHAR,link_status VARCHAR,fields JSON,"
                     "snapshot_id VARCHAR,profile_version INTEGER,materialization_id VARCHAR,PRIMARY KEY(profile,record_key))")
        conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_record_history "
                     "(materialization_id VARCHAR,record_key VARCHAR,payload VARCHAR,PRIMARY KEY(materialization_id,record_key))")
        conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_projection_heads "
                     "(profile VARCHAR PRIMARY KEY,materialization_id VARCHAR)")
        conn.execute("DELETE FROM customer360.records WHERE profile=?", [profile_name])
        if records:
            conn.executemany("INSERT INTO customer360.records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                [profile_name, r["record_key"], r["customer_id"], r["kind"], r["connection"], r["entity"],
                 r["source_id"], r["evidence_id"], r["link_status"], canonical(r["fields"]),
                 snapshot_id, candidate["version"], materialization_id] for r in records])
            conn.executemany("INSERT OR IGNORE INTO _contextbase.customer360_record_history VALUES (?,?,?)",
                             [[materialization_id, r["record_key"], canonical(r)] for r in records])
        if not found:
            conn.execute("INSERT INTO _contextbase.customer360_materializations VALUES (?,?)", [materialization_id, canonical(receipt)])
        conn.execute("INSERT OR REPLACE INTO _contextbase.customer360_projection_heads VALUES (?,?)", [profile_name, materialization_id])
    return receipt


@contextmanager
def readable_profile(workspace, profile_name, snapshot_id):
    """Yield validated profile evidence and its still-open source transaction.

    The tuple is (connection, snapshot, approved profile, receipt, payload).
    This is a trusted local operator interface, not a SQL authorization layer.
    Consumers must validate their queries and must not commit, close or replace
    the connection. Source locks stay held through successful commit or rollback.
    """
    name(profile_name)
    with readable_snapshot(workspace, snapshot_id) as (conn, snapshot):
        tables = set(conn.execute("SELECT table_schema,table_name FROM information_schema.tables "
                                  "WHERE table_catalog=current_database()").fetchall())
        if ("_contextbase", "customer360_profile_approvals") not in tables:
            raise ValueError("No approved customer profile.")
        approved = _latest(conn, profile_name)
        if approved is None:
            raise ValueError("No approved customer profile.")
        if ("_contextbase", "customer360_projection_heads") not in tables:
            raise ValueError("Materialize the current customer snapshot before reading evidence.")
        current = conn.execute("SELECT materialization_id FROM _contextbase.customer360_projection_heads WHERE profile=?", [profile_name]).fetchone()
        if current is None:
            raise ValueError("Materialize the current customer snapshot before reading evidence.")
        required = {("_contextbase", "customer360_materializations"), ("_contextbase", "customer360_record_history"),
                    ("customer360", "records")}
        if not required <= tables:
            raise ValueError("Customer materialization receipt, history or projection is missing.")
        saved = conn.execute("SELECT payload FROM _contextbase.customer360_materializations WHERE materialization_id=?", [current[0]]).fetchone()
        if saved is None:
            raise ValueError("Customer materialization receipt is missing.")
        receipt = json.loads(saved[0])
        _check_receipt_integrity(receipt)
        if receipt["snapshot_id"] != snapshot_id or receipt["profile_approval"] != approved["approval_sha256"] or receipt["identity"] != identity_head(conn):
            raise ValueError("Customer context changed; rebuild the projection before reading evidence.")
        history = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM _contextbase.customer360_record_history "
                   "WHERE materialization_id=? ORDER BY record_key", [current[0]]).fetchall()]
        basis = {"profile": profile_name, "profile_approval": approved["approval_sha256"],
                 "snapshot_id": snapshot_id, "identity": identity_head(conn), "records_sha256": sha256(history)}
        if sha256(basis) != current[0] or sha256(history) != receipt["records_sha256"]:
            raise ValueError("Customer evidence history no longer matches its receipt.")
        _check_receipt_metadata(receipt, approved, basis, history)
        actual = [list(r) for r in conn.execute("SELECT * FROM customer360.records WHERE profile=? ORDER BY record_key", [profile_name]).fetchall()]
        for row in actual:
            row[9] = json.loads(row[9])
        expected = [[profile_name,r["record_key"],r["customer_id"],r["kind"],r["connection"],r["entity"],r["source_id"],
                     r["evidence_id"],r["link_status"],r["fields"],snapshot_id,receipt["profile_version"],current[0]] for r in history]
        if (json.dumps(actual, sort_keys=True, separators=(",", ":"), allow_nan=False)
                != json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)):
            raise ValueError("Customer projection changed after materialization; rebuild it from source evidence.")
        payload = {"profile": profile_name, "snapshot_id": snapshot_id,
                   "profile_version": receipt["profile_version"], "profile_approval": receipt["profile_approval"],
                   "materialization_id": receipt["materialization_id"], "receipt_sha256": receipt["receipt_sha256"],
                   "capabilities": receipt["capabilities"], "coverage": receipt["coverage"],
                   "source_observed_at": {s["name"]: s["receipt"]["finished_at"] for s in snapshot["sources"]},
                   "scope": "trusted_local_operator_selected_profile", "records": history}
        yield conn, snapshot, approved, receipt, payload


@contextmanager
def readable_customer(workspace, profile_name, customer_id, snapshot_id):
    """Yield one customer's evidence after full profile and membership checks.

    Shares readable_profile's five-tuple and trust boundary. The payload keeps
    records_for_customer's existing response shape; full profile and receipt
    context are available separately in the tuple. This helper does not make
    arbitrary SQL safe or impose row-level permissions on the local operator.
    """
    text(customer_id, "customer ID", maximum=128)
    with readable_profile(workspace, profile_name, snapshot_id) as (conn, snapshot, approved, receipt, profile_payload):
        if not conn.execute("SELECT 1 FROM _contextbase.customer360_customers WHERE id=?", [customer_id]).fetchone():
            raise ValueError("Select a customer belonging to this workspace.")
        record_fields = ("record_key", "evidence_id", "kind", "connection", "entity", "source_id", "fields")
        payload = {"customer_id": customer_id, "snapshot_id": snapshot_id,
                   "profile_version": receipt["profile_version"], "capabilities": receipt["capabilities"],
                   "source_observed_at": profile_payload["source_observed_at"], "coverage": receipt["coverage"],
                   "records": [{**{key: record[key] for key in record_fields},
                                **({"attachments": record["attachments"]} if "attachments" in record else {})}
                               for record in profile_payload["records"] if record["customer_id"] == customer_id]}
        yield conn, snapshot, approved, receipt, payload


def records_for_customer(workspace, profile_name, customer_id, snapshot_id):
    """Read only the current reviewed projection, checking freshness each time."""
    with readable_customer(workspace, profile_name, customer_id, snapshot_id) as (_, _, _, _, payload):
        return payload
