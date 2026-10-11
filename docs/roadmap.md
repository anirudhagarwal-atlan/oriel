# Roadmap

Oriel brings customer data through **off-the-shelf connectors → DuckDB → context agents and reviewed definitions → customer briefs and answers**. That path stays the same as we add sources and workflows.

These are priorities, not dated delivery commitments. See [current status](build-status.md) for what has been verified.

## Available in the preview

- Local customer reviews with explicit source links, field mappings and approved definitions.
- Customer briefs, source evidence, model-assisted questions and saved reviews.
- Refresh comparisons that reuse reviewed context and reveal changes in meaning.
- Two synthetic customer stacks, with public-clone installation and configured-model checks.
- CLI, read-only MCP access and portable context artifacts for advanced workflows.

## Next

1. **Validate real customer stacks.** Check authentication, selected resources, coverage and refresh for authorized live sources, including an additional connector.
2. **Make the first own-data review easier.** Learn where users get stuck in setup and mapping; improve relationship support where direct keys are insufficient.
3. **Make the next review worth returning for.** Help users reuse definitions and compare changing customer facts.

## Later

- Scheduled refresh and recurring review workflows.
- Portfolio questions and narrative summaries, with correctness and attribution checks.
- Context sharing through a verified registry and a second consumer.
- Team access with authentication and source-derived visibility.

Comparative MCP benchmarks remain deferred. We will publish performance claims only with reproducible evidence. Planning details and earlier experiments live in the [maintainer reference](maintainers.md).
