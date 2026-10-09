"""Single-operator application jobs over the public Customer 360 contracts.

No job supplies SQL, shell commands, a model client or an arbitrary workspace.
Source configuration and sync are explicit operator actions. Question tools do
not inherit their authority. Requests are serialized and never resumed after a
process interruption, especially when model usage may be unknown.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from ..connectors import available, describe, readiness, service
from ..model import strict_json_loads
from . import agent, answers, contexts, identity, profiles, reviews, snapshots
from .contracts import canonical, sha256

MAX_JOB_BYTES = 8_000_000
MAX_JOBS = 500


def private_workspace(workspace):
    """Create a private app workspace; never silently alter an existing shared one."""
    workspace.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (workspace.root, workspace.db_path):
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        regular = stat.S_ISDIR(info.st_mode) if path == workspace.root else stat.S_ISREG(info.st_mode)
        if not regular or (hasattr(os, "getuid") and info.st_uid != os.getuid()) or info.st_mode & 0o077:
            raise ValueError("The application needs an owner-only workspace directory and database. Select a private workspace or review its filesystem permissions before starting.")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Use a canonical request or job UUID.") from None
    return value


def _write(path, value):
    raw = canonical(value).encode()
    if len(raw) > MAX_JOB_BYTES:
        raise ValueError("Application result exceeds its retention limit; reduce selected source scope.")
    if path.is_symlink():
        raise ValueError("Application state cannot use symbolic links.")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JOB_BYTES:
        raise ValueError("Application state must be a regular bounded local file.")
    return strict_json_loads(path.read_text())


def _presentation(value):
    """Attach derived display scopes to a response copy, never stored artifacts.

    Paths distinguish repeated definitions in historical/current scopes. Python
    produces the text before the browser's unsafe-integer transport conversion.
    A bounded incomplete presentation remains explicit; no prose is a fallback.
    """
    from .metric_scope import calculation_scope
    scopes, nodes, characters, truncated = [], 0, 0, False

    def visit(item, path, snapshot_id=None, customer_id=None, depth=0):
        nonlocal nodes, characters, truncated
        if truncated:
            return
        nodes += 1
        if nodes > 100_000 or depth > 40 or len(scopes) >= 1024 or characters > 1_000_000:
            truncated = True
            return
        if isinstance(item, dict):
            snapshot_id = item.get("snapshot_id", snapshot_id)
            customer_id = item.get("customer_id", customer_id)
            if set(item) == contexts.DEFINITION_FIELDS:
                try:
                    scope = calculation_scope(item, snapshot_id=snapshot_id, customer_id=customer_id)
                except ValueError:
                    return  # Missing display scope is explicit in the browser.
                entry = {"path": path, "definition_sha256": sha256(item), "scope": scope}
                size = len(canonical(entry).encode())
                if characters + size > 1_000_000:
                    truncated = True
                    return
                characters += size
                scopes.append(entry)
                return
            for key, child in item.items():
                visit(child, path + [key], snapshot_id, customer_id, depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, path + [index], snapshot_id, customer_id, depth + 1)

    visit(value, [])
    return {**value, "presentation": {"calculation_scopes": scopes, "truncated": truncated}}


class Application:
    def __init__(self, workspace, *, model_client_factory=None):
        private_workspace(workspace)
        self.workspace = workspace
        self.model_client_factory = model_client_factory
        self.model_origin = "scripted_agent_test" if model_client_factory is not None else "configured_claude_cli"
        self.root = workspace.root / "customer360-app"
        if self.root.is_symlink():
            raise ValueError("Application state cannot use symbolic links.")
        self.root.mkdir(mode=0o700, exist_ok=True)
        root_info = self.root.stat()
        if root_info.st_mode & 0o077 or (hasattr(os, "getuid") and root_info.st_uid != os.getuid()):
            raise ValueError("Application state must be owner-only.")
        try:
            import fcntl
        except ImportError:
            raise ValueError("This local application currently requires POSIX workspace locking.") from None
        descriptor = os.open(self.root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("Application ownership lock must be a private regular file.")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ValueError("Another application already owns this workspace. Use its existing session or stop it first.") from None
        except BaseException:
            os.close(descriptor)
            raise
        self._owner_fd = descriptor
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = None
        self._closed = False
        try:
            self._load_jobs()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oriel-customer")
        except BaseException:
            os.close(self._owner_fd)
            self._owner_fd = None
            raise

    def _load_jobs(self):
        paths = sorted(self.root.glob("job-*.json"))
        if len(paths) > MAX_JOBS:
            raise ValueError("Application job history exceeds its supported bound.")
        for path in paths:
            value = _read(path)
            job_id = _id(value["job_id"])
            if path.name != "job-" + job_id + ".json" or value["status"] not in {"queued", "running", "succeeded", "failed"}:
                raise ValueError("Invalid application job history.")
            if value["status"] in {"queued", "running"}:
                value.update(status="failed", finished_at=_now(), error={"code": "interrupted",
                             "message": "The process stopped before this job finished. It was not resumed. Inspect source or model receipts before starting another run."})
                value.pop("result", None)
                _write(path, value)
            self._jobs[job_id] = value

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.shutdown(wait=True, cancel_futures=False)
        finally:
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None

    def catalog(self):
        return available()

    def connector(self, source_type):
        return {"connector": describe(source_type), "readiness": readiness.inspect(source_type)}

    def source_plan(self, source_type, resources=None):
        """Read packaged declarations only; no workspace or connector execution."""
        from ..connectors.policy import selection_plan
        return selection_plan(describe(source_type), [] if resources is None else resources)

    def state(self):
        from . import onboarding
        with self._lock:
            if self._active is not None:
                return {"busy": True, "active_job_id": self._active, "model_origin": self.model_origin}
            private_workspace(self.workspace)
            result = onboarding.workspace_state(self.workspace)
            result["pending"] = {kind: onboarding.pending_proposals(self.workspace, kind=kind) for kind in ("links", "profile", "context")}
            result["model_origin"] = self.model_origin
            result["sample"] = _read(self.root / "sample.json") if (self.root / "sample.json").exists() else None
            result["busy"] = False
            return _presentation(result)

    def submit(self, operation, arguments, request_id=None):
        if not isinstance(operation, str) or operation not in self.operations():
            raise ValueError("Select a declared application operation.")
        if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > 1_000_000:
            raise ValueError("Supply a bounded object of operation arguments.")
        # Reject unknown arguments, including client/workspace injection, before
        # retaining or queuing the request. Domain validators check their values.
        function, allowed = self.operations()[operation]
        if set(arguments) - allowed:
            raise ValueError("Unexpected application operation argument.")
        job_id = _id(request_id) if request_id is not None else str(uuid.uuid4())
        request_hash = sha256({"operation": operation, "arguments": arguments})
        with self._lock:
            if self._closed:
                raise ValueError("Application is shutting down.")
            if job_id in self._jobs:
                job = self._jobs[job_id]
                if job["request_sha256"] != request_hash:
                    raise ValueError("A request ID cannot be reused for different arguments.")
                return {"job_id": job_id, "status": job["status"]}
            if self._active is not None:
                raise RuntimeError("Another operation is running. Wait for its result before starting a new one.")
            if len(self._jobs) >= MAX_JOBS:
                raise ValueError("Application job history is full. Retain its receipts and start a new private workspace.")
            private_workspace(self.workspace)
            job = {"job_id": job_id, "operation": operation, "request_sha256": request_hash,
                   "status": "queued", "created_at": _now(), "model_origin": self.model_origin}
            _write(self.root / ("job-" + job_id + ".json"), job)
            self._jobs[job_id] = job
            self._active = job_id
            self._executor.submit(self._execute, job_id, function, dict(arguments))
            return {"job_id": job_id, "status": "queued"}

    def job(self, job_id):
        with self._lock:
            value = self._jobs.get(_id(job_id))
            if value is None:
                raise ValueError("Unknown job in this application workspace.")
            return _presentation(json.loads(canonical(value)))

    def _execute(self, job_id, function, arguments):
        job = dict(self._jobs[job_id])
        try:
            with self._lock:
                job.update(status="running", started_at=_now())
                _write(self.root / ("job-" + job_id + ".json"), job)
                self._jobs[job_id] = dict(job)
            result = function(**arguments)
            # All source/model paths are responsible for their own domain
            # receipts. A failed agent run is not a successful application job.
            if isinstance(result, dict) and "model_origin" in result and result.get("status") not in {"candidate", "answered", "abstained"}:
                job.update(status="failed", result=result, error={"code": "model_run_stopped",
                           "message": "The model run stopped without an accepted result. Its usage and private receipts were retained."})
            else:
                job.update(status="succeeded", result=result)
        except ValueError as error:
            job.update(status="failed", error={"code": "operation_refused", "message": str(error)[:1000]})
        except Exception:
            job.update(status="failed", error={"code": "operation_failed",
                       "message": "The operation failed. Check source readiness and local workspace state; no automatic retry was attempted."})
        finally:
            try:
                if self.workspace.db_path.is_file() and not self.workspace.db_path.is_symlink():
                    os.chmod(self.workspace.db_path, 0o600)
                private_workspace(self.workspace)
                job["finished_at"] = _now()
                _write(self.root / ("job-" + job_id + ".json"), job)
            except Exception:
                job.pop("result", None)
                job.update(status="failed", error={"code": "receipt_storage_failed",
                           "message": "The application could not retain this result. Inspect local permissions and domain receipts before another operation."})
            with self._lock:
                self._jobs[job_id] = job
                self._active = None

    def _model(self, operation, **arguments):
        if arguments.get("allow_model_data") is not True:
            raise ValueError("Explicit model-data consent is required before sending customer context or evidence.")
        if operation is agent.bootstrap and arguments.get("intent") is not None:
            agent.validate_bootstrap_intent(arguments["intent"])
        # The browser can request a small bounded run, not replace its transport.
        cost = arguments.get("max_cost_usd", 1.0)
        if type(cost) not in {int, float} or not 0 < cost <= 1:
            raise ValueError("Application model runs require a cost limit greater than zero and at most USD 1.")
        client = self.model_client_factory() if self.model_client_factory else None
        return operation(self.workspace, **arguments, client=client)

    def _sample_connect(self, profile, phase="initial"):
        from . import samples
        result = samples.connect(self.workspace, profile, phase=phase)
        _write(self.root / "sample.json", {"profile": profile, "phase": phase, "scope": "synthetic_local_sources"})
        return result

    def _refresh(self, profile_name):
        from .contracts import connections
        approved = profiles.load_profile(self.workspace, profile_name)
        names = connections(approved["candidate"]["profile"])
        receipts = [service.sync(self.workspace, source, bootstrap_context=False) for source in names]
        snapshot = snapshots.capture_snapshot(self.workspace, names)
        materialized = profiles.materialize(self.workspace, profile_name, snapshot["snapshot_id"])
        return {"snapshot_id": snapshot["snapshot_id"], "profile": profile_name,
                "materialization": materialized, "publications": receipts,
                "context": contexts.current_context(self.workspace, profile_name, snapshot["snapshot_id"])}

    def operations(self):
        from . import onboarding, samples
        def bound(function, exclude=()):
            fields = set(inspect.signature(function).parameters) - {"workspace", *exclude}
            return lambda **kwargs: function(self.workspace, **kwargs), fields
        return {
            "source.add": bound(service.add, ("replace_config",)),
            "source.sync": (lambda name, request_budget=None, fetch_scope=None: service.sync(
                self.workspace, name, bootstrap_context=False, request_budget=request_budget, fetch_scope=fetch_scope),
                {"name", "request_budget", "fetch_scope"}),
            "source.inspect": bound(onboarding.inspect_sources),
            "snapshot": bound(snapshots.capture_snapshot),
            "customer.create": bound(identity.create_customer),
            "links.propose": bound(identity.propose_links),
            "links.approve": bound(identity.approve_links),
            "profile.propose": bound(profiles.propose_profile),
            "profile.approve": bound(profiles.approve_profile),
            "profile.show": bound(profiles.load_profile),
            "profile.refresh": (self._refresh, {"profile_name"}),
            "materialize": bound(profiles.materialize),
            "context.bootstrap": (lambda **kwargs: self._model(agent.bootstrap, **kwargs),
                                  {"profile_name", "snapshot_id", "allow_model_data", "max_cost_usd", "max_turns", "intent"}),
            "context.propose": bound(contexts.propose_context, ("origin", "stages", "expected_profile_approval", "expected_materialization_id")),
            "context.approve": bound(contexts.approve_context),
            "context.show": bound(contexts.current_context),
            "brief": bound(answers.brief),
            "ask": (lambda **kwargs: self._model(agent.ask, **kwargs),
                    {"profile_name", "snapshot_id", "customer_id", "question", "allow_model_data", "max_cost_usd", "max_turns"}),
            "evidence": bound(answers.read_evidence),
            "evidence.latest": bound(answers.latest_evidence),
            "review.save": bound(reviews.save_review),
            "review.list": bound(reviews.list_reviews),
            "review.load": bound(reviews.load_review),
            "review.compare": bound(reviews.compare_review),
            "sample.connect": (self._sample_connect, {"profile", "phase"}),
            "sample.prepare": bound(samples.prepare),
        }
