# Preparing and collecting the recurring study

The study must compare the complete recurring analytics job: first use, repeated questions, a changed source state and appropriate abstention after invalid input. The controller now installs frozen context, binds observed phases, runs the common agent loop and retains a cumulative budget across the original schedule. It has been exercised with synthetic source doubles and scripted answers. No comparative Rystic model result exists, and native fixture lifecycle and complete billing reconciliation remain unverified.

## Commit meaning before measuring answers

Native seeding assigns customer, payment and issue IDs, issue identifiers and workflow identities. Those values occur in questions, expected issue sets and exact abstention blockers. They must be known before the current semantic task contract can be frozen. Later transitions can also generate timestamps that cannot honestly be predicted at that freeze.

The intended sequence is:

1. **Allocate the study.** Commit disjoint declared development seeds, fixture sizes, independent batches, repeats and paired task order with `allocate-study --workload recurring_v1`. This step creates no fixtures and authorizes no spending.
2. **Provision baseline fixtures.** After explicit runtime and spending authorization, seed isolated baselines through native APIs and retain complete read-back evidence and physical identities. Verify each batch against its allocated seed, size and variant. Do not apply future transitions or run model tasks. The isolation and lifecycle of all required native fixtures still need verification; the compiler must not assume an untested reset, clone or restore facility.
3. **Compile authored task semantics.** Derive baseline, refreshed and invalid-input intents, exact transition actions, question templates, unchanged response schemas and independent expected business answers. The current development commands are `phase-plan` and `task-templates`. The template compiler emits no observed source hash. Only one explicit provenance slot remains to be filled later; all other question and scoring content is committed.
4. **Freeze the complete protocol.** `acceptance-protocol` binds allocation, provisioning, intent and template hashes; the model, runtime, context, renderer and scorer; the full schedule; reviewed power assumptions; accounting policy; and execution limits. Keep future observations absent. Every allocated batch must be present in order. Current development artifacts cannot be upgraded by changing their labels.
5. **Collect each phase in order.** Finish both arms of the prior phase. Verify the declared before-state, apply only the approved source changes, and capture the complete observed result. Import a valid cohort or retain a structured refusal that blocks stale workspace reads. `acceptance-bind` records a new binding to the original protocol, full capture, snapshot or refusal, and previous transition/binding chain. The supported contract requires a fresh workspace for baseline and the correct changed-snapshot predecessor for refresh. `acceptance-run` enforces this order and the cumulative budget; the report independently checks retained receipts. Native execution still requires its integration pilot and approved study inputs.
6. **Retain failures and costs.** A lost write response may still have changed a source. Stop further writes, retain the uncertainty and read back where possible. Unknown source state, unknown spend or an incomplete planned schedule leaves the study inconclusive. Do not regenerate gold, refreeze after outcomes or silently repeat mutations.
7. **Assess the complete study.** Resample whole independent paired batches and retain the charter's accuracy, cost and speed gates together. Publish controls, losses, cold use, refresh and uncertainty. Independent evidence review remains necessary before a public claim.

The current template compiler is evaluator preparation. Its complete output includes expected answers and must never be sent to the agent. Only each rendered public question and response schema belongs in a model prompt.

## Offline acceptance preparation commands

These commands consume supplied evidence and perform no source or model calls. The examples require future native provisioning and review files that have not been collected. Current tests use explicitly fabricated sources, reviews and costs; their passing checks are not benchmark results.

```sh
uv run --frozen oriel rystic acceptance-intent \
  --allocation benchmark-runs/recurring-allocation.json --batch batch_001 \
  --manifest benchmark-runs/batch-001-seed.json \
  --capture benchmark-runs/batch-001-provisioning-capture.json \
  --provisioning benchmark-runs/batch-001-provisioning.json \
  --evidence-origin recorded_native --output benchmark-runs/batch-001-intent.json

# The JSON array must include every allocated intent in its original order.
uv run --frozen oriel rystic acceptance-protocol \
  --allocation benchmark-runs/recurring-allocation.json \
  --intents benchmark-runs/acceptance-intents.json \
  --settings benchmark-runs/reviewed-study-settings.json \
  --historical-context-events benchmark-runs/context-authoring-review-events.json \
  --output benchmark-runs/acceptance-protocol.json

# This capture/import must occur after protocol freeze; provisioning capture
# cannot stand in for the measured cold import.
uv run --frozen oriel rystic acceptance-bind \
  --protocol benchmark-runs/acceptance-protocol.json --batch batch_001 --phase baseline \
  --capture benchmark-runs/batch-001-baseline-capture.json \
  --snapshot benchmark-runs/batch-001-baseline-snapshot.json \
  --output benchmark-runs/batch-001-baseline-binding.json
```

