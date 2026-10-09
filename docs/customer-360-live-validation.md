# Preparing authorized live validation

Oriel's local tests and configured-model checks do not establish vendor access or live connector compatibility. The live acceptance gate still needs a separately authorized initial pair of tools and a third distinct connector, reconciled against their source-visible records. No live account, credential or source has been accessed to prepare the helper described here.

The test-only [contract helper](../tests/customer360/live_contract.py) validates declarations and reconciles supplied evidence. It performs no network, file, credential or model access. The separate [bundle inspector](../scripts/inspect_customer360_live_bundle.py) reads explicitly selected private evidence files, verifies their byte hashes and passes their decoded declarations to that checker. Neither grants permission, configures a source, approves an adapter or completes the live gate. The four live acceptance cases remain unimplemented until a collector can execute the authorized product workflow.

## Inspect retained evidence files

Run this repository-only command once a collector and independent reviewer have supplied the required artifacts:

```sh
uv run --no-sync python scripts/inspect_customer360_live_bundle.py \
  --bundle /absolute/private/evidence-bundle \
  --capabilities /absolute/private/adapter-review/capabilities.json \
  --capability-sha256 REVIEWED_CAPABILITY_FILE_SHA256
```

Select the capability file and its expected byte hash separately from the source bundle. The allowed filename is `capabilities.json` or `reviewed-capabilities.json`, in a different private directory. Obtain this reviewed input independently; copying a bundle's assertions into another file does not create an adapter review. The command defaults to the current UTC time; `--checked-at UTC_TIME` supports explicit historical inspection without making old evidence current.

The evidence directory contains `bundle.json` with this exact shape. Hashes are SHA256 of actual file bytes; byte counts are exact integers:

```json
{
  "spec": "oriel.customer360.live-evidence-bundle/v1",
  "documents": {
    "contract": {"file": "contract.json", "sha256": "SHA256", "bytes": 123},
    "evidence": {"file": "evidence.json", "sha256": "SHA256", "bytes": 123},
    "extractions": {"file": "extractions.json", "sha256": "SHA256", "bytes": 123},
    "references": {"file": "references.json", "sha256": "SHA256", "bytes": 123}
  },
  "proof_artifacts": [
    {"file": "artifacts/SHA256.bin", "sha256": "SHA256", "bytes": 123}
  ]
}
```

The placeholders above are structural documentation, not usable evidence. `contract.json` contains the access contract; `evidence.json` maps proof hashes to envelopes; the other two documents contain the six extraction and six independent-reference receipts. Every submitted proof envelope needs its exact retained artifact, and unreferenced manifest artifacts are refused.

The separate capability file contains exactly `spec: "oriel.customer360.reviewed-capabilities/v1"`, a `capabilities` hash-to-capability map, an `evidence` hash-to-envelope map, and a `proof_artifacts` array using the same relative artifact format. Its artifacts are resolved under the capability file's own private directory. Evidence maps cannot overlap. Proof bytes are hashed and discarded, not interpreted as executable instructions.

The loader requires POSIX ownership and directory-relative no-follow file controls; unsupported platforms refuse before opening files. It requires owner-only directories and regular single-link files, refuses symlinks and escaping paths, checks byte limits before JSON decoding, and rejects duplicate JSON keys and floats. Limits are 64 KiB for the manifest, 2 MiB for the capability file, 16 MiB per document/artifact, 64 MiB overall and 128 proof artifacts. A ten-second cooperative deadline bounds repeated checks; it is not a hard cancellation guarantee for a blocked operating-system file read. Only the explicitly declared files are opened.

Exit code zero means **file integrity and conditional reconciliation passed**. The output excludes record contents, quotations, financial totals and absolute paths. It still says `live_gate_passed: false`, `execution_authorized: false` and `origin_authentication: "operator_assertion_only"`. Real-looking declarations and matching files cannot prove who produced them, how data was fetched, or whether a vendor authorized that access. The live acceptance suite does not accept this report as a substitute for running the product and inspecting authentic source evidence.

## Interfaces and trust boundary

```python
validate_contract(contract, adapter_capabilities=capabilities, evidence=proofs)

reconcile_receipts(
    contract, extraction_receipts, independent_references,
    adapter_capabilities=capabilities,
    evidence=proofs,
    checked_at="2026-10-09T12:00:00Z",
)
```

These functions accept JSON-compatible values in memory. They return detached data or raise `LiveContractError` with a fixed error code that does not echo submitted records. Unknown and missing fields are rejected. Times must be UTC. `digest(value)` hashes strict, canonical ASCII JSON; floats are refused rather than rounded. Convert exact decimals to strings in both acquisition paths. Source IDs must be strings.

Each digested artifact is bounded to 200,000 decoded nodes and 16 MiB of scalar/key content, in addition to depth, row and cell limits. These are in-memory checks; a future file/HTTP loader must enforce its own byte cap before decoding.

`adapter_capabilities` is a **separately reviewed allowlist**, keyed by capability hash. An authorization document cannot declare its own adapter trustworthy. The future collector must pin the reviewed implementation and verify its actual runtime identity. This helper checks declared identities and proofs; it does not load the adapter or enforce its controls.

