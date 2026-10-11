# Oriel documentation

Build a customer view from the tools you already use. Oriel brings connector output into DuckDB, turns business context into reviewed definitions, and answers customer questions with calculations and source evidence.

**New here? [Run your first customer review →](quickstart.md)**

The sample runs locally without vendor credentials or a model. When you are ready, use a context agent to propose definitions and answer questions with your configured model.

## Use Oriel

| You want to… | Start here |
| --- | --- |
| Try the complete workflow | [Quickstart](quickstart.md) — connect, review, open a brief and compare a refresh |
| Connect customer tools | [Connectors](connectors.md) — available sources, setup and verification status |
| Learn the application | [Application guide](customer-360-app.md) — sources, reviews, questions and evidence |
| Understand and reuse context | [Context guide](context-guide.md) — observations, definitions and approval |
| Work from the terminal | [Customer 360 CLI](customer-360-cli.md) — command equivalents for the customer workflow |
| Use an existing agent | [Agent setup](agents.md) — Claude CLI and read-only MCP |
| Check requirements and limits | [FAQ](faq.md) and [build status](build-status.md) |

## Explore further

- [Use cases](use-cases.md): questions and recipes to adapt to your customer stack.
- [Export walkthrough](tool-exports.md): import CSV/JSONL, join records, review metrics and reuse a context bundle.
- [Architecture](architecture.md): connectors, DuckDB, agent tools and local execution.
- [Context and registry contract](context-and-registry.md): artifact formats and sharing boundaries.
- [Contributing](../CONTRIBUTING.md): develop locally, verify a change and submit a pull request.

Oriel is a developer preview for one trusted local operator. The sample workflows have local and configured-model verification; authenticated vendor workflows remain unverified. See [build status](build-status.md) for the current evidence.

[Project overview](../README.md) · [Release notes](release-notes.md) · [Source licenses](upstream.md) · [Maintainer reference](maintainers.md)
