"""Vendor-independent customer IDs and explicitly reviewed, append-only source links.

Names, domains and the descriptive basis never imply a match. Existing source
assignments cannot be replaced through this API; a changed identifier is a new
reviewed alias. Each review pins the entire mapping head observed at proposal.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import json
import re
import uuid

import duckdb

from ..workspace import canonical, digest


_KEY = ("connection", "entity", "source_id")
_LINK = {*_KEY, "customer_id", "basis"}


def _text(value, maximum, field):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise ValueError(f"Supply a nonempty bounded {field} string without control characters.")
    return value


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("Supply a canonical identity UUID.")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("Supply a canonical identity UUID.") from None
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _mapping_hash(conn, workspace_id, version):
    rows = conn.execute(
        "SELECT connection, entity, source_id, customer_id, basis, version, proposal_id "
        "FROM _contextbase.customer360_links ORDER BY connection, entity, source_id"
    ).fetchall()
    return digest({"workspace_id": workspace_id, "version": version, "links": rows})


def ensure_identity_schema(conn):
    """Create metadata in the caller's transaction and return its persistent head."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _contextbase")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_identity_state "
                 "(singleton INTEGER PRIMARY KEY CHECK (singleton=1), workspace_id VARCHAR NOT NULL, "
                 "version BIGINT NOT NULL, head_sha256 VARCHAR NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_customers "
                 "(id VARCHAR PRIMARY KEY, label VARCHAR NOT NULL, created_at VARCHAR NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_link_proposals "
                 "(proposal_id VARCHAR PRIMARY KEY, payload VARCHAR NOT NULL, "
                 "approval_sha256 VARCHAR NOT NULL, status VARCHAR NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_link_candidates "
                 "(proposal_id VARCHAR NOT NULL, ordinal INTEGER NOT NULL, connection VARCHAR NOT NULL, "
                 "entity VARCHAR NOT NULL, source_id VARCHAR NOT NULL, customer_id VARCHAR NOT NULL, "
                 "PRIMARY KEY (proposal_id, ordinal))")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_link_reviews "
                 "(proposal_id VARCHAR PRIMARY KEY, payload VARCHAR NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_links "
                 "(connection VARCHAR NOT NULL, entity VARCHAR NOT NULL, source_id VARCHAR NOT NULL, "
                 "customer_id VARCHAR NOT NULL, basis VARCHAR NOT NULL, version BIGINT NOT NULL, "
                 "proposal_id VARCHAR NOT NULL, PRIMARY KEY (connection, entity, source_id))")
    row = conn.execute("SELECT workspace_id, version, head_sha256 FROM "
                       "_contextbase.customer360_identity_state WHERE singleton=1").fetchone()
    if row is None:
        workspace_id = str(uuid.uuid4())
        head_hash = _mapping_hash(conn, workspace_id, 0)
        conn.execute("INSERT INTO _contextbase.customer360_identity_state VALUES (1, ?, 0, ?)",
                     [workspace_id, head_hash])
        row = (workspace_id, 0, head_hash)
    elif row[2] != _mapping_hash(conn, row[0], row[1]):
        raise ValueError("Customer identity mapping history does not match its recorded head.")
    return {"workspace_id": row[0], "mapping_version": row[1], "mapping_sha256": row[2]}


@contextmanager
def _transaction(workspace):
    with workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            head = ensure_identity_schema(conn)
            yield conn, head
            conn.commit()
        except duckdb.Error:
            conn.rollback()
            raise ValueError("Customer identity transaction could not commit; inspect the current mapping and retry review explicitly.") from None
        except BaseException:
            conn.rollback()
            raise


def create_customer(workspace, label):
    label = _text(label, 256, "customer label")
    customer_id = str(uuid.uuid4())
    with _transaction(workspace) as (conn, head):
        conn.execute("INSERT INTO _contextbase.customer360_customers VALUES (?, ?, ?)",
                     [customer_id, label, _now()])
    return {"id": customer_id, "label": label, "workspace_id": head["workspace_id"]}


