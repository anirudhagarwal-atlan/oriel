# Connector implementation goal

Updated 9 October 2026 from the user's request to continue implementation. This is the immediate implementation scope; comparative evaluations remain deferred.

## Goal

Make Oriel's reused dlt connector layer support a reproducible user journey:

**Configure an existing tool → sync into the user's DuckDB workspace → bootstrap draft context → query → refresh and query again.**

Preserve the full 101-entry catalog. The product serves users' existing tools; no particular vendor pair or simulator defines its positioning. Retain required third-party attribution in technical notices and distributed licenses.

## Starting point

- All 101 catalog entries, 56 YAML definitions and 25 pinned verified-source templates are bundled.
- The CLI supports source list, describe, add and sync. File sync publishes a separate staged snapshot and bootstraps context.
- 81 focused tests pass. Wheel and source-distribution checks verified 218 connector files; the unpacked wheel completed the CSV configure → sync → bootstrap → query flow.
- Actual dlt execution has not yet been verified. Catalog presence does not imply working vendor connectivity.
- Eleven source types have known source-level blockers: Asana, Salesforce, Kafka, Kinesis, Mux, Bing Webmaster, Google Ads, Azure, GCS, S3 and SFTP. Further resource-level translation gaps are recorded in the catalog.

## Work to complete

1. **Enable the reviewed runtime after approval.** Add the optional runtime, resolve and inspect its lockfile under the existing release-age cutoff, then install it. The pending proposal is `dlt[duckdb,sql_database,filesystem]==1.24.0` and `PyYAML==6.0.3`; see [the exact proposal](connector-runtime-proposal.md). Account explicitly for the runtime's supported Python versions. Additional vendor SDKs and drivers require their own dependency review and authorization.
2. **Prove the shared path with actual dlt.** Use a synthetic local SQLite database, explicit credential environment references and selected tables. Run extraction in the real worker, publish into DuckDB, bootstrap context and execute a query checked against an independent expected result. Modify the source, sync again and verify the updated result and provenance. Preserve approved context across refresh.
3. **Repair inherited adapter gaps.** Fix the eleven known source-level blockers: credential/factory mappings, missing required configuration, resource-to-source wrappers and filesystem readers that materialize selected file contents. Implement and test the catalogued resource features needed for correctness, including pagination, parent bindings, resource selection, endpoint authentication, JSONL and supported parameters. Never remove a blocker merely to make configuration succeed; tie each removal to focused evidence.
4. **Verify API extraction locally.** Run the actual dlt runtime against synthetic REST and GraphQL fixtures on loopback. Exercise pagination, selection, reference-based authentication and error behavior through production adapter paths. Cover required vendor-specific mappings with controlled contract tests without connecting to live services.
5. **Finish onboarding and readiness reporting.** Make supported parameters, credentials, resources, required dependencies and remaining limitations discoverable before sync. Produce a readiness record for every catalog entry, separating definition coverage, tested local contracts, fixture-tested extraction and authenticated live verification. Preserve explicit unsupported states for any externally blocked feature; do not call the full connector implementation complete while known in-scope adapter repairs remain.
6. **Verify the distributed product.** Build the wheel and source distribution with all definitions, source packages and licenses. Run the documented configure → sync → bootstrap → query and refresh paths from the distribution in a clean working directory. Update onboarding documentation and retain reproducible evidence.

## Acceptance checks

- A fresh installation follows the documented commands without undocumented setup steps beyond declared dependencies and credential injection.
- Real dlt SQL, REST and GraphQL extraction each produce the expected query result and a schema-grounded context draft.
- Refresh changes the answer according to changed source data while retaining reviewed business definitions.
- Missing credentials, unsupported resources, partial extraction and timeouts never replace the last successful snapshot. Status accurately distinguishes failed extraction from committed publication and subsequent bookkeeping failure.
- Credential values never enter saved configuration, logs, source control or model prompts. Parameters cannot override authentication or introduce arbitrary execution.
- Known adapter repairs have focused tests; all previously passing relevant checks remain green.
- Packaged source and license integrity checks pass. Readiness documentation states exactly what was tested and what still requires vendor access.

## Constraints and autonomy

- Continue independent local implementation and tests while dependency approval is pending. Pending approval is not permission to install.
- Use synthetic data and local fixtures. Do not read credential stores, connect to real customer tools, invoke models or send source data externally.
- Keep evaluations, Rystic benchmark execution and accuracy/cost/speed claims deferred until the user resumes that work.
- No source writeback, publishing, deployment, git push, integration enablement or security control-plane changes are included.
- Keep bootstrap observations as drafts. Do not infer authoritative business rules from schema alone.
- Preserve existing work and required licenses. Product positioning remains Oriel and existing-tool analytics.
- Report concrete progress, tests and remaining limits. Mark completion only after the acceptance checks pass. If external prerequisites prevent completion, identify them precisely and follow the goal tool's blocked-state rules.

The broader commercial and product direction remains in the [build charter](build-charter.md); this goal takes precedence for the immediate implementation sequence.
