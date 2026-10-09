"""Fresh local browser acceptance, with explicit scripted-model provenance.

These tests launch isolated Chrome profiles against newly provisioned app
workspaces. They neither replay old screenshots nor treat an API-only flow as
browser evidence. Real dlt SQLite extraction and DuckDB are used; model calls are
the server's deterministic test transport, never a provider. Missing installed
runtime/browser prerequisites fail visibly rather than skipping acceptance.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


def _publish_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with _private_file(temporary) as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


class ProductOnboardingAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / ".oriel" / "verification" / "customer360" / ("browser-suite-" + uuid.uuid4().hex)
        cls.root.mkdir(mode=0o700, parents=True)
        cls.node = shutil.which("node")
        cls.chrome = next((item for item in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"))
            if item and Path(item).is_file()), None)
        if not cls.node or not cls.chrome:
            raise AssertionError("Product browser acceptance requires installed Node 22+ and Chrome; no browser run was skipped. Evidence: " + str(cls.root))
        # Independent app processes, workspaces and Chrome profiles permit both
        # real browser flows to run concurrently without sharing domain state.
        profiles = ("billing-support", "salesforce-gong")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {profile: executor.submit(cls._run_fresh_browser, profile) for profile in profiles}
            cls.results = {profile: future.result() for profile, future in futures.items()}

    @classmethod
    def _run_fresh_browser(cls, profile):
        workspace, proof = cls.root / (profile + "-workspace"), cls.root / (profile + "-browser")
        with _private_file(cls.root / (profile + "-server-stderr.txt")) as errors:
            server = subprocess.Popen(
                [sys.executable, "-m", "tests.customer360.scripted_browser_server", "--workspace", str(workspace),
                 "--port", "0", "--synthetic-fixtures"], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=errors, text=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(server.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=20):
                        raise AssertionError("Synthetic app server did not announce readiness: " + str(cls.root))
                    line = server.stdout.readline(4096)
                announced = json.loads(line)
                if announced.get("model_origin") != "scripted_agent_test" or announced.get("actual_model_calls") != 0:
                    raise AssertionError("Only the explicit no-provider test server is permitted.")
                command = [cls.node, "scripts/customer360_browser_check.mjs", "--origin", announced["origin"],
                           "--profile", profile, "--output", str(proof), "--chrome", cls.chrome,
                           "--synthetic-fixtures", "--scripted-model", "--external-refresh-check", "--timeout-seconds", "180"]
                stop, mutation_errors = threading.Event(), []
                def refresh_after_browser_checkpoint():
                    try:
                        cls._external_refresh(workspace, proof, profile, stop)
                    except BaseException as error:
                        mutation_errors.append(error)
                        _publish_json(cls.root / (profile + "-external-refresh-error.json"),
                                      {"status": "failed", "error_type": type(error).__name__, "error": str(error)[:1000]})
                        if proof.is_dir() and not (proof / "external-refresh-complete.json").exists():
                            _publish_json(proof / "external-refresh-complete.json", {"status": "failed"})
                mutation = threading.Thread(target=refresh_after_browser_checkpoint, name="synthetic-browser-refresh")
                mutation.start()
                try:
                    completed = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, text=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=210, check=False)
                except subprocess.TimeoutExpired as error:
                    # Even a completed browser checkpoint is not a successful
                    # test if its command cannot terminate. Preserve that fact.
                    def bounded(value):
                        return (value.decode(errors="replace") if isinstance(value, bytes) else value or "")[:10000]
                    with _private_file(cls.root / (profile + "-driver.json")) as handle:
                        json.dump({"status": "driver_timeout", "timeout_seconds": 210,
                                   "stdout": bounded(error.stdout), "stderr": bounded(error.stderr),
                                   "browser_receipt_is_not_command_success": True}, handle, indent=2)
                    raise AssertionError("Browser driver timed out; retained evidence: " + str(cls.root)) from error
                finally:
                    stop.set()
                    mutation.join(timeout=150)
                if mutation.is_alive():
                    raise AssertionError("Synthetic source refresh did not stop within its bound: " + str(cls.root))
                if mutation_errors:
                    raise AssertionError("Trusted synthetic refresh failed: " + str(mutation_errors[0]) + "; evidence: " + str(cls.root))
                with _private_file(cls.root / (profile + "-driver.json")) as handle:
                    json.dump({"returncode": completed.returncode, "stdout": completed.stdout[:10000],
                               "stderr": completed.stderr[:10000]}, handle, indent=2)
                receipt_file = proof / "receipt.json"
                if not receipt_file.is_file():
                    raise AssertionError("Browser did not retain a result: " + str(cls.root))
                receipt = json.loads(receipt_file.read_text())
                if completed.returncode or receipt.get("status") != "passed":
                    raise AssertionError("Actual browser flow failed: " + str(receipt.get("error")) + "; evidence: " + str(proof))
                jobs = [json.loads(path.read_text()) for path in (workspace / "customer360-app").glob("job-*.json")]
                jobs.sort(key=lambda value: value["created_at"])
                return {"workspace": workspace, "proof": proof, "receipt": receipt, "jobs": jobs,
                        "checkpoints": {value["name"]: value for value in receipt["checkpoints"]}}
            finally:
                if server.poll() is None:
                    server.send_signal(signal.SIGINT)
                    try:
                        server.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        server.terminate()
                        try:
                            server.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait(timeout=5)
                if server.stdout:
                    server.stdout.close()

    @classmethod
    def _external_refresh(cls, workspace, proof, profile, stop):
        """Mutate only this test's newly-created synthetic workspace, not HTTP."""
        request_path = proof / "external-refresh-request.json"
        deadline = time.monotonic() + 180
        while not stop.is_set() and time.monotonic() < deadline:
            if request_path.is_file():
                try:
                    request = json.loads(request_path.read_text())
                    break
                except json.JSONDecodeError:
                    pass  # Writer may still be finishing this bounded file.
            stop.wait(.1)
        else:
            if stop.is_set():
                return
            raise AssertionError("Browser never requested its actual source-refresh checkpoint.")
        if workspace != cls.root / (profile + "-workspace") or workspace.is_symlink() or proof.is_symlink():
            raise AssertionError("Only this invocation's private synthetic workspace may be refreshed.")
        if request_path.is_symlink() or request_path.stat().st_size > 16000 or request_path.stat().st_mode & 0o077:
            raise AssertionError("Require a bounded owner-only browser request.")
        if (set(request) != {"spec", "request_id", "profile", "snapshot_id", "phase", "operation"}
                or request["spec"] != "oriel.customer360.browser-refresh-request/v1"
                or request["profile"] != profile or request["phase"] != "initial"
                or request["operation"] != "samples.connect" or str(uuid.UUID(request["request_id"])) != request["request_id"]):
            raise AssertionError("Unexpected browser source-refresh request.")
        sample = json.loads((workspace / "customer360-app" / "sample.json").read_text())
        if sample != {"profile": profile, "phase": "refreshed", "scope": "synthetic_local_sources"}:
            raise AssertionError("The source-refresh probe requires this test's completed synthetic refreshed phase.")
        jobs = [json.loads(path.read_text()) for path in (workspace / "customer360-app").glob("job-*.json")]
        if any(job["status"] in {"queued", "running"} for job in jobs):
            raise AssertionError("The application must be idle before out-of-band source publication.")
        asks = sorted((job for job in jobs if job["operation"] == "ask"), key=lambda job: job["created_at"])
        if not asks or asks[-1]["result"]["answer"]["snapshot_id"] != request["snapshot_id"]:
            raise AssertionError("Browser checkpoint must name the actual displayed current-answer snapshot.")
        from contextbase.connectors import service
        from contextbase.customer360 import samples
        from contextbase.workspace import Workspace

        current = Workspace(workspace)
        configured = service.configured(current)["sources"]
        names = {"fixture_stripe", "fixture_intercom"} if profile == "billing-support" else {"fixture_salesforce", "fixture_gong"}
        if {item["name"] for item in configured} != names or any(item["source_type"] != "sqlite" for item in configured):
            raise AssertionError("Only the two actual local synthetic SQLite sources are permitted.")
        extraction = samples.connect(current, profile, phase="initial")
        if extraction["actual_dlt_executed"] is not True or extraction["vendor_adapter_verified"] is not False or extraction["model_calls"] != 0:
            raise AssertionError("Require actual local extraction, not a simulated HTTP answer.")
        payload = json.dumps(extraction, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with _private_file(proof / "external-refresh-publication.json") as handle:
            handle.write(payload)
        ack = {"request_id": request["request_id"], "profile": profile, "phase": "initial", "status": "passed",
               "actual_dlt_executed": True, "model_calls": 0, "vendor_calls": 0,
               "extraction_receipt_sha256": hashlib.sha256(payload.encode()).hexdigest()}
        _publish_json(proof / "external-refresh-complete.json", ack)

    def test_clean_checkout_documented_sample_workflow(self):
        from scripts.check_customer360_install import verify_install

        # This builds and installs now into new isolated environments. It never
        # substitutes a previous successful installation receipt for execution.
        result = verify_install(ROOT, self.root / "clean-install", command_timeout=180)
        self.assertEqual(result["status"], "passed")
        for key in ("model_calls", "vendor_calls", "network_dependency_fetches"):
            self.assertEqual(result[key], 0)
        self.assertEqual(result["wheel"]["ui_assets_verified"], 3)
        self.assertEqual(result["wheel"]["sample_json_verified"], 25)
        self.assertFalse(result["wheel"]["oracle_packaged"])
        self.assertRegex(result["wheel"]["sha256"], r"^[a-f0-9]{64}$")
        workflow = result["installed_workflow"]
        self.assertTrue(workflow["isolated_python"])
        self.assertFalse(workflow["checkout_on_sys_path"])
        self.assertFalse(workflow["editable"])
        self.assertEqual({item["profile"] for item in workflow["profiles"]}, {"billing-support", "salesforce-gong"})
        for profile in workflow["profiles"]:
            self.assertEqual(profile["status"], "passed")
            self.assertEqual([phase["phase"] for phase in profile["phases"]], ["initial", "refreshed"])
            self.assertTrue(all(phase["status"] == "passed" and phase["quote_count"] > 0 for phase in profile["phases"]))
            self.assertNotEqual(profile["phases"][0]["snapshot_id"], profile["phases"][1]["snapshot_id"])
            self.assertEqual(profile["phases"][0]["definition_sha256"], profile["phases"][1]["definition_sha256"])
        self.assertEqual(result["installed_cli_http"]["status"], "passed")
        self.assertTrue(result["installed_cli_http"]["session_established"])

    def test_interface_connect_review_ask_evidence_refresh(self):
        result = self.results["billing-support"]
        receipt, jobs = result["receipt"], result["jobs"]
        self.assertEqual(receipt["interaction"], "chrome_cdp_actual_dom_forms_and_clicks")
        self.assertEqual(receipt["actual_model_calls"], 0)
        self.assertEqual(receipt["browser_cleanup"], {"main_process_exited": True, "isolated_profile_removed": True})
        self.assertTrue(receipt["scripted_model_enabled"])
        self.assertFalse(receipt["model_reasoning_verified"])
        self.assertEqual(receipt["console_errors"], [])
        self.assertEqual(receipt["blocked_requests"], [])
        self.assertEqual(len(receipt["job_receipts"]), len(jobs))
        self.assertEqual({item["job_id"] for item in receipt["job_receipts"]}, {item["job_id"] for item in jobs})
        for item in receipt["job_receipts"]:
            self.assertRegex(item["response_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(set(receipt["implementation"]["assets"]), {"/", "/app.js", "/style.css"})
        operations = [job["operation"] for job in jobs]
        for earlier, later in (("sample.connect", "sample.prepare"), ("links.approve", "profile.propose"),
                               ("profile.approve", "materialize"), ("context.bootstrap", "context.approve"),
                               ("context.approve", "ask"), ("review.save", "review.compare")):
            self.assertLess(operations.index(earlier), operations.index(later))
        bootstrap = next(job["result"] for job in jobs if job["operation"] == "context.bootstrap")
        prepared = next(job["result"] for job in jobs if job["operation"] == "sample.prepare")
        proposed = next(job["result"] for job in jobs if job["operation"] == "profile.propose")
        approved_mapping = next(job["result"] for job in jobs if job["operation"] == "profile.approve")
        self.assertEqual(proposed["profile"], prepared["mapping"])
        self.assertEqual(proposed["profile"], approved_mapping["candidate"]["profile"])
        self.assertEqual(bootstrap["candidate"]["profile_approval"], approved_mapping["approval_sha256"])
        self.assertIn("explicit_measure_policy_form_submission_and_review", receipt["verified"])
        self.assertIn("measure_policy_form_defaults_unknown", receipt["verified"])
        self.assertFalse(bootstrap["approval_performed"])
        self.assertEqual([call["stage"] for call in bootstrap["usage"]["calls"]], ["observe", "define"])
        runs = [job["result"] for job in jobs if job["operation"] in {"context.bootstrap", "ask"}]
        self.assertEqual(len(runs), 3)
        for run in runs:
            self.assertEqual(run["model_origin"], "injected_client_unverified")
            self.assertEqual(run["evidence_origin"], "scripted_agent_test")
            self.assertTrue(run["receipt_persisted"])
            self.assertEqual(run["usage"]["model_ids"], ["scripted-browser-transport"])
            self.assertEqual(run["usage"]["reported_cost_usd"], 0)
        asks = [job["result"]["answer"] for job in jobs if job["operation"] == "ask"]
        self.assertEqual(len(asks), 2)
        self.assertEqual(asks[0]["customer_id"], asks[1]["customer_id"])
        self.assertEqual(asks[0]["definition_sha256"], asks[1]["definition_sha256"])
        self.assertNotEqual(asks[0]["snapshot_id"], asks[1]["snapshot_id"])
        self.assertNotEqual(asks[0]["answer_id"], asks[1]["answer_id"])
        self.assertEqual([{x["currency"]: x["value"] for x in answer["metrics"][0]["result"]["values"]} for answer in asks],
                         [{"USD": "125000", "EUR": "30000"}, {"USD": "10000", "EUR": "30000"}])
        self.assertTrue(all(answer["quotes"] for answer in asks))
        self.assertIn("Invoice export is fixed", json.dumps(asks[1]["quotes"]))
        quote_job = next(job["result"] for job in jobs if job["operation"] == "evidence")
        self.assertIn(quote_job["text"], result["checkpoints"]["source-quote-opened"]["observed"]["#evidence-content"])
        comparison = next(job["result"] for job in jobs if job["operation"] == "review.compare")
        self.assertIn("identity_sha256", comparison["changed_basis"])
        self.assertEqual(comparison["metric_changes"][0]["before"], "125000")
        self.assertEqual(comparison["metric_changes"][0]["after"], "10000")
        self.assertIsNone(comparison["metric_changes"][0]["delta"])
        self.assertIn("Not comparable", result["checkpoints"]["saved-current-comparison"]["observed"]["#comparison-panel"])

    def test_both_profiles_use_same_customer_interface(self):
        self.assertEqual(set(self.results), {"billing-support", "salesforce-gong"})
        asset_sets, operation_sets = [], []
        for profile, result in self.results.items():
            receipt, jobs = result["receipt"], result["jobs"]
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["profile"], profile)
            self.assertFalse(receipt["vendor_adapter_verified"])
            asset_sets.append(receipt["implementation"]["assets"])
            operation_sets.append([item["operation"] for item in receipt["operations"]])
            self.assertIn("mobile_no_horizontal_overflow", receipt["verified"])
            expected_intent = "Prepare a customer review with the approved account metrics and attributed source evidence."
            self.assertEqual(receipt["bootstrap_intent"], expected_intent)
            self.assertEqual(next(op["intent"] for op in receipt["operations"] if op["operation"] == "context.bootstrap"), expected_intent)
            bootstrap = next(job["result"] for job in jobs if job["operation"] == "context.bootstrap")
            intent_hash = hashlib.sha256(json.dumps(expected_intent, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual([stage["intent"] for stage in bootstrap["candidate"]["stages"]], [expected_intent] * 2)
            self.assertEqual([stage["intent_sha256"] for stage in bootstrap["candidate"]["stages"]], [intent_hash] * 2)
            self.assertIn("operator_bootstrap_intent_entered_and_retained", receipt["verified"])
            self.assertIn("deterministic_calculation_scope_separate_from_provisional_notes", receipt["verified"])
            candidate_text = result["checkpoints"]["scripted-context-candidate-unapproved"]["observed"]["#context-proposals"]
            approved_text = result["checkpoints"]["scripted-context-reviewed"]["observed"]["#approved-context"]
            for displayed in (candidate_text, approved_text):
                self.assertIn(expected_intent, displayed)
                self.assertIn("Retained operator task request — not reviewed policy", displayed)
                self.assertIn("Provisional description / reviewer note — not calculation scope", displayed)
                self.assertIn("resolved", displayed)
            # Check exact executable structure, without pretending this proves
            # arbitrary model-authored descriptions are semantically truthful.
            for job in jobs:
                if job["operation"] != "ask":
                    continue
                answer = job["result"]["answer"]
                for metric in answer["metrics"]:
                    scope = metric["calculation_scope"]
                    self.assertEqual(scope["population"], "selected_customer_resolved_records")
                    for key in ("operation", "kind", "field", "filters", "unit", "currency_field"):
                        self.assertEqual(scope[key], metric["definition"][key])
                    self.assertEqual(scope["snapshot_id"], answer["snapshot_id"])
                    self.assertEqual(scope["customer_id"], answer["customer_id"])
                    self.assertNotIn("description", scope)
            policy_text = result["checkpoints"]["measure-policy-entered-and-proposed"]["observed"]["#mapping-proposals"]
            self.assertIn("Currency — minor units" if profile == "billing-support" else "Currency — major units", policy_text)
            self.assertIn("Sum permitted", policy_text)
            self.assertIn("Currency role: currency", policy_text)
            self.assertTrue((result["proof"] / "mobile-brief.png").stat().st_size > 1000)
            materializations = [job["result"] for job in jobs if job["operation"] == "materialize"]
            self.assertEqual(len(materializations), 2)
            capabilities = json.dumps(materializations[0]["capabilities"])
            self.assertIn("billing" if profile == "billing-support" else "commercial", capabilities)
            self.assertIn("support" if profile == "billing-support" else "conversation", capabilities)
        self.assertEqual(asset_sets[0], asset_sets[1])
        self.assertEqual(operation_sets[0], operation_sets[1])
        asks = [job["result"]["answer"] for job in self.results["salesforce-gong"]["jobs"] if job["operation"] == "ask"]
        self.assertEqual([{x["currency"]: x["value"] for x in answer["metrics"][0]["result"]["values"]} for answer in asks],
                         [{"USD": "12500", "EUR": "3000"}, {"USD": "9000", "EUR": "3000"}])
        self.assertNotEqual(self.results["billing-support"]["workspace"], self.results["salesforce-gong"]["workspace"])

    def test_setup_failure_and_missing_capability_are_visible(self):
        for profile, result in self.results.items():
            with self.subTest(profile=profile):
                jobs, points = result["jobs"], result["checkpoints"]
                self.assertEqual(jobs[0]["operation"], "source.add")
                self.assertEqual(jobs[0]["status"], "failed")
                # FileNotFoundError is deliberately rendered as the bounded
                # generic operation failure, without leaking filesystem paths.
                self.assertEqual(jobs[0]["error"]["code"], "operation_failed")
                refusal = result["receipt"]["terminal_failure_check"]
                self.assertEqual(refusal["operation"], "source.add")
                self.assertEqual(refusal["job_id"], jobs[0]["job_id"])
                self.assertTrue(refusal["impossible_success_predicate"])
                self.assertTrue(refusal["observed_refusal_before_success_wait"])
                self.assertEqual(refusal["model_calls"], 0)
                self.assertLess(refusal["elapsed_seconds"], 10)
                self.assertIn("terminal_job_failure_preempts_success_dom_wait", result["receipt"]["verified"])
                self.assertEqual([job["operation"] for job in jobs if job["status"] == "failed"],
                                 ["source.add", "evidence", "review.save", "brief", "review.compare"])
                failed = points["visible-connection-refusal"]["observed"]
                self.assertEqual(failed["#job-title"], "Operation stopped")
                self.assertTrue(failed["#global-error"].strip())
                self.assertIn("No sources connected", failed["#sources-list"])
                self.assertIn("Unresolved conflict", points["identity-reviewed-conflicts-retained"]["observed"]["#identity-proposals"])
                self.assertIn("Also fetched to locate selected records: channels, guilds.",
                              points["declared-parent-fetch-preview"]["observed"]["#resource-plan"])
                self.assertIn("selection changed", points["changed-selection-refused"]["observed"]["#global-error"])
                self.assertIn("Select at least one resource", points["empty-resource-selection-refused"]["observed"]["#global-error"])
                self.assertEqual(sum(job["operation"] == "source.add" for job in jobs), 1)
                for key in ("initial-brief-with-gaps", "initial-scripted-agent-answer", "refreshed-scripted-agent-answer"):
                    rendered = points[key]["observed"]["#answer-panel"]
                    self.assertIn("Current snapshot coverage", rendered)
                    self.assertIn("Notes retained from context approval", rendered)
                    self.assertIn("No reviewed source capability", rendered)
                    self.assertIn("excluded", rendered)
                    self.assertEqual("not rechecked after refresh" in rendered, key.startswith("refreshed"))
                self.assertFalse(result["receipt"]["hosted_authorization_verified"])
                self.assertTrue(result["receipt"]["external_refresh_check"])
                self.assertTrue(result["receipt"]["external_refresh"]["actual_dlt_executed"])
                for label in ("evidence", "save", "brief", "compare"):
                    after_failure = points["stale-" + label + "-refused"]["observed"]
                    self.assertTrue(after_failure["#global-error"].strip())
                    self.assertNotIn("Evidence checked for this snapshot", after_failure["#answer-panel"])
                    self.assertNotIn("Inspect calculation", after_failure["#answer-panel"])
                    self.assertEqual(after_failure["#comparison-panel"], "")
                historical = points["historical-review-after-source-change"]["observed"]["#answer-panel"]
                self.assertIn("Historical saved brief", historical)
                self.assertNotIn("Evidence checked for this snapshot", historical)
                self.assertEqual(jobs[-1]["operation"], "review.load")
                self.assertEqual(jobs[-1]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
