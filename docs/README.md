# Oriel documentation

Talk to data across the tools you already use, then keep the definitions behind the answer.

## Start here

| I want to… | Read this |
| --- | --- |
| Get a result with sample data | [Quickstart](quickstart.md): a cross-tool question, a checked answer and a refresh |
| Bring data from my tools | [Connectors](connectors.md): exports, configuration, credentials and readiness |
| Ask questions through my agent | [Agent setup](agents.md): Claude CLI and MCP stdio |
| Choose a useful first question | [Use cases](use-cases.md): runnable recipes and questions to adapt |
| Understand what bootstrap produces | [Context guide](context-guide.md): observations, reviewed definitions and reuse |
| Check limitations and data handling | [FAQ](faq.md) and [current evidence](build-status.md) |

The [project README](../README.md) gives the overview. Oriel is a developer preview, with local execution and explicit review. You can try the core workflow without a model, a simulator or a registry account.

## Work through a complete example

Start with [CRM seats and product activity](tool-exports.md). It shows why identity mapping matters, how to review a metric, how a separate consumer reuses it, and how the answer changes when source data changes. Its fixtures and independent expected-answer code are in [examples/tool-exports](../examples/tool-exports/README.md).

The [payments and unresolved issues example](rystic.md) is a separate synthetic recipe. Its recorded capture can run offline; running the simulator itself has additional requirements. Neither recipe establishes performance advantages over another architecture.

## Reference

- [Context and registry contract](context-and-registry.md): exact artifacts, approval semantics and integration boundaries.
- [Architecture](architecture.md): extraction, DuckDB, agent tools, local changes and research infrastructure.
- [Source provenance and licenses](upstream.md): reused components and snapshot compatibility.
- [Release notes](release-notes.md): implemented behavior and limitations.
- [Roadmap](roadmap.md): current scope and next priorities.
- [Contributing](../CONTRIBUTING.md): local development, verification and useful contributions.

## Project direction

- [GTM plan](gtm.md): audience, activation, repeat use and the path to team context.
- [Launch kit](launch-kit.md): repository description, announcement drafts, demo script and release preparation.
- [Build charter](build-charter.md): longer-term objectives and constraints.

## Evaluation research

Comparative evaluations are deferred. These documents preserve the research design; they are not setup requirements or evidence of a proven advantage.

- [Evaluation infrastructure status](evaluation-status.md)
- [Historical exploratory benchmarks](benchmarks.md)
- [Rystic comparison design](rystic-benchmark.md)
- [Sequential study and accounting](sequential-study.md)
