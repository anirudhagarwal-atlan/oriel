# Mixpanel + Salesforce: verification protocol

**Superseded source choice:** the user selected synthetic Stripe + Linear in Rystic on 9 October 2026 and deferred benchmarking until after the build. This document is retained as an earlier study design. The current scope is the [build charter](build-charter.md) and [Rystic verification plan](rystic-benchmark.md); no Rystic result may be presented as evidence for this source pair.

Version 0.1, 9 October 2026. **Status: design specified; adapters, fixtures and acceptance runner not implemented. No results under this protocol yet.** Freeze task, fixture, prompt, model, tool-schema and implementation hashes before a confirmatory run. Existing generic demo results are separate evidence.

## Claim and comparison

Target claim: “For the declared recurring analytics workload across Mixpanel and Salesforce, Oriel gave the same agent more accurate answers at lower cost per correct answer and lower time to a correct answer than direct source MCP access.” The thresholds are requirements to meet, not expected findings. The broader “cheaper workload” claim also requires the total-cost gate below.

Both designs may use MCP. The independent variable is how the agent accesses and computes over business data. A comparison against simulated list endpoints cannot substantiate a claim about native source MCP servers.

| Arm | Access | Context | Role |
| --- | --- | --- | --- |
| A: direct source MCP | Actual Mixpanel and Salesforce read tools, native queries/aggregates and common isolated code helper | Same approved business definitions and source metadata as B | Primary baseline |
| B: Oriel | Source snapshots in DuckDB, bounded SQL, approved metric execution and the same code helper | Same approved business definitions as A | Primary product arm |
| C: direct source MCP, default setup | Same capabilities as A | Native source context and the same initial business evidence, without the reviewed pack | Secondary product-experience comparison |
| D: DuckDB without reviewed pack | SQL access without B's named metric execution | Same initial evidence as C | Secondary combined context-and-metric-surface ablation |

Primary gates compare B with A. Do not use a weaker C result when A ties or wins. If multiple eligible baseline configurations are tuned on development tasks, lock all of them before held-out evaluation and require superiority over each for a combined claim. Secondary arms cannot rescue a failed primary gate.

