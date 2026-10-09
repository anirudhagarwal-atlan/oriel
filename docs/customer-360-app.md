# Run the local Customer 360 application

Oriel's application is a developer preview for one trusted operator on a private computer. It uses the same connector, DuckDB, identity, context and answer contracts as the [customer CLI](customer-360-cli.md). A browser interface does not establish live-vendor compatibility or complete the [build acceptance gates](customer-360-goal.md).

## Install and start

From a checkout, install the locked connector runtime and choose a new workspace:

```sh
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer customer app --port 8787
```

Open **http://127.0.0.1:8787** directly. The server binds only to numeric loopback. A new application workspace is created with owner-only directory access; its database and job receipts are kept owner-only. Existing shared directories, databases with broader permissions and symbolic-link workspace paths are refused. Choose a new private workspace or review its permissions explicitly; the application does not silently change an existing shared workspace.

The current application requires POSIX workspace locking. Only one application process can own a workspace at a time. Stop it with Ctrl-C. Other trusted operator CLI actions still use the domain's source locks and transaction checks; application ownership is not a universal filesystem lock.

## Try both source combinations

Choose **Billing + support** or **CRM + conversations** in the application. Both follow the same review flow. The first has no CRM or transcript records.

1. Load the sample sources. This creates controlled source databases and runs the actual SQLite/dlt extractor into staged DuckDB tables. The records are synthetic; Salesforce, Gong, Stripe and Intercom-shaped records do not constitute calls to those vendors.
2. Prepare the review. Inspect proposed customer links, conflicts and field mappings. Approve only the exact unambiguous link proposal. A shared customer name or domain cannot establish identity.
3. Propose and review the source mapping, then materialize the reviewed customer records. Conflicting and missing relationships remain unattributed and visible as gaps.
4. Bootstrap definitions with the configured model after explicitly allowing model-data access and setting a cost limit. Optionally enter a **Task request** to guide the proposed definitions. To explore without inference, use the clearly labeled authored sample definitions instead. They are an example, not a model run.
5. Inspect the typed calculation scope, units and unresolved meaning before approving the exact context candidate. The model's freeform description is a provisional reviewer note, not the calculation's meaning.
6. Select a customer, read its brief, and inspect the supporting calculation and exact source evidence. Ask a question with the configured model when ready; consent is off by default.
7. Save the brief. Load the refreshed sample data, review any new source aliases, materialize the new snapshot, and compare with the saved brief. Preserve existing source mapping and business definitions when they are unchanged.

Approvals are separate actions. Preparing data or a proposal never approves customer links, field mappings or definitions. Reviewers are local operator labels, not authenticated identities. Model-written free factual prose is not accepted by the current answer contract: the agent selects recomputed metrics and exact attributed quotations.

The optional task request is single-line text, bounded to 10,000 characters and 10,000 encoded JSON bytes. Unicode characters and escaped quotes consume more of the byte allowance; the server checks it before model initialization. The browser trims outer whitespace and omits an empty request. Both bootstrap stages retain the submitted text and its hash; the candidate and approved context display it as **Retained operator task request — not reviewed policy**. It does not establish units, business meaning or approval. The CLI's `customer context bootstrap --intent` and the application's `context.bootstrap` job `intent` field support the same optional request; consent and budget requirements still apply.

Definition reviews and metric cards show a deterministic calculation before any description: count or sum, capability kind and mapped value role, all typed filters joined conjunctively, unit and separate currency grouping. Only the selected customer's resolved records in the selected snapshot qualify; unresolved, conflicting and other customers' records are excluded. The definition name is an identifier. Its freeform description appears under **Provisional description / reviewer note — not calculation scope** and cannot change the query. Boolean `true`, integer `1`, number `1.0` and string `"1"` remain distinguishable in the calculation text. Historical receipts are not rewritten to add this presentation.

## Connect your own sources

Choose a connector, an explicit connection name and selected resources. Readiness reports installed prerequisites and adapter limitations; it is not evidence of successful authorization or live compatibility. Credential inputs are environment-variable **names**, never credential values. Set credentials through your existing local credential workflow before starting the application. Do not paste tokens, passwords or private keys into the interface.

The resource preview distinguishes selected output tables from parent resources that must also be fetched. For example, selecting Discord `messages` declares reads of `channels` and `guilds` to locate those messages. Expand **Inspect selected outputs and fetch dependencies** to see the plan. Required parent parameters appear in the form with a **parent dependency** label. Review access to every listed resource before syncing.

A `declaration_derived` plan follows packaged REST parent declarations; it does not verify actual requests, permissions or complete vendor coverage. Other families report `fetch_scope_unknown`, even when their output names are known. With a declared resource list, unchecking every checkbox refuses saving rather than restoring defaults. Sources without a static resource list retain their factory defaults and unknown fetch scope.

The same read-only preview is available through `oriel source plan TYPE --resource NAME` and the existing application session's authenticated `GET /api/source-plan?type=TYPE&resource=NAME`. Repeat `--resource` or the `resource` query parameter for several outputs; omission requests defaults. The endpoint accepts no credentials or vendor parameters, creates no job and does not connect to a source. See the [connector selection guide](connectors.md#choose-resources-and-parameters).

