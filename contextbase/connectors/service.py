"""Explicit connector configuration and local snapshot publication.

Only environment-variable references are stored for credentials. Extraction
runs separately; failed extraction never replaces the queryable workspace.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import catalog, describe
from .policy import validate_parameters, validate_selection
from ..workspace import identifier, digest

SPEC = "contextbase.source/v1"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _directory(path):
    if path.is_symlink():
        raise ValueError("Connector directories must not be symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write(path, value, *, exclusive=False):
    raw = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(raw)
        return
    if path.is_symlink():
        raise ValueError("Connector files must not be symbolic links.")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    _write(temporary, value, exclusive=True)
    os.replace(temporary, path)


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("Select a regular, bounded connector configuration file.")
    return json.loads(path.read_text())


def _root(workspace):
    _directory(workspace.root)
    return _directory(workspace.root / "connectors")


def _source_root(workspace, name):
    _name(name)
    return _directory(_root(workspace) / name)


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("Connection names must start with a lowercase letter and use lowercase letters, digits or underscores.")
    return name


def _resources(resources):
    if (not isinstance(resources, list) or len(resources) > 256
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", item) for item in resources)
            or len(resources) != len(set(resources))):
        raise ValueError("Select unique resource identifiers (up to 256).")
    return resources


def _credential_fields(entry):
    return {item["name"]: item for item in entry["credential_specs"]}


def _file_path(entry, path, refs, params, resources):
    if entry["execution_family"] == "file":
        if not isinstance(path, (str, Path)) or not path or refs or params or resources:
            raise ValueError("File connectors require --path; credentials, parameters and resource selection do not apply.")
        file_path = Path(path).expanduser().resolve(strict=True)
        if not file_path.is_file() or file_path.suffix.lower() != "." + entry["name"]:
            raise ValueError("Select one local file matching this connector type.")
        return str(file_path)
    if path is not None:
        raise ValueError("--path applies only to the CSV and Parquet connectors.")
    return None


def _environment_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            or name in {"HOME", "PATH", "PYTHONPATH", "CODEX_HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                        "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
            or name.startswith(("DLT_", "RUNTIME__", "SOURCES__", "DESTINATION__"))):
        raise ValueError("Use an explicit credential environment-variable name, not a value or runtime setting.")
    return name


def add(workspace, source_type, *, name=None, credential_env=None, resources=None, params=None, path=None, replace_config=False):
    entry = describe(source_type)
    name = name or source_type
    _name(name)
    fields = _credential_fields(entry)
    refs = credential_env
    if refs is None:
        refs = {key: value["env_var"] for key, value in fields.items() if value.get("env_var") and value.get("required", True)}
    if not isinstance(refs, dict) or set(refs) - set(fields):
        raise ValueError("Credential fields must match 'source describe'.")
    refs = {key: _environment_name(value) for key, value in refs.items()}
    resources = _resources([] if resources is None else resources)
    params = validate_parameters(entry, {} if params is None else params)
    path = _file_path(entry, path, refs, params, resources)
    config = {"spec": SPEC, "name": name, "source_type": source_type, "credential_env": refs,
              "resources": resources, "params": params, "path": path,
              "catalog_sha256": digest(catalog()), "created_at": _now()}
    directory = _source_root(workspace, name)
    if replace_config:
        with _lock(directory):
            previous = _read(directory / "source.json")
            if previous.get("spec") != SPEC or previous.get("name") != name or previous.get("source_type") != source_type:
                raise ValueError("An existing connection can only be reconfigured with the same name and source type.")
            _write(directory / ("config-before-" + uuid.uuid4().hex + ".json"), previous, exclusive=True)
            _write(directory / "source.json", config)
    else:
        _write(directory / "source.json", config, exclusive=True)
    return {**config, "configured": True, "connected": False,
            "next": f"oriel --workspace {workspace.root} source sync {name}"}


def configured(workspace):
    root = workspace.root / "connectors"
    if not root.exists():
        return {"sources": []}
    if root.is_symlink():
        raise ValueError("Connector directories must not be symbolic links.")
    committed = {}
    if workspace.db_path.exists():
        with workspace._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='_contextbase' "
                            "AND table_name='connector_receipts'").fetchone()[0]:
                committed = {name: json.loads(payload) for name, payload in
                             conn.execute("SELECT name,payload FROM _contextbase.connector_receipts").fetchall()}
    sources = []
    for directory in sorted(root.iterdir()):
        if directory.is_symlink() or not directory.is_dir() or not (directory / "source.json").exists():
            continue
        config = _read(directory / "source.json")
        status = _read(directory / "status.json") if (directory / "status.json").exists() else None
        receipt = committed.get(config["name"])
        if receipt and (status is None or (status.get("attempt") == receipt.get("attempt") and status.get("status") != "synced")):
            status = {**receipt, "bookkeeping": "recovered_from_database_receipt"}
        sources.append({"name": config["name"], "source_type": config["source_type"],
                        "resources": config["resources"], "credential_env": config["credential_env"],
                        "configuration_sha256": digest(config),
                        "configuration_changed_since_sync": bool(receipt and receipt.get("configuration_sha256") != digest(config)),
                        "last_snapshot": receipt, "last_sync": status})
    return {"sources": sources}


def _load(workspace, name):
    directory = _source_root(workspace, name)
    config = _read(directory / "source.json")
    if config.get("spec") != SPEC or config.get("name") != name or config.get("catalog_sha256") != digest(catalog()):
        raise ValueError("Connector configuration or catalog changed; review and rerun source add with --replace-config.")
    entry = describe(config["source_type"])
    fields = _credential_fields(entry)
    if not isinstance(config["credential_env"], dict) or set(config["credential_env"]) - set(fields):
        raise ValueError("Unexpected credential fields in connector configuration.")
    required = {key for key, field in fields.items() if field.get("required", True)}
    if entry["execution_family"] != "file" and required - set(config["credential_env"]):
        raise ValueError("Configure explicit environment references for every required credential field listed by 'source describe'.")
    for value in config["credential_env"].values():
        _environment_name(value)
    _resources(config["resources"])
    validate_parameters(entry, config["params"])
    config["path"] = _file_path(entry, config["path"], config["credential_env"], config["params"], config["resources"])
    return config, entry, directory


def _check_resources(entry, resources, params):
    validate_selection(entry, resources, params)
    declared = {item["name"]: item for item in entry["resources"]}
    if declared and set(resources) - set(declared):
        raise ValueError("Unknown selected resource; inspect 'source describe'.")
    selected = resources or [name for name, item in declared.items() if item.get("selected", True)]
    issues = [item for item in entry.get("limitations", []) if isinstance(item, dict) and item.get("blocking")]
    issues += [issue for name in selected for issue in declared.get(name, {}).get("limitations", [])
               if issue.get("blocking")]
    if issues:
        codes = sorted({item["code"] for item in issues})
        raise ValueError("Selected resources need an adapter fix before sync: " + ", ".join(codes))


@contextmanager
def _lock(directory):
    path = directory / "sync.lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError("A sync is running or was interrupted. Inspect it before removing its local sync.lock.") from None
    os.close(fd)
    try:
        yield
    finally:
        path.unlink()


def _publish(workspace, config, database, receipt=None):
    """Copy only staged base tables into the connector's owned schema atomically."""
    from ..context import _identifier
    from ..refresh_state import require_readable_snapshot
    database = Path(database).resolve(strict=True)
    if not database.is_file() or database == workspace.db_path:
        raise ValueError("Expected a separate completed connector snapshot.")
    name = config["name"]
    schema = identifier(name)
    snapshot_hash = hashlib.sha256()
    with database.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            snapshot_hash.update(chunk)
    with workspace._connect(writable=True, imports=True) as conn:
        conn.execute("SET allowed_paths=?", [[str(database), str(database) + ".wal"]])
        conn.execute("SET enable_external_access=false")
        literal = str(database).replace("'", "''")
        conn.execute(f"ATTACH '{literal}' AS connector_snapshot (READ_ONLY)")
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog='connector_snapshot' "
                              "AND table_schema=? AND table_type='BASE TABLE' ORDER BY table_name", [name]).fetchall()
        tables = [row[0] for row in tables if not row[0].startswith("_dlt_")]
        if not tables:
            raise ValueError("The connector produced no materialized tables; the previous workspace was retained.")
        conn.begin()
        try:
            require_readable_snapshot(workspace, connection=conn)
            workspace._metadata(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.connector_ownership "
                         "(name VARCHAR PRIMARY KEY,source_type VARCHAR,tables JSON)")
            owner = conn.execute("SELECT source_type,tables FROM _contextbase.connector_ownership WHERE name=?", [name]).fetchone()
            existing = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_catalog=current_database() AND table_schema=?", [name]).fetchall()}
            if existing and not owner:
                raise ValueError("This schema already contains other data. Configure the connector with a new --name.")
            if owner:
                previous = set(json.loads(owner[1]))
                if owner[0] != config["source_type"] or existing - previous:
                    raise ValueError("Connector schema ownership changed; inspect the workspace before syncing.")
                if previous - set(tables):
                    raise ValueError("The new snapshot is missing previously loaded tables; previous data was retained.")
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            copied = []
            for table in tables:
                quoted = _identifier(table)
                target = f"{schema}.{quoted}"
                conn.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM connector_snapshot.{schema}.{quoted}")
                count = conn.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
                conn.execute("INSERT INTO _contextbase.imports VALUES (?,?,current_timestamp,?,?)",
                             ["connector:" + name, name + "." + table, count, snapshot_hash.hexdigest()])
                copied.append({"table": name + "." + table, "rows": count})
            conn.execute("INSERT OR REPLACE INTO _contextbase.connector_ownership VALUES (?,?,?)",
                         [name, config["source_type"], json.dumps(tables)])
            published = {"tables": copied, "snapshot_sha256": snapshot_hash.hexdigest(), "scope": "local_snapshot"}
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.connector_receipts (name VARCHAR PRIMARY KEY,payload JSON)")
            conn.execute("INSERT OR REPLACE INTO _contextbase.connector_receipts VALUES (?,?)",
                         [name, json.dumps({**(receipt or {}), **published, "status": "synced", "finished_at": _now()})])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return published


