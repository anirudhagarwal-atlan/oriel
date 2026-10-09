# Roadmap

Oriel's direction is simple: help someone answer a question across their existing tools, then make the reviewed definition useful to the next question or agent.

This is a priority sequence for a developer preview, not a dated delivery commitment. [Current evidence](build-status.md) separates implementation from validation.

## Available now

- File imports, a 101-entry source catalog, source configuration and explicit sync into DuckDB.
- SQL, schema profiling, draft context, reviewed metric versions and local transformations.
- A built-in Claude question loop and read-only MCP stdio interface.
- Portable context bundles and a reproducible separate-consumer example.
- Real dlt checks using SQLite and selected local REST/GraphQL fixtures, including refresh and failure retention.

Authenticated live vendor compatibility remains unverified. Source catalog coverage is not a promise of live connectivity.

## Next: a dependable first workflow on your data

1. Validate selected source authentication and resource coverage with explicitly authorized accounts.
2. Observe fresh-checkout setup, first query and refresh with technical users on more than one tool combination.
3. Improve the setup and definition-review steps where those users get stuck.
4. Verify documented setup in specific agent clients before advertising those integrations as tested.

An export-based workflow can establish value while a live connector is being validated. Contributions should include synthetic reproductions and precise coverage, so others can distinguish a tested resource from an untested source family.

## Then: reuse across people and agents

- Make recurring definition review easier without treating inference as approval.
- Verify a real registry create/read/update round trip and a separate consumer using the selected version.
- Learn when teams need discovery, access controls and coordinated distribution beyond a local bundle.

The free local workflow must remain useful on its own. An external registry's authentication, permissions and publication lifecycle remain its responsibility.

## Deferred: comparative evaluation

The repository retains the Rystic comparison design and historical exploratory results. New comparative runs are deferred. Before publishing accuracy, cost or latency advantages, measure equivalent data and business context, capable baselines, setup and refresh work, failures and uncertainty. [Evidence policy](gtm.md#scorecard-and-evidence-policy).

## Outside the current scope

Managed hosting, a universal OAuth flow, scheduled synchronization, broad source writeback and production access control are not part of this local preview. Feature requests should start with the user question and the observed limitation. [Contribute a use case](../CONTRIBUTING.md).
