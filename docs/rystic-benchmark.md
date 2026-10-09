# Benchmark plan: existing-tool analytics, tested with Rystic

**Product premise:** let an agent talk to data across the tools a user already has, with DuckDB computation and reviewed reusable context. **First test environment:** synthetic Stripe + Linear APIs supplied by Rystic. The pair bounds this experiment; it does not define product compatibility or the target market. Users can bring other tool data through supported exports, snapshots and adapters.

The user selected this synthetic pair on 9 October 2026 and asked to benchmark at the end of the build. **No comparative model benchmark has been run for this pair.** The [recorded checks](../examples/rystic/README.md) verify functionality only; timings in adapter receipts are operational diagnostics, not performance claims.

The current build connects to pinned Rystic Stripe 0.0.8 and Linear 0.0.1 APIs. Both the read-only source MCP tools and DuckDB tools are implemented. A common development-pilot runner uses actual stdio MCP for both arms, an independent structured scorer and explicit budget/failure handling. Offline allocation, statistical reporting and a controller for cumulative study budgets are implemented. The controller is tested end to end with in-memory sources and scripted responses; native lifecycle/provisioning/authorization integration and observed full cost accounting remain work for the release study. The legacy `benchmark` command does not run this study.

## Prepare the development pilot

The pilot has four public tasks: qualifying accounts with issue owners, aggregate exposure, payment/exclusion controls, and a single-account lookup. Each runs once per arm. Gold is computed from the seed manifest with independent integer arithmetic, after checking that every intended record matches the workspace. Questions and answer schemas exclude gold. This is development data, so even eight correct answers cannot satisfy the release gates.

Both arms receive the same business rules and reviewed context. The source arm exposes read-only Stripe API tools and supported Linear GraphQL. The workspace arm exposes catalog, context and SQL. Both also receive `compute`, a read-only SQL calculator over only the tool results that arm retrieved during that trial. This lets the source baseline join and aggregate deterministically without receiving the workspace's data for free. Complete responses remain available to the calculator even when the model-facing display is bounded; storage limits fail explicitly.

The source metadata tool includes the observed native field schemas and working pagination/projection examples. In the pilot, simulator identities are checked at source-server startup and at study boundaries; ordinary interactive use still checks each source call. Initialization remains timed, and source responses are never cached by the identity-check policy. Trace byte counts describe MCP frames and calculator JSON, not total underlying simulator HTTP traffic.

Both arms receive the same allowlisted evaluation context: approved recipe versions, exact SQL and fixed business rules. Examples, previous results, result hashes and free-form context payloads are omitted from that view; the product's reviewed context stays unchanged. The plan fingerprints the full local context and the evaluation view and rejects a metric that substitutes constant SQL for the recipe. Correct answers alone still do not establish fresh retrieval: inspect the retained tool trace as part of evidence review.

After the [live recipe](rystic.md) has been seeded, synced and reviewed, freeze a plan without making model calls:

```sh
uv run --frozen oriel --workspace .rystic-workspace rystic pilot-plan \
  --manifest .rystic-workspace/rystic-seed.json \
  --output benchmark-runs/rystic-pilot-plan.json \
  --max-turns 8 --max-calls 64 --max-cost-usd 3 \
  --max-wall-seconds 900 --trial-seconds 180
```

Review the tasks, expected-answer oracle, two arms, source/context hashes, shuffled trial order, runtime/code fingerprints and budgets in the plan. Choose a new output path for every plan and run. The final live pilot is an explicit opt-in:

```sh
uv run --frozen oriel --workspace .rystic-workspace rystic pilot \
  --plan benchmark-runs/rystic-pilot-plan.json --approve REVIEWED_PLAN_SHA256 \
  --output benchmark-runs/rystic-pilot-result.json --allow-model-data \
  --stripe-url http://127.0.0.1:18981 --linear-url http://127.0.0.1:18982
```

This uses the configured Claude CLI and sends the synthetic questions, approved context and selected tool results to that model. It requires running, licensed Rystic simulators; an offline capture cannot substitute for a live source arm. Preflight and postflight check source/workspace hashes and simulator identity against the plan. Keep the simulators quiescent: matching boundary checks do not prove that no intermediate change occurred.

