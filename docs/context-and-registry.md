# From local data to reusable context

Contextbase separates observable data structure from business policy. Importing
tables makes them queryable. It does not establish what a business means by
pipeline, active customer, quarter, ARR, or at risk.

`bootstrap(workspace)` writes `context/draft.json` with the full local catalog,
row counts, candidate joins and unresolved policy questions. It checks candidate
parent keys for uniqueness and nulls, measures child key cardinality, and counts
unmatched child records. It exports these aggregate observations without source
rows. A join that works on this snapshot remains a candidate: its meaning and
future integrity require review. Empty parent tables are not evidence of a key.
Bootstrap never generates certified metrics.

## The synthetic demo's approved context

`install_demo_context(workspace)` installs four hand-authored demo definitions
after executing their SQL locally. The source is embedded in the package, so an
installed CLI does not depend on files in a cloned repository. The readable
copy is `examples/demo-context.json`.

| Metric | Explicit policy |
| --- | --- |
| `eligible_pipeline` | Sum opportunity ARR for S2, S3 and S4, closing on or after August 1 and before November 1, 2026. Exclude S1; do not use TCV. |
| `active_arr` | Sum positive MRR from active subscriptions and multiply by 12. |
| `active_accounts` | Count distinct accounts with at least one active subscription with positive MRR. |
| `at_risk_arr` | Active paid ARR where observed usage fell at least 30% and an open high/urgent support ticket exists. |

For risk, aggregate subscriptions and usage by account before combining them.
Tickets are an existence check, so two tickets cannot double ARR. Usage needs
both periods and a positive previous-period total. A missing or all-null current
period is unknown; an observed zero is a valid decline. These are demo decisions, not
universal business definitions.

`run_metric(workspace, name)` reads `context/approved.json`, requires a matching
approved metric and executes its whole SQL through the workspace's restricted
SELECT boundary. It returns actual rows, SQL, the context/metric versions and
an execution receipt with SQL/result hashes. It refuses unknown governed metric
names. It does not answer arbitrary natural-language questions or infer a new
metric from the question.

**Approval, validation and execution are distinct.** The demo's approval kind is
`curated_synthetic_demo`. Successful execution establishes that the SQL ran on
this local snapshot; it does not certify the business definition. Installation
receipts record their execution time and are not fresh-query receipts. A fresh
`run_metric` call executes again and returns a new receipt.

## Bootstrap real business definitions

For your own data, use the draft as a review packet. Add existing analyst SQL,
BI definitions and domain-owner decisions as evidence. Preserve full SQL and
business literals, including stage/status filters. Record disagreements and
keep unresolved candidates out of governed answers. Define population,
exclusions, grain, time/calendar policy, joins, owner and version explicitly.

For a fabricated example, `metric propose --synthetic-example` records that
label and keeps exported definitions as candidates. It does not certify source
data or authorize model access. The [tool-export walkthrough](tool-exports.md)
uses this path with different source schemas and a reviewed identity map.

Build a sealed set of representative questions before iterating on definitions.
Check SQL results against independently reviewed answers and changed-data cases,
including duplicate joins, missing usage, fiscal boundaries and excluded stages.
Promote only reviewed definitions. The CLI supports explicit local review for
your own imported data:

```sh
oriel --workspace ./my-data metric propose \
  --name paid_arr --sql-file ./paid-arr.sql \
  --description 'Annual recurring revenue from paid active subscriptions' \
  --grain 'One subscription' --unit 'USD/year' --owner analytics_owner \
  --rule 'Only active subscriptions with positive MRR; annualize MRR by 12'
oriel --workspace ./my-data metric approve paid_arr \
  --approve PROPOSAL_SHA256 --reviewer finance_reviewer
```

The first command writes `context/candidates/paid_arr.json`, including its
complete SQL, explicit business declarations, native definition, schema hash,
execution evidence, unresolved questions and approval hash. It witnesses exactly
one finite numeric scalar. Execution alone never approves a candidate. A missing
description, grain, unit, owner or rule remains an unresolved question and blocks
approval. Review that complete proposal before using its exact SHA-256.

Approval reruns SQL and refuses if the proposal, result, schema/row counts or
previous approved context changed. Regenerate and review a fresh proposal in
that case. A local lock prevents concurrent approvals from losing one another.
The approved definition records the supplied reviewer, time and proposal hash.
Other metrics remain intact, earlier contexts are saved under `context/history`,
and proposing an existing metric creates its next semantic version. This is a
local operator assertion, not authentication or production certification. The
hash protects the exact reviewed content; it does not identify the human.

For these custom metrics the native source is a derived query containing the
complete supplied SQL, with its one output normalized to `metric_value`. The
formula reads that value rather than guessing a new formula from the schema.
Grain, unit, owner and explicit rules remain in the definition. Execution receipts
include column names, row count and result hash, with no source-value sample.

## Portable export and Agent Registry handoff

`export_bundle(workspace, destination, workspace_id="example-workspace")` writes
only to a new or empty local directory. The result contains:

