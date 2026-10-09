# Customer 360: build goal and self-verification

Status: active build goal, 10 October 2026. The [build plan](customer-360-plan.md) explains product direction and sequencing. The [acceptance contract](../verification/customer360/acceptance.json) defines the required verification suites and named cases. This document defines what their evidence must establish.

## Goal

**Build Oriel into a usable open-source Customer 360 agent for the customer tools a user already has.** A user can connect supported sources, land their output in DuckDB, bootstrap and review customer context, get a grounded brief and follow-up answers, refresh the data, and reuse the same reviewed definitions in another session.

The mandatory process is:

**Off-the-shelf connectors → DuckDB → context agents and reviewed definitions → grounded customer briefs and answers.**

Customer identity, context contracts, answer logic and the application must be independent of vendor names. Salesforce/Gong is one example. CRM, conversations, support, product usage, billing and internal customer sources are peer inputs. The available evidence determines which customer questions can be answered.

## Current evidence

The latest local acceptance run finished at **23:42 UTC on 9 October 2026** and passed **58 cases across all eight local gates**, with no skips and unchanged inputs. Its 413-file fingerprint is `f22e6a69af4f65f9af95be20507bbffe4b31ac658402224e6246be40faf810ec`; the retained report is `runs/a1e538640c7346ba9f2b01b7d85340f4.json` under `.oriel/verification/customer360/`. The source-pipeline gate includes six actual loopback REST/dlt scope cases, and both browser workflows and isolated installation pass. The focused connector/translation/provenance command separately passed 118 tests. **Model and live gates were not run in this revision's acceptance command; overall completion remains false.**

Run **g** passed **52 local and four actual-model cases** at **23:08 UTC on 9 October 2026**. Both synthetic profiles completed reviewed bootstrap and correct answers before and after refresh using the same context. Twelve calls reported **$1.4556692** in list-price estimates, with zero retries or unknown spend. **Live vendors: not run. Overall goal complete: no.**

