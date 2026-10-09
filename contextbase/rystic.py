"""Explicitly local Rystic clients and atomic, source-backed snapshots.

These adapters do not connect to Stripe or Linear production services. Source
records stay on numeric loopback addresses; no provider credentials are needed.
Rystic itself has a separate licensed runtime and network check-in policy.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .workspace import canonical, table_identifier

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 1000
PINS = {"stripe": ("stripe-twin", "0.0.8"), "linear": ("linear-twin", "0.0.1")}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Simulator returned duplicate JSON fields.")
        value[key] = item
    return value


def _decode(raw):
    def invalid(_):
        raise ValueError("Simulator returned non-finite JSON.")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Simulator returned invalid JSON.") from exc


def local_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
                 and parsed.port and 1 <= parsed.port <= 65535
                 and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Rystic URLs must be http://127.0.0.1:PORT or http://[::1]:PORT with no path or credentials.")
    return value.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Simulator redirects are refused.")


class RysticClient:
    def __init__(self, url: str):
        self.url = local_url(url)
        # Do not let shell HTTP_PROXY settings route synthetic API payloads out.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.trace = []
        self.request_guard = None

    def request(self, method, path, *, query=None, form=None, json_body=None):
        if method not in {"GET", "POST"} or not isinstance(path, str) or not re.fullmatch(
                r"/(?:v1/[a-z_]+(?:/[A-Za-z0-9_-]+)?|graphql|_rystic/(?:version|routes))", path):
            raise ValueError("Unsupported simulator request route.")
        if (form is not None and json_body is not None) or (method == "GET" and (form is not None or json_body is not None)):
            raise ValueError("Invalid simulator request body.")
        if path.startswith("/_rystic/") and method != "GET":
            raise ValueError("Simulator control writes are not supported.")
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        url = self.url + path + (("?" + urlencode(query, doseq=True)) if query else "")
        request = Request(url, method=method, data=data, headers=headers)
        timeout = 15
        if self.request_guard is not None:
            if not callable(self.request_guard):
                raise ValueError("Simulator request guard must be callable or None.")
            remaining = self.request_guard()
            try:
                valid = type(remaining) in (int, float) and math.isfinite(remaining) and remaining > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Simulator request guard must return positive finite remaining seconds.")
            timeout = min(timeout, remaining)
        # A guard refusal is not a dispatched HTTP request and gets no request
        # trace. This bounds the socket timeout, not the OS or remote operation:
        # an in-flight call may still overrun; every later call checks again.
        started = time.perf_counter()
        status, received = None, 0
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                received = len(raw)
                if received > MAX_RESPONSE_BYTES:
                    raise ValueError("Simulator response exceeded the bounded size; narrow the request.")
                return _decode(raw) if raw else {}
        except HTTPError as exc:
            status = exc.code
            raise ValueError(f"Simulator request failed with HTTP {exc.code}; check its supported API and runtime status.") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("Local simulator is unavailable or timed out; check the Rystic instance and trial/license.") from None
        finally:
            self.trace.append({"method": method, "path": path, "status": status,
                               "request_body_bytes": len(data or b""), "response_bytes": received,
                               "elapsed_seconds": round(time.perf_counter() - started, 6)})

    def graphql(self, query, variables=None):
        result = self.request("POST", "/graphql", json_body={"query": query, "variables": variables or {}})
        if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
            raise ValueError("Simulator GraphQL operation failed; check supported fields and inputs.")
        return result["data"]


class RysticPair:
    def __init__(self, stripe_url, linear_url):
        self.stripe = RysticClient(stripe_url)
        self.linear = RysticClient(linear_url)
        if self.stripe.url == self.linear.url:
            raise ValueError("Select separate Stripe and Linear simulator instances.")

    def verify(self):
        result = {}
        for name, client in (("stripe", self.stripe), ("linear", self.linear)):
            info = client.request("GET", "/_rystic/version")
            product, version = PINS[name]
            if not isinstance(info, dict) or info.get("product") != product or info.get("version") != version:
                raise ValueError(f"Expected the pinned {product} {version} simulator; refusing source access.")
            result[name] = {key: info[key] for key in ("product", "version", "model_hash", "model", "initial_clock", "edition") if key in info}
        return result


def stripe_pages(client, resource, *, query=None):
    if resource not in {"customers", "charges", "refunds", "payment_intents"}:
        raise ValueError("Unsupported Stripe list resource.")
    rows, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        params = {**(query or {}), "limit": 100}
        if cursor:
            params["starting_after"] = cursor
        response = client.request("GET", "/v1/" + resource, query=params)
        page = response.get("data") if isinstance(response, dict) else None
        if not isinstance(page, list) or type(response.get("has_more")) is not bool:
            raise ValueError("Stripe simulator returned an incomplete pagination envelope.")
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                raise ValueError("Stripe simulator pagination repeated or omitted an identity.")
            seen.add(item["id"])
            rows.append(item)
        if not response["has_more"]:
            return rows
        if not page:
            raise ValueError("Stripe simulator pagination did not advance.")
        cursor = page[-1]["id"]
    raise ValueError("Stripe simulator exceeded the page budget.")


ISSUE_FIELDS = "id identifier title description priority state { id name type } assignee { id name } updatedAt"


def linear_issues(client):
    rows, cursor, seen = [], None, set()
    query = "query SnapshotIssues($after: String) { issues(first: 100, after: $after, includeArchived: true) { nodes { " + ISSUE_FIELDS + " } pageInfo { hasNextPage endCursor } } }"
    for _ in range(MAX_PAGES):
        data = client.graphql(query, {"after": cursor})
        connection = data.get("issues", {})
        page, info = connection.get("nodes"), connection.get("pageInfo")
        if not isinstance(page, list) or not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
            raise ValueError("Linear simulator returned an incomplete pagination envelope.")
        for item in page:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                raise ValueError("Linear simulator pagination repeated or omitted an identity.")
            seen.add(item["id"])
            rows.append(item)
        if not info["hasNextPage"]:
            return rows
        following = info.get("endCursor")
        if not page or not isinstance(following, str) or not following or following == cursor:
            raise ValueError("Linear simulator pagination did not advance.")
        cursor = following
    raise ValueError("Linear simulator exceeded the page budget.")


SCHEMAS = {
    "stripe.customers": [("customer_id", "VARCHAR"), ("account_id", "VARCHAR"), ("name", "VARCHAR"), ("is_internal", "BOOLEAN")],
    "stripe.charges": [("charge_id", "VARCHAR"), ("customer_id", "VARCHAR"), ("amount_cents", "BIGINT"), ("amount_refunded_cents", "BIGINT"), ("currency", "VARCHAR"), ("paid", "BOOLEAN"), ("captured", "BOOLEAN"), ("status", "VARCHAR"), ("disputed", "BOOLEAN"), ("created_at", "VARCHAR")],
    "linear.issues": [("issue_id", "VARCHAR"), ("identifier", "VARCHAR"), ("account_id", "VARCHAR"), ("title", "VARCHAR"), ("priority", "INTEGER"), ("state_type", "VARCHAR"), ("state_name", "VARCHAR"), ("owner_id", "VARCHAR"), ("owner_name", "VARCHAR"), ("updated_at", "VARCHAR")],
}


def _text(value, field, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not nullable and not value):
        raise ValueError(f"Source record is missing a valid {field}.")
    return value


def _integer(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"Source record is missing a valid {field}.")
    return value


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError(f"Source record is missing a valid {field}.")
    return value


def normalize_snapshot(customer_rows, charge_rows, issue_rows, namespace):
    """Normalize observed source records without network, workspace, or gold.

    Iterables are consumed in customer/charge/issue order. Keeping this helper
    strict preserves the ordinary import path's identity and policy checks.
    """
    return _normalize_snapshot(customer_rows, charge_rows, issue_rows, namespace)


def _normalize_snapshot(customer_rows, charge_rows, issue_rows, namespace, *, _customer_guard_mode=False):
    """Shared pure normalization; guarded diagnostics never produce an import.

    The private guard mode only tolerates missing customer policy and duplicate
    logical mappings so the validator can inspect every remaining source field.
    Callers must discard these tables if any customer blocker exists. Orphan
    issue mappings remain unresolved under duplicate customer identity; their
    physical, observed account IDs are retained without guessed reassignment.
    """
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", namespace):
        raise ValueError("Invalid synthetic fixture namespace.")
    customers = []
    for item in customer_rows:
        meta = item.get("metadata") or {}
        if meta.get("contextbase_fixture") != namespace:
            continue
        policy = meta.get("is_internal")
        if policy not in {"true", "false"} and not (_customer_guard_mode and policy is None):
            raise ValueError("Synthetic customer is missing its explicit internal-account policy.")
        customers.append({"customer_id": _text(item.get("id"), "customer_id"),
                          "account_id": _text(meta.get("account_id"), "account_id"),
                          "name": _text(item.get("name"), "customer name"),
                          "is_internal": None if policy is None else policy == "true"})
    if not customers:
        raise ValueError("No synthetic customers found for this namespace; seed the recipe first.")
    customer_ids = {item["customer_id"] for item in customers}
    account_ids = {item["account_id"] for item in customers}
    duplicate_accounts = len(account_ids) != len(customers)
    if duplicate_accounts and not _customer_guard_mode:
        raise ValueError("Ambiguous customer-to-account identity mapping.")
    charges = []
    for item in charge_rows:
        if item.get("customer") not in customer_ids:
            continue
        amount = _integer(item.get("amount"), "charge amount")
        refunded = _integer(item.get("amount_refunded"), "refunded amount")
        if refunded > amount:
            raise ValueError("Refunded amount exceeds its charge.")
        created = _integer(item.get("created"), "creation timestamp")
        charges.append({"charge_id": _text(item.get("id"), "charge_id"), "customer_id": item["customer"],
                        "amount_cents": amount, "amount_refunded_cents": refunded,
                        "currency": _text(item.get("currency"), "currency").lower(),
                        "paid": _boolean(item.get("paid"), "paid flag"), "captured": _boolean(item.get("captured"), "captured flag"),
                        "status": _text(item.get("status"), "payment status"),
                        "disputed": _boolean(item.get("disputed"), "disputed flag"),
                        "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat()})
    issues, orphan_ids = [], []
    for item in issue_rows:
        description = item.get("description") or ""
        namespaces = re.findall(r"^contextbase_fixture=([a-z0-9_]+)$", description, re.MULTILINE)
        if namespace not in namespaces:
            continue
        matches = re.findall(r"^contextbase_account_id=([A-Za-z0-9_-]+)$", description, re.MULTILINE)
        if len(namespaces) != 1 or len(matches) != 1:
            raise ValueError("Issue has ambiguous or missing explicit account mapping.")
        account_id = matches[0]
        if account_id not in account_ids:
            orphan_ids.append(item["id"])
        state, owner = item.get("state") or {}, item.get("assignee") or {}
        issues.append({"issue_id": _text(item.get("id"), "issue_id"),
                       "identifier": _text(item.get("identifier"), "issue identifier"), "account_id": account_id,
                       "title": _text(item.get("title"), "issue title"), "priority": _integer(item.get("priority"), "priority"),
                       "state_type": _text(state.get("type"), "workflow state type"),
                       "state_name": _text(state.get("name"), "workflow state name"),
                       "owner_id": _text(owner.get("id"), "owner_id", nullable=True),
                       "owner_name": _text(owner.get("name"), "owner name", nullable=True),
                       "updated_at": _text(item.get("updatedAt"), "update timestamp")})
    if orphan_ids and not (_customer_guard_mode and duplicate_accounts):
        raise ValueError("Synthetic issues reference unknown accounts; resolve identity mapping before sync.")
    tables = {"stripe.customers": customers, "stripe.charges": charges, "linear.issues": issues}
    for name, rows in tables.items():
        rows.sort(key=lambda row: row[SCHEMAS[name][0][0]])
    return tables


def collect_snapshot(pair, namespace):
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", namespace):
        raise ValueError("Invalid synthetic fixture namespace.")
    identities = pair.verify()
    started = time.perf_counter()

    def charges():
        yield from stripe_pages(pair.stripe, "charges")

    def issues():
        yield from linear_issues(pair.linear)

    # Lazy iterators preserve the existing fail-before-later-reads behavior.
    tables = normalize_snapshot(stripe_pages(pair.stripe, "customers"), charges(), issues(), namespace)
    return tables, {"namespace": namespace, "simulators": identities,
                    "source_read_seconds": round(time.perf_counter() - started, 6),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "consistency": "sequential API reads; run with quiescent simulators for a consistent snapshot",
                    "synthetic": True, "scope": "Only the explicitly marked recipe cohort", "refresh_mode": "full_cohort_replace"}


def snapshot_info(workspace):
    if not workspace.db_path.exists():
        return None
    with workspace._connect() as conn:
        if not conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='_contextbase' AND table_name='rystic_snapshots'").fetchone()[0]:
            return None
        row = conn.execute("SELECT payload FROM _contextbase.rystic_snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None


def _table_hash(conn, name):
    relation = table_identifier(name)
    return conn.execute(f"SELECT sha256(COALESCE(string_agg(to_json(t), chr(10) ORDER BY to_json(t)), '')) FROM {relation} t").fetchone()[0]


def _reserve_capture_output(workspace, path):
    target = Path(path)
    database = workspace.db_path.resolve()
    if target.resolve() in {database, Path(str(database) + ".wal"),
                            Path(str(workspace.db_path) + ".wal").resolve()}:
        raise ValueError("Capture output cannot be the workspace database or its WAL file.")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Capture output already exists; select a new file.") from None
    try:
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def sync_snapshot(workspace, pair, namespace, *, capture_output=None):
    """Observe sources and atomically import valid rows, optionally retaining evidence.

    An optional new private file retains exactly this attempt's full validation
    capture, including refusals. It is flushed and closed before any successful
    import. Its receipt describes observation, so ``workspace_imported`` remains
    false even when the separate workspace transaction subsequently succeeds.
    An interrupted or failed capture write may leave an incomplete file; the
    corresponding refresh attempt stays blocked rather than certifying it.
    """
    handle = _reserve_capture_output(workspace, capture_output) if capture_output is not None else None
    try:
        return _sync_snapshot(workspace, pair, namespace, capture_handle=handle)
    finally:
        if handle is not None and not handle.closed:
            # All successful paths close before materialization. Cleanup must
            # not replace a recorded failure with an incidental close error.
            try:
                handle.close()
            except OSError:
                pass


def _sync_snapshot(workspace, pair, namespace, *, capture_handle):
    from .refresh_state import begin_refresh, finish_failed_refresh
    from .rystic_validation import capture_validation
    if workspace.db_path.exists() and snapshot_info(workspace) is None:
        with workspace._connect() as conn:
            existing = {schema + "." + table for schema, table in conn.execute("SELECT table_schema,table_name FROM information_schema.tables").fetchall()}
        if existing.intersection(SCHEMAS):
            raise ValueError("Refusing to overwrite source tables not managed by this adapter.")
    attempt = begin_refresh(workspace, namespace)
    validation = None
    try:
        observed = capture_validation(pair, namespace)
        validation = observed["receipt"]
    except Exception:
        finish_failed_refresh(workspace, attempt["attempt_id"], outcome="unverified")
        raise
    if capture_handle is not None:
        try:
            json.dump(observed, capture_handle, indent=2, sort_keys=True, allow_nan=False)
            capture_handle.write("\n")
            capture_handle.flush()
            os.fsync(capture_handle.fileno())
            capture_handle.close()
        except Exception:
            finish_failed_refresh(workspace, attempt["attempt_id"], outcome="storage_failed", validation=validation)
            raise ValueError("Source validation capture could not be retained. Historical rows are preserved; inspect refresh_status for the failed attempt.") from None
    if validation["validation_status"] != "valid":
        outcome = "refused_invalid_source" if validation["validation_status"] == "blocked" else "unverified"
        finish_failed_refresh(workspace, attempt["attempt_id"], outcome=outcome, validation=validation)
        raise ValueError("Source refresh was refused or could not be verified. Historical rows are preserved; inspect refresh_status for the recorded evidence.")
    receipt = {key: validation[key] for key in ("namespace", "simulators", "source_read_seconds", "observed_at", "consistency", "synthetic")}
    receipt.update({"scope": "Only the explicitly marked recipe cohort", "refresh_mode": "full_cohort_replace",
                    "source_validation_sha256": hashlib.sha256(canonical(validation).encode()).hexdigest()})
    try:
        return _store_snapshot(workspace, observed["tables"], receipt,
                               refresh_attempt_id=attempt["attempt_id"], validation=validation)
    except Exception:
        # A failed import rolls back rows and receipt together. Record failure
        # separately so an old success cannot be mistaken for current data.
        finish_failed_refresh(workspace, attempt["attempt_id"], outcome="storage_failed", validation=validation)
        raise


def load_example(workspace):
    """Load the bundled, hash-checked capture from an actual synthetic Rystic run."""
    from importlib.resources import files
    raw = files("contextbase").joinpath("data/rystic-stripe-linear.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != "a8026f8117968014b2531c177dd1dd3ef4354f16f027c4327a4988f6eec061df":
        raise ValueError("Bundled Rystic capture hash mismatch.")
    captured = _decode(raw)
    receipt = dict(captured["source"])
    receipt.update({"refresh_mode": "bundled_capture", "source_read_seconds": 0,
                    "loaded_at": datetime.now(timezone.utc).isoformat(),
                    "consistency": "Fixed capture from verified Rystic simulators; not a live source read or refresh."})
    return _store_snapshot(workspace, captured["tables"], receipt)


def _store_snapshot(workspace, tables, receipt, *, refresh_attempt_id=None, validation=None):
    receipt["data_sha256"] = hashlib.sha256(canonical(tables).encode()).hexdigest()
    receipt["snapshot_id"] = "rystic-" + receipt["data_sha256"][:16]
    receipt["rows"] = {name: len(rows) for name, rows in tables.items()}
    previous = snapshot_info(workspace)
    if previous and previous["namespace"] != receipt["namespace"]:
        raise ValueError("This workspace belongs to a different recipe namespace; use a new workspace.")
    with workspace._connect(writable=True) as conn:
        conn.begin()
        try:
            existing = {schema + "." + table for schema, table in conn.execute("SELECT table_schema,table_name FROM information_schema.tables").fetchall()}
            if not previous and existing.intersection(SCHEMAS):
                raise ValueError("Refusing to overwrite source tables not managed by this adapter.")
            if previous:
                for name in SCHEMAS:
                    if name not in existing or _table_hash(conn, name) != previous["table_sha256"][name]:
                        raise ValueError("Local source rows changed since the last sync; preserve the changes and use a new workspace.")
            for name, fields in SCHEMAS.items():
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {table_identifier(name).split('.')[0]}")
                definitions = ",".join('"' + column + '" ' + kind for column, kind in fields)
                conn.execute(f"CREATE OR REPLACE TABLE {table_identifier(name)} ({definitions})")
                if tables[name]:
                    placeholders = ",".join("?" for _ in fields)
                    conn.executemany(f"INSERT INTO {table_identifier(name)} VALUES ({placeholders})",
                                     [[row[column] for column, _ in fields] for row in tables[name]])
            receipt["table_sha256"] = {name: _table_hash(conn, name) for name in SCHEMAS}
            receipt["previous_snapshot_id"] = previous["snapshot_id"] if previous else None
            receipt["data_changed"] = not previous or previous["data_sha256"] != receipt["data_sha256"]
            workspace._metadata(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.rystic_snapshots (sequence BIGINT, payload VARCHAR)")
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM _contextbase.rystic_snapshots").fetchone()[0]
            conn.execute("INSERT INTO _contextbase.rystic_snapshots VALUES (?,?)", [sequence, canonical(receipt)])
            for name, count in receipt["rows"].items():
                conn.execute("INSERT INTO _contextbase.imports VALUES (?,?,current_timestamp,?,?)",
                             ["rystic:" + name.split('.')[0], name, count, receipt["table_sha256"][name]])
            if refresh_attempt_id is not None:
                from .refresh_state import _finish
                _finish(conn, refresh_attempt_id, outcome="materialized", validation=validation,
                        materialized_snapshot=receipt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return receipt


def save_receipt(path, value):
    target = Path(path)
    if target.exists():
        raise ValueError("Receipt already exists; select a new file.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
