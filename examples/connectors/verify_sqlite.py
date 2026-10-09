"""Actual dlt → DuckDB → context → query verification on synthetic local SQLite.

Run explicitly after the optional runtime is approved and installed. This script
fails when prerequisites are missing; it does not substitute an import or fake
extractor, install packages, contact a vendor or invoke a model.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


INITIAL = [("account_001", "active", 3), ("account_002", "active", 7), ("account_003", "inactive", 50)]
REFRESHED = [("account_001", "active", 4), ("account_002", "inactive", 7), ("account_003", "inactive", 50)]


def expected(rows):
    return sum(seats for _, status, seats in rows if status == "active")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def prerequisites():
    for name, required in (("dlt", "1.24.0"), ("PyYAML", "6.0.3"), ("duckdb", "1.4.5"), ("SQLAlchemy", None)):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ValueError("Install the explicitly approved connector runtime before this verification.") from None
        if required and installed != required:
            raise ValueError("This verification requires the reviewed connector runtime versions.")
    if not (3, 10) <= sys.version_info[:2] < (3, 15):
        raise ValueError("The reviewed connector runtime requires Python 3.10 through 3.14.")


def verify(output):
    prerequisites()
    output = Path(output).expanduser().absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    source = output / "synthetic.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY, status TEXT, seats INTEGER)")
        db.executemany("INSERT INTO accounts VALUES (?,?,?)", INITIAL)
        db.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, account_id TEXT)")
        db.executemany("INSERT INTO events VALUES (?,?)", [("event_001", "account_001"), ("event_002", "account_002")])
    # This generated URL contains no password or real source data. It remains
    # in the child environment; saved configuration contains only its name.
    environment = {**os.environ, "CONTEXTBASE_VERIFY_SQLITE_URL": "sqlite:///" + str(source)}
    commands = []

    def run(*arguments):
        completed = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(workspace), *arguments],
                                   env=environment, capture_output=True, text=True, timeout=180, check=False)
        if completed.returncode:
            raise RuntimeError("Local connector verification command failed: " + " ".join(arguments[:2]))
        value = json.loads(completed.stdout)
        commands.append({"arguments": list(arguments), "result": value})
        return value

    run("source", "add", "sqlite", "--name", "crm", "--credential-env", "credentials=CONTEXTBASE_VERIFY_SQLITE_URL",
        "--resource", "accounts", "--resource", "events")
    initial_sync = run("source", "sync", "crm")
    require(initial_sync["status"] == "synced" and initial_sync["context"]["status"] == "draft",
            "Initial extraction must publish data and bootstrap draft context.")
    sql = "SELECT SUM(seats) AS active_seats FROM crm.accounts WHERE status = 'active'"
    initial_answer = run("query", sql)
    require(initial_answer["rows"] == [{"active_seats": expected(INITIAL)}], "Initial query differs from the fixture oracle.")
    draft = json.loads((workspace / "context/draft.json").read_text())
    require({item["table"] for item in draft["schema"]} >= {"accounts", "events"}, "Context draft missed selected tables.")

    sql_file = output / "active-seats.sql"
    sql_file.write_text(sql + "\n")
    proposal = run("metric", "propose", "--name", "active_seats", "--sql-file", str(sql_file),
                   "--description", "Synthetic active account seats", "--grain", "All active synthetic accounts",
                   "--unit", "seats", "--rule", "Include only status active", "--owner", "synthetic_owner", "--synthetic-example")
    run("metric", "approve", "active_seats", "--approve", proposal["approval_sha256"], "--reviewer", "synthetic_reviewer")
    approved_path = workspace / "context/approved.json"
    approved_hash = hashlib.sha256(approved_path.read_bytes()).hexdigest()
    require(run("metric", "run", "active_seats")["rows"] == initial_answer["rows"], "Approved metric differs from the initial query.")
    with sqlite3.connect(source) as db:
        for account_id, status, seats in REFRESHED:
            db.execute("UPDATE accounts SET status=?,seats=? WHERE account_id=?", (status, seats, account_id))
    refreshed_sync = run("source", "sync", "crm")
    refreshed_answer = run("metric", "run", "active_seats")
    require(refreshed_sync["status"] == "synced" and refreshed_sync["context"]["status"] == "draft",
            "Refresh must publish data and bootstrap draft context.")
    require(refreshed_answer["rows"] == [{"active_seats": expected(REFRESHED)}], "Refreshed metric differs from the fixture oracle.")
    require(hashlib.sha256(approved_path.read_bytes()).hexdigest() == approved_hash, "Refresh changed approved context.")
    require(refreshed_sync["snapshot_sha256"] != initial_sync["snapshot_sha256"], "Refresh did not replace the snapshot.")
    report = {"spec": "contextbase.native-connector-verification/v1", "result": "passed", "actual_dlt_executed": True,
              "fixture": "synthetic_local_sqlite", "initial_expected": expected(INITIAL),
              "refreshed_expected": expected(REFRESHED), "approved_context_sha256": approved_hash,
              "commands": commands, "live_vendor_calls": 0, "model_calls": 0}
    receipt = output / "receipt.json"
    receipt.write_text(json.dumps(report, indent=2) + "\n")
    return {"result": "passed", "receipt": str(receipt), "initial": expected(INITIAL), "refreshed": expected(REFRESHED)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="A new directory for the synthetic fixture and evidence")
    arguments = parser.parse_args()
    try:
        print(json.dumps(verify(arguments.output), indent=2))
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"result": "not_verified", "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
