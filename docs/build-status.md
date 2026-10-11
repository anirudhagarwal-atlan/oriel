# What works today

Oriel is a **Customer 360 developer preview** for a single local operator. Its core workflow is available today: bring connector output into DuckDB, review customer identity and business context, then explore customer briefs and answers with inspectable evidence.

Start with the [sample walkthrough](quickstart.md). It works without source credentials or a model; model-driven context bootstrap and questions are optional and require explicit consent.

## Available now

| You can… | What that means today |
| --- | --- |
| Bring customer data together | Select connector resources, stage their output and publish a local DuckDB snapshot. Inspect the [connector catalog and setup guide](connectors.md). |
| Review customer identity | Approve explicit cross-source links. Conflicting or missing links stay visible; names and domains do not silently merge customers. |
| Bootstrap reusable context | Review customer links and field mappings, then let a context agent propose business definitions for approval. Definitions record calculation scope, units and currency policy. |
| Inspect a customer brief | See calculated facts alongside source passages, with references to the snapshot and reviewed context used. |
| Ask supported questions | Get scoped calculations and source passages. Supports explicit abstention when definitions or evidence are insufficient. |
| Refresh and compare | Reuse reviewed context on a new snapshot, retain prior briefs and inspect changed facts. Mapping or meaning changes require review. |

Two bundled synthetic examples cover different customer stacks: **CRM + conversations** and **billing + support**. They exercise the same workflow; the second requires neither a CRM nor a transcript source.

## Verified so far

| Check | Result | Scope |
| --- | --- | --- |
| Public installation | Passed on 11 October 2026 from a fresh anonymous clone of public commit [`38dc254`](https://github.com/anirudhagarwal-atlan/oriel/commit/38dc254b11aae972554d4fbb0401a5b54fc3ad6b). | Locked, cached dependencies on macOS ARM64 / Python 3.12. The installed package completed both sample profiles before and after refresh, outside the source checkout. No model or vendor calls. |
| Local acceptance | **58 cases passed.** | Connect, identity, context review, grounded answers, refresh, source replacement, access boundaries, browser workflows and installation. |
| Actual-model workflow | **4 cases passed across both sample profiles.** | Context bootstrap, definition review and correct answers before and after refresh, reusing reviewed context. These are bounded product checks on synthetic data. |
| Authenticated SaaS sources | **Not yet verified.** | Catalog availability and vendor-shaped samples do not establish live authentication, permissions or complete history. |
| Comparison with raw MCP | **Deferred.** | No demonstrated accuracy, cost or latency advantage is claimed. |

The local and actual-model acceptance checks ran on candidate `eaec132` on 9 October 2026. All 413 recorded test inputs are unchanged in public commit `38dc254`; the later commit added reporting documentation. The installation result above is a separate check of the actual public checkout.

Detailed revision-specific receipts and earlier attempts are summarized in the [verification history](verification-history.md). The [acceptance contract](customer-360-goal.md) defines each required gate. Live validation remains incomplete.

## Current limits

- **Local preview:** one trusted operator on a loopback server. Hosted accounts, team isolation and authenticated approval workflows are outside this release.
- **Connector compatibility:** validate the exact driver, resources and permissions for your source. Gong appears in a synthetic example and is not a verified live catalog integration. See [connector coverage](connectors.md#what-has-been-verified).
- **Answer scope:** calculations and source passages are constrained by reviewed definitions and available evidence. General narrative synthesis, health predictions and autonomous source-system actions are outside the implemented answer contract.
- **Refresh scope:** retained notes are not automatically revalidated, and local snapshots do not prove complete vendor history. There is no built-in scheduler or notification service.

## Reproduce the checks

After installing the committed connector dependencies, run the local acceptance gates:

```sh
.venv/bin/python scripts/verify_customer360.py --timeout 300
```

Browser checks require Node 22+ and an existing Chrome executable. The isolated installation check requires the locked dependencies in uv's offline cache. Missing prerequisites or skipped cases are reported as failures. Reports stay under `.oriel/verification/customer360/`.

The command deliberately reports overall completion as incomplete when model or live gates have not run. Those gates need their own authorization and setup; see [verification instructions](customer-360-goal.md#how-verification-runs).

## What comes next

1. Validate authorized live source combinations, including a third connector, through initial sync and refresh.
2. Observe users completing an own-data customer review and returning to reuse their context.
3. Verify context sharing with a receiving registry and consumer when that workflow is enabled.

[Quickstart](quickstart.md) · [Connector guide](connectors.md) · [Roadmap](roadmap.md) · [Verification history](verification-history.md)
