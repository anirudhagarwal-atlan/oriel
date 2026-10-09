"""Reviewable context and reports for the synthetic Rystic recipe."""
from __future__ import annotations

from .context import bootstrap, propose_metric, run_metric, load_context, _digest, _write_json
from .rystic import snapshot_info
from .rystic_recipe import ACCOUNT_REPORT_SQL, NET_EXPOSED_SQL, NAMESPACE

METRIC_NAME = "net_exposed_collected_usd"
RULES = [
    "Scope only the explicitly marked contextbase_stripe_linear_v1 synthetic cohort.",
    "Net collected payments are succeeded, paid, captured, undisputed USD charge amounts minus refunds; do not annualize or call this ARR.",
    "Exclude internal accounts and non-USD charges; do not combine currencies or infer conversion rates.",
    "Unresolved means workflow state type backlog, unstarted or started; urgent/high means priority 1 or 2.",
    "Aggregate payments and qualifying issues separately per account, then join; multiple issues must not multiply payments.",
    "Use explicit account IDs carried in source metadata and issue descriptions; ambiguous or orphan mappings block sync.",
    "Report positive net collected payments over the imported fixture history as of the snapshot; this is historical payment association, not predicted revenue loss.",
    "Keep an unassigned owner explicit. Missing payment flags or workflow definitions are errors, not default business decisions.",
]


def propose_context(workspace, owner="demo_policy_owner"):
    info = snapshot_info(workspace)
    if not info or info["namespace"] != NAMESPACE:
        raise ValueError("Sync the Rystic recipe before proposing its context.")
    draft = bootstrap(workspace)
    draft.update({"synthetic": True, "recipe": NAMESPACE,
                  "provenance": "Observed Rystic source schema plus an explicitly authored synthetic recipe policy; business semantics are not inferred from column names.",
                  "proposed_business_rules": RULES,
                  "source_snapshot": info,
                  "unresolved_questions": ["Review the proposed payment, currency, priority, state and identity rules before approving the metric."]})
    _write_json(workspace.root / "context" / "draft.json", draft)
    proposal = propose_metric(workspace, METRIC_NAME, NET_EXPOSED_SQL,
        "Net collected USD associated with synthetic external customers having unresolved urgent/high Linear issues.",
        unit="USD", grain="One customer account after independent payment and issue aggregation.",
        rules=RULES, owner=owner)
    proposal["synthetic"] = True
    proposal["definition"]["extensions"]["contextbase"]["synthetic"] = True
    proposal["definition"]["provenance"].update({"source": "rystic_synthetic_recipe_policy",
        "derived_from": ["observed Stripe and Linear simulator snapshots", "reviewable synthetic recipe policy"]})
    proposal["definition"]["extensions"]["contextbase"]["recipe"] = NAMESPACE
    proposal["approval_sha256"] = _digest({key: value for key, value in proposal.items() if key != "approval_sha256"})
    _write_json(workspace.root / "context" / "candidates" / (METRIC_NAME + ".json"), proposal)
    return {"status": "candidate", "schema_draft": str(workspace.root / "context" / "draft.json"),
            "proposal": proposal, "approval_required": True,
            "next": "Review the rules and SQL, then run metric approve with this exact approval_sha256."}


def report(workspace):
    before = snapshot_info(workspace)
    context = load_context(workspace)
    metric = next((item for item in context["metrics"] if item["name"] == METRIC_NAME), None)
    if not metric or metric.get("sql", "").strip().rstrip(";") != NET_EXPOSED_SQL.strip().rstrip(";"):
        raise ValueError("Approve the recipe metric before this report. A changed definition needs its own matching account report.")
    accounts = workspace.query(ACCOUNT_REPORT_SQL, max_rows=1000)
    total = run_metric(workspace, METRIC_NAME)
    after = snapshot_info(workspace)
    if before != after:
        raise ValueError("Snapshot changed while reporting; retry after sync completes.")
    return {"question": "Which paying customers have unresolved urgent/high issues, what net collected USD do they represent, and who owns the issues?",
            "synthetic": True, "accounts": accounts["rows"], "account_count": accounts["row_count"],
            "metric": total, "snapshot": after,
            "account_sql": ACCOUNT_REPORT_SQL,
            "interpretation": "Historical payment association with unresolved issues; not ARR, churn probability or predicted loss."}


def preview(workspace):
    info = snapshot_info(workspace)
    if not info or info["namespace"] != NAMESPACE:
        raise ValueError("Sync Rystic or load-example first.")
    accounts = workspace.query(ACCOUNT_REPORT_SQL, max_rows=1000)
    total = workspace.query(NET_EXPOSED_SQL)
    if snapshot_info(workspace) != info:
        raise ValueError("Snapshot changed while previewing; retry after sync completes.")
    return {"status": "provisional", "synthetic": True, "approval_required": True,
            "accounts": accounts["rows"], "account_count": accounts["row_count"],
            "net_collected_usd": total["rows"][0][METRIC_NAME],
            "rules": RULES, "sql": ACCOUNT_REPORT_SQL, "snapshot": info,
            "next": "Review the explicit recipe policy using rystic propose-context before approving or reusing its metric."}
