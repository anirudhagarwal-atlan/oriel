"""Packaged synthetic samples with separate extraction and review preparation.

Connect executes the production dlt SQLite connector. Prepare reads only the
committed DuckDB snapshot and proposes explicit source relationships. Neither
operation approves links, profiles or business definitions. This is authored
sample onboarding, not vendor compatibility or a human business review.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import uuid


from . import identity, snapshots
from .contracts import PROFILE_SPEC, canonical, sha256
from ..workspace import table_identifier, digest


ROOT = Path(__file__).resolve().parent / "sample_data"
SPEC = "oriel.customer360.sqlite-fixture-runtime/v1"
_IDS = {"salesforce.account": ("Id",), "salesforce.contact": ("Id",),
        "salesforce.opportunity": ("Id",), "salesforce.sf_user": ("Id",),
        "gong.extensive_calls": ("metaData", "id"), "gong.call_transcripts": ("callId",),
        "stripe.customers": ("id",), "stripe.invoices": ("id",),
        "intercom.companies": ("id",), "intercom.contacts": ("id",), "intercom.conversations": ("id",)}
_RESERVED = {"fixture_record_id", "fixture_position", "record_json"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def prerequisites():
    versions = {}
    for package, expected in (("dlt", "1.24.0"), ("PyYAML", "6.0.3"), ("duckdb", "1.4.5"), ("SQLAlchemy", None)):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            raise ValueError("Install the approved pinned connector runtime before extracting fixture profiles.") from None
        if expected and versions[package] != expected:
            raise ValueError("Fixture extraction requires the approved pinned connector runtime.")
    return versions


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        raise ValueError("Select bounded regular synthetic fixture files.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate fixture JSON keys.")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("Nonfinite fixture JSON value.")
    return json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=invalid)


def _quoted(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", name):
        raise ValueError("Fixture fields must use bounded ASCII identifiers.")
    return '"' + name + '"'


def _normalized(name):
    # Restricted ASCII subset of the pinned dlt snake_case convention. Every
    # predicted name and its values are checked against the actual publication.
    _quoted(name)
    value = re.sub(r"([^_])([A-Z][a-z]+)", r"\1_\2", name)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()
    trailing = len(value) - len(value.rstrip("_"))
    return re.sub("_+", "_", value.rstrip("_") + "x" * trailing)


def _record_id(resource, record):
    value = record
    for part in _IDS[resource]:
        if not isinstance(value, dict) or part not in value:
            raise ValueError("Fixture record is missing its declared source ID.")
        value = value[part]
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Fixture source identifiers must remain bounded strings.")
    return value


def _fixtures(root, profile_name, phase):
    if profile_name not in {"salesforce-gong", "billing-support"} or phase not in {"initial", "refreshed"}:
        raise ValueError("Choose a declared synthetic profile and initial or refreshed phase.")
    manifest = _read(root / "manifest.json")
    if (manifest.get("spec") != "oriel.customer360.synthetic-fixtures/v1" or manifest.get("synthetic") is not True
            or manifest.get("native_connector_executed") is not False):
        raise ValueError("Fixture root must declare authored synthetic source records.")
    definition = manifest["profiles"][profile_name]
    records, hashes = {}, {}
    if set(definition["phases"]) != {"initial", "refreshed"}:
        raise ValueError("Both fixture phases must be explicitly declared.")
    for state, files in definition["phases"].items():
        if not isinstance(files, dict) or not files or set(files) - _IDS.keys():
            raise ValueError("Unsupported fixture resource selection.")
        records[state] = {}
        for resource, relative in sorted(files.items()):
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError("Fixture paths must stay inside the declared profile.")
            path = root / relative
            if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
                raise ValueError("Fixture paths cannot traverse or follow symlinks.")
            rows = _read(path)
            if not isinstance(rows, list) or len(rows) > 10_000 or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Fixture resources require bounded arrays of source records.")
            ids = [_record_id(resource, row) for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError("Fixture resource has duplicate source IDs.")
            records[state][resource] = rows
            hashes[relative] = _hash(rows)
    if set(records["initial"]) != set(records["refreshed"]):
        raise ValueError("Fixture refresh must retain its explicit resource selection.")
    fixture_hash = _hash({"manifest": manifest, "resource_hashes": hashes})
    return records, fixture_hash


def _columns(rows):
    result, seen = {}, set(_RESERVED)
    for original in sorted({key for row in rows for key in row}):
        normalized = _normalized(original)
        if normalized in seen or normalized.startswith("_dlt_"):
            raise ValueError("Fixture column normalization is ambiguous.")
        seen.add(normalized)
        values = [row[original] for row in rows if row.get(original) is not None]
        types = {type(value) for value in values}
        if not values or types == {str}:
            sql_type, encoding = "TEXT", "text"
        elif types == {bool}:
            sql_type, encoding = "BOOLEAN", "boolean"
        elif types == {int} and all(-(2**63) <= value < 2**63 for value in values):
            sql_type, encoding = "INTEGER", "integer"
        elif types == {float} and all(math.isfinite(value) for value in values):
            sql_type, encoding = "REAL", "float"
        else:
            sql_type, encoding = "TEXT", "json_text"
        result[original] = {"column": normalized, "sqlite_type": sql_type, "encoding": encoding}
    return result


def _value(value, column):
    return _canonical(value) if value is not None and column["encoding"] == "json_text" else value


def _write(path, value):
    if path.is_symlink():
        raise ValueError("Fixture runtime state cannot be a symlink.")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(directory):
    path = directory / "runtime.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Fixture extraction is running or was interrupted; inspect its retained receipts.") from None
    os.close(descriptor)
    try:
        yield
    finally:
        path.unlink()


def _source_database(path, resource_rows, schemas):
    if path.is_symlink():
        raise ValueError("Fixture SQLite files cannot be symlinks.")
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS excluded_fixture_sentinel (value TEXT)")
        database.execute("DELETE FROM excluded_fixture_sentinel")
        database.execute("INSERT INTO excluded_fixture_sentinel VALUES (?)", ["synthetic_unselected_record"])
        for resource, rows in resource_rows.items():
            table = resource.split(".")[1]
            columns = schemas[resource]
            declared = ",".join(_quoted(key) + " " + column["sqlite_type"] for key, column in columns.items())
            database.execute(f"CREATE TABLE IF NOT EXISTS {_quoted(table)} (fixture_record_id TEXT PRIMARY KEY, "
                             f"fixture_position INTEGER NOT NULL, record_json TEXT NOT NULL,{declared})")
            database.execute(f"DELETE FROM {_quoted(table)}")
            placeholders = ",".join("?" for _ in range(3 + len(columns)))
            database.executemany(f"INSERT INTO {_quoted(table)} VALUES ({placeholders})", [
                [_record_id(resource, row), index, _canonical(row), *[_value(row.get(key), column) for key, column in columns.items()]]
                for index, row in enumerate(rows)])


def _sync(workspace, connection, env_name, database):
    from contextbase.connectors.worker import _PROCESS_ENV
    # Only process settings and this generated local URL enter the child.
    # No ambient vendor credentials or source/dlt configuration are inherited.
    environment = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
    environment[env_name] = "sqlite:///" + str(database)
    process = subprocess.Popen([sys.executable, "-m", "contextbase", "--workspace", str(workspace.root),
                                "source", "sync", connection, "--no-bootstrap"],
                               env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()
        raise RuntimeError("Fixture extraction exceeded its deadline; inspect retained private source status.") from None
    if process.returncode:
        raise RuntimeError("The actual local SQLite connector failed; inspect its sanitized private source status.")
    result = json.loads(stdout)
    if result.get("status") != "synced" or result.get("source_type") != "sqlite":
        raise RuntimeError("The local connector did not produce a successful SQLite publication.")
    return result


def _reconcile(workspace, connection, resource_rows, schemas, receipt):
    expected_tables = {connection + "." + resource.split(".")[1] for resource in resource_rows}
    if {table["table"] for table in receipt["tables"]} != expected_tables:
        raise RuntimeError("Actual connector table selection differs from the declared fixture resources.")
    resources = []
    with workspace._connect() as conn:
        for resource, rows in sorted(resource_rows.items()):
            table = resource.split(".")[1]
            columns = schemas[resource]
            actual_columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?", [connection, table]).fetchall()}
            expected_columns = _RESERVED | {column["column"] for column in columns.values()}
            if {column for column in actual_columns if not column.startswith("_dlt_")} != expected_columns:
                raise RuntimeError("Actual dlt column normalization differs from the declared scalar mapping.")
            names = ["fixture_record_id", "fixture_position", "record_json", *[column["column"] for column in columns.values()]]
            observed = conn.execute(f"SELECT {','.join(_quoted(name) for name in names)} FROM "
                                    f"{_quoted(connection)}.{_quoted(table)} ORDER BY fixture_position").fetchall()
            expected = [(_record_id(resource, row), index, _canonical(row),
                         *[_value(row.get(key), column) for key, column in columns.items()]) for index, row in enumerate(rows)]
            if _canonical(observed) != _canonical(expected):
                raise RuntimeError("Actual connector records differ from the independent source arrays.")
            resources.append({"resource": resource, "table": connection + "." + table, "records": len(rows),
                              "source_records_sha256": _hash(rows), "reconciled": True,
                              "record_id_path": list(_IDS[resource]), "columns": columns})
    return resources


def load_profile(workspace, profile_name, phase="initial", *, fixture_root=None):
    """Run real local extraction, preserving raw records and returning receipts.

    Use one workspace per synthetic profile. Refreshes retain the configuration
    and run a new complete extraction. Existing unrelated connections are never
    replaced. Identity and profile review remain explicit separate operations.
    """
    from contextbase.connectors import service
    versions = prerequisites()
    root = Path(fixture_root).resolve() if fixture_root is not None else ROOT
    records, fixture_hash = _fixtures(root, profile_name, phase)
    schemas = {resource: _columns(records["initial"][resource] + records["refreshed"][resource]) for resource in records[phase]}
    grouped = defaultdict(dict)
    for resource, rows in records[phase].items():
        grouped[resource.split(".")[0]][resource] = rows
    source_names = {source: "fixture_" + source for source in grouped}
    directory = workspace.root / "customer360-fixture-runtime"
    if directory.is_symlink():
        raise ValueError("Fixture runtime directory cannot be a symlink.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock(directory):
        state_path = directory / "state.json"
        state = _read(state_path) if state_path.exists() else None
        configured = service.configured(workspace)["sources"]
        if state:
            actual = {item["name"]: item["configuration_sha256"] for item in configured}
            if (state.get("spec") != SPEC or state.get("profile") != profile_name or state.get("fixture_sha256") != fixture_hash
                    or state.get("source_names") != source_names or actual != state.get("configuration_sha256")):
                raise ValueError("Fixture profile or connector configuration changed; select a new dedicated workspace.")
        elif configured:
            raise ValueError("Fixture extraction requires a dedicated workspace without existing source connections.")
        if not state:
            for source, resources in grouped.items():
                env_name = "ORIEL_CUSTOMER360_FIXTURE_" + source.upper() + "_SQLITE_URL"
                service.add(workspace, "sqlite", name=source_names[source], credential_env={"credentials": env_name},
                            resources=sorted(resource.split(".")[1] for resource in resources), params={"chunk_size": 2})
            state = {"spec": SPEC, "profile": profile_name, "fixture_sha256": fixture_hash, "source_names": source_names,
                     "configuration_sha256": {item["name"]: item["configuration_sha256"] for item in service.configured(workspace)["sources"]}}
            _write(state_path, state)
        run_dir = directory / ("run-" + uuid.uuid4().hex)
        run_dir.mkdir(mode=0o700)
        report_path = run_dir / "receipt.json"
        report = {"spec": SPEC, "status": "running", "profile": profile_name, "phase": phase,
                  "fixture_sha256": fixture_hash, "runtime_versions": versions, "started_at": _now(),
                  "source_names": sorted(source_names.values()), "source_label_mapping": source_names,
                  "scope": "actual_dlt_sqlite_extraction_of_authored_source_shaped_records",
                  "vendor_adapter_verified": False, "model_calls": 0, "reviews_performed": False,
                  "chunk_size": 2, "publications": [], "resources": [], "receipt_path": str(report_path)}
        _write(report_path, report)
        try:
            for source, resources in sorted(grouped.items()):
                database = directory / (source + ".sqlite")
                _source_database(database, resources, schemas)
                receipt = _sync(workspace, source_names[source], "ORIEL_CUSTOMER360_FIXTURE_" + source.upper() + "_SQLITE_URL", database)
                report["publications"].append(receipt)
                report["resources"].extend(_reconcile(workspace, source_names[source], resources, schemas, receipt))
                _write(report_path, report)
            report.update(status="passed", actual_dlt_executed=True, finished_at=_now())
        except Exception:
            report.update(status="failed", error_code="fixture_runtime_or_reconciliation_failed", finished_at=_now())
            _write(report_path, report)
            raise
        _write(report_path, report)
        return report

def _records(workspace, resources, snapshot_id, extraction):
    result = {}
    with snapshots.readable_snapshot(workspace, snapshot_id) as (conn, snapshot):
        claimed = {item["name"]: item for item in extraction["publications"]}
        if len(claimed) != len(extraction["publications"]) or set(claimed) != set(extraction["source_names"]):
            raise ValueError("Require one actual publication receipt for every selected fixture source.")
        for source in snapshot["sources"]:
            receipt = claimed[source["name"]]
            if (receipt.get("status") != "synced" or receipt.get("source_type") != "sqlite"
                    or any(receipt.get(key) != source["receipt"].get(key) for key in
                           ("attempt", "configuration_sha256", "snapshot_sha256", "table_evidence"))):
                raise ValueError("Fixture publication receipts do not match the current source snapshot.")
        observed = {table["table"] for source in snapshot["sources"] for table in source["tables"]}
        for resource, info in resources.items():
            if info["table"] not in observed:
                raise ValueError("Fixture resources must belong to the selected committed snapshot.")
            result[resource] = [json.loads(row[0]) for row in conn.execute(
                "SELECT record_json FROM " + table_identifier(info["table"]) + " ORDER BY fixture_position").fetchall()]
            if sha256(result[resource]) != info["source_records_sha256"]:
                raise ValueError("The fixture extraction receipt does not match current committed source records.")
    return result


def _customers(workspace, profile_name, declared):
    """Persist explicit fixture aliases; customer labels are never identity keys."""
    ids = [item["customer_id"] for item in declared]
    if len(ids) != len(set(ids)):
        raise ValueError("Synthetic fixture customer aliases must be unique.")
    result = {}
    with workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            identity.ensure_identity_schema(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_fixture_customers "
                         "(profile VARCHAR,fixture_customer_id VARCHAR,customer_id VARCHAR, "
                         "PRIMARY KEY(profile,fixture_customer_id))")
            for fixture_id in ids:
                row = conn.execute("SELECT customer_id FROM _contextbase.customer360_fixture_customers "
                                   "WHERE profile=? AND fixture_customer_id=?", [profile_name, fixture_id]).fetchone()
                if row:
                    if not conn.execute("SELECT 1 FROM _contextbase.customer360_customers WHERE id=?", [row[0]]).fetchone():
                        raise ValueError("Fixture alias refers to a missing workspace customer.")
                    result[fixture_id] = row[0]
                else:
                    customer_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO _contextbase.customer360_customers VALUES (?,?,current_timestamp)",
                                 [customer_id, "synthetic-" + profile_name + "-" + fixture_id])
                    conn.execute("INSERT INTO _contextbase.customer360_fixture_customers VALUES (?,?,?)",
                                 [profile_name, fixture_id, customer_id])
                    result[fixture_id] = customer_id
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def _prepare_extraction(workspace, extraction, *, fixture_root=ROOT):
    if (extraction.get("spec") != "oriel.customer360.sqlite-fixture-runtime/v1"
            or extraction.get("status") != "passed" or extraction.get("actual_dlt_executed") is not True
            or extraction.get("vendor_adapter_verified") is not False):
        raise ValueError("Require the successful native SQLite fixture extraction receipt.")
    profile_name, phase = extraction["profile"], extraction["phase"]
    if profile_name not in ("salesforce-gong", "billing-support") or phase not in ("initial", "refreshed"):
        raise ValueError("Unknown synthetic profile or phase.")
    fixture_root = Path(fixture_root)
    decisions = _read(fixture_root / "profiles" / profile_name / "mapping-decisions.json")
    if (decisions.get("spec") != "oriel.customer360.fixture-mapping/v1" or decisions.get("synthetic") is not True
            or decisions.get("profile") != profile_name or decisions.get("review_status") != "authored_example_not_a_real_review"):
        raise ValueError("Require authored synthetic mapping decisions for this exact fixture profile.")
    resources = {item["resource"]: item for item in extraction["resources"]}
    source_names = extraction["source_label_mapping"]
    if (len(resources) != len(extraction["resources"]) or sorted(source_names.values()) != extraction["source_names"]
            or any(info["table"] != source_names[resource.split(".")[0]] + "." + resource.split(".")[1]
                   for resource, info in resources.items())):
        raise ValueError("Fixture resource and source namespaces must match the extraction receipt.")
    snapshot = snapshots.capture_snapshot(workspace, extraction["source_names"])
    data = _records(workspace, resources, snapshot["snapshot_id"], extraction)
    customer_ids = _customers(workspace, profile_name, decisions["customers"])

    candidates, incomplete = {}, set()
    def add(source, entity, source_id, customer_ids_for_record):
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("Synthetic source identifiers must remain nonempty strings.")
        key = (source_names[source], entity, source_id)
        candidates.setdefault(key, set()).update(customer_ids_for_record)

    for decision in decisions["bindings"]:
        if phase in decision["phases"] and decision["status"] == "approved_example":
            add(decision["source"], decision["resource"], decision["source_id"], [customer_ids[decision["customer_id"]]])

    def assigned(source, entity, source_id):
        return candidates.get((source_names[source], entity, source_id), set())

    def derive(source, entity, source_id, members, missing=False):
        add(source, entity, source_id, members)
        if missing:
            incomplete.add((source_names[source], entity, source_id))

    def binding(resource, kind, ref_source, ref_entity, ref_column, fields):
        info = resources[resource]
        return {"connection": info["table"].split(".")[0], "entity": resource.split(".")[1], "table": info["table"],
                "kind": kind, "record_id": "fixture_record_id",
                "customer_ref": {"connection": source_names[ref_source], "entity": ref_entity,
                                 "column": "fixture_record_id" if ref_column is None else info["columns"][ref_column]["column"]},
                "fields": {role: info["columns"][column]["column"] for role, column in fields.items()}}

    if profile_name == "salesforce-gong":
        opportunities = {row["Id"]: row["AccountId"] for row in data["salesforce.opportunity"]}
        for call in data["gong.extensive_calls"]:
            members, missing = set(), False
            for context in call["context"]:
                if context["system"] != "Salesforce":
                    missing = True
                    continue
                for obj in context["objects"]:
                    account = (obj["objectId"] if obj["objectType"] == "Account" else
                               opportunities.get(obj["objectId"]) if obj["objectType"] == "Opportunity" else None)
                    found = assigned("salesforce", "account", account)
                    missing |= not found
                    members.update(found)
            derive("gong", "extensive_calls", call["metaData"]["id"], members, missing)
        bindings = [
            binding("salesforce.account", "customer", "salesforce", "account", "Id", {"label": "Name"}),
            binding("salesforce.opportunity", "commercial", "salesforce", "account", "AccountId",
                    {"amount": "Amount", "currency": "CurrencyIsoCode", "is_closed": "IsClosed", "close_date": "CloseDate"}),
            binding("gong.call_transcripts", "conversation", "gong", "extensive_calls", "callId", {"text": "transcript"}),
        ]
        bindings[1]["measures"] = {"amount": {
            "unit": "currency", "currency_field": "currency", "aggregation": "sum",
            "meaning": "Authored synthetic fixture policy: opportunity amount is in major units of its source currency and may be summed per currency. This does not establish ARR, bookings or recurrence.",
        }}
        metadata = resources["gong.extensive_calls"]
        bindings[-1]["attachments"] = [{
            "name": "call_metadata", "connection": source_names["gong"], "entity": "extensive_calls",
            "table": metadata["table"], "record_id": "fixture_record_id",
            "local_key": resources["gong.call_transcripts"]["columns"]["callId"]["column"],
            "remote_key": "fixture_record_id",
            "customer_ref": {"connection": source_names["gong"], "entity": "extensive_calls", "column": "fixture_record_id"},
            "fields": {"metadata": metadata["columns"]["metaData"]["column"]},
        }]
        bindings[-1]["passages"] = [{"name": "call_statement", "field": "text", "text_path": "/*/sentences/*/text",
            "time": {"source": "attachment", "attachment": "call_metadata", "field": "metadata", "path": "/started",
                     "encoding": "rfc3339", "meaning": "event_started"}}]
        definitions = [{"name": "open_pipeline", "description": "Sum opportunity amounts where IsClosed is false, by source currency; not ARR.",
                        "kind": "commercial", "operation": "sum", "field": "amount", "filters": [{"field": "is_closed", "operator": "eq", "value": False}],
                        "unit": "currency", "currency_field": "currency"}]
    else:
        for company in data["intercom.companies"]:
            add("intercom", "companies", company["id"], assigned("stripe", "customers", company["custom_attributes"].get("billing_customer_id")))
        for contact in data["intercom.contacts"]:
            members, missing = set(), False
            for company in contact["companies"]["data"]:
                found = assigned("intercom", "companies", company["id"])
                missing |= not found
                members.update(found)
            derive("intercom", "contacts", contact["id"], members, missing)
        for conversation in data["intercom.conversations"]:
            members, missing = set(), False
            for contact in conversation["contacts"]["contacts"]:
                found = assigned("intercom", "contacts", contact["id"])
                missing |= not found or (source_names["intercom"], "contacts", contact["id"]) in incomplete
                members.update(found)
            derive("intercom", "conversations", conversation["id"], members, missing)
        bindings = [
            binding("stripe.customers", "customer", "stripe", "customers", "id", {"label": "name"}),
            binding("stripe.invoices", "billing", "stripe", "customers", "customer",
                    {"amount": "amount_remaining", "currency": "currency", "status": "status"}),
            binding("intercom.conversations", "support", "intercom", "conversations", "id", {"text": "source", "status": "state"}),
        ]
        bindings[1]["measures"] = {"amount": {
            "unit": "minor_currency", "currency_field": "currency", "aggregation": "sum",
            "meaning": "Authored synthetic fixture policy: remaining invoice amount is in minor units of its source currency and may be summed per currency. This does not establish recurring revenue or overdue status.",
        }}
        bindings[-1]["fields"]["updates"] = resources["intercom.conversations"]["columns"]["conversation_parts"]["column"]
        for role in ("created_at", "updated_at"):
            bindings[-1]["fields"][role] = resources["intercom.conversations"]["columns"][role]["column"]
        bindings[-1]["passages"] = [
            {"name": "original_message", "field": "text", "text_path": "/body",
             "time": {"source": "field", "field": "created_at", "path": "", "encoding": "unix_seconds", "meaning": "record_created"}},
            {"name": "support_update", "field": "updates", "text_path": "/conversation_parts/*/body",
             "time": {"source": "item", "path": "/created_at", "encoding": "unix_seconds", "meaning": "passage_created"}},
        ]
        definitions = [
            {"name": "outstanding_invoices", "description": "Sum remaining invoice minor units where status is open, separated by currency; not recurring revenue.",
             "kind": "billing", "operation": "sum", "field": "amount", "filters": [{"field": "status", "operator": "eq", "value": "open"}],
             "unit": "minor_currency", "currency_field": "currency"},
            {"name": "open_support_cases", "description": "Count attributed support conversation records where state is open.",
             "kind": "support", "operation": "count", "field": None, "filters": [{"field": "status", "operator": "eq", "value": "open"}],
             "unit": "records", "currency_field": None},
        ]

    good, proposals = [], []
    for (connection, entity, source_id), members in sorted(candidates.items()):
        existing = identity.resolve(workspace, connection, entity, source_id)
        invalid = len(members) != 1 or (connection, entity, source_id) in incomplete
        if existing["status"] == "resolved":
            if invalid or existing["customer_id"] not in members:
                raise ValueError("A previously reviewed fixture relationship changed; require a new explicit identity review.")
            continue
        if len(members) <= 1 and (connection, entity, source_id) in incomplete:
            continue
        links = [{"connection": connection, "entity": entity, "source_id": source_id, "customer_id": customer,
                  "basis": "Authored synthetic fixture review using explicit source references and mapping decisions"}
                 for customer in sorted(members)]
        if len(links) > 1:
            proposals.append(_pending_proposal(workspace, links))
        else:
            good.extend(links)
    if good:
        proposals.insert(0, _pending_proposal(workspace, good))
    return {"scope": "synthetic_sample_review_preparation", "profile": "customer_review",
            "sample_profile": profile_name, "phase": phase, "snapshot_id": snapshot["snapshot_id"],
            "customer_ids": customer_ids, "links": proposals,
            "mapping": {"spec_version": PROFILE_SPEC, "name": "customer_review",
                        "description": "Authored synthetic source mappings", "bindings": bindings},
            "definitions": definitions,
            "gaps": [{"code": "limited_source_coverage", "description": "Authored local fixture scope only; vendor completeness is unverified."}],
            "extraction": {key: extraction[key] for key in ("profile", "phase", "status", "source_names", "resources",
                                                           "actual_dlt_executed", "runtime_versions", "chunk_size")},
            "approvals_performed": False, "model_calls": 0, "vendor_adapter_verified": False,
            "human_business_review": False}


def _pending_proposal(workspace, links):
    """Reuse an unchanged pending proposal across UI refreshes and restarts."""
    with workspace._connect() as conn:
        head = identity.identity_head(conn)
        rows = conn.execute("SELECT p.payload,p.approval_sha256 FROM _contextbase.customer360_link_proposals p "
                            "LEFT JOIN _contextbase.customer360_link_reviews r ON r.proposal_id=p.proposal_id "
                            "WHERE r.proposal_id IS NULL LIMIT 1000").fetchall()
        for payload, proof in rows:
            value = json.loads(payload)
            if digest(value) != proof:
                raise ValueError("Stored synthetic identity proposal failed its consistency check.")
            if (value["workspace_id"] == head["workspace_id"] and value["mapping_version"] == head["version"]
                    and value["mapping_sha256"] == head["sha256"] and value["links"] == links):
                return {**value, "approval_sha256": proof}
    return identity.propose_links(workspace, links)


def _sample_profile(profile, phase="initial"):
    if profile not in ("salesforce-gong", "billing-support") or phase not in ("initial", "refreshed"):
        raise ValueError("Choose a declared synthetic sample profile and phase.")


def _sample_directory(workspace, *, create=False):
    directory = workspace.root / "customer360-samples"
    if directory.is_symlink():
        raise ValueError("Sample state directory cannot be a symlink.")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise ValueError("Connect a synthetic sample before preparing its review.")
    return directory


def connect(workspace, profile, phase="initial"):
    """Extract a known packaged sample without making any review decisions."""
    _sample_profile(profile, phase)
    directory = _sample_directory(workspace, create=True)
    with _lock(directory):
        state = {"spec": "oriel.customer360.sample-session/v1", "profile": profile, "phase": phase,
                 "status": "extracting", "started_at": _now()}
        _write(directory / "state.json", state)
        try:
            receipt = load_profile(workspace, profile, phase)
            state.update(status="extracted", receipt=str(Path(receipt["receipt_path"]).relative_to(workspace.root)),
                         receipt_sha256=_hash(receipt), finished_at=_now())
            _write(directory / "state.json", state)
        except Exception:
            state.update(status="failed", finished_at=_now())
            _write(directory / "state.json", state)
            raise
        return receipt


def prepare(workspace, profile):
    """Create pending review proposals from the last successful sample sync.

    Review links with identity.approve_links, then propose/review the mapping
    with the normal profiles APIs. Materialize it before proposing context.
    This operation never treats pending proposals as approved facts.
    """
    _sample_profile(profile)
    directory = _sample_directory(workspace)
    with _lock(directory):
        state = _read(directory / "state.json")
        if (state.get("spec") != "oriel.customer360.sample-session/v1" or state.get("profile") != profile
                or state.get("status") != "extracted"):
            raise ValueError("Require a successful extraction for this sample before preparing its review.")
        relative = state.get("receipt")
        if not isinstance(relative, str) or not re.fullmatch(r"customer360-fixture-runtime/run-[0-9a-f]{32}/receipt\.json", relative):
            raise ValueError("Invalid retained sample extraction reference.")
        path = workspace.root / relative
        if path.resolve() != path.absolute():
            raise ValueError("Retained sample extraction cannot follow symlinks.")
        extraction = _read(path)
        if (_hash(extraction) != state.get("receipt_sha256") or extraction.get("profile") != profile
                or extraction.get("phase") != state.get("phase")):
            raise ValueError("Retained sample extraction does not match its recorded session.")
        return _prepare_extraction(workspace, extraction)
