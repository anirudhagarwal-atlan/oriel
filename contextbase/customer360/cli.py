"""Operator commands for the Customer 360 data and review foundation."""
from __future__ import annotations

import json
from pathlib import Path


def add_parser(subparsers):
    customer = subparsers.add_parser("customer", help="Review customer identity and source mappings over committed DuckDB data")
    commands = customer.add_subparsers(dest="customer_action", required=True)
    app = commands.add_parser("app", help="Open the local Customer 360 application over a private workspace")
    app.add_argument("--port", type=int, default=8787)
    create = commands.add_parser("create", help="Create a workspace customer independent of vendor identifiers")
    create.add_argument("--label", required=True)
    commands.add_parser("list", help="List this workspace's customers")

    links = commands.add_parser("links", help="Propose and review explicit cross-source customer links")
    link_commands = links.add_subparsers(dest="customer_operation", required=True)
    link_propose = link_commands.add_parser("propose")
    link_propose.add_argument("--file", required=True, help="Local JSON array of explicit source links")
    _approval(link_commands)

    snapshot = commands.add_parser("snapshot", help="Pin selected committed connector publications and their freshness policy")
    snapshot.add_argument("--source", action="append", required=True, dest="sources")
    snapshot.add_argument("--max-age-seconds", type=int, default=86400)
    snapshot.add_argument("--max-skew-seconds", type=int, default=3600)

    profile = commands.add_parser("profile", help="Propose, review or inspect declarative source-to-customer mappings")
    profile_commands = profile.add_subparsers(dest="customer_operation", required=True)
    profile_propose = profile_commands.add_parser("propose")
    profile_propose.add_argument("--file", required=True, help="Local JSON profile with exact observed column mappings")
    profile_propose.add_argument("--snapshot", required=True)
    _approval(profile_commands)
    profile_show = profile_commands.add_parser("show")
    profile_show.add_argument("name")

    materialize = commands.add_parser("materialize", help="Publish reviewed customer records with retained evidence history")
    materialize.add_argument("--profile", required=True)
    materialize.add_argument("--snapshot", required=True)
    records = commands.add_parser("records", help="Read mapped records for one customer after checking snapshot and review versions")
    records.add_argument("--profile", required=True)
    records.add_argument("--customer", required=True)
    records.add_argument("--snapshot", required=True)

    context = commands.add_parser("context", help="Bootstrap, review and reuse business definitions")
    context_commands = context.add_subparsers(dest="customer_operation", required=True)
    context_bootstrap = context_commands.add_parser("bootstrap", help="Ask the configured model to propose business context from the committed snapshot")
    _scope(context_bootstrap)
    _model_options(context_bootstrap)
    context_bootstrap.add_argument("--intent", help="Optional single-line task request for the context agent; does not approve definitions or measure policy")
    context_propose = context_commands.add_parser("propose", help="Propose explicitly authored business definitions for review")
    _scope(context_propose)
    context_propose.add_argument("--file", required=True, help="JSON object with definitions and gaps")
    _approval(context_commands)
    context_show = context_commands.add_parser("show")
    _scope(context_show)
    context_export = context_commands.add_parser("export-template", help="Export structural definitions without source text, customer data or filter literals")
    _scope(context_export)
    context_export.add_argument("--output", required=True)

    brief = commands.add_parser("brief", help="Read a customer brief from reviewed definitions and evidence")
    _scope(brief, customer=True)
    metric = commands.add_parser("metric", help="Calculate one reviewed definition with query and evidence receipts")
    _scope(metric, customer=True)
    metric.add_argument("definition_name")
    ask = commands.add_parser("ask", help="Ask the configured model a customer-scoped question using reviewed context")
    _scope(ask, customer=True)
    ask.add_argument("question")
    _model_options(ask)
    evidence = commands.add_parser("evidence", help="Inspect an exact supporting text excerpt for this customer")
    _scope(evidence, customer=True)
    evidence.add_argument("evidence_id")
    evidence.add_argument("--field", default="text")
    evidence.add_argument("--start", type=int, default=0)
    evidence.add_argument("--end", type=int)
    evidence.add_argument("--passage-name")
    evidence.add_argument("--json-pointer")
    latest = commands.add_parser("latest", help="Select latest customer passages using reviewed source timestamps")
    _scope(latest, customer=True)
    latest.add_argument("--kind", required=True)
    latest.add_argument("--limit", type=int, default=5)

    review = commands.add_parser("review", help="Save a customer brief and compare it with refreshed evidence")
    review_commands = review.add_subparsers(dest="customer_operation", required=True)
    review_save = review_commands.add_parser("save", help="Compute and retain a brief with its exact evidence")
    _scope(review_save, customer=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--profile", required=True)
    review_list.add_argument("--customer", required=True)
    review_list.add_argument("--limit", type=int, default=20)
    review_show = review_commands.add_parser("show", help="Read a retained review, explicitly separate from current facts")
    review_show.add_argument("review_id")
    review_compare = review_commands.add_parser("compare", help="Compare a retained review against a validated current snapshot")
    review_compare.add_argument("review_id")
    review_compare.add_argument("--snapshot", required=True)


def _scope(command, *, customer=False):
    command.add_argument("--profile", required=True)
    command.add_argument("--snapshot", required=True)
    if customer:
        command.add_argument("--customer", required=True)


def _model_options(command):
    command.add_argument("--allow-model-data", action="store_true", help="Authorize sending selected customer evidence to the configured model")
    command.add_argument("--max-cost-usd", type=float, default=1.0)
    command.add_argument("--max-turns", type=int, default=6)


def _approval(subparsers):
    approve = subparsers.add_parser("approve", help="Record local operator review of the exact proposal")
    approve.add_argument("proposal_id")
    approve.add_argument("--approve", required=True, help="Exact approval_sha256 from the inspected proposal")
    approve.add_argument("--reviewer", required=True)


def _read_json(filename):
    # Bound reads before parsing. Errors never echo file contents or values.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys are not allowed.")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("JSON numbers must be finite.")

    try:
        with Path(filename).open("rb") as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("JSON input exceeds the one-megabyte limit.")
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Supply a readable JSON file under one megabyte with unique keys and finite numbers.") from None


def execute(args, workspace):
    from . import identity, profiles, snapshots

    action = args.customer_action
    if action == "app":
        from .webserver import serve
        return serve(workspace, port=args.port)
    if action == "review":
        from . import reviews
        if args.customer_operation == "save":
            return reviews.save_review(workspace, args.profile, args.snapshot, args.customer)
        if args.customer_operation == "list":
            return reviews.list_reviews(workspace, args.profile, args.customer, limit=args.limit)
        if args.customer_operation == "show":
            return reviews.load_review(workspace, args.review_id)
        return reviews.compare_review(workspace, args.review_id, args.snapshot)
    if action == "create":
        return identity.create_customer(workspace, args.label)
    if action == "list":
        return identity.list_customers(workspace)
    if action == "links":
        if args.customer_operation == "propose":
            return identity.propose_links(workspace, _read_json(args.file))
        return identity.approve_links(workspace, args.proposal_id, args.approve, args.reviewer)
    if action == "snapshot":
        return snapshots.capture_snapshot(workspace, args.sources, max_age_seconds=args.max_age_seconds,
                                          max_skew_seconds=args.max_skew_seconds)
    if action == "profile":
        if args.customer_operation == "propose":
            return profiles.propose_profile(workspace, _read_json(args.file), args.snapshot)
        if args.customer_operation == "approve":
            return profiles.approve_profile(workspace, args.proposal_id, args.approve, args.reviewer)
        return profiles.load_profile(workspace, args.name)
    if action == "materialize":
        return profiles.materialize(workspace, args.profile, args.snapshot)
    if action == "records":
        return profiles.records_for_customer(workspace, args.profile, args.customer, args.snapshot)
    if action == "context":
        from . import contexts
        operation = args.customer_operation
        if operation == "bootstrap":
            from .agent import bootstrap
            return bootstrap(workspace, args.profile, args.snapshot, allow_model_data=args.allow_model_data,
                             max_turns=args.max_turns, max_cost_usd=args.max_cost_usd, intent=args.intent)
        if operation == "propose":
            value = _read_json(args.file)
            if not isinstance(value, dict) or set(value) != {"definitions", "gaps"}:
                raise ValueError("Context JSON must contain definitions and gaps.")
            return contexts.propose_context(workspace, args.profile, args.snapshot, value["definitions"], gaps=value["gaps"])
        if operation == "approve":
            return contexts.approve_context(workspace, args.proposal_id, args.approve, args.reviewer)
        if operation == "export-template":
            return contexts.export_template(workspace, args.profile, args.snapshot, args.output)
        return contexts.current_context(workspace, args.profile, args.snapshot)
    if action in ("brief", "metric", "evidence", "latest"):
        from . import answers
        if action == "brief":
            return answers.brief(workspace, args.profile, args.snapshot, args.customer)
        if action == "metric":
            return answers.execute_metric(workspace, args.profile, args.snapshot, args.customer, args.definition_name)
        if action == "latest":
            return answers.latest_evidence(workspace, args.profile, args.snapshot, args.customer, args.kind, limit=args.limit)
        return answers.read_evidence(workspace, args.profile, args.snapshot, args.customer, args.evidence_id,
                                     field=args.field, start=args.start, end=args.end,
                                     passage_name=args.passage_name, json_pointer=args.json_pointer)
    if action == "ask":
        from .agent import ask
        return ask(workspace, args.profile, args.snapshot, args.customer, args.question,
                   allow_model_data=args.allow_model_data, max_turns=args.max_turns, max_cost_usd=args.max_cost_usd)
    raise ValueError("Unknown Customer 360 command.")
