# Bring your existing tool exports

This entirely synthetic example joins CRM seat entitlements to product activation events. It uses the ordinary import/query/transform/context CLI and different schemas from the benchmark fixture. No simulator, account, model or network access is required.

The question is: **Which active external customers activated fewer than half their licensed seats, and how many seats remain unused at those customers?** The period is 24 September through 7 October 2026 UTC. The independent Python-set oracle returns **51 seats across five organizations**, then **8 seats at one organization** after new activation events are imported.

From the repository root, run:

```sh
uv run --frozen python examples/tool-exports/verify.py \
  --output benchmark-runs/tool-exports-verification
```

Use a new or empty output directory. The verifier creates only local workspaces, an offline context bundle and `receipt.json`; it leaves the checked-in exports unchanged. It executes the synthetic review decisions automatically for repeatable testing. A person adapting this example to their data must review their own mappings, SQL and rules before approval.

The [full walkthrough](../../docs/tool-exports.md) explains every command. The [recorded proof](functional-proof.json) records one actual run, all command arguments and checksums. Approval hashes and context versions change between runs because they include review identities/timestamps and local plan IDs. Answers, file hashes and checks are reproducible.

| Artifact | Purpose |
| --- | --- |
| `fixtures/crm-seats.csv` | CRM organization IDs and contract seats |
| `fixtures/product-events-*.jsonl` | Initial and refreshed complete-period product exports |
| `fixtures/workspace-map-unresolved.csv` | Explicit cross-tool mapping with an ambiguous link and a missing enabled link |
| `fixtures/workspace-map-reviewed.csv` | Same mapping after the two declared review decisions |
| `sql/mapping-issues.sql` | Explain identity problems before computing an answer |
| `sql/activation-accounts.sql` | Account-level answer, deduplicated across product workspaces |
| `sql/unused-seats.sql` | Guarded scalar metric proposed for review |
| `oracle.py` | Independent expected answer using Python sets; no SQL or DuckDB |
| `reuse.py` | New process and workspace consuming the exported metric without receiving its business policy again |
| `verify.py` | CLI workflow and functional receipts |

This is functional evidence of import and context portability. It does not measure natural-language task accuracy, model cost, latency, autonomous context discovery, real Registry import, or user adoption. Generic transforms are materialized snapshots; the metric here queries current imported source tables on every run.