Later bindings require `--previous-binding` and `--transition` pointing to the adjacent completed evidence chain. Guarded phases omit `--snapshot`. Complete stored sync receipts are required for valid phases; a minimal fabricated snapshot or prior imported workspace does not meet this contract. Files are created exclusively with owner-only permissions. CLI output contains summaries; full artifacts include evaluator gold and retained source evidence.

The [intent contract](../contextbase/acceptance_intent.py) replays the declared allocation, checks the allocated seed/size/variant, verifies complete baseline read-back and reconstructs every phase/template. New allocations retain fixture-size ordering so their randomization is reproducible. Older allocation files missing that parameter are rejected instead of guessed. Seed replay verifies case and monetary semantics; physical IDs and owners are bound to read-back. Exact discovered owner-roster ordering, separate refund objects and team discovery are not independently replayed.

The [protocol contract](../contextbase/acceptance_protocol.py) requires explicit settings for the model, context identity, power review, accounting review, budgets and bootstrap. Power review includes assumptions, methods, analysis reference, exact targets, estimated joint power and a separate reviewer. Accounting review includes event ownership, rate basis, shared allocations, clock/cache rules and unknown-cost handling. Its classification must be reviewed before provisioning begins. Blank content, missing reviews, unknown provisioning costs, incomplete batch coverage and a total cap that omits apparatus spending are rejected. The compiler validates the supplied declarations; it does not authenticate reviewers or perform a power analysis.

## Account for the whole authorized study

Synthetic fixture creation and product ingestion are different measured activities. An accounting policy may classify fixture creation as benchmark apparatus, reported separately from comparative workload ratios. That choice must be declared before provisioning, and fixture/runtime costs still consume the total authorized study budget. If fixture creation belongs in the primary workload, commit its common-cost allocation before provisioning and retain its actual earlier timestamps. Never relabel pre-freeze expense as post-freeze work.

Product cold ingestion, source/MCP startup, context creation and review, refresh, model/tool calls, runtime/license allocation, retries and failures remain part of the declared comparison. Keep the total study spending limit separate from any comparative-workload cap. Unknown measurements stay unknown; the preparer must not fill them with zero.

The collection policy must identify which operations belong to each arm, how shared review and capture costs are allocated, the clock boundaries for cold use and waiting, and the cache/freshness policy. It must distinguish evaluator validation from the capture needed to import a workspace. Current reports preserve supplied measurements; they do not determine these allocations.

The initial acceptance protocol supports a conservative fixed event mapping. Identical common context creation/review is split equally; source-specific workflow configuration and tool/model work belong to the incurring arm. Configuration means preparing this fixture's workflow, not product development or changing tools after inspecting held-out results. Such changes require retiring the affected study and allocating fresh fixtures. Previously incurred context authoring/review must retain its actual timing; only reproduction or installation of the frozen content can be measured as later work.

Phase-transition writes are charged through a separate transition ledger. Their elapsed time is not inserted into a post-transition preparation window. The acceptance report consumes these events directly and adds their allocations to paired batch totals before resampling. Earlier context authoring/review events are frozen into the original protocol with their actual timestamps. They cannot be invented or changed when results arrive. The development report remains a separate format.

## Offline receipt collection and reporting

`acceptance-execution-plan --protocol … --binding … --output …` prepares exact common-engine inputs for one bound phase. Questions, schemas, expected answers and repeated paired order are reconstructed from the original protocol. Optional `--limits` can only lower phase budgets. This command makes no calls and grants no execution permission. Use the separate controller for cumulative execution limits; independently launching phase plans would reset their local budgets.

