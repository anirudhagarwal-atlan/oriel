# Contributing to Oriel

Help people understand their customers across the tools they already use. A clearer tutorial, a reproducible connector fix or a useful customer recipe is a welcome contribution.

Try the [quickstart](docs/quickstart.md) first. For a broad change or new dependency, open an issue describing the user problem and proposed scope before starting work.

## Develop locally

Use Python 3.10–3.14, uv and a POSIX environment:

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer-dev customer app --port 8787
```

Open `http://127.0.0.1:8787` and use a sample profile. Keep the committed lockfile; some vendor adapters need additional drivers or SDKs beyond the connector extra.

## Verify your change

Run the tests relevant to your change. The test suite uses Python's standard test runner:

```sh
uv run --no-sync python -m unittest discover -s tests -v
```

For a connector or data-path change, these independent examples need no model or vendor account:

```sh
uv run --no-sync python examples/tool-exports/verify.py --output benchmark-runs/contributor-example
uv run --no-sync python examples/connectors/verify_sqlite.py --output benchmark-runs/contributor-sqlite
uv run --no-sync python examples/connectors/verify_apis.py --output benchmark-runs/contributor-apis
```

Use new output directories. The API example uses a synthetic loopback server. Full Customer 360 acceptance has additional browser and installation prerequisites; follow [build status](docs/build-status.md#reproduce-the-checks) when your change needs those checks. Model and live-vendor runs require separate setup and authorization.

For documentation, verify the affected commands and links, then regenerate the HTML mirrors:

```sh
uv run --no-sync python scripts/render_docs.py
uv run --no-sync python scripts/render_docs.py --check
uv run --no-sync python -m http.server 8879 --bind 127.0.0.1
```

Preview `http://127.0.0.1:8879/docs/index.html`. Markdown is the source of truth; commit the changed Markdown and generated HTML together.

## Useful contributions

| Area | What makes a strong contribution |
| --- | --- |
| First use | A clearer setup step, actionable error message or tested walkthrough |
| Customer recipes | Synthetic inputs, explicit identity mappings, reviewed definitions, independently checked answers and a refresh case |
| Connectors | Exact resources and runtime tested, with fixture checks distinguished from authenticated vendor checks |
| Context | A business rule, join, unit or time boundary that is easier to inspect and review |

## Preserve the contract

The core path is **connectors → DuckDB → context proposals and review → grounded answers**. Keep these properties intact:

- Context proposals and metric definitions need explicit review; a successful query does not approve a business rule.
- Identity conflicts stay visible. Names or domains alone do not silently merge customers.
- Failed syncs preserve the last good snapshot and report freshness honestly.
- Answers retain their source evidence, units and reviewed definitions.
- Agent-facing tools stay read-only. Local transformations do not imply upstream actions.
- Catalog availability, fixture execution and live compatibility remain separate claims.

Use synthetic data in examples and public attachments. Never commit credentials, customer exports, private databases, production traces or internal screenshots. Configuration examples use environment-variable names, never secret values.

## Submit a pull request

Explain the user problem, resulting behavior, verification and remaining limits. Keep the change focused and call out changes to credential handling, SQL restrictions, local writes or dependencies for reviewer attention.

New dependencies need a clear reason, provenance review and a lockfile update consistent with the project's release-age cutoff. If you adapt upstream code, preserve its license and notices and update [source provenance](docs/upstream.md).

Comparative evaluations are deferred and are not required for core contributions. Research plans, evidence standards and historical results live in the [maintainer reference](docs/maintainers.md).
