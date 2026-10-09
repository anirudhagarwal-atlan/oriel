# GTM plan: Customer 360 for the tools you already use

Strategy proposal, updated 9 October 2026. Oriel's first useful job is a customer review: understand what is happening with an account, inspect the evidence, and return after refresh using the same reviewed context. The [product plan](customer-360-plan.md) and [active goal](customer-360-goal.md) define implementation and verification. Comparative evaluations remain deferred.

## Positioning

**Headline:** Customer 360 for the tools you already use.

**Product description:** Connect supported customer tools, bring their output into DuckDB, bootstrap and review business context, and get customer briefs and answers with inspectable evidence.

**Differentiation hypothesis:** the durable output is a useful customer review plus the definitions that make the next review dependable: customer identity, field mappings, metric rules, source coverage and a reviewed version. Test whether this reduces repeated explanation and makes the second review useful. Connector count and natural-language querying alone do not establish that value.

The required execution path is off-the-shelf connectors → staged DuckDB → context agents and reviewed definitions → grounded briefs and answers. CRM, billing, support, product usage, conversations and internal customer data are interchangeable source categories. The first connected stack should come from a user's customer-review workflow and authorized access, not a mandatory vendor pair.

Keep three messages consistent:

- **Start:** Prepare a customer review from the tools you already use.
- **Return:** See what changed using the definitions you reviewed last time.
- **Share:** Give another authorized consumer the same version and source requirements.

## Audience and first useful job

Start with technical customer-success operators and RevOps builders, paired with a customer/account owner. They can configure a local workspace and review source relationships; the account owner can judge whether the brief helps prepare a conversation. Hosted onboarding for nontechnical teams is a later product boundary.

The activation question is **“What is happening with this customer, what changed, and what evidence should I inspect?”** Available sources determine the answer. Billing/support may establish open invoice balances and unresolved issues. CRM/conversations may establish open opportunities and attributed statements. Neither automatically establishes ARR, renewal risk or a health score.

The [quickstart](quickstart.md) supplies both sample stacks in the same interface. Use a synthetic account with a real extractor, explicit review and visible gaps. Show changed data and retained definitions before introducing another feature. The earlier [CRM/product export recipe](tool-exports.md) remains an advanced technical example.

Qualify an own-data trial with a recurring customer-review job, bounded authorized source access, and someone able to review identity and business rules. Record needed assistance. A successful synthetic sample is interest, not activation on customer data.

## Acquisition → activation → repeat use → shared context

| Stage | User action | Product or content | Observable completion |
| --- | --- | --- | --- |
| Acquisition | Recognize their customer-review problem | A short account-focused demo, source categories and clear prerequisites | A qualified operator begins a sample or own-data setup |
| First result | Complete the sample and inspect evidence | Local app, two contrasting stacks, authored definitions or consented model path | User can explain an amount, its definition and its supporting records |
| Activation | Complete a customer review on authorized source data | Connector setup, identity/mapping review, context proposal and approval | Account owner finds the brief useful with gaps and evidence visible |
| Repeat use | Refresh and open the next review | Saved review, current facts, unchanged meaning and visible failed syncs | A later useful review reuses approved context without silently changing it |
| Shared context | Hand the version to another authorized consumer | Structural template, source requirements and separate-consumer checks | Consumer identifies and uses the intended version on compatible data |
| Team expansion | Coordinate definition ownership and updates | A scoped registry integration when needed | Multiple consumers need a reviewed change distributed consistently |

Primary CTA: **Try a customer review.** Secondary CTA: **Connect your customer tools.** Keep the no-model authored example visible without narrating it as agent reasoning. Model calls require consent and spending limits; relevant source data can reach the configured provider.

## Channel execution

These are proposed activities, not completed outreach or distribution. The [launch kit](launch-kit.md) contains drafts; nothing is posted automatically.

