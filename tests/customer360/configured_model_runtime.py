"""Opt-in, fresh actual-browser/model smoke over synthetic connector records.

This test controller does not supply a client, answers, SQL or expected values
to the application. It wraps the default application's model entry point only
to reserve a shared budget before dispatch. Semantic decisions stay outside
model inputs and reach the browser through exact-artifact private handshakes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid

from contextbase.customer360 import agent, contexts
from contextbase.customer360.application import Application
from contextbase.customer360.webserver import Server
from contextbase.model import strict_json_loads
from contextbase.workspace import Workspace
from tests.customer360.model_oracle import review_definitions, check_answer

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ("billing-support", "salesforce-gong")
QUESTIONS = {
    "billing-support": "For this customer, show the remaining amount on open invoices by source currency and quote the latest available support update with its source attribution. Use the reviewed metric and preserve its unit; do not convert currencies.",
    "salesforce-gong": "For this customer, show the open opportunity value by source currency and quote the latest available call evidence with its source attribution. Use the reviewed metric and preserve its unit; do not convert currencies or describe pipeline as recurring revenue.",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read_private(path, *, maximum=8_000_000):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o077
            and info.st_uid == os.getuid() and info.st_size <= maximum, "require_bounded_owner_only_regular_file")
    return strict_json_loads(path.read_text())


def publish(path, value, *, replace=False):
    raw = canonical(value).encode()
    require(not path.is_symlink(), "refuse_symlink")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def inputs():
    from scripts.verify_customer360 import _fingerprint
    manifest = ROOT / "verification/customer360/acceptance.json"
    contract = strict_json_loads(manifest.read_text())
    return _fingerprint(ROOT, manifest, contract["gates"])


def validate_plan(plan, run_dir):
    require(plan.get("spec") == "oriel.customer360.configured-model-plan/v1"
            and plan.get("synthetic") is True and plan.get("allow_model_data") is True
            and type(plan.get("total_max_cost_usd")) is float and plan["total_max_cost_usd"] == 3.0,
            "explicit_synthetic_three_dollar_plan_required")
    require(str(uuid.UUID(plan["run_id"])) == plan["run_id"], "canonical_run_uuid_required")
    require([p["name"] for p in plan["profiles"]] == list(PROFILES), "exact_sequential_profiles_required")
    scheduled = []
    for profile in plan["profiles"]:
        require(re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", profile["origin"])
                and int(profile["origin"].rsplit(":", 1)[1]) <= 65535, "numeric_loopback_required")
        require(profile["browser_output"] == str(run_dir / (profile["name"] + "-browser")), "private_output_binding_required")
        require(set(profile["asks"]) == {"initial", "refreshed"}, "exact_ask_phases_required")
        for phase, operation, settings in (("initial", "context.bootstrap", profile["bootstrap"]),
                                          ("initial", "ask", profile["asks"]["initial"]),
                                          ("refreshed", "ask", profile["asks"]["refreshed"])):
            require(type(settings["max_cost_usd"]) is float and settings["max_cost_usd"] == 0.5
                    and type(settings["max_turns"]) is int and settings["max_turns"] == 4,
                    "frozen_job_limits_required")
            require(set(settings) == {"max_cost_usd", "max_turns", "intent" if operation == "context.bootstrap" else "question"},
                    "exact_model_job_settings_required")
            if operation == "ask":
                require(settings["question"] == QUESTIONS[profile["name"]], "frozen_question_required")
            else:
                require(settings["intent"] == QUESTIONS[profile["name"]], "frozen_bootstrap_intent_required")
            scheduled.append({"profile": profile["name"], "operation": operation, "phase": phase, **settings})
    require(len({p["origin"] for p in plan["profiles"]}) == 2, "distinct_application_origins_required")
    return scheduled


class BudgetLedger:
    """Reserve full job caps durably; interruptions and unreviewed runs stop reuse.

    A single controller owns the OS lock for its lifetime. Reopening a ledger is
    prohibited, even if all earlier jobs succeeded. Unknown or above-cap charges
    remain recorded; a requested provider limit is not an invoice guarantee.
    """

    def __init__(self, run_dir, plan, *, fingerprint=inputs):
        import fcntl
        self.root, self.plan, self.fingerprint = run_dir, plan, fingerprint
        self.plan_hash = digest(plan)
        self.schedule = validate_plan(plan, run_dir)
        self.path = run_dir / "ledger.json"
        self.lock = threading.RLock()
        self.fd = os.open(run_dir / "controller.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.state = {"spec": "oriel.customer360.configured-model-ledger/v1", "run_id": plan["run_id"],
                      "plan_sha256": self.plan_hash, "status": "ready", "total_reserved_usd": 0.0,
                      "known_cost_usd": 0.0, "spend_unknown": False, "model_ids": [], "entries": []}
        try:
            publish(self.path, self.state)
            self.disk_hash = digest(self.state)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _write(self):
        publish(self.path, self.state, replace=True)
        self.disk_hash = digest(self.state)

    def _stable(self):
        require(digest(self.plan) == self.plan_hash, "plan_changed")
        require(read_private(self.root / "plan.json") == self.plan, "plan_changed")
        require(self.fingerprint() == self.plan["input_fingerprint"], "verification_inputs_changed")
        require(digest(read_private(self.path)) == self.disk_hash, "ledger_changed")

    def poison(self, code):
        with self.lock:
            self.state.update(status="stopped", stopped_reason=code, stopped_at=now())
            self._write()

    def reserve(self, profile, operation, arguments, job_id):
        with self.lock:
            try:
                self._stable()
                require(self.state["status"] == "ready", "ledger_not_ready_no_retry")
                index = len(self.state["entries"])
                require(index < len(self.schedule), "no_additional_model_jobs_authorized")
                expected = self.schedule[index]
                require(expected["profile"] == profile and expected["operation"] == operation,
                        "model_job_order_changed")
                require(arguments.get("allow_model_data") is True and arguments.get("profile_name") == "customer_review"
                        and arguments.get("max_cost_usd") == expected["max_cost_usd"]
                        and type(arguments.get("max_turns")) is int and arguments["max_turns"] == expected["max_turns"],
                        "model_job_consent_or_limits_changed")
                require(isinstance(arguments.get("snapshot_id"), str) and re.fullmatch(r"c360-[a-f0-9]{64}", arguments["snapshot_id"]),
                        "snapshot_binding_required")
                if operation == "ask":
                    require(arguments.get("question") == expected["question"] and isinstance(arguments.get("customer_id"), str),
                            "model_question_changed")
                else:
                    require(arguments.get("intent") == expected["intent"], "bootstrap_intent_changed")
                reserved = Decimal(str(self.state["total_reserved_usd"])) + Decimal(str(expected["max_cost_usd"]))
                require(reserved <= Decimal(str(self.plan["total_max_cost_usd"])), "global_budget_exhausted")
                entry = {"index": index, **expected, "job_id": job_id, "arguments_sha256": digest(arguments),
                         "snapshot_id": arguments["snapshot_id"], "customer_id": arguments.get("customer_id"),
                         "status": "reserved_dispatch_or_spend_unknown", "reserved_at": now()}
                self.state["entries"].append(entry)
                self.state.update(status="inflight", total_reserved_usd=float(reserved), spend_unknown=True)
                self._write()  # Must finish before Application._model can run.
                return index
            except BaseException:
                self.poison("reservation_refused")
                raise

    def settle(self, index, result):
        with self.lock:
            require(self.state["status"] in {"inflight", "stopped"} and index == len(self.state["entries"]) - 1
                    and self.state["entries"][index]["status"] == "reserved_dispatch_or_spend_unknown", "settlement_order_changed")
            entry = self.state["entries"][index]
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            entry.update(result_status=result.get("status") if isinstance(result, dict) else None,
                         usage=usage, settled_at=now(), result_sha256=digest(result))
            # Retain every reported charge before checking whether it is valid.
            known = usage.get("known_cost_usd")
            if type(known) in {int, float} and Decimal(str(known)).is_finite() and known >= 0:
                self.state["known_cost_usd"] = float(Decimal(str(self.state["known_cost_usd"])) + Decimal(str(known)))
            try:
                require(self.state["status"] == "inflight" and index == len(self.state["entries"]) - 1
                        and entry["status"] == "reserved_dispatch_or_spend_unknown", "settlement_order_changed")
                require(usage.get("cost_complete") is True and usage.get("spend_unknown") is False
                        and usage.get("model_calls_complete") is True
                        and usage.get("prepared_calls_with_dispatch_unknown") == 0, "model_cost_or_dispatch_unknown")
                cost = usage.get("reported_cost_usd")
                require(type(cost) in {int, float} and Decimal(str(cost)).is_finite() and cost >= 0
                        and cost == known, "invalid_model_cost")
                calls = usage.get("calls")
                require(isinstance(calls, list) and type(usage.get("model_calls")) is int
                        and len(calls) == usage["model_calls"] and 1 <= len(calls) <= entry["max_turns"],
                        "incomplete_model_calls")
                summed = Decimal(0)
                for call in calls:
                    charge, requested = call.get("cost_usd"), call.get("requested_max_cost_usd")
                    require(call.get("status") == "returned" and call.get("cost_known") is True
                            and call.get("is_error") is False and type(charge) in {int, float}
                            and type(requested) in {int, float}, "unknown_or_failed_model_call")
                    value, limit = Decimal(str(charge)), Decimal(str(requested))
                    require(value.is_finite() and limit.is_finite() and 0 <= value <= limit <= Decimal("0.25"),
                            "model_call_budget_exceeded")
                    summed += value
                require(abs(summed - Decimal(str(cost))) <= Decimal("0.000000001")
                        and summed <= Decimal(str(entry["max_cost_usd"]))
                        and Decimal(str(self.state["known_cost_usd"])) <= Decimal("3.0"), "model_budget_exceeded")
                ids = usage.get("model_ids")
                require(isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str) and ids[0], "actual_model_identity_required")
                require(not self.state["model_ids"] or self.state["model_ids"] == ids, "configured_model_changed")
                self.state["model_ids"] = ids
                require(result.get("model_origin") == "configured_claude_cli" and result.get("evidence_origin") == "model_agent"
                        and result.get("approval_performed") is False and result.get("receipt_persisted") is True
                        and result.get("status") == ("candidate" if entry["operation"] == "context.bootstrap" else "answered"),
                        "real_model_success_required")
                if entry["operation"] == "context.bootstrap":
                    verify_candidate_intent(result.get("candidate"), entry["intent"])
                self._stable()
                entry.update(status="awaiting_independent_review", reported_cost_usd=cost,
                             receipt_sha256=result["receipt_sha256"])
                self.state.update(status="awaiting_independent_review", spend_unknown=False)
                self._write()
            except BaseException:
                entry["status"] = "failed_or_unknown_no_retry"
                self.state["spend_unknown"] = usage.get("cost_complete") is not True or usage.get("spend_unknown") is not False
                self.poison("model_result_failed_budget_or_integrity_check")
                raise

    def reviewed(self, job_id, decision):
        with self.lock:
            self._stable()
            entry = self.state["entries"][-1]
            require(self.state["status"] == "awaiting_independent_review" and entry["job_id"] == job_id
                    and entry["status"] == "awaiting_independent_review", "review_order_changed")
            entry.update(status="reviewed", independent_review_sha256=digest(decision), reviewed_at=now())
            self.state["status"] = "complete" if len(self.state["entries"]) == 6 else "ready"
            self._write()


class BudgetedApplication(Application):
    def __init__(self, workspace, profile):
        self.smoke_profile, self.ledger = profile, None
        super().__init__(workspace)  # Keep the real, unmodified configured CLI.

    def _model(self, operation, **arguments):
        require(self.ledger is not None and self.model_client_factory is None, "unmodified_configured_adapter_required")
        operation_name = {agent.bootstrap: "context.bootstrap", agent.ask: "ask"}.get(operation)
        require(operation_name is not None, "unexpected_paid_operation")
        index = self.ledger.reserve(self.smoke_profile, operation_name, arguments, self._active)
        try:
            result = super()._model(operation, **arguments)
        except BaseException:
            self.ledger.poison("model_operation_interrupted_spend_unknown")
            raise
        self.ledger.settle(index, result)
        return result


def jobs(app):
    return sorted((read_private(path) for path in app.root.glob("job-*.json")), key=lambda job: job["created_at"])


def latest(items, operation, phase=None):
    matches = [j for j in items if j["operation"] == operation and j["status"] == "succeeded"
               and (phase is None or j.get("result", {}).get("phase") == phase)]
    require(bool(matches), "required_application_job_missing")
    return matches[-1]


def verify_candidate_intent(candidate, expected):
    stages = candidate.get("stages") if isinstance(candidate, dict) else None
    require(isinstance(stages, list) and len(stages) == 2
            and all(isinstance(stage, dict) for stage in stages)
            and [stage.get("stage") for stage in stages] == ["observe", "define"]
            and all(stage.get("intent") == expected and stage.get("intent_sha256") == digest(expected) for stage in stages),
            "bootstrap_candidate_intent_changed")


def verify_agent_receipt(app, result, *, expected_bootstrap_intent=None):
    path = Path(result["receipt_path"])
    require(path.name == "receipt.json" and path.parent.parent == app.workspace.root / "customer360/agent-runs"
            and not path.parent.is_symlink(), "private_actual_agent_receipt_required")
    receipt = read_private(path)
    require(receipt["receipt_sha256"] == result["receipt_sha256"]
            == digest({key: value for key, value in receipt.items() if key != "receipt_sha256"}), "agent_receipt_changed")
    require(receipt["usage"] == result["usage"] and receipt["model_origin"] == "configured_claude_cli"
            and receipt["evidence_origin"] == "model_agent", "actual_agent_usage_mismatch")
    stored_output = read_private(path.parent / "result.json")
    require(set(stored_output) == ({"candidate"} if receipt["operation"] == "bootstrap" else {"answer"})
            and stored_output == {key: result[key] for key in stored_output}
            and receipt["status"] == result["status"] and receipt["approval_performed"] is False
            and result["approval_performed"] is False, "actual_model_output_mismatch")
    stored_request = read_private(path.parent / "request.json")
    require(digest(read_private(path.parent / "trace.json")) == receipt["trace_sha256"]
            and digest(stored_request) == receipt["request_sha256"]
            and digest(stored_output) == receipt["result_sha256"], "agent_trace_or_result_changed")
    if expected_bootstrap_intent is not None:
        require(receipt["operation"] == "bootstrap" and stored_request.get("intent") == expected_bootstrap_intent,
                "bootstrap_request_intent_changed")
        verify_candidate_intent(result.get("candidate"), expected_bootstrap_intent)
    require(receipt["implementation"] == agent._implementation(), "agent_implementation_changed")
    return receipt


def review_request(app, ledger, proof, request_path, reviews):
    request = read_private(request_path, maximum=16000)
    require(str(uuid.UUID(request["request_id"])) == request["request_id"]
            and request_path.name == "model-review-request-" + request["request_id"] + ".json"
            and request["run_id"] == ledger.plan["run_id"] and request["profile"] == app.smoke_profile,
            "model_review_request_identity_changed")
    kind, phase = request["kind"], request["phase"]
    require(kind in {"context_review", "answer_review"} and phase in {"initial", "refreshed"}, "unexpected_review_kind")
    expected_fields = {"kind", "request_id", "run_id", "profile", "phase", "job_id"}
    expected_fields |= {"proposal_id", "approval_sha256"} if kind == "context_review" else {"answer_id"}
    require(set(request) == expected_fields, "unexpected_review_request_fields")
    job = app.job(request["job_id"])
    require(job["status"] == "succeeded" and job["operation"] == ("context.bootstrap" if kind == "context_review" else "ask"),
            "review_requires_completed_actual_job")
    result = job["result"]
    expected_intent = next(p["bootstrap"]["intent"] for p in ledger.plan["profiles"] if p["name"] == app.smoke_profile)
    verify_agent_receipt(app, result, expected_bootstrap_intent=expected_intent if kind == "context_review" else None)
    if kind == "context_review":
        require(phase == "initial" and not reviews, "context_review_must_be_first")
        candidate = result["candidate"]
        require(candidate["proposal_id"] == request["proposal_id"] and candidate["approval_sha256"] == request["approval_sha256"],
                "candidate_identity_changed")
        decision = review_definitions(app.smoke_profile, candidate)
        metric_name = next(d["name"] for d in decision["reviewed_definitions"] if d["required"])
    else:
        require(len(reviews) == (1 if phase == "initial" else 2), "answer_review_order_changed")
        answer = result["answer"]
        require(answer["answer_id"] == request["answer_id"], "answer_identity_changed")
        retained = jobs(app)
        prepared = latest(retained, "sample.prepare", phase)["result"]
        extraction = latest(retained, "sample.connect", phase)["result"]
        context = contexts.current_context(app.workspace, prepared["profile"], prepared["snapshot_id"])
        require(context["proposal_id"] == reviews[0]["proposal_id"] and context["definitions"] == reviews[0]["definitions"],
                "approved_model_definitions_changed")
        decision = check_answer(app.smoke_profile, phase, answer, definitions=context["definitions"],
                                customer_id=prepared["customer_ids"]["customer_a"], snapshot_id=prepared["snapshot_id"],
                                context_id=context["context_id"], source_names=extraction["source_label_mapping"], require_temporal=True)
        metric_name = reviews[0]["metric_name"]
    decision_path = proof / ("independent-review-" + request["request_id"] + ".json")
    publish(decision_path, decision)
    ledger.reviewed(job["job_id"], decision)
    retained_review = {**request, "metric_name": metric_name, "decision": decision}
    if kind == "context_review":
        retained_review["definitions"] = result["candidate"]["definitions"]
    reviews.append(retained_review)
    publish(proof / ("model-review-response-" + request["request_id"] + ".json"),
            {**request, "approved": True, "metric_name": metric_name, "decision_sha256": digest(decision)})


def run_browser(app, server, ledger, run_dir, node, chrome):
    profile = app.smoke_profile
    proof = run_dir / (profile + "-browser")
    command = [node, str(ROOT / "scripts/customer360_browser_check.mjs"), "--origin", server.origin,
               "--profile", profile, "--output", str(proof), "--chrome", chrome, "--synthetic-fixtures",
               "--configured-model-plan", str(run_dir / "plan.json"), "--allow-model-data", "--timeout-seconds", "1150"]
    reviews, handled = [], set()
    streams = [os.fdopen(os.open(run_dir / (profile + "-driver-" + suffix + ".txt"),
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") for suffix in ("stdout", "stderr")]
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1], start_new_session=True)
    deadline = time.monotonic() + 1180
    try:
        while process.poll() is None:
            require(time.monotonic() < deadline, "configured_browser_timeout_no_retry")
            for path in sorted(proof.glob("model-review-request-*.json")):
                if path.name in handled:
                    continue
                try:
                    review_request(app, ledger, proof, path, reviews)
                except BaseException as error:
                    ledger.poison("independent_review_failed")
                    request = read_private(path, maximum=16000)
                    publish(proof / ("model-review-response-" + request["request_id"] + ".json"),
                            {**request, "approved": False, "error_code": getattr(error, "code", "independent_review_failed")})
                    publish(run_dir / (profile + "-review-failure.json"),
                            {"error_type": type(error).__name__, "error_code": str(error)[:200],
                             "review_receipt": getattr(error, "review_receipt", None)})
                handled.add(path.name)
            time.sleep(.1)
        receipt = read_private(proof / "receipt.json")
        publish(run_dir / (profile + "-driver.json"), {"returncode": process.returncode, "receipt_sha256": digest(receipt)})
        require(process.returncode == 0 and receipt["status"] == "passed" and len(reviews) == 3,
                "actual_configured_browser_or_independent_review_failed")
        return {"profile": profile, "workspace": str(app.workspace.root), "proof": str(proof),
                "receipt": receipt, "jobs": jobs(app), "reviews": reviews}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for stream in streams:
            stream.close()


def execute_smoke():
    """Require an explicit per-invocation opt-in and a never-used private path."""
    require(os.environ.get("ORIEL_C360_CONFIGURED_MODEL") == "1", "model_smoke_requires_explicit_opt_in")
    selected = os.environ.get("ORIEL_C360_MODEL_RUN_DIR", "")
    run_dir = Path(selected)
    parent = ROOT / ".oriel/verification/customer360"
    require(run_dir.is_absolute() and run_dir.parent == parent and re.fullmatch(r"configured-model-[a-z0-9-]{1,100}", run_dir.name),
            "new_private_model_run_directory_required")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not parent.is_symlink() and parent.resolve() == parent and parent.stat().st_uid == os.getuid()
            and not parent.stat().st_mode & 0o022, "owner_controlled_verification_parent_required")
    run_dir.mkdir(mode=0o700, exist_ok=False)  # A previous paid run is never replayed or resumed.
    node, cli = shutil.which("node"), shutil.which("claude")
    chrome = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome"),
                              shutil.which("chromium")) if p and Path(p).is_file()), None)
    require(node and cli and chrome, "installed_node_chrome_and_configured_cli_required")
    version = subprocess.run([cli, "--version"], cwd=run_dir, capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    require(len(version) < 200 and re.fullmatch(r"[a-zA-Z0-9 .()_-]+", version), "bounded_cli_version_required")
    apps, servers, threads, ledger = [], [], [], None
    summary = {"spec": "oriel.customer360.configured-product-smoke/v1", "status": "running", "started_at": now(),
               "run_dir": str(run_dir), "vendor_adapter_verified": False, "comparative_benchmark": False,
               "cost_basis": "provider_reported_usage_estimate_not_invoice", "profiles": []}
    try:
        for name in PROFILES:
            app = BudgetedApplication(Workspace(run_dir / (name + "-workspace")), name)
            apps.append(app)
            servers.append(Server(app, 0))
        plan = {"spec": "oriel.customer360.configured-model-plan/v1", "run_id": str(uuid.uuid4()),
                "created_at": now(), "total_max_cost_usd": 3.0, "allow_model_data": True, "synthetic": True,
                "transport": "existing_configured_claude_cli_default_model_no_injected_client",
                "cli_version": version, "cli_executable_sha256": hashlib.sha256(Path(cli).read_bytes()).hexdigest(),
                "input_fingerprint": inputs(), "retry_policy": "none_after_failed_unknown_or_unreviewed_job",
                "questions_contain_oracle_values": False, "profiles": []}
        for name, server in zip(PROFILES, servers):
            plan["profiles"].append({"name": name, "origin": server.origin, "browser_output": str(run_dir / (name + "-browser")),
                                     "bootstrap": {"intent": QUESTIONS[name], "max_cost_usd": 0.5, "max_turns": 4},
                                     "asks": {phase: {"question": QUESTIONS[name], "max_cost_usd": 0.5, "max_turns": 4}
                                              for phase in ("initial", "refreshed")}})
        validate_plan(plan, run_dir)
        publish(run_dir / "plan.json", plan)  # Predeclare everything before inference.
        ledger = BudgetLedger(run_dir, plan)
        summary.update(run_id=plan["run_id"], plan_sha256=digest(plan), input_fingerprint=plan["input_fingerprint"])
        publish(run_dir / "summary.json", summary)
        for app, server in zip(apps, servers):
            app.ledger = ledger
            thread = threading.Thread(target=server.serve_forever, name="customer360-configured-smoke", daemon=True)
            thread.start()
            threads.append(thread)
        for app, server in zip(apps, servers):
            result = run_browser(app, server, ledger, run_dir, node, chrome)
            summary["profiles"].append(result)
            publish(run_dir / "summary.json", summary, replace=True)
        ledger._stable()
        require(ledger.state["status"] == "complete" and len(ledger.state["entries"]) == 6
                and all(entry["status"] == "reviewed" for entry in ledger.state["entries"]), "six_reviewed_model_jobs_required")
        summary.update(status="passed", ledger=ledger.state, finished_at=now())
        publish(run_dir / "summary.json", summary, replace=True)
        return summary
    except BaseException as error:
        if ledger:
            ledger.poison("controller_stopped_no_additional_dispatch")
        summary.update(status="failed", error_type=type(error).__name__, error_code=str(error)[:200], finished_at=now(),
                       ledger=ledger.state if ledger else None)
        publish(run_dir / "summary.json", summary, replace=True)
        raise AssertionError("Fresh configured-model product smoke failed; retained evidence: " + str(run_dir)) from error
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            thread.join(timeout=10)
        for server in servers:
            server.server_close()
        for app in apps:
            app.close()
        if ledger:
            # A browser failure may precede the active provider's return. Drain
            # it before final accounting; ledger settlement retains late costs
            # even after the controller has forbidden any further dispatch.
            summary.update(ledger=ledger.state, finished_at=now())
            publish(run_dir / "summary.json", summary, replace=True)
            ledger.close()
