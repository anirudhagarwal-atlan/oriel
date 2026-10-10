# What works today

Oriel is a **Customer 360 developer preview** for one trusted local operator. This page records observed scope as of 10 October 2026. Start with the [application quickstart](quickstart.md). Authored sample definitions let you inspect a brief without a model or source credentials; context-agent bootstrap and questions require model consent.

## Customer 360 evidence

| Capability | Observed behavior | Boundary |
| --- | --- | --- |
| Customer application | Both synthetic source profiles complete real Chrome interactions: connect, review, materialize, context bootstrap/review, brief, question, evidence, save and refresh comparison | Scripted orchestration and actual configured-model browser runs are recorded separately. Neither establishes live-vendor compatibility. |
| Connector extraction | Actual dlt SQLite extraction for both profiles; additional controlled local REST/GraphQL tests cover resource selection and failed pages | No authenticated SaaS calls are implied by vendor-shaped fixtures. |
| Connector selection preview | CLI and application distinguish selected outputs from declared parent fetches; empty selections and changes during a pending save are refused | Static declarations expose dependencies; they do not establish actual runtime scope, permissions or live compatibility. |
| API sync limits | Optional REST/GraphQL request-attempt and elapsed-time budgets span resources and pages; failed or interrupted extraction preserves the last published data | These limits require explicit selection. They do not establish field, history, customer-record, page or row authorization. Full attempt evidence stays private. |
| REST fetch guard | Optional compiled origin/resource scope checks prepared request shapes, counts pages/rows including unselected parents, and requires complete child chains before publication | Controlled local HTTP/dlt evidence only. Vendor permissions, filter semantics, read-only operation and resolved destinations remain unverified; the browser form does not yet expose this additional mode. |
| Customer identity | Workspace identities, explicit reviewed cross-source links, collision and missing-link handling | Names and domains do not silently merge customers. Complex relationship chains may require an adapter. |
| Business context | Bounded source inspection, an optional operator question retained in both proposal stages, exact-hash reviews, versioning and fresh-process reuse. Mapping review records measure units, currency roles and sum permission; new model sums must match every contributing binding | A question guides candidate definitions but does not approve them. Unknown or conflicting policies block sums. Local reviewer labels are not authenticated governance. |
| Grounded answers | Exact currency-separated calculations, scoped source passages, fixed abstentions and snapshot/context receipts. The CLI and application expose calculation scope from typed definitions separately from provisional notes | Freeform descriptions are not certified by structural metric checks. General narrative synthesis, health predictions and autonomous actions remain outside the implemented answer contract. |
| Evidence inspection | Bounded previews guide inspection; reviewed passage mappings attach exact source times and decoded text leaves. Latest selection checks all attributed records in a pinned local snapshot, retaining ties and source lineage | Missing dates, incomplete traversal and truncated ties prevent latest claims. Local coverage does not establish full vendor history or question relevance. |
| Refresh and history | Retained briefs and source records, changed-fact comparisons, unchanged-definition reuse and invalidation on mapping/meaning changes. Retained review notes identify their approval snapshot and are separated from current coverage checks | Retained notes are not automatically rechecked after refresh. Identity changes can require withholding numeric deltas. No scheduler or automatic notification service is included. |
| Configured model | Both synthetic profiles completed task-guided bootstrap, independent definition review and correct answers before and after refresh using the same context. Six reviewed jobs used twelve Claude Opus 5.5 calls reporting $1.4307704, with zero retries and no unknown spend | Provider-reported list-price estimates, not invoice charges. This bounded product check does not establish general accuracy, comparative performance or vendor compatibility. Earlier failures remain retained. |
| Application boundary | Numeric-loopback server, origin/session/CSRF checks, private workspaces, serialized idempotent jobs and interrupted-run handling | One local operator only; this is not hosted authentication or isolation from other local processes. |
| Installation | Clean local commit `6f4b0c6` passes committed-blob verification, with all 316 installation inputs matching candidate `eaec132`; its installed wheel runs both profiles outside the source directory with UI assets and synthetic data packaged | Verified on macOS ARM64/Python 3.12 with locked cached dependencies. Publication and public-checkout reproduction remain pending. |
| Live integrations | Catalog and readiness inspection are available | Selected authorized vendor authentication, resource reconciliation and refresh remain unverified. |

The [goal and acceptance contract](customer-360-goal.md) separate **implementation**, **actual model execution** and **live integrations**. The latest acceptance run tested clean candidate **`eaec13244d0d4167f5507f1aa73d6b1607586111`** from **23:51:38 to 23:57:43 UTC on 9 October 2026**. It passed **58 local and four actual-model cases**, with no skips and unchanged recorded inputs. It includes actual form-based intent and calculation-scope review, both customer stacks and both source phases. **Live validation remains `not_run`, so overall completion is false.** The local suite also covers source addition and replacement through shared contracts.

