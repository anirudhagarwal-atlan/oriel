# Oriel launch kit

Draft assets for the Customer 360 developer preview. The selected repository is [anirudhagarwal-atlan/oriel](https://github.com/anirudhagarwal-atlan/oriel). These drafts have not been posted. Local implementation changes are not automatically present on GitHub; inspect and publish the exact release candidate separately. [GTM strategy](gtm.md) · [Release evidence](customer-360-goal.md).

## Before publication

- Reproduce the [quickstart](quickstart.md) and complete the current candidate's required acceptance gates. Keep scripted orchestration, configured inference and authenticated vendor evidence separate.
- Verify the exact documented source installation, UI assets, links, package contents and third-party licenses. Do not advertise a PyPI package or hosted documentation until separately published and checked.
- Inspect public artifacts and history. Keep private workspaces, credentials, model traces and customer records out of the release.
- Assign issue triage and establish a real private security-reporting route before inviting reports; do not invent a support commitment.
- Record the actual demo. The talk track below is not an existing recording. Keep synthetic and authored/scripted/model labels visible.

## Repository metadata

**Tagline:** Customer 360 for the tools you already use.

**GitHub About:** An open-source Customer 360 agent. Connect customer tools to DuckDB, review reusable context, and get briefs and answers with evidence.

**Suggested topics:** `customer-360`, `customer-success`, `revops`, `duckdb`, `ai-agents`, `data-connectors`, `context-engineering`, `open-source`.

**One-sentence description:** Oriel brings supported customer data into DuckDB, bootstraps reviewed business context, and helps account owners inspect what is happening and what changed.

## Repository announcement draft

Your customer story lives across billing, support, product usage, CRM and conversations. Preparing the next account review means joining those records and agreeing on what they mean.

Oriel is an open-source Customer 360 agent for that work. Off-the-shelf connectors load data into DuckDB. Context agents propose business definitions for review. Customer briefs and answers retain the calculations, source passages and context version behind each fact.

The local preview includes two synthetic stacks—billing plus support, and CRM plus conversations—in the same interface. Review the customer links, inspect a brief, save it, refresh the data, and compare what changed. No particular CRM or conversation tool is required.

Source coverage and missing evidence stay visible. This is a developer preview for one local operator; authenticated source coverage and configured-model evidence must be described using the release candidate's [current status](build-status.md). No comparative accuracy, cost or speed claim is part of this launch.

[Try a customer review](quickstart.md), then connect sources you are authorized to use. Tell us which review step was useful and where setup or business meaning needed help, using a synthetic reproduction in an issue.

## Short social post draft

A useful customer brief should survive the next data refresh.

Oriel connects supported customer tools to DuckDB, bootstraps business context for review, and keeps the evidence behind the account brief. Save a review, refresh, and see what changed using the same definitions.

Try the local Customer 360 preview with either billing/support or CRM/conversation samples, then bring a customer-review workflow from your own stack.

Attach the actual repository link and a verified recording. Label synthetic data and any scripted or authored steps. The recording must not imply a model or vendor connection that did not run.

## Technical community post draft

**Title:** An open-source Customer 360 agent with DuckDB and reviewed context

The required execution path is off-the-shelf extraction → committed DuckDB snapshots → context agents and reviewed definitions → customer briefs and answers. Customer identity and capability mappings are independent of source brands.

The local application separates customer-link, field-mapping and definition approvals. Its answer contract recomputes metrics and validates exact source passages. Saved reviews retain the original snapshot and context; refresh can change facts without changing the reviewed definition. Missing coverage and changed comparison basis remain visible.

Two contrasting synthetic stacks exercise the same interface. The [acceptance contract](customer-360-goal.md) distinguishes real extractor execution, scripted browser orchestration, actual inference and authorized vendor validation. A connector catalog entry alone does not pass any live-integration gate.

Feedback sought: a synthetic identity edge case, a business definition that needed correction, or the first source/setup step that failed. Keep customer data and credentials out of issues.

## Ninety-second demo talk track

Use the [application walkthrough](quickstart.md). Keep “Synthetic data” visible. For an authored definition demo, say so. Include model bootstrap/questions only when an actual consented model run is recorded and its claims have been independently checked. If edited for time, label the cuts.

| Time | Show | Say |
| --- | --- | --- |
| 0–15 seconds | A customer brief and one amount/source passage | “What is happening with this customer, and what evidence should I inspect?” |
| 15–35 seconds | Sources, customer links and mapping review | “These sources reach DuckDB through an existing extractor. Customer identity is explicit, and ambiguous links stay unresolved.” |
| 35–55 seconds | Definition filters, units, approval and evidence | “The amount uses this reviewed rule. The passage comes from this attributed record. Missing source capabilities stay visible.” |
| 55–75 seconds | Saved brief, refreshed facts and comparison | “The source facts changed. The definition remains the same. Any changed identity or mapping basis is shown separately.” |
| 75–90 seconds | The second source profile in the same interface | “Use your existing customer stack. Start with the sample, then connect an authorized source and prepare your next review.” |

## Issue feedback CTA

**Try one customer review, then report the first point of friction.** Include the intended question, source categories, setup stage, expected behavior, needed business decision and a minimal synthetic reproduction. Do not upload customer exports, private context or credentials.

## Claims guide

| Supported wording | Omit unless separately demonstrated |
| --- | --- |
| Customer 360 for supported customer sources, with reviewed context | Any tool connects instantly; all catalog entries are verified live integrations |
| Source output is staged in DuckDB before customer context and answers | All data stays local when a configured model receives evidence |
| Identity, mappings and definitions have explicit review | Automatic certification of business meaning or authenticated organizational governance |
| Actual browser workflows with a labeled scripted transport | Scripted responses presented as model reasoning |
| Grounded metric results and exact source passages | Arbitrary narrative synthesis, churn predictions or autonomous customer actions |
| Local structural context export | Managed registry deployment, team permissions or automatic distribution |
| Comparative studies remain deferred | Better-than-MCP claims, cost savings, accuracy lift or speed multipliers |

Use the [build evidence](build-status.md), [connector limits](connectors.md) and [evidence policy](gtm.md#scorecard-and-evidence-policy) as the factual scope references. Lead with the customer job and make the next step runnable.
