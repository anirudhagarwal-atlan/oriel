# Talk to data across your existing tool exports

Bring the CRM and product analytics data you already have into a shared local workspace. Ask a cross-tool question, inspect the mapping and SQL, then retain the reviewed definition for reuse. This walkthrough uses fabricated exports with different source schemas from the benchmark recipe. It requires no simulator, source account or model.

**Example question:** Which active external customers activated fewer than half their licensed seats, and how many unused seats do they have?

The answer is **51 unused seats across five organizations** in the initial fixture. After importing additional activation events, the same definition returns **8 unused seats at one organization**. [Independent Python code](../examples/tool-exports/oracle.py) computes the expected answers without DuckDB or the product SQL. [Recorded functional evidence](../examples/tool-exports/functional-proof.json) includes the actual CLI outcomes and input hashes.

## Run the complete check

From the repository root with the existing locked environment:

```sh
uv run --frozen python examples/tool-exports/verify.py \
  --output benchmark-runs/tool-exports-verification
```

Use a new or empty output directory. This makes local workspaces, a context bundle and a receipt; it does not modify the example exports. The verifier runs the following steps, including explicitly authored synthetic review decisions. The manual flow below separates each review from its approval.

## Import and understand identity

The CRM calls its key `organization_id`; product events identify `workspace_ref`. A reviewed directory bridges `workspace_ref` to CRM `customer_key`. Multiple product workspaces can belong to one organization. Never join customer and product IDs because they happen to look alike.

```sh
cb() { uv run --frozen oriel --workspace benchmark-runs/tool-exports-manual/workspace "$@"; }
cb ingest examples/tool-exports/fixtures/crm-seats.csv --schema sales --table seats
cb ingest examples/tool-exports/fixtures/product-events-initial.jsonl --schema telemetry --table events
cb ingest examples/tool-exports/fixtures/workspace-map-unresolved.csv --schema directory --table workspace_map
cb context bootstrap
cb query "$(cat examples/tool-exports/sql/mapping-issues.sql)"
```

Bootstrap produces a draft from observed schemas and keys; it supplies no business metric or different-name identity mapping. The explicit mapping query reports an ambiguous `ws_alpha`, a missing enabled mapping for `ws_theta`, and therefore an unmapped active customer `org_008`. The metric refuses to answer while these remain unresolved.

The fixture's declared review decisions are to disable the duplicate `map_10` link and enable `map_08`. For each plan below, inspect its exact `before`, `after`, `scope`, `plan_id` and `approval_sha256`, then pass that plan's ID and hash to `changes apply`. The uppercase placeholders represent values from the reviewed output.

```sh
cb changes plan --table directory.workspace_map --key mapping_id --value map_10 --set '{"enabled":false}'
cb changes apply PLAN_ID_FROM_FIRST_PREVIEW --approve SHA256_FROM_FIRST_PREVIEW
cb changes plan --table directory.workspace_map --key mapping_id --value map_08 --set '{"enabled":true}'
cb changes apply PLAN_ID_FROM_SECOND_PREVIEW --approve SHA256_FROM_SECOND_PREVIEW
cb query "$(cat examples/tool-exports/sql/mapping-issues.sql)"
```

The last result must have no rows. These changes affect the local imported mapping only. The CSV, CRM and product tool remain unchanged. The verifier also proves that incorrect approval hashes and repeated application are rejected.

## Inspect the answer and retain a local result

```sh
cb query "$(cat examples/tool-exports/sql/activation-accounts.sql)"
cb transform activation_initial --sql "$(cat examples/tool-exports/sql/activation-accounts.sql)"
```

The account rows expose the population and arithmetic:

| Organization | Licensed seats | Distinct activated members | Unused seats |
| --- | ---: | ---: | ---: |
| `org_001` | 20 | 4 | 16 |
| `org_003` | 15 | 0 | 15 |
| `org_006` | 12 | 5 | 7 |
| `org_007` | 8 | 0 | 8 |
| `org_008` | 6 | 1 | 5 |

The authored policy counts distinct non-null members who published a report in `[2026-09-24, 2026-10-08)` UTC, across all mapped workspaces. It excludes inactive contracts, internal organizations and customers at or above 50% activation. Repeated events and members appearing in two workspaces must not inflate adoption. The synthetic export explicitly covers the complete period; only under that assumption does no qualifying activity mean zero. Missing or partial exports require a completeness review.

`models.activation_initial` is a materialized local table. It remains an inspection artifact of the initial data after future source imports; transformations do not automatically refresh.

## Propose and review the business definition

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

Inspect the candidate SQL, rules, grain, owner, execution evidence and approval hash. Successful execution is evidence of computability; the policy still needs review. `--synthetic-example` labels this operator-authored example and keeps registry exports as candidates. It does not certify input data, infer policy or authorize model access.

```sh
cb metric approve unused_seats_at_low_activation \
  --approve SHA256_FROM_REVIEWED_CANDIDATE --reviewer synthetic_example_reviewer
cb metric run unused_seats_at_low_activation
cb bundle export --output benchmark-runs/tool-exports-manual/bundle --workspace-id synthetic-tool-exports
```

The run returns 51 and its context version. The offline bundle contains definitions, observed schemas, a reusable skill and file hashes; it excludes source rows. The verifier checks that running before approval and approving the wrong hash are rejected. Approval is a local review assertion, not authenticated organizational certification.

## Reuse in a separate process

The next command starts a new Python process with a fresh workspace. It imports the exports, verifies bundle file hashes, reads the exported context and executes the retained definition. It receives no repeated question, SQL template or business policy. It also executes the registry envelope's formula against its exported derived source and requires both forms to agree.

```sh
uv run --frozen python examples/tool-exports/reuse.py \
  --bundle benchmark-runs/tool-exports-manual/bundle \
  --workspace benchmark-runs/tool-exports-manual/second-consumer \
  --crm examples/tool-exports/fixtures/crm-seats.csv \
  --mapping examples/tool-exports/fixtures/workspace-map-reviewed.csv \
  --events examples/tool-exports/fixtures/product-events-initial.jsonl
```

The answer remains 51 with the same context version. This proves separate-process context reuse against separately imported data. It does not install or certify a definition in a real Registry, nor measure an LLM's ability to select the right definition.

## Refresh and rerun

```sh
cb ingest examples/tool-exports/fixtures/product-events-initial.jsonl --schema telemetry --table events --replace
cb metric run unused_seats_at_low_activation
cb ingest examples/tool-exports/fixtures/product-events-refreshed.jsonl --schema telemetry --table events --replace
cb metric run unused_seats_at_low_activation
cb query "$(cat examples/tool-exports/sql/activation-accounts.sql)"
```

Replacing with the identical export preserves the answer. The changed export produces 8 at `org_007` while the approved context file, version and metric SQL remain unchanged. The complete verifier runs another separate consumer against these refreshed exports and requires the same result. Generic refresh here means explicit replacement of one selected local table; it is not automatic source polling or an atomic refresh across multiple sources. Review observation times and completeness before replacing a real multi-source snapshot.

This workflow demonstrates tool-independent ingestion, identity handling, local transformation, reviewed context and reuse. It makes no claim about natural-language accuracy, cost, speed or adoption. Those require the separate comparative benchmark and approved user trials in the [build charter](build-charter.md).
