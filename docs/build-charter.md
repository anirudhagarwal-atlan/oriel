# Build charter: talk to data across existing tools

The [Customer 360 agent plan](customer-360-plan.md) and [active build goal](customer-360-goal.md) define the next product and its verification gates. It focuses the launch on customer reviews across existing tools while preserving connectors → DuckDB → context agents → answers. The charter below records the earlier generic scope and deferred benchmark requirements.

Updated 9 October 2026. Product direction: **existing-tool analytics is the product; Rystic supplies the first synthetic benchmark environment.** Product name: Oriel. Stripe + Linear is the selected benchmark pair. Comparative evaluations remain deferred. Performance targets below are unverified requirements for a later performance-led launch; the current developer preview makes no such claims.

## Goal

**Build an open-source way for users to talk to data across the tools they already use, using their existing agent.** Bring source data into DuckDB, analyze and transform it, make reviewed local changes, and retain the business definitions and evidence as reusable context. A user should move from a question across their tools to an inspectable answer, then reuse the approved definition in another session or after a data refresh.

The product should accept data from any tool through a supported export, compatible snapshot or source adapter. CRM, product analytics, billing, support and internal systems are use cases, not separate products. Today, file and snapshot imports, a 101-entry connector catalog, explicit credential configuration and source sync are available. Actual SQLite and selected local REST/GraphQL paths are verified; authenticated vendor compatibility remains work to do. Tool independence describes the architecture and product direction, not a claim that every native connector already exists.

Finish and functionally verify the general workflow first, using synthetic data. At the end, use Rystic's Stripe + Linear simulators to compare the same agent using this method versus direct source MCP access. **More accurate, cheaper and faster remains a core release criterion**, with all three gates required before making the combined claim. Completing the synthetic integration does not establish that claim.

The commercial purpose is to acquire builders through useful analytics and expand into Analytics and Agent Registry when teams need recurring execution, shared definitions, ownership, and controlled updates.

## Product scope and current access paths

| User need | Current path | Planned extension |
| --- | --- | --- |
| Bring data from existing tools | File/snapshot imports and source configure/sync/bootstrap/refresh across a 101-entry catalog | Validate selected authenticated vendor paths and improve setup from own-data trials |
| Ask across sources | Shared DuckDB workspace, SQL, CLI and read-only MCP; configured-model question loop | Validate advertised agent clients and improve mapping/setup guidance |
| Retain business meaning | Schema/key/join drafts, reviewed metric proposals, versions and portable export | Measure review effort and reuse on unfamiliar tool schemas |
| Complete useful work | Local transformations and exact-preview local changes | Add source writeback only as a separately scoped, reviewed capability |

The source interface ends at observed tables, identity mapping and provenance. The analysis, review and export flow should work without a dependency on Rystic or hard-coded Stripe/Linear schema names. Rystic-specific policies and commands belong to the benchmark recipe.

## First benchmark fixture: two sources and one example job

Use the following pair to develop and verify the evaluation path. It does not restrict which tools the product can accept.

- **Rystic Stripe 0.0.8:** synthetic customers, payments and refunds.
- **Rystic Linear 0.0.1:** synthetic customer-linked issues, priorities, workflow states and owners.

Fixture question: **“Which paying customers have unresolved high-priority issues, what net collected USD payments do they represent, and who owns the issues?”**

Create the linked fixture through normal simulator APIs. Use explicit account IDs, retain source identity, subtract refunds in integer cents, exclude internal accounts and non-USD payments, and distinguish urgent/high unresolved issues from completed, canceled or lower-priority issues. Aggregate payments and issues separately before joining so multiple issues cannot multiply revenue. Keep unassigned ownership visible.

Return the account set, payment basis, issue owners, executed query, source observation time and context version. The metric is historical payment association, not ARR or a prediction of churn/loss. The selected Stripe simulator does not implement subscriptions. Agent-facing tools remain read-only; fixture setup and source-change checks affect only the verified local simulators.

## Definition of success

Product success requires this loop to work with a user's supported tool data, independently of the benchmark recipe:

**Bring tool data → query across sources → inspect/transform → propose context → review and approve → export/reuse → refresh and rerun.**

