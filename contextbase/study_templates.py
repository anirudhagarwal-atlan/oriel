"""Freeze authored semantic task inputs before any future source capture exists.

``compile_templates(prepared, output=None)`` validates a complete prepared phase
plan through the existing intent/action validator, then uses the independent
scorer's task builders. The result is evaluator-only development preparation,
not a full sequential protocol, observed source evidence or an acceptance study.

``public_templates`` contains only question templates and unchanged response
schemas. Parallel ``evaluator_templates`` carries IDs, phases and semantic gold;
``tasks`` carries scheduling/control declarations separately. Never send the
complete compiler output to an agent. A later binding must replace the single
PROVENANCE_SLOT with actual source provenance and match every frozen commitment.

Builders need provenance to render their existing question format. This compiler
uses an ephemeral placeholder solely in memory, removes its exact serialized
block immediately, and excludes ONLY top-level expected.provenance from semantic
gold. No temporary source hash, source receipt or invented capture is exported.
Full schemas, all other question text and all other gold fields remain literal.
Rendering with a second distinct ephemeral provenance must produce identical
commitments, so provenance cannot silently leak into other template/gold fields.

Hashes use study_binding's strict ASCII canonical JSON codec. templates_sha256
binds the complete result excluding that field. It is not a signature. Output,
when requested, is created exclusively after validation; existing files are not
overwritten. No native, network or model calls occur.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .rystic_eval import build_guarded_task, build_phase_tasks
from .sequential_study import PROVENANCE_SLOT, task_commitment
from .study_binding import _hash, _json, _plan, _same

SPEC = "contextbase.task-templates/v1"


def compile_templates(prepared, output=None):
    """Return exact semantic templates from a validated development phase plan.

    This does not promote the input's held-out status, authorize execution, freeze
    a statistical protocol, or declare source observations that have not happened.
    """
    try:
        _json(prepared)
        # Read only the fixed phase order after the first full plan validation.
        baseline = _plan(prepared, "baseline")
        phases = {"baseline": baseline}
        order = prepared["recurring_profile"]["phase_order"]
        for phase_id in order[1:]:
            phases[phase_id] = _plan(prepared, phase_id)
        public, private, commitments = [], [], {}
        # This is an internal renderer input, never an observed source identity.
        for phase_id in order:
            phase = phases[phase_id]
            manifest = phase["intent_manifest"]
            variants = []
            for character in ("0", "f"):
                temporary = {"namespace": manifest["namespace"], "data_sha256": character * 64,
                             "snapshot_id": "rystic-" + character * 16}
                tasks = [] if phase["kind"] == "guarded" else build_phase_tasks(manifest, temporary, phase_id=phase_id)
                tasks.append(build_guarded_task(manifest, {**temporary, "receipt_kind": "source_state_validation"}, phase_id=phase_id))
                _same([task["id"] for task in tasks], phase["task_ids"], "Compiled phase task coverage")
                variants.append(tasks)
            for task, alternate in zip(*variants):
                original_public = {key: deepcopy(task[key]) for key in ("question", "answer_schema")}
                commitment = task_commitment(original_public, task["expected"], prepared["scorer_sha256"])
                alternate_public = {key: alternate[key] for key in ("question", "answer_schema")}
                _same(commitment, task_commitment(alternate_public, alternate["expected"], prepared["scorer_sha256"]),
                      "Provenance leaked outside its permitted question/gold slots")
                block = json.dumps(task["expected"]["provenance"], sort_keys=True, allow_nan=False)
                # task_commitment has already refused missing/duplicate blocks
                # and a preexisting sentinel. No broad string replacement occurs.
                template = task["question"].replace(block, PROVENANCE_SLOT, 1)
                semantic = {key: deepcopy(value) for key, value in task["expected"].items() if key != "provenance"}
                _same(_hash(template), commitment["question_template_sha256"], "Question template commitment")
                _same(_hash(semantic), commitment["semantic_expected_sha256"], "Semantic expected commitment")
                public.append({"question_template": template, "answer_schema": deepcopy(task["answer_schema"])})
                private.append({"id": task["id"], "phase_id": phase_id, "semantic_expected": semantic,
                                "semantic_expected_sha256": commitment["semantic_expected_sha256"]})
                commitments[task["id"]] = commitment
        _same([task["id"] for task in private], [task["id"] for task in prepared["tasks"]], "Complete compiled task coverage")
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("Malformed prepared plan; no task templates were written.") from exc
    compiled = {"spec": SPEC, "kind": "offline_task_templates", "evidence_origin": "authored_intent",
                "plan_sha256": prepared["plan_sha256"], "scorer_sha256": prepared["scorer_sha256"],
                "recurring_profile": deepcopy(prepared["recurring_profile"]), "tasks": deepcopy(prepared["tasks"]),
                "phase_intents": {phase_id: phases[phase_id]["intent_sha256"] for phase_id in order},
                "public_templates": public, "evaluator_templates": private, "task_commitments": commitments,
                "held_out": False, "execution_authorized": False, "claim_eligible": False,
                "source_calls": 0, "model_calls": 0,
                "limitations": [
                    "Authored semantic task inputs only; not observed source evidence, a complete statistical protocol or model results.",
                    "Future questions require actual provenance in the one explicit slot and an immutable phase binding before either arm runs.",
                    "Only rendered public question/schema entries may enter agent prompts; semantic gold and all other compiler fields are evaluator-only.",
                    "Development source plans remain development evidence; compilation does not establish held-out independence or authorize execution.",
                    "Hashes establish consistency, not authenticated provenance, native execution or business correctness."]}
    compiled["templates_sha256"] = _hash(compiled)
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as handle:
            json.dump(compiled, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    return compiled
