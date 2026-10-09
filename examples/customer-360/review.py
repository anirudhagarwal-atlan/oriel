"""Explicit synthetic review compatibility recipe for the checkout examples."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from contextbase.customer360 import contexts, identity, profiles, samples
from contextbase.customer360.contracts import canonical
from contextbase.workspace import Workspace

ROOT = Path(__file__).resolve().parent


def setup_review(workspace, extraction, *, approve_synthetic_fixture=False, fixture_root=ROOT):
    if approve_synthetic_fixture is not True:
        raise ValueError("This authored fixture recipe requires explicit synthetic approval; it is not a real business review.")
    plan = samples._prepare_extraction(workspace, extraction, fixture_root=fixture_root)
    for proposal in plan["links"]:
        if proposal["status"] == "proposed":
            identity.approve_links(workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-fixture-recipe")
    profile, definitions = plan["mapping"], plan["definitions"]
    snapshot = {"snapshot_id": plan["snapshot_id"]}
    customer_ids = plan["customer_ids"]
    conflicts = [proposal["proposal_id"] for proposal in plan["links"] if proposal["status"] == "conflict"]
    with workspace._connect() as conn:
        exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='_contextbase' "
                              "AND table_name='customer360_profile_approvals'").fetchone()
        existing = profiles._latest(conn, profile["name"]) if exists else None
    if existing is None or canonical(existing["candidate"]["profile"]) != canonical(profile):
        proposal = profiles.propose_profile(workspace, profile, snapshot["snapshot_id"])
        profiles.approve_profile(workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-fixture-recipe")
    materialized = profiles.materialize(workspace, profile["name"], snapshot["snapshot_id"])
    try:
        current = contexts.current_context(workspace, profile["name"], snapshot["snapshot_id"])
    except ValueError:
        current = None
    if current is None or canonical(current["definitions"]) != canonical(definitions):
        proposal = contexts.propose_context(workspace, profile["name"], snapshot["snapshot_id"], definitions,
                                            gaps=[{"code": "limited_source_coverage", "description": "Authored local fixture scope only; vendor completeness is unverified."}],
                                            origin="scripted_agent_test")
        current = contexts.approve_context(workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-fixture-recipe")
    return {"scope": "authored_synthetic_review_after_native_sqlite_extraction", "profile": profile["name"],
            "snapshot_id": snapshot["snapshot_id"], "customer_ids": customer_ids, "context_id": current["context_id"],
            "definition_sha256": current["definition_sha256"], "materialization_id": materialized["materialization_id"],
            "profile_approval": materialized["profile_approval"],
            "conflict_proposals": conflicts, "coverage": materialized["coverage"], "model_calls": 0,
            "vendor_adapter_verified": False, "human_business_review": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--extraction-receipt", required=True)
    parser.add_argument("--approve-synthetic-fixture", action="store_true")
    args = parser.parse_args()
    value = setup_review(Workspace(args.workspace), json.loads(Path(args.extraction_receipt).read_text()),
                         approve_synthetic_fixture=args.approve_synthetic_fixture)
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