def sync(workspace, name, *, timeout=300, bootstrap_context=True):
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("Sync timeout must be between 1 and 3600 seconds.")
    directory = _source_root(workspace, name)
    with _lock(directory):
        config, entry, directory = _load(workspace, name)
        _check_resources(entry, config["resources"], config["params"])
        attempt = _directory(directory / ("attempt-" + uuid.uuid4().hex))
        status = {"name": name, "source_type": config["source_type"], "status": "running",
                  "started_at": _now(), "attempt": attempt.name, "refresh_mode": "new_extraction",
                  "configuration_sha256": digest(config)}
        _write(directory / "status.json", status)
        try:
            database = attempt / "snapshot.duckdb"
            if entry["execution_family"] == "file":
                from ..workspace import Workspace
                staging = Workspace(attempt / "file-workspace")
                staging.ingest(config["path"], name, "records")
                database = staging.db_path
            else:
                request = {key: config[key] for key in ("source_type", "name", "credential_env", "params", "resources")}
                request.update(staging_database=str(database), work_dir=str(attempt))
                _write(attempt / "request.json", request, exclusive=True)
                command = [sys.executable, "-m", "contextbase.connectors.worker", "--request", str(attempt / "request.json"),
                           "--result", str(attempt / "result.json")]
                try:
                    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, timeout=timeout, check=False)
                finally:
                    # A killed worker cannot run its own finally block. The
                    # parent owns cleanup once subprocess.run has reaped it.
                    from .worker import cleanup_scratch
                    try:
                        cleanup_scratch(attempt)
                    except OSError:
                        raise RuntimeError("Connector scratch cleanup failed; inspect this private attempt before retrying.") from None
                result = _read(attempt / "result.json") if (attempt / "result.json").exists() else {}
                if completed.returncode or result.get("status") != "success":
                    allowed = {"runtime_dependency_missing", "connector_dependency_missing", "credential_unavailable", "extraction_failed",
                               "runtime_isolation_unavailable", "invalid_request", "runtime_version_unsupported", "resource_selection_mismatch",
                               "extraction_incomplete", "extraction_empty_schema", "invalid_output_schema", "output_exists", "result_too_large",
                               "invalid_source_factory"}
                    code = result.get("error_code")
                    raise RuntimeError("Connector sync failed (" + (code if code in allowed else "worker_failed") +
                                       "). Inspect source configuration and installed connector dependencies; previous data was retained.")
            published = _publish(workspace, config, database, status)
            status.update(status="synced", finished_at=_now(), **published)
        except subprocess.TimeoutExpired:
            status.update(status="failed", error_code="timeout", finished_at=_now())
            _write(directory / "status.json", status)
            raise RuntimeError("Connector sync exceeded its timeout; previous data was retained.") from None
        except Exception:
            status.update(status="failed", error_code="sync_failed", finished_at=_now())
            _write(directory / "status.json", status)
            raise
        # The database receipt commits with its tables. File bookkeeping cannot
        # turn a committed publication into a claimed extraction failure.
        try:
            _write(attempt / "receipt.json", status, exclusive=True)
            _write(directory / "status.json", status)
        except (OSError, ValueError):
            status["bookkeeping"] = "file_receipt_failed; database_receipt_committed"
        if bootstrap_context:
            from ..context import bootstrap
            try:
                draft = bootstrap(workspace)
                status["context"] = {"status": "draft", "version": draft["context_version"],
                                     "path": str(workspace.root / "context" / "draft.json")}
            except Exception:
                status["context"] = {"status": "bootstrap_failed", "next": "Run context bootstrap after inspecting the imported catalog."}
            try:
                _write(directory / "status.json", status)
            except (OSError, ValueError):
                status["bookkeeping"] = "file_receipt_failed; database_receipt_committed"
        return status