The runner stops further inference when cost is unknown, reported model identity is missing or changes, or a global call/cost/time budget is exhausted. Missing, wrong, stale or unattempted answers count as failures and receive the declared capped-time penalty. Time limits stop new agent work and reject late answers; source validation and process cleanup may finish after the deadline. Their elapsed time is recorded separately and included in overall duration. These limits are not a hard operating-system deadline or an assurance against an unreported provider charge.

The report retains actual answers, scoring errors, model usage, model-facing messages, MCP byte/timing traces and every planned trial, including skipped trials. `known_cost_usd` is a subtotal when usage is missing. Provider estimates are not necessarily invoiced spend. Cold setup, review, refresh, runtime and licensing costs stay `null` until measured; all-in cost/time and release claims remain unavailable. Every pilot report explicitly sets `release_gates.claim_eligible: false`.

The automated harness tests use authored model responses and local subprocesses. They verify the runner; they are not comparative model results. No live Rystic model pilot has been completed yet.

## Core performance goal

For a declared recurring workload, the same agent should achieve at least 90% fully correct answers and a 10 percentage-point lift, at least 30% lower cost per correct answer with lower total workload spending, and at least 2× faster capped time to a correct answer. All three must pass in one locked, complete study, with confidence intervals supporting the improvements. These remain engineering targets.

Both arms may use MCP. The comparison is direct source-tool access versus a materialized DuckDB workspace with the same reviewed business context. It tests where an agent retrieves and computes its answers, rather than the MCP protocol. Bootstrap effort and the added value of executable metric definitions need separate experiments so context quality is not mistaken for a data-access advantage.

The earlier [Mixpanel + Salesforce protocol](mixpanel-salesforce-benchmark.md) preserves the detailed accounting and fairness design, but its source selection and task manifest are not the current test. Do not report Rystic findings as Mixpanel, Salesforce, production Stripe or production Linear results.

## Work to do after the build

Offline preparation now also supports `rystic allocate-study`. It allocates fixture seeds disjoint from the declared development seeds, balances dataset sizes, and counterbalances paired arm order. It produces an allocation only: fixture hashes, a model budget and execution authorization remain absent. The default allocation is 30 independent batches × four task families × three repeats × two arms = 720 planned trials. That count is a planning floor, not a completed power analysis or authorization to spend.

```sh
# Declare every seed actually used for development or tuning, not just these examples.
uv run --frozen oriel rystic allocate-study \
  --master-seed 20261009 --development-seed 7 --development-seed 31 \
  --output benchmark-runs/study-allocation.json
```

Once runtime access is available, each isolated fixture can use `rystic seed --variant shuffled_cases_v1 --seed ALLOCATED_SEED --accounts ALLOCATED_SIZE`. This variant changes account labels, which semantic case belongs to each account, and insertion order. It preserves ordinary supported simulator writes and the default demo unchanged. Variation alone does not make a dataset held out: keep its outcomes out of tuning, retire any contaminated batch, and freeze the final study before measuring.

1. Create separate development and held-out synthetic source fixtures. The bundled twelve-customer example and its known answer are development data, never acceptance evidence. Cover partial/full refunds, currency exclusion, internal accounts, missing identity, many-to-many joins, unassigned owners, issue priorities/states and changed source records.
2. Fix the same model, runtime, context, session boundaries, computing tools, limits and permissions for both arms. Source tools use Rystic APIs through the read-only MCP adapter. Workspace tools use materialized DuckDB and reviewed metrics through MCP. Permit source-side query/projection/pagination and deterministic postprocessing wherever supported.
3. Inventory simulator limitations first. In the verified Linear version, `issues.filter` is ignored. Disclose this, compensate with supported operations where possible, and do not count an artificial simulator deficiency as evidence of superiority over real Linear. Production evidence requires a later approved real-source study.
4. Measure cold setup, warm queries and refresh separately. Include data acquisition, context preparation/review, source/model/runtime costs, failed attempts and retries. Allocate common context preparation equally; count architecture-specific adaptation separately. Rystic licensing/runtime must be included or explicitly separated, never silently zeroed.
5. Score complete answer sets, exact integer-cent amounts, currency, identity mapping, issue ownership/state, snapshot provenance and appropriate abstention against an independent oracle. Wrong, stale and unanswered results count as failures. Do not use the product SQL to generate gold.
6. Predeclare the workload, scales, independent batches, repeated model runs, budgets and paired confidence procedure before measuring. Preserve shared setup and cache dependence when resampling complete batches. Incomplete runs are inconclusive. Publish controls and losses alongside the headline comparison.
7. Prepare the bounded execution budget after an integration pilot. Publish the exact scoped result only when all acceptance gates pass; otherwise improve the build or report the narrower observed finding.