Installation also passed from a clean detached checkout of local commit `d8836a6`, with the same runtime, package metadata and lockfile as run g. Publication and public-checkout reproduction remain pending. See [how verification runs](#how-verification-runs) for the commands and [retained verification history](#retained-verification-history) for immutable reports, fingerprints and earlier failures.

Subsequent implementation adds an opt-in [REST fetch guard](connectors.md#restrict-a-rest-fetch-graph): before-credential graph compilation, prepared-request checks, parent-inclusive page/row accounting and incomplete-extraction refusal. Its controlled-source tests are separate evidence. Run g and commit `d8836a6` remain unchanged historical results; new connector code does not inherit their complete runtime fingerprint. Vendor semantic restrictions and resolved-destination enforcement are still outstanding parts of the live collector.

Strict clean-Git installation of the newer guard candidate, commit `6f4b0c6`, passed at **23:48 UTC on 9 October 2026**. Both installed profiles completed initial and refreshed workflows, with zero model/vendor calls or dependency downloads. The [build status](build-status.md#reproduce-the-checks) records its exact commit, tree, package inventory and receipt hash. Public-checkout reproduction remains pending.

## Definition of done

The goal is complete only when all ten gates below pass for the tested revision, their evidence has been inspected, and the public-facing capability descriptions match the demonstrated scope. A green foundation test suite, a screenshot, a fixed SQL recipe or a plausible agent answer is insufficient.

**Public release verification is an additional required completion check.** After authorized publication, clone the intended public repository into a fresh directory, record the remote URL and exact commit, and run the documented Customer 360 quickstart and strict clean-Git installation check there. Inspect the resulting receipts against the published revision. The runner's `overall_passed` field covers the ten gates below; it does not prove publication or public-checkout reproduction. Even if that field becomes true, the persistent goal must remain active until this separate check passes. A local commit or working-tree installation cannot satisfy it.

| Gate | Observable outcome | Required evidence |
| --- | --- | --- |
| Source pipeline | Actual off-the-shelf runtimes extract selected data and publish it to DuckDB before context generation | Connector/version and selection receipts; table reconciliation; complete and failed-page cases; an answer with vendor access unavailable |
| Customer identity | Cross-source customer links are correct and ambiguous links remain unresolved | Independent expected mappings, collision/missing-link cases, stable workspace identity and no multiplied commercial totals |
| Context lifecycle | Context stages produce useful candidates for the operator's review question, reviewed definitions and reusable versions | Pinned intent when supplied, proposed and approved artifacts, source lineage, unresolved decisions, fresh-process reuse and dependency invalidation |
| Grounded answers | Numerical results, customer sets and material factual claims are supported | Independent result oracle; deterministic calculation scope separated from provisional descriptions; inspected records/text supporting each claim; rejected fabricated citations; explicit unknowns; snapshot/context/query receipts |
| Refresh and history | New facts change the brief while reviewed meaning remains stable | Before/after source and answer evidence; unchanged definition hashes; no duplicate history; stale/failed/schema-change/deletion behavior |
| Source portability | Materially different customer stacks use the same core workflow | Two profiles, one without CRM or transcripts; source addition/replacement using adapters and mappings; preserved identity and context |
| Access and actions | Customer evidence stays within its allowed scope and answering is read-only | Workspace isolation, consent and retrieval-bound checks, untrusted-text cases and refused write attempts |
| Product onboarding | A fresh user can complete the workflow through the actual interface | Clean-install execution, interface interactions, evidence drill-down, visible failures and a second review after refresh |
| Configured-model smoke | The real model can bootstrap context and answer correctly in the product | Actual model and tool traces for both profiles and changed data, independently checked facts/citations, real usage and declared spend limits |
| Authorized live connectors | Advertised connections work against selected real source resources | Separately authorized bounded runs, reconciliation to source-visible data, permission/coverage limitations, refresh receipts and an additional connector extension proof |

Three completion dimensions remain separate: **implementation verified**, **actual model path verified**, and **live integrations verified**. Overall completion requires all three. Controlled HTTP/database sources demonstrate connector runtime integration, not authenticated vendor compatibility. Scripted model responses demonstrate orchestration behavior, not agent success.

## Principles and constraints

- **Customer outcome first.** The first job is a useful customer review with inspectable evidence, followed by another review after the data changes. Connector count, model fluency and repository stars are not completion criteria.
- **One mandatory data path.** Existing extractors publish to DuckDB before context agents and answer tools run. There is no direct-source MCP fallback for missing local evidence.
- **Source-independent meaning.** Customer IDs, capabilities, definitions and answer contracts belong to Oriel's workspace, rather than a particular CRM schema. Source adapters and reviewed mappings bridge each tool.
- **Proposal is not approval.** Identity links, field mappings and business definitions need explicit review. Unknown or conflicting meaning stays visible. Refresh reuses unchanged reviewed definitions.
- **Start from the review question.** Bootstrap retains the operator's optional analytical intent with its proposal. The question guides candidate populations and filters; it cannot approve meaning or override reviewed units, customer scope or source permissions.
- **Evidence determines the answer.** SQL computes exact quantities with units and currencies; selected source passages support textual facts. Missing coverage narrows the answer. The current product does not accept unconstrained model-written factual prose.
- **Make calculation scope inspectable.** Show the operation, mapped role, typed filters, units, currency grouping and resolved customer population separately from model-written or operator-written descriptions. Those notes do not change execution or become independently verified facts through review.
- **Keep the local product useful.** One trusted operator, a private workspace and an existing configured model are sufficient. A registry account, hosted service, new front-end framework or source writeback is not a prerequisite.
- **State what was actually tested.** Synthetic fixtures, real extractor execution, scripted orchestration, actual inference and authorized vendor reads are distinct. Keep failures and spend; never pass a gate from a renamed fixture or a skipped check.

Initial constraints: retain the existing Python/DuckDB foundation and locked dependencies; use synthetic public examples; keep credentials outside forms and model inputs; obtain explicit source scope before live validation; keep answering read-only and browser access on loopback. Generic direct-key mappings are available; arbitrary nested relationship discovery, hosted/team access, scheduling and managed registry distribution require further work. MCP/Rystic comparisons stay deferred.

## Two source profiles, not one vendor-specific demo

Use source-shaped synthetic data with independently specified ground truth:

- **Profile A: customer directory/CRM plus conversations.** Accounts, people, commercial records, source references and dated customer statements. Salesforce/Gong can supply the first adapter example.
- **Profile B: billing plus support or product usage.** Customers, subscriptions/payments and cases or usage events. It contains no CRM and no transcript tables, and uses different field names and identifier structures from profile A.

Both profiles must run connect → sync → context → review → brief → ask → evidence → refresh through the same customer-facing contracts. Account summaries can differ with available capabilities. Profile B must not invent pipeline, renewal dates or call evidence.

Add a single-source case, then enrich it, then replace one source with an alternative adapter in controlled tests. Stable Oriel customer identity and reviewed definitions must survive. No hardcoded Salesforce account key, Gong call requirement or vendor branch in the answer logic is allowed.

Synthetic portability verification and live compatibility are separate gates. The live gate requires an authorized initial source combination and an additional real connector through the existing contract. Claim live support only for the exact resources and authentication paths actually tested.

## How verification runs

The [verification runner](../scripts/verify_customer360.py) reads the acceptance contract and runs suites in isolated processes. Use the project environment after the documented locked installation:

```sh
.venv/bin/python scripts/verify_customer360.py --timeout 300
```

By default it runs only local verification. It does not contact model providers or vendor accounts. Its output is a sanitized status report under `.oriel/verification/customer360/`, which is ignored by Git.

The browser gate requires an existing Chrome executable and Node 22 or newer. It creates isolated browser profiles and performs actual form interactions against fresh synthetic-only workspaces. The installation check uses the committed lockfile and an offline dependency cache; a missing prerequisite fails the gate rather than skipping it. Local verification never installs a new browser, invokes a configured model or grants source access. On another platform, supply an explicit existing Chrome path as documented by the browser checker; platform support requires its own successful run.

The default installation helper labels its input `isolated_working_tree_snapshot`: it copies allowlisted current files and does not claim Git provenance. After preparing an audited clean commit, run this separate check from that actual checkout using an existing approved helper environment with **setuptools 84.0.0** and the locked offline dependency cache. The private output directory must not already exist:

```sh
/path/to/approved-helper-env/bin/python scripts/check_customer360_install.py \
  --repository "$PWD" \
  --output "$PWD/.oriel/verification/customer360/clean-git-proof" \
  --require-clean-git
```

This opt-in mode pins a clean local commit/tree and verifies copied public files plus the helper/workflow against committed blobs, checking again at completion. Its `clean_git_commit_snapshot` receipt is separate from run g; it passed for local commit `d8836a6`. It does not fetch a repository or authenticate a remote: public-checkout proof additionally requires cloning the intended public revision and verifying its identity and reproducible behavior. Do not infer that proof from a dirty working-tree pass or the directory name `fresh-checkout`.

The configured-model suite requires a new private output path and explicit per-run consent. It freezes six jobs (bootstrap, initial answer and refreshed answer for each sample stack), each with a $0.50 requested limit and four model turns, before dispatch. Each bootstrap receives its profile's predeclared review question as intent. Browser requests, private request hashes and both proposal stages must match that frozen intent. The shared controller durably reserves $0.50 before each job under the frozen $3 cap, uses the application's existing configured Claude CLI without an injected client, and stops after any failed review, unknown cost or exceeded limit. Provider-reported costs are estimates, not an invoice guarantee. This sends synthetic records to the configured model; it makes no live vendor calls.

```sh
ORIEL_C360_CONFIGURED_MODEL=1 \
ORIEL_C360_MODEL_RUN_DIR="$PWD/.oriel/verification/customer360/configured-model-$(date -u +%Y%m%d-%H%M%S)" \
.venv/bin/python scripts/verify_customer360.py --include model --timeout 2400
```

Run that command only when the small paid run is intended; reusing an existing output directory fails. Ordinary local verification never invokes it. Independent checks review the exact generated definitions before the browser approves them, then verify currency totals, source-record populations and substantive current quotes before the next paid job. Unknown extra definitions stop for review rather than being silently approved. Every failure and known charge remains in the private ledger.

The `--include live` selector remains unmet until a separately authorized collector and its acceptance suite can produce real source receipts. The [live-validation contract](customer-360-live-validation.md) describes the required scope, enforcement evidence and independent reconciliation; its offline checker cannot certify live execution. Verification flags do not provision credentials, authorize an account, publish data or replace product consent checks. A model smoke is a small functional product check, not the deferred comparative benchmark.

The runner must fail closed:

- All ten charter gates, their execution modes, suite paths and baseline case inventory must remain present. A shortened or redirected contract can never mark the goal verified. A mode change cannot turn a model or live suite into a default local run.
- Missing suites are **unimplemented**; unselected model/live suites are **not run**.
- Every named acceptance case must execute successfully. Zero discovered tests, absent cases, skips, expected failures, import failures and timeouts cannot pass a gate.
- No previous report is silently reused as evidence for a new code revision or run.
- The verifier's sanitized status report records the tested code and contract identities without customer rows or transcript text. Detailed model-run summaries, traces and browser artifacts remain private evidence and can contain selected source passages; their filename does not make them safe to publish. No artifact may contain credentials.
- The overall exit status is successful only if every required gate passes. An unrun live gate keeps overall completion false even when all local tests pass.

Runner unit tests check these reporting rules. They are not Customer 360 acceptance evidence and must never be counted toward product completion.

## Oracles and evidence review

Write expected answers before implementing the feature being checked. Use simple independently reviewed calculations and explicit source-to-claim mappings; do not generate expected results with the same SQL, mapping or model that produced the answer.

Keep expected-answer files outside the model's tool-visible data. Provide ordinary source records and approved business definitions to the agent. Verify customer membership and numeric values exactly, including currency, units, time windows and join grain. For each material narrative claim, check that the actual cited passage supports the claim and its attribution, rather than checking only that a citation ID exists.

The real-model smoke should be small and budgeted across context bootstrap, answers and retries. Declare the model, task set and maximum spend before execution; stop on exhausted or unknown spend. Retain failures. Do not rerun until one attractive answer appears or use an LLM's self-rating as the sole correctness judgment.

Each acceptance run should retain locally:

- Code, connector, fixture/snapshot and context versions or hashes, with run time and scope.
- Selected source capabilities, resource coverage, source observation times and exclusions.
- Executed queries, result hashes and evidence references for accepted answers.
- Actual model/tool traces and usage for the configured-model gate, with access-controlled private evidence stored separately from public summaries.
- Exact failed, skipped, blocked or unrun cases and the reason they remain unmet.

The agent reviews the receipts and independent oracle alongside the machine report before marking the persistent goal complete. Passing a command does not excuse a broken assertion, weakened criterion or unsupported product claim. Change acceptance criteria only when product scope changes explicitly; record that change instead of deleting a difficult gate.

## Retained verification history

Implementation progress: the [local foundation](customer-360-foundation.md) provides customer identity, content-verified connector snapshots and reviewed field mappings. The [customer CLI](customer-360-cli.md) adds versioned business definitions, scoped calculations, exact evidence quotations, saved-review comparisons and model orchestration. The [local application](customer-360-app.md) exposes these through separate setup approvals, customer briefs, questions, evidence and refreshed comparisons. Actual Chrome workflows now exercise both sample profiles using a visibly labeled scripted transport. Those runs verify the interface and orchestration; they do not establish model reasoning or live-vendor compatibility. The acceptance runner remains the authority for the current tested revision.

The latest configured-model acceptance run, **g**, completed at **23:08 UTC on 9 October 2026**. All eight local gates passed (**52 cases**) and the configured-model gate passed (**four cases**), with no skips and unchanged recorded inputs. Both synthetic profiles completed actual browser onboarding, task-guided bootstrap, independent definition review, and independently checked answers before and after refresh using the same reviewed context. Six reviewed jobs used **12 Claude Opus 5.5 calls**, reporting **$1.4556692** under the predeclared $3 cap, with **zero retries and no unknown spend**. Costs are provider-reported list-price estimates, not invoice charges. **Implementation verified for the tested revision: yes. Configured-model gate: passed. Live-vendor gate: not run. Overall goal complete: no.**

The immutable report is `runs/ebeb804057274cde8caad4adab8957e6.json` under `.oriel/verification/customer360/`; its 408-file input fingerprint is `f5990ad73822993f5a4dd53c31dae94b5fcc1a2e85b3b40b1a214256f5cd02a2`. Model receipts are in `configured-model-product-20261010-g` (the directory date is local; the timestamps above are UTC). The independent checks cover exact currency totals, substantive attributed passages, source-time lineage and complete latest selections for both phases. This bounded synthetic run does not establish general accuracy, comparative MCP performance or authenticated vendor compatibility.

Run g's installation case copied the current working tree into fresh isolated environments, built a wheel and ran it outside the source directory. Its report records dirty Git HEAD `36583b44a5612e1a530419e63b1c5539dcb2bca4`; its fingerprint pins the tested files, not a committed or published Customer 360 revision. Later documentation or verification-helper edits do not alter run g's retained evidence.

A separate clean-Git installation passed at **23:19 UTC on 9 October 2026** from a detached checkout of **`d8836a6ed8d0a34981b07dc7b87ca7ee08880e3f`**, tree `9bafe481c64cc04437d1fffd3ae1f80073c37335`. The private receipt is `clean-git-install-d8836a6ed8d0/receipt.json` under `.oriel/verification/customer360/`, SHA-256 `6de251e91365411c055efc4cf1203d626040bba70c155696094d611798a5f34e`. It verifies 313 copied public files plus the executing helper and installed-workflow helper against committed blobs, with clean unchanged HEAD/tree at completion. The wheel includes all 308 package files, three UI assets, 25 sample JSON files and the license. Both profiles pass initial/refreshed installed application workflows in isolated Python, with no editable install or source checkout on the import path. No model, vendor or dependency-download calls occurred. All 310 captured runtime, package-metadata and lockfile hashes match run g's installation receipt. These are complementary checks: the configured-model evidence remains run g, while committed-source installation is proved by this separate run. **Publication and public-repository reproduction remain pending.**

The preceding run, **f**, completed at **22:57 UTC on 9 October 2026**. All eight local gates passed (52 cases), with no skips and unchanged recorded inputs. The billing/support model bootstrap received the operator's exact review question, proposed the required open-invoice metric and passed independent definition review. Its first answer job computed the correct currency-separated amounts, then requested the nonexistent evidence kind `support_update` instead of the mapped kind `support`. Tool validation refused the answer. Three Claude Opus 5.5 calls reported **$0.34438** under the predeclared $3 cap, with no unknown costs or paid retry. Refresh and the CRM profile were not reached. Its configured-model gate failed during setup; its four named cases did not execute.

The retained report is `runs/1b4154cff9b54bb9bd7bb35186d52ec1.json` under `.oriel/verification/customer360/`; its 408-file fingerprint is `09e932f77f05a5a03bd82c7fa7355ef3b3479622c6edcbfd18150dbb3eda7ad7`. Model receipts are in `configured-model-product-20261009-f`. The browser harness waited for a success condition after the failed job; a separately recorded intervention closed only its owned isolated Chrome process. Its final receipt confirms process exit and profile cleanup, with no console errors or blocked requests. This intervention is failure cleanup, not a passed browser check. Costs are provider-reported list-price estimates, not invoice charges. Subsequent code and reporting edits do not alter the retained run's evidence.

Follow-up offline changes expose the exact evidence-kind enum and reviewed passage kinds in the model's tool contract, keep invented aliases invalid, and make the browser harness detect a terminal refusal before waiting for a success screen. All 34 agent tests and four fresh product tests passed after these changes. Both browser profiles detected a real missing-file failure despite an impossible success predicate in about 1.1 seconds; the product suite also completed onboarding, refresh and isolated working-tree installation with no provider calls. Its receipts are in `browser-suite-072247804bdc431e86a5d31f75ed3219`. Run g subsequently supplied fresh actual-model evidence; run f was never resumed or relabeled as passing.

The preceding run at **22:31 UTC** also passed 52 local cases, but its billing/support bootstrap proposed an all-invoice sum instead of the requested open-invoice metric. Independent review stopped the unapproved candidate with `required_currency_sum_missing`. Two calls reported **$0.226568**. Its report `runs/ef459f5d4b6a4c138e1675a2fe64e061.json`, 406-file fingerprint `3ed14bf2ad7594b05308be12403c45dda0a9c3f37b1eacb435f771ff7d5e88ec`, and `configured-model-product-20261009-e` receipts remain unchanged.

Inspection also found a provisional definition description claiming that conflicting/unresolved records were included, although customer metric execution excludes them. The automated definition oracle checks structured operation, unit and filters; it does not certify freeform descriptions. The candidate was already refused for its missing open-invoice metric. Follow-up implementation carries the operator's optional analytical question through the CLI, application, both context stages and the sealed proposal. Typed calculation scope is displayed separately from provisional descriptions; historical approvals remain unchanged. Run g verifies these contracts for the two synthetic profiles. The configured smoke freezes the same ordinary question for bootstrap and answering; expected answers and oracle-authored filters are not model inputs.

The preceding run at **22:16 UTC** passed 52 local cases and billing/support model checks through refresh, then refused the CRM proposal's currency-filtered scalar sums. It reported **$1.1153332** across nine calls. Its report `runs/8f636b433d054e4fb443800b9dcf8f19.json`, 404-file fingerprint `d39a80d71ae9e224a4d77712653b65ac9f575969d0396869c764d108e17a6c7c`, and `configured-model-product-20261009-d` receipts remain unchanged. That failure motivated explicit reviewed measure policies; neither failed run is a model-gate pass.

An earlier acceptance run, **c**, completed at **21:46 UTC on 9 October 2026**. All eight local gates passed (52 cases), and the configured-model gate passed (four cases), with no skips and unchanged recorded inputs. Both source profiles completed actual browser onboarding, model context bootstrap, independently reviewed answers, refresh and new answers using unchanged definitions. Twelve Claude Opus 5.5 calls reported **$1.2906632** under the predeclared $3 cap, with no unknown costs. That result remains evidence for its recorded revision; it is not merged with later runs or used to erase their failures.

That earlier private report is `runs/8908315adc634005989e4ee7a195754f.json` under `.oriel/verification/customer360/`; its 398-file input fingerprint is `a71956dd13170ef78a6533af73fa5f73c80469724594c06216a3e36a0493b3b7`. Model receipts are retained in `configured-model-product-20261009-c`. The independent checker confirmed exact currency totals, substantive source text, source-time lineage and complete latest selections before and after refresh. Browser screenshots and cleanup receipts were inspected. This proves a bounded product workflow over synthetic source-shaped data extracted with the actual local SQLite/dlt runtime; it is not an MCP comparison or authenticated SaaS validation. Relevant changes require fresh verification.

Documentation status text, including the README evidence row, was updated after this run to report its outcome. The receipt retains the original tested fingerprint; these reporting edits made no runtime, test, fixture or context-contract changes and did not trigger additional inference.

Subsequent live-validation preparation added a separate private-file bundle inspector. Forty offline contract, file-integrity and CLI checks pass on fictional local evidence, with no vendor or model calls. The inspector pins separately selected capability files and all retained proof bytes; it cannot authenticate their origin or pass the live gate. The collector still needs a selected authorized source scope and demonstrated controls for every fetched resource, including parent dependencies. See the [live-validation guide](customer-360-live-validation.md). CLI setup examples now explicitly use the application's workspace and include a complete metadata-attachment mapping.

The current candidate also exposes declaration-derived connector fetch plans in the CLI and application, including unselected parent resources; unknown runtime scope stays explicit. Empty resource selections and selections changed while saving are refused. Context-review notes retain their approval snapshot after refresh, separately from computed coverage for the current answer snapshot. These behaviors passed the latest local run. Reviewed field mappings now carry optional measure units, currency roles, sum permission and meaning. New model sums must match compatible confirmed policies across every contributing binding; unknown, missing or conflicting policies cannot be converted into a guessed sum. Existing approvals remain readable. Focused tests cover policy changes, major/minor units, new currencies after refresh, preserved raw values and policy-bypass refusals.

### Retained earlier attempts

An initial billing/support probe reported $0.670200 across six calls under a $1 cap, including a failed bootstrap before a prompt-contract correction. It did not cover both profiles, refresh or the browser flow. The next actual-browser run reported $0.235828 across two calls and stopped because additional generated definitions were outside the checker's reviewed semantic inventory. An offline extension independently checked those meanings against raw records; it did not resume or retroactively pass the stopped run. A setup refusal made no provider calls.

A subsequent run reported $0.401092 across four calls. It passed context review and computed the correct initial billing amounts, but quoted an empty updates envelope instead of substantive support text. Independent review stopped it before refresh or the second profile. Follow-up inspection also found missing call event timestamps. Both failed runs remain retained with their original outcomes and costs.

Bounded field inspections now distinguish source text from structural envelopes. Reviewed passage mappings add explicit source timestamps, optional metadata joins and exact decoded text-leaf citations. Latest selection scans the complete attributed customer projection, retaining uncertainty and ties. A mandatory local acceptance case and the fresh configured-model checks verify this path for both profiles across initial and refreshed data. The independent substantive-text criteria remain in force; the oracle additionally checks source times and lineage. The interface describes its checks as evidence checks, without implying full question coverage or complete vendor history.

## Working loop

1. Implement the next bounded product milestone from the build plan.
2. Add its independent acceptance cases and run the relevant local gate.
3. Fix functional failures and retain the resulting receipts.
4. Re-run affected gates when code, schema, mappings or context contracts change; avoid paid external checks unless that boundary needs fresh evidence.
5. Run the full acceptance command before declaring the build complete and inspect the actual interface from a fresh workspace.
6. Report implementation, model and live-vendor status separately. If external access is missing, continue independent implementation and describe the remaining live gate precisely.

Comparative MCP/Rystic evaluations remain deferred. This goal establishes a useful Customer 360 product and a defensible verification path; it does not establish an accuracy, cost or speed advantage over another architecture.
