# Your first cross-tool answer

Bring data from your existing tools into DuckDB, inspect an answer, and retain the reviewed definition for the next question. This walkthrough uses synthetic CRM and product exports and needs no API key or model.

The question is: **which active external customers activated fewer than half their licensed seats, and how many unused seats do they have?** The initial answer is **51 unused seats across five organizations**. New events change it to **8**, with the same approved metric.

## Install from your checkout

The committed lockfile uses public PyPI artifacts. The base and connector installation paths have been checked in a fresh environment on macOS ARM64 with Python 3.12; other platform combinations need their own verification.

Use Python 3.10–3.14 and an existing installation of [uv](https://docs.astral.sh/uv/). Clone the repository and install its locked dependencies:

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen
```

These commands use a POSIX shell, including WSL on Windows, and that checkout's installed executable. Keep the same terminal and repository working directory throughout. Create a fresh workspace without changing the example files:

```sh
ORIEL_QUICKSTART="$(mktemp -d)"
cb() { .venv/bin/oriel --workspace "$ORIEL_QUICKSTART/workspace" "$@"; }
```

The workspace is temporary. Choose a persistent directory instead of `mktemp -d` when retaining your own data.

## Import, bootstrap and query

```sh
cb ingest examples/tool-exports/fixtures/crm-seats.csv --schema sales --table seats
cb ingest examples/tool-exports/fixtures/product-events-initial.jsonl --schema telemetry --table events
cb ingest examples/tool-exports/fixtures/workspace-map-reviewed.csv --schema directory --table workspace_map
cb context bootstrap
cb query "$(cat examples/tool-exports/sql/unused-seats.sql)"
cb query "$(cat examples/tool-exports/sql/activation-accounts.sql)"
```

The first query returns this row inside the CLI's JSON result:

```json
{"unused_seats_at_low_activation": 51}
```

The second query shows the five organizations and their arithmetic:

| Organization | Licensed seats | Activated members | Unused seats |
| --- | ---: | ---: | ---: |
| `org_001` | 20 | 4 | 16 |
| `org_003` | 15 | 0 | 15 |
| `org_006` | 12 | 5 | 7 |
| `org_007` | 8 | 0 | 8 |
| `org_008` | 6 | 1 | 5 |

CRM organization IDs and product workspace IDs differ. This example supplies an explicitly reviewed synthetic mapping between them. Bootstrap writes schema, profile and key candidates to `<workspace>/context/draft.json`; it does not infer that mapping or the meaning of activation. The SQL counts distinct members who published a report in the declared period, across mapped workspaces, and refuses unresolved identities.

## Keep the definition after review

Propose the example's SQL with its business rules:

```sh
cb metric propose --name unused_seats_at_low_activation \
  --sql-file examples/tool-exports/sql/unused-seats.sql \
  --description 'Unused seats at active external organizations below 50% report activation in the synthetic period.' \
  --grain 'One CRM organization, aggregated across its product workspaces.' \
  --unit seats --owner synthetic_activation_owner --synthetic-example \
  --rule 'Require unique valid enabled workspace-to-organization mappings; unresolved identities block the answer.' \
  --rule 'Only active external customer contracts with positive licensed seats.' \
  --rule 'Distinct non-null members publishing a report in [2026-09-24,2026-10-08) UTC; deduplicate across workspaces.' \
  --rule 'The synthetic export is complete for the period; no qualifying activity is zero, incomplete exports need review.' \
  --rule 'Below 50% activation strictly; sum licensed seats minus activators once per qualifying organization.'
```

Inspect the returned SQL, rules, owner, grain and execution evidence. Successful SQL execution alone does not validate the business definition. After reviewing this synthetic candidate, replace `SHA256_FROM_REVIEWED_CANDIDATE` below with its exact `approval_sha256`:

```sh
cb metric approve unused_seats_at_low_activation \
  --approve SHA256_FROM_REVIEWED_CANDIDATE --reviewer synthetic_example_reviewer
cb metric run unused_seats_at_low_activation
cb bundle export --output "$ORIEL_QUICKSTART/bundle" --workspace-id synthetic-tool-exports
```

The approved metric returns 51 with a context version. The local bundle contains definitions, schema context, a reusable skill and registry handoff files. It excludes source rows but includes SQL and witness evidence, which can contain sensitive aggregates for real data. Review it before sharing. Export does not publish or write to a Registry.

`--synthetic-example` labels the example definition; it neither verifies that inputs are synthetic nor authorizes model access. Omit it for real business definitions. Local approval records a review assertion, not an authenticated organizational sign-off.

## Refresh without redefining the metric

```sh
cb ingest examples/tool-exports/fixtures/product-events-refreshed.jsonl --schema telemetry --table events --replace
cb metric run unused_seats_at_low_activation
cb query "$(cat examples/tool-exports/sql/activation-accounts.sql)"
```

The metric now returns **8**, and the account query returns only `org_007`. The approved metric and context version are unchanged. This is an explicit local table replacement; it does not modify the product tool or schedule future refreshes.

The [complete export walkthrough](tool-exports.md) starts with ambiguous and missing mappings, fixes them through reviewed local changes, materializes a transformation, and reuses the exported definition in a separate process. Its independent Python oracle checks the same 51 → 8 result.

## Connect your own tool

For a repeatable connection, save a source configuration and sync it. This separate example uses the same CRM fixture; replace `--path` with your own CSV export when ready:

```sh
ORIEL_SOURCE_DEMO="$(mktemp -d)"
source_cb() { .venv/bin/oriel --workspace "$ORIEL_SOURCE_DEMO/workspace" "$@"; }
source_cb source add csv --name crm --path examples/tool-exports/fixtures/crm-seats.csv
source_cb source sync crm
source_cb catalog
source_cb query 'SELECT COUNT(*) AS organizations FROM crm.records'
source_cb source sync crm
source_cb source list
```

The bundled CSV has **8 organizations**. `source add` saves configuration without extracting data. `source sync` publishes `<connection_name>.records` and bootstraps a context draft by default. Repeat sync after the file changes to refresh it; the example's second sync rereads unchanged data. Inspect the catalog before writing SQL for your own fields. These tables are separate from the three-table metric workspace above.

For API, database or filesystem sources, install the optional connector runtime from the same lockfile, then inspect your chosen source:

```sh
uv sync --frozen --extra connectors
.venv/bin/oriel source list --available
.venv/bin/oriel source describe mixpanel
.venv/bin/oriel source doctor mixpanel
```

Use the installed `.venv/bin/oriel` after enabling the extra. The catalog covers 101 source types; source-specific drivers, credentials and resource parameters may still be needed. Catalog inspection and doctor checks do not contact a service. Catalog coverage is not live connectivity certification.

Follow [connector setup](connectors.md) to configure explicit environment-variable references and resource selections, sync into a dedicated schema, and review refresh status. Keep credential values out of command arguments. A network source's sync contacts that service; a failed extraction retains the previous published snapshot. Review identity mappings and business rules before creating metrics over your own data.

Continue with [using an agent](agents.md) when you want natural-language questions or read-only MCP access to the workspace.
