#!/usr/bin/env python3
"""Run staged synthetic Customer360 workflows from an installed wheel only.

Invoked by check_customer360_install.py with Python -I from outside the checkout.
Expected values below are fixed, independent acceptance facts, never packaged
with the product. No provider is configured or called by this workflow.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def write_private(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)


def run_installed(output, package_manifest, excluded_checkout):
    os.umask(0o077)
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    expected = json.loads(Path(package_manifest).read_text())
    checkout = Path(excluded_checkout).resolve()
    environment = Path(sys.prefix).resolve()
    assert sys.flags.isolated == 1, "Installed verification must run under Python -I."
    assert Path.cwd().resolve() != checkout and checkout not in Path.cwd().resolve().parents
    assert all(str(checkout) not in path for path in sys.path), "Checkout appeared on sys.path."
    modules = {}
    for name in ("contextbase", "duckdb", "dlt", "yaml"):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert environment in path.parents, "An import escaped the fresh installed environment."
        modules[name] = str(path.relative_to(environment))
    import contextbase
    package = Path(contextbase.__file__).resolve().parent
    for relative, value in expected.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink(), "Missing installed package file: " + relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == value, "Installed file mismatch: " + relative
    assets = sorted(str(path.relative_to(package)) for path in (package / "customer360" / "web").iterdir() if path.is_file())
    assert assets == ["customer360/web/app.js", "customer360/web/index.html", "customer360/web/style.css"]
    samples = sorted((package / "customer360" / "sample_data").rglob("*.json"))
    assert len(samples) == 25
    assert not any("oracle" in path.name.lower() or "expected" in path.name.lower() for path in package.rglob("*") if path.is_file())
    direct = metadata.distribution("oriel").read_text("direct_url.json")
    if direct:
        assert not json.loads(direct).get("dir_info", {}).get("editable", False)
    versions = {name: metadata.version(name) for name in ("oriel", "duckdb", "dlt", "PyYAML", "SQLAlchemy")}
    assert versions["duckdb"] == "1.4.5" and versions["dlt"] == "1.24.0" and versions["PyYAML"] == "6.0.3"

    from contextbase.customer360.application import Application
    from contextbase.workspace import Workspace

    outcomes = []
    for profile in ("billing-support", "salesforce-gong"):
        workspace = Workspace(output / (profile + "-workspace"))
        model_calls = []
        def forbidden_model():
            model_calls.append(True)
            raise AssertionError("Clean installation verification cannot call a model.")
        app = Application(workspace, model_client_factory=forbidden_model)
        jobs = []
        def job(operation, arguments):
            started = time.monotonic()
            submitted = app.submit(operation, arguments)
            deadline = started + 90
            while time.monotonic() < deadline:
                receipt = app.job(submitted["job_id"])
                if receipt["status"] in {"succeeded", "failed"}:
                    jobs.append({"operation": operation, "job_id": receipt["job_id"],
                                 "status": receipt["status"], "receipt_sha256": digest(receipt)})
                    if receipt["status"] != "succeeded":
                        raise AssertionError("Installed operation failed: " + operation + " / " + receipt.get("error", {}).get("code", "unknown"))
                    return receipt["result"]
                time.sleep(0.01)
            raise AssertionError("Installed operation timed out: " + operation)
        phases = []
        try:
            initial_context = initial_customers = saved = initial_mapping = None
            for phase in ("initial", "refreshed"):
                extraction = job("sample.connect", {"profile": profile, "phase": phase})
                assert extraction["actual_dlt_executed"] is True
                assert extraction["vendor_adapter_verified"] is False and extraction["model_calls"] == 0
                plan = job("sample.prepare", {"profile": profile})
                assert plan["approvals_performed"] is False and plan["model_calls"] == 0
                conflict_count = 0
                for proposal in plan["links"]:
                    if proposal["status"] == "conflict":
                        conflict_count += 1
                        continue
                    job("links.approve", {"proposal_id": proposal["proposal_id"], "approval_sha256": proposal["approval_sha256"], "reviewer": "synthetic-clean-install-review"})
                if phase == "initial":
                    initial_customers = plan["customer_ids"]
                    initial_mapping = digest(plan["mapping"])
                    proposed = job("profile.propose", {"profile": plan["mapping"], "snapshot_id": plan["snapshot_id"]})
                    job("profile.approve", {"proposal_id": proposed["proposal_id"], "approval_sha256": proposed["approval_sha256"], "reviewer": "synthetic-clean-install-review"})
                else:
                    assert plan["customer_ids"] == initial_customers
                    assert digest(plan["mapping"]) == initial_mapping, "Sample refresh unexpectedly changed reviewed field mapping."
                scope = {"profile_name": plan["profile"], "snapshot_id": plan["snapshot_id"]}
                materialized = job("materialize", scope)
                assert materialized["snapshot_id"] == scope["snapshot_id"]
                if phase == "initial":
                    candidate = job("context.propose", {**scope, "definitions": plan["definitions"], "gaps": plan["gaps"]})
                    context = job("context.approve", {"proposal_id": candidate["proposal_id"], "approval_sha256": candidate["approval_sha256"], "reviewer": "synthetic-clean-install-review"})
                    initial_context = context
                else:
                    context = job("context.show", scope)
                    assert context["context_id"] == initial_context["context_id"]
                    assert context["definition_sha256"] == initial_context["definition_sha256"]
                customer_scope = {**scope, "customer_id": plan["customer_ids"]["customer_a"]}
                brief = job("brief", {**customer_scope, "fields": ["text", "updates"], "quote_limit": 5})
                assert brief["snapshot_id"] == plan["snapshot_id"] and brief["context_id"] == context["context_id"]
                numeric = {metric["result"]["definition_name"]: {value["currency"]: Decimal(str(value["value"])) for value in metric["result"]["values"]} for metric in brief["metrics"]}
                if profile == "billing-support":
                    assert numeric["outstanding_invoices"] == ({"USD": Decimal(125000), "EUR": Decimal(30000)} if phase == "initial" else {"USD": Decimal(10000), "EUR": Decimal(30000)})
                    assert numeric["open_support_cases"] == {None: Decimal(1 if phase == "initial" else 0)}
                else:
                    assert numeric["open_pipeline"] == ({"USD": Decimal("12500.00"), "EUR": Decimal("3000.00")} if phase == "initial" else {"USD": Decimal("9000.00"), "EUR": Decimal("3000.00")})
                assert brief["quotes"], "Installed brief must expose actual source evidence."
                quote = brief["quotes"][0]
                evidence = job("evidence", {**customer_scope, **{key: quote[key] for key in ("evidence_id", "field", "start", "end")}})
                assert evidence["quote_id"] == quote["quote_id"] and evidence["text"] == quote["text"]
                if phase == "initial":
                    saved = job("review.save", {**customer_scope, "fields": ["text", "updates"], "quote_limit": 5})
                    assert saved["status"] == "retained_review_not_current_answer"
                else:
                    comparison = job("review.compare", {"review_id": saved["review_id"], "current_snapshot_id": plan["snapshot_id"]})
                    assert comparison["previous_snapshot_id"] != plan["snapshot_id"]
                    assert comparison["current_brief"]["snapshot_id"] == plan["snapshot_id"]
                    assert comparison["metric_changes"] and comparison["record_changes"]["changed"]
                    assert "identity_sha256" in comparison["changed_basis"]
                    assert all(change["delta"] is None for change in comparison["metric_changes"])
                phases.append({"phase": phase, "status": "passed", "source_count": len(extraction["source_names"]),
                               "snapshot_id": plan["snapshot_id"], "context_id": context["context_id"],
                               "definition_sha256": context["definition_sha256"], "answer_sha256": digest(brief),
                               "quote_count": len(brief["quotes"]), "conflicting_proposals_excluded": conflict_count})
            assert not model_calls
            outcomes.append({"profile": profile, "status": "passed", "phases": phases, "jobs": jobs,
                             "model_calls": 0, "vendor_calls": 0})
        finally:
            app.close()
    result = {"spec": "oriel.customer360.installed-workflow/v1", "status": "passed",
              "synthetic": True, "actual_dlt_sqlite": True, "model_calls": 0, "vendor_calls": 0,
              "isolated_python": True, "checkout_on_sys_path": False, "editable": False,
              "module_paths_relative_to_venv": modules, "versions": versions,
              "package_file_count": len(expected), "ui_assets": assets, "sample_json_count": len(samples),
              "oracle_packaged": False, "profiles": outcomes,
              "limitations": ["Application jobs exercised directly; browser DOM acceptance is a separate check.",
                              "Synthetic local SQLite extraction does not verify vendor authentication or actual model success."]}
    write_private(output / "receipt.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--package-manifest", required=True)
    parser.add_argument("--excluded-checkout", required=True)
    args = parser.parse_args()
    result = run_installed(args.output, args.package_manifest, args.excluded_checkout)
    print(json.dumps({"status": result["status"], "profiles": len(result["profiles"]), "model_calls": 0, "vendor_calls": 0}))


if __name__ == "__main__":
    main()
