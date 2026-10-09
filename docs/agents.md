# Use your agent with Oriel

An agent can inspect a local snapshot, read its context, query DuckDB and execute approved metrics. Start with the [quickstart](quickstart.md) to create a workspace and review a definition. Your source tools remain behind the explicit configure-and-sync workflow.

## Ask through an authenticated Claude CLI

Oriel's built-in `ask` command uses a separately installed, authenticated `claude` executable on your `PATH` and its configured model. It does not install Claude, log in or choose a model for you.

From the same terminal and repository directory as the quickstart, inspect the local state first:

```sh
.venv/bin/oriel --workspace "$ORIEL_QUICKSTART/workspace" catalog
.venv/bin/oriel --workspace "$ORIEL_QUICKSTART/workspace" context show
.venv/bin/oriel --workspace "$ORIEL_QUICKSTART/workspace" source list
```

`context show` requires the approved context from the metric-review step. If using a different workspace or terminal, substitute its actual directory for `$ORIEL_QUICKSTART/workspace`.

For configured connectors, inspect `source list` manually: compare `last_sync` (the latest attempt) with `last_snapshot` (the last published data) and `configuration_changed_since_sync`. A failed sync preserves older, still-queryable tables. The quickstart's direct file imports have no configured connectors, so its source list is empty. Their catalog shows table shapes and counts; import timestamps and file hashes are stored in `_contextbase.imports`. Those timestamps record local ingestion, not the tool's export time or completeness. A context draft's `generated_at` likewise records bootstrap time.

The `refresh-status` CLI command and MCP `refresh_status` tool currently track only the recorded Rystic cohort workflow. They normally return `untracked` for ordinary connectors and file imports, including after a failed connector sync. They cannot substitute for inspecting connector attempts. None of these local records checks the current remote source.

When you are ready to send the question, context and relevant tool results to the configured model:

```sh
.venv/bin/oriel --workspace "$ORIEL_QUICKSTART/workspace" ask \
  --allow-model-data --max-cost-usd 0.50 --max-turns 4 \
  'Run the approved unused_seats_at_low_activation metric. Explain the result, cite its context version, and state that the answer uses the local snapshot.'
```

This command makes a model call and may incur charges. `--allow-model-data` explicitly permits that data flow for imported workspaces. The SQL query, bootstrap, metric execution and bundle export commands themselves run locally without a model.

The JSON response contains the answer, run status, tool trace, model usage and reported cost information. Check `status`, `cost_complete` and `spend_unknown` before treating a run as completed or fully accounted for. A model can answer without calling a tool, so a fluent answer alone is not evidence that it ran the approved metric. Inspect the trace for the metric result and version. A turn or budget stop may leave no final answer; unknown provider cost is not zero.

The built-in loop exposes only the workspace's read tools to the model. It does not let Claude operate the shell or call additional tools through this command.

## Connect a compatible MCP client

The installed CLI can serve the same local tools over stdio. Use absolute paths for both the executable and workspace. This is an example configuration shape for clients that accept `mcpServers`; adapt it to your client's documented configuration:

```json
{
  "mcpServers": {
    "oriel": {
      "command": "/absolute/path/to/oriel/.venv/bin/oriel",
      "args": ["--workspace", "/absolute/path/to/workspace", "serve"]
    }
  }
}
```

Replace both paths; JSON configuration does not expand the quickstart's shell variables. Use the executable installed in your chosen environment, which must remain available when the client starts. No hosted server, additional MCP package or automatic client configuration is required. Local stdio protocol behavior is tested; installation and interoperability in individual desktop or agent clients remain to be validated.

The default server exposes:

| Tool | Purpose |
| --- | --- |
| `catalog` | Inspect local schemas, columns and snapshot tables. |
| `context` | Read approved context, or a clearly marked draft when no approved context exists. |
| `query` | Run restricted read-only SQL over the local workspace. |
| `metric` | Execute an approved metric by name. |
| `refresh_status` | Inspect recorded Rystic cohort refresh evidence; ordinary connector and file-import freshness is untracked. |

These tools cannot sync a source, approve a metric, transform a table or apply a change. MCP being read-only here does not constrain other tools that you separately enable in your client.

There is no dedicated connector-status MCP tool. Do not assume the agent detected a failed generic sync: review `source list` yourself before asking and provide the relevant status when needed.

The client's model and permission settings govern whether MCP results leave your machine. Oriel's `ask --allow-model-data` flag applies to its built-in Claude loop; it does not authorize or control an external MCP client's data sharing.

## Give the agent a grounded question

For the reviewed quickstart workspace, use a prompt such as:

> Inspect the catalog and context. Run the approved `unused_seats_at_low_activation` metric, explain its population and exclusions, and cite its context version. State that this is a local snapshot and identify any freshness you cannot establish. If a required mapping or rule is missing, identify the gap instead of inventing it.

The deterministic metric returns **51** before the quickstart refresh and **8** afterwards. This expected SQL result is not a promise about a model's response. Compare the agent's trace with the local metric output.

For your own data, ask the agent to help inspect candidate keys, mappings and definitions. Have an operator review the SQL and rules, then explicitly approve the exact proposal through the CLI. Run source refreshes and reviewed local changes separately; agents have no autonomous source-write path. The [export walkthrough](tool-exports.md) covers the full review and reuse lifecycle, and [connector setup](connectors.md) covers explicit source refresh.