After syncing, select committed sources and capture a snapshot. Inspect their observed tables and columns, create workspace customers, and propose explicit source-key links. Build source mappings with the observed column selectors, review and materialize them, then bootstrap business definitions. The customer screen and answer tools use capability categories rather than vendor names.

For REST and GraphQL sources, expand **API sync limits** to set a maximum HTTP-attempt count and request deadline before using a source's **Sync** button. Each sync shares those limits across its resources and nested pages; failed attempts count. A failed or exhausted extraction retains the previous published data. These session settings do not apply to sample or profile refresh actions, and unsupported source families refuse the budget. The [connector guide](connectors.md#bound-an-api-sync) describes the CLI equivalent and scope limitations.

Generic direct-key mappings are available. Complex nested relationships may require a source-specific adapter to propose explicit links first; the packaged examples contain such adapters. The current interface does not infer arbitrary relationship chains automatically. Missing resources, dependency failures, unresolved identities and missing source capabilities must remain visible rather than being silently filled in.

In each field binding, use **Measure policies → Add measure policy** when a mapped value needs an explicit business rule. Choose its logical role, unit, currency role if applicable, whether summing is permitted, and a brief meaning and review basis. Unit and aggregation start as **Unknown — not confirmed**. No policy is added until you add a row; column names, numeric types and sample values do not confirm one. Changing the selected table clears its draft measure policies for review against the new fields.

Choose **Currency — major units** or **Currency — minor units** only when the source meaning establishes that scale, and select a different mapped currency role. **Nonmonetary number** has no currency role. If the scale is unknown, you can still record a known currency role without confirming the scale. **Sum permitted** concerns the mapped record grain; it does not approve a particular metric, filter or recurring-revenue interpretation. The server validates the policy with the mapping.

The sample-mapping preview and mapping proposal display each policy's unit, currency role, aggregation and meaning before approval. Unknown or missing policies stay explicit. New model-proposed sums need a compatible reviewed policy; the context agent cannot invent one. Legacy operator-authored definitions remain readable. Review a revised mapping before asking the model to use newly established meaning. The [foundation guide](customer-360-foundation.md#review-measure-meaning) shows the mapping JSON, and the [context guide](customer-360-cli.md#bootstrap-and-review-business-context) explains the separate definition approval.

Expand **Passage timestamps (advanced)** when a binding needs dated text. The optional JSON contains `passages` and `attachments` arrays described in the [CLI guide](customer-360-cli.md#ask-which-passage-is-latest). The server validates their fields, source keys and timestamp rules before producing a reviewable mapping proposal. Both packaged profiles include these mappings. The source-mapping review shows the passage paths, time meaning and any related metadata join.

For a latest-evidence question, the agent can select dated passages across the customer's complete local projection. Answers retain the selection receipt, exact text leaf, timestamp and source lineage; **Open evidence** reopens that same passage. Unknown dates, incomplete coverage and excessive ties cannot become a latest claim. **Evidence checked for this snapshot** confirms the scoped evidence checks; it does not certify that a quote answers every part of the question or that all vendor history was extracted.

## Read context notes after refresh

Approved definitions can be reused when their reviewed mapping remains compatible. The notes saved with that approval keep their original snapshot; a refresh does not rerun or rewrite them. **Notes retained from context approval** shows that snapshot and identifies notes that were not rechecked after refresh. Treat them as retained review observations, not newly established source facts.

**Current snapshot coverage** is computed from the projection used for that answer: missing reviewed capability categories, profile-wide unresolved identities and whether records are attributed to the selected customer. Profile-wide exclusions are not assigned to that customer. A historical brief's coverage describes its own recorded snapshot. Neither section establishes that every upstream record was extracted; use current evidence and source status when reassessing an earlier note.

## Model calls and retained data

The configured Claude CLI supplies inference. The application's individual model-run limit is at most USD 1; each run accounts for all its stages and stops on unknown cost, failures or exhausted limits. The application does not silently retry or resume interrupted inference. Its configured transport cannot be replaced through an HTTP request. Controlled verification may start a separate application with a scripted transport, which is labeled as test evidence.

Source extraction, model work and refresh run as serialized jobs. Their status and bounded results remain in the private workspace. Reusing a request ID does not dispatch the same job twice. Retain domain receipts before deciding whether to repeat an interrupted operation, especially when a provider call may already have occurred. A failed or stale current snapshot cannot become a current answer just because a saved historical brief remains readable.

If a current brief, evidence lookup, save or comparison cannot revalidate its source snapshot, the interface hides previous current results. This also covers a source refresh performed by another trusted local process. Saved historical reviews remain available with their original snapshot and an explicit historical label.

The server uses exact Host/Origin checks, a startup browser session, CSRF protection and text rendering for source content. This protects the browser boundary from other web origins; it is not hosted authentication or isolation from other processes on the same computer. Do not expose or proxy this local preview to a network or treat it as a multi-user service.

Browser receipt previews display integers outside JavaScript's exact range as decimal strings. The underlying stored values, evidence and approval hashes retain their original types and digits. Browser JSON previews are not a byte-for-byte export of the stored approval artifact.

Saved briefs, source samples, context candidates, job results and model traces may contain customer data. They stay in the workspace and are not public launch artifacts. Structural context-template export excludes customer rows, source text and rule literals.
