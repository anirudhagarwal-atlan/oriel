"""Explicit native operations for a previously frozen acceptance study.

Construction validates local inputs only. Source access occurs only in explicit
transition, sync and verify calls; there is no model invocation, fixture seeding,
reset, automatic retry, context regeneration or implicit phase advancement.
The controller owns authorization, cost allocation, global reservations and the
requirement to finish both arms/postflight before advancing a phase.

The complete protocol is validated once, then privately copied. Every operation
checks its hash and current code/runtime identity. Binding uses the existing
acceptance binder's pure _build on that unchanged validated copy, avoiding an
extra full reconstruction of all independent fixture intents at every phase.
This is an internal adapter boundary, not a bypass for unvalidated protocols.

Operation envelopes retain their actual outer clocks. A transition also retains
its original inner executor receipt; accounting must not replace that receipt's
clocks with the wrapper clocks or add the same interval twice. Source setup
constructs configuration only; actual MCP startup remains inside agent trials.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .acceptance_binding import _build, _same_state
from .acceptance_protocol import runtime_identity, validate_protocol
from .context import load_context
from .evaluation_context import context_view
from .evaluation_tools import StdioMCPClient
from .phase_benchmark import _local_state
from .rystic import RysticPair, local_url, sync_snapshot
from .rystic_validation import capture_validation
from .study_binding import _hash, _json, _same, _validation
from .study_phases import PHASE_ORDER
from .study_transitions import apply_transition, TransitionError
from .workspace import Workspace

SPEC = "contextbase.study-operation/v1"


class RuntimeOperationError(ValueError):
    """Sanitized failure retaining original operation and partial receipts."""
    def __init__(self, receipt):
        self.receipt = deepcopy(receipt)
        super().__init__("Study operation failed; retain its receipt and stop. No automatic retry is safe.")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _endpoint(value):
    parsed = urlsplit(local_url(value))
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return "http://" + host + ":" + str(parsed.port)


def _write_new(path, value):
    """Write private evidence once; never follow a pre-existing output link."""
    raw = _json(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(raw + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class NativeStudyRuntime:
    def __init__(self, protocol, approved_context, assignments, root, *, pair_factory=None, client_factory=None):
        """Validate without creating files, starting MCP, or contacting a source.

        ``assignments`` exactly maps every batch ID to ``environment_id``,
        ``stripe_url`` and ``linear_url``. The external controller must also bind
        that exact assignment map to its reviewed executable cost contract.
        Injectable factories support offline source/configuration doubles only;
        their presence never establishes native provenance or execution consent.
        """
        validate_protocol(protocol)
        _json(approved_context); _json(assignments)
        self._protocol = deepcopy(protocol)
        self._approved = deepcopy(approved_context)
        _same(_hash(self._approved), protocol["context_sha256"], "Exact frozen approved context artifact")
        batches = [row["id"] for row in protocol["batches"]]
        if (type(assignments) is not dict or set(assignments) != set(batches)
                or len(batches) != len(set(batches))
                or any(type(batch) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", batch) for batch in batches)):
            raise ValueError("Assign exactly the declared safe batch IDs.")
        intents = {row["batch_id"]: row for row in protocol["intents"]}
        self._assignments, endpoints, environments, ports = {}, set(), set(), set()
        for batch in batches:
            assignment = assignments[batch]
            if type(assignment) is not dict or set(assignment) != {"environment_id", "stripe_url", "linear_url"}:
                raise ValueError("Each batch needs its exact provisioned environment and two source URLs.")
            environment = assignment["environment_id"]
            if type(environment) is not str or not environment.strip() or environment in environments:
                raise ValueError("Every batch requires a separate nonempty provisioned environment.")
            _same(environment, intents[batch]["provisioning"]["environment_id"], "Provisioned batch environment")
            environments.add(environment)
            normalized = {"environment_id": environment}
            for source in ("stripe", "linear"):
                endpoint = _endpoint(assignment[source + "_url"])
                port = urlsplit(endpoint).port
                if endpoint in endpoints or port in ports:
                    raise ValueError("Every assigned simulator endpoint must be separate across both sources and all batches.")
                endpoints.add(endpoint)
                ports.add(port)
                normalized[source + "_url"] = endpoint
            self._assignments[batch] = normalized
        self._root = Path(root).expanduser().resolve()
        self._workspaces = {batch: Workspace(self._root / batch / "workspace") for batch in batches}
        self._pair_factory = RysticPair if pair_factory is None else pair_factory
        self._client_factory = StdioMCPClient if client_factory is None else client_factory
        self._pairs, self._prepared, self._views = {}, set(), {}
        self._transitions, self._syncs, self._bindings = {}, {}, {}
        self._attempted, self._receipts = set(), []
        self._failed = False
        self._request_guard = None

    @property
    def receipts(self):
        return deepcopy(self._receipts)

    def workspace(self, batch_id):
        if batch_id not in self._workspaces:
            raise ValueError("Unknown assigned batch.")
        return self._workspaces[batch_id]

    def pair(self, batch_id):
        if batch_id not in self._pairs:
            raise ValueError("Configure the assigned source workflow first.")
        return self._pairs[batch_id]

    def set_request_guard(self, guard):
        """Forward an optional pre-request deadline check to source clients.

        The guard returns positive finite remaining seconds, or raises before
        dispatch. Construction and forwarding perform no source calls. Socket
        timeouts do not guarantee an in-flight operation stops at that instant.
        """
        if guard is not None and not callable(guard):
            raise ValueError("Study request guard must be callable or None.")
        for pair in self._pairs.values():
            for source in ("stripe", "linear"):
                getattr(pair, source).request_guard = guard
        self._request_guard = guard

    def _directory(self, batch):
        return self._root / batch

    def _intent(self, batch):
        return next(row for row in self._protocol["intents"] if row["batch_id"] == batch)

    def _context(self, batch):
        workspace = self.workspace(batch)
        _same(_hash(load_context(workspace)), self._protocol["context_sha256"], "Installed frozen context")
        view = context_view(workspace)
        if batch in self._views:
            _same(_hash(view), self._views[batch], "Frozen evaluation context view")
        return view

    def _guard(self, batch):
        self.workspace(batch)
        _same(self._protocol["protocol_sha256"],
              _hash({key: value for key, value in self._protocol.items() if key != "protocol_sha256"}),
              "Private frozen protocol")
        _same(runtime_identity(), self._protocol["runtime"], "Frozen code and runtime identity")
        _same(_hash(self._approved), self._protocol["context_sha256"], "Private frozen context artifact")
        if batch in self._prepared:
            self._context(batch)
        if batch in self._pairs:
            for source in ("stripe", "linear"):
                _same(_endpoint(getattr(self._pairs[batch], source).url),
                      self._assignments[batch][source + "_url"], "Assigned source endpoint")

    def _operation(self, name, batch, phase, action):
        if self._failed:
            raise ValueError("A previous operation failed; this runtime is terminal and must not resume.")
        key = batch, name, phase
        if key in self._attempted:
            raise ValueError("This operation was already attempted; automatic retries are forbidden.")
        self._attempted.add(key)
        started = time.perf_counter()
        receipt = {"spec": SPEC, "operation": name, "batch_id": batch, "phase_id": phase,
                   "protocol_sha256": self._protocol["protocol_sha256"], "started_at": _now(),
                   "finished_at": None, "elapsed_seconds": None, "outcome": "in_progress",
                   "result": None, "model_calls": 0, "claim_eligible": False}
        payload = {}
        error = None
        try:
            self._guard(batch)
            action(payload)
            self._guard(batch)
            receipt["outcome"] = "completed"
        except Exception as exc:
            error = exc
            self._failed = True
            receipt.update(outcome="failed", error_type=type(exc).__name__)
            if isinstance(exc, TransitionError):
                payload["transition"] = deepcopy(exc.receipt)
        if name == "transition":
            receipt["result"] = payload.get("transition")
            if "precondition_capture" in payload:
                receipt["observations"] = {"before_transition": payload["precondition_capture"]}
        elif name == "bind":
            receipt["result"] = payload.get("binding")
        elif name.startswith("verify_"):
            receipt["result"] = payload.get("capture")
        else:
            receipt["result"] = payload
        receipt.update(finished_at=_now(), elapsed_seconds=time.perf_counter() - started)
        receipt["receipt_sha256"] = _hash(receipt)
        self._receipts.append(deepcopy(receipt))
        if error is not None:
            raise RuntimeOperationError(receipt) from None
        return deepcopy(receipt)

    def setup_source(self, batch_id):
        """Construct client/configuration only; actual MCP startup is in trials."""
        def configure(receipt):
            assignment = self._assignments[batch_id]
            pair = self._pair_factory(assignment["stripe_url"], assignment["linear_url"])
            for source in ("stripe", "linear"):
                getattr(pair, source).request_guard = self._request_guard
                _same(_endpoint(getattr(pair, source).url), assignment[source + "_url"], "Constructed source endpoint")
            self._pairs[batch_id] = pair
            client = self._client_factory(self.workspace(batch_id), arm="rystic_source",
                stripe_url=assignment["stripe_url"], linear_url=assignment["linear_url"],
                evaluation_context=True, verification_policy="evaluation_session")
            receipt.update(environment_id=assignment["environment_id"],
                           source_endpoints={source: assignment[source + "_url"] for source in ("stripe", "linear")},
                           mcp_command=deepcopy(client.command), mcp_started=False)
        return self._operation("setup_source", batch_id, "baseline", configure)

    def prepare_batch(self, batch_id):
        """Install the unchanged approved artifact into a fresh empty workspace."""
        def prepare(receipt):
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = self._directory(batch_id)
            directory.mkdir(mode=0o700)  # Refuse any prior workspace or evidence.
            workspace = self.workspace(batch_id)
            workspace.root.mkdir(mode=0o700)
            (workspace.root / "context").mkdir(mode=0o700)
            _write_new(workspace.root / "context" / "approved.json", self._approved)
            view = self._context(batch_id)
            self._views[batch_id] = _hash(view)
            self._prepared.add(batch_id)
            receipt.update(workspace_root=str(workspace.root), context_sha256=_hash(load_context(workspace)),
                           evaluation_context_sha256=self._views[batch_id], database_created=False,
                           context_regenerated=False)
        return self._operation("prepare_batch", batch_id, "baseline", prepare)

    def _require_phase(self, batch, phase):
        if batch not in self._prepared or batch not in self._pairs or phase not in PHASE_ORDER:
            raise ValueError("Prepare the fresh workspace and source workflow, then select a declared phase.")
        index = PHASE_ORDER.index(phase)
        if index and (batch, PHASE_ORDER[index - 1]) not in self._bindings:
            raise ValueError("A bound adjacent preceding phase is required.")
        return index

    def transition(self, batch_id, to_phase, *, approve_plan, approve_action):
        """Apply one explicit adjacent frozen action, preserving failed receipts."""
        def advance(receipt):
            index = self._require_phase(batch_id, to_phase)
            if index == 0:
                raise ValueError("Baseline has no transition.")
            plan = self._intent(batch_id)["phase_plan"]
            declared = next(row for row in plan["transitions"] if row["to_phase"] == to_phase)
            _same(approve_plan, plan["plan_sha256"], "Approved transition plan")
            _same(approve_action, declared["action_sha256"], "Approved transition action")
            previous = self._bindings[(batch_id, PHASE_ORDER[index - 1])]
            _local_state(self.workspace(batch_id), previous["capture"])
            before_phase = next(row for row in plan["phases"] if row["id"] == PHASE_ORDER[index - 1])
            observed = capture_validation(self.pair(batch_id), before_phase["intent_manifest"]["namespace"])
            receipt["precondition_capture"] = deepcopy(observed)
            _write_new(self._directory(batch_id) / (to_phase + "-pre-transition-capture.json"), observed)
            _validation(observed, before_phase["intent_manifest"], plan["source_issue_account_ids"])
            _same_state(observed, previous["capture"], "Full source state before any phase write")
            value = apply_transition(self.pair(batch_id), plan, PHASE_ORDER[index - 1], to_phase,
                self._directory(batch_id) / (to_phase + "-transition.json"),
                approve_plan=approve_plan, approve_action=approve_action)
            receipt["transition"] = value
            self._transitions[(batch_id, to_phase)] = deepcopy(value)
        return self._operation("transition", batch_id, to_phase, advance)

    def sync(self, batch_id, phase_id):
        """Save the actual first capture and import, or retain expected refusal."""
        def synchronize(receipt):
            index = self._require_phase(batch_id, phase_id)
            if index and (batch_id, phase_id) not in self._transitions:
                raise ValueError("Apply the adjacent frozen transition before syncing its phase.")
            plan = self._intent(batch_id)["phase_plan"]
            phase = next(row for row in plan["phases"] if row["id"] == phase_id)
            path = self._directory(batch_id) / (phase_id + "-capture.json")
            failure = None
            try:
                sync_snapshot(self.workspace(batch_id), self.pair(batch_id), phase["intent_manifest"]["namespace"], capture_output=path)
            except Exception as exc:
                failure = exc
            if not path.exists() or not path.stat().st_size:
                raise ValueError("Sync did not retain a complete validation capture.") from None
            capture = json.loads(path.read_text())
            receipt["capture"] = deepcopy(capture)
            validation, tables, _, _ = _validation(capture, phase["intent_manifest"], plan["source_issue_account_ids"])
            status, snapshot = _local_state(self.workspace(batch_id), capture)
            expected_refusal = phase_id in {"guard_a", "guard_b"}
            if expected_refusal:
                if type(failure) is not ValueError or tables is not None or validation["validation_status"] != "blocked" or snapshot is not None:
                    raise ValueError("Guarded phase must retain its exact blocked capture and refused workspace.")
            elif failure is not None or tables is None or snapshot is None:
                raise ValueError("Valid phase must retain its first successful materialization.") from None
            value = {"capture": capture, "snapshot": snapshot, "refresh_status": status,
                     "readiness": "refused_invalid_source" if expected_refusal else "materialized"}
            receipt.update(deepcopy(value))
            self._syncs[(batch_id, phase_id)] = deepcopy(value)
        return self._operation("sync", batch_id, phase_id, synchronize)

    def bind(self, batch_id, phase_id):
        """Bind the saved original capture and actual current local readiness."""
        def bind(receipt):
            index = self._require_phase(batch_id, phase_id)
            value = self._syncs.get((batch_id, phase_id))
            if value is None:
                raise ValueError("Sync this phase once and retain its complete evidence before binding.")
            status, snapshot = _local_state(self.workspace(batch_id), value["capture"])
            _same(status, value["refresh_status"], "Saved sync readiness")
            _same(snapshot, value["snapshot"], "Saved sync snapshot")
            binding = _build(self._protocol, batch_id, phase_id, deepcopy(value["capture"]), snapshot=snapshot,
                previous_binding=self._bindings.get((batch_id, PHASE_ORDER[index - 1])) if index else None,
                transition=self._transitions.get((batch_id, phase_id)), bound_at=_now())
            _write_new(self._directory(batch_id) / (phase_id + "-binding.json"), binding)
            self._bindings[(batch_id, phase_id)] = deepcopy(binding)
            receipt["binding"] = binding
        return self._operation("bind", batch_id, phase_id, bind)

    def verify(self, batch_id, binding, stage):
        """Check exact policy, local readiness and the full live source projection."""
        if stage not in {"preflight", "postflight"}:
            raise ValueError("Verification stage must be preflight or postflight.")
        phase_id = binding.get("phase_id") if type(binding) is dict else None
        def verify(receipt):
            self._require_phase(batch_id, phase_id)
            original = self._bindings.get((batch_id, phase_id))
            _same(binding, original, "Original runtime phase binding")
            _local_state(self.workspace(batch_id), original["capture"])
            plan = self._intent(batch_id)["phase_plan"]
            phase = next(row for row in plan["phases"] if row["id"] == phase_id)
            observed = capture_validation(self.pair(batch_id), original["capture"]["receipt"]["namespace"])
            receipt["capture"] = deepcopy(observed)
            _write_new(self._directory(batch_id) / (phase_id + "-" + stage + "-capture.json"), observed)
            _validation(observed, phase["intent_manifest"], plan["source_issue_account_ids"])
            _same_state(observed, original["capture"], "Current full bound source state")
            status, snapshot = _local_state(self.workspace(batch_id), original["capture"])
            value = self._syncs[(batch_id, phase_id)]
            _same(status, value["refresh_status"], "Unchanged phase readiness")
            _same(snapshot, value["snapshot"], "Unchanged phase snapshot")
            receipt.update(binding_sha256=original["binding_sha256"], context_sha256=self._protocol["context_sha256"],
                           evaluation_context_sha256=_hash(self._context(batch_id)), stage=stage,
                           source_state_matches=True, local_state_matches=True)
        return self._operation("verify_" + stage, batch_id, phase_id, verify)
