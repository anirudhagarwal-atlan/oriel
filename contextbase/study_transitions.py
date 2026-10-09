"""Approved, narrowly scoped phase writes with full source readback.

The supplied pair must address pinned local Rystic simulators. This executor
does not seed, reset, retry, import a workspace, or call a model. Prepared
intent and a successful request response are not proof of source state: both
phase boundaries are checked against complete validation captures. Ordinary
native mutation support remains unverified until an actual pair is exercised.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .rystic import local_url
from .rystic_validation import capture_validation
from .study_binding import _hash, _identities, _plan, _same, _validation

SPEC = "contextbase.phase-transition/v1"
MAX_OPERATIONS = 100
ISSUE_UPDATE = """mutation ContextBasePhaseIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id } }
}"""


class TransitionError(ValueError):
    """A sanitized failure with the latest in-memory observation receipt."""
    def __init__(self, receipt):
        self.receipt = deepcopy(receipt)
        super().__init__("Phase transition did not complete verification. Inspect the local transition receipt; do not retry writes blindly.")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _reserve(path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Transition output already exists; select a new file.") from None
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return target


def _checkpoint(path, receipt):
    """Replace only our reserved artifact; preserve the last checkpoint on failure."""
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    body["receipt_sha256"] = _hash(body)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".phase-transition-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(body, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        receipt.clear()
        receipt.update(body)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _operations(changes):
    if not 1 <= len(changes) <= MAX_OPERATIONS:
        raise ValueError("Transition operation count is outside the supported bound.")
    result = []
    for change in changes:
        identity = change["record_id"]
        if type(identity) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise ValueError("Transition requires a safe physical record ID.")
        source, operation = change["source"], change["operation"]
        if source == "linear.issues" and operation == "update_workflow_state":
            state_id = change["after"]["state_id"]
            if type(state_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", state_id):
                raise ValueError("Transition requires a safe workflow state ID.")
            request = {"query": ISSUE_UPDATE, "variables": {"id": identity, "input": {"stateId": state_id}}}
        elif source == "stripe.customers" and operation == "update_metadata":
            account_id = change["after"]["account_id"]
            if type(account_id) is not str or not account_id:
                raise ValueError("Transition requires an explicit account mapping.")
            request = {"method": "POST", "path": "/v1/customers/" + identity,
                       "form": {"metadata[account_id]": account_id}}
        elif source == "stripe.customers" and operation == "remove_metadata_key":
            if change["after"]["is_internal"] is not None:
                raise ValueError("Metadata removal must declare an absent policy.")
            request = {"method": "POST", "path": "/v1/customers/" + identity,
                       "form": {"metadata[is_internal]": ""}}
        else:
            raise ValueError("Unsupported phase transition operation.")
        result.append({"change": deepcopy(change), "request": request, "status": "not_attempted"})
    return result


def _apply(pair, operation):
    request = operation["request"]
    identity = operation["change"]["record_id"]
    if operation["change"]["source"] == "linear.issues":
        result = pair.linear.graphql(request["query"], deepcopy(request["variables"]))
        payload = result.get("issueUpdate") if isinstance(result, dict) else None
        if (not isinstance(payload, dict) or payload.get("success") is not True
                or not isinstance(payload.get("issue"), dict) or payload["issue"].get("id") != identity):
            raise ValueError("Source did not confirm the declared issue update.")
    else:
        result = pair.stripe.request(request["method"], request["path"], form=deepcopy(request["form"]))
        if not isinstance(result, dict) or result.get("id") != identity:
            raise ValueError("Source did not confirm the declared customer update.")


def apply_transition(pair, plan, from_phase, to_phase, output, *, approve_plan, approve_action):
    """Apply exactly one approved adjacent transition, with no automatic retries.

    The output is a new private receipt, checkpointed before every possible
    source mutation. A failed request may have changed the source: remaining
    writes stop, and full readback is still attempted. Partial/uncertain results
    raise ``TransitionError`` carrying the receipt. Full captures are local
    evidence; no workspace snapshot or authenticated native-run claim is made.
    """
    plan = deepcopy(plan)
    before = _plan(plan, from_phase)
    after = _plan(plan, to_phase)
    matches = [item for item in plan["transitions"]
               if item["from_phase"] == from_phase and item["to_phase"] == to_phase]
    if len(matches) != 1:
        raise ValueError("Select one declared adjacent phase transition.")
    transition = matches[0]
    if (type(approve_plan) is not str or approve_plan != plan["plan_sha256"]
            or type(approve_action) is not str or approve_action != transition["action_sha256"]):
        raise ValueError("Approve the exact prepared plan and transition action SHA-256 values.")
    operations = _operations(transition["changes"])
    stripe_url, linear_url = local_url(pair.stripe.url), local_url(pair.linear.url)
    if stripe_url == linear_url:
        raise ValueError("Select separate Stripe and Linear simulator instances.")
    target = _reserve(output)
    started = time.perf_counter()
    receipt = {"spec": SPEC, "outcome": "in_progress", "plan_sha256": plan["plan_sha256"],
               "action_sha256": transition["action_sha256"], "from_phase": from_phase, "to_phase": to_phase,
               "before_intent_sha256": before["intent_sha256"], "after_intent_sha256": after["intent_sha256"],
               "started_at": _now(), "finished_at": None, "elapsed_seconds": None,
               "source_endpoints": {"stripe": stripe_url, "linear": linear_url},
               "simulators": None, "before_capture": None, "after_capture": None,
               "before_state_matches": False, "after_state_matches": False, "operations": operations,
               "write_attempts": 0, "write_responses_confirmed": 0, "model_calls": 0,
               "workspace_imported": False, "claim_eligible": False, "held_out": False,
               "evidence_origin": "supplied_pair_observation", "origin_authentication": "not_authenticated",
               "limitations": ["Native mutation support is unverified until the supplied local pair is exercised and read back.",
                               "Sequential reads and writes require a quiescent simulator; there is no cross-service transaction.",
                               "A request failure may have applied a write. No writes are retried or automatically rolled back.",
                               "Captures and receipts do not import or refresh a workspace. Guarded states must remain refused."]}

    def checkpoint():
        try:
            _checkpoint(target, receipt)
        except Exception:
            receipt["outcome"] = "receipt_storage_failed"
            raise TransitionError(receipt) from None

    def finish(outcome, error_type=None):
        receipt.update(outcome=outcome, finished_at=_now(), elapsed_seconds=time.perf_counter() - started)
        if error_type is not None:
            receipt["error_type"] = error_type
        checkpoint()
        if outcome != "applied":
            raise TransitionError(receipt) from None
        return deepcopy(receipt)

    checkpoint()
    try:
        identities = pair.verify()
        _identities(identities)
        receipt["simulators"] = deepcopy(identities)
        seeded_identities = before["intent_manifest"].get("simulator_versions")
        if seeded_identities is not None:
            _same(identities, seeded_identities, "Seeded simulator identities")
        captured = capture_validation(pair, before["intent_manifest"]["namespace"])
        receipt["before_capture"] = captured
        checkpoint()
        _validation(captured, before["intent_manifest"], plan["source_issue_account_ids"])
        _same(captured["receipt"]["simulators"], identities, "Before source identities")
        receipt["before_state_matches"] = True
        checkpoint()
    except TransitionError:
        raise
    except Exception as exc:
        return finish("before_state_unverified", type(exc).__name__)

    write_failure = None
    for operation in operations:
        operation["status"] = "in_progress"
        receipt["write_attempts"] += 1
        # Persist uncertainty before sending anything. If this fails there is
        # no new source call; earlier effects remain recorded and need review.
        try:
            checkpoint()
        except TransitionError:
            operation["status"] = "not_sent_checkpoint_failed"
            receipt["write_attempts"] -= 1
            write_failure = "receipt_storage_failed"
            break
        try:
            _apply(pair, operation)
            operation["status"] = "response_confirmed"
            receipt["write_responses_confirmed"] += 1
            checkpoint()
        except TransitionError:
            write_failure = "receipt_storage_failed"
            break
        except Exception as exc:
            operation["status"] = "effect_uncertain"
            operation["error_type"] = type(exc).__name__
            write_failure = "write_uncertain"
            break

    after_error = None
    try:
        captured = capture_validation(pair, after["intent_manifest"]["namespace"])
        receipt["after_capture"] = captured
        _validation(captured, after["intent_manifest"], plan["source_issue_account_ids"])
        _same(captured["receipt"]["simulators"], identities, "After source identities")
        receipt["after_state_matches"] = True
    except Exception as exc:
        after_error = type(exc).__name__
    if write_failure:
        return finish(write_failure, after_error)
    if after_error:
        return finish("after_state_unverified", after_error)
    return finish("applied")