- `context.json`, with the context and local execution evidence;
- `models/*.json`, one observed schema model per table;
- `metrics/*.json`, native metric create envelopes;
- `metric_versions/*.json`, separate native append-version bodies preserving the full definition and Oriel source provenance;
- `registry_handoff.json`, matching create/update paths and source identities, with target artifact IDs deliberately unset;
- `skills/talk-to-data/SKILL.md` and `skill_index.json`;
- `manifest.json`, with file sizes, SHA-256 hashes and a bundle hash.

The export does not include source table rows, the DuckDB database, credentials,
or a network upload. Schemas and metric SQL can themselves be sensitive; review
your real bundle before sharing it. Hashes identify bytes and detect accidental
changes; they are not signatures or proof that a definition is correct.

Metric envelopes target the native Registry pilot `1.0-draft.1` contract:

```json
{
  "workspace_id": "example-workspace",
  "name": "active_arr",
  "source_type": "local",
  "source_id": "contextbase.metrics/active_arr",
  "source_uri": "metrics/active_arr.json",
  "source_version": "synthetic-gtm-v1",
  "definition": {
    "spec_version": "1.0-draft.1",
    "version": 1,
    "status": "candidate",
    "dialect": "duckdb",
    "formula": "SUM(s.mrr) * 12",
    "aggregation": {"type": "sum"},
    "output": {"alias": "active_arr", "datatype": "decimal", "unit": "USD/year"},
    "source": {"entity": "billing.subscriptions", "table": "billing.subscriptions", "alias": "s"},
    "mandatory_filters": ["s.status = 'active'", "s.mrr > 0"]
  }
}
```

The actual payload includes the complete example SQL, provenance and local
execution evidence under `definition.extensions.contextbase`. `source_id` is
stable across revisions; the source version and semantic metric version serve
different purposes. Synthetic demo approvals are exported as **candidates** for
the target workspace. The portable bundle format and skill index are Contextbase
formats, not a claim that Registry accepts the whole directory as one artifact.

A separately authorized integration can submit each create envelope to
`POST /registry/v1/artifacts/metric`. For an existing artifact, use the separate
append body at `POST /registry/v1/artifacts/metric/{artifact_id}/versions`.
The append body includes the complete `definition`, `description`, `source_uri`,
`source_version` and `source_metadata`. Replaying a create envelope as an update
is invalid: the native append route refuses artifact identity fields such as
`name`, `workspace_id` and `source_type`. Omitting `source_version` or
`source_metadata` clears them, so the export restates both explicitly.

Choose a real workspace deliberately and resolve the existing artifact by its
workspace and stable source identity. The handoff map never infers an artifact ID
from a name or authorizes a write. An append replaces the whole definition and
metadata bag; review any remote-only fields before submitting. The inspected
append API has no expected-parent/If-Match guard. Check the current head during
review and coordinate updates rather than assuming this export prevents races.

The Registry's execution fingerprint includes definition extensions. Fresh
query receipts therefore stay separate from the exported approval-time evidence;
rerunning an unchanged approved metric does not change its definition. A reviewed
definition revision advances its semantic `definition.version`. The exported
`source_version` identifies the local context revision, and the Registry's own
version ordinal is a separate value. Do not overwrite evidence extensions under
an unchanged semantic version just to record a data refresh.

Code inspection against the native metric pilot at gateway revision
`50524000a4b16884044c3120a5c3a85b955f2407` found the current create/append fields
structurally consistent. Its native `metric-validate` executable was then built
offline and accepted six unchanged exported create envelopes: four curated demo
metrics and two reviewed synthetic revisions of the tool-export metric. An
unsupported-spec control was rejected. The [structural verification receipt](../examples/tool-exports/native-metric-structure-proof.json)
records the input and native definition fingerprints. These hash profiles are
distinct; do not compare a native fingerprint directly with a raw JSON file hash.

The validator reported `metric-1.0-draft.1-structural-1`, pilot/experimental-field
warnings and `sql_validation: not_performed` for every accepted input. It checks
the definition and required name, not all standard artifact fields or append
admission. The custom revision changed eligibility from below 50% activation to
at-or-below 50%, advancing semantic version 1 to 2 and the local result from 51
to 56 on unchanged source data; a separate Python oracle checked those local
results. That local execution is separate from native structural validation.

No Registry server was started and no remote create/read/append round trip was
performed. Structural acceptance does not certify business meaning, SQL,
organizational access or remote persistence. Synthetic approvals remain
candidates. This repository performs no Registry write.

The later integration proof must create one reviewed synthetic metric in an
authorized workspace, read it back from `metadata.definition`, append a reviewed
second definition version to the same artifact ID, and read both versions back.
Compare complete definitions and source provenance; confirm a separate consumer
can execute the selected definition and that the first version remains available.
Local envelope tests and separate-process bundle reuse do not establish this
remote round trip or organizational access control.

## Why this also works with MCP

An agent can obtain the same context through local files, a CLI or MCP. Serving
a versioned metric and executing approved SQL addresses business semantics;
MCP is a transport. Compare per-source retrieval, SQL over MCP, SQL with context,
and named metrics separately. A direct local query beating paginated source
tools does not establish that it beats a SQL-capable MCP server.