Context bootstrap remains a separate product question: how much evidence gathering and human correction is needed before a useful definition can be reused in a fresh agent session? The current recipe proposes declared synthetic policy with source evidence. It does not demonstrate autonomous discovery of business meaning.

## Prepare the recurring workload offline

The four warm-query families alone do not cover the charter. `phase-plan` prepares a development fixture's authored states and intended transitions without making source or model calls:

```sh
uv run --frozen oriel rystic phase-plan \
  --manifest .rystic-workspace/rystic-seed.json \
  --output benchmark-runs/phase-preparation.json
```

The plan has four phases in a fixed order: baseline, resolved issues, duplicate customer mapping, then restored mapping with required account policy removed. Baseline and refreshed states each contain the original four tasks plus a guarded answer control. The two invalid states require abstention with the exact affected records and fields. That gives twelve tasks per fixture. The guarded question and schema use the same contract for valid and invalid data. Public provenance uses a uniform hash-derived observation alias, so the original receipt ID prefix cannot reveal whether to answer or abstain. Phase names and expected outcomes stay with the evaluator.

The refreshed gold is recomputed independently from its authored manifest. Returning old numbers with a new provenance hash fails. An invalid cohort must produce a source-validation receipt and a refused refresh, never a successful workspace-import receipt. Both MCP paths now expose the same recorded `refresh_status`; failed or interrupted refreshes block snapshot reads while leaving direct-source reads available. Schema-faithful source doubles and real local MCP subprocesses verify this behavior offline. The new refusal/recovery path still needs native Rystic integration verification before executing acceptance phases.

Complete valid validation receipts retain the normalized snapshot hash. Complete supported refusals instead hash the full observed cohort projection, including unaffected records, and explicitly label that representation. Unknown failures certify neither hash nor blockers. The independent task oracle reads authored intent; the source validator reads actual API fields and does not consult gold. Treat shared validation/preflight work as an explicit accounting item, allocated by the frozen policy; providing the same receipt to both agents does not make its collection free.

Intent hashes are explicitly separate from observed source hashes, which remain `null`. Native issue resolution was verified for the original fixture; the new customer metadata transitions still need native API support/read-back checks. No generated plan authorizes source mutations, inference or spending, and the inspected development fixture is not a holdout.

To allocate fresh candidate fixtures and preserve phase order while counterbalancing arms within each phase:

```sh
# Supply the complete actual development-seed inventory.
uv run --frozen oriel rystic allocate-study \
  --master-seed 20261009 --development-seed 7 --development-seed 31 \
  --workload recurring_v1 --output benchmark-runs/recurring-allocation.json
```

At the default sizes this prepares 30 batches × twelve tasks × three repeats × two arms = 2,160 planned trials. This is an unexecuted design, not a power recommendation or an approved run budget. The small eight-trial development pilot remains the next live comparison. Final questions, schemas, expected-answer hashes and actual phase receipts must be bound before acceptance measurement.

### Compile task semantics before source changes

```sh
uv run --frozen oriel rystic task-templates \
  --plan benchmark-runs/phase-preparation.json \
  --output benchmark-runs/task-templates.json
```

This offline command validates the complete plan and compiles all twelve question templates, response schemas, semantic expected answers and scorer commitments. It leaves exactly one source-provenance slot in each question. Two different internal renderings must yield identical commitments, so placeholder values cannot leak into the question or expected business answer. No placeholder source hash or invented observation is exported.

The output is evaluator-only development preparation, not a complete frozen statistical protocol. It contains expected answers; never send the whole file to an agent. Later observed bindings must match these commitments. See the [sequential collection design](sequential-study.md) for the baseline-provisioning, protocol-freeze and accounting requirements.

### Apply one declared source transition when live work resumes