Before launch, demonstrate the generic import, answer and reuse path on a separate synthetic fixture with different source schemas, without calling any `rystic` command. Track first own-data answer, next-week repeat use, definition reuse and a second consumer during approved design-partner trials. A successful simulator demo alone does not meet these product and adoption criteria.

The [generic export walkthrough](tool-exports.md) now verifies that functional path with CRM seats, product events and an explicit identity map: 51 unused seats across five organizations becomes 8 at one after refresh, with the same approved definition and separate-process reuse. This is synthetic functional evidence; design-partner activation and retention remain unverified.

The Rystic evaluation environment has its own functional acceptance checks:

| Benchmark infrastructure requirement | Completion evidence |
| --- | --- |
| Real use of Rystic | Pinned native simulators, normal API fixture writes and read-back; no local fake presented as Rystic |
| Useful first result | Twelve customers, 24 payments and 17 issues; independent fixture oracle agrees with the initial five-account / $1,857.69 answer |
| Reviewed reusable context | Provisional preview, candidate SQL/rules/evidence, exact-hash local approval and portable metric/skill bundle |
| Refresh correctness | Resolving the two qualifying issues for one customer changes the result to four accounts / $1,490.61 after refresh, retaining the metric definition |
| Two agent access paths | Read-only DuckDB/context/metric MCP tools and source API MCP tools with the same context; source mutations blocked |
| Reproducible entry point | Bundled verified synthetic capture works without a Rystic account; live walkthrough clearly requires the Rystic runtime |

The [Rystic walkthrough](rystic.md), [functional evidence](../examples/rystic/README.md) and tests cover this stage. The following performance gates apply to the **later** study described in the [deferred verification plan](rystic-benchmark.md):

| Requirement | Release target |
| --- | --- |
| More accurate | At least 90% fully correct tasks and at least 10 percentage points above the strongest eligible direct-source MCP baseline |
| Cheaper | At least 30% lower total workload cost per correct answer and lower total batch spending, including sync, context preparation, refresh, retries and failed attempts |
| Faster | At least 2× faster by mean capped time to a correct answer on the declared recurring workload; report first-use latency and p50/p95 separately |
| Supported by evidence | Paired 95% confidence intervals support all three improvements; complete planned runs, held-out tasks, independent scoring and reproducible artifacts |
| Bootstrap and reuse | Measure reviewer effort and reuse on unseen tasks; the current authored synthetic policy must not be called autonomous business-definition discovery |

Performance targets apply to the specified synthetic workload, model, simulator versions, scale and freshness policy. Single-record lookups and other controls must also be published, including cases where direct MCP wins. Simulator findings do not establish production Stripe/Linear or Mixpanel/Salesforce performance.

The primary north-star remains repeated useful work with approved context. The benchmark is a hard gate for the performance-led launch positioning, not a replacement for evidence that people return.

## Principles

1. **Fit the tools users already have.** Keep ingestion extensible and the analysis/context workflow independent of source brands. Publish actual support separately from the broader product promise.
2. **Build toward the advantage; measure the outcome honestly.** An unsuccessful or inconclusive comparison means improve the product and run a new held-out evaluation. Never weaken a baseline or discard failures to manufacture a win.
3. **Compare the same agent with strong alternatives.** Fix the model, prompts, permissions, business evidence and budgets. Let the source baseline use its actual aggregation, query, metadata and context capabilities.
4. **Separate computation from context.** Equal-context runs establish the execution advantage. Separate bootstrap experiments measure setup effort, semantic corrections and reuse. Context inferred by the product stays a draft until reviewed.
5. **Count the whole job.** Include ingestion, refresh, context creation, model usage, retries and stale-data errors. State when savings require repeated queries to amortize setup.
6. **Fit the existing agent.** Keep the CLI, MCP interface, SQL and context artifacts portable. MCP can also carry Oriel; the comparison is between two agent workflows.
7. **Make the open-source product independently useful.** No Atlan account or Rystic runtime is required for local analysis, transformations, definitions, evaluations or export. Shared organizational needs create the registry opportunity.
8. **Earn trust with inspectable work.** Preserve definitions, identity mapping, versions, provenance, freshness and deterministic checks. Unresolved meaning must be visible.

