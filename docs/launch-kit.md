# Oriel launch kit

Draft assets for a developer preview. The selected public repository is [anirudhagarwal-atlan/oriel](https://github.com/anirudhagarwal-atlan/oriel). Nothing on this page has been posted or sent as an announcement. Complete the [launch-readiness checks](gtm.md#launch-readiness-evidence-and-remaining-gates) and verify the linked instructions before posting. The positioning and measurement plan live in [GTM](gtm.md).

## Before publication

The README, getting-started guides, agent instructions, contribution templates and launch drafts are prepared for the public repository owned by `anirudhagarwal-atlan`:

- **Public installation checked.** The lockfile uses public PyPI registry and artifact URLs for all 59 external package records, preserving reviewed versions and hashes. Fresh base and connector installs passed on macOS ARM64 / Python 3.12 without registry overrides. Other platforms still need verification.
- Use `https://github.com/anirudhagarwal-atlan/oriel` as the repository destination and set its About text. Link announcements to the repository's [quickstart](https://github.com/anirudhagarwal-atlan/oriel/blob/main/docs/quickstart.md); a separately hosted docs site is not configured. Do not advertise a PyPI install until that package is actually published and tested.
- Assign issue triage and establish a private security-reporting channel before inviting public reports. Do not invent a support address or service commitment in the announcement.
- Reproduce the quickstart from the release candidate, regenerate the HTML guides, check links and confirm that required third-party notices are present.
- Review exactly what will be published, including example artifacts and repository history. Keep local workspaces, credentials and private data out of the release.
- Record the actual demo if using video; the talk track below is a script, not an existing recording.

Hosting, package publication and announcements are separate release actions. The current assets position a developer preview; live vendor, managed registry and comparative-performance claims remain outside that announcement.

The source installation is `git clone https://github.com/anirudhagarwal-atlan/oriel.git`, followed by `cd oriel` and `uv sync --frozen`. Use `.venv/bin/oriel` from that checkout. The [quickstart](quickstart.md) is the command reference for the public release.

## Repository metadata

**Tagline:** Talk to data across the tools you already use.

**GitHub About:** Join tool data in DuckDB, work with your agent, and keep reviewed business definitions for the next question. Open source.

**Suggested topics:** `duckdb`, `analytics`, `ai-agents`, `mcp`, `semantic-layer`, `data-connectors`, `sql`, `developer-tools`, `open-source`.

**One-sentence description:** Oriel is an open-source local analytics workspace for bringing tool data together, inspecting answers, and reusing reviewed metrics and context across sessions.

## Repository announcement draft

Oriel helps you talk to data across the tools you already use.

Bring supported tool exports or configure a source adapter, load the data into DuckDB, and work with SQL or your agent. Inspect the identity mappings and query behind an answer, review the business definition, then keep it for the next session or refresh.

The first walkthrough joins CRM entitlements with product activity to find active customers with unused seats. It covers the parts that make this question tricky: different IDs, duplicate activity, time windows and exclusions. A synthetic refresh changes the answer from 51 unused seats to 8 while retaining the same approved definition.

This is a developer preview. Local file workflows and selected SQLite and loopback REST/GraphQL integrations have functional verification. Authenticated vendor compatibility is still unverified. Context candidates need review, and bundle export does not deploy to a hosted registry.

Start with the [repository instructions](../README.md) and [CRM + product walkthrough](tool-exports.md). Then try an authorized export from your own tools. Which recurring question would you use it for, and where did setup or review become difficult? Open an issue in this repository with a sanitized example.

## Short social post draft

Your CRM and product tool often describe the same customer differently. Answering a cross-tool question means resolving those IDs and agreeing on what the metric means.

Oriel puts tool data in DuckDB and lets you work with SQL or your agent. Review and version the definition, keep its evidence, and reuse it after refresh.

The developer preview includes a runnable synthetic CRM + product recipe. Try it, then tell us which recurring question you would bring from your own tools.

Attach the verified demo and the actual repository link when publishing. No performance comparison is part of this announcement.

## Technical community post draft

**Title:** A DuckDB workflow for cross-tool questions and reusable reviewed context

The example starts with three exports: CRM entitlements, product events and an identity map. It checks ambiguous mappings, deduplicates activity and applies an explicit adoption policy. You inspect the SQL and approve a versioned definition, then rerun it on changed data and export it for another consumer.

The implementation includes a CLI, a read-only MCP interface and local transformations with reviewed local changes. It can draft schema/key/join context; business rules remain a review decision. The current proof is functional, using synthetic fixtures. It is not a comparative model benchmark or a claim that every source adapter is production-validated.

The [walkthrough](tool-exports.md) includes commands, expected results and the reuse check. Feedback sought: a synthetic mapping edge case, a definition the draft could not infer, or the first setup step that failed. If the workflow fits a recurring question across your tools, describe the inputs and required business rule in an issue without sharing private data.

## Ninety-second demo talk track

Use the [generic export walkthrough](tool-exports.md). Keep “Synthetic data · deterministic execution” visible throughout. Record actual commands and outputs; if edited for time, label the cuts. The command reference remains the walkthrough, so the recording does not become a second, drifting setup guide.

| Time | Show | Say |
| --- | --- | --- |
| 0–15 seconds | The recurring question and three small input schemas | “Which active customers have unused seats? The CRM and product tool use different IDs, so the mapping is part of the answer.” |
| 15–35 seconds | Imported tables, draft context and mapping checks | “Oriel brings these exports into DuckDB. The draft surfaces possible keys and joins. We review the mapping and business rules before relying on them.” |
| 35–55 seconds | Reviewed SQL, approval version and the result of 51 seats across five organizations | “This example counts distinct members who published a report in a declared window, with explicit exclusions. The result comes from this executable definition.” |
| 55–75 seconds | Refreshed inputs and the result of 8 seats at one organization | “The data changed. The approved definition stayed the same, and the result changed with it.” |
| 75–90 seconds | Exported bundle and separate-consumer result | “Another consumer can inspect and run the retained definition. Start with this recipe, then bring your own authorized tool exports.” |

The recorded reuse check is a separate program executing exported definitions. Do not narrate it as a second model discovering the right metric. A live agent segment needs its own verified client setup, explicit model-data consent and visible tool trace.

## Issue feedback CTA

**Try one recurring question across your tools, then open an issue in this repository.** Include:

- The question, source types and whether you used exports or an adapter.
- The setup stage, command shape, runtime versions and sanitized error or result shape.
- What you expected, what happened and which business rule needed review.
- A minimal synthetic fixture if it helps reproduce the problem.

Do not attach credentials, source rows, customer identifiers or a private context bundle. A reusable fixture or clear failure report is more useful than a connector-name request alone.

## Claims guide

“Supported wording” below means supported by the repository's current functional evidence, not approval to publish.

| Supported wording | Unsupported claim to omit |
| --- | --- |
| “Talk to data across existing tools through supported exports and configured adapters.” | “Connect any tool instantly” or “101 verified live connectors.” |
| “Local DuckDB analysis, with CLI and read-only MCP access.” | “All data stays local” when an agent or configured model receives tool results. |
| “Draft context, review business definitions, and reuse approved versions.” | “Automatically understands or certifies your business metrics.” |
| “Synthetic file, SQLite and selected loopback API workflows have functional verification.” | Authenticated SaaS compatibility or customer success not demonstrated by those checks. |
| “Export an inspectable context and metric bundle for a receiving workflow.” | Managed registry deployment, access control or automatic team distribution. |
| “Reviewed changes affect the local workspace.” | Source-system writeback or autonomous production changes. |
| “Comparative evaluations are deferred.” | Better-than-MCP, accuracy lift, cost savings, speed multipliers or equivalent performance claims. |

Keep the [connector limits](connectors.md), [build evidence](build-status.md) and [evidence policy](gtm.md#scorecard-and-evidence-policy) near the technical details. Lead the launch with the useful workflow and the artifact a user can keep.