`apply-phase` performs source writes and is separate from agent-facing tools. It requires the exact reviewed prepared-plan and action hashes, a new receipt path and a pinned pair on numeric loopback. The following is a future live command; offline preparation does not execute it:

```sh
uv run --frozen oriel rystic apply-phase \
  --plan benchmark-runs/phase-preparation.json \
  --from-phase baseline --to-phase refreshed \
  --approve-plan REVIEWED_PLAN_SHA256 --approve-action REVIEWED_ACTION_SHA256 \
  --output benchmark-runs/baseline-to-refreshed-transition.json
```

Before writing, the executor verifies the complete cohort against the declared before-state. It applies only the adjacent plan's issue workflow or customer-metadata operations, then reads back and validates the complete after-state, including expected invalid-input blockers. Full captures and per-operation progress stay in an owner-only local receipt; stdout contains a summary. The receipt is checkpointed before each possible write. Existing receipt paths are refused.

An ambiguous write response stops remaining writes and still attempts read-back. No automatic retry or rollback occurs; even if the expected after-state is observed, an uncertain write remains explicitly uncertain. A storage failure also stops subsequent writes. Checkpoints use file flush/fsync and atomic replacement; they are not a guarantee against machine or power loss. Inspect the retained evidence before taking further action. A completed transition does not refresh the workspace: use a separate captured `sync` attempt and retain its success or refusal. Native support for the metadata operations remains unverified; current executor tests use in-memory source doubles.

### Check saved phase evidence

`bind-phase` checks a saved development capture against its prepared source intent and freezes the corresponding task artifacts. It makes no source or model calls. A capture must contain the complete return value of `capture_validation`, including the observed cohort records; a receipt alone is insufficient. For valid phases, supply the matching successful full-cohort snapshot receipt:

```sh
uv run --frozen oriel rystic bind-phase \
  --plan benchmark-runs/phase-preparation.json --phase baseline \
  --validation benchmark-runs/baseline-validation-capture.json \
  --snapshot benchmark-runs/baseline-snapshot-receipt.json \
  --evidence-origin recorded_native \
  --output benchmark-runs/baseline-task-binding.json
```

These capture files must come from a separately authorized collection run; `bind-phase` does not create them. A valid snapshot must reference the exact supplied validation receipt. When native collection resumes, the optional `sync --capture-output` flag retains that attempt's full capture while stdout returns the successful snapshot receipt. Choose new paths for both:

```sh
# Contacts local simulators. Run only when the live collection phase resumes.
uv run --frozen oriel --workspace .rystic-workspace-live rystic sync \
  --capture-output benchmark-runs/baseline-validation-capture.json \
  > benchmark-runs/baseline-snapshot-receipt.json
```

The capture is written before import, using a new file with owner-only permissions. An existing capture path is refused before source access. Complete source refusals and unverified failures also retain their capture, while sync exits unsuccessfully and snapshot reads stay blocked. A capture-write failure prevents import; any partial capture is invalid evidence. The source receipt always says `workspace_imported: false`; only a separate successful snapshot receipt asserts import. Account for capture writing as part of the measured preparation work.

Use `synthetic_test` (the default) for authored source doubles. The origin label is an operator assertion, not authenticated evidence. For a guarded phase, omit `--snapshot`: a refused source state must not be represented as a successful import. A separate later capture cannot replace the receipt associated with an earlier import. The retention-to-binding path is verified with local source doubles; native verification remains deferred.

The check recomputes source hashes and normalization, checks receipt status and blockers, and matches the full physical customer, payment and issue sets to intent. It also checks workflow state IDs and preserves each issue's explicit account mapping when customer metadata becomes ambiguous. A matching total alone is insufficient. Mismatched records, stale receipts or unsupported source defects prevent task generation.

The new output contains agent-facing questions/schemas separately from evaluator-only answers, plus per-task hashes compatible with the study report contract. Send only the question and answer schema to the agent; the complete packet contains gold and must stay with the evaluator. Existing output files are never overwritten. This development binding does not establish native execution, complete pagination, freshness at a later time, held-out status or authorization to run a study. All performance claims remain ineligible until the complete measurement and evidence review.

### Run one observed development phase