The retained report is `runs/84d6e08bb5ac4c008dd08db2d7e320bb.json` under `.oriel/verification/customer360/`, with 413-file fingerprint `f22e6a69af4f65f9af95be20507bbffe4b31ac658402224e6246be40faf810ec`. Actual-model receipts are in `configured-model-release-eaec132`; its summary has SHA-256 `e38fe316b73bfd07b052b656ae684ad92180a76ed10238eb665c38586d4e24db`. Both profiles reused the same reviewed context against new snapshots. Runs f, e and d remain failed at their original boundaries, and the earlier c and g passes remain revision-specific; their costs and evidence are preserved in the [verification history](customer-360-goal.md#retained-verification-history).

The REST fetch guard's focused connector/translation/provenance command passed **118 tests**. Its six native scope cases are included in the **58 local cases** above, alongside both browser workflows and isolated installation. The earlier local-only report `runs/a1e538640c7346ba9f2b01b7d85340f4.json` passed the same local case inventory at **23:42 UTC on 9 October 2026**, with the same 413-file fingerprint; its model and live gates remain unrun in that historical report. The newer candidate acceptance supplies fresh model evidence for this connector runtime. See [scope controls and limits](connectors.md#restrict-a-rest-fetch-graph).

## Reproduce the checks

Install the committed connector dependencies, then run:

```sh
.venv/bin/python scripts/verify_customer360.py --timeout 300
```

This runs local gates only. The browser gate needs Node 22+ and an existing Chrome executable; the clean-install gate needs the locked dependencies in uv's offline cache. Missing prerequisites, skipped tests and missing suites fail verification rather than being counted as success. Reports and private receipts are retained under `.oriel/verification/customer360/`.

The default install helper records an isolated working-tree snapshot. A separate `--require-clean-git` run passed at **23:19 UTC on 9 October 2026** from a detached checkout of `d8836a6ed8d0a34981b07dc7b87ca7ee08880e3f`. It reconciled 313 copied public files plus its helper and workflow against committed blobs; the wheel contained all 308 package files, three UI assets and 25 sample JSON files. Both installed profiles passed before and after refresh with no model/vendor calls or dependency downloads. All 310 captured runtime, package-metadata and lockfile hashes match run g. See the [exact command and limits](customer-360-goal.md#how-verification-runs). This is local Git provenance, not remote publication proof.

The command intentionally returns an incomplete overall result while required model or live gates remain unrun. Model and live flags select separately authorized checks; they do not grant access or provision credentials. See [verification instructions](customer-360-goal.md#how-verification-runs).

The newer connector candidate also passed strict clean-Git installation at **23:48 UTC on 9 October 2026**, from commit `6f4b0c664b00b57c59dd6494581c3cd7cbbf10c2` (tree `8fc2e13a35a16967e858c6a71dc8f1dabcfe6048`). The checker reconciled 316 committed inputs, verified all 309 package files, three UI assets and 25 sample JSON files, then ran both installed profiles before and after refresh. It used no model calls, vendor calls or dependency downloads. The private receipt is `clean-git-install-6f4b0c6/receipt.json`, SHA256 `470ac2ed4de133dd529ce009f2de2e72a78dc616f290b61904fe9f72bd3f3be7`, under `.oriel/verification/customer360/`. All 316 captured installation inputs match candidate `eaec13244d0d4167f5507f1aa73d6b1607586111`. This proves local committed-source installation; the candidate model result above is separate evidence. Publication approval and public-checkout reproduction remain pending.

## Existing advanced workflows

Local CSV/JSON/JSONL/Parquet imports, SQL, reviewed metric versions, read-only MCP stdio, local transformations and portable context bundles remain available. The [CRM/product recipe](tool-exports.md) demonstrates a separate consumer reusing a definition on changed data. These are useful independent features, not evidence of authenticated vendor compatibility or managed registry distribution.

The catalog includes 101 source definitions. Specific driver, resource and authentication combinations need their own checks. The [connector guide](connectors.md#what-has-been-verified) and [native verification record](../contextbase/connectors/native-verification.json) describe earlier runtime coverage. Gong is not currently a live catalog integration.

## Remaining release evidence

1. Publish the audited committed candidate and reproduce its documented experience from the intended public checkout. Local committed-source installation has passed; it cannot establish that the public repository contains this product revision.
2. Run separately authorized live source combinations and an additional connector, recording exact resources, permissions, coverage and refresh behavior.
3. Observe technical users completing an own-data customer review, then returning to reuse their context.
4. Validate a receiving registry and consumer access when an actual sharing workflow requires them.

Comparative MCP/Rystic evaluations remain deferred. There is no qualifying accuracy, cost or latency advantage claim. [Product plan](customer-360-plan.md) · [Roadmap](roadmap.md) · [Evidence policy](gtm.md#scorecard-and-evidence-policy).