## Constraints

- Limit the first comparative study to two sources: Stripe + Linear in Rystic. This bounds the experiment, not product compatibility. Additional native connectors are not required to run this benchmark.
- Keep the core independent of the simulator. Own-tool imports must use the generic workflow; do not require users to adopt Stripe, Linear or Rystic.
- Use synthetic data for development and evaluation now. Keep Rystic endpoints on numeric loopback; no production source connection is needed. Full-cohort refresh is implemented for the Rystic cohort; do not advertise universal or incremental sync.
- Respect simulator limitations and licensing. Rystic's Linear filter behavior is not identical to production Linear. An API simulator does not establish real authorization enforcement.
- Develop public fixtures and traces with synthetic identities. Real source data needs an explicitly approved source-to-model flow and stays out of public artifacts.
- Keep agent analytics read-only; transformations and reviewed changes remain local. Explicit fixture writes stay within local Rystic. Do not mix write success into the analytics benchmark.
- Preserve required third-party licenses. Product positioning uses its own name and measured results.
- Comparative benchmarking is deferred by the user. A later model study needs its final task manifest, strong baseline, accounting and bounded execution budget before it runs.

## Current evidence and next build sequence

The Rystic functional run verified initial source ingestion, independent monetary/account correctness, approved metric execution, unchanged and changed refresh, source MCP compatibility and matching context. It made no model calls and is not a performance benchmark. The older generic model smoke test tied at 3/3 for source tools with context and SQL with context. **The accuracy/cost/speed release gate remains unverified.**

The general file/snapshot and connector paths support data outside the Rystic pair. Authenticated vendor compatibility, organizational access and own-data validation are later work. The old Mixpanel/Salesforce protocol is retained as historical design material, not the current acceptance workload.

The development-pilot infrastructure includes actual source/workspace stdio MCP, equal deterministic result computation, frozen plans and budget/failure guards. A shared runner supports the four warm tasks and all twelve tasks across the prepared baseline, refreshed and guarded phases. Offline tools allocate candidate fixture seeds outside the declared development inventory and validate paired study receipts with accounting and confidence intervals. Distinct acceptance intent/protocol/binding commands check fresh allocated baselines, substantive review declarations and later observed state against unchanged task commitments. Offline receipt collection and reporting validate recorded event intervals, independently rescore paid answers, retain failures and account for earlier context work and transitions.

The study controller is now implemented and tested end to end with in-memory sources, actual local DuckDB and scripted responses. It enforces cumulative budgets, frozen source assignments, unchanged approved context and sequential phase execution. Partial failed runs remain available, and automatic resume is disabled. These tests establish controller behavior, not native execution or performance. Native isolated lifecycle, provisioning and authorization integration, a live model pilot, actual power/accounting/rate reviews, the complete held-out study and independent evidence review remain required. Existing development bindings can never pass the release gate.

The current runtime measurement starts after offline protocol/input validation and runtime construction, and stops before final statistical reporting and export. These exclusions are unmeasured analysis overhead, not measured zero cost or time. Complete billing reconciliation remains a prerequisite for the charter's all-in cost criterion; the implementation and synthetic tests do not satisfy it.

1. Lead the README and site with existing-tool analytics and a bring-your-data entry point. Show actual supported formats and adapters alongside the broader promise.
2. Make generic source mapping, first answer, reviewed context and reuse clear and reproducible. Verify portability with a different synthetic schema and collect approved own-data feedback later.
3. Verify the implemented comparison infrastructure against native isolated source lifecycle, provisioning and authorization. Review the capable source baseline, power assumptions and actual billing rates; reconcile the whole job and freeze budgets and scoring before measurement.
4. Run the benchmark at the end of the build and publish all outcomes. Use accuracy/cost/speed claims only within the tested scope and only when all gates pass.
5. Measure repeated own-tool use, second-consumer reuse and definition updates. Validate a real Registry import/update round trip before promising managed team integration.

The [GTM plan](gtm.md) turns this sequence into acquisition, activation, retention and expansion milestones.