The warm pilot and `phase-pilot` now share the same agent loop, source/workspace MCP factory, result calculator, model identity checks and budget/failure handling. This allows the guarded tasks to run after a refused refresh without using stale workspace rows. First record the selected phase with `sync --capture-output`, then freeze its plan offline:

```sh
uv run --frozen oriel --workspace .rystic-workspace-live rystic phase-pilot-plan \
  --prepared-plan benchmark-runs/phase-preparation.json --phase guard_a \
  --capture benchmark-runs/guard-a-validation-capture.json \
  --evidence-origin recorded_native --output benchmark-runs/guard-a-pilot-plan.json
```

The planner requires the exact capture recorded by the workspace's latest refresh attempt. It checks the approved policy and independently rebuilds the tasks. Valid phases contain five tasks/two arms; guarded phases contain one task/two arms. Each task's arm order is counterbalanced. Source doubles default to `synthetic_test` and cannot start the configured model through the CLI.

When live measurement resumes, review the new plan and its budget before execution:

```sh
uv run --frozen oriel --workspace .rystic-workspace-live rystic phase-pilot \
  --plan benchmark-runs/guard-a-pilot-plan.json --approve REVIEWED_PLAN_SHA256 \
  --allow-model-data --output benchmark-runs/guard-a-pilot-result.json
```

Execution rechecks current source state before and after the trials, including the complete refused cohort. Drift invalidates results; failed, late and unattempted trials stay visible. The command makes no source transitions. A separate phase pilot has its own budget and remains development evidence. Combining these reports does not create a held-out recurring study or supply its missing setup, transition and runtime accounting. Offline verification uses authored responses and source doubles; native/model runs remain deferred.

## Assess complete study receipts offline

`rystic study-report` checks supplied study receipts offline and selects the validator from the manifest's `spec`:

Observation ledgers have a separate bounded 128 MiB input allowance to accommodate the declared multi-batch study. Plans and other evaluation inputs retain their 8 MiB limit; this does not change source HTTP, MCP frame or model-context limits.

```sh
uv run --frozen oriel rystic study-report \
  --manifest benchmark-runs/acceptance-manifest.json \
  --observations benchmark-runs/acceptance-observations.json \
  --output benchmark-runs/acceptance-report.json
```

These acceptance inputs do not exist yet. The [original statistical input contract](../contextbase/study_report.py) requires a frozen complete schedule, source/model/context/runtime identities, held-out status, a reviewed power-analysis reference, and every attempted run with timestamps and explicit costs. Each independent paired batch carries setup, refresh, common-review and runtime overhead. Unknown values remain unknown: a missing or null component makes the result inconclusive. Actual costs include failed attempts and retries; late or unanswered results receive the predeclared time penalty.

The summarizer resamples whole paired batches, retaining repeated-task and shared-overhead dependence. It checks the point targets and 95% paired intervals for accuracy lift, cost per correct answer, total spend and capped time. Accuracy and latency use predeclared task weights; total spend and cost per correct answer use actual workload totals. Thirty batches is a minimum guard, not a substitute for a reviewed power analysis. Intervals are marginal, not a simultaneous-confidence claim.

`statistical_acceptance` is conditional on the supplied evidence and declarations. The tool cannot authenticate a receipt, prove fixture independence or establish that a holdout stayed unused during tuning. It therefore always leaves `claim_eligible: false` for separate evidence review. Unit tests use explicitly fabricated arrays; they are not an observed benchmark.

The original contract supports source-state phases: each task can declare `phase_id`, and each batch supplies an exact `phase_snapshots` mapping. A refreshed task must cite its refreshed snapshot hash; an old baseline hash is rejected. The whole independent batch remains the resampling unit. However, requiring every future observed hash in the initial freeze cannot support a native sequential run whose transitions generate timestamps or other unpredictable source values. Do not fabricate those hashes or refreeze the protocol after seeing earlier model outcomes.

The [sequential contract](../contextbase/sequential_study.py), `contextbase.sequential-study/v2`, addresses this ordering problem. Its initial protocol commits the complete schedule, phase intents, question templates, response schemas, semantic gold and scorer. Only the explicitly declared source-provenance block is bound later. After each source transition, a chained phase receipt binds the actual observed capture and final task artifacts before either arm starts that phase's queries. Any other question, schema, gold or scorer change is rejected. Run provenance must name both the original protocol and its phase binding.

