"""Run the synthetic generic workflow through the public CLI and a new consumer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from oracle import expected, mapping_issues, read_fixture


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIXTURES = HERE / "fixtures"
METRIC = "unused_seats_at_low_activation"
RULES = [
    "Synthetic complete-period fixture; only active external customer contracts with positive licensed seats.",
    "Identity requires exactly one enabled workspace mapping and an existing unique CRM organization; unresolved mappings block the answer.",
    "Activation is distinct non-null member_ref per organization publishing a report in [2026-09-24, 2026-10-08) UTC, across all mapped workspaces.",
    "The export covers the full period; no qualifying event means zero activators. Missing or incomplete exports require a separate completeness review.",
    "Select organizations with strictly fewer than half of licensed seats activated; sum licensed seats minus activators once per selected organization.",
]


def verify(output: Path):
    if output.is_symlink() or (output.exists() and any(output.iterdir())):
        raise ValueError("Verification output must be a new or empty directory.")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    commands = []

    def cli(*arguments, fails=False):
        args = [sys.executable, "-m", "contextbase", "--workspace", str(workspace), *map(str, arguments)]
        process = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, check=False)
        entry = {"argv": [arg.replace(str(output), "$OUTPUT").replace(str(ROOT) + "/", "") for arg in args[1:]],
                 "expected_failure": fails, "exit_code": process.returncode}
        commands.append(entry)
        if fails:
            if process.returncode == 0:
                raise AssertionError("Expected CLI rejection: " + repr(entry["argv"]))
            entry["error"] = json.loads(process.stderr)["error"]
            return entry
        if process.returncode != 0:
            raise AssertionError("CLI failed: " + repr(entry["argv"]) + " " + process.stderr)
        return json.loads(process.stdout)

    def ingest(file, schema, table, replace=False):
        return cli("ingest", FIXTURES / file, "--schema", schema, "--table", table, *(["--replace"] if replace else []))

    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(FIXTURES.iterdir())}
    imported = [ingest("crm-seats.csv", "sales", "seats"),
                ingest("workspace-map-unresolved.csv", "directory", "workspace_map"),
                ingest("product-events-initial.jsonl", "telemetry", "events")]
    draft = cli("context", "bootstrap")
    assert draft["status"] == "draft" and draft["metrics"] == []
    assert not any(join["from"] == "directory.workspace_map" and join["to"] == "sales.seats"
                   for join in draft["candidate_joins"]), "Bootstrap must not invent a different-name join."
    issues = cli("query", (HERE / "sql/mapping-issues.sql").read_text())["rows"]
    assert issues == mapping_issues(*read_fixture(mapping="workspace-map-unresolved.csv"))
    blocked = cli("query", (HERE / "sql/unused-seats.sql").read_text(), fails=True)

    changes = []
    for mapping_id, enabled in (("map_10", False), ("map_08", True)):
        plan = cli("changes", "plan", "--table", "directory.workspace_map", "--key", "mapping_id", "--value", mapping_id,
                   "--set", json.dumps({"enabled": enabled}))
        assert plan["scope"] == "local_only" and plan["before"]["enabled"] != plan["after"]["enabled"]
        cli("changes", "apply", plan["plan_id"], "--approve", "0" * 64, fails=True)
        applied = cli("changes", "apply", plan["plan_id"], "--approve", plan["approval_sha256"])
        cli("changes", "apply", plan["plan_id"], "--approve", plan["approval_sha256"], fails=True)
        changes.append({"mapping_id": mapping_id, "before": plan["before"], "after": plan["after"],
                        "scope": applied["scope"], "approval_sha256": plan["approval_sha256"]})
    assert cli("query", (HERE / "sql/mapping-issues.sql").read_text())["rows"] == []
    account_sql = (HERE / "sql/activation-accounts.sql").read_text()
    first = cli("query", account_sql)["rows"]
    oracle_initial = expected(*read_fixture())
    assert first == oracle_initial["accounts"]
    transform = cli("transform", "activation_initial", "--sql", account_sql)
    assert transform["scope"] == "local_only"
    candidate = cli("metric", "propose", "--name", METRIC, "--sql-file", HERE / "sql/unused-seats.sql",
                    "--description", "Unused licensed seats at active external organizations below 50% report activation in the synthetic period.",
                    "--grain", "One CRM organization, aggregated across mapped product workspaces.", "--unit", "seats",
                    "--owner", "synthetic_activation_owner", "--synthetic-example",
                    *[argument for rule in RULES for argument in ("--rule", rule)])
    assert candidate["status"] == "candidate" and candidate["synthetic"] is True
    cli("metric", "run", METRIC, fails=True)
    cli("metric", "approve", METRIC, "--approve", "0" * 64, "--reviewer", "synthetic_example_reviewer", fails=True)
    approved = cli("metric", "approve", METRIC, "--approve", candidate["approval_sha256"], "--reviewer", "synthetic_example_reviewer")
    initial = cli("metric", "run", METRIC)
    assert initial["rows"] == [{METRIC: oracle_initial[METRIC]}]
    approved_hash = hashlib.sha256((workspace / "context/approved.json").read_bytes()).hexdigest()
    manifest = cli("bundle", "export", "--output", output / "bundle", "--workspace-id", "synthetic-tool-exports")
    assert manifest["synthetic"] is True and manifest["contains_source_rows"] is False
    envelope = json.loads((output / "bundle" / "metrics" / (METRIC + ".json")).read_text())
    assert envelope["definition"]["status"] == "candidate" and envelope["source_metadata"]["synthetic"] is True

    def consumer(name, events):
        args = [sys.executable, str(HERE / "reuse.py"), "--workspace", str(output / name), "--bundle", str(output / "bundle"),
                "--crm", str(FIXTURES / "crm-seats.csv"), "--mapping", str(FIXTURES / "workspace-map-reviewed.csv"),
                "--events", str(FIXTURES / events)]
        process = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, check=True)
        answer = json.loads(process.stdout)
        assert answer["has_local_approved_context"] is False
        assert answer["context_version"] == initial["context_version"]
        return answer

    reuse_initial = consumer("second-consumer-initial", "product-events-initial.jsonl")
    assert reuse_initial["rows"] == initial["rows"]
    unchanged_import = ingest("product-events-initial.jsonl", "telemetry", "events", replace=True)
    unchanged = cli("metric", "run", METRIC)
    assert unchanged["rows"] == initial["rows"]
    refreshed_import = ingest("product-events-refreshed.jsonl", "telemetry", "events", replace=True)
    refreshed = cli("metric", "run", METRIC)
    oracle_refreshed = expected(*read_fixture("product-events-refreshed.jsonl"))
    assert refreshed["rows"] == [{METRIC: oracle_refreshed[METRIC]}]
    assert cli("query", account_sql)["rows"] == oracle_refreshed["accounts"]
    assert initial["context_version"] == unchanged["context_version"] == refreshed["context_version"]
    assert hashlib.sha256((workspace / "context/approved.json").read_bytes()).hexdigest() == approved_hash
    frozen_transform = cli("query", "SELECT * FROM models.activation_initial ORDER BY organization_id")["rows"]
    assert frozen_transform == first, "Materialized transforms are snapshots, not automatically refreshed views."
    reuse_refreshed = consumer("second-consumer-refreshed", "product-events-refreshed.jsonl")
    assert reuse_refreshed["rows"] == refreshed["rows"]
    assert hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(FIXTURES.iterdir())}
    receipt = {
        "spec_version": "contextbase.tool-export-proof/v1", "synthetic": True,
        "observed_at": datetime.now(timezone.utc).isoformat(), "verification": "passed",
        "model_calls": 0, "network_calls": 0, "rystic_commands": 0,
        "scope": "Functional CLI/import/context portability check; no performance or adoption evidence.",
        "fixture_sha256": hashes, "tables": [{key: item[key] for key in ("table", "rows", "sha256")} for item in imported],
        "unresolved_mapping": issues, "unresolved_metric_rejected": blocked["exit_code"] != 0,
        "reviewed_local_changes": changes, "source_files_unchanged": True,
        "bootstrap": {"status": draft["status"], "business_metrics_inferred": len(draft["metrics"]),
                      "different_name_identity_mapping_inferred": False},
        "initial": oracle_initial, "refreshed": oracle_refreshed,
        "context": {"version": initial["context_version"], "metric_version": approved["version"],
                    "approval_kind": approved["approval_kind"], "approved_context_unchanged_after_refresh": True,
                    "exported_registry_status": envelope["definition"]["status"]},
        "second_consumer_initial": reuse_initial, "second_consumer_refreshed": reuse_refreshed,
        "refresh": {"unchanged_export_sha256": unchanged_import["sha256"], "changed_export_sha256": refreshed_import["sha256"],
                    "unchanged_answer_verified": True, "changed_answer_verified": True,
                    "prior_materialized_transform_stays_at_initial_value": True,
                    "mode": "Explicit replacement of the selected local table; no automatic source polling or multi-table atomic refresh."},
        "commands": commands,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = verify(args.output)
    print(json.dumps({"verification": receipt["verification"], "synthetic": True,
                      "initial": receipt["initial"], "refreshed": receipt["refreshed"],
                      "receipt": str(args.output.resolve() / "receipt.json")}, indent=2))
