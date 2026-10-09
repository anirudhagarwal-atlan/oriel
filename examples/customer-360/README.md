# Synthetic Customer360 source fixtures

These are **authored synthetic examples**, not captured vendor responses or connector verification. They contain no credentials, customer data, model outputs or execution receipts. All email addresses and URLs use `example.invalid`. Source IDs are invented. The two profiles are separate workspaces; a `customer_id` is scoped to its profile.

The intended implementation path is an existing connector → DuckDB → context agents → cited answers. These files supply repeatable inputs and an independent expected result for that path. Reading the JSON directly does not prove the connector worked. Runtime harnesses must still exercise real connector code, its wire format, pagination and failure behavior. Live-vendor and actual-model verification remain separate.

## Files and reuse

`manifest.json` lists both profiles, capabilities, phase files and mapping files. Each resource file is a plain JSON array of source-shaped records; it is not an HTTP response envelope. `initial` and `refreshed` are complete alternative snapshots, not incremental deltas. Preserve source IDs as strings, especially Gong call IDs larger than JavaScript's safe integer range.

`mapping-decisions.json` is separate from raw source rows. Its `approved_example` status means an authored example decision, not a real human approval. It declares stable customer identities, explicit associations and historical source-ID aliases. Names, websites, email addresses and free text are never identity keys. One deliberately contradictory pair of mapping decisions must be rejected.

`expected.json` is computed by `oracle.py`, which uses Python records, sets and `Decimal`. It imports neither product code nor the fixture generator and executes no SQL. It contains expected identities, unresolved references, currency-separated amounts, counts, customer sets and exact text citations. Citation JSON pointers resolve to the corresponding raw JSON file. The latest text field is **the latest attributed statement**, not an inferred health score or a promise that every older claim is superseded.

From the repository root, reproduce and verify the oracle without optional dependencies:

```sh
.venv/bin/python examples/customer-360/oracle.py --check
```

Expected summary: `status: passed`, `scope: synthetic_fixture_oracle_only`, two profiles, two phases each, zero vendor/model calls and no native connector execution. To inspect the complete independent result:

```sh
.venv/bin/python examples/customer-360/oracle.py
```

To reproduce the raw fixtures into a **new** directory:

```sh
.venv/bin/python examples/customer-360/generate.py /tmp/oriel-customer360-fixtures
.venv/bin/python examples/customer-360/oracle.py --root /tmp/oriel-customer360-fixtures
```

Choose another directory if that path exists. Generation refuses an existing directory. It writes only `manifest.json` and the raw/profile mapping files; it does not create expected answers or execution evidence. The oracle's `--check` option requires the saved `expected.json`, so use it against the committed example directory.

## Profile A: Salesforce + Gong

| Fixture resource | Important source fields |
| --- | --- |
| `salesforce.account` | `Id`, `Name`, `Website`, `OwnerId` |
| `salesforce.opportunity` | `Id`, `AccountId`, `Amount`, `CurrencyIsoCode`, `IsClosed`, `StageName` |
| `salesforce.contact` | `Id`, `AccountId`, `Email` |
| `salesforce.sf_user` | `Id`, `Name`, `Email` |
| `gong.extensive_calls` | `metaData.id/started/url`, `context[].system`, `objects[].objectType/objectId/timing`, `parties` |
| `gong.call_transcripts` | `callId`, `transcript[].speakerId`, `sentences[].start/end/text` |

The Gong file labels are fixture names. An Airbyte bridge must map its `extensiveCalls`/`callTranscripts` streams to them; these files do not add a Gong source to Oriel's catalog.

Four customers have open opportunities. `customer_a` and `customer_b` have the same account name and website. Account A has multiple opportunities, contacts and call participants: joining these must not multiply pipeline amounts or call counts. Calls link through explicit Account or Opportunity references; call 5 uses an explicit mapping decision. Call 3 lacks a mapping despite a shared email domain. Call 4 has conflicting Account/Opportunity references and conflicting mapping decisions. Both unresolved calls retain their raw transcripts but contribute no attributed customer claim.

| Expected result | Initial | Refreshed |
| --- | ---: | ---: |
| Open pipeline, USD major units | 26,500.00 | 23,000.00 |
| Open pipeline, EUR major units | 7,000.00 | 7,000.00 |
| Customer A open pipeline, USD | 12,500.00 | 9,000.00 |
| Customer A open pipeline, EUR | 3,000.00 | 3,000.00 |
| Attributed calls | 3 | 4 |
| Unattributed calls | 2 | 2 |

The metric sums `Amount` only where `IsClosed == false`. No FX conversion or cross-currency total is defined. Customer sets are `{customer_a, customer_b, customer_c, customer_d}` for open pipeline, `{customer_a, customer_b, customer_c}` for attributed calls and `{customer_d}` for no attributed call.

