# GTM plan: talk to data across the tools you already use

Strategy proposal, updated 9 October 2026. Oriel's first public offer is a **developer preview for cross-tool analytics with reusable, reviewed context**. Comparative evaluations remain deferred. This launch should earn users through a useful workflow and inspectable evidence.

## Positioning

**Headline:** Talk to data across the tools you already use.

**Product description:** Bring tool data into DuckDB, work with it through SQL or your agent, and keep reviewed business definitions for the next question.

**Differentiation hypothesis:** the useful output is both an answer and the context needed to reproduce it: identity mappings, metric rules, SQL, provenance and a reviewed version. Test whether saving that context reduces repeated explanation and makes a second session or consumer useful. SQL access, connector count and natural-language querying alone are insufficient reasons to choose the project.

The product is independent of a particular source pair. Users can bring supported exports or configure an adapter. The [connector guide](connectors.md) states the tested paths, dependencies and source-specific limitations. A packaged source definition does not establish a working authenticated connection. MCP is one way an agent accesses Oriel; it belongs in setup documentation, not in a replacement claim.

Keep three messages consistent across the repository and launch assets:

- **Start:** Ask a question that spans tools you already use.
- **Return:** Reuse the business definition you reviewed last time.
- **Share:** Hand another consumer the same version and its evidence.

## Audience and first useful job

Start with analytics engineers, developers building agents, and technical operations builders. They can run a local CLI, inspect SQL and help resolve source identities. Pair them with an operator who owns the recurring question. A business user who needs a hosted, managed connection experience is a later audience.

The first recipe is **CRM entitlements plus product activity: which active customers have low adoption and unused seats?** It has a recurring use, understandable inputs and a visible reason to review context. Customer IDs may differ across systems; repeated activity must not inflate adoption; the definition needs a time window, exclusions and a completeness check.

Use the [CRM and product export walkthrough](tool-exports.md) as the acquisition example. Its synthetic fixture produces 51 unused seats across five organizations, then 8 at one after refresh using the same approved definition. Those are fixture results, not customer outcomes. The user can then map exports from their own CRM and product tooling into the same workflow.

Qualify an early trial with three facts: a question asked repeatedly, authorized data available from at least two sources, and someone willing to review the business rules. Ask which tools and input paths they use; let observed demand determine the next authenticated connector to validate. Billing plus support, and pipeline plus product activity, are subsequent recipe candidates rather than advertised completed workflows.

## Acquisition → activation → repeat use → shared context

| Stage | User action | Product or content to provide | Observable completion |
| --- | --- | --- | --- |
| Acquisition | Recognize their question in a concrete example | Short demo, repository overview, supported-input table and one recipe | A qualified builder starts the example or own-data path |
| First result | Run the synthetic example and inspect the answer | Copyable commands, expected output and visible SQL | Expected result reproduced; user can explain its population and rule |
| Activation | Answer their recurring question using authorized tool data | Import/source setup, identity-map review and context draft | User reviews a useful answer with SQL, source observation details and unresolved assumptions visible |
| Repeat use | Refresh data or open a new session and run the approved definition again | Saved version, explicit refresh and failure status | A later useful run reuses an approved definition without silently changing its meaning |
| Shared context | Give another consumer the reviewed bundle | Portable export, inspectable files and a receiving-workspace walkthrough | Second consumer executes the definition and identifies its version and source requirements |
| Team expansion | Change a definition used by more than one consumer | Ownership and update discussion; scoped registry validation | A concrete need for shared distribution or coordinated updates, beyond downloading a bundle |

For the first visit, make **Try the example** the primary CTA and **Bring your tool data** the second. Link each to working instructions. After a successful run, lead to review and reuse. Introduce registry handoff after a user has something worth sharing.

Do not require a model account for the first example. Keep deterministic SQL execution visibly distinct from a model-generated answer. When a user chooses an agent, explain which context and tool results may reach its model provider. No source data or context bundle is uploaded automatically.

## Channel execution

These are proposed activities, not completed distribution or outreach. Use the [launch kit](launch-kit.md) after the public-preview checklist is met.

| Channel | Specific asset and action | CTA | Evaluate by |
| --- | --- | --- | --- |
| GitHub repository | Lead with the question, one runnable example, prerequisites and a scope table. Link limitations beside source setup. | Complete the example; report the first point of friction in an issue | Example completions and actionable setup reports, separate from stars |
| Technical article or project page | Explain mismatched IDs or repeated events in the synthetic recipe. Show SQL, correction, reviewed definition and changed-data rerun. | Reproduce the recipe, then try an authorized export | Readers who complete a workflow and return |
| Analytics-engineering communities | Share the modeling walkthrough where project posts are welcome; ask for counterexamples to its identity and metric rules. | Suggest a synthetic edge case or recurring question | Useful rule corrections and qualified own-data trials |
| Agent/developer communities | Demonstrate the documented agent interface and portable context artifact. Name only verified client setups. | Try the setup and inspect the tool trace | Successful setup and reuse in another session |
| Source-tool communities | Publish a recipe with an explicit export path. Promote a live adapter only after authentication and selected resources are verified. | Map that tool's export to the recipe | Completed own-data workflows for that source combination |
| Opt-in builder/operator trials | Observe setup, first answer, review and a later refresh; record where help was required. | Complete one recurring job and return with changed data | Activation, repeat use and second-consumer handoff |

Use one reusable demo and recipe across channels initially. Change the opening question to fit the audience. Record the route by which volunteers found the project, with consent, rather than installing tracking as part of onboarding. Scale the channel that produces repeat users, not the one producing the most clicks.

## Launch readiness: evidence and remaining gates

