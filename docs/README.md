# Oriel documentation

Customer 360 for the tools you already use, with reviewed context and evidence behind each answer.

## Start here

| I want to… | Read this |
| --- | --- |
| Try a customer review | [Quickstart](quickstart.md): the local app, two sample stacks, evidence and refresh |
| Understand the application | [Customer 360 app](customer-360-app.md): separate reviews, model consent, own-source setup and local boundaries |
| Bring data from my tools | [Connectors](connectors.md): exports, configuration, credentials and readiness |
| Ask questions through my agent | [Agent setup](agents.md): Claude CLI and MCP stdio |
| Choose a useful first question | [Use cases](use-cases.md): runnable recipes and questions to adapt |
| Understand what bootstrap produces | [Context guide](context-guide.md): observations, reviewed definitions and reuse |
| Check limitations and data handling | [FAQ](faq.md) and [current evidence](build-status.md) |

The [project README](../README.md) gives the overview. Oriel is a developer preview, with local execution and explicit review. Authored sample definitions let you explore briefs without a model, simulator or registry account. Context-agent bootstrap and questions use the configured model with consent.

The latest tested revision passes 52 local cases and four configured-model cases across both synthetic profiles before and after refresh. Live-vendor validation and clean Git/public-checkout reproduction remain pending. The [evidence record](customer-360-goal.md) distinguishes current working-tree installation checks from committed and published source proof.

## Work through a complete example

Start with the [Customer 360 application](customer-360-app.md). Billing/support and CRM/conversation samples use the same interface. Load data, review identities and mappings, bootstrap and approve context, inspect a customer brief, then save and compare it after refresh. [Goal and verification](customer-360-goal.md) separates deterministic, actual model and live-vendor evidence.

The earlier [CRM seats and product activity](tool-exports.md) recipe remains a useful advanced CLI example. It shows identity review, metric reuse by a separate consumer and changed-data calculations. Its fixtures and independent expected-answer code are in [examples/tool-exports](../examples/tool-exports/README.md).

The [payments and unresolved issues example](rystic.md) is a separate synthetic recipe. Its recorded capture can run offline; running the simulator itself has additional requirements. Neither recipe establishes performance advantages over another architecture.

## Reference

- [Context and registry contract](context-and-registry.md): exact artifacts, approval semantics and integration boundaries.
- [Architecture](architecture.md): extraction, DuckDB, agent tools, local changes and research infrastructure.
- [Source provenance and licenses](upstream.md): reused components and snapshot compatibility.
- [Release notes](release-notes.md): implemented behavior and limitations.
- [Roadmap](roadmap.md): current scope and next priorities.
- [Contributing](../CONTRIBUTING.md): local development, verification and useful contributions.

## Project direction

- [Customer 360 goal and verification](customer-360-goal.md): active build goal, pass/fail gates, independent evidence and the verification command.
- [Customer 360 build plan](customer-360-plan.md): customer intelligence across existing tools, source-independent context agents and implementation gates.
- [Customer 360 foundation](customer-360-foundation.md): implemented operator commands for identity, snapshots, reviewed source mappings and evidence records; remaining product work is explicit.
- [Customer 360 CLI](customer-360-cli.md): business-context review, customer briefs, scoped model questions, evidence and refresh.
- [GTM plan](gtm.md): audience, activation, repeat use and the path to team context.
- [Launch kit](launch-kit.md): repository description, announcement drafts, demo script and release preparation.
- [Build charter](build-charter.md): longer-term objectives and constraints.

## Evaluation research

Comparative evaluations are deferred. These documents preserve the research design; they are not setup requirements or evidence of a proven advantage.

- [Evaluation infrastructure status](evaluation-status.md)
- [Historical exploratory benchmarks](benchmarks.md)
- [Rystic comparison design](rystic-benchmark.md)
- [Sequential study and accounting](sequential-study.md)
