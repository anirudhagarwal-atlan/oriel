"""One extraction's durable request count and monotonic time budget.

Reserve immediately before transport dispatch. A reservation is conservative:
it is not proof that a request reached a vendor. Failed attempts count, and a
pending reservation after interruption is unknown, never permission to retry.
The object cannot reopen/resume a ledger. Threads share one object and deadline.

Only sanitized fixed fields are persisted; never supply URLs, headers, source
rows or exception text. Publication fsyncs the file and its private directory.
This is local crash evidence, not authenticated network or vendor evidence.
The caller must enforce the returned timeout and terminate a blocked worker at
the deadline: Python cannot cancel arbitrary transports from this helper.
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time


SPEC = "oriel.connector-extraction-budget/v1"
MAX_LEDGER_BYTES = 32 * 1024 * 1024
_OUTCOMES = {"pending", "succeeded", "failed", "timeout", "unknown"}
_REASONS = {"request_limit", "deadline", "request_failed", "request_timeout",
            "request_unknown", "pending_requests", "worker_failed", "worker_timeout",
            "interrupted", "transport_failed", "persistence_failed"}
_FAIL_REASONS = {"worker_failed", "worker_timeout", "interrupted", "transport_failed"}


class BudgetError(ValueError):
    """Sanitized budget or receipt validation failure."""


class BudgetExceeded(BudgetError):
    """No further transport dispatch is permitted."""


class BudgetStorageError(BudgetError):
    """Durable evidence could not be verified or written; dispatch must stop."""


def validate_limits(value):
    if type(value) is not dict or set(value) != {"max_requests", "max_seconds"}:
        raise BudgetError("extraction_budget_requires_max_requests_and_max_seconds")
    for key, maximum in (("max_requests", 100_000), ("max_seconds", 3600)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise BudgetError("extraction_budget_bounds_invalid")
    return dict(value)


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise BudgetError("invalid_budget_receipt") from None
    if len(raw) > MAX_LEDGER_BYTES:
        raise BudgetError("budget_receipt_size_exceeded")
    return raw


def _seal(value):
    result = copy.deepcopy(value)
    result["ledger_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    if type(value) is not str or len(value) > 40:
        raise BudgetError("invalid_budget_receipt")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed
    except (TypeError, ValueError, OverflowError):
        raise BudgetError("invalid_budget_receipt") from None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _counts(requests):
    return {"reserved": len(requests), **{name: sum(item["outcome"] == name for item in requests)
                                       for name in sorted(_OUTCOMES)}}


def _validate(value):
    keys = {"spec", "limits", "started_at", "updated_at", "elapsed_seconds", "status",
            "stop_reason", "requests", "counts", "limits_exceeded", "ledger_sha256"}
    if type(value) is not dict or set(value) != keys or value["spec"] != SPEC:
        raise BudgetError("invalid_budget_receipt")
    limits = validate_limits(value["limits"])
    if _timestamp(value["updated_at"]) < _timestamp(value["started_at"]):
        raise BudgetError("invalid_budget_receipt")
    elapsed = value["elapsed_seconds"]
    if not _number(elapsed) or value["status"] not in {"active", "completed", "stopped"}:
        raise BudgetError("invalid_budget_receipt")
    if value["stop_reason"] is not None and value["stop_reason"] not in _REASONS:
        raise BudgetError("invalid_budget_receipt")
    requests = value["requests"]
    if type(requests) is not list or len(requests) > limits["max_requests"]:
        raise BudgetError("invalid_budget_receipt")
    previous = 0
    for number, row in enumerate(requests, 1):
        if type(row) is not dict or set(row) != {"request_id", "reserved_elapsed_seconds",
                                              "completed_elapsed_seconds", "outcome", "status_code"}:
            raise BudgetError("invalid_budget_receipt")
        when = row["reserved_elapsed_seconds"]
        if type(row["request_id"]) is not int or row["request_id"] != number or not _number(when) or not previous <= when <= elapsed:
            raise BudgetError("invalid_budget_receipt")
        previous = when
        if row["outcome"] not in _OUTCOMES:
            raise BudgetError("invalid_budget_receipt")
        end, status = row["completed_elapsed_seconds"], row["status_code"]
        if row["outcome"] == "pending":
            if end is not None or status is not None:
                raise BudgetError("invalid_budget_receipt")
        elif not _number(end) or not when <= end <= elapsed:
            raise BudgetError("invalid_budget_receipt")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise BudgetError("invalid_budget_receipt")
        if row["outcome"] in {"timeout", "unknown"} and status is not None:
            raise BudgetError("invalid_budget_receipt")
    expected_counts = _counts(requests)
    if (type(value["counts"]) is not dict or value["counts"] != expected_counts
            or any(type(count) is not int for count in value["counts"].values())):
        raise BudgetError("invalid_budget_receipt")
    exceeded = value["limits_exceeded"]
    if (type(exceeded) is not dict or set(exceeded) != {"requests", "seconds"}
            or any(type(flag) is not bool for flag in exceeded.values())
            or exceeded["seconds"] != (elapsed >= limits["max_seconds"])
            or exceeded["requests"] != (value["stop_reason"] == "request_limit")):
        raise BudgetError("invalid_budget_receipt")
    if value["status"] == "stopped":
        if value["stop_reason"] is None:
            raise BudgetError("invalid_budget_receipt")
    elif value["stop_reason"] is not None:
        raise BudgetError("invalid_budget_receipt")
    if value["status"] in {"active", "completed"} and (exceeded["seconds"] or
            any(expected_counts[key] for key in ("failed", "timeout", "unknown"))):
        raise BudgetError("invalid_budget_receipt")
    if value["status"] == "completed" and expected_counts["pending"]:
        raise BudgetError("invalid_budget_receipt")
    basis = {key: item for key, item in value.items() if key != "ledger_sha256"}
    if value["ledger_sha256"] != hashlib.sha256(_canonical(basis)).hexdigest():
        raise BudgetError("budget_receipt_hash_mismatch")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetError("invalid_budget_receipt")
        result[key] = value
    return result


def _reject_constant(_value):
    raise BudgetError("invalid_budget_receipt")


def _directory(path):
    """Open each directory component without following symlinks."""
    parts = path.parent.parts
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()):
            raise BudgetStorageError("budget_directory_must_be_private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_at(directory, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > MAX_LEDGER_BYTES):
            raise BudgetStorageError("budget_receipt_must_be_private_regular_file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_LEDGER_BYTES + 1)
        if len(raw) > MAX_LEDGER_BYTES:
            raise BudgetError("budget_receipt_size_exceeded")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if _canonical(value) != raw:
            raise BudgetError("budget_receipt_not_canonical")
        _validate(value)
        return value, (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


def read_budget(ledger_path):
    """Read bounded exact evidence, including pending requests after a kill.

    A stored active state means the worker did not durably finish. It is never
    converted to success, and pending requests are not inferred to have failed.
    """
    directory = None
    try:
        path = Path(ledger_path).absolute()
        directory = _directory(path)
        return _read_at(directory, path.name)[0]
    except BudgetError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise BudgetStorageError("budget_receipt_unavailable_or_invalid") from None
    finally:
        if directory is not None:
            os.close(directory)


def compact_summary(summary):
    """Validate a full sealed ledger, then return its bounded envelope view.

    ``ledger_sha256`` still identifies the full private ledger, including all
    request records; it is not a hash of this compact view. The fixed filename
    is relative to the extraction's private attempt directory, never a URL or
    caller-controlled path. Consumers compare this view with ``read_budget``.
    """
    try:
        _validate(summary)
        _canonical(summary)
    except BudgetError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        raise BudgetError("invalid_budget_receipt") from None
    return {**copy.deepcopy({key: value for key, value in summary.items() if key != "requests"}),
            "requests_file": "request-budget.json"}


class ExtractionBudget:
    """A single-use budget, shared by all requests in one extraction.

    The ledger's parent must already be an owner-only directory. A preexisting
    file is always refused; use ``read_budget`` for postmortem inspection.
    Methods are thread-safe. Completion after a stop records what is known but
    cannot reopen dispatch. Returned dictionaries are detached snapshots.
    """

    def __init__(self, ledger_path, limits):
        self.limits = validate_limits(limits)
        self.path = Path(ledger_path).absolute()
        self._lock = threading.RLock()
        self._start = time.monotonic()
        self._deadline = self._start + self.limits["max_seconds"]
        now = _now()
        self._state = {"spec": SPEC, "limits": dict(self.limits), "started_at": now,
                       "updated_at": now, "elapsed_seconds": 0.0, "status": "active",
                       "stop_reason": None, "requests": []}
        self._identity = None
        self._directory_identity = None
        self._last_hash = None
        self._storage_failed = False
        self._persist(initial=True)

    def _elapsed(self):
        return max(self._state["elapsed_seconds"], time.monotonic() - self._start, 0.0)

    def _summary(self):
        value = copy.deepcopy(self._state)
        value["counts"] = _counts(value["requests"])
        value["limits_exceeded"] = {
            "requests": value["stop_reason"] == "request_limit",
            "seconds": value["elapsed_seconds"] >= self.limits["max_seconds"],
        }
        return _seal(value)

    def summary(self):
        """Return the latest detached accounting snapshot, without a write."""
        with self._lock:
            return self._summary()

    def _persist(self, *, initial=False):
        directory = descriptor = None
        temporary = None
        try:
            if self._storage_failed:
                raise BudgetStorageError("budget_storage_failed")
            directory = _directory(self.path)
            info = os.fstat(directory)
            identity = (info.st_dev, info.st_ino)
            if self._directory_identity is not None and identity != self._directory_identity:
                raise BudgetStorageError("budget_directory_changed")
            self._directory_identity = identity
            if not initial:
                previous, identity = _read_at(directory, self.path.name)
                if identity != self._identity or previous["ledger_sha256"] != self._last_hash:
                    raise BudgetStorageError("budget_receipt_changed")
            value = self._summary()
            _validate(value)
            raw = _canonical(value)
            temporary = ".budget-" + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if initial:
                os.link(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                os.unlink(temporary, dir_fd=directory)
                temporary = None
            else:
                os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
            os.fsync(directory)
            persisted, self._identity = _read_at(directory, self.path.name)
            if persisted != value:
                raise BudgetStorageError("budget_receipt_changed")
            self._last_hash = value["ledger_sha256"]
        except (OSError, BudgetError, ValueError, TypeError):
            self._storage_failed = True
            self._state["status"] = "stopped"
            self._state["stop_reason"] = "persistence_failed"
            raise BudgetStorageError("budget_evidence_persistence_failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None and directory is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            if directory is not None:
                os.close(directory)

    def _touch(self):
        self._state["elapsed_seconds"] = self._elapsed()
        self._state["updated_at"] = max(self._state["updated_at"], _now())

    def _stop(self, reason):
        self._touch()
        self._state["status"] = "stopped"
        # Preserve the first terminal reason; later completions cannot reopen it.
        self._state["stop_reason"] = self._state["stop_reason"] or reason

    def _dispatch_allowed(self):
        if self._storage_failed:
            raise BudgetStorageError("budget_storage_failed")
        if self._state["status"] != "active":
            raise BudgetExceeded("extraction_budget_stopped")
        if time.monotonic() >= self._deadline:
            self._stop("deadline")
            self._persist()
            raise BudgetExceeded("extraction_deadline_exceeded")

    def remaining_seconds(self):
        """Clamp a transport timeout immediately before sending; never resets."""
        with self._lock:
            self._dispatch_allowed()
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                self._stop("deadline")
                self._persist()
                raise BudgetExceeded("extraction_deadline_exceeded")
            return remaining

    def reserve_request(self):
        """Persist an attempt before returning authority for a single send."""
        with self._lock:
            self._dispatch_allowed()
            if len(self._state["requests"]) >= self.limits["max_requests"]:
                self._stop("request_limit")
                self._persist()
                raise BudgetExceeded("extraction_request_limit_exceeded")
            self._touch()
            request_id = len(self._state["requests"]) + 1
            self._state["requests"].append({"request_id": request_id,
                "reserved_elapsed_seconds": self._state["elapsed_seconds"],
                "completed_elapsed_seconds": None, "outcome": "pending", "status_code": None})
            self._persist()
            # Disk latency consumes the same deadline. A late reservation is
            # left pending conservatively and never returned for dispatch.
            return {"request_id": request_id, "timeout_seconds": self.remaining_seconds()}

    def complete_request(self, request_id, *, outcome="succeeded", status_code=None):
        """Retain the known result, including late responses; never retry.

        A completion after the deadline records its outcome and a terminal
        deadline stop. The caller must check status before using the response.
        Transport failures/timeouts/unknown outcomes stop all new dispatch.
        """
        if type(outcome) is not str or outcome not in _OUTCOMES - {"pending"}:
            raise BudgetError("invalid_request_outcome")
        if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
            raise BudgetError("invalid_response_status")
        if outcome in {"timeout", "unknown"} and status_code is not None:
            raise BudgetError("invalid_response_status")
        with self._lock:
            if type(request_id) is not int or not 1 <= request_id <= len(self._state["requests"]):
                raise BudgetError("invalid_request_reservation")
            row = self._state["requests"][request_id - 1]
            if row["outcome"] != "pending":
                raise BudgetError("request_already_completed")
            self._touch()
            row.update(outcome=outcome, status_code=status_code,
                       completed_elapsed_seconds=self._state["elapsed_seconds"])
            if outcome != "succeeded":
                self._stop({"failed": "request_failed", "timeout": "request_timeout", "unknown": "request_unknown"}[outcome])
            elif self._state["elapsed_seconds"] >= self.limits["max_seconds"]:
                self._stop("deadline")
            self._persist()
            return self._summary()

    def fail(self, reason="worker_failed"):
        """Stop with a fixed sanitized reason; unresolved reservations remain."""
        if type(reason) is not str or reason not in _FAIL_REASONS:
            raise BudgetError("invalid_budget_failure_reason")
        with self._lock:
            if self._state["status"] == "completed":
                raise BudgetError("budget_already_finished")
            self._stop(reason)
            self._persist()
            return self._summary()

    def finish(self):
        """Durably finish only after every reserved request succeeded in time."""
        with self._lock:
            if self._storage_failed:
                raise BudgetStorageError("budget_storage_failed")
            if self._state["status"] == "completed":
                return self._summary()
            self._touch()
            if self._state["status"] == "active":
                if self._state["elapsed_seconds"] >= self.limits["max_seconds"]:
                    self._stop("deadline")
                elif any(item["outcome"] == "pending" for item in self._state["requests"]):
                    self._stop("pending_requests")
                else:
                    self._state["status"] = "completed"
            self._persist()
            return self._summary()
