# Your first Customer 360 review

Start Oriel locally, connect synthetic customer records, and build a brief whose numbers and source passages you can inspect. The same flow works with **Billing + support** or **CRM + conversations**. You can complete this walkthrough with authored sample definitions and **no model calls or vendor credentials**.

Oriel is a local developer preview for one trusted operator. Its path is: **connectors → DuckDB snapshots → reviewed customer identity and context → answers**. Context agents can propose definitions when you choose to use a model; approval remains yours.

## Install from your checkout

Use Python 3.10–3.14, an existing installation of [uv](https://docs.astral.sh/uv/), and a POSIX environment. Fresh isolated installation from an allowlisted working-tree copy has been checked on macOS ARM64 with Python 3.12; other platform combinations need their own verification. A clean committed Git checkout and reproduction from the public repository remain pending. The commands below target the Customer 360 candidate. The app requires POSIX workspace locking.

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer customer app --port 8787
```

Open **http://127.0.0.1:8787** directly in your browser. Keep the terminal running; Ctrl-C stops the application. The locked connector runtime is needed even for the sample: extraction uses the real SQLite/dlt pipeline.

Choose a new workspace directory. Oriel creates it privately and keeps the database and job receipts there across restarts. Existing shared directories or databases and symbolic-link workspace paths are refused. Only one app process can own a workspace. This preview is for local use; do not expose or proxy it to a network.

## Connect the sample

In **Sources**, leave **Billing + support** selected and click **Connect sample data**. Wait for the extraction job to finish, then click **Prepare sample review**.

This loads synthetic invoices and support threads from local source databases through SQLite/dlt into DuckDB. It does not call a billing or support vendor. Preparing the review creates proposals and a snapshot; it approves nothing.

## Review identity, mapping, and definitions

In **Review**, enter a local reviewer label. For each approval, inspect the proposal and its source scope, select **I reviewed this exact proposal and its source scope**, then use the approval button. The displayed hash identifies the exact proposal you are approving; the reviewer label is not an authenticated organizational sign-off.

1. **Identity:** inspect each source record's proposed customer assignment and its basis. Approve the unambiguous proposal with **Approve identity links**. Leave conflicting proposals unresolved; they cannot be approved. A matching name or domain is not enough to link records.
2. **Field mapping:** inspect the authored source-to-business mapping, click **Propose sample field mapping**, review it, then **Approve field mapping**. Click **Materialize approved mapping** to build customer records from this snapshot.
3. **Definitions:** leave model permission off and click **Propose sample definitions**. Inspect the filters, units, currencies and gaps, then **Approve definitions**. These are authored examples, not output from a model.

Missing or conflicting relationships remain visible as gaps and are excluded from attributed customer answers. They are not silently merged or filled in.

## Open a brief and follow its evidence

Open **Customers**, select a customer, and click **Open current brief**. For Billing + support, inspect outstanding invoice amounts by currency and open support case counts alongside attributed source passages.

Use **Inspect calculation** to see a metric's reviewed definition and result. Use **Open evidence** to inspect an exact source passage. Currency groups stay separate, and amounts retain their declared unit, including minor currency units where applicable. The brief uses deterministic calculations and attributed evidence; opening it does not invoke a model.

Click **Save brief** to retain this review before changing the sample data.

## Refresh and compare

1. Click **Load sample refresh**, wait for extraction, and click **Prepare sample review** again.
2. Inspect and approve any new unambiguous identity links. Reuse the approved field mapping and click **Materialize approved mapping** for the new snapshot.
3. Use **Show approved context** to inspect the existing definitions. Reuse them when their meaning is unchanged.
4. Return to the same customer, open the current brief, then choose **Saved reviews → Compare with current**.

The saved brief retains its original evidence. The comparison shows current and previous facts, with changes in records and reviewed basis. If identity, mapping or definitions changed, it reports that change and suppresses numeric deltas that would imply an unchanged basis. Failed or stale current data cannot be presented as fresh merely because an earlier brief is still readable.

To try **CRM + conversations**, stop the app and start it with a different new workspace, such as `.oriel-customer-crm`. Choose that sample and follow the same review stages. These are local Salesforce- and Gong-shaped fixtures, not authenticated vendor connections. Neither sample requires the other tool categories.

## Optional: use the configured model

The authored route above needs no inference. To use a context agent, configure the supported Claude CLI first, then review **Model permission** and the budget in **Definitions**. The checkbox explicitly allows selected customer schema, context and evidence to be sent to the configured model. Consent starts off.

Choose **Propose with model** to generate a candidate, then inspect and approve it separately. Once context is approved, **Ask with evidence** can answer a customer question through the configured model. The current answer contract validates recomputed metrics and exact quotations; it does not accept arbitrary model-written factual prose.

The application's per-run limit is at most USD 1. Runs stop on unknown costs, failures or exhausted limits; interrupted inference is not silently retried. See the [application guide](customer-360-app.md#model-calls-and-retained-data) for the data boundary and [customer CLI guide](customer-360-cli.md) for configuration and command equivalents.

Both sample flows have browser verification with a **scripted test transport**, recorded separately from the configured-model check. The latest configured-model run passed independent checks of proposed definitions, exact amounts, substantive source passages and source times before and after refresh for both profiles, reusing the same reviewed context. Its six jobs used twelve calls reporting $1.4556692 in list-price estimates, with no retries or unknown spend. The full run passed 52 local and four model cases. These use synthetic data and real local extraction; the live-vendor gate remains open and overall completion remains false. See [build status](build-status.md) for the dated evidence and scope.

## Bring your own customer tools

Use **Sources → Connect a tool** to choose an available connector, explicit connection name and resources. Enter credential **environment-variable names only**, with values supplied through your existing local credential workflow before starting the app. Never paste secret values into the interface. Readiness checks report prerequisites and adapter limitations; they do not establish live compatibility.

After sync, capture selected committed sources, inspect their fields, and create explicit customer links and mappings. The model uses customer, commercial, billing, support, usage and conversation capabilities rather than a fixed vendor pair. Complex source relationships may need an adapter before their links can be reviewed.

Continue with the [application guide](customer-360-app.md) and [connector setup](connectors.md). For the advanced generic SQL workflow, the [tool-export walkthrough](tool-exports.md) covers CSV/JSONL joins, transformations, metric review and local bundle reuse.
