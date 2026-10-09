# Customer 360 foundation: identity, snapshots and reviewed mappings

Status: local developer preview. These operator commands implement the data and review foundation of the [Customer 360 build goal](customer-360-goal.md). The [customer CLI workflow](customer-360-cli.md) adds reviewed business definitions, scoped calculations, exact source quotations and the configured-model integration. The [local application](customer-360-app.md) exposes the same workflow; live-vendor validation remains pending. A reviewed field mapping does not certify a business metric.

## What works

The existing `oriel source add` and `oriel source sync` commands publish source tables to DuckDB. New publications include logical schema and content hashes, committed atomically with their receipts. A Customer 360 snapshot pins selected publications and their age/skew limits. It refuses failed, reconfigured, stale, changed or unverifiable publications. Older publications without content evidence need a successful refresh first.

Workspace customer IDs are independent of vendor IDs. Explicit source links require review; names and shared domains never silently merge customers. Reviewed profiles map observed columns to customer evidence categories. Materialization retains unresolved records, gives evidence stable record and content identities, and preserves earlier materializations. Reading current records verifies the snapshot, identity, profile and projection.

These checks validate the local synchronized data. They cannot prove upstream completeness or whether a vendor granted every required permission. [Connector readiness](connectors.md) and authorized live validation remain separate concerns.

## Operator workflow

Run commands from the checkout root after the documented installation. The examples use `uv run --no-sync` and the same private `.oriel-customer` workspace as the [application quickstart](quickstart.md). If you launched the app with another workspace, replace `.oriel-customer` in every command with that path. Use the IDs returned from that same workspace. The examples below use synthetic file sources; replace their file paths, connection names and mapped columns with your actual inputs.

1. Configure and sync sources using the existing connector workflow:

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer source add csv --name billing --path /absolute/path/to/billing.csv
   uv run --no-sync oriel --workspace .oriel-customer source add csv --name support --path /absolute/path/to/support.csv
   uv run --no-sync oriel --workspace .oriel-customer source sync billing --no-bootstrap
   uv run --no-sync oriel --workspace .oriel-customer source sync support --no-bootstrap
   ```

   `--no-bootstrap` skips the existing generic schema bootstrap. Customer business-context bootstrap happens after reviewed identity and field mapping in the current CLI. These file connectors exercise local publication; they do not establish live billing or support integration.

2. Create the workspace customer and inspect its returned ID:

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer customer create --label example-customer
   uv run --no-sync oriel --workspace .oriel-customer customer list
   ```

3. Write a local `links.json` array using that customer ID. For example:

   ```json
   [
     {"connection":"billing","entity":"party","source_id":"party-a","customer_id":"WORKSPACE_CUSTOMER_UUID","basis":"Reviewed explicit customer reference"},
     {"connection":"support","entity":"organization","source_id":"org-a","customer_id":"WORKSPACE_CUSTOMER_UUID","basis":"Reviewed explicit customer reference"}
   ]
   ```

   Propose, inspect the links and conflicts, then approve the exact returned proposal and hash:

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer customer links propose --file links.json
   uv run --no-sync oriel --workspace .oriel-customer customer links approve PROPOSAL_ID --approve APPROVAL_SHA256 --reviewer example-reviewer
   ```

   Conflicting proposals cannot be approved. Adding an explicitly reviewed alias preserves customer identity when a source replaces an identifier. Arbitrary reassignment of an existing link is not supported by this foundation.

4. Capture the committed source combination:

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer customer snapshot --source billing --source support
   ```

   Retain its `snapshot_id`. Defaults allow an extraction age of 86,400 seconds and finish-time skew of 3,600 seconds; use `--max-age-seconds` and `--max-skew-seconds` to set an explicit policy. Age is conservatively measured from extraction start. A snapshot proves committed local scope, not upstream completeness.

