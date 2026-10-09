# Benchmark the claim before publishing it

**Current stage:** build and verify the synthetic Stripe + Linear workflow using Rystic. Comparative benchmarking is deferred. The [Rystic verification plan](rystic-benchmark.md) preserves accuracy, total cost per correct answer and time to a correct answer as release gates, with held-out tasks and full lifecycle accounting. It is not implemented by the commands below, and no run has passed it. See the [build charter](build-charter.md).

The hypothesis is that materializing internal-tool data in DuckDB reduces data
movement and makes cross-source analysis easier, while explicit business context
and metric contracts reduce interpretation errors. These are separate claims.
MCP can expose any of these designs, including SQL. This benchmark compares tool
surfaces and context availability; it cannot show that a database is better than
the MCP protocol.

The [recorded runs](../benchmarks/results/README.md) include a real twelve-trial
model experiment and plan replays at 40 and 400 accounts. In the model run,
source tools with context, SQL with the same context, and metrics each answered
3/3 questions correctly. Schema-only SQL answered 2/3. This does not establish a
SQL accuracy advantage over an equally contextualized source-tool baseline.

From an installed checkout, create a fresh fixture and keep your results separate
from the committed evidence:

```sh
uv run oriel --workspace .benchmark-40 demo --accounts 40 --seed 7
uv run oriel --workspace .benchmark-40 benchmark --mode replay --output benchmark-runs/replay.json
uv run oriel --workspace .benchmark-40 benchmark --mode model --output benchmark-runs/model.json
```

The model command uses the existing authenticated Claude CLI configuration and
makes real model calls. The replay command is local and requires no model account.
Use a new workspace path for a second fixture, for example `--accounts 400`.

## Four explicit arms

| Arm | Data operations | Business context | Purpose |
| --- | --- | --- | --- |
| `per_source` | Paginated records, equality filters, column projection | Full approved context | Source-tool baseline |
| `sql` | DuckDB SQL | Names, types and row counts only | Schema-only ablation |
| `sql_context` | DuckDB SQL | Same approved context as source tools | Access-path comparison with equal context |
| `metrics` | SQL plus named metric execution | Same approved context | Metric-contract execution |

Every arm has the same catalog. Context includes the curated definitions and SQL
implementations of the synthetic metrics; this is declared assistance, not
discovered domain knowledge. Primary access-path comparisons are `per_source`
versus `sql_context`; comparing source tools directly with `sql` changes two
variables. `sql_context` versus `metrics` measures the named execution surface.
The schema-only arm cannot infer an organization's private stage policy from
column types. Its eligible-pipeline task is intentionally under-specified without
context; a wrong guess measures missing business definitions, not weak SQL.

The source baseline supports filtering and projection rather than forcing full
record dumps. It has no source-side aggregate endpoint or general code execution.
Some real connector MCP servers have those capabilities and may perform better.
Mechanical replay permits Python postprocessing for source records; the live
model experiment exposes only the declared tools and requires the model to do
the remaining arithmetic. Do not equate those two experiments.

## Experiment 1: authored plan replay

`run_replay` executes four hand-authored plans across all four arms:

- Active paid ARR: active subscriptions with positive MRR, multiplied by 12.
- Eligible pipeline: ARR for stages S2, S3 and S4, closing August 1 through
  October 31, 2026. S1 and TCV are deliberate traps.
- At-risk ARR: paid active subscriptions, at least a 30% usage decline, and an
  open high or urgent ticket. Duplicate subscriptions, usage observations and
  tickets must not multiply revenue. Missing observations are unknown.
- Active accounts: distinct accounts with paid active subscriptions.

Ground truth is independently computed using Python `Decimal`, sets and date
comparisons from the fixture rows, without running the task SQL or metric code.
Tests cover fiscal boundaries, unpaid subscriptions, duplicate tickets, duplicate
usage observations and missing current observations.

