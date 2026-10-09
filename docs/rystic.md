# Synthetic Stripe + Linear with Rystic

This recipe runs a cross-tool analytics workflow against local [Rystic simulators](https://www.rystic.ai/simulators), saves their data in DuckDB, and turns a reviewed answer into reusable context. It uses synthetic customers, payments and issues. It needs no Stripe or Linear credentials.

Rystic is the first benchmark environment for Oriel's broader existing-tool analytics workflow. Stripe + Linear is the selected synthetic fixture; users can bring data from other tools through the [generic import path](../README.md#bring-your-own-tools). A Rystic runtime is not required for that path.

The question is:

> Which paying customers have unresolved urgent/high issues, what net collected USD do they represent, and who owns the issues?

The working loop is **source APIs → DuckDB snapshot → provisional answer → reviewed metric → refresh → reuse → bundle export**. The source-tool MCP path is also implemented so a later agent benchmark can compare the two access methods. **That benchmark is deferred. This integration makes no accuracy, cost or speed claim.**

## Try the captured example without an account

From the repository root, with its dependencies installed:

```sh
uv run oriel --workspace .rystic-workspace-demo rystic load-example
uv run oriel --workspace .rystic-workspace-demo rystic preview
```

This loads a hash-checked [capture](../contextbase/data/rystic-stripe-linear.json) from an actual run of the pinned native Rystic simulators. It contains the initial recipe cohort: **12 customers, 24 charges and 17 issues**. The provisional answer identifies **5 customers associated with $1,857.69 in net collected USD**.

The output includes the SQL, explicit business rules and snapshot receipt. `refresh_mode: bundled_capture` identifies this as a fixed capture, not a fresh source read. No Rystic runtime, account, API key or model call is needed for these two commands. Use a separate workspace for the live procedure below.

## Review the context, then reuse it

```sh
uv run oriel --workspace .rystic-workspace-demo rystic propose-context \
  --owner demo_policy_owner
```

Read the returned proposal and `.rystic-workspace-demo/context/candidates/net_exposed_collected_usd.json`. The proposal combines observed schema and join evidence with an explicitly authored synthetic recipe policy. It does not infer business meaning from column names or approve itself.

The policy counts succeeded, paid, captured, undisputed USD charges, subtracts refunds, excludes internal customers, and requires at least one unresolved issue with priority 1 or 2. Payment and issue totals are calculated separately before joining, so several issues cannot multiply payments. Missing owners remain explicit. The result describes historical payments associated with unresolved issues; it is not ARR, churn probability or predicted loss.

After reviewing the SQL, rules and witnessed result, replace `REVIEWED_SHA256` below with that proposal's exact `approval_sha256`:

```sh
uv run oriel --workspace .rystic-workspace-demo metric approve \
  net_exposed_collected_usd --approve REVIEWED_SHA256 --reviewer demo_policy_owner
uv run oriel --workspace .rystic-workspace-demo rystic report
uv run oriel --workspace .rystic-workspace-demo metric run net_exposed_collected_usd
uv run oriel --workspace .rystic-workspace-demo bundle export \
  --output rystic-bundle --workspace-id example-workspace
```

Approval binds the exact candidate and its evidence; a stale or altered proposal is rejected. This is a local review record, not authenticated organizational certification. The export contains context, metric definitions, skill material and registration envelopes for review. It does not publish to a registry or include source rows. Synthetic definitions remain labeled as examples.

## Run the two simulators locally

The verified versions are:

| Component | Version | Published |
| --- | --- | --- |
| Rystic CLI | `0.0.25` | October 1, 2026 |
| `stripe-twin` | `0.0.8` | September 30, 2026 |
| `linear-twin` | `0.0.1` | October 1, 2026 |

Follow Rystic's [installation documentation](https://www.rystic.ai/docs/installation). Download and inspect the installer before executing it; do not pipe a network response into a shell. Pin `RYSTIC_VERSION=0.0.25`, set `RYSTIC_NO_TRIAL=1` to defer the short trial until you are ready, and choose a local installation path. Alternatively, download that version's archive from the official release endpoint and verify its release checksum before extracting the binary. Keep the runtime outside tracked source files; this repository does not redistribute Rystic binaries.

The verified local setup downloaded the pinned CLI archive, checked its official SHA-256, and extracted only the executable to `.rystic-tools/rystic`. The commands below use that layout and keep runtime state separate:

```sh
export RYSTIC_HOME="$PWD/.rystic-runtime"
export PATH="$PWD/.rystic-tools:$PATH"
rystic version
rystic trial start
rystic pull --version 0.0.8 stripe-twin
rystic pull --version 0.0.1 linear-twin

rystic run --version 0.0.8 --scenario design_studio \
  --port 18981 --name oriel-stripe -d --json stripe-twin \
  -- --addr 127.0.0.1:18981
rystic run --version 0.0.1 --scenario linear_sprint_board \
  --port 18982 --name oriel-linear -d --json linear-twin \
  -- --addr 127.0.0.1:18982
```

The explicit addresses bind the simulators to numeric loopback. Oriel accepts local simulator URLs, verifies the expected product/version and records the reported model hash before source operations; it is not a configurable proxy to production services. If either port is occupied, choose two unused ports and pass matching `--stripe-url` and `--linear-url` values to the Oriel commands.

The [CLI reference](https://www.rystic.ai/docs/cli-reference) documents the no-signup 15-minute trial, pinned pulls and named-process lifecycle. The trial can be unavailable if the network already used its daily allowance. After expiry, running simulators need an eligible Rystic account/key or enterprise license. Configure credentials through Rystic's supported local mechanism; never put a key in this repository. The offline captured example remains usable after the trial ends.

## Create the fixture and sync it

```sh
uv run oriel --workspace .rystic-workspace-live rystic status
uv run oriel --workspace .rystic-workspace-live rystic seed \
  --accounts 12 --seed 7
uv run oriel --workspace .rystic-workspace-live rystic sync
uv run oriel --workspace .rystic-workspace-live rystic preview
```

`seed` creates synthetic records through ordinary Stripe and Linear simulator APIs. It does not call the premium `/_rystic/seed` control route, reset a simulator or change a real service. It uses an existing synthetic Linear team and states from `linear_sprint_board`. The two built-in worlds are unrelated, so this recipe adds its own explicit account identifiers and fixture namespace rather than guessing customer matches.

The setup writes a local seed receipt. Duplicate fixture creation is refused. A partially completed setup leaves a partial receipt and retains the created records for inspection; do not delete that receipt to force a retry. Start a fresh pair and workspace when a clean episode is needed.

`sync` exhausts supported pagination, validates required fields and account mappings, then atomically replaces only the adapter's managed source tables. Built-in simulator records remain outside the selected cohort. Ambiguous or orphan identities fail the sync. Local edits to managed source rows prevent an overwrite.

Every required sync now records an attempt before reading sources. If the source is invalid, unavailable, or the import fails, existing tables are retained and ordinary snapshot queries, metrics, transformations and change previews stop. An interrupted attempt also stays blocked. Inspect the recorded evidence without contacting Rystic:

```sh
uv run oriel --workspace .rystic-workspace-live refresh-status
```

The status includes the latest attempt and last successful snapshot. A complete source-validation receipt can identify duplicate customer mappings or missing internal-account policy. Unknown schema or transport failures are marked unverified, without a fabricated source hash or a confident list of blockers. Repair the source and complete `rystic sync` to restore access. Loading a captured example cannot clear a failed live refresh; use a separate workspace for that example. This is a recorded refresh boundary, not a continuous freshness monitor or protection against someone directly editing the database.

For benchmark preparation, `sync --capture-output NEW_PATH.json` additionally retains the full source-validation capture from that exact attempt. It contains source records, uses owner-only file permissions and refuses to overwrite an existing file before reading sources. Valid and refused captures can then feed the offline [phase evidence check](rystic-benchmark.md#check-saved-phase-evidence). The capture is saved before import; an inability to save it prevents import and leaves the failed attempt blocking snapshot reads. A partial file is not usable evidence. This optional retention path has been checked with source doubles and local DuckDB, but still needs native integration verification.

Run the review commands above with `.rystic-workspace-live` as the workspace. After a source change, run `rystic sync` and `rystic report` again. Reports remain tied to the imported snapshot until refreshed; they do not claim to show current source state between syncs. An unchanged refresh preserves the data hash, and a data change can reuse the same approved definition.

## Connect an agent through MCP

For local SQL and reviewed metrics, configure your stdio MCP client to launch:

```sh
uv run oriel --workspace .rystic-workspace-demo serve --arm metrics
```

For direct reads from the running source simulators, use the live workspace and launch:

```sh
uv run oriel --workspace .rystic-workspace-live serve --arm rystic_source \
  --stripe-url http://127.0.0.1:18981 --linear-url http://127.0.0.1:18982
```

Use absolute executable and workspace paths in your client's configuration. Both commands speak MCP over stdio; the difference is data access. The source arm exposes `sources`, `context`, `stripe_list`, `stripe_get`, `linear_query` and `refresh_status`. Stripe lists support pagination and scalar source query parameters. Linear accepts read-only GraphQL queries, variables, aliases, fragments and projections; mutation and subscription operations are rejected. Both arms can read the same local context and recorded refresh evidence. A blocked local snapshot does not disable direct-source reads. Neither MCP arm exposes source writes.

This is Oriel's MCP server backed by Rystic's modeled APIs. It is not the official hosted Stripe or Linear MCP server. Functional checks cover protocol initialization and execution, pagination, shared context and fixture identity parity. Model evaluation, inference costs and performance comparison remain future work.

## What was verified

The [functional proof](../examples/rystic/functional-proof.json) records an actual native simulator run. Its initial answer matched an independent arithmetic oracle. After the verification harness resolved both qualifying issues for one synthetic customer through Linear's simulator API, the existing snapshot retained its old answer until sync. Refresh then produced **4 customers and $1,490.61**, with the same context version.

The separate [source-tool proof](../examples/rystic/source-tools-smoke.json) records successful source pagination, object reads, GraphQL projections and cursors, MCP calls, read-only denials, and shared context checks. Both proofs used **zero model calls**. They establish a working integration, not comparative agent accuracy or production API fidelity.

The [Stripe simulator](https://www.rystic.ai/docs/simulators/stripe) does not model subscriptions or invoices, so this example must not be presented as recurring-revenue analytics. The [Linear simulator](https://www.rystic.ai/docs/simulators/linear) documents that `issues.filter` is ignored; the live checks reproduced that limitation. An agent must inspect returned rows and apply the required predicates. A later benchmark must disclose this limitation and must not attribute it to production Linear or to MCP as a protocol.

Simulator payloads and fixture records stay local in this setup. Metered Rystic runtimes still make license/usage check-ins containing runtime metadata; see its [privacy policy](https://www.rystic.ai/privacy). These local commands do not invoke a model. Connecting an agent adds that agent's configured model data flow.

Stop only the named simulator processes when finished:

```sh
rystic stop oriel-stripe
rystic stop oriel-linear
```

Their state is in memory. Keep the exported context and snapshot receipts to inspect the work after shutdown; use the captured example for a repeatable account-free walkthrough.
