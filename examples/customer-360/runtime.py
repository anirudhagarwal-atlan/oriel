"""Compatibility entrypoint for real local SQLite sample extraction."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from contextbase.customer360 import samples
from contextbase.customer360.samples import _canonical, _hash, _now, prerequisites, _read, _quoted, _normalized, _record_id, _fixtures, _columns, _value, _write, _lock, _source_database, _sync, _reconcile

ROOT = Path(__file__).resolve().parent
SPEC = samples.SPEC


def load_profile(workspace, profile_name, phase="initial", *, fixture_root=None):
    return samples.load_profile(workspace, profile_name, phase,
                                fixture_root=ROOT if fixture_root is None else fixture_root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--profile", required=True, choices=["salesforce-gong", "billing-support"])
    parser.add_argument("--phase", default="initial", choices=["initial", "refreshed"])
    args = parser.parse_args()
    try:
        from contextbase.workspace import Workspace
        result = load_profile(Workspace(args.workspace), args.profile, args.phase)
        print(json.dumps({key: result[key] for key in ("status", "profile", "phase", "receipt_path", "scope", "vendor_adapter_verified")}))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
        print(json.dumps({"status": "failed", "error": "Fixture extraction failed; inspect the private receipt."}), file=sys.stderr)
        raise SystemExit(1)