[Mixpanel's official MCP documentation](https://docs.mixpanel.com/docs/mcp) lists analytics queries, query schemas, metadata and business-context retrieval. Include their read capabilities. [Salesforce SObject Reads](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/sobject-reads.html) supports schema/context discovery, SOQL, search and relationship retrieval. Inventory the tools actually enabled in the selected environment and pin their schemas; the documentation is not evidence of local access.

Allow source filtering, projection, aggregation and pagination where supported. Give A the ability to combine retrieved data deterministically; do not force arithmetic through model text. Use the same code-execution limits for both arms. Allow normal ephemeral files and within-batch reuse for A and count their cost; do not make A repeatedly download identical records while B retains them. Disclose any read capability unavailable in the selected integration. Exclude source mutation tools from all arms.

Use one common agent runtime and model configuration. Run both primary arms through actual MCP clients, with source tools for A and Oriel tools for B. Hold history retention, session boundaries, context limits, concurrency, timeout, retry and computing budgets equal; actual transcripts will differ. Model/provider revisions, caching and native usage counters must be recorded. Do not compare a native persistent tool session to a repeatedly restarted JSON model loop without explicitly declaring that confound.

Both primary arms can inspect the complete approved definition logic, formulas and mappings; no private business rule is available only through B's metric implementation. Charge implementation/adaptation costs to the relevant arm. This primary comparison evaluates the delivered workflows, including metric execution. An additional SQL-only run is needed to attribute an effect specifically to the database rather than executable metrics.

## Data contract and workload

Use a Salesforce sandbox and Mixpanel test project with synthetic data for publishable live evidence once their setup is authorized. Until then, schema-faithful local fixtures can validate implementation but must be called simulations.

The data contract includes Salesforce accounts, opportunity/renewal records, an explicit revenue basis and owners; Mixpanel account/group mappings, identity rules, qualifying events and timestamped usage. Pin timezone, event deduplication, excluded users, currency, fiscal calendar, stage policy and revenue grain. Surface missing or conflicting definitions. Never infer ARR from the standard opportunity amount field.

Prepare separate development examples and **30 held-out task families** before tuning:

| Family group | Count | Examples and traps |
| --- | --- | --- |
| Cross-source analysis | 12 | Declining usage on renewing accounts; pipeline by activation cohort; expansion candidates; owner rollups; duplicate opportunities and user/account joins |
| Semantic decisions | 6 | ARR versus contract value; eligible stages; fiscal boundary; identity ambiguity; missing revenue definition; explicit abstention when evidence is insufficient |
| Refresh and context reuse | 6 | Late events; changed opportunity stage; updated ownership; revoked account visibility; renamed activity event; reviewed definition revision in a new session |
| Controls | 6 | Fresh single-record lookup; native source aggregate; product-only query; CRM-only query; empty cohort; no-change repeat |

The first 24 families define the primary recurring cross-source workload. Publish the six controls and the equally weighted 30-family aggregate alongside it. Do not broaden a primary-subset win into an all-task claim. Freeze all exact questions, expected outputs, weights and scenario assignments before held-out runs.

Use three independently generated fixtures spanning small, medium and large event volumes, with manifests recording actual rows and hashes, and three repeated runs per task/fixture/arm. The primary two-arm study therefore schedules **540 trials** before secondary ablations: nine paired 24-task core batches plus nine paired six-task control batches. Cold, warm and refresh observations occur within these batches; they are not three additional 540-trial studies. Repetitions measure model variability; they do not turn 30 task families into 270 independent questions per arm. Use development-pilot variance to assess power and the number of independent workspaces before freezing the final sample size. Nine paired batches may be inadequate; an underpowered or incomplete study is inconclusive.

Source permissions and business evidence must match. For the correctness study, use frozen source states and verify identical visibility and as-of boundaries against the DuckDB snapshot. For refresh tasks, apply predefined changes only in authorized synthetic sandboxes, measure propagation and snapshot refresh, and score against the new source state. Permission revocation is a freshness/correctness failure if a snapshot still exposes disallowed rows.

## First use, reuse and freshness

Report three scenarios separately:

1. **Cold:** no materialized source data or reusable context. Start the timer after source credentials are available; include discovery, ingestion and context preparation. Measure human setup/review time separately, using a declared cost rate if making a combined monetary claim.
2. **Warm:** a verified snapshot and reviewed pack exist. Show query/model cost and latency, with data age. Both arms may use their permitted caches.
3. **Refresh:** apply a declared source change, enforce the same freshness requirement, and include checking, invalidation, incremental sync and revalidation before answering.

Primary end-to-end accounting uses a locked recurring batch containing all 24 core tasks: begin cold, refresh before each of the six change/reuse tasks, and amortize shared setup only over those 24 tasks. Assign shared setup cost and duration evenly across the batch for per-task summaries; retain the unallocated cold and refresh durations. A separate controls batch receives its own overhead. Never hide setup by amortizing it over hypothetical future users or queries.

The approved pack is created once from development evidence. Charge that common preparation/review cost identically to both primary arms; charge architecture-specific mapping, executable-metric adaptation and validation only to the arm that needs it. Primary cold timing represents this fully accounted setup. Independent context-creation savings are tested in the secondary bootstrap experiment, not inferred from providing B with a free pack.

Reset source fixture state, persistent agent memory, local snapshots and caches between independent batches. Within a batch, run questions serially using the same predeclared task/change sequence and session boundaries for both arms, permit normal reuse and pin gold to the state at each step. Randomize which arm runs first for each paired batch; use separate isolated source fixtures or a verified reset to prevent one arm observing the other's state. Preserve external-provider cache counters and disclose any cache reset that cannot be enforced. Fresh-session reuse tasks keep approved artifacts but reset conversation history in both arms.

Report cost/latency curves at 1, 5, 10 and 24 core questions and the observed break-even point. Faster warm queries do not establish faster first use. If the source baseline wins on cold or fresh lookups, show it prominently.

## Independent correctness

Compute gold results from the synthetic source manifest using a separate implementation with exact decimal arithmetic and explicit identity/time rules. Do not compute gold by executing the product's own metrics. Keep expected outputs unavailable to the agent and context bootstrap. Validate source native query results against the manifest before the study.

Require a structured response with account IDs, measures, units, time boundaries, definition version and freshness. A task passes only if all required fields satisfy its frozen rubric: exact sets/counts, monetary tolerance fixed at half a cent, predefined numerical tolerances, no duplicated revenue, and required qualification or abstention. Narrative fluency cannot compensate for an incorrect result. Track partial scores diagnostically; primary accuracy is the fraction of fully correct tasks.

Include timeouts, tool errors, malformed answers, wrong abstentions and retries. Never drop them from the denominator. An expected request for clarification counts as correct only on tasks whose rubric explicitly requires it.

## Accounting and hard acceptance gates

For each scheduled trial retain its status, pair ID, task/fixture/version hashes, prompt and tool trace, native model identity, input/output/cache tokens, provider cost basis, all tool/API calls, retry delays, ingestion and refresh cost, and elapsed time. Publish only synthetic traces. Unknown costs remain unknown; list-price model estimates must not be called actual charges. Flat subscription access requires a declared allocation or a narrower marginal-cost claim.

| Gate | Definition and threshold |
| --- | --- |
| Accuracy | B passes at least 90% of core tasks and exceeds A by at least 10 percentage points; paired 95% CI for the lift is entirely above zero |
| Cost | B's total batch cost divided by its number of correct answers is at most 0.70× A's; paired 95% upper bound for the ratio is below 1 |
| Total cost | B's total batch cost is lower than A's, with paired 95% upper bound for the ratio below 1; required for the combined cheaper-workload release claim |
| Speed | B's mean capped time to a correct answer, including allocated setup/refresh duration, is at most 0.50× A's; paired 95% upper bound for the ratio is below 1 |
| Completeness | Every scheduled trial has a terminal outcome; early global budget stops and missing trials make the release report inconclusive |
| Integrity | Verified source/snapshot parity, reproducible scorers, fixed conditions and no data/answer leakage; all safety and permission fixtures pass |

Cost per correct answer counts all attempt costs in the numerator. Also report total batch cost and mean cost per attempted task so a claim cannot conceal higher total spending. A broad “cheaper workload” claim additionally requires lower total batch cost, not only lower cost per success. If a denominator is zero, report it explicitly; do not manufacture a finite ratio.

For speed, use a **300-second task deadline** in both arms, excluding separately timed shared batch setup. A correct completed task contributes its actual elapsed duration; an incorrect answer, tool failure or timeout contributes 300 seconds. Add the allocated setup/refresh duration after this cap. Thus a fast wrong answer cannot win. Also publish uncapped wall time, p50/p95 answer latency, and latency on pairs where both arms succeed. “2× faster” refers specifically to this declared time-to-correct metric; it must not be presented as a p50 claim.

Use paired whole-batch resampling for primary confidence intervals: preserve every task, change sequence, shared setup and cache dependency within the paired workspace batch, and stratify by fixture scale. Do not treat its 24 tasks as independent samples of ingestion or refresh cost. These intervals describe run variability conditional on the fixed workload and fixtures. Generalization to other questions or organizations requires additional independently sampled task sets and workspaces; model repeats alone cannot establish it. Predeclare the statistical procedure and seeds in the study manifest, verify sufficient independent batches with the pilot, and report per-family effects and sample sizes. All gates must pass together in the same frozen confirmatory study; missing measurements yield **inconclusive**, not pass. Do not keep adding runs until significance appears. A revised product gets a new version and fresh held-out tasks.

## Verify bootstrapping separately

Start from source metadata, existing business definitions and development queries. Record model cost, elapsed time, reviewer effort, unresolved questions and changes required to approve a pack. Export the exact approved pack to both primary arms. Neither receives held-out answers.

Then run a new agent session, related unseen questions, refreshed data and a changed definition. Verify definition-version use and regression detection. The goal is evidence that context is created with less repeated setup and remains useful across consumers. Manually supplying complete gold SQL for every test question does not prove bootstrapping.

## Execution and release decision

First implement a deterministic local fixture/scorer check, then a small real-source/model smoke run, then the confirmatory study. The earlier authorization for a small configured-model run does not authorize 540 trials. Prepare an exact bounded cost proposal after smoke measurements, before execution.

As checked in this session, Mixpanel is available but not installed; Salesforce's plugin is disabled by an administrator. This is an access dependency. No connector was installed, no restriction bypassed, and no private source data accessed to define this protocol.

If all gates pass, publish the exact scope, model, task mix, scale, freshness policy, point estimates, confidence intervals and artifacts beside the claim. If one gate fails, improve that part of the product or publish the narrower observed result. A result where accuracy ties and cost improves remains useful, but does not meet this build's combined success criterion.
