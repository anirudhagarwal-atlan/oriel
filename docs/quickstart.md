# Your first customer review

Connect sample customer data, review its meaning, and open a brief with numbers and source passages you can inspect. Then refresh the data and see what changed.

This walkthrough uses **Billing + support**. **CRM + conversations** follows the same steps. Both are synthetic samples extracted through the real SQLite/dlt pipeline into DuckDB. You need no vendor credentials or model calls to try them.

## 1. Start Oriel

You need Python 3.10–3.14, [uv](https://docs.astral.sh/uv/), and a POSIX environment. Installation from a fresh public clone has been verified on macOS ARM64 with Python 3.12; other platform combinations need verification.

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer customer app --port 8787
```

Open **http://127.0.0.1:8787**. Keep the terminal running; Ctrl-C stops the app. The connector extra is required for sample extraction too.

Use a new workspace directory. Oriel keeps your DuckDB database, reviewed context and saved briefs there between sessions. This preview serves one trusted local operator; keep it on loopback rather than exposing it to a network. See the [application guide](customer-360-app.md) for workspace requirements.

## 2. Connect the sample

In **Sources**, select **Billing + support** and click **Connect sample data**. When extraction finishes, click **Prepare sample review**.

You now have a DuckDB snapshot and proposals ready for review. Nothing has been approved yet.

## 3. Review customer identity and context

In **Review**, enter a local reviewer label. Before each approval, inspect the proposal and select **I reviewed this exact proposal and its source scope**. The displayed hash identifies exactly what you are approving.

| Review | What to do |
| --- | --- |
| **Identity** | Inspect the proposed customer assignments and their basis, then **Approve identity links**. Conflicting links remain unresolved. |
| **Field mapping** | Click **Propose sample field mapping**. Inspect and **Approve field mapping**, then **Materialize approved mapping**. |
| **Definitions** | Keep model permission off. Click **Propose sample definitions**, inspect the filters, units, currencies and gaps, then **Approve definitions**. |

The sample definitions are authored examples. A context agent can propose definitions when you enable the model in step 6; both routes require your review. Missing or conflicting identity links stay visible as gaps and are excluded from attributed answers.

## 4. Open a brief and inspect its evidence

In **Customers**, select a customer and click **Open current brief**. You will see outstanding invoice amounts by currency, open support cases and attributed source passages.

- **Inspect calculation** shows a metric's reviewed definition and result.
- **Open evidence** shows the exact source passage behind a fact.
- **Save brief** retains this review before you refresh the data.

Currencies remain separate, and amounts retain their declared units, including minor currency units where applicable. Opening a brief uses deterministic calculations and does not call a model.

## 5. Refresh and compare

1. Click **Load sample refresh**. When extraction finishes, click **Prepare sample review** again.
2. Review any new identity links. Reuse the approved field mapping and click **Materialize approved mapping** for the new snapshot.
3. Click **Show approved context**. Keep the existing definitions when their meaning is unchanged.
4. Open the same customer's current brief, then choose **Saved reviews → Compare with current**.

The comparison shows previous and current facts while preserving the saved brief's original evidence. A change to identity, mapping or definitions is called out; Oriel withholds numeric deltas when the basis is no longer comparable.

To try **CRM + conversations**, stop the app and restart it with a new workspace, such as `.oriel-customer-crm`. Choose that sample and repeat the same workflow. Its local Salesforce- and Gong-shaped records are fixtures, not authenticated vendor connections.

## 6. Let a context agent propose and answer

For this optional step, use a separately installed, authenticated [`claude` executable on your `PATH`](agents.md#ask-through-an-authenticated-claude-cli). In **Definitions**, review **Model permission** and the per-run budget before opting in: selected customer schema, context and evidence will be sent to your configured model.

Click **Propose with model**, then inspect and approve the resulting definitions. With approved context, use **Ask with evidence** to ask a customer question. Answers validate recomputed metrics and exact quotations; arbitrary model-written factual prose is outside the current answer contract.

The app permits a budget of at most USD 1 per run and stops on unknown costs or exhausted limits. Read [model calls and retained data](customer-360-app.md#model-calls-and-retained-data) for the full boundary.

## Connect your own customer tools

Use **Sources → Connect a tool** to select a connector, connection name and resources. Supply credentials through your existing local environment before starting Oriel; enter **environment-variable names only** in the interface.

After sync, inspect the selected sources and create explicit customer links, field mappings and reviewed context. CRM, billing, support, product usage and conversation sources can participate; the workflow does not require one vendor pair. Connector readiness and authenticated compatibility are separate checks, and some source relationships need an adapter.

Continue with [connector setup](connectors.md) and the [application guide](customer-360-app.md). [Build status](build-status.md) records what has been verified, including the remaining live-vendor limitations.
