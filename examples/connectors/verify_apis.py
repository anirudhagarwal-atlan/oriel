"""Actual dlt REST/GraphQL → DuckDB verification using synthetic loopback APIs.

Run only after the optional pinned runtime is approved and installed. Importing
this module starts no server and imports no third-party dependencies. Missing
runtime is a failure before fixture creation, never a successful skipped check.
The example-only launcher changes endpoints; production adapters, extraction,
publication, context and queries execute unchanged. This is not vendor testing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit


SYNTHETIC_TOKEN = "contextbase-synthetic-fixture-only"
CONTACTS_INITIAL = [("contact_001", "active", 3), ("contact_002", "active", 7), ("contact_003", "inactive", 50)]
CONTACTS_REFRESHED = [("contact_001", "active", 4), ("contact_002", "inactive", 7), ("contact_004", "active", 11)]
ISSUES_INITIAL = [("issue_001", "contact_001", 1, "started"), ("issue_002", "contact_002", 1, "started"),
                  ("issue_003", "contact_003", 1, "started"), ("issue_004", "contact_001", 3, "started"),
                  ("issue_005", "contact_002", 1, "completed")]
ISSUES_REFRESHED = [("issue_001", "contact_001", 1, "started"), ("issue_002", "contact_002", 1, "started"),
                    ("issue_004", "contact_001", 3, "started"), ("issue_006", "contact_004", 1, "started")]
SQL = ("SELECT SUM(c.custom_attributes__seats) AS active_seats_at_risk FROM crm.contacts c "
       "WHERE c.custom_attributes__account_status = 'active' AND EXISTS "
       "(SELECT 1 FROM tracker.issues i WHERE i.description = c.id AND i.priority = 1 "
       "AND i.state__type = 'started')")


def expected(contacts, issues):
    """Independent Python membership oracle; never derives gold from SQL/output."""
    qualifying = {account for _, account, priority, state in issues if priority == 1 and state == "started"}
    return sum(seats for account, status, seats in contacts if status == "active" and account in qualifying)


def expected_rows(contacts, issues):
    return {
        "contacts": [{"id": key, "status": status, "seats": seats} for key, status, seats in sorted(contacts)],
        "issues": [{"id": key, "account_id": account, "priority": priority, "state": state}
                   for key, account, priority, state in sorted(issues)],
    }


def prerequisites():
    versions = {}
    for name, required in (("dlt", "1.24.0"), ("PyYAML", "6.0.3"), ("duckdb", "1.4.5"), ("requests", None)):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ValueError("Install the explicitly approved connector runtime before this verification.") from None
        if required and versions[name] != required:
            raise ValueError("This verification requires the reviewed connector runtime versions.")
    if not (3, 10) <= sys.version_info[:2] < (3, 15):
        raise ValueError("The reviewed connector runtime requires Python 3.10 through 3.14.")
    return versions


class FixtureServer:
    """Two read-only protocols, strict synthetic auth and deterministic pagination."""

    def __init__(self):
        self.phase = "initial"
        self.failure = None
        self.requests = []
        self._lock = threading.Lock()
        self.server = None
        self.thread = None

    def set_state(self, phase, *, failure=None):
        if phase not in {"initial", "refreshed"} or failure not in {None, "rest", "graphql"}:
            raise ValueError("Unknown synthetic fixture state.")
        with self._lock:
            self.phase, self.failure = phase, failure

    def _response(self, method, target, headers, body):
        parsed = urlsplit(target)
        with self._lock:
            phase, failure = self.phase, self.failure
        protocol = "rest" if parsed.path == "/rest/contacts" else "graphql" if parsed.path == "/graphql" else None
        token = ("Bearer " if protocol == "rest" else "") + SYNTHETIC_TOKEN
        status, payload, offset = 400, {"error": "invalid synthetic request"}, None
        if headers.get("Host") != self.origin.removeprefix("http://") or parsed.netloc or parsed.fragment:
            status = 400
        elif protocol is None:
            status = 404
        elif headers.get("Authorization") != token:
            status = 401
        else:
            try:
                if protocol == "rest":
                    if method != "GET" or body or headers.get("Intercom-Version") != "2.10":
                        raise ValueError()
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                    if set(query) - {"starting_after"} or len(query.get("starting_after", [""])) != 1:
                        raise ValueError()
                    cursor = query.get("starting_after", [None])[0]
                else:
                    if method != "POST" or parsed.query or headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError()
                    request = json.loads(body)
                    query, variables = request["query"], request.get("variables", {})
                    if (set(request) != {"query", "variables"} or not isinstance(query, str)
                            or not query.lstrip().startswith("query") or re.search(r"\bmutation\b", query)
                            or not re.search(r"\bissues\(first:\s*100,\s*after:\s*\$cursor", query)
                            or not isinstance(variables, dict) or set(variables) - {"cursor"}):
                        raise ValueError()
                    cursor = variables.get("cursor")
                if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"offset_[24]", cursor)):
                    raise ValueError()
                offset = 0 if cursor is None else int(cursor.split("_")[1])
                contacts = CONTACTS_INITIAL if phase == "initial" else CONTACTS_REFRESHED
                issues = ISSUES_INITIAL if phase == "initial" else ISSUES_REFRESHED
                if protocol == "rest":
                    rows = [{"id": key, "custom_attributes": {"account_status": state, "seats": seats}}
                            for key, state, seats in contacts]
                else:
                    rows = [{"id": key, "description": account, "priority": priority,
                             "state": {"id": "state_" + state, "name": state, "type": state}}
                            for key, account, priority, state in issues]
                if offset >= len(rows):
                    raise ValueError()
                rows = copy.deepcopy(rows)
                if failure == protocol:
                    if offset:
                        status, payload = 503, {"error": "synthetic page two failure"}
                    else:
                        # A partial extract must never replace the committed data.
                        if protocol == "rest":
                            rows[0]["custom_attributes"]["seats"] = 99999
                        else:
                            rows[0]["description"] = "unpublished_partial_account"
                        status = 200
                else:
                    status = 200
                if status == 200:
                    page = rows[offset:offset + 2]
                    more = offset + 2 < len(rows)
                    next_cursor = f"offset_{offset + 2}" if more else None
                    if protocol == "rest":
                        payload = {"data": page, "pages": {"next": {"starting_after": next_cursor} if more else None}}
                    else:
                        payload = {"data": {"issues": {"nodes": page, "pageInfo": {
                            "hasNextPage": more, "endCursor": next_cursor}}}}
            except (ValueError, TypeError, KeyError):
                status, payload = 400, {"error": "invalid synthetic request"}
        with self._lock:
            self.requests.append({"phase": phase, "failure": failure, "protocol": protocol,
                                  "method": method, "path": parsed.path, "offset": offset, "status": status})
        return status, payload

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_fixture(self):
                raw_length = self.headers.get("Content-Length", "0")
                if not raw_length.isdigit() or int(raw_length) > 32768 or self.headers.get("Transfer-Encoding"):
                    status, payload = 400, {"error": "invalid synthetic request size"}
                else:
                    body = self.rfile.read(int(raw_length))
                    status, payload = fixture._response(self.command, self.path, self.headers, body)
                encoded = json.dumps(payload, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = handle_fixture
            do_POST = handle_fixture
            do_PUT = handle_fixture
            do_DELETE = handle_fixture

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _listed_connection(value, name):
    sources = value.get("sources") if isinstance(value, dict) else None
    _require(isinstance(sources, list), "Source list must return its sources envelope.")
    matching = [item for item in sources if isinstance(item, dict) and item.get("name") == name]
    _require(len(matching) == 1, "Source list must identify one configured connection.")
    return matching[0]


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def verify(output):
    versions = prerequisites()  # Must precede all fixture/filesystem creation.
    output = Path(output).expanduser().absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    launcher = Path(__file__).with_name("api_fixture_worker.py").resolve()
    environment = {**os.environ, "CONTEXTBASE_VERIFY_REST_TOKEN": SYNTHETIC_TOKEN,
                   "CONTEXTBASE_VERIFY_GRAPHQL_TOKEN": SYNTHETIC_TOKEN}
    report = {"spec": "contextbase.native-connector-verification/v1", "result": "not_verified",
              "fixture": "synthetic_loopback_rest_graphql", "runtime_versions": versions,
              "scope": "Actual dlt against authored HTTP fixtures; no vendor compatibility claim",
              "actual_dlt_executed": False, "live_vendor_calls": 0, "model_calls": 0, "commands": [],
              "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    fixture = FixtureServer()
    try:
        with fixture:
            def run(*arguments, failure=False):
                completed = subprocess.run([sys.executable, str(launcher), "--fixture-origin", fixture.origin, "--",
                                            "--workspace", str(workspace), *arguments],
                                           env=environment, capture_output=True, text=True, timeout=180, check=False)
                value = json.loads(completed.stderr if completed.returncode else completed.stdout)
                report["commands"].append({"arguments": list(arguments), "exit_code": completed.returncode, "result": value})
                _require(bool(completed.returncode) == failure, "Unexpected command outcome: " + " ".join(arguments[:2]))
                if arguments[:2] == ("source", "sync") and value.get("status") == "synced":
                    report["actual_dlt_executed"] = True
                if failure:
                    _require("previous data was retained" in value.get("error", ""), "Failure must confirm snapshot retention.")
                return value

            def check_rows(contacts, issues):
                oracle = expected_rows(contacts, issues)
                contact_rows = run("query", "SELECT id, custom_attributes__account_status AS status, "
                                   "custom_attributes__seats AS seats FROM crm.contacts ORDER BY id")["rows"]
                issue_rows = run("query", "SELECT id, description AS account_id, priority, state__type AS state "
                                 "FROM tracker.issues ORDER BY id")["rows"]
                _require(contact_rows == oracle["contacts"] and issue_rows == oracle["issues"],
                         "Extracted rows differ from the independent fixture oracle.")

            for source, name, field, env, resource in (
                ("intercom", "crm", "token", "CONTEXTBASE_VERIFY_REST_TOKEN", "contacts"),
                ("linear", "tracker", "api_key", "CONTEXTBASE_VERIFY_GRAPHQL_TOKEN", "issues"),
            ):
                run("source", "add", source, "--name", name, "--credential-env", field + "=" + env, "--resource", resource)
            initial = [run("source", "sync", name) for name in ("crm", "tracker")]
            report["actual_dlt_executed"] = True
            _require(all(item["status"] == "synced" and item.get("context", {}).get("status") == "draft" for item in initial),
                     "Extraction must publish data and bootstrap draft context.")
            draft = json.loads((workspace / "context/draft.json").read_text())
            _require({item["table"] for item in draft["schema"]} >= {"contacts", "issues"}, "Draft context missed selected tables.")
            check_rows(CONTACTS_INITIAL, ISSUES_INITIAL)
            initial_value = expected(CONTACTS_INITIAL, ISSUES_INITIAL)
            _require(run("query", SQL)["rows"] == [{"active_seats_at_risk": initial_value}], "Initial cross-source query differs from oracle.")
            sql_file = output / "active-seats-at-risk.sql"
            sql_file.write_text(SQL + "\n")
            proposal = run("metric", "propose", "--name", "active_seats_at_risk", "--sql-file", str(sql_file),
                           "--description", "Synthetic active seats with an open priority-one issue", "--grain", "Synthetic account",
                           "--unit", "seats", "--rule", "Active account with at least one started priority-one issue",
                           "--owner", "synthetic_owner", "--synthetic-example")
            run("metric", "approve", "active_seats_at_risk", "--approve", proposal["approval_sha256"], "--reviewer", "synthetic_reviewer")
            approved = workspace / "context/approved.json"
            approved_hash = hashlib.sha256(approved.read_bytes()).hexdigest()
            fixture.set_state("refreshed")
            refreshed = [run("source", "sync", name) for name in ("crm", "tracker")]
            _require(all(item["status"] == "synced" and item.get("context", {}).get("status") == "draft" for item in refreshed),
                     "Refresh must publish data and bootstrap draft context.")
            _require(all(new["snapshot_sha256"] != old["snapshot_sha256"] for new, old in zip(refreshed, initial)),
                     "Refresh must replace each source snapshot.")
            check_rows(CONTACTS_REFRESHED, ISSUES_REFRESHED)
            refreshed_value = expected(CONTACTS_REFRESHED, ISSUES_REFRESHED)
            _require(run("metric", "run", "active_seats_at_risk")["rows"] == [{"active_seats_at_risk": refreshed_value}],
                     "Refreshed metric differs from oracle.")
            for protocol, name in (("rest", "crm"), ("graphql", "tracker")):
                fixture.set_state("refreshed", failure=protocol)
                run("source", "sync", name, failure=True)
                check_rows(CONTACTS_REFRESHED, ISSUES_REFRESHED)
                _require(run("metric", "run", "active_seats_at_risk")["rows"] == [{"active_seats_at_risk": refreshed_value}],
                         "A failed page changed the approved metric result.")
                connection = _listed_connection(run("source", "list"), name)
                previous = refreshed[0 if name == "crm" else 1]
                _require(connection["last_sync"]["status"] == "failed"
                         and connection["last_snapshot"]["snapshot_sha256"] == previous["snapshot_sha256"],
                         "Failed extraction replaced the previous committed snapshot receipt.")
            _require(hashlib.sha256(approved.read_bytes()).hexdigest() == approved_hash, "Refresh changed approved context.")
            for protocol in ("rest", "graphql"):
                for phase in ("initial", "refreshed"):
                    pages = [item for item in fixture.requests if item["protocol"] == protocol and item["phase"] == phase
                             and item["failure"] is None and item["status"] == 200]
                    _require(len(pages) >= 2 and any(item["offset"] == 2 for item in pages), "Pagination was not exercised.")
                failed = [item for item in fixture.requests if item["protocol"] == protocol and item["failure"] == protocol]
                _require(any(item["offset"] == 0 and item["status"] == 200 for item in failed)
                         and any(item["offset"] == 2 and item["status"] == 503 for item in failed), "Partial failure was not exercised.")
            guards = [json.loads(path.read_text()) for path in workspace.glob("connectors/*/attempt-*/result.json.network.json")]
            _require(len(guards) == 6 and all(item["fixture_origin"] == fixture.origin and item["blocked_network_attempts"] == 0
                                             and item["allowed_connects"] >= 2 for item in guards),
                     "Every extraction needs complete loopback-only network evidence.")
            report.update(result="passed", initial_expected=initial_value, refreshed_expected=refreshed_value,
                          approved_context_sha256=approved_hash, network_guards=guards, failure_retention_verified=True)
    finally:
        report["fixture_requests"] = fixture.requests
        _write(output / "receipt.json", report)
    return {"result": "passed", "receipt": str(output / "receipt.json"), "initial": initial_value, "refreshed": refreshed_value}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="A new private directory for fixture evidence")
    arguments = parser.parse_args()
    try:
        print(json.dumps(verify(arguments.output), indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"result": "not_verified", "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
