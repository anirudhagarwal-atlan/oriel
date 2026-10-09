"""Private, cumulative reservation receipts for one frozen acceptance study.

This is a spending guard, not authorization to run a study. The controller must
already have permission and must supply conservative, reviewed cost ceilings.
Receipts preserve known charges; unknown outcomes and interrupted outstanding
reservations stop further work. They do not authenticate provider invoices.

A budget is an exclusive directory. One process holds its writer lock for the
session. Checkpoints are immutable, chained, strict JSON files, published only
after fsync; a reopened session may inspect an interrupted reservation but may
not reconcile it or silently resume. A long-lived runtime reservation may
coexist with individual source and model reservations in its owning session.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .acceptance_protocol import EVENT_RULES, validate_protocol
from .model import finite_number, reported_cost, strict_json_loads

SPEC = "contextbase.study-budget/v1"
_MAX_RECEIPT = 65536
_FILE = re.compile(r"[0-9]{12}\.json")
_LIMITS = ("comparison_usd", "total_study_usd", "max_model_calls", "study_seconds")


class BudgetError(ValueError):
    """A sanitized guard refusal; no provider action is authorized by it."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("Study budget refused: " + reason)


class NoDispatch(BudgetError):
    """The model wrapper refused before entering its underlying client call."""


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BudgetError("invalid_timestamp") from exc


def _money(value):
    if not finite_number(value):
        raise BudgetError("invalid_cost")
    return Decimal(str(value))


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise BudgetError("invalid_receipt")


def _label(value):
    if value is not None and (type(value) is not str or not value.strip() or len(value) > 200):
        raise BudgetError("invalid_operation_identity")
    return value


def _header(protocol):
    validate_protocol(protocol)
    limits = {key: protocol["budgets"][key] for key in _LIMITS}
    for key in ("comparison_usd", "total_study_usd", "study_seconds"):
        if not finite_number(limits[key], positive=True):
            raise BudgetError("invalid_limits")
    if type(limits["max_model_calls"]) is not int or limits["max_model_calls"] < 1:
        raise BudgetError("invalid_limits")
    frozen = _timestamp(protocol["frozen_at"])
    historical, unknown = Decimal(0), []
    for event in protocol["historical_context_events"]:
        if event["cost_usd"] is None:
            unknown.append(event["event_id"])
        else:
            historical += _money(event["cost_usd"])
    return {"protocol_sha256": protocol["protocol_sha256"], "protocol_artifact_sha256": _hash(protocol),
            "limits": limits, "started_at": protocol["frozen_at"],
            "deadline": (frozen + timedelta(seconds=limits["study_seconds"])).isoformat(),
            "provisioning_spend_usd": float(_money(protocol["provisioning_spend_usd"])),
            "historical_context_spend_usd": float(historical),
            "historical_context_events_sha256": protocol["historical_context_events_sha256"],
            "historical_unknown_event_ids": unknown}