The v2 report validates that chain before resolving the known observations for the existing statistical machinery. It preserves the original freeze, actual costs, times, failures and accounting. The internal resolved manifest is explicitly labelled a statistical projection, never a new preregistration. Capture ownership and shared-cost allocation still require a reviewed collection policy; the report preserves supplied preparation and first-use waiting spans without inventing allocations.

Current `phase-binding/v1` packets are development evidence even when an operator labels their source capture `recorded_native`. Accordingly, v2 reports always remain acceptance-inconclusive with `charter_ready: false`; changing packet flags cannot promote them. A [distinct acceptance path](sequential-study.md#offline-acceptance-preparation-commands) validates fresh allocated baselines, reviewed protocol inputs and full phase bindings. The offline acceptance collector/report checks retained common-engine receipts, original phase order, cold use and separate event accounting.

The [study controller](../contextbase/study_controller.py) now enforces cumulative cost, call and elapsed budgets across the frozen schedule. It uses the exact assigned simulator pairs, installs the unchanged approved context into each fresh workspace before its first import, and retains the actual capture used to bind each phase. Both arms and postflight complete before the next source transition. Unknown charges, uncertain operations or failed checks stop further execution and preserve partial evidence; automatic resume is disabled. End-to-end tests use source doubles, local DuckDB and scripted responses. They are neither native controller execution nor a performance benchmark. Isolated native lifecycle, provisioning and authorization integration, actual power/accounting/rate reviews, and the full approved study remain necessary.

The measured controller runtime starts after offline protocol/input validation and runtime construction, and ends before final statistical reporting and export. These excluded analysis costs and durations are unmeasured, not zero. Frozen rate declarations and retained workload charges still require reconciliation with complete billing evidence before an all-in claim can be made.

The optional `contextbase.recurring-workload/v1` profile is required for the report's recurring-benchmark `charter_ready` field. It binds every run to immutable question/schema/scorer/gold hashes, requires baseline/refreshed/guarded coverage, a single-record control and a positive guarded-answer control, and verifies transition order and per-arm readiness before dependent runs. Invalid-source workspace phases must carry a validation/refusal receipt; they cannot claim materialization. Phase costs and times must reconcile to the authoritative batch totals, including the observed preparation span. Cold-start spans must fit their batch accounting window. Measurement receipts cannot be reused across roles, and unknown phase cost fields are rejected. Missing transitions, readiness or accounting make a profiled study inconclusive. A generic warm-only statistical result has `charter_ready: false`.

The report separately exposes p50/p95 query latency and sample counts, overall and by phase, with the quantile convention and treatment of failures stated. These unweighted distributions exclude batch overhead; the primary weighted capped-time measure includes it. First-use latency stays unknown unless explicit cold-start/first-run receipts identify each arm's first planned query and include its preparation time. Wrong or late first answers retain the declared capped-time penalty.

The per-task scorecard publishes both arms' correctness, attempts, costs and capped time, including controls where the source path wins. Batch overhead is allocated equally per planned run for that descriptive scorecard. These task-level differences are not separate statistical-significance claims.

`charter_ready` in the original report covers the conditional recurring-benchmark evidence contract and statistical gates only. It does not establish authentic source/model receipts, product adoption, Registry integration or permission to publish. `claim_eligible` remains false for separate evidence review. Before a full sequential study, freeze the semantic task contracts and collection policy; retain each transition and bind its observed state before dependent queries. Allocating a refresh cost alone is insufficient evidence of refreshed-answer correctness.

## How the result supports the product

Publish a scorecard with accuracy, cost and time together, alongside cold setup, refresh, failure cases, source limitations and raw traces. A passing study supports a claim about the declared synthetic workload. It does not establish that every tool, task or production MCP integration will see the same benefit.

The [separate export recipe](tool-exports.md) verifies the generic import and context lifecycle with a different synthetic schema. Own-tool activation and repeated use still need validation with design partners under approved access. Select later native connectors from those users' recurring jobs. Broader performance claims require additional source/workload studies; a wider connector list alone is not evidence. See the [build evidence and remaining work](build-status.md) for the current requirement-level status.
