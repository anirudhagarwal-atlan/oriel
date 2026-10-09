"""Sequential source binding without retroactively freezing observed snapshots.

No I/O, source calls, model calls or authentication occur here. Example contract::

    protocol = {
      # Same frozen model, tasks, schedule, caps, bootstrap, power and recurring
      # profile fields as paired-study/v1, but no manifest_sha256.
      "spec": "contextbase.sequential-study/v2", "protocol_sha256": "...",
      "batches": [{"id": "batch_0", "fixture_sha256": "<independent intent>",
        "plan_sha256": "<prepared plan>",
        "phase_intents": {"baseline": "...", "refreshed": "...", "guard": "..."},
        "task_commitments": {"task_id": task_commitment(public, expected, scorer)}}]
    }
    observations = {
      # Unchanged v1 runs/accounting/phases/transitions/first_use ledgers, plus:
      "phase_bindings": [{"protocol_sha256": "...", "batch_id": "batch_0",
        "phase_id": "baseline", "previous_binding_sha256": None,
        "transition_receipt_sha256": None, "capture_observed_at": "...",
        "bound_at": "...", "packet": "<complete bind_phase packet>",
        "binding_sha256": "<hash of wrapper without this field>"}]
    }

Run provenance replaces manifest_sha256 with protocol_sha256 and
phase_binding_sha256; all other v1 identities and four task-artifact hashes remain.
Each batch has exactly one chained binding per phase, including failed/refused
refreshes, made after transition/capture and before either arm's first query.
Bindings preserve source hash kind, full public questions/schemas, semantic gold
and the exact late-bound provenance. They are evaluator records, never prompts.

Commitments, packets and wrappers use study_binding's strict ASCII canonical
JSON codec. Protocol hashes use study_report's documented UTF-8 canonical codec.
Questions use the existing scorer's exact json.dumps(provenance, sort_keys=True)
rendering (including spaces), with exactly one complete provenance block replaced
by PROVENANCE_SLOT. Only expected's top-level provenance is excluded from the
semantic commitment. Other strings, nested provenance, schema and gold stay literal.

The resolved v1 manifest is an INTERNAL STATISTICAL PROJECTION, not a newly frozen
preregistration. Its original frozen_at, schedule, costs, timings, caps, failures
and power gates are retained. Capture may be shared validation or workspace import
work; this adapter does not infer which arm owns it. Actual phase preparation
spans and allocations remain unchanged, including any first-use waiting time.
No time or spend is adjusted to make the internal projection pass.

Hashes cannot authenticate receipts or prove native capture/held-out independence.
The bind_phase packet omits source rows, so its validation_sha256 is a reference,
not independently recomputed source validation. Native verification is separate.
Current phase-binding/v1 packets are explicitly development-only, even with an
operator's recorded_native label. They always make acceptance inconclusive and
charter_ready false. A distinct future acceptance-binding contract is required;
changing their held_out or execution_authorized flags is not an upgrade path.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json

from . import study_report as v1
from .study_binding import _hash, _json, SPEC as PACKET_SPEC
from .rystic_validation import NORMALIZED_HASH_KIND, OBSERVED_HASH_KIND
from .rystic import PINS, SCHEMAS
from .rystic_recipe import FIXTURE_NAMESPACE

SPEC = "contextbase.sequential-study/v2"
REPORT_SPEC = "contextbase.sequential-study-report/v2"
PROVENANCE_SLOT = "{{CONTEXTBASE_SEQUENTIAL_PROVENANCE_V2}}"
COMMITMENT_FIELDS = {"question_template_sha256", "response_schema_sha256",
                     "semantic_expected_sha256", "scorer_sha256", "provenance_fields"}
PROTOCOL_FIELDS = {"spec", "protocol_sha256", "kind", "held_out", "evidence_origin", "frozen_at",
                   "model_id", "runtime_sha256", "context_sha256", "tasks", "batches", "repeats",
                   "planned", "caps", "bootstrap", "power", "recurring_profile"}
BATCH_FIELDS = {"id", "fixture_sha256", "plan_sha256", "phase_intents", "task_commitments"}
WRAPPER_FIELDS = {"protocol_sha256", "batch_id", "phase_id", "previous_binding_sha256",
                  "transition_receipt_sha256", "bound_at", "capture_observed_at", "packet", "binding_sha256"}
PACKET_FIELDS = {"spec", "phase_id", "public_tasks", "evaluator_tasks", "task_artifacts", "artifact_hashes",
                 "validation_receipt", "snapshot_receipt", "evidence_origin", "origin_authentication",
                 "held_out", "execution_authorized", "claim_eligible", "model_calls", "source_calls",
                 "limitations", "binding_sha256"}
PACKET_HASH_FIELDS = {"plan_sha256", "phase_intent_sha256", "scorer_sha256", "validation_sha256",
                      "observed_source_sha256", "normalized_tables_sha256", "snapshot_receipt_sha256",
                      "public_tasks_sha256", "evaluator_tasks_sha256"}
VALIDATION_FIELDS = {"receipt_kind", "namespace", "validation_status", "complete", "blockers",
                     "workspace_imported", "synthetic", "consistency", "simulators", "hash_kind",
                     "data_sha256", "observed_source_sha256", "snapshot_id", "source_rows",
                     "observed_at", "source_read_seconds"}
SNAPSHOT_FIELDS = {"namespace", "simulators", "synthetic", "data_sha256", "snapshot_id", "observed_at",
                   "source_read_seconds", "refresh_mode", "source_validation_sha256", "rows"}
STORED_SNAPSHOT_FIELDS = {"scope", "consistency", "table_sha256", "previous_snapshot_id", "data_changed"}


def _same(actual, expected, message):
    v1._check(_json(actual) == _json(expected), message)


def _hashed(record, field, label):
    v1._sha(record.get(field), label)
    _same(record[field], _hash({key: value for key, value in record.items() if key != field}),
          label + " does not match its complete record")


def task_commitment(public_task, expected, scorer_sha256):
    """Return immutable semantic/task commitments; malformed inputs raise ValueError."""
    _json(public_task)
    _json(expected)
    v1._record(public_task, {"question", "answer_schema"}, "public task")
    question = v1._text(public_task["question"], "question")
    v1._object(public_task["answer_schema"], "answer_schema")
    v1._object(expected, "expected")
    provenance = v1._object(expected.get("provenance"), "expected.provenance")
    v1._check(set(provenance) in ({"snapshot_id", "data_sha256"},
                                 {"snapshot_id", "data_sha256", "receipt_kind"}),
              "Only snapshot_id/data_sha256 and optional receipt_kind may be substituted")
    data_hash = v1._sha(provenance["data_sha256"], "provenance.data_sha256")
    guarded = "receipt_kind" in provenance
    if guarded:
        _same(provenance["receipt_kind"], "source_state_validation", "Unsupported provenance receipt_kind")
    _same(provenance["snapshot_id"], ("source-validation-" if guarded else "rystic-") + data_hash[:16],
          "Provenance snapshot alias must derive from its source hash")
    block = json.dumps(provenance, sort_keys=True, allow_nan=False)
    v1._check(PROVENANCE_SLOT not in question and question.count(block) == 1,
              "Question must contain exactly one serialized provenance block and no reserved placeholder")
    template = question.replace(block, PROVENANCE_SLOT, 1)
    return {"question_template_sha256": _hash(template),
            "response_schema_sha256": _hash(public_task["answer_schema"]),
            "semantic_expected_sha256": _hash({key: value for key, value in expected.items() if key != "provenance"}),
            "scorer_sha256": v1._sha(scorer_sha256, "scorer_sha256"),
            "provenance_fields": sorted(provenance)}


def _packet(packet, batch, phase, tasks, kind, protocol_origin):
    v1._record(packet, PACKET_FIELDS, "phase packet")
    _hashed(packet, "binding_sha256", "packet hash")
    _same(packet["spec"], PACKET_SPEC, "Unsupported phase packet spec")
    _same(packet["phase_id"], phase, "Packet phase differs from wrapper")
    origin = packet["evidence_origin"]
    v1._check(origin in ("synthetic_test", "recorded_native"), "Unsupported packet evidence origin")
    v1._check(protocol_origin != "observed" or origin == "recorded_native",
              "Observed studies cannot use synthetic phase packets")
    _same(packet["origin_authentication"], "operator_assertion_only", "Packet cannot claim authenticated origin")
    for field in ("held_out", "execution_authorized", "claim_eligible"):
        _same(packet[field], False, "Packet cannot assert " + field)
    for field in ("model_calls", "source_calls"):
        _same(packet[field], 0, "Binding itself must not claim " + field)
    hashes = v1._record(packet["artifact_hashes"], PACKET_HASH_FIELDS, "packet artifact hashes")
    for field in PACKET_HASH_FIELDS - {"normalized_tables_sha256", "snapshot_receipt_sha256"}:
        v1._sha(hashes[field], "packet." + field)
    _same(hashes["plan_sha256"], batch["plan_sha256"], "Packet prepared plan differs from frozen intent")
    _same(hashes["phase_intent_sha256"], batch["phase_intents"][phase], "Packet phase intent differs from protocol")
    receipt = v1._record(packet["validation_receipt"], VALIDATION_FIELDS, "source validation receipt")
    _same(receipt["namespace"], FIXTURE_NAMESPACE, "Unsupported source recipe namespace")
    identities = v1._object(receipt["simulators"], "simulator identities")
    v1._check(set(identities) == set(PINS), "Both pinned simulator identities are required")
    for source, (product, version) in PINS.items():
        v1._object(identities[source], "simulator identity")
        _same([identities[source].get("product"), identities[source].get("version")], [product, version], "Simulator pin differs")
    counts = v1._object(receipt["source_rows"], "source row counts")
    v1._check(set(counts) == set(SCHEMAS) and all(type(value) is int and value >= 0 for value in counts.values()),
              "Source row counts must cover each collection with nonnegative integers")
    for key, value in (("receipt_kind", "source_state_validation"), ("complete", True),
                       ("workspace_imported", False), ("synthetic", True)):
        _same(receipt[key], value, "Source validation " + key + " differs")
    guarded = kind == "guarded"
    _same(receipt["validation_status"], "blocked" if guarded else "valid", "Source phase validity differs")
    v1._list(receipt["blockers"], "source blockers", nonempty=guarded)
    v1._check(guarded or receipt["blockers"] == [], "Valid source cannot retain blockers")
    _same(receipt["hash_kind"], OBSERVED_HASH_KIND if guarded else NORMALIZED_HASH_KIND, "Source hash kind differs")
    data_hash = v1._sha(receipt["data_sha256"], "source data hash")
    _same(hashes["observed_source_sha256"], receipt["observed_source_sha256"], "Observed source hash differs")
    _same(hashes["normalized_tables_sha256"], None if guarded else data_hash, "Normalized source hash differs")
    if guarded:
        _same(data_hash, hashes["observed_source_sha256"], "Guard must bind observed invalid source state")
    _same(receipt["snapshot_id"], ("source-state-" if guarded else "rystic-") + data_hash[:16], "Receipt source alias differs")
    snapshot = packet["snapshot_receipt"]
    if guarded:
        _same(snapshot, None, "Guarded source must not claim workspace materialization")
        _same(hashes["snapshot_receipt_sha256"], None, "Guarded source must not claim snapshot receipt")
    else:
        v1._object(snapshot, "snapshot receipt")
        fields = set(snapshot) - {"receipt_kind"}
        v1._check(fields in (SNAPSHOT_FIELDS, SNAPSHOT_FIELDS | STORED_SNAPSHOT_FIELDS), "Unexpected snapshot receipt fields")
        _same(snapshot.get("receipt_kind", "workspace_snapshot"), "workspace_snapshot", "Invalid snapshot receipt kind")
        _same(hashes["snapshot_receipt_sha256"], _hash(snapshot), "Snapshot receipt hash differs")
        _same(snapshot["refresh_mode"], "full_cohort_replace", "Snapshot must replace the complete cohort")
        _same(snapshot["source_validation_sha256"], _hash(receipt), "Snapshot uses another source validation")
        for key in ("namespace", "simulators", "synthetic", "data_sha256", "snapshot_id", "observed_at", "source_read_seconds"):
            _same(snapshot[key], receipt[key], "Snapshot and validation differ: " + key)
        _same(snapshot["rows"], receipt["source_rows"], "Snapshot row counts differ")
        if "table_sha256" in snapshot:
            _same(snapshot["consistency"], receipt["consistency"], "Snapshot consistency differs")
            v1._text(snapshot["scope"], "snapshot.scope")
            table_hashes = v1._object(snapshot["table_sha256"], "snapshot.table_sha256")
            v1._check(set(table_hashes) == set(SCHEMAS), "Snapshot table hashes must cover each collection")
            for value in table_hashes.values():
                v1._sha(value, "snapshot table hash")
            v1._check(type(snapshot["data_changed"]) is bool, "snapshot.data_changed must be boolean")
            if snapshot["previous_snapshot_id"] is not None:
                v1._text(snapshot["previous_snapshot_id"], "snapshot.previous_snapshot_id")
    public = v1._list(packet["public_tasks"], "public tasks")
    private = v1._list(packet["evaluator_tasks"], "evaluator tasks")
    _same(hashes["public_tasks_sha256"], _hash(public), "Public task packet hash differs")
    _same(hashes["evaluator_tasks_sha256"], _hash(private), "Evaluator task packet hash differs")
    v1._check(len(public) == len(private) == len(tasks), "Packet has missing or additional tasks")
    artifacts = v1._object(packet["task_artifacts"], "task artifacts")
    v1._check(set(artifacts) == set(tasks), "Task artifact coverage differs")
    for identity, pub, evaluator in zip(tasks, public, private):
        v1._record(evaluator, {"id", "phase_id", "expected", "expected_sha256"}, "evaluator task")
        _same([evaluator["id"], evaluator["phase_id"]], [identity, phase], "Task order/phase differs")
        expected = evaluator["expected"]
        commitment = task_commitment(pub, expected, hashes["scorer_sha256"])
        _same(commitment, batch["task_commitments"][identity], "Task semantic gold, question template, schema or scorer changed")
        _same(expected["provenance"]["data_sha256"], data_hash, "Task provenance uses another source state")
        v1._check(not guarded or "receipt_kind" in expected["provenance"], "Guarded task must use source-validation provenance")
        if "receipt_kind" in expected["provenance"]:
            _same(expected.get("blockers"), receipt["blockers"], "Guard task and source validation blockers differ")
            _same(expected.get("status"), "abstained" if guarded else "answered", "Guard task and source validity differ")
        _same(evaluator["expected_sha256"], _hash(expected), "Expected answer hash differs")
        _same(artifacts[identity], {"question_sha256": _hash(pub["question"]), "response_schema_sha256": _hash(pub["answer_schema"]),
                                   "scorer_sha256": hashes["scorer_sha256"], "expected_sha256": _hash(expected)},
              "Final task artifact hashes differ")
    return data_hash, receipt, artifacts


def _resolve(protocol, observations):
    _json(protocol)
    _json(observations)
    v1._record(protocol, PROTOCOL_FIELDS, "protocol")
    _same(protocol["spec"], SPEC, "Unsupported sequential study specification")
    locked = v1._sha(protocol["protocol_sha256"], "protocol_sha256")
    _same(locked, v1._digest({key: value for key, value in protocol.items() if key != "protocol_sha256"}), "Protocol changed after freezing")
    frozen = v1._timestamp(protocol["frozen_at"], "frozen_at")
    v1._record(protocol["caps"], {"run_seconds", "total_cost_usd"}, "caps")
    v1._record(protocol["bootstrap"], {"seed", "replicates", "confidence"}, "bootstrap")
    v1._record(protocol["power"], {"minimum_independent_batches", "target_power", "analysis_sha256", "reviewed"}, "power")
    v1._record(protocol["recurring_profile"], {"spec", "phase_order", "phase_kinds"}, "recurring profile")
    for row in v1._list(protocol["planned"], "planned"):
        v1._record(row, {"batch_id", "task_id", "repeat", "arm"}, "planned run")
    v1._record(observations, {"runs", "batches", "phases", "transitions", "first_use", "phase_bindings"}, "observations")
    for row in v1._list(observations["runs"], "runs"):
        v1._record(row, {"batch_id", "task_id", "repeat", "arm", "correct", "started_at", "finished_at",
                        "overhead_seconds", "scored_evidence_sha256", "provenance", "attempts"}, "run")
    for row in v1._list(observations["batches"], "batch accounting"):
        v1._record(row, {"batch_id", "arm", "started_at", "finished_at", "evidence_sha256", "costs", "seconds"}, "batch accounting")
    task_phases = {}
    for task in v1._list(protocol["tasks"], "tasks"):
        v1._object(task, "task")
        fields = {"id", "weight", "phase_id", "response_kind", "control_kind"}
        v1._check(set(task) in (fields, fields | {"family"}), "Task must contain exactly declared fields and optional family")
        if "family" in task:
            v1._text(task["family"], "task.family")
        identity = v1._text(task["id"], "task.id")
        v1._check(identity not in task_phases, "Duplicate task")
        task_phases[identity] = v1._text(task["phase_id"], "task.phase_id")
    profile = v1._profile(protocol, task_phases)
    v1._check(profile is not None, "Sequential studies require the recurring profile")
    order, kinds = profile["phase_order"], profile["phase_kinds"]
    batches = {}
    for batch in v1._list(protocol["batches"], "batches"):
        v1._record(batch, BATCH_FIELDS, "protocol batch")
        identity = v1._text(batch["id"], "batch.id")
        v1._check(identity not in batches, "Duplicate protocol batch")
        batches[identity] = batch
        v1._sha(batch["plan_sha256"], "batch.plan_sha256")
        v1._check(set(v1._object(batch["phase_intents"], "phase_intents")) == set(order), "Phase intent coverage differs")
        for value in batch["phase_intents"].values():
            v1._sha(value, "phase intent hash")
        v1._check(set(v1._object(batch["task_commitments"], "task_commitments")) == set(task_phases), "Task commitment coverage differs")
        for item in batch["task_commitments"].values():
            v1._record(item, COMMITMENT_FIELDS, "task commitment")
            for field in COMMITMENT_FIELDS - {"provenance_fields"}:
                v1._sha(item[field], field)
    phases = {}
    for row in v1._list(observations["phases"], "phase accounting"):
        v1._record(row, v1.PHASE_FIELDS, "phase accounting")
        key = row["batch_id"], row["phase_id"], row["arm"]
        v1._check(key not in phases, "Duplicate phase accounting")
        phases[key] = row
    transitions = {}
    for row in v1._list(observations["transitions"], "transitions"):
        v1._record(row, v1.TRANSITION_FIELDS, "transition")
        key = row["batch_id"], row["to_phase_id"]
        v1._check(key not in transitions, "Duplicate transition")
        transitions[key] = row
    bindings, seen, packet_origins = {}, set(), set()
    expected_keys = [(batch, phase) for batch in batches for phase in order]
    wrappers = v1._list(observations["phase_bindings"], "phase bindings")
    v1._check(len(wrappers) == len(expected_keys), "Missing or additional phase binding")
    for wrapper, key in zip(wrappers, expected_keys):
        v1._record(wrapper, WRAPPER_FIELDS, "phase binding")
        _same([wrapper["batch_id"], wrapper["phase_id"]], list(key), "Phase bindings must follow declared batch/phase order exactly")
        _same(wrapper["protocol_sha256"], locked, "Phase binding names another protocol")
        _hashed(wrapper, "binding_sha256", "phase binding hash")
        batch_id, phase = key
        batch = batches[batch_id]
        tasks = [task for task, task_phase in task_phases.items() if task_phase == phase]
        source_hash, receipt, artifacts = _packet(wrapper["packet"], batch, phase, tasks, kinds[phase], protocol["evidence_origin"])
        packet_origins.add(wrapper["packet"]["evidence_origin"])
        for digest in (wrapper["binding_sha256"], wrapper["packet"]["binding_sha256"]):
            v1._check(digest not in seen, "A phase binding or packet was reused")
            seen.add(digest)
        captured = v1._timestamp(wrapper["capture_observed_at"], "capture_observed_at")
        _same(captured.isoformat(), v1._timestamp(receipt["observed_at"], "source observed_at").isoformat(), "Binding capture time differs from receipt")
        bound = v1._timestamp(wrapper["bound_at"], "bound_at")
        read_seconds = v1._number(receipt["source_read_seconds"], "source_read_seconds")
        capture_started = captured - timedelta(seconds=read_seconds)
        v1._check(frozen <= capture_started <= captured <= bound, "Capture/binding precedes protocol or has reversed chronology")
        index = order.index(phase)
        if index == 0:
            _same(wrapper["previous_binding_sha256"], None, "Baseline cannot replace a prior binding")
            _same(wrapper["transition_receipt_sha256"], None, "Baseline cannot invent a transition")
        else:
            previous = bindings[(batch_id, order[index - 1])]
            _same(wrapper["previous_binding_sha256"], previous["wrapper"]["binding_sha256"], "Phase binding chain differs")
            transition = transitions[key]
            _same(wrapper["transition_receipt_sha256"], transition["evidence_sha256"], "Binding transition receipt differs")
            v1._check(v1._timestamp(transition["finished_at"], "transition.finished_at") <= capture_started,
                      "Capture must begin after the completed transition")
        for arm in v1.ARMS:
            selected = [run for run in observations["runs"] if run["batch_id"] == batch_id and run["arm"] == arm
                        and task_phases[run["task_id"]] == phase]
            v1._check(selected and all(bound <= v1._timestamp(run["started_at"], "run.started_at") for run in selected),
                      "Binding must precede every dependent run in both arms")
        # These are shared evidence events, not a rule assigning all capture work
        # to one arm. Existing phase/accounting ledgers retain their actual spans.
        starts = [v1._timestamp(phases[(batch_id, phase, arm)]["started_at"], "phase.started_at") for arm in v1.ARMS]
        finishes = [v1._timestamp(phases[(batch_id, phase, arm)]["finished_at"], "phase.finished_at") for arm in v1.ARMS]
        v1._check(min(starts) <= capture_started and bound <= max(finishes),
                  "Capture and binding fall outside the declared phase window")
        bindings[key] = {"wrapper": wrapper, "source_hash": source_hash, "artifacts": artifacts}
    v1._check(len(packet_origins) == 1, "Mixed synthetic/native phase origins are invalid")
    resolved = deepcopy(protocol)
    del resolved["protocol_sha256"]
    resolved["spec"] = v1.SPEC
    resolved["batches"] = [{"id": batch, "fixture_sha256": values["fixture_sha256"],
        "phase_snapshots": {phase: bindings[(batch, phase)]["source_hash"] for phase in order},
        "task_artifacts": {task: bindings[(batch, phase)]["artifacts"][task] for task, phase in task_phases.items()}}
        for batch, values in batches.items()]
    resolved["manifest_sha256"] = v1._digest(resolved)
    projected = deepcopy(observations)
    del projected["phase_bindings"]
    for run in projected["runs"]:
        binding = bindings[(run["batch_id"], task_phases[run["task_id"]])]
        provenance = run["provenance"]
        expected = {"protocol_sha256": locked, "phase_binding_sha256": binding["wrapper"]["binding_sha256"],
                    "model_id": protocol["model_id"], "runtime_sha256": protocol["runtime_sha256"],
                    "context_sha256": protocol["context_sha256"], "fixture_sha256": binding["source_hash"],
                    **binding["artifacts"][run["task_id"]]}
        _same(provenance, expected, "Run provenance differs from original protocol/phase binding/task artifacts")
        del provenance["protocol_sha256"]
        del provenance["phase_binding_sha256"]
        provenance["manifest_sha256"] = resolved["manifest_sha256"]
        for digest in [run["scored_evidence_sha256"], *(attempt["evidence_sha256"] for attempt in run["attempts"])]:
            v1._check(digest not in seen, "Measurement receipt reused as phase binding")
    for ledger in ("batches", "phases", "transitions", "first_use"):
        for row in projected[ledger]:
            v1._check(row["evidence_sha256"] not in seen, "Measurement receipt reused as phase binding")
    return resolved, projected


def evaluate_sequential_study(protocol, observations):
    """Validate immutable sequential bindings, then reuse all paired-study gates."""
    report = {"spec": REPORT_SPEC, "protocol_spec": SPEC, "protocol_sha256": None,
              "statistical_acceptance": "inconclusive", "claim_eligible": False, "charter_ready": False,
              "reasons": [], "metrics": None, "paired_intervals": None, "gates": None,
              "charter_missing": ["Sequential binding evidence has not been validated."]}
    try:
        resolved, projected = _resolve(protocol, observations)
        report = v1.evaluate_study(resolved, projected)
        report.pop("manifest_sha256", None)
        report.update(spec=REPORT_SPEC, protocol_spec=SPEC, protocol_sha256=protocol["protocol_sha256"],
                      observations_sha256=v1._digest(observations), claim_eligible=False)
        report["statistical_projection"] = {"spec": v1.SPEC, "sha256": resolved["manifest_sha256"],
            "is_preregistration": False, "frozen_at": protocol["frozen_at"],
            "purpose": "Internal resolution of already committed tasks and chained phase observations for unchanged v1 statistics/accounting; not a new freeze."}
        report["limitations"].append("Sequential hashes check consistency, not authenticated native execution; phase packets retain only a reference to full source validation.")
        report["limitations"].append("Capture work ownership/shared allocation requires a separately reviewed collection policy. Supplied phase preparation and first-use waiting spans are retained without invented attribution or allocation adjustments.")
        report["sequential_binding_validated"] = True
        reason = "Development-only phase-binding/v1 packets cannot establish held-out acceptance; a distinct acceptance-binding contract is required."
        report["statistical_acceptance"] = "inconclusive"
        report["charter_ready"] = False
        report["reasons"].append(reason)
        report["charter_missing"].append(reason)
    except (v1.InvalidStudy, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        report["reasons"] = [str(exc) if isinstance(exc, v1.InvalidStudy) else "Malformed or non-finite sequential study input"]
        report["charter_missing"].extend(report["reasons"])
    return report