`evidence` maps proof-envelope SHA256 values to exact envelopes:

```text
kind, origin, subject_sha256, recorded_at, producer_ref,
artifact_sha256, derived_from_sha256
```

`artifact_sha256` identifies a retained, privately reviewed artifact such as an authorization record, permission readback or complete acquisition trace. The helper does not open that artifact or authenticate its origin. A collector/operator must verify that it exists, matches the hash and demonstrates the stated control. A renamed fixture, a declared `authenticated_vendor` string or a self-generated hash is insufficient.

Every successful reconciliation report therefore includes:

```json
{
  "conditional_reconciliation_passed": true,
  "live_gate_passed": false,
  "execution_authorized": false,
  "origin_authentication": "operator_assertion_only",
  "network_calls": 0,
  "model_calls": 0
}
```

Those zero counts describe this pure checker, not the acquisition work represented by submitted receipts. A successful report is one input to a future live-run review; it cannot substitute for that review or for the product acceptance cases.

## Access contract

The exact top-level fields are:

```text
spec = oriel.customer360.live-access/v1
created_at
authorization
initial_sources = [source_id, source_id]
additional_source = third_source_id
sources = [three source scopes]
max_evidence_age_seconds
max_reference_skew_seconds
contract_sha256
```

All three source IDs and connector types must be distinct. The additional connector must later demonstrate addition or replacement through the shared Customer360 contract; merely configuring its name does not meet that requirement.

`authorization` contains `reference`, `approved_by`, `approved_at`, `expires_at`, `source_ids`, `source_scope_sha256`, `read_only: true`, `model_data_allowed: false`, and `evidence_sha256`. Each source-scope hash commits the exact origin, principal, resources, fields, history and bounds described below. Authorization cannot cover only a source name while allowing its scope to change afterward. The authorization proof uses kind `authorization` and origin `operator_assertion`.

Model disclosure is deliberately outside this contract. Reading an authorized vendor account does not authorize sending its records or transcripts to a model. That requires a separate approved data scope and product consent.

Each source scope contains:

| Field | Meaning |
| --- | --- |
| `source_id`, `connector` | Stable local source identity and distinct connector type. |
| `endpoint` | Exact approved HTTPS origin, without credentials, path, query or fragment. Loopback, private IP and reserved fixture origins are refused. |
| `tenant_ref`, `principal_ref` | Opaque references to the approved account and read-only principal. No credential values. |
| `capability_sha256` | Exact separately reviewed adapter capability. |
| `resources` | Exact selected resource, field, record-ID and history scope. |
| `permissions` | Observed resource/field permission scope and explicit exclusions. |
| `limits` | Positive `max_requests`, `max_pages`, `max_rows`, `max_seconds`. All dispatched requests, including failures/retries, must be accounted for by the collector. |
| `refresh_mode` | `fresh_replace` only in this version. |
| `enforcement_sha256` | Proof of this scope, kind `scope_enforcement`, origin `authenticated_vendor`. |

Origin validation here is offline syntax and literal-IP validation. The collector still needs approved resolved destinations, TLS, redirect and DNS-rebinding controls; an allowed hostname alone does not prove where a request will connect.

A resource has exactly `name`, `fields`, `id_field`, `history`, `record_ids`, `numeric_fields`, `currency_field`, and `text_fields`. `history` contains a selected timestamp `field` and a half-open `[start, end)` UTC window. `record_ids` is an explicit unique list or `null` for the whole authorized visible resource. Null does not mean unrestricted access beyond the separately approved principal and resource scope. ID, history and reconciliation fields must all be selected.

Permissions have exact `resources`, `fields` by resource, and `exclusions`. An exclusion contains `resource`, nullable `field`, and `reason`. A selected field/resource cannot simultaneously be excluded. Empty exclusions are an explicit statement that none were observed within the approved scope; they must be checked against the actual principal rather than filled by default. A permission change requires new review and scope, not silent omission of inaccessible records.

## Adapter capability and enforcement

A capability has exactly:

```text
spec = oriel.customer360.live-capability/v1
connector, implementation_sha256, origins, resources,
controls, refresh_modes, reviewed_by, reviewed_at, evidence_sha256
```

`resources` maps resource names to supported fields. `controls` must cover `endpoint`, `principal`, `read_only`, `resources`, `fields`, `history`, `record_ids`, `requests`, `pages`, `rows`, and `seconds`. There is no `declared_only` mode. Endpoint, selection, filtering and execution bounds require `adapter_enforced`; principal, read-only and field scope may instead use `vendor_permission_enforced` with the corresponding permission proof. A query parameter that the adapter ignores is not enforcement.

The capability proof uses kind `capability_review`, origin `controlled_runtime_verification`, and binds the full capability excluding its own proof reference. Controlled tests can establish a control contract; they cannot establish vendor authorization. The separate source-scope enforcement proof must bind the exact selected scope and principal, and occur within the authorization interval before contract freeze.

