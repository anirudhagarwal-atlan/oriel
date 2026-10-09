"""A materialized local snapshot with a restricted agent query boundary.

Source imports are operator actions. Agent queries cannot read arbitrary files,
load extensions, connect to networks, or execute multiple statements.
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
from pathlib import Path
import random
import re
import threading
import tempfile
import time
import uuid

import duckdb


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Identifiers must start with a letter and contain only letters, digits, or underscores.")
    return '"' + value + '"'


def table_identifier(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 2:
        raise ValueError("Use a schema.table identifier.")
    return ".".join(identifier(part) for part in parts)


def serializable(value, *, lossless=False):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value) if lossless else float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): serializable(v, lossless=lossless) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(v, lossless=lossless) for v in value]
    return value


def canonical(value) -> str:
    return json.dumps(serializable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Workspace:
    def __init__(self, root: str | Path = ".oriel"):
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / "data.duckdb"

    def _connect(self, *, writable=False, imports=False):
        if not writable and not self.db_path.exists():
            raise ValueError("Workspace does not exist. Run oriel demo or oriel ingest first.")
        if writable:
            self.root.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(self.db_path), read_only=not writable, config={
            "enable_external_access": imports,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "allow_unsigned_extensions": False,
            "allow_persistent_secrets": False,
            "threads": 2,
            "memory_limit": "256MB",
        })
        return conn

    @staticmethod
    def _select(conn, sql):
        if not isinstance(sql, str) or len(sql) > 100_000:
            raise ValueError("Supply SQL text of at most 100,000 characters.")
        statements = conn.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("Agent queries must contain exactly one SELECT statement.")

    def query(self, sql: str, max_rows: int = 200, *, parameters=None) -> dict:
        from .refresh_state import require_readable_snapshot
        if type(max_rows) is not int or not 1 <= max_rows <= 10_000:
            raise ValueError("max_rows must be between 1 and 10,000.")
        with self._connect() as conn:
            require_readable_snapshot(self, connection=conn)
            self._select(conn, sql)
            timer = threading.Timer(10, conn.interrupt)
            timer.daemon = True
            timer.start()
            started = time.perf_counter()
            try:
                result = conn.execute(sql, parameters or [])
                columns = [d[0] for d in result.description]
                if len(columns) != len(set(columns)):
                    raise ValueError("Use unique aliases for duplicate output columns.")
                records = result.fetchmany(max_rows + 1)
                rows = [dict(zip(columns, serializable(row, lossless=True))) for row in records[:max_rows]]
                response = {"columns": columns, "rows": rows, "row_count": len(rows),
                            "truncated": len(records) > max_rows,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                            "freshness": "local_snapshot"}
                decimal_types = {d[0]: str(d[1]) for d in result.description if str(d[1]).startswith("DECIMAL")}
                if decimal_types:
                    response["decimal_columns"] = decimal_types
                if conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='_contextbase' AND table_name='rystic_snapshots'").fetchone()[0]:
                    snapshot = conn.execute("SELECT payload FROM _contextbase.rystic_snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
                    if snapshot:
                        metadata = json.loads(snapshot[0])
                        response["snapshot"] = {key: metadata[key] for key in ("snapshot_id", "observed_at", "refresh_mode", "consistency", "synthetic")}
                return response
            finally:
                timer.cancel()
                timer.join()

    def catalog(self) -> list[dict]:
        from .refresh_state import require_readable_snapshot
        from .context import _identifier
        with self._connect() as conn:
            require_readable_snapshot(self, connection=conn)
            tables = conn.execute("SELECT table_schema, table_name FROM information_schema.tables "
                                  "WHERE table_schema NOT IN ('information_schema','pg_catalog','_contextbase') "
                                  "AND table_type='BASE TABLE' ORDER BY 1,2").fetchall()
            result = []
            for schema, table in tables:
                columns = conn.execute("SELECT column_name,data_type FROM information_schema.columns "
                                       "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                                       [schema, table]).fetchall()
                count = conn.execute(f"SELECT count(*) FROM {_identifier(schema)}.{_identifier(table)}").fetchone()[0]
                result.append({"schema": schema, "table": table, "columns": [
                    {"name": name, "type": dtype} for name, dtype in columns], "row_count": count})
            return result

    def _metadata(self, conn):
        conn.execute("CREATE SCHEMA IF NOT EXISTS _contextbase")
        conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.imports "
                     "(source VARCHAR, target VARCHAR, imported_at TIMESTAMP, row_count BIGINT, sha256 VARCHAR)")
        conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.changes "
                     "(plan_id VARCHAR PRIMARY KEY, payload VARCHAR, sha256 VARCHAR, applied_at TIMESTAMP)")

    def is_synthetic(self):
        """Only untouched generated data and curated context may use implicit consent.

        A marker alone cannot authorize model access after imports, row edits,
        free-text transform SQL, custom context, or a planned change.
        """
        marker = self.root / "demo.json"
        if not marker.exists():
            return False
        try:
            metadata = json.loads(marker.read_text())
            if metadata.get("synthetic") is not True or type(metadata.get("seed")) is not int:
                return False
            with self._connect() as conn:
                if conn.execute("SELECT count(*) FROM _contextbase.imports").fetchone()[0] or conn.execute("SELECT count(*) FROM _contextbase.changes").fetchone()[0]:
                    return False
            from .context import load_context
            with tempfile.TemporaryDirectory(prefix="contextbase-synthetic-check-") as temporary:
                expected = init_demo(Path(temporary) / "data", accounts=metadata.get("accounts"), seed=metadata["seed"])
                if self.catalog() != expected.catalog():
                    return False
                for table in self.catalog():
                    name = table_identifier(table["schema"] + "." + table["table"])
                    # Count and hash in DuckDB to keep source rows inside the process.
                    query = f"SELECT sha256(COALESCE(string_agg(to_json(t), chr(10) ORDER BY to_json(t)), '')) AS row_hash, count(*) AS n FROM {name} t"
                    if self.query(query)["rows"] != expected.query(query)["rows"]:
                        return False
                contexts = [load_context(self), load_context(expected)]
                for context in contexts:
                    for metric in context.get("metrics", []):
                        metric.pop("execution_evidence", None)
                return contexts[0] == contexts[1]
        except (ValueError, OSError, duckdb.Error):
            return False

    def ingest(self, path: str | Path, schema: str, table: str, *, replace=False) -> dict:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("Import one local CSV, JSON, JSONL, or Parquet file.")
        readers = {".csv": "read_csv_auto", ".json": "read_json_auto", ".jsonl": "read_json_auto", ".parquet": "read_parquet"}
        reader = readers.get(source.suffix.lower())
        if reader is None:
            raise ValueError("Supported file extensions: .csv, .json, .jsonl, .parquet.")
        target = table_identifier(schema + "." + table)
        # Streaming hash avoids loading large source files into the agent process.
        sha = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha.update(chunk)
        started = time.perf_counter()
        with self._connect(writable=True, imports=True) as conn:
            conn.execute("SET allowed_paths = ?", [[str(source)]])
            conn.execute("SET enable_external_access = false")
            conn.begin()
            try:
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {identifier(schema)}")
                verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
                conn.execute(f"{verb} {target} AS SELECT * FROM {reader}(?)", [str(source)])
                count = conn.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
                self._metadata(conn)
                conn.execute("INSERT INTO _contextbase.imports VALUES (?,?,current_timestamp,?,?)",
                             [source.name, schema + "." + table, count, sha.hexdigest()])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"table": schema + "." + table, "rows": count, "sha256": sha.hexdigest(),
                "ingest_ms": round((time.perf_counter() - started) * 1000, 3)}

    def import_dinobase(self, database: str | Path, tables: list[str], *, parquet_root=None) -> dict:
        """Copy explicitly selected local base tables or local-file backed views.

        ATTACH is read-only. No upstream package/config/credentials are loaded.
        Views may read only Parquet files in the separately selected directory.
        Cloud-backed views and files outside that allowlist fail closed.
        """
        source = Path(database).expanduser().resolve(strict=True)
        if source == self.db_path or not source.is_file():
            raise ValueError("Select a separate local Dinobase database.")
        if not tables or len(tables) != len(set(tables)):
            raise ValueError("Select at least one unique schema.table to import.")
        targets = [(table, table_identifier(table)) for table in tables]
        allowed = [str(source), str(source) + ".wal"]
        if parquet_root is not None:
            given_dir = Path(parquet_root).expanduser().absolute()
            parquet_dir = Path(parquet_root).expanduser().resolve(strict=True)
            if not parquet_dir.is_dir():
                raise ValueError("parquet_root must be a local directory.")
            for path in given_dir.rglob("*.parquet"):
                if path.is_file() and path.resolve().is_relative_to(parquet_dir):
                    allowed.extend([str(path), str(path.resolve())])
        started = time.perf_counter()
        with self._connect(writable=True, imports=True) as conn:
            conn.execute("SET allowed_paths = ?", [allowed])
            conn.execute("SET enable_external_access = false")
            # DuckDB ATTACH does not accept bind parameters; use SQL string escaping.
            path_literal = str(source).replace("'", "''")
            conn.execute(f"ATTACH '{path_literal}' AS upstream (READ_ONLY)")
            conn.begin()
            try:
                copied = []
                for name, target in targets:
                    schema = name.split(".")[0]
                    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {identifier(schema)}")
                    conn.execute(f"CREATE TABLE {target} AS SELECT * FROM upstream.{target}")
                    count = conn.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
                    copied.append({"table": name, "rows": count})
                self._metadata(conn)
                for item in copied:
                    conn.execute("INSERT INTO _contextbase.imports VALUES (?,?,current_timestamp,?,?)",
                                 ["dinobase:" + source.name, item["table"], item["rows"], None])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"tables": copied, "ingest_ms": round((time.perf_counter() - started) * 1000, 3),
                "source_access": "read_only", "upstream_modified": False}

    def transform(self, name: str, sql: str) -> dict:
        from .refresh_state import require_readable_snapshot
        target = "models." + identifier(name)
        with self._connect(writable=True) as conn:
            timer = threading.Timer(10, conn.interrupt)
            timer.daemon = True
            timer.start()
            try:
                conn.begin()
                require_readable_snapshot(self, connection=conn)
                self._select(conn, sql)
                conn.execute("CREATE SCHEMA IF NOT EXISTS models")
                conn.execute(f"CREATE TABLE {target} AS {sql}")
                count = conn.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
                self._metadata(conn)
                conn.execute("INSERT INTO _contextbase.imports VALUES (?,?,current_timestamp,?,?)",
                             ["local_transform", "models." + name, count, digest(sql)])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                timer.cancel()
                timer.join()
        return {"table": "models." + name, "rows": count, "sql_sha256": digest(sql), "scope": "local_only"}

    @staticmethod
    def _change_columns(conn, target: str, key: str, changes: dict) -> dict[str, str]:
        from .context import _identifier
        columns = [column[0] for column in conn.execute(f"SELECT * FROM {target} LIMIT 0").description]
        if not isinstance(key, str) or key not in columns:
            raise ValueError("Unknown lookup column.")
        if not isinstance(changes, dict) or not changes or key in changes:
            raise ValueError("Supply changed columns without modifying the lookup key.")
        if any(not isinstance(column, str) or column not in columns for column in changes):
            raise ValueError("Unknown changed column.")
        # Imported column names may contain spaces, underscores or quotes.
        # Only exact observed names reach the shared catalog-identifier quoter.
        return {column: _identifier(column) for column in [key, *changes]}

    def plan_change(self, table: str, key: str, value, changes: dict) -> dict:
        from .refresh_state import require_readable_snapshot
        target = table_identifier(table)
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Supply changed columns without modifying the lookup key.")
        for changed in changes.values():
            if not isinstance(changed, (str, int, float, bool, type(None))):
                raise ValueError("Change values must be JSON scalars.")
        with self._connect() as conn:
            require_readable_snapshot(self, connection=conn)
            quoted = self._change_columns(conn, target, key, changes)
            key_sql = quoted[key]
            cursor = conn.execute(f"SELECT * FROM {target} WHERE {key_sql}=? LIMIT 2", [value])
            columns = [d[0] for d in cursor.description]
            before = [serializable(dict(zip(columns, row)), lossless=True) for row in cursor.fetchall()]
        if len(before) != 1:
            raise ValueError("A change must target exactly one existing row by a unique key.")
        payload = {"plan_id": str(uuid.uuid4()), "table": table, "key": key, "value": value,
                   "before": before[0], "after": {**before[0], **changes}, "changes": changes, "scope": "local_only"}
        sha = digest(payload)
        with self._connect(writable=True) as conn:
            conn.begin()
            try:
                require_readable_snapshot(self, connection=conn)
                self._metadata(conn)
                conn.execute("INSERT INTO _contextbase.changes VALUES (?,?,?,NULL)", [payload["plan_id"], canonical(payload), sha])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {**payload, "approval_sha256": sha}

    def apply_change(self, plan_id: str, approval_sha256: str) -> dict:
        from .refresh_state import require_readable_snapshot
        with self._connect(writable=True) as conn:
            conn.begin()
            try:
                require_readable_snapshot(self, connection=conn)
                self._metadata(conn)
                record = conn.execute("SELECT payload,sha256,applied_at FROM _contextbase.changes WHERE plan_id=?", [plan_id]).fetchone()
                if not record:
                    raise ValueError("Unknown change plan.")
                payload = json.loads(record[0])
                if record[2] is not None:
                    raise ValueError("This change was already applied.")
                if approval_sha256 != record[1] or digest(payload) != approval_sha256:
                    raise ValueError("Approval must match the exact preview SHA-256.")
                target = table_identifier(payload["table"])
                quoted = self._change_columns(conn, target, payload["key"], payload["changes"])
                key = quoted[payload["key"]]
                cursor = conn.execute(f"SELECT * FROM {target} WHERE {key}=?", [payload["value"]])
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                if len(rows) != 1 or serializable(dict(zip(columns, rows[0])), lossless=True) != payload["before"]:
                    raise ValueError("The row changed since preview. Create and review a new plan.")
                assignments = ",".join(quoted[column] + "=?" for column in payload["changes"])
                conn.execute(f"UPDATE {target} SET {assignments} WHERE {key}=?",
                             [*payload["changes"].values(), payload["value"]])
                conn.execute("UPDATE _contextbase.changes SET applied_at=current_timestamp WHERE plan_id=?", [plan_id])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"plan_id": plan_id, "applied": True, "scope": "local_only", "upstream_modified": False}


def init_demo(root: str | Path = ".oriel", accounts=40, seed=7) -> Workspace:
    if type(accounts) is not int or not 4 <= accounts <= 10_000:
        raise ValueError("Demo account count must be between 4 and 10,000.")
    w = Workspace(root)
    if w.db_path.exists():
        raise ValueError("Workspace already exists. Choose a new --workspace path to preserve your data.")
    rng = random.Random(seed)
    started = time.perf_counter()
    with w._connect(writable=True) as conn:
        for schema in ("crm", "billing", "product", "support"):
            conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute("CREATE TABLE crm.accounts(account_id VARCHAR PRIMARY KEY,name VARCHAR,segment VARCHAR,health VARCHAR)")
        conn.execute("CREATE TABLE crm.opportunities(opportunity_id VARCHAR PRIMARY KEY,account_id VARCHAR,stage VARCHAR,arr DOUBLE,tcv DOUBLE,close_date DATE)")
        conn.execute("CREATE TABLE billing.subscriptions(subscription_id VARCHAR PRIMARY KEY,account_id VARCHAR,status VARCHAR,mrr DOUBLE)")
        conn.execute("CREATE TABLE product.usage(account_id VARCHAR,period VARCHAR,active_users BIGINT)")
        conn.execute("CREATE TABLE support.tickets(ticket_id VARCHAR PRIMARY KEY,account_id VARCHAR,status VARCHAR,priority VARCHAR)")
        for i in range(1, accounts + 1):
            account_id = f"customer_{i:03d}"
            conn.execute("INSERT INTO crm.accounts VALUES (?,?,?,?)", [account_id, f"Example account {i:03d}", ["startup", "midmarket", "enterprise"][i % 3], "unreviewed"])
            for j in range(1, 1 + (2 if i % 5 == 0 else 1)):
                status = "active" if i % 6 else "canceled"
                mrr = float(rng.randrange(10, 150) * 100) if i % 9 else 0.0
                conn.execute("INSERT INTO billing.subscriptions VALUES (?,?,?,?)", [f"sub_{i}_{j}", account_id, status, mrr])
            previous = rng.randrange(50, 300)
            current = int(previous * (0.45 if i % 3 == 0 else 1.1))
            conn.executemany("INSERT INTO product.usage VALUES (?,?,?)", [(account_id, "previous", previous), (account_id, "current", current)])
            for j in range(1, 3 if i % 4 == 0 else 2):
                conn.execute("INSERT INTO support.tickets VALUES (?,?,?,?)", [f"ticket_{i}_{j}", account_id, "open" if i % 2 else "closed", "high" if i % 3 == 0 else "low"])
            for j in range(1, 3):
                arr = float(rng.randrange(2, 40) * 12000)
                date = dt.date(2026, 7, 1) + dt.timedelta(days=rng.randrange(160))
                conn.execute("INSERT INTO crm.opportunities VALUES (?,?,?,?,?,?)", [f"opp_{i}_{j}", account_id, ["S1", "S2", "S3", "S4", "closed_won"][rng.randrange(5)], arr, arr * 3, date])
        w._metadata(conn)
    metadata = {"synthetic": True, "seed": seed, "accounts": accounts, "as_of": "2026-10-08",
                "fiscal_quarter": {"start": "2026-08-01", "end_exclusive": "2026-11-01"},
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ingest_ms": round((time.perf_counter() - started) * 1000, 3)}
    (w.root / "demo.json").write_text(json.dumps(metadata, indent=2) + "\n")
    from .context import install_demo_context
    install_demo_context(w)
    return w
