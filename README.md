![Oriel — Your customers, in context.](docs/assets/oriel-banner.png)

# The open-source Customer 360 agent

Your customer call starts in 20 minutes. You're piecing together CRM updates, support history, and call notes. Ask about an issue that was fixed last week, and the customer has to catch you up.

**What changed since the last time you spoke?**

Oriel brings your customer data into **DuckDB**, helps you review its business context, and builds briefs and answers with evidence you can inspect before the conversation.

**[Get started](#quickstart)** · [Documentation](docs/README.md) · [Connect your tools](docs/connectors.md) · [Contribute](CONTRIBUTING.md)

For customer success and account teams, with the technical teams supporting them. Bring the sources you have: CRM, conversations, support, billing, product usage, or internal data.

> **Developer preview.** Run locally for one operator. Try both sample workflows without vendor credentials or model calls. [What works today →](docs/build-status.md)

## The issue you were going to raise is already fixed

In the included **synthetic CRM + conversations sample**, the first review contains this call passage:

> “SSO rollout is blocked by an identity-provider configuration issue.”

Save the brief and load the sample refresh. The newer call says:

> “SSO rollout is unblocked; procurement approval is now pending.”

That changes the next conversation. You can ask about procurement instead of reopening the implementation issue. Oriel keeps both passages tied to their source records; you decide what they mean for the account.

![Oriel's local customer review application, using synthetic sample data.](docs/assets/customer-review.png)

*The working application showing the refreshed synthetic sample. [Follow the walkthrough](docs/quickstart.md).*

The next review brings a new question: **what changed again?** Refresh the data, reuse the definitions you approved, and compare the current brief with the one you saved. Changes to identity or business meaning stay visible too.

## Quickstart

You'll need Python 3.10–3.14, [uv](https://docs.astral.sh/uv/), and macOS or Linux.

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer customer app --port 8787
```

Open **http://127.0.0.1:8787**, then:

1. **Connect a sample.** Choose **Billing + support** or **CRM + conversations**.
2. **Review the context.** Follow the identity and field-mapping reviews, then use the authored sample definitions. No model needed for this route.
3. **Open a customer brief.** Inspect a calculation or source passage, save the review, and load the sample refresh to see what changed.

[Follow the walkthrough](docs/quickstart.md) for the exact review steps. To bootstrap definitions with a context agent and ask questions, use a separately installed, authenticated [`claude` CLI on your `PATH`](docs/agents.md#ask-through-an-authenticated-claude-cli), enable model access, and set a spending limit. Selected data is sent to the model only with your consent.

## From your tools to an answer you can inspect

![Customer tools flow through off-the-shelf connectors into DuckDB snapshots, then context agents and human review, then customer briefs and answers. Refresh the data and reuse approved context.](docs/assets/how-oriel-works.svg)

1. **Connect.** Use off-the-shelf connectors to extract selected resources from your tools.
2. **Load into DuckDB.** Keep queryable snapshots in your own local workspace.
3. **Bootstrap context.** Review customer links and field mappings. A context agent proposes business definitions; you inspect and approve them.
4. **Ask and inspect.** Compute answers against the captured data, follow the evidence, and reuse the context after refresh.

A new record can change the story. A different definition can change the number. Review what “open” means, which records belong to the customer, and whether amounts are in dollars or cents. Then use that same context on the next snapshot.

## Bring your customer stack

Oriel's connector catalog covers customer APIs, SQL databases, object stores, and files. Explore an adapter before configuring it:

```sh
uv run --no-sync oriel source list --available
uv run --no-sync oriel source describe salesforce
```

The customer workflow is independent of any one vendor. The included examples pair billing with support and CRM with conversations; neither pair is required. Add sources and map their fields to the customer information you need.

**A catalog entry is not a verified live integration.** Adapter requirements and resource coverage vary. Both included samples use real SQLite/dlt extraction on synthetic data; they do not connect to vendor accounts. See [connector readiness](docs/connectors.md#what-has-been-verified) and [the own-data setup guide](docs/customer-360-app.md).

## Build with us

Run the sample, save the first brief, and refresh it. Then [tell us where you got stuck](https://github.com/anirudhagarwal-atlan/oriel/issues). Useful contributions include source mappings, reviewed definitions, connector validation, and clearer onboarding.

[Contributing](CONTRIBUTING.md) · [Roadmap](docs/roadmap.md) · [Architecture](docs/architecture.md) · [CLI and MCP](docs/agents.md) · [FAQ](docs/faq.md)

Oriel is MIT licensed. Bundled third-party components retain their licenses and notices. [License](LICENSE) · [Attribution](docs/upstream.md)