A useful developer preview can launch without comparative performance claims. A performance-led launch is a separate, deferred decision under the [build charter](build-charter.md).

| Area | Evidence completed | Gate for the public preview or later claim |
| --- | --- | --- |
| Generic workflow | Synthetic export recipe covers mapping, local transformations/changes, reviewed metrics, separate-process reuse and refresh | Reproduce the published first-run instructions from a clean installation; keep expected outputs and failure guidance current |
| Connector execution | Actual dlt checks cover SQLite and selected loopback REST/GraphQL resources through publication, context bootstrap and refresh; failed API pages retain previous snapshots | Show verification per path. Authenticated vendor connections need their own authorized tests before promotion as validated |
| Context | Draft generation, explicit metric review, versions and portable export work | Describe drafts as candidates. Make the review step and local nature of approval clear in the demo |
| Agent experience | CLI/MCP interfaces and a configured-model question loop exist | Verify every client setup named in launch copy; do not represent deterministic execution as a new model evaluation |
| Reuse and registry | Separate consumer processes execute exported definitions; exported metric structures have passed a local native structural check | Real registry create/read/update, organizational identity and access remain unverified; present export as a handoff |
| Public repository | Clone instructions, contribution materials and rendered guides are prepared; fresh base and connector installs passed with public PyPI artifacts on macOS ARM64 / Python 3.12 | Retain tested instructions, working links, licenses/notices and safe artifacts. Hosted documentation and a separate package release need their own publication and checks |
| Adoption | No own-tool trial, retention or commercial conversion evidence has been collected | Run the proposed cohort below; publish observations with denominators and assistance level |
| Comparative performance | No qualifying completed acceptance study | Keep evaluations deferred. Make no comparative accuracy, cost or speed claim |

The [build evidence](build-status.md) and [connector verification record](../contextbase/connectors/native-verification.json) carry technical scope. Link to them instead of compressing all stages into “works with every tool.” The catalog's 101 entries describe packaged coverage; they are not 101 verified live integrations.

## Execution sequence

1. **Prepare the public preview.** Finish clean-install instructions, the synthetic recipe, agent setup, contribution guidance and a short execution recording. Check the actual outputs against launch-copy claims. Assign an owner for incoming issues before inviting users.
2. **Run a small opt-in cohort.** Seek ten qualified builders with recurring cross-tool questions across at least two tool combinations. Observe the supported export path where authentication would otherwise block learning; separately record credential and connection setup effort.
3. **Ask users to return.** Within two weeks of activation, observe a new session or refresh that reuses the reviewed definition. Capture required corrections and work that still had to be repeated.
4. **Test sharing.** Where a second consumer exists, export the bundle and observe reuse. Ask what happens when a rule changes. Validate a receiving registry only for a concrete, authorized handoff.
5. **Publish the learning and prioritize.** Fix repeated setup or semantic failures, then produce the next recipe or source verification from demand. Resume comparative studies only when requested, under their existing evidence requirements.

If users can run the demo but cannot answer their own question, prioritize import and mapping. If they answer once but never reuse the definition, revisit the recurring job and saved artifact. If they return but cannot share context safely, prioritize the receiving workflow. These decisions precede a broader campaign or commercial packaging.

## Analytics and Agent Registry handoff

Keep local analysis, transformations, reviewed definitions and portable export independently useful. A local workspace does not require an Atlan account.

Analytics becomes a relevant conversation when users need a maintained recurring workflow. Agent Registry becomes relevant when multiple consumers need to discover, own and update the same reviewed definition. These are expansion hypotheses; they do not establish current hosted integration, pricing, access controls or operational support.

The current boundary is an inspectable bundle and explicit target mapping. Exporting one does not deploy it. Before promising a managed handoff, demonstrate an authorized create/read/update round trip, verify consumer access, and show how a changed definition reaches the intended consumers. Track **second consumer plus a requested definition change** as stronger evidence of this need than export count alone.

## Scorecard and evidence policy

**North-star hypothesis:** weekly active workspaces completing a useful own-data run with previously approved context. The project has no adoption measurements yet; the following targets are learning hypotheses for the first ten qualified builders, not forecasts or observed results.

| Proposed learning target | Exact event and window |
| --- | --- |
| At least 7 of 10 complete the example | Reproduce and inspect its expected result; aim for ten minutes after installation, while measuring installation separately |
| At least 5 of 10 activate on their own data | Review one useful answer within seven days of starting; collectively cover at least two source combinations |
| At least 3 activated users repeat | Complete another useful run using an approved definition within fourteen days of activation |
| At least 2 activated users demonstrate sharing | A second consumer executes the exported definition within twenty-one days of activation |

Record denominators, input paths, source combinations, assisted versus unassisted completion, installation/authentication time, mapping/review effort, failures and unresolved rules. Count source access blockers as outcomes rather than dropping those users. Use opt-in observations or user-submitted sanitized receipts; do not collect source rows, credentials or private context in public issues. These small counts cannot establish product-market fit.

Use the following evidence rules in every launch asset:

- Describe synthetic fixture outcomes as synthetic and name the workflow verified.
- Distinguish packaged definitions, controlled tests, actual runtime integration and authenticated vendor validation.
- Distinguish a deterministic consumer process from an agent independently choosing a definition.
- Call generated business meaning a draft until reviewed; local approval is not organizational certification.
- Keep source writeback, scheduled sync and managed registry distribution out of current capability claims.
- Keep comparative evaluations deferred. Existing experiments and implementation tests do not support claims of being more accurate, cheaper, faster or better than MCP.

The [launch kit](launch-kit.md) applies these rules to ready-to-edit copy. The [deferred benchmark plan](rystic-benchmark.md) remains the place for later study design and acceptance criteria.
