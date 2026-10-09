"""Durable local refresh attempts and a fail-closed snapshot read boundary.

This records the latest observed refresh, not live source freshness. Historical
tables remain on disk after failure; only the normal analytics boundary blocks
their use until a complete materialization succeeds. No network calls occur.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

REFRESH_STATUS_TOOL = {
    "name": "refresh_status",
    "description": "Inspect the latest recorded refresh attempt, source-validation blockers and last successful snapshot. This does not contact a source or prove real-time freshness. A failed or incomplete required refresh blocks local snapshot analytics; direct-source reads remain separate.",
    "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}
TABLE = "_contextbase.refresh_attempts"
OUTCOMES = {"in_progress", "materialized", "refused_invalid_source", "unverified", "storage_failed", "superseded"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _exists(conn, table):
    schema, name = table.split(".")
    return bool(conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                             [schema, name]).fetchone()[0])


def _snapshot(conn):
    if not _exists(conn, "_contextbase.rystic_snapshots"):
        return None
    row = conn.execute("SELECT payload FROM _contextbase.rystic_snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def _latest(conn):
    if not _exists(conn, TABLE):
        return None
    # Validate every stored position before selecting the head. Checking only
    # the maximum row could hide a newer failure whose sequence was corrupted.
    latest = None
    rows = conn.execute("SELECT sequence,attempt_id,payload FROM _contextbase.refresh_attempts ORDER BY sequence").fetchall()
    for position, (sequence, attempt_id, payload) in enumerate(rows, 1):
        attempt = json.loads(payload)
        if not isinstance(attempt, dict) or attempt.get("outcome") not in OUTCOMES:
            raise ValueError("Recorded refresh metadata is invalid; inspect the local workspace before querying.")
        if (type(attempt.get("sequence")) is not int or attempt["sequence"] != sequence
                or sequence != position or attempt.get("attempt_id") != attempt_id):
            raise ValueError("Recorded refresh ordering changed; inspect the local workspace before querying.")
        digest = attempt.get("attempt_sha256")
        body = {key: value for key, value in attempt.items() if key != "attempt_sha256"}
        if digest != hashlib.sha256(_encode(body).encode()).hexdigest():
            raise ValueError("Recorded refresh metadata changed; inspect the local workspace before querying.")
        latest = attempt
    return latest


def _sealed(attempt):
    result = deepcopy(attempt)
    result.pop("attempt_sha256", None)
    result["attempt_sha256"] = hashlib.sha256(_encode(result).encode()).hexdigest()
    return result


def refresh_status(workspace, *, connection=None):
    """Return only recorded evidence; generic untracked workspaces stay usable."""
    latest = snapshot = None
    if connection is not None:
        latest, snapshot = _latest(connection), _snapshot(connection)
    elif workspace.db_path.exists():
        with workspace._connect() as conn:
            latest, snapshot = _latest(conn), _snapshot(conn)
    blocked = latest is not None and latest["outcome"] != "materialized"
    if latest and latest["outcome"] == "materialized":
        materialized = latest.get("materialized_snapshot")
        if not isinstance(materialized, dict) or materialized != snapshot:
            blocked = True
    return {"status": "blocked" if blocked else "recorded_snapshot" if snapshot else "untracked",
            "currentness": "latest_observed_state_only", "source_contacted": False,
            "snapshot_reads_allowed": not blocked, "latest_attempt": latest,
            "last_successful_snapshot": snapshot,
            "historical_data_policy": "Failed refreshes preserve existing tables but block normal snapshot analytics until a verified full refresh succeeds."}


def require_readable_snapshot(workspace, *, connection=None):
    if not refresh_status(workspace, connection=connection)["snapshot_reads_allowed"]:
        raise ValueError("The latest required refresh did not produce a verified current snapshot. Historical rows are preserved; inspect refresh_status, resolve the source problem, and complete a new refresh before querying.")


def begin_refresh(workspace, namespace):
    """Record intent before source access; interrupted attempts remain blocking.

    A new attempt supersedes an interrupted/running older attempt. The old
    caller cannot commit later: completion checks the latest attempt identity
    inside the same transaction as materialization.
    """
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", namespace):
        raise ValueError("Invalid refresh namespace.")
    with workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            previous = _snapshot(conn)
            if previous and previous.get("namespace") != namespace:
                raise ValueError("Workspace source namespace differs; use a new workspace.")
            conn.execute("CREATE SCHEMA IF NOT EXISTS _contextbase")
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.refresh_attempts (sequence BIGINT, attempt_id VARCHAR PRIMARY KEY, payload VARCHAR)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS refresh_attempt_sequence ON _contextbase.refresh_attempts(sequence)")
            latest = _latest(conn)
            if latest and latest["outcome"] == "in_progress":
                latest.update(outcome="superseded", finished_at=_now(), error_code="new_refresh_attempt")
                latest = _sealed(latest)
                conn.execute("UPDATE _contextbase.refresh_attempts SET payload=? WHERE attempt_id=?", [_encode(latest), latest["attempt_id"]])
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM _contextbase.refresh_attempts").fetchone()[0]
            attempt = _sealed({"spec": "contextbase.refresh-attempt/v1", "attempt_id": uuid.uuid4().hex, "sequence": sequence,
                               "namespace": namespace, "outcome": "in_progress", "started_at": _now(),
                               "finished_at": None, "validation": None, "error_code": None,
                               "previous_snapshot_id": previous.get("snapshot_id") if previous else None,
                               "materialized_snapshot": None})
            conn.execute("INSERT INTO _contextbase.refresh_attempts VALUES (?,?,?)", [sequence, attempt["attempt_id"], _encode(attempt)])
            conn.commit()
            return attempt
        except Exception:
            conn.rollback()
            raise


def _finish(conn, attempt_id, *, outcome, validation=None, materialized_snapshot=None):
    """Called inside the caller's transaction; successful imports commit together."""
    if outcome not in {"materialized", "refused_invalid_source", "unverified", "storage_failed"}:
        raise ValueError("Invalid refresh completion outcome.")
    latest = _latest(conn)
    if not latest or latest["attempt_id"] != attempt_id or latest["outcome"] != "in_progress":
        raise ValueError("Refresh attempt was completed or superseded; refusing stale completion.")
    if validation is not None:
        if (not isinstance(validation, dict) or validation.get("receipt_kind") != "source_state_validation"
                or validation.get("namespace") != latest["namespace"]):
            raise ValueError("Refresh completion requires matching source validation evidence.")
        _encode(validation)
    expected = {"materialized": "valid", "refused_invalid_source": "blocked"}.get(outcome)
    if expected and (not validation or validation.get("validation_status") != expected or validation.get("complete") is not True):
        raise ValueError("Refresh completion lacks complete matching validation evidence.")
    if outcome == "materialized":
        snapshot = _snapshot(conn)
        if (not isinstance(materialized_snapshot, dict) or not snapshot or snapshot != materialized_snapshot
                or snapshot.get("namespace") != latest["namespace"] or snapshot.get("refresh_mode") != "full_cohort_replace"
                or snapshot.get("data_sha256") != validation.get("data_sha256")
                or snapshot.get("snapshot_id") != validation.get("snapshot_id")):
            raise ValueError("Verified source and committed materialization do not match.")
    elif materialized_snapshot is not None:
        raise ValueError("A failed refresh cannot claim a materialized snapshot.")
    latest.update(outcome=outcome, finished_at=_now(), validation=deepcopy(validation),
                  materialized_snapshot=deepcopy(materialized_snapshot),
                  error_code=None if outcome == "materialized" else outcome)
    latest = _sealed(latest)
    conn.execute("UPDATE _contextbase.refresh_attempts SET payload=? WHERE attempt_id=?", [_encode(latest), attempt_id])
    return latest


def finish_failed_refresh(workspace, attempt_id, *, outcome, validation=None):
    if outcome not in {"refused_invalid_source", "unverified", "storage_failed"}:
        raise ValueError("Use atomic source materialization to complete a successful refresh.")
    with workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            result = _finish(conn, attempt_id, outcome=outcome, validation=validation)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