def list_customers(workspace):
    with _transaction(workspace) as (conn, head):
        rows = conn.execute("SELECT id, label FROM _contextbase.customer360_customers ORDER BY label, id").fetchall()
    return [{"id": customer_id, "label": label, "workspace_id": head["workspace_id"]} for customer_id, label in rows]


def _links(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError("Propose between 1 and 1,000 explicit source links.")
    result = []
    for link in value:
        if not isinstance(link, dict) or set(link) != _LINK:
            raise ValueError("Each link requires connection, entity, source_id, customer_id and descriptive basis.")
        result.append({"connection": _text(link["connection"], 256, "connection"),
                       "entity": _text(link["entity"], 256, "entity"),
                       "source_id": _text(link["source_id"], 1024, "source ID"),
                       "customer_id": _uuid(link["customer_id"]),
                       "basis": _text(link["basis"], 2000, "mapping basis")})
    return result


def _conflicts(conn, links):
    seen, conflicts = set(), []
    for link in links:
        if not conn.execute("SELECT 1 FROM _contextbase.customer360_customers WHERE id=?", [link["customer_id"]]).fetchone():
            raise ValueError("A proposed link refers to a customer outside this workspace.")
        key = tuple(link[field] for field in _KEY)
        current = conn.execute("SELECT customer_id FROM _contextbase.customer360_links "
                               "WHERE connection=? AND entity=? AND source_id=?", list(key)).fetchone()
        if key in seen:
            conflicts.append({"source_key": dict(zip(_KEY, key)), "reason": "duplicate_source_key"})
        if current and current[0] != link["customer_id"]:
            conflicts.append({"source_key": dict(zip(_KEY, key)), "reason": "existing_assignment"})
        seen.add(key)
    return conflicts


def propose_links(workspace, links):
    links = _links(links)
    with _transaction(workspace) as (conn, head):
        conflicts = _conflicts(conn, links)
        proposal = {"spec": "contextbase.customer360-identity-proposal/v1", "proposal_id": str(uuid.uuid4()),
                    **head, "created_at": _now(), "links": links, "conflicts": conflicts,
                    "status": "conflict" if conflicts else "proposed"}
        approval_hash = digest(proposal)
        conn.execute("INSERT INTO _contextbase.customer360_link_proposals VALUES (?, ?, ?, ?)",
                     [proposal["proposal_id"], canonical(proposal), approval_hash, proposal["status"]])
        conn.executemany("INSERT INTO _contextbase.customer360_link_candidates VALUES (?, ?, ?, ?, ?, ?)",
                         [[proposal["proposal_id"], index, *[link[field] for field in _KEY], link["customer_id"]]
                          for index, link in enumerate(links)])
    return {**proposal, "approval_sha256": approval_hash}


def approve_links(workspace, proposal_id, approval_sha256, reviewer):
    proposal_id = _uuid(proposal_id)
    reviewer = _text(reviewer, 256, "reviewer")
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", approval_sha256):
        raise ValueError("Supply the exact proposal approval SHA-256.")
    with _transaction(workspace) as (conn, head):
        row = conn.execute("SELECT payload, approval_sha256 FROM _contextbase.customer360_link_proposals "
                           "WHERE proposal_id=?", [proposal_id]).fetchone()
        if not row:
            raise ValueError("Customer identity proposal does not exist in this workspace.")
        proposal = json.loads(row[0])
        if (not hmac.compare_digest(row[1], approval_sha256)
                or not hmac.compare_digest(digest(proposal), approval_sha256)):
            raise ValueError("Approval does not match the exact stored identity proposal.")
        if conn.execute("SELECT 1 FROM _contextbase.customer360_link_reviews WHERE proposal_id=?", [proposal_id]).fetchone():
            raise ValueError("This identity proposal was already reviewed.")
        if any(proposal[key] != head[key] for key in head):
            raise ValueError("Identity mapping changed after proposal; create and review a new proposal.")
        links = _links(proposal["links"])
        if proposal["status"] != "proposed" or proposal["conflicts"] or _conflicts(conn, links):
            raise ValueError("Conflicting identity links cannot be approved; review a new unambiguous proposal.")
        version = head["mapping_version"] + 1
        added = 0
        for link in links:
            key = [link[field] for field in _KEY]
            if not conn.execute("SELECT 1 FROM _contextbase.customer360_links "
                                "WHERE connection=? AND entity=? AND source_id=?", key).fetchone():
                conn.execute("INSERT INTO _contextbase.customer360_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [*key, link["customer_id"], link["basis"], version, proposal_id])
                added += 1
        new_hash = _mapping_hash(conn, head["workspace_id"], version)
        updated = conn.execute("UPDATE _contextbase.customer360_identity_state SET version=?, head_sha256=? "
                               "WHERE singleton=1 AND version=? AND head_sha256=? RETURNING singleton",
                               [version, new_hash, head["mapping_version"], head["mapping_sha256"]]).fetchone()
        if not updated:
            raise ValueError("Identity mapping changed during review; review again explicitly.")
        receipt = {"spec": "contextbase.customer360-identity-review/v1", "status": "approved",
                   "workspace_id": head["workspace_id"], "proposal_id": proposal_id,
                   "approval_sha256": approval_sha256, "reviewer": reviewer, "reviewed_at": _now(),
                   "mapping_version": version, "mapping_sha256": new_hash,
                   "previous_mapping_sha256": head["mapping_sha256"], "links_added": added}
        conn.execute("INSERT INTO _contextbase.customer360_link_reviews VALUES (?, ?)", [proposal_id, canonical(receipt)])
    return receipt


def identity_head(conn):
    """Read and verify an already initialized head in the caller's transaction."""
    row = conn.execute("SELECT workspace_id, version, head_sha256 FROM "
                       "_contextbase.customer360_identity_state WHERE singleton=1").fetchone()
    if row is None or row[2] != _mapping_hash(conn, row[0], row[1]):
        raise ValueError("Customer identity mapping history does not match its recorded head.")
    return {"workspace_id": row[0], "version": row[1], "sha256": row[2]}


def resolve_in_connection(conn, connection, entity, source_id):
    """Resolve against the caller's transaction; does not open or commit one."""
    key = [_text(connection, 256, "connection"), _text(entity, 256, "entity"), _text(source_id, 1024, "source ID")]
    head = identity_head(conn)
    row = conn.execute("SELECT customer_id, version, proposal_id FROM _contextbase.customer360_links "
                       "WHERE connection=? AND entity=? AND source_id=?", key).fetchone()
    result = {"workspace_id": head["workspace_id"], "mapping_version": head["version"],
              "mapping_sha256": head["sha256"], "source_key": dict(zip(_KEY, key)),
              "customer_id": None, "status": "unresolved"}
    if row:
        return {**result, "status": "resolved", "customer_id": row[0], "link_version": row[1], "proposal_id": row[2]}
    candidates = conn.execute(
        "SELECT DISTINCT c.customer_id, p.status FROM _contextbase.customer360_link_candidates c "
        "JOIN _contextbase.customer360_link_proposals p ON p.proposal_id=c.proposal_id "
        "LEFT JOIN _contextbase.customer360_link_reviews r ON r.proposal_id=c.proposal_id "
        "WHERE c.connection=? AND c.entity=? AND c.source_id=? AND r.proposal_id IS NULL", key
    ).fetchall()
    result["candidate_count"] = len({row[0] for row in candidates})
    if result["candidate_count"] > 1 or any(row[1] == "conflict" for row in candidates):
        result["status"] = "conflict"
    return result


def resolve(workspace, connection, entity, source_id):
    with _transaction(workspace) as (conn, _):
        return resolve_in_connection(conn, connection, entity, source_id)
