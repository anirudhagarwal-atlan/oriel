"""Immutable evidence for selected committed local connector publications.

Connector sync remains a separate action. These APIs never resolve credentials,
read upstream data or contact a source. Freshness describes local extraction
windows, not upstream event time or completeness. Hashes establish consistency,
not authentication against an operator who can rewrite the entire workspace.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from ..connectors import service
from ..connectors.evidence import observe_table
from ..refresh_state import require_readable_snapshot
from ..workspace import digest


SPEC = "contextbase.customer360-snapshot/v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"c360-[0-9a-f]{64}")
_MANIFEST_FIELDS = {"spec", "snapshot_id", "observed_at", "sources", "coverage", "freshness", "manifest_sha256"}


def _now():
    return datetime.now(timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate fields in source snapshot evidence.")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("Nonfinite source snapshot evidence.")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON source snapshot evidence.") from exc


def _timestamp(value):
    if (not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value)):
        raise ValueError("Source timestamps must be complete timezone-bearing ISO timestamps.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid source timestamp.") from exc
    if result.utcoffset() is None:
        raise ValueError("Source timestamps require an explicit timezone.")
    return result.astimezone(timezone.utc)


def _limit(value, name, *, zero=False):
    if (type(value) not in {int, float} or not math.isfinite(value)
            or value < 0 or (not zero and value == 0)):
        raise ValueError(name + " must be a finite " + ("nonnegative" if zero else "positive") + " number.")
    return value


def _connections(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 101:
        raise ValueError("Select a nonempty list of configured connection names (up to 101).")
    for name in value:
        service._name(name)
    if len(value) != len(set(value)):
        raise ValueError("Select each connection only once.")
    return sorted(value)


@contextmanager
def _locks(workspace, connections):
    # Do not create a missing source/workspace as a side effect of validation.
    root = workspace.root / "connectors"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Select existing configured connector sources.")
    with ExitStack() as stack:
        for name in connections:
            directory = root / name
            if directory.is_symlink() or not directory.is_dir() or not (directory / "source.json").is_file():
                raise ValueError("Select existing configured connector sources.")
            stack.enter_context(service._lock(directory))
        yield


def _exists(conn, table):
    return bool(conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE "
                            "table_catalog=current_database() AND table_schema='_contextbase' AND table_name=?",
                            [table]).fetchone()[0])


def _table_evidence(value):
    if not isinstance(value, list) or not value:
        raise ValueError("Committed table evidence is missing; refresh the source before Customer360 capture.")
    names = []
    for table in value:
        if (not isinstance(table, dict) or set(table) != {"table", "rows", "columns", "schema_sha256", "content_sha256"}
                or not isinstance(table["table"], str) or not table["table"]
                or type(table["rows"]) is not int or table["rows"] < 0
                or not isinstance(table["columns"], list) or not table["columns"]):
            raise ValueError("Malformed committed table evidence.")
        for column in table["columns"]:
            if (not isinstance(column, dict) or set(column) != {"name", "type", "nullable"}
                    or not isinstance(column["name"], str) or not column["name"] or "\x00" in column["name"]
                    or not isinstance(column["type"], str) or not column["type"] or type(column["nullable"]) is not bool):
                raise ValueError("Malformed committed schema evidence.")
        if len({column["name"] for column in table["columns"]}) != len(table["columns"]):
            raise ValueError("Duplicate committed columns.")
        if (table["schema_sha256"] != _hash(table["columns"])
                or not isinstance(table["content_sha256"], str) or not _HASH.fullmatch(table["content_sha256"])):
            raise ValueError("Committed table evidence hash mismatch.")
        names.append(table["table"])
    if names != sorted(set(names)):
        raise ValueError("Committed tables must be unique and ordered.")
    return value


def _receipt(value, name):
    required = {"name", "source_type", "status", "started_at", "attempt", "refresh_mode", "configuration_sha256",
                "tables", "snapshot_sha256", "scope", "finished_at", "table_evidence"}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("Incomplete committed source receipt; refresh the source before Customer360 capture.")
    _canonical(value)
    if (value["name"] != name or value["status"] != "synced" or value["scope"] != "local_snapshot"
            or value["refresh_mode"] != "new_extraction" or not isinstance(value["source_type"], str)
            or not isinstance(value["attempt"], str) or not re.fullmatch(r"attempt-[0-9a-f]{32}", value["attempt"])):
        raise ValueError("Invalid committed source publication receipt.")
    for field in ("configuration_sha256", "snapshot_sha256"):
        if not isinstance(value[field], str) or not _HASH.fullmatch(value[field]):
            raise ValueError("Invalid committed source receipt hash.")
    if _timestamp(value["started_at"]) > _timestamp(value["finished_at"]):
        raise ValueError("Source extraction starts after its publication.")
    evidence = _table_evidence(value["table_evidence"])
    if value["tables"] != [{"table": item["table"], "rows": item["rows"]} for item in evidence]:
        raise ValueError("Committed receipt table counts and evidence disagree.")
    return value


def _source(workspace, conn, name):
    directory = workspace.root / "connectors" / name
    config = service._read(directory / "source.json")
    _canonical(config)  # Fail closed on nonfinite configuration values.
    if (not isinstance(config, dict) or config.get("spec") != service.SPEC or config.get("name") != name
            or config.get("catalog_sha256") != digest(service.catalog())):
        raise ValueError("Source configuration or catalog changed; review and refresh the source.")
    record = conn.execute("SELECT payload FROM _contextbase.connector_receipts WHERE name=?", [name]).fetchone()
    if record is None:
        raise ValueError("Every selected source requires a successful committed publication.")
    receipt = _receipt(_json(record[0]), name)
    if receipt["source_type"] != config.get("source_type") or receipt["configuration_sha256"] != digest(config):
        raise ValueError("Selected source was reconfigured without a matching committed refresh.")
    if _timestamp(config.get("created_at")) > _timestamp(receipt["started_at"]):
        raise ValueError("Source configuration postdates its committed extraction.")
    status_path = directory / "status.json"
    if status_path.exists() or status_path.is_symlink():
        status = service._read(status_path)
        _canonical(status)
        if not isinstance(status, dict) or status.get("attempt") != receipt["attempt"]:
            raise ValueError("Selected source has an unpublished or failed latest sync attempt.")
        header = ("name", "source_type", "started_at", "attempt", "refresh_mode", "configuration_sha256")
        if any(status.get(key) != receipt[key] for key in header) or status.get("status") not in {"running", "failed", "synced"}:
            raise ValueError("Latest source status does not match its committed receipt.")
        # The DB receipt and its tables commit together. A missing/unfinished
        # status for that *same* attempt is known service bookkeeping recovery;
        # an attempt with any other identity above is never treated as current.
        if status["status"] == "synced":
            fields = ("tables", "table_evidence", "snapshot_sha256", "scope")
            if any(status.get(key) != receipt[key] for key in fields):
                raise ValueError("Latest source status and committed table evidence disagree.")
            if _timestamp(status.get("finished_at")) < _timestamp(receipt["finished_at"]):
                raise ValueError("Latest source status predates its committed publication.")
    owner = conn.execute("SELECT source_type,tables FROM _contextbase.connector_ownership WHERE name=?", [name]).fetchone()
    if owner is None or owner[0] != receipt["source_type"]:
        raise ValueError("Selected table ownership is not verified by the source receipt.")
    owned = _json(owner[1])
    if (not isinstance(owned, list) or not owned or any(not isinstance(table, str) or not table or "\x00" in table for table in owned)
            or owned != sorted(set(owned))):
        raise ValueError("Invalid connector table ownership.")
    actual = conn.execute("SELECT table_name,table_type FROM information_schema.tables WHERE "
                          "table_catalog=current_database() AND table_schema=? ORDER BY table_name", [name]).fetchall()
    if actual != [(table, "BASE TABLE") for table in owned]:
        raise ValueError("Selected source tables or ownership changed after publication.")
    tables = [observe_table(conn, name, table) for table in owned]
    if tables != receipt["table_evidence"]:
        raise ValueError("Source table content or schema changed after its committed publication; refresh before capture.")
    for table in tables:
        latest = conn.execute("SELECT source,row_count,sha256 FROM _contextbase.imports WHERE target=? "
                              "ORDER BY imported_at DESC LIMIT 1", [table["table"]]).fetchone()
        if latest != ("connector:" + name, table["rows"], receipt["snapshot_sha256"]):
            raise ValueError("Source import provenance does not match its committed receipt.")
    return {"name": name, "source_type": receipt["source_type"], "configuration_sha256": receipt["configuration_sha256"],
            "receipt_sha256": _hash(receipt), "receipt": receipt, "tables": tables}


def _coverage(sources):
    return {"connections": [source["name"] for source in sources],
            "tables": [table["table"] for source in sources for table in source["tables"]],
            "total_rows": sum(table["rows"] for source in sources for table in source["tables"]),
            "source_scope": "committed_local_snapshots", "upstream_completeness": "not_verified"}


def _freshness(sources, max_age_seconds, max_skew_seconds, now):
    _limit(max_age_seconds, "max_age_seconds")
    _limit(max_skew_seconds, "max_skew_seconds", zero=True)
    starts = [_timestamp(source["receipt"]["started_at"]) for source in sources]
    finishes = [_timestamp(source["receipt"]["finished_at"]) for source in sources]
    if max(finishes) > now or max(starts) > now:
        raise ValueError("Source publication timestamps are in the future.")
    if (now - min(starts)).total_seconds() > max_age_seconds:
        raise ValueError("Selected source snapshot is stale; refresh before Customer360 use.")
    skew = (max(finishes) - min(finishes)).total_seconds()
    if skew > max_skew_seconds:
        raise ValueError("Selected source publication skew exceeds the configured freshness limit.")
    return {"max_age_seconds": max_age_seconds, "max_skew_seconds": max_skew_seconds,
            "age_basis": "oldest_extraction_started_at", "skew_basis": "connector_finished_at",
            "oldest_extraction_started_at": min(starts).isoformat(),
            "oldest_publication_at": min(finishes).isoformat(), "newest_publication_at": max(finishes).isoformat(),
            "publication_skew_seconds": skew}


def _identity(manifest):
    return {key: manifest[key] for key in ("spec", "sources", "coverage", "freshness")}


def _validate_manifest(value, snapshot_id):
    if not isinstance(snapshot_id, str) or not _ID.fullmatch(snapshot_id):
        raise ValueError("Invalid Customer360 snapshot identifier.")
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS or value.get("spec") != SPEC:
        raise ValueError("Invalid stored Customer360 snapshot manifest.")
    if (value["snapshot_id"] != snapshot_id or snapshot_id != "c360-" + _hash(_identity(value))
            or value["manifest_sha256"] != _hash({key: item for key, item in value.items() if key != "manifest_sha256"})):
        raise ValueError("Customer360 snapshot manifest hash mismatch.")
    observed = _timestamp(value["observed_at"])
    sources = value["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("A snapshot requires selected source evidence.")
    names = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"name", "source_type", "configuration_sha256", "receipt_sha256", "receipt", "tables"}:
            raise ValueError("Invalid stored source evidence.")
        service._name(source["name"])
        receipt = _receipt(source["receipt"], source["name"])
        if (source["receipt_sha256"] != _hash(receipt) or source["source_type"] != receipt["source_type"]
                or source["configuration_sha256"] != receipt["configuration_sha256"] or source["tables"] != receipt["table_evidence"]
                or any(not table["table"].startswith(source["name"] + ".") for table in source["tables"])):
            raise ValueError("Stored source and receipt evidence disagree.")
        names.append(source["name"])
    if _connections(names) != names or value["coverage"] != _coverage(sources):
        raise ValueError("Stored source coverage is inconsistent.")
    policy = value["freshness"]
    if not isinstance(policy, dict) or "max_age_seconds" not in policy or "max_skew_seconds" not in policy:
        raise ValueError("Stored freshness policy is missing.")
    if policy != _freshness(sources, policy["max_age_seconds"], policy["max_skew_seconds"], observed):
        raise ValueError("Stored freshness evidence is inconsistent.")
    return value


def _load(conn, snapshot_id):
    if not isinstance(snapshot_id, str) or not _ID.fullmatch(snapshot_id):
        raise ValueError("Invalid Customer360 snapshot identifier.")
    if not _exists(conn, "customer360_snapshots"):
        raise ValueError("Customer360 snapshot does not exist.")
    row = conn.execute("SELECT payload FROM _contextbase.customer360_snapshots WHERE snapshot_id=?", [snapshot_id]).fetchone()
    if row is None:
        raise ValueError("Customer360 snapshot does not exist.")
    return _validate_manifest(_json(row[0]), snapshot_id)


def _observe(workspace, conn, connections):
    require_readable_snapshot(workspace, connection=conn)
    if not all(_exists(conn, table) for table in ("connector_receipts", "connector_ownership", "imports")):
        raise ValueError("Every selected source requires a successful committed publication.")
    return [_source(workspace, conn, name) for name in connections]


def capture_snapshot(workspace, connections, *, max_age_seconds=86400, max_skew_seconds=3600):
    """Capture selected publications and table hashes in one DB transaction.

    Repeating an unchanged observation with the same freshness limits returns
    the original immutable manifest and original observed_at. A new connector
    publication has a different identity even if its data values are unchanged.
    """
    connections = _connections(connections)
    _limit(max_age_seconds, "max_age_seconds")
    _limit(max_skew_seconds, "max_skew_seconds", zero=True)
    if not workspace.db_path.is_file():
        raise ValueError("Every selected source requires a successful committed publication.")
    with _locks(workspace, connections), workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            sources = _observe(workspace, conn, connections)
            now = _now()
            value = {"spec": SPEC, "observed_at": now.isoformat(), "sources": sources, "coverage": _coverage(sources),
                     "freshness": _freshness(sources, max_age_seconds, max_skew_seconds, now)}
            value["snapshot_id"] = "c360-" + _hash(_identity(value))
            value["manifest_sha256"] = _hash(value)
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_snapshots "
                         "(snapshot_id VARCHAR PRIMARY KEY,payload JSON NOT NULL)")
            previous = conn.execute("SELECT payload FROM _contextbase.customer360_snapshots WHERE snapshot_id=?",
                                    [value["snapshot_id"]]).fetchone()
            if previous:
                value = _validate_manifest(_json(previous[0]), value["snapshot_id"])
            else:
                conn.execute("INSERT INTO _contextbase.customer360_snapshots VALUES (?,?)", [value["snapshot_id"], _canonical(value)])
            conn.commit()
            return value
        except Exception:
            conn.rollback()
            raise


def load_snapshot(workspace, snapshot_id):
    """Load and integrity-check historical evidence; does not assert freshness."""
    with workspace._connect() as conn:
        return _load(conn, snapshot_id)


def _validate_current(workspace, conn, manifest):
    current = _observe(workspace, conn, manifest["coverage"]["connections"])
    if current != manifest["sources"]:
        raise ValueError("Customer360 snapshot no longer matches current source publications or data.")
    policy = manifest["freshness"]
    _freshness(current, policy["max_age_seconds"], policy["max_skew_seconds"], _now())
    return manifest


def assert_snapshot_current(workspace, snapshot_id):
    """Verify current publication, content and freshness without DB writes.

    Selected connector coordination locks are held transiently to prevent sync
    or reconfiguration during validation. No persistent source state is changed.
    """
    manifest = load_snapshot(workspace, snapshot_id)
    with _locks(workspace, manifest["coverage"]["connections"]), workspace._connect() as conn:
        conn.begin()
        try:
            manifest = _validate_current(workspace, conn, _load(conn, snapshot_id))
            conn.commit()
            return manifest
        except Exception:
            conn.rollback()
            raise


@contextmanager
def readable_snapshot(workspace, snapshot_id):
    """Yield (writable connection, manifest) for atomic consumer materialization.

    Validation and the consumer's reads/writes use this same transaction and
    connection. Selected source locks remain held until commit or rollback.
    The caller must not commit, close or replace the yielded connection.
    """
    manifest = load_snapshot(workspace, snapshot_id)
    with _locks(workspace, manifest["coverage"]["connections"]), workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            manifest = _validate_current(workspace, conn, _load(conn, snapshot_id))
            yield conn, manifest
            conn.commit()
        except Exception:
            conn.rollback()
            raise