The common engine now records stable queue/trial identities, actual UTC trial, inference, tool and verification intervals, argument hashes, model identities and paid failures. Its optional frozen-model check stops further spend on a mismatch. Full receipts contain private evaluator gold. Only public task questions/schemas and the permitted policy/tool results belong in model prompts.

The collection JSON uses `spec: contextbase.acceptance-collection/v1`, `protocol_sha256`, ordered `phases` containing each original `binding` and `execution`, plus `events` and `first_use`. See the exact event fields in [acceptance_accounting.py](../contextbase/acceptance_accounting.py). Each event retains kind, scope, actual interval, cost or explicit unknown, arm allocation and evidence hash. `exclusive_overhead` adds cost and time; `cost_only` adds separately billed fees without duplicating elapsed time; `within_attempt` describes work already owned by a measured trial. Separate source fees cannot be hidden inside model spend. Provisioning stays in the total study budget and is never added twice to the comparative ratios.

```sh
uv run --frozen oriel rystic acceptance-pack \
  --protocol benchmark-runs/acceptance-protocol.json \
  --collection benchmark-runs/acceptance-collection.json \
  --store benchmark-runs/receipts --output benchmark-runs/collection-index.json

uv run --frozen oriel rystic acceptance-report \
  --index benchmark-runs/collection-index.json --store benchmark-runs/receipts \
  --output benchmark-runs/acceptance-report.json
```

The private content-addressed store checks canonical JSON, digests and file types, refuses overwrites/symlinks, and stores recursive binding history once. The index names only digest references. Artifact reads and the distinct-artifact collection read are capped at 128 MiB; oversized studies need a streaming collector before use. Publication fsyncs artifact contents but does not promise directory durability across power loss.

The report independently rescores final paid answers and checks every planned trial, model identity, tool request, known cost, phase transition, cold start and verification envelope. It refuses unrecognized charge fields, omitted tool execution, falsely downgraded correct answers, retimed review/transition work, overlapping overhead and missing fee coverage. Wrong, late and failed trials stay in the denominator. Unknown costs and incomplete execution remain inconclusive, with known paid amounts retained when their execution records validate. Cold wall time includes any shared-barrier waiting and is reported separately from allocated service time. The paired statistical calculation reuses the existing whole-batch metrics; it does not project events into fictional development-report windows.

Offline tests include real shared-loop receipts with scripted responses, fabricated complete accounting scenarios and local DuckDB binding integration. They establish implementation behavior, not native performance. A passing conditional report still cannot authenticate provider receipts, review quality, the strongest source baseline or held-out independence, and always leaves public-claim eligibility false.

For a complete offline rehearsal from the source checkout, run `.venv/bin/python tests/run_controller_rehearsal.py`. It compiles and validates all 30 synthetic batches, runs their 120 phases and 720 scripted answers, and prints the private temporary output directory. Native requests and model constructors are blocked. The scripted client deliberately reads evaluator gold, so its answers are implementation tests and provide no evidence of agent accuracy. This longer rehearsal is separate from the ordinary unit suite.

## Sequential execution controller

`acceptance-run` requires the original protocol hash, permission for the synthetic source-to-model flow, permission for its exact local source transitions, and a fresh output directory. These flags authorize an execution; preparing a protocol or reading this example does not. The CLI uses the configured model and pre-provisioned native simulator pairs. It does not create, reset, clone, stop or authenticate them. No native execution is being requested by the offline preparation workflow.

```sh
# Future authorized native study; not an offline preparation command.
uv run --frozen oriel rystic acceptance-run \
  --protocol benchmark-runs/acceptance-protocol.json \
  --cost-contract benchmark-runs/reviewed-execution-costs.json \
  --context benchmark-runs/frozen-approved-context.json \
  --output benchmark-runs/new-acceptance-execution \
  --approve EXACT_PROTOCOL_SHA256 --allow-model-data --allow-source-transitions
```

