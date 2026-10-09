# Start with a customer review

The primary workflow is now the [Customer 360 application](quickstart.md): bring supported customer sources together, review identity and definitions, inspect a brief, and compare the next review after refresh. Billing/support and CRM/conversation samples use the same interface. Your own stack can include support, product usage, billing, conversations, CRM or internal customer sources; the available evidence determines what can be answered.

The analytical recipes below remain useful advanced examples and extensions. They do not establish that every question or source category is implemented in the current customer answer contract.

Oriel is most useful when a question needs records from more than one source and a business definition that you expect to use again. Start with a bounded time period, data you are authorized to use, and one result you can independently check.

## Runnable examples

| Question | Data | What you learn | Start |
| --- | --- | --- | --- |
| Which customers activated fewer than half their licensed seats? | Synthetic CRM seats, product events and a customer mapping | Resolve identity, deduplicate people, review activation rules, refresh and reuse a metric | [Quickstart](quickstart.md), then [complete recipe](tool-exports.md) |
| Which paying customers have unresolved high-priority issues? | Recorded synthetic payments and issues | Aggregate before joining, subtract refunds, handle currencies and retain a definition across refresh | [Payments and issues walkthrough](rystic.md) |

The first example returns 51 unused seats across five organizations, then 8 at one after refresh. The second returns $1,857.69 in associated net collected USD, then $1,490.61 after the recorded issue change. These are fixture results. They do not measure recovered revenue, reduced churn or a model's accuracy.

## Questions to adapt to your data

These are recipe ideas, not prebuilt or live-validated integrations.

| Question | Likely inputs | The definition to review first |
| --- | --- | --- |
| Which accounts bought capacity but have little product activity? | CRM contracts + product events | Account mapping, eligible contracts and meaningful activity |
| Which customers have repeated support issues before renewal? | Support tickets + contract dates | Duplicate tickets, unresolved states and the renewal window |
| Which product behaviors appear in won opportunities? | CRM opportunity history + product events | Observation before the outcome, opportunity/account grain and attribution limits |
| Which operational exceptions overlap with delayed payments? | Internal exceptions + invoice records | Invoice status, due dates, currency and entity identity |

Use your tool's CSV, JSON, JSONL or Parquet exports, or inspect its [connector readiness](connectors.md). Selecting a familiar brand does not remove the need to validate its schema, permissions and authentication.

## Choose a first workflow

Pick a question that has an owner, recurs, and currently requires manual reconciliation. A good first result is a small account list or a single metric with inspectable SQL. Avoid starting with an unbounded request to understand the entire business.

1. Import a small, complete slice from two tools.
2. Confirm how identities join and which periods the data covers.
3. Run the query and compare it with an independently checked result.
4. Review and save the definition.
5. Refresh the source data and run it again.
6. Export the context when another person or agent needs it.

For a single current record or a source-specific action, direct source tools may be the better fit. Oriel's local snapshots suit analytical work where joins and repeatable definitions matter. [Tradeoffs and limits](faq.md).

## Contribute a recipe

A useful recipe includes a real question, synthetic inputs, an explicit identity mapping, SQL, independently checked expected results, unresolved assumptions, and a changed-data case. Name the input path honestly: export, locally tested connector fixture, or authenticated vendor connection with scoped evidence.

Use the repository's **Use case** issue template to describe the question before building a broad integration. Do not attach customer exports or credentials. [Contribution guide](../CONTRIBUTING.md).