| Channel | Asset | CTA | Learn from |
| --- | --- | --- | --- |
| Repository | Working customer review, setup instructions, source limits and evidence | Run the local app and report the first setup failure | Completed workflows and reproducible issues |
| RevOps and technical CS communities | Account review before/after refresh | Bring one recurring review job and an authorized stack | Own-data usefulness and repeat use |
| Agent developers | Context approval, bounded tools, exact quantities and evidence | Inspect a tool trace or add a synthetic edge case | Correctness fixes and reusable definitions |
| Source-tool communities | A scoped recipe for that stack | Map supported source output to customer capabilities | Verified resources and onboarding friction |
| Opt-in pilot workspaces | Observed setup, first review and second review | Return with changed data | Assistance burden, retained context and actionability |

Record how volunteers found the project only with consent. No hidden tracking is required for local onboarding. Expand the channel that produces repeated useful reviews, rather than maximizing impressions or stars.

## Launch readiness: evidence and remaining gates

| Area | Available evidence | Remaining requirement |
| --- | --- | --- |
| Customer workflow | Local app and two sample profiles; actual browser flows with scripted model transport | Reproduce the candidate from a clean checkout and inspect the complete current gate report |
| Extraction | Real off-the-shelf runtimes on controlled sources | Authorized vendor authentication, selected resource reconciliation, refresh and extension proof |
| Context and answers | Explicit reviews, versioned meaning, exact calculations, evidence and historical comparison | Full configured-model product smoke across both profiles and changed data |
| Public repository | Source install, packaged assets, documentation and third-party notices | Review and publish the exact candidate; do not imply unpushed local work is already public |
| Reuse and registry | Local retained context and structural export | Real receiving registry, access and consumer lifecycle checks when a use case requires them |
| Adoption | No own-data retention or commercial conversion evidence | Observe the pilot cohort and report denominators and assistance |
| Comparative performance | Deferred | No accuracy, cost or speed advantage claim |

The [build evidence](build-status.md) and [goal](customer-360-goal.md) are the scope references. A catalog entry is not an authenticated integration. A scripted answer is not model reasoning. A local template export is not managed registry distribution.

## Execution sequence

1. Finish the release candidate's clean installation, real-model smoke and authorized connector evidence. Keep unsupported claims out of preview copy.
2. Recruit five opt-in pilot workspaces across more than one customer stack. Observe connection, identity review and first useful brief separately.
3. Ask activated users to return within two weeks for another customer review using the same definitions.
4. Where a second consumer exists, observe context reuse and a requested definition change before building registry distribution.
5. Fix repeated setup or semantic failures, then expand source recipes from demand. Resume comparative studies only under a separately agreed evaluation scope.

If visitors only complete the sample, fix connection and setup. If they get one useful brief but do not return, revisit the recurring review job. If they return but cannot share context, prioritize the receiving workflow. These observations should guide expansion before a broad campaign.

## Analytics and Agent Registry handoff

Keep the free local workflow useful independently of a registry account. Portfolio analytics becomes relevant when recurring reviews produce cross-account questions. Agent Registry becomes relevant when several agents or account owners need to discover, own and update the same definitions.

The initial handoff is an inspectable structural context template; private customer records stay out of that export. Before promising managed distribution, demonstrate authorized create/read/update, consumer access and a definition revision reaching the intended consumers. A second consumer with a requested rule change is stronger evidence of need than export count.

## Scorecard and evidence policy

**North-star hypothesis:** weekly useful customer reviews completed on a user's own connected data with previously reviewed context. There are no adoption results yet.

Proposed learning targets: five pilot workspaces; three complete an own-data review; two activated workspaces return within two weeks; one demonstrates a second account owner or agent reusing context. These are learning targets, not forecasts or measured outcomes.

Track connection completion, time to first useful brief, number of semantic clarifications, unsupported-claim rate, assistance level, repeated reviews and second-consumer reuse. Measure source authorization and extraction separately from answer latency. Sample completion, repository stars and connector count remain secondary indicators.

Publish only evidence tied to the tested code, source scope and context version. Retain failures, costs and missing coverage. Label synthetic inputs, scripted transports and actual model runs separately. Do not claim a performance advantage from the functional acceptance suite or the small model smoke.