The [execution cost contract](../contextbase/study_costs.py) has `spec: contextbase.execution-cost-contract/v1`, `currency: USD`, `rules` and `assignments`. Its canonical hash must be the original accounting review's `evidence_sha256`; prices and assignments cannot be supplied or changed after outcomes arrive. Every batch must name its provisioned environment and two distinct numeric loopback ports. Shared ports and missing batch assignments are rejected before source access.

Each rule includes `method`, `rate`, `max_seconds`, `allocation`, `billing_evidence_sha256` and `rationale`. Pricing is explicitly zero, fixed USD per operation, or USD per measured second. All operation kinds in `COST_KINDS` are required. Provider charges supply model costs; frozen events supply previous context creation/review and provisioning charges. Evaluator validation is one bundle per phase, allocated across its actual nonoverlapping intervals. Runtime/license allocation is one bundle across the execution interval. Source-tool and MCP-startup rules currently support explicit zero fees only: their elapsed time is measured inside trials, but separate nonzero per-call billing is not instrumented. Do not use this controller for a pricing arrangement it cannot represent.

The private budget journal reserves maximum costs before work, keeps comparison and total-study caps separate, counts model-call reservations conservatively, and never resets at a phase boundary. A reservation can consume the call cap even if a later check prevents dispatch. Actual paid amounts are retained when they exceed a reservation; further execution stops. Unknown model charges and uncertain source writes retain unresolved reservations. Request guards recheck native source deadlines before each HTTP request and reduce its socket timeout. An already running request can still exceed that timeout; the controller cannot promise a hard operating-system deadline.

The controller verifies the exact installed context and full source state, retains original captures and failed binding/transition receipts, and checks the budget before model calls, MCP startup, source tools and postflight reads. Each checkpoint records completed phases separately from partially recorded phases, along with unexecuted schedule entries. Interrupted or uncertain runs cannot resume automatically. A collection containing no executed phases can still be reopened for inspection.

If a budget receipt cannot be published after a paid call, execution stops. When other storage remains writable, the controller preserves the paid response and separately records later known fees that could not be journaled. `budget_journal_complete: false` means its subtotal is incomplete; inspect the collection events and unpublished settlement references. No software can guarantee new receipts when all storage is unavailable.

**Accounting boundary:** the runtime interval begins after offline protocol/input validation and runtime construction, and ends before final statistical reporting/export. These activities are excluded analysis overhead, not observed zero costs. Full native billing, idle/provisioned runtime and these exclusions still need reconciliation with the frozen accounting policy before an all-in claim. Both controller execution and standalone acceptance-report replay preserve scoped statistical results but keep `charter_ready: false` while billing reconciliation is missing, even if those statistical gates pass. Repacking or replaying a collection does not establish reconciliation. Recording the limitation alone is not a release gate. Offline success does not satisfy the release requirements.

## Required acceptance inputs

An acceptance intent must bind the allocation and batch, generator/seed/size/variant, native provisioning evidence, fixed physical identity mapping, simulator versions, isolation and holdout review, phase intents/actions and task-template/scorer commitments. An acceptance binding must then reference that intent and the original protocol, preceding binding and transition, complete observed capture, successful snapshot association or refusal, final task hashes and actual timestamps.

These distinct intent, protocol, binding, event-ledger and report contracts and the sequential controller are implemented and tested offline. Existing `phase-binding/v1` packets remain development-only, and the sequential v2 report keeps them acceptance-inconclusive. Remaining work includes native controller integration, isolated fixture provisioning/lifecycle, real reviews and billing reconciliation, and the approved full study. A source-origin label or matching hash does not authenticate native execution or prove that a holdout stayed unused during tuning. Compilation and binding always leave execution authorization and public-claim eligibility false.

A power review needs an actual analysis artifact with assumptions about paired batch variation, target effects, failure rates and all three required improvements. Thirty batches is a lower guard; repeats do not replace independent batches. Bootstrap replicate count controls interval estimation, not study power. The bounded development pilot should inform these assumptions before the acceptance budget is set.

See the [benchmark commands and evidence limits](rystic-benchmark.md) and [requirement-level build status](build-status.md).
