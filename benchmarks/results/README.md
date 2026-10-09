# Recorded evidence

These are synthetic local runs, not a universal comparison with MCP. The model
run does **not** show an accuracy advantage over source tools given equal
business context. It does show that named metrics were useful in this small
sample, and replay measures lower data movement as the fixture grows.

## Real model run

[Full model trace](model-smoke.json) · 2026-10-08 UTC · 40 accounts · seed 7
· Python 3.12.12 · DuckDB 1.4.5 · CLI-reported model `claude-opus-5-5`

The configured model was used without an override. Each arm received the same
three questions and four-turn limit. One turn allowed up to four tool calls.
The twelve trials ran in seeded random order. All trials finished; none were
discarded or retried with different conditions.

| Arm | Correct | Model invocations | Tool calls | Total wall seconds | CLI cost estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Source tools + context | 3/3 | 9 | 11 | 42.93 | $0.760787 |
| SQL schema only | 2/3 | 11 | 11 | 47.77 | $0.607944 |
| SQL + same context | 3/3 | 9 | 10 | 32.62 | $0.658504 |
| Named metrics + same context | 3/3 | 6 | 9 | 22.97 | $0.379819 |

The schema-only arm answered eligible pipeline as $11,436,000 instead of
$8,760,000. It included S1, whose exclusion is an organization-specific policy
that the catalog cannot supply. This is evidence about missing business context,
not a claim that SQL cannot calculate the answer. The contextualized source-tool
arm answered all three questions correctly, including the cross-source risk task.

The run used 35 CLI invocations and took 146.39 seconds. The CLI's aggregate
list-price estimate was $2.4070542; it is not necessarily a subscription charge.
A separate arithmetic/authentication smoke call, excluded from these trials,
reported $0.032724. Native aggregate token counters remain separate:

| Native counter | Tokens |
| --- | ---: |
| `input_tokens` | 70 |
| `cache_creation_input_tokens` | 285,466 |
| `cache_read_input_tokens` | 12,431 |
| `output_tokens` | 6,028 |

The small `input_tokens` count does not mean that the experiment used only 70
prompt tokens. The CLI reports most prompt processing under cache creation.
The full artifact retains each native usage object and `model_usage.costBasis`.
The adapter starts a fresh restricted CLI invocation for each action and sends
the conversation to date; startup, repeated context and cache behavior affect
these timings and estimates. No statistical significance is claimed from three
questions per arm and one run.

## Authored plan replay

All four tasks across all four arms matched an independent Python arithmetic
oracle at both sizes. These 32 plan checks are not LLM accuracy measurements.

| Fixture | Arm | Handler calls | Response bytes |
| --- | --- | ---: | ---: |
| [40 accounts](replay.json) | Source tools + context | 16 | 75,994 |
| 40 accounts | SQL schema only | 8 | 5,623 |
| 40 accounts | SQL + same context | 12 | 57,184 |
| 40 accounts | Named metrics + same context | 12 | 60,647 |
| [400 accounts](replay-400.json) | Source tools + context | 39 | 244,002 |
| 400 accounts | SQL schema only | 8 | 5,647 |
| 400 accounts | SQL + same context | 12 | 57,208 |
| 400 accounts | Named metrics + same context | 12 | 60,671 |

At 400 accounts (2,980 rows across five source tables), source tools returned
4.27 times as many serialized response bytes and required 3.25 times as many
handler calls as SQL with the same context. Both include catalog and full context
retrieval once per task. SQL's smaller schema-only numbers do not represent an
equal-context comparison. Metric responses include SQL and execution provenance,
which explains their slightly larger payloads.

Measured fixture ingestion was 0.164836 seconds at 40 accounts and 1.784368 seconds
at 400 accounts, recorded separately from query execution. No external SaaS
network, refresh, or MCP transport cost is included. Serialized bytes are not
token estimates. See the [methodology](../../docs/benchmarks.md) for limits,
reproduction commands and the experiments required before broader claims.
