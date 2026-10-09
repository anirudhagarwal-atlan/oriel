"""Resolve explicitly configured, preinstalled pinned connector source code.

This replaces the upstream download/install helper. No environment lookup,
network request, package installation or user configuration discovery occurs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

VERIFIED_SOURCES_REF = "75b3ec17eab99d0079d9f61b7f47fc8b899a5738"
VERIFIED_SOURCES_REPO = "https://github.com/dlt-hub/verified-sources"
_configured_root: Path | None = None
_manifest_sha256: str | None = None


def configure_verified_sources(root: str | Path) -> None:
    """Select an existing bundle explicitly; this does not import a connector."""
    global _configured_root, _manifest_sha256
    given = Path(root)
    if given.is_symlink():
        raise ImportError("Verified-source bundle must be a regular local directory.")
    try:
        resolved = given.resolve(strict=True)
        manifest_path = resolved / "manifest.json"
        if not resolved.is_dir() or manifest_path.is_symlink():
            raise ValueError("Invalid bundle location")
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raise ImportError("The pinned verified-source bundle is unavailable.") from None
    if (not isinstance(manifest, dict)
            or manifest.get("spec") != "contextbase.verified-connectors/v1"
            or manifest.get("repository") != VERIFIED_SOURCES_REPO
            or manifest.get("ref") != VERIFIED_SOURCES_REF
            or not isinstance(manifest.get("sources"), dict)
            or not isinstance(manifest.get("files"), list)):
        raise ImportError("Verified-source bundle does not match the pinned source contract.")
    _configured_root = resolved
    _manifest_sha256 = hashlib.sha256(raw).hexdigest()


def ensure_verified_source(name: str) -> Path:
    """Verify local source bytes and expose their root; never fetch or install."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ImportError("Invalid verified-source module name.")
    if _configured_root is None:
        raise ImportError("Configure the preinstalled pinned verified-source bundle before use.")
    root = _configured_root
    try:
        raw = (root / "manifest.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != _manifest_sha256:
            raise ValueError("Manifest changed")
        manifest = json.loads(raw)
        directory = "sources/" + name
        if not any(item.get("source_directory") == directory for item in manifest["sources"].values()):
            raise ValueError("Source is not listed")
        selected = [item for item in manifest["files"]
                    if item["path"].startswith(directory + "/") or item["path"] == "sources/__init__.py"]
        if not any(item["path"] == directory + "/__init__.py" for item in selected):
            raise ValueError("Source implementation is absent")
        for item in selected:
            path = root / item["path"]
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("Source path escapes bundle")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("Source bytes changed")
        existing = sys.modules.get("sources")
        if existing is not None:
            paths = list(getattr(existing, "__path__", []))
            if not paths or any(not Path(path).resolve().is_relative_to(root) for path in paths):
                raise ValueError("Another source package is already loaded")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ImportError("The selected verified-source implementation is missing or changed.") from None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
