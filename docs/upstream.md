# Third-party source notices and snapshot compatibility

Oriel includes adapted connector code and source definitions, with their original attribution and licenses retained. These technical notices identify the reused implementation and its boundaries; they do not imply upstream sponsorship or live connector validation. For the user-facing workflow, see [connect your existing tools](connectors.md).

## Connector registry and source definitions

The registry, YAML translator, source configuration loader, source factory dispatch, GraphQL implementation and packaged configurations originate in [Dinobase](https://github.com/kappa90/dinobase), version 0.2.9, commit [`2dd06369fc333cd51663c27b7d47a21a7946757b`](https://github.com/kappa90/dinobase/tree/2dd06369fc333cd51663c27b7d47a21a7946757b).

The copied material is under [`contextbase/connectors/upstream`](../contextbase/connectors/upstream/). Its [complete original LICENSE](../contextbase/connectors/upstream/LICENSE), including its introductory exceptions and copyright notice, is retained verbatim. The license identifies the MIT Expat terms. [SOURCE.json](../contextbase/connectors/upstream/SOURCE.json) records the revision, original and adapted file hashes, and local adaptations. All 56 packaged YAML definitions are retained, including the legacy Amplitude asset that is not executable through this registry.

The adaptation changes Python import paths, limits configuration discovery to packaged definitions, and replaces automatic verified-source fetching with an explicit resolver for the bundled pinned source tree. The resolver checks the recorded manifest and selected source bytes; it neither downloads code nor installs dependencies. Ambient project and home-directory connector overrides are not loaded. Upstream metadata enrichment is explicitly unavailable in this adaptation; Oriel bootstraps context from the tables it actually imports.

Local adapters repair credential/factory mappings, wrap resource factories, validate SQL and resource parameters, and materialize selected filesystem contents with provenance. The REST translator handles declared parent dependencies, endpoint authentication, JSONL and supported pagination; GraphQL adds bounded Relay and Monday continuation. Source templates remain unchanged. These adaptations have controlled tests. Actual dlt extraction is additionally verified for SQLite accounts/events and selected loopback Intercom contacts and Linear issues. Native and live evidence remain separate in each catalog entry; no authenticated vendor compatibility is claimed.

The [generated catalog](../contextbase/connectors/catalog.json) contains all 101 registry source types, credential specifications, every declared resource and known translation limitations. It preserves definitions without representing unsupported features as implemented. Resource-level refusal is enforced before sync. Catalog coverage is distinct from dependency availability and live verification.

## Official dlt source templates

The 25 referenced verified-source packages are bundled separately from [dlt-hub/verified-sources](https://github.com/dlt-hub/verified-sources), pinned to commit [`75b3ec17eab99d0079d9f61b7f47fc8b899a5738`](https://github.com/dlt-hub/verified-sources/tree/75b3ec17eab99d0079d9f61b7f47fc8b899a5738). Source directories and their included helper files are copied byte for byte; the package boundary, manifest and notice are local additions.

The bundle retains its full [Apache-2.0 LICENSE.txt](../contextbase/connectors/verified/LICENSE.txt), [NOTICE.md](../contextbase/connectors/verified/NOTICE.md) and [file and dependency manifest](../contextbase/connectors/verified/manifest.json). Upstream's project metadata labels the package MIT while its actual license file states Apache-2.0; the bundle preserves the actual license and records this inconsistency rather than substituting the metadata label. Per-file hashes and Git blob IDs establish correspondence with the selected repository objects, not publisher-signature authentication.

These are source templates, not installed drivers or a dependency lock. Requirements and static import hints are retained for review. No automatic installation occurs. The approved shared runtime is an optional, pinned package extra; additional vendor drivers and SDKs remain source-specific requirements. The implementation uses dlt; it does not use Singer taps. Offline integrity and adapter tests do not establish authenticated live extraction.

## Import a compatible existing snapshot

The separate `import-dinobase` compatibility command remains available for an existing local DuckDB snapshot. Finish the source writer's sync before opening its database, and select tables explicitly:

```sh
uv run oriel --workspace ./my-data import-dinobase /path/to/dinobase.duckdb \
  --table crm.accounts --table billing.subscriptions \
  --parquet-root /path/to/dinobase/data
uv run oriel --workspace ./my-data catalog
uv run oriel --workspace ./my-data context bootstrap
```

The source database is opened read-only and selected tables are materialized into the Oriel workspace. The optional `--parquet-root` permits source views to read local Parquet files within that selected directory. Cloud-backed views need to be exported to local Parquet first. This bridge does not load upstream configuration or credentials and does not run a source sync. A compatible snapshot is required; future upstream storage layouts are not implicitly supported.

Review imported tables, row counts and provenance before use. Source permissions do not become row-level authorization in a copied snapshot. `ask` on imported data requires `--allow-model-data` because it sends relevant context and tool results to the configured model. Deterministic queries and context bootstrap remain local.

Oriel's metric review, local change approval, reusable context bundles and MCP interface are separate from these connector notices. See [architecture](architecture.md) and [context and registry](context-and-registry.md) for those contracts.