On refresh, Account A's ID changes from `001SYN000000000001` to `001SYN000000000009`. Current opportunities/contacts use the new ID; the old call reference remains valid through the explicit historical alias. Its first opportunity changes from USD 10,000 to 9,000 and the USD 2,500 opportunity closes. A new attributed call says: “SSO rollout is unblocked; procurement approval is now pending.” The prior call's statement about the blocked rollout remains historical evidence. Customer D still has no call evidence; the answer must not invent it.

## Profile B: Stripe + Intercom, without CRM or transcripts

| Fixture resource | Important source fields |
| --- | --- |
| `stripe.customers` | `id`, `object`, `name`, `email` |
| `stripe.invoices` | `id`, `customer`, `currency`, `status`, `amount_remaining` |
| `intercom.companies` | `id`, `company_id`, `custom_attributes.billing_customer_id` |
| `intercom.contacts` | `id`, `external_id`, `companies.data[].id` |
| `intercom.conversations` | `id`, `state`, `contacts.contacts[].id`, `source.body`, `conversation_parts` |

The billing linkage is an explicitly configured synthetic Intercom custom attribute, not a guaranteed default vendor field. Conversation → contact → company → billing customer is a different identity path from Profile A. A separate reviewed contact mapping associates customer C. One conversation has two contacts for customer A and counts once; another contact spans A and B and must remain ambiguous. An unlinked company with the shared name/domain must remain unresolved.

| Expected result | Initial | Refreshed |
| --- | ---: | ---: |
| Outstanding open invoices, USD minor units | 205,000 | 90,000 |
| Outstanding open invoices, EUR minor units | 75,000 | 75,000 |
| Customer A outstanding, USD minor units | 125,000 | 10,000 |
| Customer A outstanding, EUR minor units | 30,000 | 30,000 |
| Attributed open support conversations | 3 | 2 |
| Unattributed conversations | 2 | 2 |

The billing metric sums `amount_remaining` only for `status == open` and a positive remaining amount. Paid and void invoices are excluded. Values remain in source minor units; USD 205,000 minor units means USD 2,050.00, not USD 205,000.00. All three customers have outstanding invoices in both phases. The open-support customer set changes from `{customer_a, customer_b, customer_c}` to `{customer_b, customer_c}`.

On refresh, customer A's billing ID becomes `cus_synthetic_a_reissued`; Intercom retains the old ID, linked by the explicit historical alias. One invoice is paid, another partially paid, and conversation 1 closes with a new customer-authored part: “Invoice export is fixed; CSV export now succeeds.” Preserve both the original issue and its later resolution with separate citations. The capability flags remain `crm: false` and `transcripts: false`; answers must not manufacture pipeline stages, opportunities or call summaries.

## What this foundation does and does not verify

The oracle verifies fixture semantics and exact text attribution. It does not verify an adapter, normalization into DuckDB, a context agent, a model answer, live authentication, refresh scheduling, durable incremental state, tenant isolation or performance. Actual end-to-end implementations must compare against these expectations and retain their own measured evidence. A passing fixture oracle is not Customer360 acceptance.

## Run the actual local connector

With the documented pinned connector extra installed, `runtime.py` makes local SQLite sources from these arrays and runs the production SQLite → dlt worker → staged DuckDB publication path. Use a separate workspace for each profile:

```sh
.venv/bin/python examples/customer-360/runtime.py \
  --workspace .oriel/customer360-crm-sample --profile salesforce-gong
.venv/bin/python examples/customer-360/runtime.py \
  --workspace .oriel/customer360-billing-sample --profile billing-support

# A new complete extraction into the same reviewed source configuration:
.venv/bin/python examples/customer-360/runtime.py \
  --workspace .oriel/customer360-crm-sample --profile salesforce-gong --phase refreshed
```

The importable entry point is `load_profile(workspace, profile_name, phase="initial", fixture_root=None)`, with `fixture_root` passed by keyword. Its receipt lists actual source publications, resource tables, record counts and original-to-normalized column mappings. All resources are explicitly selected with `chunk_size=2`. Each published record is reconciled against its source array, including nested values and string IDs.

Connections are named `fixture_salesforce`, `fixture_gong`, `fixture_stripe` and `fixture_intercom`, and their connector type is **SQLite**. Scalar source fields become typed columns; nested containers remain JSON text. `record_json` retains each complete original record, `fixture_record_id` retains its string ID, and `fixture_position` retains array order. An unselected sentinel table checks resource exclusion. Private run receipts remain under the sample workspace's `customer360-fixture-runtime/` directory, including failures.

This loader does not approve customer identities or profiles and does not invoke a model. It verifies the actual local SQL connector transport for both source-shaped profiles; it does not verify Salesforce, Gong, Stripe or Intercom adapters, API pagination, or live vendor access. Existing unrelated or reconfigured source connections are refused. The full source-pipeline acceptance gate also requires the real context and answer boundaries.