5. Write `profile.json` with exact columns observed in the source tables:

   ```json
   {
     "spec_version":"oriel.customer360.profile/v1",
     "name":"customer_review",
     "description":"Reviewed billing and support fields",
     "bindings":[
       {
         "connection":"billing","entity":"invoice","table":"billing.records","kind":"billing",
         "record_id":"invoice_id",
         "customer_ref":{"connection":"billing","entity":"party","column":"party_ref"},
         "fields":{"amount":"amount","currency":"currency"},
         "measures":{
           "amount":{
             "unit":"unknown",
             "currency_field":"currency",
             "aggregation":"unknown",
             "meaning":"Currency is mapped, but scale and permitted aggregation have not been reviewed."
           }
         }
       },
       {
         "connection":"support","entity":"case","table":"support.records","kind":"support",
         "record_id":"case_id",
         "customer_ref":{"connection":"support","entity":"organization","column":"organization"},
         "fields":{"text":"body","status":"status"}
       }
     ]
   }
   ```

   The schema must match the actual source output. Profiles contain column mappings, never executable expressions. Each physical table has one binding, and each binding must have unique record IDs. The example deliberately leaves the amount policy unknown; a numeric type or a field called `amount` does not establish its unit or permission to sum.

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer customer profile propose --file profile.json --snapshot SNAPSHOT_ID
   uv run --no-sync oriel --workspace .oriel-customer customer profile approve PROPOSAL_ID --approve APPROVAL_SHA256 --reviewer example-reviewer
   uv run --no-sync oriel --workspace .oriel-customer customer profile show customer_review
   ```

   Inspect mapped types, customer coverage and unresolved/conflicting links before approving. Source or identity changes between proposal and review invalidate the proposal. Review is an assertion by the local operator, not multi-user authentication.

6. Materialize and inspect customer evidence:

   ```sh
   uv run --no-sync oriel --workspace .oriel-customer customer materialize --profile customer_review --snapshot SNAPSHOT_ID
   uv run --no-sync oriel --workspace .oriel-customer customer records --profile customer_review --customer WORKSPACE_CUSTOMER_UUID --snapshot SNAPSHOT_ID
   ```

   Records remain separate at their source grain. An invoice amount is not automatically ARR, and multiple support cases do not multiply invoice value. Unresolved records contribute to coverage gaps but are not attributed to a customer.

7. After a source refresh, capture a new snapshot and materialize the same reviewed profile against it. Unchanged mapped types reuse approval. Mapped schema drift requires a revised profile review. Current reads refuse the old snapshot. Once business context is approved, you can [save a review before refreshing and compare it with the new snapshot](customer-360-cli.md#save-a-review-and-inspect-what-changed). The saved review remains explicitly historical; comparisons with changed identity, mapping or definition approvals withhold arithmetic deltas.

## Review measure meaning

Each binding can include an optional `measures` object keyed by a logical value role already present in `fields`. A policy contains exactly `unit`, `currency_field`, `aggregation` and `meaning`. It is part of the mapping proposal and its approval hash; it does not create or approve a metric.

| Field | Supported values and meaning |
| --- | --- |
| `unit` | `number` for nonmonetary quantities, `currency` for major monetary units, `minor_currency` for minor monetary units, or `unknown` when unconfirmed. |
| `currency_field` | A different mapped logical role for `currency` or `minor_currency`; `null` for `number`. An `unknown` unit may retain a known currency role or use `null`. |
| `aggregation` | `sum` when summing records at this binding's grain is permitted, `none` when it is not, or `unknown` when not reviewed. |
| `meaning` | Nonempty, single-line business meaning and review basis, at most 2,000 characters. Keep unresolved questions explicit. |

For example, after independently confirming that a source amount is in major monetary units and can be summed at the mapped record grain, the binding could contain:

```json
{
  "measures": {
    "amount": {
      "unit": "currency",
      "currency_field": "currency",
      "aggregation": "sum",
      "meaning": "Major-unit invoice balances at one record per invoice; reviewed as additive within each currency."
    }
  }
}
```

This is an example policy to review, not a default for invoice data. Missing policies remain unknown. Selecting a unit does not cast or rescale values. Exact sums still need supported integer or DECIMAL source columns, and the business definition separately specifies the population and filters. New model-proposed sums require a compatible reviewed policy; the agent cannot supply one by guessing from a column name, numeric type or sample value. See [business-context review](customer-360-cli.md#bootstrap-and-review-business-context) for that boundary.

If several bindings share a capability kind, a sum over that kind needs compatible policies for the value role across all of them. A declared unknown, conflicting or non-summable policy cannot be bypassed by an operator-authored sum. Earlier operator-authored definitions without any measure policies remain readable; adding or changing a policy creates a new mapping approval and requires dependent context review.

## Verification and limits

The executable local integration examples are [profile tests](../tests/test_customer360_profiles.py) and [CLI tests](../tests/test_customer360_cli.py). They create synthetic CSV files, run the actual file connector publication, review links and mappings, read customer records and exercise refresh/refusal behavior. No vendor API or model provider runs in these tests.

```sh
.venv/bin/python -m unittest tests.test_customer360_cli tests.test_customer360_profiles tests.test_customer360_snapshots tests.customer360.test_customer_identity
.venv/bin/python scripts/verify_customer360.py
```

The acceptance runner still reports an incomplete product until all [ten gates](customer-360-goal.md#definition-of-done) pass. Foundation unit tests do not substitute for the complete source-runtime, context-agent, answer, UI, model or live-connector gates.

Initial projection limits are 10,000 records per binding, 64 mapped fields, 64 KB per projected record and 20 MB across projected fields. Oversized input is refused before publication. Snapshot hashing is deliberately conservative: unsupported types and nonfinite numeric values cannot receive incomplete content evidence. Nested transcript normalization, large-scale projections, source-link reassignment, selective history revocation and multi-user permissions require additional implementation.
