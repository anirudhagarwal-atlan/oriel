"""Sequential, budgeted acceptance execution with durable partial evidence.

The normal entry point uses pre-provisioned native local sources and the
configured model only after exact protocol and data-flow authorization. Tests
inject source/model doubles; those receipts remain explicitly synthetic. This
controller never provisions, resets or automatically retries a source fixture.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import time

from .acceptance_protocol import validate_protocol
from .acceptance_report import (_plan, SPEC as COLLECTION_SPEC, build_report,
    store_collection, verification_evidence, require_billing_reconciliation)
from .evidence_store import EvidenceStore
from .model import ClaudeCLI
from .rystic_benchmark import _run_comparison, _write
from .study_binding import _hash, _same
from .study_budget import StudyBudget, BudgetedModel, BudgetError
from .study_costs import validate_cost_contract, quote, maximum_reservation
from .study_runtime import NativeStudyRuntime, RuntimeOperationError
from .study_transitions import _checkpoint

SPEC = "contextbase.study-controller/v1"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _span(start, finish):
    elapsed = (_stamp(finish) - _stamp(start)).total_seconds()
    if elapsed < 0:
        raise ValueError("Controller clock moved backwards.")
    return {"started_at": start, "finished_at": finish, "elapsed_seconds": elapsed}


def _allocation(contract, kind, arm=None):
    mode = contract["rules"][kind]["allocation"]
    if isinstance(mode, dict):
        return deepcopy(mode)
    if mode == "common_equal":
        return {"source": .5, "workspace": .5}
    owner = arm if mode == "incurring_arm" else mode
    if owner not in {"source", "workspace"}:
        raise ValueError("Specify the actual incurring arm.")
    return {"source": int(owner == "source"), "workspace": int(owner == "workspace")}


class _Collector:
    def __init__(self, protocol, contract, runtime, budget, store, root):
        self.protocol, self.contract, self.runtime, self.budget = protocol, contract, runtime, budget
        self.store, self.root = store, root
        self.collection = {"spec": COLLECTION_SPEC, "protocol_sha256": protocol["protocol_sha256"],
                           "phases": [], "events": deepcopy(protocol["historical_context_events"]), "first_use": []}
        self.completed_keys, self.operation_refs = set(), []
        self.cold_starts = {}
        self.settlement_failures = []

    def retain(self, receipt):
        digest = self.store.put(receipt)
        self.operation_refs.append(digest)
        return digest

    def settle(self, reservation_id, *, actual_cost_usd, outcome="known"):
        try:
            self.budget.settle(reservation_id, actual_cost_usd=actual_cost_usd, outcome=outcome)
        except BudgetError as exc:
            if exc.reason != "receipt_storage_failed":
                raise
            # The journal may be unwritable after a paid action. Preserve the
            # charge separately when the evidence store still works; never
            # pretend the durable reservation was settled or authorize more work.
            failure = {"kind": "unpublished_budget_settlement", "reservation_id": reservation_id,
                       "actual_cost_usd": actual_cost_usd, "outcome": outcome,
                       "reason": exc.reason, "observed_at": _now()}
            self.settlement_failures.append(self.retain(failure))

    def stop(self, reason):
        try:
            self.budget.stop(reason)
        except BudgetError as exc:
            if exc.reason != "receipt_storage_failed":
                raise

    def event(self, kind, batch, phase, interval, evidence, *, arm=None, cost=None, role="exclusive_overhead"):
        event = {"event_id": "controller-event-" + str(len(self.collection["events"])),
                 "kind": kind, "batch_id": batch, "phase_id": phase,
                 **{key: interval[key] for key in ("started_at", "finished_at", "elapsed_seconds")},
                 "cost_usd": cost, "allocation": _allocation(self.contract, kind, arm),
                 "evidence_sha256": evidence, "duration_role": role,
                 "cost_role": "incremental", "attempt_id": None}
        self.collection["events"].append(event)
        return event

    def evaluate(self, batch, phase, interval, evidence):
        # All evaluator work in one phase is one priced bundle. Its actual
        # non-overlapping spans receive proportional shares of that bill.
        return self.event("evaluator_validation", batch, phase, interval, evidence)

    def _settle_operation(self, reservation, kind, interval, *, outcome="known"):
        cost = quote(self.contract, kind, interval["elapsed_seconds"])
        self.settle(reservation["reservation_id"], actual_cost_usd=cost, outcome=outcome)
        if interval["elapsed_seconds"] > self.contract["rules"][kind]["max_seconds"]:
            self.stop("operation_duration_exceeded")
        return cost

    def reserve_operation(self, kind, batch, phase):
        reservation = self.budget.reserve(kind, max_cost_usd=maximum_reservation(self.contract, kind),
                                          batch_id=batch, phase_id=phase)
        try:
            self.budget.check_dispatch(reservation["reservation_id"])
        except BudgetError:
            # The action has not been entered; no operation charge occurred.
            self.settle(reservation["reservation_id"], actual_cost_usd=0.)
            raise
        return reservation

    @contextmanager
    def source_scope(self, reservation, kind, remaining_seconds=None):
        started = time.monotonic()
        def guard():
            self.budget.check_dispatch(reservation["reservation_id"])
            remaining = min(self.budget.remaining_seconds,
                self.contract["rules"][kind]["max_seconds"] - (time.monotonic() - started))
            if remaining_seconds is not None:
                remaining = min(remaining, remaining_seconds())
            if remaining <= 0:
                self.stop("operation_duration_exceeded")
                raise BudgetError("operation_duration_exceeded")
            return remaining
        self.runtime.set_request_guard(guard)
        try:
            yield
        finally:
            self.runtime.set_request_guard(None)

    def operation(self, kind, batch, phase, action, *, arm=None):
        reservation = self.reserve_operation(kind, batch, phase)
        try:
            with self.source_scope(reservation, kind):
                receipt = action()
        except RuntimeOperationError as exc:
            receipt = exc.receipt
            self.retain(receipt)
            cost = self._settle_operation(reservation, kind, receipt)
            self.event(kind, batch, phase, receipt, receipt["receipt_sha256"], arm=arm, cost=cost)
            self.stop("runtime_operation_failed")
            raise
        except Exception:
            # No trustworthy measured result: do not assume the reservation
            # was unused or repeat a possibly applied mutation.
            self.settle(reservation["reservation_id"], actual_cost_usd=None, outcome="unknown")
            raise
        self.retain(receipt)
        cost = self._settle_operation(reservation, kind, receipt)
        evidence = receipt["receipt_sha256"]
        if kind in {"workspace_ingestion", "refresh"}:
            evidence = _hash(receipt["result"]["capture"])
        self.event(kind, batch, phase, receipt, evidence, arm=arm, cost=cost)
        if self.budget.status()["blocked_reasons"]:
            raise BudgetError("operation_stopped_budget")
        return receipt

    def transition(self, batch, phase, declaration, plan):
        reservation = self.reserve_operation("phase_transitions", batch, phase)
        failure = None
        try:
            with self.source_scope(reservation, "phase_transitions"):
                receipt = self.runtime.transition(batch, phase, approve_plan=plan["plan_sha256"],
                                                  approve_action=declaration["action_sha256"])
        except RuntimeOperationError as exc:
            receipt, failure = exc.receipt, exc
        except Exception:
            self.settle(reservation["reservation_id"], actual_cost_usd=None, outcome="unknown")
            raise
        self.retain(receipt)
        inner = receipt["result"]
        if inner is None:
            # No writes were reached. The failed precondition is still actual
            # evaluator work; charge the declared attempted transition once.
            cost = self._settle_operation(reservation, "phase_transitions", receipt)
            self.event("phase_transitions", batch, phase, receipt, receipt["receipt_sha256"], cost=cost)
        else:
            outcome = "unknown" if inner.get("outcome") in {"write_uncertain", "receipt_storage_failed"} else "known"
            cost = self._settle_operation(reservation, "phase_transitions", inner, outcome=outcome)
            self.event("phase_transitions", batch, phase, inner, inner["receipt_sha256"], cost=cost)
            for label, start, end in (("before", receipt["started_at"], inner["started_at"]),
                                      ("after", inner["finished_at"], receipt["finished_at"])):
                interval = _span(start, end)
                self.evaluate(batch, phase, interval, _hash({"operation": receipt["receipt_sha256"], "part": label}))
        if failure is not None:
            self.stop("source_transition_failed")
            raise failure
        if self.budget.status()["blocked_reasons"]:
            raise BudgetError("transition_stopped_budget")

    def first_use(self, batch, execution):
        for arm, native in (("source", "rystic_source"), ("workspace", "sql_context")):
            trial = next(t for t in execution["trials"] if t["arm"] == native)
            if trial["started_at"] is None:
                continue
            receipt = {"batch_id": batch, "arm": arm, "task_id": trial["task"], "repeat": trial["repeat"],
                       "cold_started_at": self.cold_starts[(batch, arm)], "finished_at": trial["finished_at"]}
            receipt["evidence_sha256"] = self.retain({"kind": "observed_first_use", "trial_id": trial["trial_id"], **receipt})
            self.collection["first_use"].append(receipt)

    def remaining_evaluator_seconds(self, batch, phase, started, execution=None, trial=None):
        """Measure overhead so far, excluding actual query time and other events."""
        finish = _now()
        intervals = [(event["started_at"], event["finished_at"]) for event in self.collection["events"]
                     if event["batch_id"] == batch and event["phase_id"] == phase
                     and event["duration_role"] == "exclusive_overhead" and event["kind"] != "evaluator_validation"]
        trials = ([] if execution is None else execution["trials"]) + ([] if trial is None else [trial])
        intervals.extend((item["started_at"], item["finished_at"] or finish)
                         for item in trials if item["started_at"] is not None)
        cursor, occupied = _stamp(started), 0.
        for begin, end in sorted(intervals, key=lambda item: _stamp(item[0])):
            begin, end = max(_stamp(begin), cursor), min(_stamp(end), _stamp(finish))
            if end > begin:
                occupied += (end - begin).total_seconds()
                cursor = end
        seconds = _span(started, finish)["elapsed_seconds"] - occupied
        return self.contract["rules"]["evaluator_validation"]["max_seconds"] - seconds

    def check_evaluator_budget(self, batch, phase, started, execution=None, trial=None):
        if self.remaining_evaluator_seconds(batch, phase, started, execution, trial) <= 0:
            self.stop("evaluator_duration_exceeded")
            raise BudgetError("evaluator_duration_exceeded")

    def close_phase(self, batch, phase, started, finished, reservation, execution):
        events = [event for event in self.collection["events"] if event["batch_id"] == batch
                  and event["phase_id"] == phase and event["duration_role"] == "exclusive_overhead"]
        occupied = [(event["started_at"], event["finished_at"]) for event in events]
        if execution is not None:
            occupied.extend((trial["started_at"], trial["finished_at"]) for trial in execution["trials"]
                            if trial["started_at"] is not None)
        cursor = started
        for begin, end in sorted(occupied, key=lambda span: _stamp(span[0])):
            if _stamp(begin) > _stamp(cursor):
                interval = _span(cursor, begin)
                self.evaluate(batch, phase, interval, _hash({"kind": "controller_bookkeeping", "batch": batch,
                    "phase": phase, **interval, "protocol_sha256": self.protocol["protocol_sha256"]}))
            if _stamp(end) > _stamp(cursor):
                cursor = end
        if _stamp(finished) > _stamp(cursor):
            interval = _span(cursor, finished)
            self.evaluate(batch, phase, interval, _hash({"kind": "controller_bookkeeping", "batch": batch,
                "phase": phase, **interval, "protocol_sha256": self.protocol["protocol_sha256"]}))
        evaluation = [event for event in self.collection["events"] if event["batch_id"] == batch
                      and event["phase_id"] == phase and event["kind"] == "evaluator_validation"]
        seconds = sum(event["elapsed_seconds"] for event in evaluation)
        amount = quote(self.contract, "evaluator_validation", seconds)
        for index, event in enumerate(evaluation):
            event["cost_usd"] = amount * event["elapsed_seconds"] / seconds if seconds else (amount if index == 0 else 0.)
        self.settle(reservation["reservation_id"], actual_cost_usd=amount)
        if seconds > self.contract["rules"]["evaluator_validation"]["max_seconds"]:
            self.stop("evaluator_duration_exceeded")

    def checkpoint(self, outcome, reason=None):
        index = store_collection(self.store, self.protocol, self.collection)
        index_ref = self.store.put(index)
        state = {"spec": SPEC, "protocol_sha256": self.protocol["protocol_sha256"], "outcome": outcome,
                 "reason": reason, "collection_index_ref": index_ref, "operation_refs": self.operation_refs,
                 "unpublished_settlement_refs": self.settlement_failures,
                 "budget": self.budget.status(), "recorded_phases": len(self.collection["phases"]),
                 "completed_phases": sum(item["execution"]["execution_complete"] for item in self.collection["phases"]),
                 "unexecuted_schedule": [row for row in self.protocol["planned"] if
                     (row["batch_id"], row["task_id"], row["repeat"], row["arm"]) not in self.completed_keys],
                 "claim_eligible": False, "automatic_resume_allowed": False}
        _checkpoint(self.root / "controller.json", state)
        return state, index


def run_study(protocol, cost_contract, approved_context, output, *, approve,
              allow_model_data=False, allow_source_transitions=False, client=None,
              pair_factory=None, tools_factory=None):
    """Run the unchanged full schedule, or retain a terminal partial collection.

    ``tools_factory`` is a test hook called with (workspace, pair, arm). Any
    injection requires an explicit test model; native model execution uses the
    unmodified shared stdio MCP factory. Output must be a fresh directory.
    """
    if approve != protocol.get("protocol_sha256") or not allow_model_data or not allow_source_transitions:
        raise ValueError("Approve the exact protocol and explicitly allow its synthetic model flow and local fixture transitions.")
    validate_protocol(protocol)
    contract = validate_cost_contract(protocol, cost_contract)
    _same(_hash(approved_context), protocol["context_sha256"], "Frozen context artifact")
    if client is None and (protocol["evidence_origin"] != "recorded_native" or pair_factory is not None or tools_factory is not None):
        raise ValueError("Configured-model studies require declared native fixtures and the original native source/MCP adapters.")
    if {event["kind"] for event in protocol["historical_context_events"]} != {"context_creation", "context_review"}:
        raise ValueError("Freeze actual context creation and review accounting before a study.")
    root = Path(output).expanduser().resolve()
    runtime = NativeStudyRuntime(protocol, approved_context, contract["assignments"], root / "workspaces", pair_factory=pair_factory)
    root.mkdir(parents=True, mode=0o700)  # Never overwrite or resume a prior run.
    _write(root / "controller.json", {"spec": SPEC, "outcome": "starting"}, exclusive=True)
    store = EvidenceStore(root / "receipts")
    model = client
    controller_started = _now()
    with StudyBudget.create(root / "budget", protocol) as budget:
        collector = _Collector(protocol, contract, runtime, budget, store, root)
        license_reservation = None
        stopped = None
        last_end = controller_started
        try:
            license_reservation = budget.reserve("runtime_license", max_cost_usd=maximum_reservation(contract, "runtime_license"))
            if model is None:
                model = ClaudeCLI(timeout_seconds=min(90, protocol["budgets"]["run_seconds"]))
            intent_map = {item["batch_id"]: item for item in protocol["intents"]}
            for declared_batch in protocol["batches"]:
                batch = declared_batch["id"]
                prepared = intent_map[batch]["phase_plan"]
                for phase in protocol["recurring_profile"]["phase_order"]:
                    evaluation_reservation = budget.reserve("evaluator_validation",
                        max_cost_usd=maximum_reservation(contract, "evaluator_validation"), batch_id=batch, phase_id=phase)
                    execution = None
                    try:
                        if phase == "baseline":
                            source = collector.operation("workflow_configuration", batch, phase,
                                lambda: runtime.setup_source(batch), arm="source")
                            collector.cold_starts[(batch, "source")] = source["started_at"]
                            workspace = collector.operation("workflow_configuration", batch, phase,
                                lambda: runtime.prepare_batch(batch), arm="workspace")
                            collector.cold_starts[(batch, "workspace")] = workspace["started_at"]
                        else:
                            declaration = next(item for item in prepared["transitions"] if item["to_phase"] == phase)
                            collector.transition(batch, phase, declaration, prepared)
                        collector.operation("workspace_ingestion" if phase == "baseline" else "refresh", batch, phase,
                                            lambda: runtime.sync(batch, phase), arm="workspace")
                        budget.check_dispatch(evaluation_reservation["reservation_id"])
                        try:
                            bound = runtime.bind(batch, phase)
                        except RuntimeOperationError as exc:
                            collector.retain(exc.receipt)
                            collector.evaluate(batch, phase, exc.receipt, exc.receipt["receipt_sha256"])
                            raise
                        collector.retain(bound)
                        collector.evaluate(batch, phase, bound, bound["receipt_sha256"])
                        collector.check_evaluator_budget(batch, phase, last_end)
                        binding = bound["result"]
                        status = budget.status()
                        available = min(protocol["budgets"]["comparison_usd"] - status["known_comparison_cost_usd"] - status["reserved_comparison_usd"],
                                        protocol["budgets"]["total_study_usd"] - status["known_total_cost_usd"] - status["reserved_total_usd"])
                        calls = protocol["budgets"]["max_model_calls"] - status["model_calls"]
                        if available < .01 or calls <= 0 or budget.remaining_seconds <= 0 or status["blocked_reasons"]:
                            raise BudgetError("no_remaining_model_budget")
                        plan = _plan(protocol, binding, {"max_turns": protocol["budgets"]["max_model_turns_per_run"],
                            "max_calls": calls, "max_cost_usd": available, "max_wall_seconds": budget.remaining_seconds,
                            "trial_seconds": protocol["budgets"]["run_seconds"]})
                        verification, execution_progress = [], {}
                        def verify():
                            stage = "preflight" if not verification else "postflight"
                            try:
                                budget.check_dispatch(evaluation_reservation["reservation_id"])
                                with collector.source_scope(evaluation_reservation, "evaluator_validation",
                                        lambda: collector.remaining_evaluator_seconds(batch, phase, last_end,
                                            execution_progress.get("report"))):
                                    receipt = runtime.verify(batch, binding, stage)
                            except BudgetError:
                                # The engine retains this verification refusal's
                                # actual interval, without inventing a capture.
                                verification.append((stage, None))
                                raise
                            except RuntimeOperationError as exc:
                                verification.append((stage, exc.receipt))
                                collector.retain(exc.receipt)
                                raise
                            verification.append((stage, receipt))
                            collector.retain(receipt)
                        factory = None if tools_factory is None else lambda arm: tools_factory(runtime.workspace(batch), runtime.pair(batch), arm)
                        def guard_dispatch(report, trial):
                            execution_progress["report"] = report
                            budget.check_dispatch(evaluation_reservation["reservation_id"])
                            collector.check_evaluator_budget(batch, phase, last_end, report, trial)
                        execution = _run_comparison(runtime.workspace(batch), runtime.pair(batch), plan,
                            root / (batch + "-" + phase + "-execution.json"), verify=verify,
                            client=BudgetedModel(model, budget, batch_id=batch, phase_id=phase), tools_factory=factory,
                            kind="rystic_acceptance_phase", expected_model_id=protocol["model_id"],
                            model_backend="configured_claude_cli" if client is None else "injected_test_client",
                            dispatch_guard=guard_dispatch)
                        for stage, receipt in verification:
                            measured = verification_evidence(execution, stage)
                            collector.evaluate(batch, phase, measured, _hash(measured))
                        collector.collection["phases"].append({"binding": binding, "execution": execution})
                        for trial in execution["trials"]:
                            if trial["started_at"] is not None:
                                arm = "source" if trial["arm"] == "rystic_source" else "workspace"
                                collector.completed_keys.add((batch, trial["task"], trial["repeat"], arm))
                        if phase == "baseline":
                            collector.first_use(batch, execution)
                        if not execution["execution_complete"]:
                            raise BudgetError("phase_execution_incomplete")
                    finally:
                        end = _now()
                        collector.close_phase(batch, phase, last_end, end, evaluation_reservation, execution)
                        last_end = end
                        collector.checkpoint("running")
                    if budget.status()["blocked_reasons"]:
                        raise BudgetError("phase_stopped_budget")
                batch_trials = [trial for packet in collector.collection["phases"] if packet["binding"]["batch_id"] == batch
                                for trial in packet["execution"]["trials"] if trial["started_at"] is not None]
                for arm, native in (("source", "rystic_source"), ("workspace", "sql_context")):
                    selected = [trial for trial in batch_trials if trial["arm"] == native]
                    interval = _span(selected[0]["started_at"], selected[-1]["finished_at"])
                    evidence = collector.retain({"kind": "explicit_zero_tool_fees", "batch_id": batch, "arm": arm,
                        "cost_contract_sha256": _hash(contract), "billing_rule": contract["rules"]["source_tool_calls"], **interval})
                    collector.event("source_tool_calls", batch, None, interval, evidence, arm=arm, cost=0., role="cost_only")
        except Exception as exc:
            stopped = exc.reason if isinstance(exc, BudgetError) else type(exc).__name__
            collector.stop("controller_stopped")
        finally:
            if license_reservation is not None:
                interval = _span(controller_started, _now())
                price = quote(contract, "runtime_license", interval["elapsed_seconds"])
                collector.settle(license_reservation["reservation_id"], actual_cost_usd=price)
                if interval["elapsed_seconds"] > contract["rules"]["runtime_license"]["max_seconds"]:
                    collector.stop("runtime_duration_exceeded")
                evidence = collector.retain({"kind": "measured_runtime_allocation", "cost_contract_sha256": _hash(contract),
                    "billing_rule": contract["rules"]["runtime_license"], "cost_usd": price, **interval})
                collector.event("runtime_license", None, None, interval, evidence, cost=price, role="cost_only")
        completed = not stopped and not budget.status()["blocked_reasons"] and not budget.status()["pending"]
        state, index = collector.checkpoint("completed" if completed else "stopped", stopped)
        _write(root / "collection-index.json", index, exclusive=True)
        report = build_report(protocol, collector.collection)
        report["controller_evidence"] = {"outcome": state["outcome"], "budget_receipt_sha256": state["budget"]["last_receipt_sha256"],
                                         "budget_journal_known_total_usd": state["budget"]["known_total_cost_usd"],
                                         "budget_journal_known_comparison_usd": state["budget"]["known_comparison_cost_usd"],
                                         "budget_journal_complete": "receipt_storage_failed" not in state["budget"]["blocked_reasons"],
                                         "model_call_reservations": state["budget"]["model_calls"], "unexecuted_trials": len(state["unexecuted_schedule"]),
                                         "unpublished_settlement_refs": state["unpublished_settlement_refs"],
                                         "full_billing_reconciliation_verified": False,
                                         "excluded_analysis_overhead": ["Offline protocol/input validation and runtime construction before the budget opens",
                                             "Final statistical reporting and artifact export after the execution interval"]}
        report["limitations"].append("Controller timing excludes offline input validation and final statistical reporting/export. "
            "These are unmeasured analysis overhead, not zero-cost observations; complete billing reconciliation remains required.")
        require_billing_reconciliation(report)
        if not completed:
            report.update(statistical_acceptance="inconclusive", charter_ready=False, claim_eligible=False)
            report["reasons"].append("Controller stopped; preserve partial evidence and do not automatically resume")
        _write(root / "report.json", report, exclusive=True)
        return {"output": str(root), "outcome": state["outcome"], "completed_phases": state["completed_phases"],
                "recorded_phases": state["recorded_phases"],
                "unexecuted_trials": len(state["unexecuted_schedule"]), "budget": state["budget"],
                "statistical_acceptance": report["statistical_acceptance"], "reasons": report["reasons"],
                "claim_eligible": False, "automatic_resume_allowed": False}