class StudyBudget:
    """Create/open a locked directory; use as a context manager or call close.

    ``reserve`` must finish before the paid action. ``settle`` accepts a known
    complete cost or an unknown outcome with an optional known partial cost.
    Reservations count model attempts immediately, including failed calls.
    Reopening never resets the frozen deadline, charges, or attempted calls.
    """

    @classmethod
    def create(cls, path, protocol):
        return cls(path, protocol, create=True)

    @classmethod
    def open(cls, path, protocol):
        return cls(path, protocol, create=False)

    def __init__(self, path, protocol, *, create):
        expected = _header(protocol)
        self.path = Path(path).absolute()
        self._session = secrets.token_hex(16)
        self._pid = os.getpid()
        self._dir = self._lock = None
        self._poison = None
        self._records = []
        self._header = expected
        self._state = None
        self._opened_wall = _now()
        self._opened_mono = time.monotonic()
        try:
            if create:
                os.mkdir(self.path, 0o700)
            self._dir = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self._dir)
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise BudgetError("unsafe_directory_permissions")
            self._identity = (info.st_dev, info.st_ino)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            self._lock = os.open(".lock", flags, 0o600, dir_fd=self._dir)
            self._regular(self._lock)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BudgetError("writer_active") from exc
            if create:
                self._append("created", expected)
            else:
                self._load(expected)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for name in ("_lock", "_dir"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    @staticmethod
    def _regular(descriptor):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise BudgetError("unsafe_receipt_file")
        if info.st_size > _MAX_RECEIPT:
            raise BudgetError("receipt_too_large")

    def _check_open(self):
        if self._dir is None or self._lock is None or os.getpid() != self._pid:
            raise BudgetError("inactive_session")
        info = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._identity:
            raise BudgetError("directory_changed")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BudgetError("unsafe_directory_permissions")
        lock = os.stat(".lock", dir_fd=self._dir, follow_symlinks=False)
        held = os.fstat(self._lock)
        if (lock.st_dev, lock.st_ino) != (held.st_dev, held.st_ino):
            raise BudgetError("writer_lock_changed")
        self._regular(self._lock)

    def _load(self, expected):
        names = os.listdir(self._dir)
        files = sorted(name for name in names if _FILE.fullmatch(name))
        if not files or len(files) > 200000:
            raise BudgetError("invalid_receipt_sequence")
        if any(name != ".lock" and name not in files and not name.startswith(".pending-") for name in names):
            raise BudgetError("unexpected_budget_file")
        if any(name.startswith(".pending-") for name in names):
            self._poison = "interrupted_checkpoint"
        for index, name in enumerate(files):
            if name != f"{index:012d}.json":
                raise BudgetError("invalid_receipt_sequence")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._dir)
            try:
                self._regular(descriptor)
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    raw = handle.read(_MAX_RECEIPT + 1)
                record = strict_json_loads(raw.decode("utf-8"))
                if raw != _bytes(record) + b"\n":
                    raise BudgetError("noncanonical_receipt")
            except (UnicodeError, ValueError, TypeError) as exc:
                raise BudgetError("invalid_receipt") from exc
            finally:
                os.close(descriptor)
            self._apply(record)
            self._records.append(record)
        if self._header != expected:
            raise BudgetError("protocol_or_limits_changed")

    def _apply(self, record):
        _keys(record, {"spec", "sequence", "previous_sha256", "session_id", "observed_at", "event", "payload", "receipt_sha256"})
        body = {key: value for key, value in record.items() if key != "receipt_sha256"}
        if (record["spec"] != SPEC or type(record["sequence"]) is not int
                or record["sequence"] != len(self._records) or record["receipt_sha256"] != _hash(body)
                or record["previous_sha256"] != (self._records[-1]["receipt_sha256"] if self._records else None)
                or type(record["session_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", record["session_id"])):
            raise BudgetError("invalid_receipt_chain")
        observed = _timestamp(record["observed_at"])
        if self._records and observed < _timestamp(self._records[-1]["observed_at"]):
            raise BudgetError("clock_regression")
        event, data = record["event"], record["payload"]
        if event == "created":
            if self._records or data != self._header or observed < _timestamp(data["started_at"]):
                raise BudgetError("protocol_or_limits_changed")
            history = _money(data["historical_context_spend_usd"])
            self._state = {"known_comparison": history, "known_total": history + _money(data["provisioning_spend_usd"]),
                           "model_calls": 0, "pending": {}, "blocked": [], "settled": set()}
            if data["historical_unknown_event_ids"]:
                self._state["blocked"].append("historical_cost_unknown")
            return
        if not self._records:
            raise BudgetError("missing_budget_header")
        state = self._state
        if event == "reserved":
            _keys(data, {"reservation_id", "kind", "max_cost_usd", "model_calls", "batch_id", "phase_id"})
            reservation_id = data["reservation_id"]
            if type(reservation_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", reservation_id):
                raise BudgetError("invalid_reservation")
            if reservation_id in state["pending"] or reservation_id in state["settled"]:
                raise BudgetError("duplicate_reservation")
            self._validate_request(data["kind"], data["max_cost_usd"], data["model_calls"], data["batch_id"], data["phase_id"])
            if state["blocked"] or any(item["session_id"] != record["session_id"] for item in state["pending"].values()):
                raise BudgetError("reservation_after_unresolved_outcome")
            self._check_capacity(data["kind"], _money(data["max_cost_usd"]), data["model_calls"])
            if observed >= _timestamp(self._header["deadline"]):
                raise BudgetError("study_deadline")
            state["pending"][reservation_id] = {**deepcopy(data), "session_id": record["session_id"],
                                               "reserved_at": record["observed_at"], "known_cost_usd": 0.0,
                                               "outcome": "pending"}
            state["model_calls"] += data["model_calls"]
        elif event == "settled":
            _keys(data, {"reservation_id", "actual_cost_usd", "outcome"})
            pending = state["pending"].get(data["reservation_id"])
            if pending is None or pending["outcome"] != "pending" or pending["session_id"] != record["session_id"]:
                raise BudgetError("reservation_not_owned")
            if data["outcome"] not in ("known", "unknown"):
                raise BudgetError("invalid_outcome")
            if data["actual_cost_usd"] is None and data["outcome"] != "unknown":
                raise BudgetError("known_outcome_requires_cost")
            cost = Decimal(0) if data["actual_cost_usd"] is None else _money(data["actual_cost_usd"])
            state["known_total"] += cost
            if EVENT_RULES[pending["kind"]]["scope"] == "comparison":
                state["known_comparison"] += cost
            if cost > _money(pending["max_cost_usd"]):
                state["blocked"].append("reservation_exceeded")
            if data["outcome"] == "unknown":
                pending.update(outcome="unknown", known_cost_usd=float(cost))
                state["blocked"].append("outcome_unknown")
            else:
                del state["pending"][data["reservation_id"]]
                state["settled"].add(data["reservation_id"])
        elif event == "stopped":
            _keys(data, {"reason"})
            if type(data["reason"]) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", data["reason"]):
                raise BudgetError("invalid_stop_reason")
            state["blocked"].append(data["reason"])
        else:
            raise BudgetError("invalid_event")

    def _append(self, event, payload):
        self._check_open()
        record = {"spec": SPEC, "sequence": len(self._records),
                  "previous_sha256": self._records[-1]["receipt_sha256"] if self._records else None,
                  "session_id": self._session, "observed_at": _now().isoformat(), "event": event, "payload": deepcopy(payload)}
        record["receipt_sha256"] = _hash(record)
        raw = _bytes(record) + b"\n"
        if len(raw) > _MAX_RECEIPT:
            raise BudgetError("receipt_too_large")
        # Validate and apply before writing so a known charge remains inspectable
        # even if storage fails after the paid action. Such a session is poisoned.
        self._apply(record)
        temporary = ".pending-" + secrets.token_hex(16)
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._dir)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            name = f"{record['sequence']:012d}.json"
            os.link(temporary, name, src_dir_fd=self._dir, dst_dir_fd=self._dir, follow_symlinks=False)
            os.unlink(temporary, dir_fd=self._dir)
            os.fsync(self._dir)
        except OSError as exc:
            self._poison = "receipt_storage_failed"
            raise BudgetError("receipt_storage_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._records.append(record)
        return deepcopy(record)

    @staticmethod
    def _validate_request(kind, max_cost_usd, model_calls, batch_id, phase_id):
        if type(kind) is not str or kind not in EVENT_RULES:
            raise BudgetError("invalid_operation_kind")
        _money(max_cost_usd)
        if type(model_calls) is not int or model_calls != (1 if kind == "model_calls" else 0):
            raise BudgetError("invalid_model_call_count")
        _label(batch_id); _label(phase_id)

    def _reserved(self):
        comparison, total = Decimal(0), Decimal(0)
        for item in self._state["pending"].values():
            amount = max(Decimal(0), _money(item["max_cost_usd"]) - _money(item["known_cost_usd"]))
            total += amount
            if EVENT_RULES[item["kind"]]["scope"] == "comparison":
                comparison += amount
        return comparison, total

    def _check_capacity(self, kind, amount, calls):
        comparison, total = self._reserved()
        limits, state = self._header["limits"], self._state
        if state["model_calls"] + calls > limits["max_model_calls"]:
            raise BudgetError("model_call_limit")
        if state["known_total"] + total + amount > _money(limits["total_study_usd"]):
            raise BudgetError("total_study_cost_limit")
        if (EVENT_RULES[kind]["scope"] == "comparison"
                and state["known_comparison"] + comparison + amount > _money(limits["comparison_usd"])):
            raise BudgetError("comparison_cost_limit")

    @property
    def remaining_seconds(self):
        self._check_open()
        current = max(_now(), self._opened_wall + timedelta(seconds=time.monotonic() - self._opened_mono))
        return max(0.0, (_timestamp(self._header["deadline"]) - current).total_seconds())

    def _blocked(self):
        reasons = list(self._state["blocked"])
        if self._poison:
            reasons.append(self._poison)
        if any(item["session_id"] != self._session for item in self._state["pending"].values()):
            reasons.append("interrupted_reservation")
        if self._records and _now() < _timestamp(self._records[-1]["observed_at"]):
            reasons.append("clock_regression")
        if self.remaining_seconds <= 0:
            reasons.append("study_deadline")
        if self._state["known_comparison"] > _money(self._header["limits"]["comparison_usd"]):
            reasons.append("comparison_cost_exceeded")
        if self._state["known_total"] > _money(self._header["limits"]["total_study_usd"]):
            reasons.append("total_study_cost_exceeded")
        return sorted(set(reasons))

    def reserve(self, kind, *, max_cost_usd, model_calls=0, batch_id=None, phase_id=None):
        """Persist an upper-bound reservation before any potentially paid work."""
        self._check_open()
        self._validate_request(kind, max_cost_usd, model_calls, batch_id, phase_id)
        reasons = self._blocked()
        if reasons:
            raise BudgetError(reasons[0])
        self._check_capacity(kind, _money(max_cost_usd), model_calls)
        payload = {"reservation_id": secrets.token_hex(16), "kind": kind, "max_cost_usd": max_cost_usd,
                   "model_calls": model_calls, "batch_id": batch_id, "phase_id": phase_id}
        self._append("reserved", payload)
        return deepcopy(self._state["pending"][payload["reservation_id"]])

    def settle(self, reservation_id, *, actual_cost_usd, outcome="known"):
        """Record costs even if late/over budget; unknown costs stay unresolved.

        Settlement of other owned reservations remains possible after a stop,
        so incurred runtime/source charges are not lost. Interrupted sessions
        cannot settle a reservation created by an earlier writer.
        """
        self._check_open()
        if self._poison:
            raise BudgetError(self._poison)
        if type(reservation_id) is not str:
            raise BudgetError("invalid_reservation")
        self._append("settled", {"reservation_id": reservation_id, "actual_cost_usd": actual_cost_usd, "outcome": outcome})
        return self.status()

    def check_dispatch(self, reservation_id):
        """Recheck an owned reservation immediately before its external action."""
        self._check_open()
        pending = self._state["pending"].get(reservation_id)
        if pending is None or pending["session_id"] != self._session or pending["outcome"] != "pending":
            raise BudgetError("reservation_not_owned")
        reasons = self._blocked()
        if reasons:
            raise BudgetError(reasons[0])

    def stop(self, reason):
        """Persist a terminal refusal without pretending its known cost is unknown."""
        self._check_open()
        if self._poison:
            raise BudgetError(self._poison)
        self._append("stopped", {"reason": reason})
        return self.status()

    def status(self):
        self._check_open()
        comparison, total = self._reserved()
        return {"spec": SPEC, **deepcopy(self._header), "session_id": self._session,
                "known_comparison_cost_usd": float(self._state["known_comparison"]),
                "known_total_cost_usd": float(self._state["known_total"]),
                "reserved_comparison_usd": float(comparison), "reserved_total_usd": float(total),
                "model_calls": self._state["model_calls"], "pending": deepcopy(list(self._state["pending"].values())),
                "blocked_reasons": self._blocked(), "remaining_seconds": self.remaining_seconds,
                "last_receipt_sha256": self._records[-1]["receipt_sha256"] if self._records else None,
                "receipt_count": len(self._records), "execution_authorized": False}

    def receipts(self):
        self._check_open()
        return deepcopy(self._records)


class BudgetedModel:
    """Reserve/settle a model adapter without hiding its response or timeout.

    The engine calls ``before_call`` before counting an inference. ``complete``
    consumes that reservation once. Calling complete directly also reserves,
    but a common engine should use the explicit hook to avoid phantom calls.
    The wrapper never retries. Exceptions and incomplete cost reports leave an
    unresolved reservation; known paid responses are returned unchanged.
    """

    def __init__(self, client, budget, *, phase_id=None, batch_id=None):
        self.client, self.budget = client, budget
        self.phase_id, self.batch_id = _label(phase_id), _label(batch_id)
        self._reservation = None
        self._timeout = getattr(client, "timeout_seconds", 90)

    @property
    def remaining_seconds(self):
        return self.budget.remaining_seconds

    @property
    def timeout_seconds(self):
        return self._timeout

    @timeout_seconds.setter
    def timeout_seconds(self, value):
        if not finite_number(value, positive=True):
            raise BudgetError("invalid_timeout")
        self._timeout = value
        if hasattr(self.client, "timeout_seconds"):
            self.client.timeout_seconds = min(value, self.remaining_seconds)

    def before_call(self, max_cost_usd):
        if self._reservation is not None:
            raise BudgetError("model_reservation_unconsumed")
        reservation = self.budget.reserve("model_calls", max_cost_usd=max_cost_usd, model_calls=1,
                                          phase_id=self.phase_id, batch_id=self.batch_id)
        self._reservation = reservation
        return deepcopy(reservation)

    def complete(self, system, messages, max_cost_usd):
        if self._reservation is None:
            try:
                self.before_call(max_cost_usd)
            except BudgetError as exc:
                raise NoDispatch(exc.reason) from exc
        reservation, self._reservation = self._reservation, None
        try:
            if _money(max_cost_usd) != _money(reservation["max_cost_usd"]):
                self.budget.stop("model_reservation_limit_changed")
                raise BudgetError("model_reservation_limit_changed")
            self.budget.check_dispatch(reservation["reservation_id"])
        except BudgetError as exc:
            try:
                self.budget.settle(reservation["reservation_id"], actual_cost_usd=0, outcome="known")
            except (BudgetError, OSError):
                pass
            raise NoDispatch(exc.reason) from exc
        try:
            if hasattr(self.client, "timeout_seconds"):
                self.client.timeout_seconds = min(self._timeout, self.remaining_seconds)
            response = self.client.complete(system, messages, max_cost_usd=max_cost_usd)
        except BaseException as exc:
            try:
                self.budget.settle(reservation["reservation_id"], actual_cost_usd=None, outcome="unknown")
            except (BudgetError, OSError):
                pass  # The pre-dispatch reservation already blocks a reopened session.
            if isinstance(exc, NoDispatch):
                # A backend cannot retroactively declare that our call to it
                # never occurred. Its raised outcome remains potentially paid.
                raise BudgetError("model_call_outcome_unknown") from exc
            raise
        cost = reported_cost(response) if isinstance(response, dict) else None
        unknown = cost is None or bool(response.get("spend_unknown")) or response.get("cost_known") is False
        try:
            self.budget.settle(reservation["reservation_id"], actual_cost_usd=cost, outcome="unknown" if unknown else "known")
        except (BudgetError, OSError):
            # Preserve the paid response for the engine's independent receipt.
            # A failed append poisons the live ledger and leaves durable pending
            # evidence, so no subsequent provider action can be dispatched.
            self.budget._poison = "receipt_storage_failed"
        return response
