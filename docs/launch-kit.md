# Oriel launch kit

Draft assets for the Customer 360 developer preview at [anirudhagarwal-atlan/oriel](https://github.com/anirudhagarwal-atlan/oriel). **These announcement and social drafts have not been posted.** [GTM strategy](gtm.md) · [Release evidence](customer-360-goal.md).

## Before posting

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

## The story to tell

An account owner has a customer call in 20 minutes. Their last review says the customer's SSO rollout is blocked. They need to know what changed before they choose what to discuss.

The synthetic CRM + conversations sample supplies the discovery: a newer call says the rollout is unblocked and procurement approval is now pending. Both passages remain inspectable. The older note was true at the time; its continued presence does not make it the latest update.

The payoff is a customer review with evidence the account owner can inspect. The next question is already useful: what will change before the next call? Save this review, refresh the sources, and compare again with the same reviewed context.

| Story beat | What earns attention |
| --- | --- |
| Stakes | A person preparing for an imminent customer call could walk in with an outdated account of the problem. The 20-minute deadline is the scenario, not a product speed claim. |
| Question | “What changed since the last review?” |
| Discovery | The sample's newer source passage changes the discussion from a blocked SSO rollout to pending procurement approval. Show the old and new evidence together. |
| Return | Save the review now. Reuse the context after the next refresh and inspect what changed. |

Use this specific discovery to introduce the broader Customer 360 workflow. CRM and conversations are one sample; billing, support, product usage and other supported customer sources belong in the same story.

## Repository announcement draft

Your customer call starts in 20 minutes. The last review says their SSO rollout is blocked.

What changed since then?

In Oriel's synthetic sample, the next call record contains the update: “SSO rollout is unblocked; procurement approval is now pending.” You can inspect that passage alongside the earlier blocked-rollout note before deciding what to discuss.

Oriel is an open-source Customer 360 agent for preparing that review across the tools you already use. The workflow is off-the-shelf connectors → DuckDB → context agents and reviewed definitions → briefs and answers with evidence. Customer links, business meaning and source coverage are explicit, so you can inspect how an answer was assembled.

Start with the synthetic CRM + conversations sample. Review the proposed customer links and authored definitions, inspect the initial brief, save it, and load the sample refresh. Compare the new evidence using the same reviewed context. The sample runs without vendor credentials or model calls; configured context agents and questions are a separate opt-in step.

Then try billing + support in the same interface, or connect supported sources you are authorized to use. The workflow is independent of a particular CRM or conversation vendor. [Current status](build-status.md) records this local developer preview's model verification and remaining authenticated-vendor limitations.

Before the next call, refresh again. Your saved review gives you a point of comparison; your reviewed definitions give you a consistent way to read the new facts.

[Run your first customer review](quickstart.md). Start with **Sources → CRM + conversations → Connect sample data** and follow the review steps. Which piece of evidence changed the question you would ask the customer?

## Short social post draft

Your customer call is in 20 minutes. Your last review says SSO is blocked.

The newer call says SSO is unblocked. Procurement approval is pending.

That's the discovery in Oriel's synthetic Customer 360 sample. Inspect both passages, save the review, and compare again after a refresh.

Off-the-shelf connectors → DuckDB → context agents and reviewed definitions → answers with evidence. Bring supported customer tools; start with the credential-free sample and authored context.

[Try Oriel](https://github.com/anirudhagarwal-atlan/oriel#readme). What changed since your last review?

**Posting note:** If attaching a recording, label synthetic data and authored, scripted or model steps. Show only the vendor connections and model calls that actually ran.

## Technical community post draft

**Title:** An open-source Customer 360 agent with DuckDB and reviewed context

The required execution path is off-the-shelf extraction → committed DuckDB snapshots → context agents and reviewed definitions → customer briefs and answers. Customer identity and capability mappings are independent of source brands.

The local application separates customer-link, field-mapping and definition approvals. Its answer contract recomputes metrics and validates exact source passages. Saved reviews retain the original snapshot and context; refresh can change facts without changing the reviewed definition. Missing coverage and changed comparison basis remain visible.

Two contrasting synthetic stacks exercise the same interface. The [acceptance contract](customer-360-goal.md) distinguishes real extractor execution, scripted browser orchestration, actual inference and authorized vendor validation. A connector catalog entry alone does not pass any live-integration gate.

Feedback sought: a synthetic identity edge case, a business definition that needed correction, or the first source/setup step that failed. Keep customer data and credentials out of issues.

## Ninety-second demo talk track

Use the [application walkthrough](quickstart.md), selecting **CRM + conversations** in a fresh workspace. Keep **“Synthetic sample · authored context · no model calls”** visible throughout this version. Record preparation and refresh before editing; label time cuts so the 90-second story does not imply a 90-second setup. This is a talk track, not an existing recording.

The sample uses real SQLite/dlt extraction into DuckDB and authored definition proposals. The context-agent workflow adds model-generated proposals followed by review. Demonstrate that optional model path only in a separately labeled, consented recording with checked outputs; do not narrate authored proposals as model reasoning.

| Time | Show | Say |
| --- | --- | --- |
| 0–12 seconds | Initial Account A brief; open the earlier SSO passage | “Your customer call starts in 20 minutes. The last review says SSO is blocked. What changed since then?” |
| 12–30 seconds | Sources, DuckDB snapshot, then identity and mapping review; mark cuts | “Oriel brings customer records through off-the-shelf connectors into DuckDB. You review which records belong to this customer and what the fields mean.” |
| 30–43 seconds | Approved sample definitions, calculation and saved initial brief | “Context agents can propose definitions for review. This sample uses authored proposals. Inspect the rules and evidence, then save the brief before refreshing.” |
| 43–59 seconds | Load sample refresh, prepare and materialize it using the reviewed context; show the newer passage | “Now the newer call says: ‘SSO rollout is unblocked; procurement approval is now pending.’ That's the update to bring into the conversation.” |
| 59–74 seconds | Saved reviews → Compare with current; open both attributed passages | “The earlier note is still here. The new evidence is alongside it. The definitions stayed the same, so you can inspect what changed.” |
| 74–90 seconds | Repository quickstart commands and CRM + conversations selector; end on saved reviews | “Try this review locally without credentials or model calls. Then bring supported sources from your customer stack. Save the review, refresh before the next call, and ask what changed.” |

## Runnable final frame

**Prepare for the next customer conversation.** Run the sample, inspect the two SSO passages, and save your first review.

With Python 3.10–3.14, uv and a POSIX environment installed:

```sh
git clone https://github.com/anirudhagarwal-atlan/oriel.git
cd oriel
uv sync --frozen --extra connectors
uv run --no-sync oriel --workspace .oriel-customer-crm customer app --port 8787
```

Open **http://127.0.0.1:8787**, choose **Sources → CRM + conversations → Connect sample data**, then follow the [review, save and refresh steps](quickstart.md#3-review-customer-identity-and-context). Use a fresh workspace if you have already tried another source profile. This sample is synthetic; its vendor-shaped records are not live Salesforce or Gong connections.

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
