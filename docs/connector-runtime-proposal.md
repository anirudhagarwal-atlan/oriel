# Optional connector runtime decision

Decision recorded 9 October 2026. The exact optional dependencies are now declared and resolved in `uv.lock`; the runtime is installed and native SQLite plus loopback REST/GraphQL verification passed.

The copied source adapters use dlt. The approved optional group is:

```toml
[project.optional-dependencies]
connectors = [
  "dlt[duckdb,sql_database,filesystem]==1.24.0",
  "PyYAML==6.0.3",
]
```

The public lock retains the 1 October 2026 release-age cutoff and all reviewed dependency versions and hashes. Its registry and artifact URLs point to public PyPI; each artifact was matched against release metadata before the fresh-install check. Installation is followed by actual dlt extraction from a synthetic local SQLite database and loopback REST/GraphQL fixtures through DuckDB publication, context bootstrap, query and refresh. No live account credentials or vendor APIs are needed for these checks.

The dlt version matches the reviewed upstream lock and the worker's configuration-provider integration. It was published 19 March 2026; PyYAML 6.0.3 was published 25 September 2025. Both precede the project's 1 October 2026 cutoff. Package owners and declared metadata were checked against [dlt's release](https://pypi.org/project/dlt/1.24.0/) and [PyYAML's release](https://pypi.org/project/PyYAML/6.0.3/). Recorded vulnerability arrays were empty when checked; transitive versions still require lock review. The inspected dlt wheel's SHA-256 is `ae270eefe54a42d662507ec52a9b3c0608eedd6e297f2c9f02f0f95689da2e03`.

This extra supplies the shared extraction runtime, SQLAlchemy and filesystem support. It does not install every vendor SDK, database driver, or GCS/Azure filesystem adapter. Per-source requirements and import evidence are preserved in the bundled manifest; those packages need separate version and compatibility review. Oriel now declares Python 3.10–3.14 to match the supported connector runtime.

User-facing commands remain Oriel. Attribution and license terms stay in technical provenance files and distributed notices.
