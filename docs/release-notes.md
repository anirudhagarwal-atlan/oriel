# Local MVP validation

Oriel 0.1.0 is prepared for the [public source repository](https://github.com/anirudhagarwal-atlan/oriel). Install from a checkout; no package-registry release or hosted deployment is advertised.

The package supports Python 3.10–3.14 and uses DuckDB 1.4.5. The optional `connectors` extra adds dlt 1.24.0, PyYAML 6.0.3 and their locked dependencies; the build backend is setuptools 84.0.0. Locked artifacts retain the configured release-age cutoff. Runtime code does not depend on the repository's example files.

Current connector validation: **186 relevant unit and integration tests pass** under Python 3.12.12. Actual dlt extraction from synthetic SQLite and loopback REST/GraphQL passes configure → sync → bootstrap → query → refresh, retaining approved context. API partial failures retain prior snapshots. The 101-entry catalog distinguishes definition coverage, controlled adapter tests, selected native fixture checks and authenticated live verification. [Setup and exact verification scope](connectors.md).

Earlier base-MVP validation covered 46 tests and an isolated wheel import exercising demo creation, bootstrap, metric proposal/approval, execution and bundle export. MCP stdio matched direct metric execution, and the project page was checked at desktop and mobile sizes. Those earlier checks are separate from the current connector verification; no new model calls were made for connector work.

## Boundaries exercised

- Read-only SQL rejects writes, multiple statements, file/network reads, attachment, extension installation, and configuration changes.
- Local imports and transformations are separate operator commands; Dinobase imports use read-only attachment and a specific local file allowlist.
- Change approvals bind to an exact before/after preview, retain precise decimal evidence, reject stale rows, and reject replay.
- Metric approval binds to the proposal, schema, parent context and scalar execution witness; revisions retain history.
- Synthetic-model access requires unchanged generated data and curated context. Imported or edited workspaces require explicit model-data consent.
- MCP and direct tool calls produce the same metric result. The MCP surface has no change-approval tool.

These are application safeguards for a trusted local workspace. DuckDB is an embedded engine, not a multi-tenant security boundary; untrusted database files or hostile SQL require operating-system isolation. [DuckDB's security model](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview).

## Evidence and interpretation

The recorded model experiment used the configured Claude CLI with native tools and other MCP servers unavailable to the run. All model-visible fixtures were synthetic. The same-context source/SQL comparison tied on three questions; the stage-definition omission explains the schema-only miss. All four arms and every trial are retained in [the result packet](../benchmarks/results/README.md).

Source configuration and refresh are implemented. Cloud client and authenticated vendor compatibility remain unverified; native fixture results apply only to their recorded sources and resources. Live upstream writes, production identity and authorization, registry upload, and a broad multi-model evaluation remain outside this implementation. Registry output is a local reviewable payload. No claim of production certification follows from a successful SQL witness.
