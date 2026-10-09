"""JSON-first CLI: stdout is machine readable; errors go to stderr."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .workspace import Workspace, init_demo, serializable


def parser():
    p = argparse.ArgumentParser(prog="oriel", description="Talk to your tools. Keep reusable agent context.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--workspace", default=".oriel", help="Local workspace directory (default: .oriel)")
    sub = p.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Create a synthetic cross-tool workspace")
    demo.add_argument("--accounts", type=int, default=40)
    demo.add_argument("--seed", type=int, default=7)
    sub.add_parser("catalog", help="Inspect snapshot schemas")
    sub.add_parser("refresh-status", help="Inspect recorded refresh evidence without contacting sources")
    query = sub.add_parser("query", help="Run a restricted SELECT")
    query.add_argument("sql")
    query.add_argument("--max-rows", type=int, default=200)
    ingest = sub.add_parser("ingest", help="Materialize a local CSV/JSON/JSONL/Parquet file")
    ingest.add_argument("path")
    ingest.add_argument("--schema", required=True)
    ingest.add_argument("--table", required=True)
    ingest.add_argument("--replace", action="store_true")
    source = sub.add_parser("source", help="Configure tool connectors, sync into DuckDB and bootstrap context")
    source_sub = source.add_subparsers(dest="action", required=True)
    source_list = source_sub.add_parser("list", help="List configured sources or the complete connector catalog")
    source_list.add_argument("--available", action="store_true")
    source_describe = source_sub.add_parser("describe", help="Inspect credentials, resources and connector limitations")
    source_describe.add_argument("source_type")
    source_plan = source_sub.add_parser("plan", help="Inspect declared output selection and fetch dependencies without connecting")
    source_plan.add_argument("source_type")
    source_plan.add_argument("--resource", action="append", default=[], help="Repeat for specific resources; omit for the connector's default selection")
    source_doctor = source_sub.add_parser("doctor", help="Inspect local dependencies and adapter readiness without connecting")
    source_doctor.add_argument("source_type", nargs="?", help="One source type; omit to inspect all catalog entries")
    source_add = source_sub.add_parser("add", help="Save credential environment references; does not contact a source")
    source_add.add_argument("source_type")
    source_add.add_argument("--name", help="Dedicated local schema and connection name")
    source_add.add_argument("--credential-env", action="append", default=None, metavar="FIELD=ENV_VAR")
    source_add.add_argument("--resource", action="append", default=[], help="Repeat for specific resources; omit for the connector's default selection")
    source_add.add_argument("--param", action="append", default=[], metavar="NAME=JSON_VALUE")
    source_add.add_argument("--path", help="One local CSV or Parquet file, for file connectors")
    source_add.add_argument("--replace-config", action="store_true", help="Explicitly replace an existing same-type connection configuration; retains its prior configuration and data")
    source_sync = source_sub.add_parser("sync", help="Extract configured source data, publish its snapshot, then bootstrap context")
    source_sync.add_argument("name")
    source_sync.add_argument("--timeout", type=int, default=300)
    source_sync.add_argument("--no-bootstrap", action="store_true")
    source_sync.add_argument("--max-requests", type=int, help="REST/GraphQL: shared maximum HTTP attempts for this sync; requires --request-seconds")
    source_sync.add_argument("--request-seconds", type=int, help="REST/GraphQL: shared request deadline in seconds; requires --max-requests")
    upstream = sub.add_parser("import-dinobase", help="Copy explicitly selected Dinobase tables read-only")
    upstream.add_argument("database")
    upstream.add_argument("--table", dest="tables", action="append", required=True)
    upstream.add_argument("--parquet-root", help="Allow views to read only local Parquet files inside this directory")
    context = sub.add_parser("context", help="Bootstrap candidate definitions and inspect approved context")
    context_sub = context.add_subparsers(dest="action", required=True)
    context_sub.add_parser("bootstrap")
    context_sub.add_parser("show")
    metric = sub.add_parser("metric", help="Run approved metrics")
    metric_sub = metric.add_subparsers(dest="action", required=True)
    run = metric_sub.add_parser("run")
    run.add_argument("name")
    propose = metric_sub.add_parser("propose", help="Witness a metric candidate from reviewed SQL")
    propose.add_argument("--name", required=True)
    propose.add_argument("--sql-file", required=True)
    propose.add_argument("--description", required=True)
    propose.add_argument("--grain", required=True)
    propose.add_argument("--unit", default="number")
    propose.add_argument("--rule", dest="rules", action="append", required=True)
    propose.add_argument("--owner", required=True)
    propose.add_argument("--synthetic-example", action="store_true", help="Label an authored synthetic example; exported definitions remain candidates. Does not authorize model access.")
    approve = metric_sub.add_parser("approve", help="Approve the exact candidate after business review")
    approve.add_argument("name")
    approve.add_argument("--approve", required=True)
    approve.add_argument("--reviewer", required=True)
    bundle = sub.add_parser("bundle", help="Export an offline skill and registry bundle")
    export = bundle.add_subparsers(dest="action", required=True).add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--workspace-id", default="example-workspace")
    transform = sub.add_parser("transform", help="Materialize a SELECT as a new models table")
    transform.add_argument("name")
    transform.add_argument("--sql", required=True)
    changes = sub.add_parser("changes", help="Preview and approve exact local row changes")
    ch = changes.add_subparsers(dest="action", required=True)
    plan = ch.add_parser("plan")
    plan.add_argument("--table", required=True)
    plan.add_argument("--key", required=True)
    plan.add_argument("--value", required=True)
    plan.add_argument("--set", dest="changes", required=True, help="JSON object of changed columns")
    apply = ch.add_parser("apply")
    apply.add_argument("plan_id")
    apply.add_argument("--approve", required=True, help="Exact SHA-256 from the reviewed preview")
    serve = sub.add_parser("serve", help="Run a read-only MCP server over stdio")
    serve.add_argument("--arm", choices=["per_source", "sql", "sql_context", "metrics", "rystic_source"], default="metrics")
    serve.add_argument("--stripe-url", default="http://127.0.0.1:18981")
    serve.add_argument("--linear-url", default="http://127.0.0.1:18982")
    serve.add_argument("--verification-policy", choices=["per_call", "evaluation_session"], default="per_call",
                       help="Source identity checks: each call, or pinned evaluation session with external pre/post checks")
    serve.add_argument("--evaluation-context", action="store_true",
                       help="Use the pinned Rystic benchmark policy without prior result witnesses in either arm")
    rystic = sub.add_parser("rystic", help="Synthetic Stripe + Linear workflow using local Rystic simulators")
    rystic_sub = rystic.add_subparsers(dest="action", required=True)
    rystic_sub.add_parser("load-example", help="Load the bundled capture from a verified synthetic Rystic run, without a live runtime")
    rystic_sub.add_parser("preview", help="Inspect a provisional answer and its unapproved recipe policy")
    for name in ("status", "seed", "sync"):
        action = rystic_sub.add_parser(name)
        action.add_argument("--stripe-url", default="http://127.0.0.1:18981")
        action.add_argument("--linear-url", default="http://127.0.0.1:18982")
        if name == "seed":
            action.add_argument("--accounts", type=int, default=12)
            action.add_argument("--seed", type=int, default=7)
            action.add_argument("--variant", choices=["demo_v1", "shuffled_cases_v1"], default="demo_v1")
        if name == "sync":
            action.add_argument("--capture-output", help="Retain this attempt's complete source validation capture in a new private local file")
    candidate = rystic_sub.add_parser("propose-context", help="Create an evidence-backed candidate using the explicit synthetic recipe policy")
    candidate.add_argument("--owner", default="demo_policy_owner")
    rystic_sub.add_parser("report", help="Run the reviewed synthetic payment/issues report")
    pilot_plan = rystic_sub.add_parser("pilot-plan", help="Freeze a development comparison plan; no model calls")
    pilot_plan.add_argument("--manifest", required=True, help="Complete rystic seed JSON receipt")
    pilot_plan.add_argument("--output", required=True)
    pilot_plan.add_argument("--max-turns", type=int, default=8)
    pilot_plan.add_argument("--max-calls", type=int, default=64)
    pilot_plan.add_argument("--max-cost-usd", type=float, default=3)
    pilot_plan.add_argument("--max-wall-seconds", type=float, default=900)
    pilot_plan.add_argument("--trial-seconds", type=float, default=180)
    pilot_plan.add_argument("--seed", type=int, default=7)
    pilot = rystic_sub.add_parser("pilot", help="Run an explicitly approved development pilot over source and workspace MCP")
    pilot.add_argument("--plan", required=True)
    pilot.add_argument("--approve", required=True, help="Exact plan SHA-256 after reviewing tasks, scope and budget")
    pilot.add_argument("--output", required=True)
    pilot.add_argument("--allow-model-data", action="store_true")
    pilot.add_argument("--stripe-url", default="http://127.0.0.1:18981")
    pilot.add_argument("--linear-url", default="http://127.0.0.1:18982")
    allocation = rystic_sub.add_parser("allocate-study", help="Allocate unseen fixture seeds offline; does not authorize or execute a study")
    allocation.add_argument("--output", required=True)
    allocation.add_argument("--master-seed", type=int, required=True)
    allocation.add_argument("--development-seed", type=int, action="append", required=True, dest="development_seeds")
    allocation.add_argument("--batches", type=int, default=30)
    allocation.add_argument("--repeats", type=int, default=3)
    allocation.add_argument("--accounts", type=int, action="append", dest="account_counts")
    allocation.add_argument("--workload", choices=["warm_v1", "recurring_v1"], default="warm_v1")
    phase_plan = rystic_sub.add_parser("phase-plan", help="Prepare authored refresh and abstention states offline; no source/model calls")
    phase_plan.add_argument("--manifest", required=True)
    phase_plan.add_argument("--output", required=True)
    binding = rystic_sub.add_parser("bind-phase", help="Check saved source evidence against phase intent and freeze development tasks offline")
    binding.add_argument("--plan", required=True)
    binding.add_argument("--phase", required=True)
    binding.add_argument("--validation", required=True, help="Complete saved capture_validation result, including observed source records")
    binding.add_argument("--snapshot", help="Matching successful full-cohort snapshot receipt; required only for valid phases")
    binding.add_argument("--evidence-origin", choices=["synthetic_test", "recorded_native"], default="synthetic_test",
                         help="Operator-declared provenance; this command cannot authenticate a native run")
    binding.add_argument("--output", required=True)
    templates = rystic_sub.add_parser("task-templates", help="Commit phase question templates and semantic gold offline, without observed source hashes")
    templates.add_argument("--plan", required=True)
    templates.add_argument("--output", required=True)
    transition = rystic_sub.add_parser("apply-phase", help="Apply one explicitly approved synthetic source transition and retain full read-back evidence")
    transition.add_argument("--plan", required=True)
    transition.add_argument("--from-phase", required=True)
    transition.add_argument("--to-phase", required=True)
    transition.add_argument("--approve-plan", required=True)
    transition.add_argument("--approve-action", required=True)
    transition.add_argument("--output", required=True)
    transition.add_argument("--stripe-url", default="http://127.0.0.1:18981")
    transition.add_argument("--linear-url", default="http://127.0.0.1:18982")
    acceptance_intent = rystic_sub.add_parser("acceptance-intent", help="Validate one allocated fresh baseline and derive acceptance intent offline")
    acceptance_intent.add_argument("--allocation", required=True)
    acceptance_intent.add_argument("--batch", required=True)
    acceptance_intent.add_argument("--manifest", required=True)
    acceptance_intent.add_argument("--capture", required=True)
    acceptance_intent.add_argument("--provisioning", required=True)
    acceptance_intent.add_argument("--evidence-origin", choices=["synthetic_test", "recorded_native"], default="synthetic_test")
    acceptance_intent.add_argument("--output", required=True)
    acceptance_protocol = rystic_sub.add_parser("acceptance-protocol", help="Freeze all allocated intents and reviewed power/accounting settings offline")
    acceptance_protocol.add_argument("--allocation", required=True)
    acceptance_protocol.add_argument("--intents", required=True, help="JSON array covering every allocated batch in order")
    acceptance_protocol.add_argument("--settings", required=True, help="Explicit model, context, power review, accounting review and budgets")
    acceptance_protocol.add_argument("--historical-context-events", help="Actual earlier context authoring/review events to freeze unchanged")
    acceptance_protocol.add_argument("--output", required=True)
    acceptance_binding = rystic_sub.add_parser("acceptance-bind", help="Bind observed phase evidence to an unchanged acceptance protocol offline")
    acceptance_binding.add_argument("--protocol", required=True)
    acceptance_binding.add_argument("--batch", required=True)
    acceptance_binding.add_argument("--phase", required=True)
    acceptance_binding.add_argument("--capture", required=True)
    acceptance_binding.add_argument("--snapshot")
    acceptance_binding.add_argument("--previous-binding")
    acceptance_binding.add_argument("--transition")
    acceptance_binding.add_argument("--output", required=True)
    acceptance_execution = rystic_sub.add_parser("acceptance-execution-plan", help="Prepare exact phase engine inputs offline; does not authorize or execute them")
    acceptance_execution.add_argument("--protocol", required=True)
    acceptance_execution.add_argument("--binding", required=True)
    acceptance_execution.add_argument("--limits", help="Optional explicitly lower phase limits")
    acceptance_execution.add_argument("--output", required=True)
    acceptance_pack = rystic_sub.add_parser("acceptance-pack", help="Collect immutable local study receipts into a private digest-addressed store")
    acceptance_pack.add_argument("--protocol", required=True)
    acceptance_pack.add_argument("--collection", required=True)
    acceptance_pack.add_argument("--store", required=True)
    acceptance_pack.add_argument("--output", required=True, help="Small local reference index")
    acceptance_report = rystic_sub.add_parser("acceptance-report", help="Rescore and assess an indexed complete acceptance study offline")
    acceptance_report.add_argument("--index", required=True)
    acceptance_report.add_argument("--store", required=True)
    acceptance_report.add_argument("--output", required=True)
    acceptance_run = rystic_sub.add_parser("acceptance-run", help="Execute one approved full native study with a persistent global budget")
    acceptance_run.add_argument("--protocol", required=True)
    acceptance_run.add_argument("--cost-contract", required=True)
    acceptance_run.add_argument("--context", required=True, help="Exact frozen approved synthetic context JSON")
    acceptance_run.add_argument("--output", required=True, help="New private study directory; existing runs cannot be resumed")
    acceptance_run.add_argument("--approve", required=True, help="Exact original protocol SHA-256")
    acceptance_run.add_argument("--allow-model-data", action="store_true")
    acceptance_run.add_argument("--allow-source-transitions", action="store_true")
    phase_pilot_plan = rystic_sub.add_parser("phase-pilot-plan", help="Freeze one observed development phase offline, including guarded tasks")
    phase_pilot_plan.add_argument("--prepared-plan", required=True)
    phase_pilot_plan.add_argument("--phase", required=True)
    phase_pilot_plan.add_argument("--capture", required=True)
    phase_pilot_plan.add_argument("--output", required=True)
    phase_pilot_plan.add_argument("--evidence-origin", choices=["synthetic_test", "recorded_native"], default="synthetic_test")
    phase_pilot_plan.add_argument("--max-turns", type=int, default=8)
    phase_pilot_plan.add_argument("--max-calls", type=int, default=80)
    phase_pilot_plan.add_argument("--max-cost-usd", type=float, default=3)
    phase_pilot_plan.add_argument("--max-wall-seconds", type=float, default=900)
    phase_pilot_plan.add_argument("--trial-seconds", type=float, default=180)
    phase_pilot_plan.add_argument("--seed", type=int, default=7)
    phase_pilot = rystic_sub.add_parser("phase-pilot", help="Run an approved observed development phase through the common agent loop")
    phase_pilot.add_argument("--plan", required=True)
    phase_pilot.add_argument("--approve", required=True)
    phase_pilot.add_argument("--output", required=True)
    phase_pilot.add_argument("--allow-model-data", action="store_true")
    phase_pilot.add_argument("--stripe-url", default="http://127.0.0.1:18981")
    phase_pilot.add_argument("--linear-url", default="http://127.0.0.1:18982")
    study_report = rystic_sub.add_parser("study-report", help="Evaluate complete paired study receipts offline; missing evidence is inconclusive")
    study_report.add_argument("--manifest", required=True)
    study_report.add_argument("--observations", required=True)
    study_report.add_argument("--output", required=True)
    ask = sub.add_parser("ask", help="Ask the configured Claude model using local read-only tools")
    ask.add_argument("question")
    ask.add_argument("--allow-model-data", action="store_true")
    ask.add_argument("--max-cost-usd", type=float, default=1)
    ask.add_argument("--max-turns", type=int, default=6)
    bench = sub.add_parser("benchmark", help="Replay plans or run the configured model against fixed synthetic questions")
    bench.add_argument("--mode", choices=["replay", "model"], default="replay")
    bench.add_argument("--output", default="benchmark-runs/replay.json")
    bench.add_argument("--page-size", type=int, default=100)
    bench.add_argument("--max-turns", type=int, default=4)
    bench.add_argument("--max-calls", type=int, default=48)
    bench.add_argument("--max-cost-usd", type=float, default=3)
    from .customer360.cli import add_parser as add_customer_parser
    add_customer_parser(sub)
    return p


def execute(args):
    if args.command == "source" and args.action == "plan":
        from .connectors import describe
        from .connectors.policy import selection_plan
        return selection_plan(describe(args.source_type), args.resource)
    if args.command == "customer" and args.customer_action == "app" and Path(args.workspace).expanduser().is_symlink():
        raise ValueError("Select a real private application workspace directory, not a symbolic link.")
    w = Workspace(args.workspace)
    if args.command == "customer":
        from .customer360.cli import execute as execute_customer
        return execute_customer(args, w)
    if args.command == "demo":
        w = init_demo(args.workspace, accounts=args.accounts, seed=args.seed)
        return {"workspace": str(w.root), "synthetic": True, "tables": w.catalog(),
                "next": ["oriel metric run at_risk_arr", "oriel ask 'How much active paid ARR is at risk?'"]}
    if args.command == "catalog":
        return {"tables": w.catalog()}
    if args.command == "refresh-status":
        from .refresh_state import refresh_status
        return refresh_status(w)
    if args.command == "query":
        return w.query(args.sql, max_rows=args.max_rows)
    if args.command == "ingest":
        return w.ingest(args.path, args.schema, args.table, replace=args.replace)
    if args.command == "source":
        from .connectors import available, describe
        from .connectors.service import add, configured, sync
        if args.action == "list":
            return available() if args.available else configured(w)
        if args.action == "describe":
            return describe(args.source_type)
        if args.action == "doctor":
            from .connectors.readiness import inspect
            return inspect(args.source_type)
        if args.action == "sync":
            budget = None
            if args.max_requests is not None or args.request_seconds is not None:
                if args.max_requests is None or args.request_seconds is None:
                    raise ValueError("Use --max-requests and --request-seconds together.")
                budget = {"max_requests": args.max_requests, "max_seconds": args.request_seconds}
            return sync(w, args.name, timeout=args.timeout, bootstrap_context=not args.no_bootstrap,
                        request_budget=budget)
        def pairs(values, parse_values=False):
            result = {}
            for value in values:
                key, separator, item = value.partition("=")
                if not separator or not key or key in result:
                    raise ValueError("Use unique NAME=VALUE pairs.")
                result[key] = json.loads(item) if parse_values else item
            return result
        return add(w, args.source_type, name=args.name,
                   credential_env=pairs(args.credential_env) if args.credential_env is not None else None,
                   resources=args.resource, params=pairs(args.param, True), path=args.path, replace_config=args.replace_config)
    if args.command == "import-dinobase":
        return w.import_dinobase(args.database, args.tables, parquet_root=args.parquet_root)
    if args.command == "context":
        from .context import bootstrap, load_context
        return bootstrap(w) if args.action == "bootstrap" else load_context(w)
    if args.command == "metric":
        from .context import run_metric, propose_metric, approve_metric
        if args.action == "run":
            return run_metric(w, args.name)
        if args.action == "propose":
            return propose_metric(w, args.name, Path(args.sql_file).read_text(), args.description,
                                  unit=args.unit, grain=args.grain, rules=args.rules, owner=args.owner,
                                  synthetic_example=args.synthetic_example)
        return approve_metric(w, args.name, args.approve, args.reviewer)
    if args.command == "bundle":
        from .context import export_bundle
        return export_bundle(w, args.output, workspace_id=args.workspace_id)
    if args.command == "transform":
        return w.transform(args.name, args.sql)
    if args.command == "changes":
        return w.plan_change(args.table, args.key, args.value, json.loads(args.changes)) if args.action == "plan" else w.apply_change(args.plan_id, args.approve)
    if args.command == "serve":
        from .mcp import serve
        if args.arm == "rystic_source":
            from .rystic import RysticPair
            from .rystic_tools import RysticToolkit
            toolkit = RysticToolkit(w, RysticPair(args.stripe_url, args.linear_url),
                                   verification_policy=args.verification_policy)
        else:
            if args.verification_policy != "per_call":
                raise ValueError("Source verification policy applies only to the rystic_source arm.")
            from .tools import Toolkit
            toolkit = Toolkit(w, args.arm)
        if args.evaluation_context:
            if args.arm not in {"rystic_source", "sql_context"}:
                raise ValueError("Evaluation context is available only for the two Rystic comparison arms.")
            from .evaluation_context import EvaluationContextToolkit
            toolkit = EvaluationContextToolkit(w, toolkit)
        serve(w, toolkit=toolkit)
        return None
    if args.command == "rystic":
        from .rystic import RysticPair, save_receipt, snapshot_info, sync_snapshot, load_example
        from .rystic_recipe import NAMESPACE, RecipeSeedError, seed_recipe
        if args.action == "allocate-study":
            from .study_allocation import allocate_study
            plan = allocate_study(args.output, master_seed=args.master_seed, development_seeds=args.development_seeds,
                batches=args.batches, repeats=args.repeats, account_counts=args.account_counts or (12, 36, 84), workload=args.workload)
            return {key: plan[key] for key in ("kind", "allocation_sha256", "planned_trials", "model_calls",
                "execution_authorized", "release_claim_eligible", "model_budget_usd", "required_before_acceptance")}
        if args.action == "phase-plan":
            from .study_phases import prepare_phases
            from .rystic_benchmark import _read
            plan = prepare_phases(_read(args.manifest), args.output)
            return {"output": args.output, "plan_sha256": plan["plan_sha256"], "kind": plan["kind"],
                    "phases": plan["recurring_profile"]["phase_order"], "tasks": len(plan["tasks"]),
                    "source_calls": 0, "model_calls": 0, "execution_authorized": False, "claim_eligible": False}
        if args.action == "study-report":
            from .study_report import evaluate_study
            from .sequential_study import SPEC as SEQUENTIAL_SPEC, evaluate_sequential_study
            from .rystic_benchmark import _read, _write, STUDY_EVIDENCE_MAX_BYTES
            manifest = _read(args.manifest)
            observations = _read(args.observations, max_bytes=STUDY_EVIDENCE_MAX_BYTES)
            sequential = isinstance(manifest, dict) and manifest.get("spec") == SEQUENTIAL_SPEC
            evaluate = evaluate_sequential_study if sequential else evaluate_study
            report = evaluate(manifest, observations)
            _write(args.output, report, exclusive=True)
            return {"output": args.output, "statistical_acceptance": report["statistical_acceptance"],
                "claim_eligible": report["claim_eligible"], "charter_ready": report["charter_ready"],
                "charter_missing": report["charter_missing"], "reasons": report["reasons"]}
        if args.action == "bind-phase":
            from .study_binding import bind_phase
            from .rystic_benchmark import _read
            packet = bind_phase(_read(args.plan), args.phase, _read(args.validation), args.output,
                                snapshot=_read(args.snapshot) if args.snapshot else None,
                                evidence_origin=args.evidence_origin)
            return {"output": args.output, "phase_id": packet["phase_id"],
                    "binding_sha256": packet["binding_sha256"], "tasks": len(packet["public_tasks"]),
                    "evidence_origin": packet["evidence_origin"], "source_calls": 0, "model_calls": 0,
                    "execution_authorized": False, "claim_eligible": False, "held_out": False}
        if args.action == "task-templates":
            from .study_templates import compile_templates
            from .rystic_benchmark import _read
            result = compile_templates(_read(args.plan), args.output)
            return {"output": args.output, "spec": result["spec"],
                    "templates_sha256": result["templates_sha256"], "tasks": len(result["tasks"]),
                    "source_calls": 0, "model_calls": 0, "execution_authorized": False,
                    "held_out": False, "claim_eligible": False}
        if args.action == "apply-phase":
            from .study_transitions import apply_transition
            from .rystic_benchmark import _read
            result = apply_transition(RysticPair(args.stripe_url, args.linear_url), _read(args.plan),
                args.from_phase, args.to_phase, args.output,
                approve_plan=args.approve_plan, approve_action=args.approve_action)
            return {"output": args.output, **{key: result[key] for key in (
                "spec", "outcome", "receipt_sha256", "from_phase", "to_phase",
                "before_state_matches", "after_state_matches", "write_attempts",
                "write_responses_confirmed", "elapsed_seconds", "workspace_imported",
                "claim_eligible", "held_out", "model_calls")}}
        if args.action == "acceptance-intent":
            from .acceptance_intent import prepare_intent
            from .rystic_benchmark import _read
            result = prepare_intent(_read(args.allocation), args.batch, _read(args.manifest),
                _read(args.capture), _read(args.provisioning), args.output, evidence_origin=args.evidence_origin)
            return {"output": args.output, **{key: result[key] for key in (
                "spec", "batch_id", "intent_sha256", "costs_complete", "evidence_origin",
                "execution_authorized", "claim_eligible", "source_calls", "model_calls")}}
        if args.action == "acceptance-protocol":
            from .acceptance_protocol import freeze_protocol
            from .rystic_benchmark import _read, STUDY_EVIDENCE_MAX_BYTES
            result = freeze_protocol(_read(args.allocation), _read(args.intents, max_bytes=STUDY_EVIDENCE_MAX_BYTES),
                _read(args.settings), args.output,
                historical_context_events=_read(args.historical_context_events) if args.historical_context_events else None)
            return {"output": args.output, "spec": result["spec"], "protocol_sha256": result["protocol_sha256"],
                    "batches": len(result["batches"]), "planned_runs": len(result["planned"]),
                    "budgets": result["budgets"], "provisioning_spend_usd": result["provisioning_spend_usd"],
                    "evidence_origin": result["evidence_origin"], "execution_authorized": False,
                    "claim_eligible": False, "source_calls": 0, "model_calls": 0}
        if args.action == "acceptance-bind":
            from .acceptance_binding import bind_acceptance_phase
            from .rystic_benchmark import _read, STUDY_EVIDENCE_MAX_BYTES
            result = bind_acceptance_phase(_read(args.protocol, max_bytes=STUDY_EVIDENCE_MAX_BYTES),
                args.batch, args.phase, _read(args.capture), args.output,
                snapshot=_read(args.snapshot) if args.snapshot else None,
                previous_binding=_read(args.previous_binding, max_bytes=STUDY_EVIDENCE_MAX_BYTES) if args.previous_binding else None,
                transition=_read(args.transition, max_bytes=STUDY_EVIDENCE_MAX_BYTES) if args.transition else None)
            return {"output": args.output, **{key: result[key] for key in (
                "spec", "batch_id", "phase_id", "protocol_sha256", "binding_sha256", "evidence_origin",
                    "execution_authorized", "claim_eligible", "source_calls", "model_calls")}}
        if args.action == "acceptance-execution-plan":
            from .acceptance_report import prepare_execution_plan
            from .rystic_benchmark import _read, _write, STUDY_EVIDENCE_MAX_BYTES
            result = prepare_execution_plan(_read(args.protocol, max_bytes=STUDY_EVIDENCE_MAX_BYTES),
                _read(args.binding, max_bytes=STUDY_EVIDENCE_MAX_BYTES),
                limits=_read(args.limits) if args.limits else None)
            _write(args.output, result, exclusive=True)
            return {"output": args.output, "plan_sha256": result["plan_sha256"], "planned_trials": len(result["queue"]),
                    "execution_authorized": False, "source_calls": 0, "model_calls": 0}
        if args.action == "acceptance-pack":
            from .acceptance_report import store_collection
            from .evidence_store import EvidenceStore
            from .rystic_benchmark import _read, _write, STUDY_EVIDENCE_MAX_BYTES
            result = store_collection(EvidenceStore(args.store), _read(args.protocol, max_bytes=STUDY_EVIDENCE_MAX_BYTES),
                _read(args.collection, max_bytes=STUDY_EVIDENCE_MAX_BYTES))
            _write(args.output, result, exclusive=True)
            return {"output": args.output, "phases": len(result["phases"]), "source_calls": 0, "model_calls": 0,
                    "claim_eligible": False, "execution_authorized": False}
        if args.action == "acceptance-report":
            from .acceptance_report import load_collection, build_report
            from .evidence_store import EvidenceStore
            from .rystic_benchmark import _read, _write
            result = build_report(*load_collection(EvidenceStore(args.store), _read(args.index)))
            _write(args.output, result, exclusive=True)
            return {"output": args.output, **{key: result[key] for key in
                    ("statistical_acceptance", "charter_ready", "claim_eligible", "reasons")}}
        if args.action == "acceptance-run":
            from .study_controller import run_study
            from .rystic_benchmark import _read, STUDY_EVIDENCE_MAX_BYTES
            result = run_study(_read(args.protocol, max_bytes=STUDY_EVIDENCE_MAX_BYTES), _read(args.cost_contract),
                _read(args.context), args.output, approve=args.approve, allow_model_data=args.allow_model_data,
                allow_source_transitions=args.allow_source_transitions)
            return {**{key: result[key] for key in ("output", "outcome", "completed_phases", "recorded_phases", "unexecuted_trials",
                    "statistical_acceptance", "reasons", "claim_eligible", "automatic_resume_allowed")},
                    "budget_journal_known_total_usd": result["budget"]["known_total_cost_usd"],
                    "budget_journal_known_comparison_usd": result["budget"]["known_comparison_cost_usd"],
                    "budget_journal_complete": "receipt_storage_failed" not in result["budget"]["blocked_reasons"],
                    "model_call_reservations": result["budget"]["model_calls"]}
        if args.action == "phase-pilot-plan":
            from .phase_benchmark import freeze_phase_plan
            from .rystic_benchmark import _read
            plan = freeze_phase_plan(w, _read(args.prepared_plan), args.phase, _read(args.capture), args.output,
                evidence_origin=args.evidence_origin, max_turns=args.max_turns, max_calls=args.max_calls,
                max_cost_usd=args.max_cost_usd, max_wall_seconds=args.max_wall_seconds,
                trial_seconds=args.trial_seconds, seed=args.seed)
            return {"output": args.output, "kind": plan["kind"], "plan_sha256": plan["plan_sha256"],
                    "phase_id": plan["phase_id"], "planned_trials": len(plan["queue"]), "budgets": plan["budgets"],
                    "model_calls": 0, "source_calls": 0, "release_evidence": False}
        if args.action == "phase-pilot":
            from .phase_benchmark import run_phase_pilot
            from .rystic_benchmark import _read
            result = run_phase_pilot(w, RysticPair(args.stripe_url, args.linear_url), _read(args.plan), args.output,
                approve=args.approve, allow_model_data=args.allow_model_data)
            return {"output": args.output, "kind": result["kind"], "summary": result["summary"],
                    "stop_reason": result["stop_reason"], "release_gates": result["release_gates"]}
        if args.action == "load-example":
            return load_example(w)
        if args.action in {"propose-context", "report", "preview"}:
            from .rystic_workflow import propose_context, report, preview
            if args.action == "propose-context":
                return propose_context(w, args.owner)
            return report(w) if args.action == "report" else preview(w)
        if args.action == "pilot-plan":
            from .rystic_benchmark import freeze_plan, _read
            plan = freeze_plan(w, _read(args.manifest), args.output, max_turns=args.max_turns,
                max_calls=args.max_calls, max_cost_usd=args.max_cost_usd,
                max_wall_seconds=args.max_wall_seconds, trial_seconds=args.trial_seconds, seed=args.seed)
            return {"output": args.output, "kind": plan["kind"], "plan_sha256": plan["plan_sha256"],
                "planned_trials": len(plan["queue"]), "budgets": plan["budgets"],
                "release_evidence": False, "model_calls": 0}
        if args.action == "pilot":
            from .rystic_benchmark import run_pilot, _read
            result = run_pilot(w, RysticPair(args.stripe_url, args.linear_url), _read(args.plan),
                args.output, approve=args.approve, allow_model_data=args.allow_model_data)
            return {"output": args.output, "kind": result["kind"], "summary": result["summary"],
                "stop_reason": result["stop_reason"], "release_gates": result["release_gates"]}
        pair = RysticPair(args.stripe_url, args.linear_url)
        if args.action == "status":
            return {"simulators": pair.verify(), "snapshot": snapshot_info(w), "benchmark_status": "deferred"}
        if args.action == "sync":
            return sync_snapshot(w, pair, NAMESPACE, capture_output=args.capture_output)
        path = w.root / "rystic-seed.json"
        if path.exists() or (w.root / "rystic-partial-seed.json").exists():
            raise ValueError("A seed receipt already exists in this workspace; use sync or inspect the partial seed.")
        try:
            manifest = seed_recipe(pair, args.accounts, args.seed, variant=args.variant)
        except RecipeSeedError as exc:
            save_receipt(w.root / "rystic-partial-seed.json", exc.partial_manifest)
            raise
        save_receipt(path, manifest)
        return {"synthetic": True, "status": manifest["status"], "namespace": manifest["namespace"],
                "accounts": manifest["account_count"], "receipt": str(path), "next": "Run rystic sync, then rystic propose-context."}
    if args.command == "ask":
        from .agent import ask
        return ask(w, args.question, allow_model_data=args.allow_model_data, max_cost_usd=args.max_cost_usd, max_turns=args.max_turns)
    if args.command == "benchmark":
        from .benchmark import run_replay, run_model_benchmark
        if not w.is_synthetic():
            raise ValueError("The bundled benchmark requires a synthetic demo workspace.")
        if args.mode == "model":
            report = run_model_benchmark(w, Path(args.output), max_turns=args.max_turns,
                                         max_calls=args.max_calls, max_cost_usd=args.max_cost_usd)
        else:
            metadata = json.loads((w.root / "demo.json").read_text())
            report = run_replay(w, Path(args.output), page_size=args.page_size, ingest_seconds=metadata["ingest_ms"] / 1000)
        return {"output": args.output, "kind": report["kind"], "summary": report.get("summary"),
                "disclaimer": report.get("disclaimer")}
    raise ValueError("Unknown command")


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        if result is not None:
            print(json.dumps(serializable(result), indent=2, allow_nan=False))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError, RuntimeError)) else "Operation failed. Check SQL, file format, identifiers, and workspace state."
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(1)
