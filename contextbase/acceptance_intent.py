"""Offline admission of a fresh allocated fixture's baseline acceptance intent.

This distinct input path consumes an allocation, a complete seeded manifest,
complete baseline source readback and explicit provisioning/review declarations.
It never accepts a supplied development phase plan or binding as its seed input.
Phase semantics/templates are derived afresh using the existing pure oracle and
preparation logic. Their private development-format representation does not grant
held-out status, and this intent itself authorizes no execution or public claim.

Seed replay verifies generated labels, order, cases, monetary/status policies,
titles, priorities and workflow references. Server-generated IDs and actual owner
assignments are instead checked against baseline readback. Exact discovered user
roster ordering was not retained by seed_recipe and cannot be replayed. Guaranteed
unassigned controls and empty-versus-nonempty roster behavior are still checked.
Refund totals are checked, but separate refund objects/team discovery are not
captured. Declared isolation/no-tuning reviews are not authenticated proof; unique
hashes do not establish independence or rule out undeclared tuning exposure.

validate_allocation and validate_intent return fresh validated copies or raise
ValueError. prepare_intent optionally creates one exclusive output file. Inputs
are never mutated. Hashes use the existing strict ASCII canonical JSON codec.
Only local code reads and private temporary phase derivation occur; no sources,
models, Registry services, external network or dependency installs are used.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import study_report as check
from .rystic import PINS
from .rystic_recipe import FIXTURE_NAMESPACE, RECIPE_SPEC, _plan_accounts
from .sequential_study import VALIDATION_FIELDS
from .study_allocation import allocate_study
from .study_binding import _hash, _json, _plan, _same, _validation, _identities
from .study_phases import prepare_phases
from .study_templates import compile_templates

SPEC = "contextbase.acceptance-intent/v1"
PROVISIONING_SPEC = "contextbase.fixture-provisioning/v1"
PROVISIONING_FIELDS = {"spec", "allocation_sha256", "batch_id", "environment_id", "started_at", "finished_at",
                       "evidence_sha256", "costs_usd", "isolation_review", "holdout_review"}
MANIFEST_FIELDS = {"spec", "namespace", "seed", "account_count", "simulator_versions", "team_id", "status",
                   "synthetic", "owner_provenance", "metric", "accounts", "variant", "variation"}
ACCOUNT_FIELDS = {"account_id", "name", "is_internal", "customer_id", "charges", "issues"}
CHARGE_FIELDS = {"charge_id", "amount_cents", "refund_cents", "currency", "paid", "captured", "status", "disputed", "refund_ids"}
ISSUE_FIELDS = {"issue_id", "identifier", "title", "priority", "state_type", "state_name", "state_id", "owner_id", "owner_name"}
CODE_FILES = {"allocation_sha256": "study_allocation.py", "generator_sha256": "rystic_recipe.py",
              "scorer_sha256": "rystic_eval.py", "renderer_sha256": "study_templates.py",
              "phase_preparation_sha256": "study_phases.py", "source_validation_sha256": "rystic_validation.py",
              "phase_binding_sha256": "study_binding.py"}


def _hashes():
    return {key: hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()
            for key, filename in CODE_FILES.items()}


def _checked_hash(value, field):
    check._sha(value.get(field), field)
    _same(value[field], _hash({key: item for key, item in value.items() if key != field}), field + " changed")


def validate_allocation(allocation):
    """Replay the complete current allocator; never guess missing parameters."""
    try:
        _json(allocation)
        check._object(allocation, "allocation")
        _checked_hash(allocation, "allocation_sha256")
        check._timestamp(allocation.get("created_at"), "allocation.created_at")
        _same(allocation.get("workload"), "recurring_v1", "Acceptance intent requires recurring_v1")
        batches = check._list(allocation.get("fixture_batches"), "fixture_batches")
        replay = allocate_study(None, master_seed=allocation["master_seed"], development_seeds=allocation["development_seeds"],
                                batches=len(batches), repeats=allocation["repeats"], account_counts=allocation["account_counts"],
                                workload=allocation["workload"])
        replay["created_at"] = allocation["created_at"]
        replay["allocation_sha256"] = _hash({key: value for key, value in replay.items() if key != "allocation_sha256"})
        _same(allocation, replay, "Allocation differs from exact reproducible seed/schedule parameters")
        return deepcopy(allocation)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed allocation or missing reproducible parameters.") from exc


def _manifest(manifest, fixture):
    check._record(manifest, MANIFEST_FIELDS, "seed manifest")
    for field, value in (("spec", RECIPE_SPEC), ("namespace", FIXTURE_NAMESPACE), ("status", "complete"),
                         ("synthetic", True), ("variant", "shuffled_cases_v1"), ("seed", fixture["seed"]),
                         ("account_count", fixture["accounts"]), ("metric", "net_exposed_collected_usd"),
                         ("owner_provenance", "Existing synthetic users from the Rystic Linear simulator")):
        _same(manifest[field], value, "Allocated manifest " + field)
    _same(manifest["variation"], {"semantic_case_assignment": "seeded_permutation_of_demo_cases",
        "account_labels": "seeded_unique_synthetic_labels", "source_insertion_order": "seeded_account_charge_and_issue_permutations",
        "split_assignment": "not_assigned"}, "Seed variant declaration")
    _identities(manifest["simulator_versions"])
    check._text(manifest["team_id"], "manifest.team_id")
    accounts = check._list(manifest["accounts"], "manifest.accounts")
    check._check(len(accounts) == fixture["accounts"], "Allocated account count differs")
    states, state_ids, owners, refunds = {}, {}, {}, {}
    for account in accounts:
        check._record(account, ACCOUNT_FIELDS, "seed account")
        for charge in check._list(account["charges"], "seed charges"):
            check._record(charge, CHARGE_FIELDS, "seed charge")
            identifiers = check._list(charge["refund_ids"], "refund IDs", nonempty=False)
            check._check(len(identifiers) == (1 if charge["refund_cents"] else 0), "Seed refund object count differs")
            for identity in identifiers:
                check._text(identity, "refund ID")
                check._check(identity not in refunds, "Repeated refund identity")
                refunds[identity] = charge["charge_id"]
        for issue in check._list(account["issues"], "seed issues"):
            check._record(issue, ISSUE_FIELDS, "seed issue")
            state_type = check._text(issue["state_type"], "state_type")
            state = {"id": check._text(issue["state_id"], "state_id"), "name": check._text(issue["state_name"], "state_name")}
            if state_type in states:
                _same(states[state_type], state, "Seed must use one discovered workflow state per type")
            states[state_type] = state
            if state["id"] in state_ids:
                _same(state_ids[state["id"]], {"type": state_type, "name": state["name"]}, "Workflow identity is ambiguous")
            state_ids[state["id"]] = {"type": state_type, "name": state["name"]}
            check._check((issue["owner_id"] is None) == (issue["owner_name"] is None), "Owner ID/name must both be assigned or null")
            if issue["owner_id"] is not None:
                check._text(issue["owner_id"], "owner_id")
                check._text(issue["owner_name"], "owner_name")
                if issue["owner_id"] in owners:
                    _same(owners[issue["owner_id"]], issue["owner_name"], "Owner identity has inconsistent names")
                owners[issue["owner_id"]] = issue["owner_name"]
    _same(sorted(states), ["canceled", "completed", "started"], "Seed workflow state types")
    # One private marker distinguishes guaranteed unassigned controls from
    # ordinary roster-assigned cases. The marker is never exported.
    replay = _plan_accounts(fixture["accounts"], fixture["seed"], states,
                            [{"id": "__private_owner_probe__", "name": "Private probe"}], variant="shuffled_cases_v1")
    roster_assignments = []
    for actual, generated in zip(accounts, replay):
        _same({key: actual[key] for key in ACCOUNT_FIELDS - {"customer_id", "charges", "issues"}},
              {key: generated[key] for key in ACCOUNT_FIELDS - {"customer_id", "charges", "issues"}}, "Seeded account labels/cases/order")
        _same([{key: charge[key] for key in CHARGE_FIELDS - {"charge_id", "refund_ids"}} for charge in actual["charges"]],
              [{key: charge[key] for key in CHARGE_FIELDS - {"charge_id", "refund_ids"}} for charge in generated["charges"]], "Seeded monetary semantics/order")
        _same([{key: issue[key] for key in ISSUE_FIELDS - {"issue_id", "identifier", "owner_id", "owner_name"}} for issue in actual["issues"]],
              [{key: issue[key] for key in ISSUE_FIELDS - {"issue_id", "identifier", "owner_id", "owner_name"}} for issue in generated["issues"]],
              "Seeded issue semantics/order")
        for issue, probe in zip(actual["issues"], generated["issues"]):
            if probe["owner_id"] is None:
                _same([issue["owner_id"], issue["owner_name"]], [None, None], "Guaranteed unassigned owner control changed")
            else:
                roster_assignments.append(issue["owner_id"] is not None)
    check._check(len(set(roster_assignments)) == 1, "Seed roster must be uniformly empty or populated outside unassigned controls")
    mapping = {"team_id": manifest["team_id"],
        "customers": {account["customer_id"]: account["account_id"] for account in accounts},
        "charges": {charge["charge_id"]: account["customer_id"] for account in accounts for charge in account["charges"]},
        "issues": {issue["issue_id"]: account["account_id"] for account in accounts for issue in account["issues"]},
        "issue_identifiers": {issue["issue_id"]: issue["identifier"] for account in accounts for issue in account["issues"]},
        "states": state_ids, "owners": owners, "refunds": refunds}
    # _plan/independent oracle and full capture validation additionally reject
    # duplicate physical/logical IDs before this map is accepted or exported.
    return mapping


def _provisioning(provisioning, allocation, batch_id, receipt):
    check._record(provisioning, PROVISIONING_FIELDS, "provisioning")
    for key, value in (("spec", PROVISIONING_SPEC), ("allocation_sha256", allocation["allocation_sha256"]), ("batch_id", batch_id)):
        _same(provisioning[key], value, "Provisioning " + key)
    check._text(provisioning["environment_id"], "environment_id")
    check._sha(provisioning["evidence_sha256"], "provisioning.evidence_sha256")
    costs = check._record(provisioning["costs_usd"], {"fixture_generation", "runtime"}, "provisioning costs")
    for field, value in costs.items():
        if value is not None:
            check._number(value, "provisioning cost." + field)
    isolation = check._record(provisioning["isolation_review"], {"reviewer", "reviewed_at", "method", "evidence_sha256"}, "isolation review")
    holdout = check._record(provisioning["holdout_review"], {"reviewer", "reviewed_at", "no_tuning", "development_seeds_sha256"}, "holdout review")
    check._text(isolation["reviewer"], "isolation reviewer")
    check._text(holdout["reviewer"], "holdout reviewer")
    _same(isolation["method"], "separate_native_pair", "Isolation method")
    check._sha(isolation["evidence_sha256"], "isolation review evidence")
    check._check(isolation["evidence_sha256"] != provisioning["evidence_sha256"], "Provisioning and isolation review need distinct complete receipts")
    _same(holdout["no_tuning"], True, "Holdout no-tuning declaration")
    _same(holdout["development_seeds_sha256"], _hash(allocation["development_seeds"]), "Declared development seed review")
    allocated = check._timestamp(allocation["created_at"], "allocation.created_at")
    started = check._timestamp(provisioning["started_at"], "provisioning.started_at")
    finished = check._timestamp(provisioning["finished_at"], "provisioning.finished_at")
    observed = check._timestamp(receipt["observed_at"], "baseline observed_at")
    capture_start = observed - timedelta(seconds=check._number(receipt["source_read_seconds"], "baseline source_read_seconds"))
    check._check(allocated <= started <= capture_start <= observed <= finished,
                  "Allocation/provisioning/baseline capture chronology differs")
    for review in (isolation, holdout):
        check._check(finished <= check._timestamp(review["reviewed_at"], "reviewed_at"), "Review precedes completed provisioning")
    return all(value is not None for value in costs.values())


def prepare_intent(allocation, batch_id, manifest, baseline_capture, provisioning, output=None, *, evidence_origin="synthetic_test"):
    """Bind a fresh allocated baseline; retain all inputs for offline revalidation."""
    try:
        allocation = validate_allocation(allocation)
        for value in (manifest, baseline_capture, provisioning):
            _json(value)
        check._check(evidence_origin in ("synthetic_test", "recorded_native"), "Unsupported intent evidence origin")
        check._text(batch_id, "batch_id")
        candidates = [item for item in allocation["fixture_batches"] if item["batch_id"] == batch_id]
        check._check(len(candidates) == 1, "Intent must identify exactly one allocated batch")
        fixture = candidates[0]
        check._check(fixture["seed"] not in allocation["development_seeds"], "Development seed cannot become acceptance intent")
        identity_map = _manifest(manifest, fixture)
        check._record(baseline_capture, {"receipt", "tables", "observed_source"}, "baseline capture")
        check._record(baseline_capture["receipt"], VALIDATION_FIELDS, "baseline validation receipt")
        receipt, tables, _, _ = _validation(baseline_capture, manifest, identity_map["issues"])
        check._check(tables is not None and receipt["validation_status"] == "valid", "Acceptance baseline must have valid complete source data")
        _same(receipt["simulators"], manifest["simulator_versions"], "Seed/readback simulator identities")
        costs_complete = _provisioning(provisioning, allocation, batch_id, receipt)
        # Reuse preparation as private derivation, not a relabelled caller-supplied
        # development artifact. Fix the authored timestamp for exact revalidation.
        with tempfile.TemporaryDirectory(prefix="contextbase-intent-") as directory:
            phase_plan = prepare_phases(manifest, Path(directory) / "derived.json")
        phase_plan["created_at"] = provisioning["finished_at"]
        phase_plan["plan_sha256"] = _hash({key: value for key, value in phase_plan.items() if key != "plan_sha256"})
        _plan(phase_plan, "baseline")
        templates = compile_templates(phase_plan)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed acceptance intent inputs; no intent was written.") from exc
    intent = {"spec": SPEC, "kind": "acceptance_fixture_intent", "created_at": provisioning["finished_at"],
        "allocation_sha256": allocation["allocation_sha256"], "batch_id": batch_id, "allocated_fixture": deepcopy(fixture),
        "manifest": deepcopy(manifest), "manifest_sha256": _hash(manifest),
        "baseline_capture": deepcopy(baseline_capture), "baseline_capture_sha256": _hash(baseline_capture),
        "provisioning": deepcopy(provisioning), "provisioning_sha256": _hash(provisioning),
        "phase_plan": phase_plan, "templates": templates, "identity_map": identity_map, "identity_map_sha256": _hash(identity_map),
        "physical_ids": {"stripe_customers": sorted(identity_map["customers"]), "stripe_charges": sorted(identity_map["charges"]),
                         "stripe_refunds": sorted(identity_map["refunds"]), "linear_issues": sorted(identity_map["issues"]),
                         "linear_states": sorted(identity_map["states"]), "linear_owners": sorted(identity_map["owners"]),
                         "linear_teams": [manifest["team_id"]]},
        "simulator_pins": {source: {"product": product, "version": version} for source, (product, version) in PINS.items()},
        "code_hashes": _hashes(), "costs_complete": costs_complete,
        "seed_replay": {"coverage": "Current shuffled_cases_v1 labels, account/charge/issue order, case policies, monetary values/refunds, flags, titles, priorities, workflow references and unassigned-owner controls.",
            "excluded_fields": ["customer_id", "charge_id", "refund_ids", "issue_id", "identifier", "owner_id", "owner_name"],
            "limitations": "Physical source/owner assignments are baseline-readback bound. Exact discovered user roster order is not recorded/replayed; refund objects and team discovery are not independently captured."},
        "evidence_origin": evidence_origin, "origin_authentication": "operator_assertion_only",
        "execution_authorized": False, "claim_eligible": False, "source_calls": 0, "model_calls": 0,
        "limitations": [
            "This is baseline fixture intent, not a frozen execution protocol, completed model study or superiority result.",
            "Isolation and no-tuning reviews are supplied declarations with references, not authenticated proof. Unique seeds/hashes alone do not prove independence or exclude undeclared exposure.",
            "Reviewers may inspect evaluator gold to validate it; allocated outcomes must not be used for product/model tuning or post-selection.",
            "Internal phase plans/templates retain their development-format flags as implementation artifacts; they do not upgrade supplied development fixtures or establish held-out status.",
            "Null provisioning costs remain unknown; they are never treated as zero and require resolution before all-in acceptance accounting.",
            "Raw source readback is retained for consistency checks. Complete pagination/native execution and review authorship require separate evidence review."]}
    intent["intent_sha256"] = _hash(intent)
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(intent, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    return intent


def validate_intent(intent, allocation):
    """Recompute the entire retained intent; hashing flag edits is insufficient."""
    try:
        _json(intent)
        check._object(intent, "intent")
        _checked_hash(intent, "intent_sha256")
        _same(intent.get("spec"), SPEC, "Unsupported acceptance intent specification")
        replay = prepare_intent(allocation, intent["batch_id"], intent["manifest"], intent["baseline_capture"],
                                intent["provisioning"], evidence_origin=intent["evidence_origin"])
        _same(intent, replay, "Acceptance intent differs from revalidated inputs/derived semantics")
        return deepcopy(intent)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed acceptance intent; revalidation failed.") from exc
