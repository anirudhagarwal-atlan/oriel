# Connect your existing tools

Oriel's source workflow is **configure → sync into DuckDB → bootstrap draft context → query**. A connection has its own name and schema, so you can combine billing, CRM, product and internal-tool data in one workspace. The source catalog contains 101 source types; that is definition coverage, not a claim that 101 live connections have been tested.

Start from a [local checkout](quickstart.md#install-from-your-checkout). The committed lockfile uses public PyPI artifacts and preserves reviewed dependency versions and hashes.

Python 3.10–3.14 is supported. Install the base package for local CSV/Parquet, or include the connector runtime for APIs, databases and filesystem sources:

```sh
uv sync --frozen --extra connectors
```

Omit `--extra connectors` for a file-only installation. The extra includes the pinned dlt runtime, SQLAlchemy and filesystem dependencies; some tools require additional drivers or SDKs. The commands below use the installed environment directly, without changing its dependencies. If you use `uv run` instead, retain `--extra connectors` for connector workflows.

## Inspect the catalog

With the repository's base dependencies installed:

```sh
.venv/bin/oriel source list --available
.venv/bin/oriel source describe linear
.venv/bin/oriel source describe salesforce
.venv/bin/oriel source describe mixpanel
.venv/bin/oriel source doctor sqlite
```

These commands inspect packaged declarations and installed package metadata without loading a connector, contacting a source, or reading credentials. `source describe` shows required and optional credential fields, suggested environment-variable names, resources, default selections, supported parameters, dependencies, verification evidence and limitations. Its inherited `cli_flag` values describe credential fields; Oriel accepts them through `--credential-env FIELD=ENV_VAR`.

`source doctor TOOL` reports missing dependencies, exact core-runtime version mismatches, Python compatibility and remaining adapter restrictions. Omit `TOOL` for a record covering all 101 entries. A `prerequisites_present_connection_unverified` result means packages are present, not that credentials or the connection work. Vendor package version compatibility still needs source-specific verification.

| Execution family | Source types | Implementation |
| --- | ---: | --- |
| REST | 53 | Packaged YAML definitions and the dlt REST translator |
| Verified-source templates | 25 | Bundled, revision-pinned dlt source packages, including Salesforce and Stripe |
| SQL | 15 | dlt SQL database source with the appropriate driver |
| Filesystem | 4 | dlt filesystem source for S3, GCS, Azure and SFTP |
| GraphQL | 2 | Linear and Monday source definitions |
| Local files | 2 | CSV and Parquet through Oriel's existing import path |
| **Total** | **101** | |

The catalog retains all 440 declared resources, including resources disabled by default. Sources that discover their resources at runtime may have no static resource list. An additional legacy Amplitude definition is retained as an unimplemented asset; it is outside the 101 available source types. See the [technical attribution and source revisions](upstream.md).

These connectors use **dlt**, not Singer taps. dlt handles extraction and loading; MCP remains a separate interface through which an agent can query Oriel.

## Complete a local source workflow

From the repository root, use a new `.connector-demo` workspace. This example reads a bundled synthetic CSV, requires no credentials, and makes no source or model calls:

```sh
.venv/bin/oriel --workspace .connector-demo source add csv \
  --name crm_seats --path examples/tool-exports/fixtures/crm-seats.csv
.venv/bin/oriel --workspace .connector-demo source list
.venv/bin/oriel --workspace .connector-demo source sync crm_seats
.venv/bin/oriel --workspace .connector-demo catalog
.venv/bin/oriel --workspace .connector-demo query \
  'SELECT organization_id, licensed_seats, contract_state FROM crm_seats.records ORDER BY organization_id'
```

`source add` saves a configuration; its `connected: false` result is intentional. `source list` without `--available` lists configured connections and their latest sync status. Local file sources require `--path`, accept one matching file, and publish it as `<connection_name>.records`. JSON and JSONL remain available through the separate `ingest` command.

By default, a successful sync also writes `<workspace>/context/draft.json` from the observed tables and column profiles. This draft contains context candidates, not approved business metrics. Review identity mappings and business rules before proposing and approving metrics. The [generic export walkthrough](tool-exports.md) demonstrates that review and reuse lifecycle.

## Configure a network source

After the connector runtime and the chosen source's dependencies have been explicitly approved and installed, a connection can use credentials already supplied to its process environment. For example:

```sh
.venv/bin/oriel --workspace ./my-data source add linear \
  --name work_tracker --credential-env api_key=LINEAR_API_KEY \
  --resource issues --resource projects
.venv/bin/oriel --workspace ./my-data source sync work_tracker --timeout 300
.venv/bin/oriel --workspace ./my-data catalog
```

**The network source's `sync` command contacts its configured service.** This example is a configuration pattern, not evidence of a verified live Linear connection.

Only the reference `LINEAR_API_KEY` is saved, never the credential value. Supply the value through your approved environment or secret-injection mechanism. Do not paste credentials into command arguments, `--param`, source JSON, examples or logs. If you omit `--credential-env`, `source add` records default references for required fields only. Explicitly supplied references replace that default set: include every required field and any optional field you use. Every referenced variable must be available when extraction runs. There is no automatic OAuth login or credential-store discovery.

The field names differ by source: Stripe uses `stripe_secret_key`, Salesforce declares `username`, `password` and `security_token`, and SQL sources use `credentials` for the connection string. Google Sheets and Analytics accept complete service-account JSON through `GOOGLE_SERVICE_ACCOUNT_JSON`; Analytics also requires `GA_PROPERTY_ID`. Credential file paths, application-default credentials and OAuth payloads are not supported by those adapters. Inspect `source describe` before configuring a connection. Saved configurations and extraction attempts live beneath `<workspace>/connectors/<name>/`; treat that workspace as data-bearing.

## Choose resources and parameters

Repeat `--resource NAME` to limit extraction. With no explicit selection, declared YAML resources marked selected are used; dynamically discovered sources use their source factory's defaults. Those defaults can include many tables, so choose explicit resources for a bounded first connection. The worker rejects an empty or mismatched explicit selection.

Parameters use repeatable `--param NAME=JSON_VALUE` arguments. `source describe` lists `parameter_names`; REST entries also list each resource's allowed and required query/body fields under `resource_parameters`. Values must be JSON. Credentials, authentication and endpoint overrides cannot be supplied as parameters. GraphQL queries and endpoints remain fixed by their packaged definitions.

For example, Mixpanel's export requires a date range. This saves configuration only:

```sh
.venv/bin/oriel --workspace ./my-data source add mixpanel \
  --name product --resource export \
  --param 'export={"from_date":"2026-01-01","to_date":"2026-01-07"}'
```

REST resource parameters may address selected resources and their extraction dependencies. Missing required values fail before the worker reads credentials. JSONL export decoding, endpoint authentication, parent bindings and supported pagination have controlled tests; authenticated Mixpanel extraction remains unverified. Resources disabled by default remain visible and can be selected explicitly. Linear's nested labels, project teams and project members are bounded projections, not exhaustively paginated collections. Top-level pagination does not make these nested fields complete. Declared write endpoints remain metadata; this workflow does not expose source writeback.

SQL sources accept validated `schema`, `chunk_size` and `reflection_level` parameters. Select tables explicitly for a bounded first extraction:

```sh
.venv/bin/oriel --workspace ./my-data source add sqlite \
  --name internal --credential-env credentials=INTERNAL_SQLITE_URL \
  --resource accounts --param 'chunk_size=1000'
```

## Read files from object storage or SFTP

Filesystem connectors require an explicit `tables` mapping. Each named table selects a relative file glob and a format; the adapter reads file contents into DuckDB. A file listing alone is not the imported dataset.

```sh
.venv/bin/oriel source describe s3
.venv/bin/oriel --workspace ./my-data source add s3 \
  --name warehouse --resource orders \
  --param 'tables={"orders":{"file_glob":"orders/*.parquet","format":"parquet"}}' \
  --param 'max_files=100' --param 'max_file_bytes=67108864'
```

Supported formats are CSV, JSONL and Parquet, with gzip for CSV/JSONL. Each row retains file URL, hash, row number, modification time and configured bucket URL. CSV fields remain text; use SQL casts for analysis. Missing files, zero-row selected tables, malformed contents, naming collisions or exceeded limits fail extraction.

S3 requires an explicit bucket URL and access-key pair; a session token is optional. GCS uses `GCS_SERVICE_ACCOUNT_JSON`, a complete JSON value supplied through the environment. Azure uses an explicit storage-account key. SFTP uses username/password and a required `sftp_host_key_sha256` parameter containing the independently verified OpenSSH SHA256 host-key fingerprint; it does not consult SSH agents, key files or `known_hosts`. Read `filesystem_contract` in `source describe` for the complete contract. These paths have controlled reader/client tests. The shared CSV/JSONL readers also passed actual dlt normalization, loading and refresh with synthetic S3 listings and clients, including provenance and collision rejection. Real storage listing, authentication and cloud/SFTP connectivity remain unverified.

## Change a connection and refresh

Repeat `source sync NAME` after the upstream data changes. The saved source configuration is reused. To change resource selections, parameters, credential references or a file path, repeat the complete `source add` command with `--replace-config`, using the same connection name and source type:

```sh
.venv/bin/oriel --workspace ./my-data source add linear \
  --name work_tracker --credential-env api_key=LINEAR_API_KEY \
  --resource issues --replace-config
.venv/bin/oriel --workspace ./my-data source list
.venv/bin/oriel --workspace ./my-data source sync work_tracker
```

Replacement keeps a private copy of the previous configuration and preserves published data until the next successful sync. It does not merge omitted settings with the old configuration. `source list` separates `last_sync` (latest attempt) from `last_snapshot` (last committed publication). `configuration_changed_since_sync` flags when the current configuration differs from the published snapshot's configuration, including after a failed attempt. Review and reconfigure after an adapter catalog change; a stale catalog hash refuses sync.

## Dependencies and refresh behavior

Catalog inspection and configuration work without dlt or PyYAML. Network extraction requires the [reviewed connector runtime](connector-runtime-proposal.md), including dlt 1.24.0 and PyYAML 6.0.3, on Python 3.10–3.14. The runtime is declared in the optional `connectors` extra and resolved in `uv.lock`. Bundled source code is not an installed dependency environment. Database dialects, filesystem drivers and application SDKs can require additional packages, and inherited dependency hints are not a resolved or approved lockfile. The [verified-source manifest](../contextbase/connectors/verified/manifest.json) records requirements and static import hints for review. Missing dependencies fail sync; no fetch, `pip install` or `dlt init` runs automatically.

Each sync performs a new extraction into a separate private staging database; it does not promise incremental checkpoints or continuous freshness. Oriel publishes completed tables into the connection's dedicated schema in one transaction. It refuses schema ownership conflicts and snapshots missing previously published tables, including when a new resource selection would omit an old table. Use a new connection name for that narrower snapshot. A failed extraction or rejected publication leaves the previous tables available, so inspect `source list` before treating them as current. Approved business definitions are preserved across refresh. There is no automatic retry or resume.

`--timeout` bounds a network extraction worker to 1–3600 seconds; it does not include local publication or context bootstrap. `--no-bootstrap` skips draft generation. If extraction and publication succeed but bootstrap fails, the sync result identifies that separate failure; inspect the catalog and run `context bootstrap` explicitly. Source snapshot hashes and row counts are recorded with publication.

## What has been verified

Each entry's `verification` record distinguishes packaged definitions, controlled adapter checks, native dlt integration and authenticated live verification. All 101 entries retain `live_validated: false`. Offline tests cover catalog and license integrity, explicit credential mappings, REST/GraphQL translation and pagination, bounded filesystem readers, worker isolation, local publication and failed-refresh retention. They do not establish vendor compatibility or production permissions.

The following checks passed using the actual pinned dlt runtime. Reproduce them after installation, using new output directories:

```sh
.venv/bin/python examples/connectors/verify_sqlite.py --output benchmark-runs/native-sqlite-verification
.venv/bin/python examples/connectors/verify_apis.py --output benchmark-runs/native-api-verification
```

The SQLite check loaded selected accounts and events tables, bootstrapped context and changed active seats from **10 to 4** after refresh. The API check used Intercom-shaped REST contacts and Linear-shaped GraphQL issues on one loopback port: the cross-source metric changed from **10 to 15**, and page-two failures preserved both previous snapshots. Approved context remained unchanged in both checks. The [packaged verification record](../contextbase/connectors/native-verification.json) names the tested sources, resources, runtime versions and limits. These results do not validate other database drivers, untested resources or live vendor compatibility. No live vendor or model call is part of either check.

SQL queries, profiling and context bootstrap stay local. To ask a configured model about imported data, use `ask --allow-model-data`; that explicitly permits relevant context and tool results to reach the model. Review imported data and approved definitions before sharing them with an agent or exporting a reusable bundle.