The report records UTF-8 request and response bytes, tool-definition bytes,
number of handler calls, local elapsed seconds, exact tool traces, data hash and
runtime versions. Catalog and context retrieval are included once per task.
Bytes are actual serialized payload sizes; they are **not token estimates**.
The same fixed plan is used for both SQL arms, so replay cannot measure whether
context helps an LLM produce that plan. `plan_matches_gold` is a correctness
check on implementation, not model accuracy.

Ingestion is recorded separately as `ingest_seconds`; null means this invocation
did not measure it. Query time excludes ingestion, source API network latency,
refreshes, authentication, retries and storage. Report warm local results as
such. Measure cold sync and amortized refresh cost before making end-to-end
cost or latency claims. The tool handlers run in-process; any MCP round-trip
smoke check is compatibility validation, not a protocol speed comparison.

## Experiment 2: real model actions

`run_model_benchmark` runs the configured Claude CLI model, without overriding
its model selection, on three tasks and four arms by default. The seed controls
trial order. Each trial has the same four-turn budget and may request up to four
tools in one turn. The model must produce a structured numeric answer. The
harness checks it to half a cent; it never searches prose for a matching number
and uses no LLM judge. Failures and unanswered trials remain in the denominator.

The model is invoked in a fresh temporary directory with native tools, slash
commands, MCP servers and session persistence disabled. The Python harness
validates the JSON tool arguments and executes the declared read-only handlers.
The report contains the complete synthetic conversation and tool trace, the CLI's
reported cost estimate, input/output usage and cache counters, native model identifiers,
and both API and wall-clock durations when supplied by the CLI. The CLI may report
list-price cost rather than an actual charge on a subscription; preserve its
native `costBasis` field and label the estimate accordingly. Cache counters
remain separate. Subprocess error output and environment values are not saved.

Each inference receives the conversation to date, so CLI startup and repeated
prompt/context processing are part of the measured result. That differs from a
persistent native tool-use session and must be disclosed. The default guards are
48 CLI invocations, $3 reported total cost, $0.25 per invocation, 90 seconds per
invocation, and 15 minutes for the experiment. A subprocess cannot be interrupted
by the between-call total-time check, so elapsed time can exceed that total by
one bounded invocation. Unknown cost stops the run rather than bypassing its
budget. A small exploratory run is not a statistically reliable superiority
claim.

The bundled live runner accepts only an unchanged synthetic demo. It regenerates
the fixture from its seed and account count, checks all source rows, catalog and
curated context, and executes on the isolated regenerated copy. Imported tables,
edits or changed context cause it to stop before invoking the model. This also
keeps local import metadata out of the model's database. A private-data evaluation
requires a separate explicitly approved data flow. Never publish its trace.

## What a publication-quality study still requires

Run multiple repeats and fixture seeds, larger data sizes, more business domains,
and a held-out task set. Include source API aggregation/code tools where available,
equal context and computing budgets, fixed model/provider revisions, and paired
confidence intervals. Predeclare scoring and report every trial, truncation,
rate limit and error. Separate query, ingest, refresh and model costs. Include
tasks where source tools should win, such as one fresh record lookup. Publish the
trace artifacts and fixture hashes behind every claim.

## Relationship to Dinobase's benchmark

The reference audit uses [Dinobase commit
2dd0636](https://github.com/kappa90/dinobase/tree/2dd06369fc333cd51663c27b7d47a21a7946757b),
package version 0.2.9. Its published aggregate covers 15 RevOps questions across
11 models with one recorded run per arm, although the introductory README says
75 questions. The raw-tool baseline exposes 100-row list endpoints without
filters or aggregates, while SQL receives semantic annotations. Those results
support a specific comparison, not a universal claim about MCP.

The inspected scorer always invokes an LLM judge despite documentation describing
a deterministic/LLM hybrid. Judge cost is not included in the reported answer
cost; trace filenames omit repetition IDs; latency includes API retry delays.
This repository does not reuse the published percentages or infer new model
accuracy from deterministic replay.
