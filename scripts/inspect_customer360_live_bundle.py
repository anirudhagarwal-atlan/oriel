#!/usr/bin/env python3
"""Check privately retained live-evidence files without granting execution.

This repository-only inspection command makes no vendor or model calls. A zero
exit status means file integrity and conditional reconciliation succeeded; it
does not pass the live acceptance gate or authenticate submitted evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Argparse normally echoes unrecognized arguments, which could contain
        # mistakenly pasted source data. Keep failures free of submitted values.
        raise UsageError("invalid_inspection_arguments")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path,
                        help="Private directory containing bundle.json and declared evidence files.")
    parser.add_argument("--capabilities", required=True, type=Path,
                        help="Separately reviewed private capabilities.json outside the evidence bundle.")
    parser.add_argument("--capability-sha256", required=True,
                        help="Expected SHA256 of the exact separately selected capability file bytes.")
    parser.add_argument("--checked-at", help="UTC verification time; defaults to the actual current time.")
    try:
        args = parser.parse_args(argv)
    except UsageError:
        print(json.dumps({"status": "refused", "error_code": "invalid_inspection_arguments"}), file=sys.stderr)
        return 2
    from tests.customer360.live_bundle import LiveBundleError, inspect_bundle
    checked_at = args.checked_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        result = inspect_bundle(args.bundle, capability_file=args.capabilities,
                                capability_sha256=args.capability_sha256, checked_at=checked_at)
    except LiveBundleError as error:
        print(json.dumps({"status": "refused", "error_code": error.code,
                          "live_gate_passed": False, "execution_authorized": False}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "refused", "error_code": "bundle_inspection_failed",
                          "live_gate_passed": False, "execution_authorized": False}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
