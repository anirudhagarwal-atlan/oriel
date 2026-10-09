# Customer context and grounded answers through the CLI

This is the current developer workflow for the [Customer 360 build](customer-360-goal.md). It uses source connectors, committed DuckDB snapshots and reviewed context. The [local application](customer-360-app.md) exposes the same workflow. Scripted model tests prove orchestration behavior only; check the acceptance report for actual configured-model and live-vendor evidence.

## Start from reviewed customer evidence

Run these commands from the checkout root after the documented installation. They use `uv run --no-sync` and the same private `.oriel-customer` workspace as the [application quickstart](quickstart.md). If you launched the app with another workspace, replace `.oriel-customer` in every command with that path. Profile, snapshot, customer and approval IDs must come from that same workspace; replace the uppercase placeholders with its returned values.

Complete [identity, source mapping and materialization](customer-360-foundation.md) first. Retain the profile name, snapshot ID and workspace customer ID. The initial operator workflow requires explicit field and identity review before asking a context agent to propose business meaning; automatic source mapping is still to come.

For source-shaped sample data, [the native fixture runtime](../examples/customer-360/README.md#run-the-actual-local-connector) runs the production SQLite/dlt extractor against controlled databases. Its outputs preserve the original scalar and nested source values. The separate `examples/customer-360/review.py` recipe derives links from explicit references and authored synthetic decisions. Running it with `--approve-synthetic-fixture` simulates the review steps; it is not a human business review or proof of vendor API compatibility.

## Bootstrap and review business context

The configured Claude CLI supplies inference. Oriel sends bounded selected context and evidence only after `--allow-model-data`. Native model tools and MCP servers are disabled in the inference process; Oriel runs the scoped product tools itself.

```sh
uv run --no-sync oriel --workspace .oriel-customer customer context bootstrap --profile customer_review --snapshot SNAPSHOT_ID \
  --allow-model-data --max-cost-usd 1 --max-turns 6 \
  --intent 'Which reviewed amounts and support evidence can help prepare a customer review?'
```

`--intent` is optional. A supplied task request must be nonblank, single-line text, bounded to 10,000 characters and 10,000 encoded JSON bytes. Unicode characters and escaped quotes consume more of the byte allowance. Validation happens before model initialization. The CLI retains the accepted text verbatim, with its hash, in both proposal stages. It guides what definitions the agent proposes; it cannot establish business meaning, grant approval or override a reviewed measure policy. Omit the flag to bootstrap without a task request.

The context agent inspects observed capabilities and schema, retains missing or ambiguous meaning, and proposes business definitions. The output is a candidate. Inspect its source lineage, filters, units, gaps and recorded stages before reviewing the exact hash:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer context approve PROPOSAL_ID --approve APPROVAL_SHA256 --reviewer example-reviewer
uv run --no-sync oriel --workspace .oriel-customer customer context show --profile customer_review --snapshot SNAPSHOT_ID
```

An invoice amount is not automatically recurring revenue. An open support case does not establish renewal risk. The local reviewer supplies those business judgments; observed column names and model suggestions do not certify them.

Before asking the agent to propose a sum, review the source binding's optional [measure policy](customer-360-foundation.md#review-measure-meaning). `measures` is keyed by the logical value role and records its unit, currency role, permitted aggregation and business meaning. For example, this member of a binding records an unresolved decision rather than asserting major or minor currency units:

```json
{
  "measures": {
    "amount": {
      "unit": "unknown",
      "currency_field": "currency",
      "aggregation": "unknown",
      "meaning": "Currency is mapped; confirm amount scale and whether these records can be summed."
    }
  }
}
```

Both `amount` and `currency` must be mapped roles in that binding. New model-proposed sums require a reviewed compatible policy with a known unit and `aggregation: sum`; the agent cannot convert missing or unknown policy into a confirmed unit. Resolve the business decision in a new mapping proposal, approve it, materialize it, then bootstrap and review dependent context again. Legacy operator-authored definitions remain readable; compatibility does not grant a new model proposal permission to guess their meaning.

A sum covers every binding with the definition's capability kind. All need the same policy for that logical value role: known unit matching the definition, matching currency role and `aggregation: sum`. Partial, conflicting or unknown declared policies block the sum, including operator-authored proposals; filtering to one currency does not turn money into a nonmonetary number. Operator-authored sums with no policy on any matching binding retain the legacy path. Counts do not require measure policies. Changing the approved mapping or policy requires dependent context to be reviewed again.

You can also author a JSON object containing `definitions` and `gaps`, then propose it for review:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer context propose --file FILE --profile PROFILE --snapshot SNAPSHOT_ID
```

Each definition declares its name, description, capability kind, operation, logical value field, filters, unit and currency field. Version one supports sums and record counts, with explicit `eq`/`in` filters. Exact sums require integer or DECIMAL source columns; floats, absent numeric values and absent currency codes are refused. Formula metrics, relative time windows and unreviewed business classifications are not supported yet.

Review the typed calculation itself before its description: operation, capability kind and value role, every conjunctive filter, unit, and currency dimension. The calculation covers only the selected customer's resolved records in the selected snapshot. For example, `status eq "open"` is that exact string comparison; a description calling it “recurring revenue” cannot change its population or meaning. The application's review and metric cards lead with deterministic calculation scope and label freeform descriptions as provisional reviewer notes. New metric receipts retain `calculation_scope`; historical artifacts stay unchanged, with any derived display scope kept outside their stored receipts.

## Read a brief, ask, and inspect the evidence

A brief executes reviewed definitions and includes bounded attributed source quotations:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer brief --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID
uv run --no-sync oriel --workspace .oriel-customer customer metric --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID DEFINITION_NAME
```

The configured model can select relevant definitions and evidence in response to a question:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer ask --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  --allow-model-data --max-cost-usd 1 --max-turns 6 \
  'What do our reviewed numbers and support evidence show about this customer?'
```

The answer boundary recomputes selected metric references and checks exact quotations, customer attribution and text offsets. It renders verified facts and source statements. Model-written factual prose is not accepted in this initial version. Missing capabilities or definitions require an explicit abstention rather than an invented answer.

Metrics retain executed SQL, parameters, result hashes and contributing evidence IDs. Currency groups remain separate; minor units are labeled explicitly. Quotes retain their source record identity and exact field offsets. Each answer identifies its snapshot, approved source mapping, customer identity, business context and materialization. Coverage gaps remain visible. A current local publication does not establish upstream completeness.

```sh
uv run --no-sync oriel --workspace .oriel-customer customer evidence --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  EVIDENCE_ID --field text --start 0 --end 120
```

Model runs retain private input, tool and usage receipts inside the workspace. Budgets cover every inference stage. Unknown cost, failed calls, exhausted budgets or changed input versions stop the run; injected test clients are labeled separately from the configured model. Treat these receipts as customer data when using real sources.

## Ask which passage is latest

Recency requires an approved source mapping. Each optional `passages` entry names a logical text field, a JSON pointer pattern for its text leaves, and the timestamp's location, encoding and meaning. The timestamp can come from a mapped field, the containing array item, or a reviewed metadata attachment. For example, a support binding can map updates as follows:

```json
{
  "name": "support_update",
  "field": "updates",
  "text_path": "/conversation_parts/*/body",
  "time": {
    "source": "item",
    "path": "/created_at",
    "encoding": "unix_seconds",
    "meaning": "passage_created"
  }
}
```

Here `*` selects array elements in the text path; the time path is a concrete pointer relative to each text leaf's containing object. Timestamp encodings are explicit: `rfc3339` with a timezone, or `unix_seconds`. Extraction time and row order cannot establish when a statement occurred. An original message retains its creation time when later updates arrive.

Optional metadata attachments use explicit typed join keys, unique remote records and independently reviewed customer links. They enrich a record without adding another conversation or multiplying financial amounts. Cross-customer matches are refused. Missing joins remain unknown. Review these rules with the source mapping before approval.

For a complete vendor-neutral example, suppose your committed snapshot contains these observed tables. These are illustrative column names, not tables created by the example; use the exact names and types shown by **Inspect source fields** for your sources.

| Table | Required columns in this example | Purpose |
| --- | --- | --- |
| `notes.messages` | `message_id`, `organization_id`, `event_id`, `body` — all `VARCHAR` | One message per unique `message_id`; plain text in `body`. |
| `events.event_metadata` | `event_id`, `organization_id`, `started_at` — all `VARCHAR` | One metadata row per unique `event_id`; `started_at` is an explicit RFC 3339 string such as `2026-01-13T12:00:00Z`. |

Include both `notes` and `events` in the source snapshot. In the app's mapping form, choose table **notes.messages** (owned by connection **notes**), set entity **message**, kind **conversation**, record ID **message_id**, customer-reference connection **notes**, entity **organization**, column **organization_id**, and map logical field **text** to physical column **body**. Independently propose and approve each relevant `notes` / `organization` source ID and `events` / `organization` source ID to the same workspace customer. A matching `event_id` alone does not establish customer ownership.

Paste this complete object into that binding's **Passage timestamps (advanced)** field, then inspect and approve the resulting mapping proposal before materialization:

```json
{
  "attachments": [
    {
      "name": "event_time",
      "connection": "events",
      "entity": "event",
      "table": "events.event_metadata",
      "record_id": "event_id",
      "local_key": "event_id",
      "remote_key": "event_id",
      "customer_ref": {
        "connection": "events",
        "entity": "organization",
        "column": "organization_id"
      },
      "fields": {
        "started_at": "started_at"
      }
    }
  ],
  "passages": [
    {
      "name": "meeting_statement",
      "field": "text",
      "text_path": "",
      "time": {
        "source": "attachment",
        "attachment": "event_time",
        "field": "started_at",
        "path": "",
        "encoding": "rfc3339",
        "meaning": "event_started"
      }
    }
  ]
}
```

The empty `text_path` selects the entire plain-text `body`. The empty time `path` reads the mapped scalar `started_at` directly. The local and remote `event_id` columns must have identical observed SQL types, and remote keys must be unique. Attachments add time and lineage to the existing messages without adding conversation records. A missing or unresolved metadata association leaves time unknown; it does not fall back to extraction time. Review `event_started` as the intended meaning—it establishes when the event started, not when every statement within it was spoken.

After materializing and approving business context for this profile, select and reopen its dated text:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer latest --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  --kind conversation --limit 5
uv run --no-sync oriel --workspace .oriel-customer customer evidence --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  EVIDENCE_ID --field text --passage-name meeting_statement \
  --json-pointer '' --start START --end END
```

Use the evidence ID and offsets returned by `customer latest`; retain the quoted empty `--json-pointer ''` for the whole plain-text field. For the earlier nested support example, the equivalent selection is:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer latest --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  --kind support --limit 5
```

The selector checks all attributed records for that capability in the pinned local snapshot, including records outside index previews. Its receipt returns exact passage quotes, source timestamps and lineage. Missing or malformed timestamps and incomplete traversal prevent a latest claim. Equal timestamps return all ties up to the selected limit; a truncated tie set cannot support a latest answer. Local coverage does not establish completeness of upstream vendor history.

Open a returned passage using its exact locator and decoded-text offsets:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer evidence --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID \
  EVIDENCE_ID --field updates --passage-name support_update \
  --json-pointer /conversation_parts/0/body --start 0 --end 120
```

Use the returned start/end values for the actual passage. Its quote binds both the original source field and decoded text leaf. Latest selections are recomputed when an agent submits an answer; stale selections and omitted ties are refused. These checks establish source time and attribution, while question relevance still needs independent verification.

## Refresh and reuse

Run `source sync` for the selected connections, capture a new `customer snapshot`, then `customer materialize` with the existing reviewed profile and the new snapshot ID. Reuse the same approved business definitions when their source mappings are unchanged. Reopening the workspace in a fresh process uses the same stored context version.

If a field mapping changes, dependent business context requires review again. A failed refresh, including a missing configured file detected before extraction, keeps the previous publication intact but prevents presenting it as a current customer answer. Old result and quote references cannot pass against a new snapshot or context version.

## Save a review and inspect what changed

Save a brief before refreshing. The save operation computes its own brief inside the validated source transaction; it cannot accept a caller-supplied answer as trusted history.

```sh
uv run --no-sync oriel --workspace .oriel-customer customer review save --profile customer_review --snapshot SNAPSHOT_ID --customer CUSTOMER_ID
uv run --no-sync oriel --workspace .oriel-customer customer review list --profile customer_review --customer CUSTOMER_ID
uv run --no-sync oriel --workspace .oriel-customer customer review show REVIEW_ID
```

The retained review includes the original numbers, exact quotations, scoped record index and snapshot/context identifiers. It is explicitly historical and remains readable after a refresh or a failed sync, provided its retained evidence and approvals remain intact. It does not bypass freshness checks for current answers. Reviews contain private customer data and stay inside the local workspace; they are not included in exported templates. This is a single-operator local boundary: workspace/database permissions currently inherit filesystem defaults, so local storage alone does not enforce access restrictions for other operating-system users.

After sync, capture and materialization, compare against the new snapshot:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer review compare REVIEW_ID --snapshot NEW_SNAPSHOT_ID
```

The comparison shows the current brief, metric values before and after, and records added, removed or changed. Arithmetic deltas are available only when definitions, source mapping and customer identity mapping are unchanged. Changed review dependencies are flagged; a missing currency group is not treated as zero. Source-record aliases appear as additions/removals rather than guessed continuity. These are changes in observed local data, not a claim that every upstream event was extracted.

## Export a structural template

For a reusable structural starting point:

```sh
uv run --no-sync oriel --workspace .oriel-customer customer context export-template --profile customer_review --snapshot SNAPSHOT_ID --output context-template.json
```

This template excludes source records, text, descriptions, identity links, agent traces and rule literals. Filters require local values and review before use elsewhere. It is not a transferable approval or a finished Registry integration.
