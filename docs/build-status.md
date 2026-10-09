# What works today

Oriel is a **Customer 360 developer preview** for one trusted local operator. This page records observed scope as of 10 October 2026. Start with the [application quickstart](quickstart.md). Authored sample definitions let you inspect a brief without a model or source credentials; context-agent bootstrap and questions require model consent.

## Customer 360 evidence

| Capability | Observed behavior | Boundary |
| --- | --- | --- |
| Customer application | Both synthetic source profiles complete real Chrome interactions: connect, review, materialize, context bootstrap/review, brief, question, evidence, save and refresh comparison | Scripted orchestration and actual configured-model browser runs are recorded separately. Neither establishes live-vendor compatibility. |
| Connector extraction | Actual dlt SQLite extraction for both profiles; additional controlled local REST/GraphQL tests cover resource selection and failed pages | No authenticated SaaS calls are implied by vendor-shaped fixtures. |
| Connector selection preview | CLI and application distinguish selected outputs from declared parent fetches; empty selections and changes during a pending save are refused | Static declarations expose dependencies; they do not establish actual runtime scope, permissions or live compatibility. |
| API sync limits | Optional REST/GraphQL request-attempt and elapsed-time budgets span resources and pages; failed or interrupted extraction preserves the last published data | These limits require explicit selection. They do not establish field, history, customer-record, page or row authorization. Full attempt evidence stays private. |
| Customer identity | Workspace identities, explicit reviewed cross-source links, collision and missing-link handling | Names and domains do not silently merge customers. Complex relationship chains may require an adapter. |
| Business context | Bounded source inspection, an optional operator question retained in both proposal stages, exact-hash reviews, versioning and fresh-process reuse. Mapping review records measure units, currency roles and sum permission; new model sums must match every contributing binding | A question guides candidate definitions but does not approve them. Unknown or conflicting policies block sums. Local reviewer labels are not authenticated governance. |
| Grounded answers | Exact currency-separated calculations, scoped source passages, fixed abstentions and snapshot/context receipts. The CLI and application expose calculation scope from typed definitions separately from provisional notes | Freeform descriptions are not certified by structural metric checks. General narrative synthesis, health predictions and autonomous actions remain outside the implemented answer contract. |
| Evidence inspection | Bounded previews guide inspection; reviewed passage mappings attach exact source times and decoded text leaves. Latest selection checks all attributed records in a pinned local snapshot, retaining ties and source lineage | Missing dates, incomplete traversal and truncated ties prevent latest claims. Local coverage does not establish full vendor history or question relevance. |
| Refresh and history | Retained briefs and source records, changed-fact comparisons, unchanged-definition reuse and invalidation on mapping/meaning changes. Retained review notes identify their approval snapshot and are separated from current coverage checks | Retained notes are not automatically rechecked after refresh. Identity changes can require withholding numeric deltas. No scheduler or automatic notification service is included. |
| Configured model | Both synthetic profiles completed task-guided bootstrap, independent definition review and correct answers before and after refresh using the same context. Six reviewed jobs used twelve Claude Opus 5.5 calls reporting $1.4556692, with zero retries and no unknown spend | Provider-reported list-price estimates, not invoice charges. This bounded product check does not establish general accuracy, comparative performance or vendor compatibility. Earlier failures remain retained. |
| Application boundary | Numeric-loopback server, origin/session/CSRF checks, private workspaces, serialized idempotent jobs and interrupted-run handling | One local operator only; this is not hosted authentication or isolation from other local processes. |
| Installation | An allowlisted copy of the current working tree builds in fresh locked environments; the installed wheel runs outside its source directory with UI assets and synthetic data packaged | This is platform-specific isolated-install evidence, not a clean committed Git or public-checkout proof. Both checkout checks remain pending. |
| Live integrations | Catalog and readiness inspection are available | Selected authorized vendor authentication, resource reconciliation and refresh remain unverified. |

The [goal and acceptance contract](customer-360-goal.md) separate **implementation**, **actual model execution** and **live integrations**. Run **g**, completed at **23:08 UTC on 9 October**, passed **52 local and four actual-model cases** with no skips and unchanged recorded inputs. It includes actual form-based intent and calculation-scope review, both customer stacks and both source phases. **Live validation remains `not_run`, so overall completion is false.** The local suite also covers source addition and replacement through shared contracts.

The retained report is `runs/ebeb804057274cde8caad4adab8957e6.json` under `.oriel/verification/customer360/`, with 408-file fingerprint `f5990ad73822993f5a4dd53c31dae94b5fcc1a2e85b3b40b1a214256f5cd02a2`. Actual-model receipts are in `configured-model-product-20261010-g`. Runs f, e and d remain failed at their original boundaries, and the earlier c pass remains revision-specific; their costs and evidence are preserved in the [verification history](customer-360-goal.md). Run g is a fresh run, not a resumed or relabeled failure.

## Reproduce the checks

Install the committed connector dependencies, then run:

```sh
.venv/bin/python scripts/verify_customer360.py --timeout 300
```

This runs local gates only. The browser gate needs Node 22+ and an existing Chrome executable; the clean-install gate needs the locked dependencies in uv's offline cache. Missing prerequisites, skipped tests and missing suites fail verification rather than being counted as success. Reports and private receipts are retained under `.oriel/verification/customer360/`.

The default install helper records an isolated working-tree snapshot. A separate `--require-clean-git` mode checks a clean commit and its public files against Git blobs, using the existing approved setuptools 84.0.0 helper environment; see the [exact command and limits](customer-360-goal.md#how-verification-runs). No successful clean-Git or public-clone receipt is claimed yet. The local mode does not fetch or prove remote publication.

The command intentionally returns an incomplete overall result while required model or live gates remain unrun. Model and live flags select separately authorized checks; they do not grant access or provision credentials. See [verification instructions](customer-360-goal.md#how-verification-runs).

## Existing advanced workflows

Local CSV/JSON/JSONL/Parquet imports, SQL, reviewed metric versions, read-only MCP stdio, local transformations and portable context bundles remain available. The [CRM/product recipe](tool-exports.md) demonstrates a separate consumer reusing a definition on changed data. These are useful independent features, not evidence of authenticated vendor compatibility or managed registry distribution.

The catalog includes 101 source definitions. Specific driver, resource and authentication combinations need their own checks. The [connector guide](connectors.md#what-has-been-verified) and [native verification record](../contextbase/connectors/native-verification.json) describe earlier runtime coverage. Gong is not currently a live catalog integration.

## Remaining release evidence

1. Prepare and verify the exact audited committed candidate, then reproduce its documented experience from the intended public checkout. Current installation evidence includes uncommitted working-tree files; it cannot establish that the public repository contains this product revision.
2. Run separately authorized live source combinations and an additional connector, recording exact resources, permissions, coverage and refresh behavior.
3. Observe technical users completing an own-data customer review, then returning to reuse their context.
4. Validate a receiving registry and consumer access when an actual sharing workflow requires them.

Comparative MCP/Rystic evaluations remain deferred. There is no qualifying accuracy, cost or latency advantage claim. [Product plan](customer-360-plan.md) · [Roadmap](roadmap.md) · [Evidence policy](gtm.md#scorecard-and-evidence-policy).
