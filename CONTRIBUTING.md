# Contributing to Oriel

Help someone get a useful answer across their tools and keep the definition behind it. A clearer tutorial, a reproducible connector fix or a well-specified recipe can be as valuable as a new feature.

## Try the user journey first

Follow the [quickstart](docs/quickstart.md). Then choose a focused contribution:

- **Improve first use:** remove a confusing setup step or explain an error with a tested example.
- **Contribute a recipe:** add synthetic inputs, an explicit identity mapping, SQL, an independent expected answer and a changed-data case.
- **Validate a connector:** name the exact source, resources, runtime and environment tested. Separate fixture checks from authenticated vendor checks.
- **Improve context:** make an ambiguous business rule, join or time boundary easier to inspect and review.

For a broad change or a new dependency, start with an issue describing the question, current limitation and proposed scope. Use the repository's bug report, use case or connector validation template. Do not include private exports or credentials.

## Run locally

Use Python 3.10–3.14 and the committed lockfile, which references public PyPI artifacts. Clone the repository and install the connector environment:

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
.venv/bin/python examples/tool-exports/verify.py --output benchmark-runs/contributor-example
.venv/bin/python examples/connectors/verify_sqlite.py --output benchmark-runs/contributor-sqlite
.venv/bin/python examples/connectors/verify_apis.py --output benchmark-runs/contributor-apis
```

Use new output directories. The export and SQLite checks are local; the API check uses a synthetic loopback server. None needs a model or live vendor account. Some vendor adapters need additional drivers or SDKs beyond the declared extra; do not add or install them implicitly.

Run tests relevant to your change. The complete standard-library suite is available with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

For documentation-only changes, verify commands and links affected by the change. Markdown is the source of truth. Rebuild its static HTML mirrors with the Markdown renderer already present in the locked connectors environment:

```sh
.venv/bin/python scripts/render_docs.py
```

Commit the changed Markdown and generated HTML together. The guides need no browser JavaScript or external assets; the landing page's local example uses its bundled script. Serve the repository locally:

```sh
.venv/bin/python -m http.server 8879 --bind 127.0.0.1
```

Visit `http://127.0.0.1:8879/docs/index.html`. The landing page links to the generated HTML guides. GitHub readers can use the original Markdown files directly. No hosted build service or browser Markdown library is required.

## Keep examples reproducible

Use synthetic identities such as `customer_001` and `example-tenant`. Include fixture provenance, assumptions, expected outputs and a failure case where relevant. Keep imported text inert; it must never become executable instructions.

Never commit credentials, private databases, customer exports, production traces, internal conversation text or screenshots of internal tools. Configuration examples use environment-variable names, not secret values. A scrubbed error is useful; a full data-bearing workspace is not an appropriate public attachment.

## Preserve the boundaries

- Bootstrap produces draft observations. Schema alone does not certify a business rule.
- A working query does not approve its metric. Preserve exact-preview and stale-proposal checks.
- Sync failures must retain the last good snapshot and report freshness honestly.
- Agent-facing tools stay read-only. A local transformation must not become an upstream action.
- Catalog presence, controlled tests, native fixture execution and live verification are separate evidence levels.
- New dependencies need an explicit reason, package provenance review and a lockfile update consistent with the project's release-age cutoff.

Changes to credential handling, SQL restrictions, local writes, dependencies or publication workflows need explicit reviewer attention. Do not weaken checks to make a fixture pass.

## Review a pull request

Explain the user problem, changed behavior, verification and remaining limits. Keep the change focused. For documentation, state which commands you ran. For a connector, identify the exact resources and failure behavior covered; a live claim needs appropriately scoped, sanitized evidence.

If you adapt upstream code, preserve its license and notices and update [the provenance record](docs/upstream.md). Oriel's MIT license does not replace bundled third-party licenses.

## Evaluation contributions

Comparative evaluations are currently deferred and are not required to try or contribute to the core workflow. Research changes should retain the task, fixture, expected result, fair baseline, measurement boundary and failures. Separate deterministic replay from actual model runs, and source retrieval from the MCP transport.

We welcome evidence that favors another approach. [Claim standards](docs/gtm.md#scorecard-and-evidence-policy) explain what supports a public claim. Do not launch paid model runs, contact source accounts or publish results as part of an unrelated contribution.