There is no preapproved live-adapter allowlist in this change. REST and GraphQL syncs now accept explicit shared request-attempt and elapsed-time limits through the [connector sync budget](connectors.md#bound-an-api-sync). Controlled local extraction verifies those limits across resources, nested pages, failed responses and interrupted bodies. A private ledger retains every reserved attempt; incomplete extraction cannot replace published data. This does not establish vendor access or enforce all field, history, record, page and row bounds required here. An eventual collector must implement and demonstrate those remaining controls, or choose a genuinely permission-bounded scope that the reviewed adapter supports. It must not generate a capability declaration merely to satisfy this checker.

The scope must cover every fetched resource, including parent resources used to enumerate a selected child. Selected output tables alone do not describe all vendor reads. The existing REST parameter validator discovers parent dependencies, but its worker receipt lists selected output resources; it does not prove authorization of the entire fetch graph. The collector must compile that graph and enforce the frozen scope before resolving credentials or dispatching requests. Post-extraction filtering cannot establish that unauthorized fields or records were never fetched.

Oriel currently starts a fresh isolated extraction pipeline for each sync and atomically replaces the published tables. This contract supports that behavior. Upstream incremental declarations do not prove durable cursor reuse, deletion handling or recovery; relabeling a run as incremental is refused. Durable incremental support needs a distinct reviewed state contract.

## Extraction and independent references

Supply exactly one initial and one refreshed extraction **and** independent reference for each of the three sources: six of each. Duplicate, omitted, partial, failed, stale or out-of-order evidence is refused. Every refreshed extraction must name its actual initial predecessor.

Both record types share:

```text
spec, source_id, phase, contract_sha256, scope_sha256,
origin = authenticated_vendor,
started_at, finished_at, source_observed_at,
permissions, resource_coverage, rows, evidence_sha256
```

`source_observed_at` is the observation time, not a record's last-update timestamp. It must equal the oldest resource observation. Each `resource_coverage` entry contains `resource`, exact `fields`, `row_count`, `complete: true`, and `source_observed_at`. Every selected resource must appear, including a source-confirmed empty resource. The collector must retain evidence of pagination termination and coverage; a zero count or a `complete` flag alone cannot demonstrate either.

Rows have exact `resource`, string `id`, and `values`. `values` must contain exactly the selected fields and the matching ID. Record IDs must be unique within each resource. Every record is checked against the authorized ID/history bounds. Numeric and currency fields cannot be missing, null, boolean or float; currency totals remain separate. Selected text must be present as exact strings. Independently acquired row identities and every selected value must match, so equal aggregate totals cannot conceal a swapped customer, fabricated quotation or missing record.

Extraction receipts additionally contain:

```text
spec = oriel.customer360.vendor-extraction/v1
capability_sha256, adapter_sha256, endpoint, tenant_ref, principal_ref,
refresh_mode, previous_receipt_sha256,
request_count, page_count, complete = true, status = completed,
receipt_sha256
```

Independent references additionally contain:

```text
spec = oriel.customer360.vendor-reference/v1
method = vendor_export | vendor_readonly_query | operator_source_readback
observer_ref, independent_of_extractor = true,
derived_from_sha256 = [], reference_sha256
```

Reference artifacts must come from a separate source-visible acquisition, with a different recorded producer and a distinct complete artifact. Copying the connector's output into a file or recomputing its hash is not an independent reference. This first version refuses derived reference lineage entirely; it does not try to infer independence from an arbitrarily deep graph. Setup proofs also cannot be reused as extraction/reference artifacts.

The checker requires matching source/resource observation windows, exact row reconciliation, and a changed source fact across refresh. Merely updating polling/history timestamps is insufficient. Its sanitized report includes per-resource counts, exact decimal totals by currency and added/deleted/updated counts; it excludes record IDs and source quotations. Keep even these summaries private unless their disclosure is approved.

## Work still required before a live gate can pass

1. Receive and record the separately authorized source/account scope. This document does not repeat or answer the pending access request. Resolve selected SDK/runtime and authentication gaps through the normal review process.
2. Implement a collector that verifies actual adapter/code identity, enforces the approved scope before dispatch, captures every request/page and failure, retains private proof artifacts, and refuses unknown coverage. It must never read credentials into a receipt or prompt.
3. Independently acquire and review source-visible references. Prefer vendor test/sandbox accounts with synthetic records for the first authorized run; report that scope accurately. Do not create or modify vendor records without separate write authorization.
4. Execute initial extraction, reviewed identities/mappings/context, brief/question/evidence and refreshed product checks, then add or replace the third connector through the same core contract. Reconcile a known or naturally occurring change and retain failure/staleness evidence. Source permission does not automatically permit model disclosure.
5. Implement the four actual live acceptance cases using those current-run artifacts and the real workflow. Inspect evidence and update only the exact demonstrated resource/authentication claims. The checker alone cannot make `live_vendor_verified` true.

Run the offline refusal tests with:

```bash
.venv/bin/python -m unittest discover -s tests -p test_customer360_live_contract.py -v
.venv/bin/python -m unittest tests.test_customer360_live_bundle tests.test_customer360_live_bundle_cli -v
```

Their authorization records, principals, source claims and artifacts are all fabricated parser fixtures. They are not vendor receipts and must never be submitted as live evidence.
