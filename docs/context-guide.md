# Keep the meaning behind the answer

A query can run correctly and still answer the wrong business question. “Active customer” might mean a paid contract, a login this month, or a completed workflow. Oriel gives those decisions a place to live outside a conversation.

Start with one recurring question. Inspect the source data, resolve the business rules, save the SQL and review it as a metric. The next query can use the same definition even when the data changes.

## Customer 360 context

The [customer application](customer-360-app.md) separates three reviews: cross-source customer identity, field mappings, and business definitions. Once mapped records are materialized, context agents inspect bounded committed evidence and propose definitions with units, filters and unresolved meaning. Approval pins the exact artifact. Answers use that context against a named snapshot; a factual refresh can reuse unchanged definitions, while changed mappings or meaning require new review.

The current customer answer contract computes exact sums/counts and selects attributed source passages. It does not turn schema inference into an authoritative business policy or accept arbitrary model-written factual prose. See the [customer CLI](customer-360-cli.md) for its artifact lifecycle. The generic SQL-metric workflow below remains a separate advanced path.

## What bootstrap does

`context bootstrap` reads your local tables and writes `context/draft.json`. It records schemas, row counts, column profiles, candidate keys and joins, and unresolved policy questions. Successful `source sync` normally performs this step automatically.

For example, it can measure whether an account identifier is unique and how many child records have no matching parent. It cannot establish that a login means activation, that a contract counts as revenue, or that two differently named identifiers represent the same customer. Those decisions need evidence and review.

| Artifact | What it means | What happens next |
| --- | --- | --- |
| Context draft | Observations and candidates from this snapshot | Inspect identity, completeness and unresolved business questions |
| Metric proposal | Your SQL and declared rules, with an execution witness | Review the population, grain, exclusions, units and time window |
| Approved metric | An exact locally reviewed definition and version | Execute it again; revise through a new proposal when meaning changes |
| Exported bundle | Definitions and supporting artifacts for another consumer | Review what is shared and map it to that consumer's data and registry |

Local approval records a supplied reviewer name and the exact proposal hash. It is a review assertion, not authenticated organizational certification.

## One example, two kinds of change

The [CRM and product recipe](tool-exports.md) asks about unused seats at active external organizations with less than 50% activation. Its definition states what counts as activation, how to join organizations to product workspaces, how to deduplicate people, and which observation period is complete.

**The data changes.** New activity arrives. The answer changes from 51 unused seats to 8, while the approved metric and context version stay the same.

**The meaning changes.** A team decides that exactly 50% activation should now qualify. That is a definition revision. Propose and review new SQL instead of silently changing the existing metric. The previous context remains in history.

This separation makes recurring analysis inspectable: a changed answer can come from new data or a changed policy, and those should be distinguishable.

## A review worth doing

Before approving a metric, answer these questions:

1. What entity does each row represent, and can joins multiply it?
2. Which records qualify, which are excluded, and why?
3. Which timestamps and calendar boundaries apply?
4. Does missing data mean zero, unknown, or an incomplete export?
5. Who owns the definition, and which known examples should it match?

Bring existing analyst SQL, BI definitions and domain-owner decisions into that review. Oriel does not automatically ingest query histories or discover authoritative business policy. The [quickstart](quickstart.md) shows a complete proposal and approval; the [reference](context-and-registry.md) explains exact hash and stale-proposal checks.

## What another agent can reuse

`bundle export` writes a local directory containing context, observed schema models, metric definitions, a talk-to-data skill, a registry handoff map and file hashes. It excludes source table rows, the database and credentials. Schema names, SQL and business rules may still be sensitive.

The included example verifies reuse in a separate process against separately imported data. It does not prove that every model selects the right metric or that the bundle works with unrelated schemas. A receiving agent needs access to compatible data and the definition's scope.

You can use the files directly. When a team needs discovery, access controls, distribution or coordinated updates, the bundle becomes an input to a registry workflow. The exported registry payloads remain an integration boundary: no remote publication happens automatically, and a live registry create/read/update round trip has not been verified.

**Try it:** [review and reuse the example](quickstart.md) · [inspect the exact bundle contract](context-and-registry.md#portable-export-and-agent-registry-handoff).
