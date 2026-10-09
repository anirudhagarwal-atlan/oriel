#!/usr/bin/env python3
"""Execute declared Customer360 acceptance tests; never infer completion from exit 0."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("verification/customer360/acceptance.json")
OUTPUT = Path(".oriel/verification/customer360/status.json")
MODES = {"local", "model", "live"}
MAX_JSON = 1024 * 1024
CHARTER_INVENTORY_VERSION = 1
# Independent of the selectable manifest. A partial or rewritten manifest may
# run useful checks, but cannot redefine completion for customer360-v1. Changes
# to this minimum inventory require an explicit charter scope revision/review.
CHARTER_GATES = (
    ("source_pipeline", "local", (
        "test_real_connector_runtime_extracts_both_profiles",
        "test_pagination_nested_records_and_resource_bounds",
        "test_partial_extraction_is_not_published",
        "test_context_only_reads_committed_duckdb_snapshot",
        "test_answer_path_works_with_vendor_network_unavailable",
    )),
    ("customer_identity", "local", (
        "test_canonical_identity_is_not_a_vendor_primary_key",
        "test_explicit_links_and_reviewed_mapping_resolve",
        "test_duplicate_names_and_shared_domains_do_not_auto_merge",
        "test_missing_and_conflicting_links_remain_unresolved",
        "test_one_to_many_evidence_does_not_multiply_value",
        "test_changed_source_identifier_preserves_reviewed_customer_identity",
    )),
    ("context_lifecycle", "local", (
        "test_context_stages_derive_profile_appropriate_candidates",
        "test_unreviewed_definitions_cannot_become_authoritative",
        "test_conflicting_evidence_and_unknown_meaning_are_retained",
        "test_approved_context_reused_in_fresh_process",
        "test_definition_revision_invalidates_dependents",
        "test_exported_pack_excludes_customer_rows_and_text",
    )),
    ("grounded_answers", "local", (
        "test_numeric_results_match_independent_oracle",
        "test_customer_sets_match_independent_oracle",
        "test_factual_claims_resolve_to_supporting_evidence",
        "test_fabricated_citation_and_unsupported_claim_are_rejected",
        "test_missing_capability_yields_gap_or_abstention",
        "test_answer_receipt_pins_snapshot_context_queries_and_evidence",
        "test_latest_passage_matches_reviewed_source_time_across_profiles",
    )),
    ("refresh_and_history", "local", (
        "test_changed_event_updates_brief_and_preserves_definition",
        "test_repeated_refresh_does_not_duplicate_evidence",
        "test_failed_refresh_preserves_snapshot_and_exposes_staleness",
        "test_current_question_refused_when_required_coverage_is_stale",
        "test_schema_drift_invalidates_affected_context",
        "test_deleted_or_retracted_evidence_updates_current_answer",
        "test_changed_facts_compare_against_retained_review",
    )),
    ("source_portability", "local", (
        "test_materially_different_profiles_share_core_contracts",
        "test_no_crm_or_transcripts_required_for_second_profile",
        "test_single_source_then_enrichment_reports_coverage",
        "test_source_replacement_preserves_customer_identity_and_context",
        "test_core_answer_logic_has_no_required_vendor_branch",
    )),
    ("access_and_actions", "local", (
        "test_cross_workspace_evidence_access_is_rejected",
        "test_source_prompt_injection_cannot_change_tool_authority",
        "test_answering_cannot_write_to_vendor_or_local_source_tables",
        "test_model_data_consent_and_retrieval_bounds_are_enforced",
        "test_unauthorized_or_removed_evidence_is_unavailable",
    )),
    ("product_onboarding", "local", (
        "test_clean_checkout_documented_sample_workflow",
        "test_interface_connect_review_ask_evidence_refresh",
        "test_both_profiles_use_same_customer_interface",
        "test_setup_failure_and_missing_capability_are_visible",
    )),
    ("configured_model_smoke", "model", (
        "test_real_model_bootstraps_and_answers_crm_conversation_profile",
        "test_real_model_bootstraps_and_answers_non_crm_profile",
        "test_real_model_refresh_answer_matches_changed_evidence",
        "test_real_usage_and_all_stage_costs_respect_declared_budget",
    )),
    ("authorized_live_connectors", "live", (
        "test_authorized_initial_source_combination_reconciles_to_vendor",
        "test_additional_connector_works_through_existing_contract",
        "test_each_advertised_live_integration_has_current_scoped_receipt",
        "test_permissions_exclusions_and_freshness_are_recorded",
    )),
)


class ManifestError(ValueError):
    pass


def _read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    with path.open("rb") as handle:
        raw = handle.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("JSON exceeds limit")
    return json.loads(raw, object_pairs_hook=unique), raw


def _manifest(path, root):
    try:
        value, raw = _read_json(path)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "goal_id", "gates"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["goal_id"] != "customer360-v1"
                or not isinstance(value["gates"], list) or not 1 <= len(value["gates"]) <= 64):
            raise ValueError()
        ids, seen_cases = set(), set()
        for gate in value["gates"]:
            if (not isinstance(gate, dict)
                    or set(gate) != {"id", "mode", "suite", "required_cases", "description"}
                    or not isinstance(gate["id"], str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", gate["id"])
                    or gate["id"] in ids or gate["mode"] not in MODES
                    or not isinstance(gate["suite"], str)
                    or not re.fullmatch(r"tests/customer360/test_[A-Za-z0-9_]+\.py", gate["suite"])
                    or not (root / gate["suite"]).resolve().is_relative_to(root)
                    or not isinstance(gate["description"], str) or not gate["description"].strip()
                    or len(gate["description"]) > 4000):
                raise ValueError()
            cases = gate["required_cases"]
            if (not isinstance(cases, list) or not 1 <= len(cases) <= 256
                    or any(not isinstance(case, str) or not re.fullmatch(r"test_[A-Za-z0-9_]{1,195}", case) for case in cases)
                    or len(set(cases)) != len(cases) or seen_cases.intersection(cases)):
                raise ValueError()
            ids.add(gate["id"])
            seen_cases.update(cases)
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ManifestError("Invalid Customer360 acceptance manifest.") from None


def _contract_issues(contract):
    declared = {gate["id"]: gate for gate in contract["gates"]}
    protected_suites = {f"tests/customer360/test_{name}.py": name for name, _, _ in CHARTER_GATES}
    issues = []
    for name, mode, cases in CHARTER_GATES:
        gate = declared.get(name)
        if gate is None:
            issues.append({"gate_id": name, "reason": "missing_gate"})
            continue
        if gate["mode"] != mode:
            issues.append({"gate_id": name, "reason": "mode_mismatch"})
        if gate["suite"] != f"tests/customer360/test_{name}.py":
            issues.append({"gate_id": name, "reason": "suite_mismatch"})
        missing = sorted(set(cases) - set(gate["required_cases"]))
        if missing:
            issues.append({"gate_id": name, "reason": "missing_required_cases", "required_cases": missing})
    for gate in contract["gates"]:
        owner = protected_suites.get(gate["suite"])
        if owner is not None and owner != gate["id"]:
            issues.append({"gate_id": gate["id"], "reason": "reserved_suite"})
    return issues


def _fingerprint(root, manifest, gates):
    paths = {manifest, Path(__file__).resolve(), root / "pyproject.toml", root / "uv.lock",
             root / "README.md", root / "docs/customer-360-app.md"}
    # Only repository code/config and the declared synthetic Customer360 input roots.
    # Never discover inputs from private workspaces or credential-store locations.
    private = {"private", "workspace", "workspaces", "credentials", "secrets", "__pycache__"}
    def eligible(name, *, implementation_python=False):
        lowered = name.lower()
        if lowered.startswith(".") or lowered in private or lowered.endswith((".pem", ".key", ".p12", ".pfx")):
            return False
        # These are implementation modules under explicit repository code roots,
        # e.g. a public SDK credentials.py, not credential data/store discovery.
        if implementation_python and Path(name).suffix.lower() == ".py":
            return True
        return not re.search(r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?)(?:[._-]|$)", lowered)

    data_suffixes = {".py", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".sql", ".toml", ".txt", ".md", ".parquet", ".arrow"}
    for relative, suffixes, implementation_python in (
        ("contextbase", {".py", ".json", ".yaml", ".yml", ".html", ".js", ".css"}, True),
        ("scripts", {".py", ".mjs"}, True),
        ("tests", {".py"}, True),
        ("tests/customer360", data_suffixes, True),
        ("examples", {".py"}, True),
        ("examples/connectors", data_suffixes, True),
        ("examples/customer-360", data_suffixes, True),
        ("verification/customer360", data_suffixes, False),
    ):
        directory = root / relative
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if eligible(name) and not (Path(current) / name).is_symlink()]
            paths.update(Path(current) / name for name in files
                         if eligible(name, implementation_python=implementation_python)
                         and Path(name).suffix.lower() in suffixes)
    paths.update(root / gate["suite"] for gate in gates)
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        name = str(path.relative_to(root)) if path.is_relative_to(root) else "verifier.py"
        digest.update(name.encode() + b"\0")
        if path.is_file() and not path.is_symlink() and (path == Path(__file__).resolve() or path.resolve().is_relative_to(root)):
            content = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    content.update(chunk)
            digest.update(content.digest())
        else:
            digest.update(b"missing-or-unavailable")
    return {"sha256": digest.hexdigest(), "file_count": len(paths)}


def _git(root):
    result = {"head": None, "dirty": None}
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, timeout=5)
        if head.returncode == 0 and re.fullmatch(rb"[a-f0-9]{40,64}\n?", head.stdout):
            result["head"] = head.stdout.decode().strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
                               capture_output=True, timeout=5)
        if dirty.returncode == 0:
            result["dirty"] = bool(dirty.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


class _Result(unittest.TestResult):
    def __init__(self, required):
        super().__init__()
        self.required = {name: [] for name in required}
        self.counts = Counter()
        self.entries = {}

    def startTest(self, test):
        super().startTest(test)
        entry = {"status": "incomplete"}
        self.entries[id(test)] = entry
        name = getattr(test, "_testMethodName", None)
        if name in self.required:
            self.required[name].append(entry)

    def _mark(self, test, status):
        self.counts[status] += 1
        if id(test) in self.entries:
            self.entries[id(test)]["status"] = status

    def addSuccess(self, test):
        super().addSuccess(test)
        self._mark(test, "passed")

    def addFailure(self, test, err):
        self._mark(test, "failed")

    def addError(self, test, err):
        self._mark(test, "error")

    def addSkip(self, test, reason):
        self._mark(test, "skipped")

    def addExpectedFailure(self, test, err):
        self._mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        self._mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, "failed" if issubclass(err[0], test.failureException) else "error")


def _worker(root, suite, required, output):
    """Private subprocess protocol: no exception strings, test output or source rows."""
    result = _Result(required)
    error = None
    sys.path[:0] = [str(root), str(suite.parent)]
    try:
        spec = importlib.util.spec_from_file_location("_customer360_acceptance", suite)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loader = unittest.TestLoader()
        tests = loader.loadTestsFromModule(module)
        if loader.errors:
            error = "suite_import_error"
        else:
            tests.run(result)
    except BaseException:
        error = "suite_import_or_execution_error"
    outcomes = {name: [entry["status"] for entry in entries] for name, entries in result.required.items()}
    passed = (not error and result.testsRun > 0
              and not any(count for status, count in result.counts.items() if status != "passed")
              and result.counts["passed"] == result.testsRun
              and all(statuses == ["passed"] for statuses in outcomes.values()))
    body = {"status": "passed" if passed else "failed", "tests_run": result.testsRun,
            "counts": dict(result.counts), "required_cases": outcomes, "error_code": error}
    with output.open("x", encoding="utf-8") as handle:
        json.dump(body, handle, allow_nan=False)
    output.chmod(0o600)


def _suite(root, gate, timeout):
    suite = root / gate["suite"]
    if not suite.is_file():
        return {"status": "unimplemented", "error_code": "suite_missing"}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="oriel-acceptance-") as directory:
        receipt = Path(directory) / "result.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker", str(root), str(suite),
                   json.dumps(gate["required_cases"]), str(receipt)]
        try:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
                return {"status": "failed", "error_code": "suite_timeout", "elapsed_seconds": round(time.monotonic() - started, 3)}
            if process.returncode != 0:
                raise ValueError()
            value, _ = _read_json(receipt)
            if (not isinstance(value, dict) or set(value) != {"status", "tests_run", "counts", "required_cases", "error_code"}
                    or value["status"] not in {"passed", "failed"}
                    or set(value["required_cases"]) != set(gate["required_cases"])):
                raise ValueError()
            value["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return value
        except (OSError, ValueError, TypeError, KeyError):
            return {"status": "failed", "error_code": "worker_incomplete", "elapsed_seconds": round(time.monotonic() - started, 3)}


def _save(path, value):
    if path.is_symlink():
        raise OSError("Output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".customer360-status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(root=ROOT, *, manifest=None, output=None, include=(), timeout=120):
    root = Path(root).resolve()
    manifest = Path(manifest) if manifest is not None else root / MANIFEST
    output = Path(output) if output is not None else root / OUTPUT
    if not set(include) <= {"model", "live"} or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ManifestError("Invalid verifier options.")
    if output.resolve() == manifest.resolve() or output.suffix != ".json":
        raise ManifestError("Invalid verification output path.")
    contract, manifest_hash = _manifest(manifest, root)
    contract_issues = _contract_issues(contract)
    refused_gates = {issue["gate_id"] for issue in contract_issues
                     if issue["reason"] in {"mode_mismatch", "suite_mismatch", "reserved_suite"}}
    fingerprint = _fingerprint(root, manifest, contract["gates"])
    report = {"schema_version": 1, "goal_id": contract["goal_id"], "kind": "customer360_acceptance_verification",
              "charter_inventory_version": CHARTER_INVENTORY_VERSION,
              "contract_complete": not contract_issues, "contract_issues": contract_issues,
              "started_at": datetime.now(timezone.utc).isoformat(), "enabled_modes": sorted({"local", *include}),
              "repository": _git(root), "manifest_sha256": manifest_hash, "input_fingerprint": fingerprint,
              "gates": [], "runner_tests_are_product_evidence": False}
    for gate in contract["gates"]:
        if gate["id"] in refused_gates:
            # Reclassifying a protected external suite as local must not cause
            # execution, even when the resulting report would be incomplete.
            outcome = {"status": "failed", "error_code": "charter_gate_mismatch"}
        else:
            outcome = (_suite(root, gate, timeout) if gate["mode"] in report["enabled_modes"]
                       else {"status": "not_run", "error_code": "mode_not_enabled"})
        report["gates"].append({"id": gate["id"], "mode": gate["mode"], "suite": gate["suite"], **outcome})
    report["inputs_unchanged"] = fingerprint == _fingerprint(root, manifest, contract["gates"])
    for mode, name in (("local", "implementation_verified"), ("model", "model_smoke_verified"), ("live", "live_vendor_verified")):
        selected = [gate for gate in report["gates"] if gate["mode"] == mode]
        report[name] = (report["contract_complete"] and report["inputs_unchanged"]
                        and bool(selected) and all(gate["status"] == "passed" for gate in selected))
    report["overall_passed"] = all(report[name] for name in (
        "implementation_verified", "model_smoke_verified", "live_vendor_verified"))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["retained_report"] = "runs/" + uuid.uuid4().hex + ".json"
    retained = output.parent / report["retained_report"]
    retained.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(retained, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _save(output, report)
    return report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--_worker":
        root, suite, required, output = argv[1:]
        _worker(Path(root), Path(suite), json.loads(required), Path(output))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / MANIFEST)
    parser.add_argument("--output", type=Path, default=ROOT / OUTPUT)
    parser.add_argument("--include", choices=["model", "live"], action="append", default=[])
    parser.add_argument("--timeout", type=float, default=120, help="Maximum seconds per suite (default: 120).")
    args = parser.parse_args(argv)
    try:
        report = verify(manifest=args.manifest, output=args.output, include=args.include, timeout=args.timeout)
    except ManifestError:
        print("Invalid Customer360 acceptance manifest or verifier options.", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Customer360 verification could not complete or save its receipt.", file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("contract_complete", "implementation_verified", "model_smoke_verified", "live_vendor_verified", "overall_passed")} | {
        "gates": [{"id": gate["id"], "status": gate["status"]} for gate in report["gates"]]}))
    return 0 if report["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
