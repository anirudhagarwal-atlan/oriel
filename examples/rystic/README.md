# Rystic integration evidence

This is the first synthetic benchmark fixture for a product that analyzes data across users' existing tools. The Stripe + Linear pair defines this evidence set; the [generic workflow](../../README.md#start-with-your-existing-tools) accepts other supported tool exports and snapshots.

This example connects **synthetic Stripe payments and Linear issues** through local Rystic simulators, imports the selected cohort into DuckDB, and reuses a reviewed metric after a source change. It asks which paying customers have unresolved urgent/high issues, their associated net collected USD, and issue owners.

Try the capture from the repository root:

```sh
uv run oriel --workspace .rystic-workspace-demo rystic load-example
uv run oriel --workspace .rystic-workspace-demo rystic preview
```

The bundled [source capture](../../contextbase/data/rystic-stripe-linear.json) came from an actual native Rystic run. It is hash-checked at load and contains the initial fixture's 12 customers, 24 charges and 17 issues. The preview returns **5 customers and $1,857.69**, with SQL, rules and source provenance. It is provisional until the metric is explicitly reviewed and approved.

| Artifact | Evidence |
| --- | --- |
| [functional-proof.json](functional-proof.json) | Initial answer matched an independent oracle; unchanged refresh preserved its data hash; resolving two synthetic issues and refreshing changed the answer to 4 customers and $1,490.61 while preserving the context version. |
| [source-tools-smoke.json](source-tools-smoke.json) | Live source reads and pagination, GraphQL aliases/fragments/projections/variables, fixture identity parity, shared context, MCP initialization and tool execution, and read-only denials. |

Pinned components: Rystic CLI `0.0.25`, Stripe simulator `0.0.8`, Linear simulator `0.0.1`. The receipts retain the simulator model hashes. Source-tool pagination counts include the simulators' built-in records; the analytics snapshot selects only the explicitly marked recipe cohort.

These are **functional verification receipts, with zero model calls**. The agent benchmark is deferred. They do not establish an accuracy, cost or speed advantage, or fidelity to the production APIs. Linear's ignored `issues.filter` behavior was reproduced and recorded. Stripe subscriptions and invoices are unsupported; the metric is historical net collected payments associated with unresolved issues, not ARR or predicted loss.

The [full walkthrough](../../docs/rystic.md) covers context proposal and exact-hash approval, report and bundle export, starting the pinned simulators on loopback, live seeding/sync, and the source versus SQL/metric MCP entry points. Fixture setup uses ordinary local simulator API creates, not Rystic's premium state-seeding endpoint. Live runtime access requires an available trial or license; this captured walkthrough runs without a Rystic account. No proprietary simulator binaries or service credentials are included.
