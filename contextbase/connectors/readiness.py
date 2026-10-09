"""Local dependency and declaration checks without loading or contacting sources."""
from importlib import metadata
import json
from pathlib import Path
import re
import sys

from . import catalog, describe

CORE_VERSIONS = {"dlt": "1.24.0", "PyYAML": "6.0.3"}
_FILESYSTEM_PACKAGES = {"s3": ["s3fs"], "gcs": ["gcsfs"], "azure": ["adlfs"], "sftp": ["paramiko", "fsspec"]}


def _package(requirement):
    matched = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if not matched:
        raise ValueError("Invalid declared package hint.")
    return matched.group(1).replace("_", "-")


def _installed(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def inspect(source_type=None):
    """Report prerequisites, not authorization or a successful connection."""
    value = catalog()
    entries = [describe(source_type)] if source_type else value["connectors"]
    manifest = json.loads((Path(__file__).parent / "verified/manifest.json").read_text())
    installed = {}

    def version(name):
        key = name.lower().replace("_", "-")
        if key not in installed:
            installed[key] = _installed(name)
        return installed[key]

    rows = []
    for entry in entries:
        family = entry["execution_family"]
        core = {"duckdb": "1.4.5"} if family == "file" else CORE_VERSIONS
        packages = [{"package": name, "expected_version": expected, "installed_version": version(name),
                     "status": "missing" if version(name) is None else "matched" if version(name) == expected else "version_mismatch"}
                    for name, expected in core.items()]
        hints = list(entry["dependencies"].get("additional", []))
        if family == "dlt_sql":
            hints.append("sqlalchemy")
        if family == "dlt_filesystem":
            hints.extend(_FILESYSTEM_PACKAGES[entry["name"]])
        if family == "dlt_verified":
            source = manifest["sources"][entry["name"]]
            hints.extend(source["requirements"])
            hints.extend(source["install_hints"]["additional_runtime_distributions"])
        seen = {item["package"].lower().replace("_", "-") for item in packages}
        for hint in hints:
            name = _package(hint)
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            packages.append({"package": name, "declared_requirement": hint, "installed_version": version(name),
                             "status": "missing" if version(name) is None else "present_version_compatibility_unchecked"})
        blockers = [item["code"] for item in entry["limitations"] if item.get("blocking")]
        resource_blockers = [{"name": resource["name"], "selected_by_default": resource["selected"],
                              "codes": [item["code"] for item in resource["limitations"] if item.get("blocking")]}
                             for resource in entry["resources"] if any(item.get("blocking") for item in resource["limitations"])]
        python_supported = family == "file" or (3, 10) <= sys.version_info[:2] < (3, 15)
        packages_ready = all(item["status"] not in {"missing", "version_mismatch"} for item in packages)
        if blockers:
            status = "adapter_blocked"
        elif not python_supported:
            status = "unsupported_python"
        elif not packages_ready:
            status = "dependencies_required"
        elif resource_blockers:
            status = "resource_review_required"
        else:
            status = "prerequisites_present_connection_unverified"
        rows.append({"name": entry["name"], "execution_family": family, "status": status,
                     "python_supported": python_supported, "dependencies": packages,
                     "adapter_blockers": blockers, "resource_blockers": resource_blockers,
                     "verification": entry["verification"],
                     "live_validated": entry["live_validated"], "configuration_and_credentials_checked": False})
    return {"spec": "contextbase.connector-readiness/v1", "count": len(rows), "connectors": rows,
            "source_calls": 0, "credential_reads": 0, "dependency_installations": 0,
            "scope": "Local package metadata and adapter declarations; package presence does not verify connection, credential scope, or vendor compatibility."}
