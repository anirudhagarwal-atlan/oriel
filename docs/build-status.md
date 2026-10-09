# What works today

Oriel is a **developer preview** for talk-to-data workflows over existing tools. This page records evidence as of 9 October 2026. Start with the [quickstart](quickstart.md); no model, simulator or registry account is needed for that example.

## Product readiness

| Capability | Verified behavior | Boundary |
| --- | --- | --- |
| Bring tool data into DuckDB | Local file imports, compatible snapshots, source configure/sync and a 101-entry catalog | Catalog coverage is broader than authenticated connectivity. |
| Run connector extraction | Actual dlt with SQLite accounts/events, Intercom-shaped REST contacts and Linear-shaped GraphQL issues on loopback | Live vendor connections and other resource/driver combinations remain unverified. |
| Read filesystem contents | Controlled CSV/JSONL/Parquet reader tests; actual dlt CSV/JSONL composition with synthetic listing/client inputs | Cloud and SFTP listing, authentication and connectivity remain unverified. |
| Bootstrap context | Schemas, profiles, candidate keys and joins, unresolved policy questions | Outputs are drafts. Business rules and differently named identity mappings require review. |
| Review metrics | SQL witness, exact-hash approval, versions and preserved history | Local reviewer attribution is an operator assertion, not authenticated governance. |
| Refresh and reuse | Export recipe changes from 51 unused seats to 8 with the same definition; a separate consumer reproduces both | Explicit refresh; no scheduler. Generic sources refresh per connection or imported table. |
| Recover from failed extraction | Partial-page failures retain the last successful published snapshot | Inspect latest-attempt and committed-snapshot status before treating data as current. |
| Ask with an agent | Built-in Claude CLI loop and read-only MCP stdio; local MCP protocol/tool checks | Individual client setup and a second model selecting the right definition still need validation. |
| Transform and change data | Local materialized tables and exact-preview single-row changes | CLI operations only; no source-system writeback or automatic transform refresh. |
| Export context | Portable bundles, separate-consumer execution and local native metric structure checks | No automatic upload or verified live registry create/read/update round trip. |
| Install the package | Fresh base and connector installs from public PyPI artifacts on macOS ARM64 / Python 3.12, with unchanged dependency pins and hashes; source and license integrity | Other platform, driver and vendor combinations remain unverified. Install from the repository; no separate package release is advertised. |
| Adoption and performance | Functional examples and historical exploratory experiments | No own-data retention evidence or qualifying comparative accuracy/cost/latency result. |

## Reproduce the functional evidence

The [connector guide](connectors.md#what-has-been-verified) provides the actual dlt verification commands. The packaged [native verification record](../contextbase/connectors/native-verification.json) identifies tested resources, versions, outcomes and scope.

The [CRM/product recipe](tool-exports.md) includes a runnable verifier, an independent expected-answer calculation and [functional receipt](../examples/tool-exports/functional-proof.json). It covers identity review, local changes, approved metrics, bundle reuse and changed-data checks. These are deterministic synthetic results, not model or customer outcomes.

The completed connector implementation check passed 186 relevant tests. That is a recorded implementation check, not a guarantee of current test coverage or live compatibility. The [contribution guide](../CONTRIBUTING.md) explains how to run relevant checks against your checkout.

## Next validation

1. Reproduce first use with new technical users and authorized exports from their own tools.
2. Validate chosen live connectors and specific agent clients before naming them as tested integrations.
3. Observe repeat use of approved definitions and a real second-consumer handoff.
4. Verify an authorized registry create/read/update round trip when a receiving workflow needs it.

Comparative evaluations remain deferred. They are not required to try the local preview, and no accuracy, cost or speed advantage is claimed. The detailed [evaluation infrastructure status](evaluation-status.md) preserves the research work and its remaining gates. [Roadmap](roadmap.md) · [Evidence policy](gtm.md#scorecard-and-evidence-policy).
