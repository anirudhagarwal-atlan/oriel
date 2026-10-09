"""A second consumer: execute exported context against separately imported data.

No question, SQL template, business policy, oracle, approved-context installation,
model call or registry connection is supplied to this process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from contextbase.workspace import Workspace


def reuse(bundle: Path, workspace: Path, crm: Path, mapping: Path, events: Path):
    bundle = bundle.resolve(strict=True)
    if workspace.exists():
        raise ValueError("Use a new workspace for the independent consumer.")
    manifest = json.loads((bundle / "manifest.json").read_text())
    for item in manifest["files"]:
        path = (bundle / item["path"]).resolve(strict=True)
        if not path.is_relative_to(bundle) or not path.is_file():
            raise ValueError("Bundle file must stay inside its directory.")
        content = path.read_bytes()
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Bundle content does not match its manifest.")
    context = json.loads((bundle / "context.json").read_text())
    metrics = context["metrics"]
    if len(metrics) != 1 or metrics[0]["status"] != "approved":
        raise ValueError("This fixture expects one locally approved metric.")
    metric = metrics[0]
    registry = json.loads((bundle / "metrics" / (metric["name"] + ".json")).read_text())
    w = Workspace(workspace)
    imports = [w.ingest(crm, "sales", "seats"), w.ingest(mapping, "directory", "workspace_map"),
               w.ingest(events, "telemetry", "events")]
    result = w.query(metric["sql"])
    definition = registry["definition"]
    # The portable registry formula must compute the same answer independently
    # of the raw context SQL representation.
    portable = w.query("SELECT " + definition["formula"] + " AS portable_value FROM (" + definition["source"]["sql"] + ") s")
    if result["rows"][0][result["columns"][0]] != portable["rows"][0]["portable_value"]:
        raise ValueError("Export representations disagree.")
    return {"synthetic": True, "context_version": context["context_version"], "metric_version": metric["version"],
            "rows": result["rows"], "portable_rows": portable["rows"], "verified_bundle_files": len(manifest["files"]),
            "imports": [{key: item[key] for key in ("table", "rows", "sha256")} for item in imports],
            "has_local_approved_context": (w.root / "context" / "approved.json").exists(),
            "model_calls": 0, "registry_upload": "not_performed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "workspace", "crm", "mapping", "events"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(reuse(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
