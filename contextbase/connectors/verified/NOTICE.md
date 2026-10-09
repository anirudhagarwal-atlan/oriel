# Bundled dlt verified-source templates

Source: https://github.com/dlt-hub/verified-sources
Pinned commit: 75b3ec17eab99d0079d9f61b7f47fc8b899a5738
Copyright 2023 The Contributors. Upstream LICENSE.txt is preserved verbatim.

The 25 source directories, READMEs, requirements and helper modules are copied
byte for byte from this commit. No source inside those directories is changed.
sources/__init__.py is a locally added package boundary; upstream uses a namespace
directory. This notice and manifest are local additions. Upstream has no root
NOTICE file at this commit.

LICENSE.txt states Apache-2.0. Upstream pyproject.toml labels project metadata MIT,
an upstream metadata inconsistency. This bundle retains the actual Apache license
text and notices without replacing them with the metadata label.

These are source templates, not installed dependencies. Requirements are preserved
as declared upstream, often broad or unpinned. The manifest includes static import
and distribution hints, including imports absent from a source requirements file.
Those may be transitive or feature-specific requirements. No dependency resolution,
package installation, security advisory review, source import, authentication or
live extraction was performed. Review dependencies before explicit installation.

Only selected source directories and their static internal import closure are
included. Root pipeline examples, root .dlt configuration, credentials, tests and
unrelated connectors are excluded. Upstream documentation and helper scripts are
preserved; packaging and static integrity tests never execute those scripts.

SHA-256 values bind bundled bytes. Git blob IDs establish equality with the
selected git object; neither constitutes publisher-signature verification.
