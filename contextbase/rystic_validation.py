"""Observe complete local simulator source state before certifying a refusal.

This module uses the existing pinned Rystic pair APIs. It does not import data,
write sources, consult seed intent, or use task gold. A complete blocked receipt
is evidence of the two supported customer metadata defects, not a successful
workspace snapshot. Transport failures and unsupported schema errors remain
unverified, without a data hash or confident blocker list.

``capture_validation(pair, namespace)`` returns ``receipt``, ``tables`` and
``observed_source``. Only valid results include importable tables. The returned
raw projection is for explicit local verification; callers may persist just
the receipt. No error response bodies or exception messages are exposed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from .rystic import ISSUE_FIELDS, _normalize_snapshot, linear_issues, stripe_pages


NORMALIZED_HASH_KIND = "normalized_tables/v1"
OBSERVED_HASH_KIND = "observed_cohort_projection/v1"


def _hash(value):
    # Same encoding as workspace.canonical for valid normalized JSON, without
    # its display-oriented coercion of non-finite values or non-JSON objects.
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _cohort(customers, charges, issues, namespace):
    """Retain all fields actually observed for the explicitly scoped records."""
    marked_customers = []
    for item in customers:
        metadata = item.get("metadata") or {}
        if metadata.get("contextbase_fixture") == namespace:
            marked_customers.append(item)
    customer_ids = {item["id"] for item in marked_customers}
    marked_charges = [item for item in charges if item.get("customer") in customer_ids]
    marked_issues = []
    for item in issues:
        markers = re.findall(r"^contextbase_fixture=([a-z0-9_]+)$", item.get("description") or "", re.MULTILINE)
        if namespace in markers:
            marked_issues.append(item)
    return {"spec": OBSERVED_HASH_KIND, "namespace": namespace,
            "projection": {"stripe.customers": "Complete returned objects for marked customers",
                           "stripe.charges": "Complete returned objects whose customer ID is in the marked cohort",
                           "linear.issues": ISSUE_FIELDS},
            "rows": {name: sorted(deepcopy(rows), key=lambda item: item["id"])
                     for name, rows in (("stripe.customers", marked_customers),
                                        ("stripe.charges", marked_charges), ("linear.issues", marked_issues))}}


def _customer_blockers(customers):
    """Diagnose observed metadata, independently of any authored seed oracle."""
    identities, blockers = {}, []
    for customer in customers:
        metadata = customer["metadata"]
        identities.setdefault(metadata["account_id"], []).append(customer["id"])
        if metadata.get("is_internal") is None:
            blockers.append({"source": "stripe.customers", "record_id": customer["id"],
                             "field": "metadata.is_internal", "reason": "missing_required_input"})
    for customer_ids in identities.values():
        if len(customer_ids) > 1:
            blockers.extend({"source": "stripe.customers", "record_id": customer_id,
                             "field": "metadata.account_id", "reason": "ambiguous_identity"}
                            for customer_id in customer_ids)
    return sorted(blockers, key=lambda item: (item["source"], item["record_id"], item["field"], item["reason"]))


def capture_validation(pair, namespace):
    """Read full source pages, then return a valid, blocked or unverified state.

    Valid ``data_sha256`` matches the existing normalized snapshot hash exactly.
    Blocked ``data_sha256`` hashes ``observed_source`` instead: the complete
    marked projection, including unaffected records and observed issue mappings.
    ``observed_source_sha256`` always identifies that raw representation when
    validation is complete. ``hash_kind`` prevents the two hash meanings being
    silently substituted. Hashes identify observed state; they are not signatures.

    Duplicate customer mappings may make otherwise well-formed issue links
    unresolved. Such links are preserved, not guessed or reparented. All other
    schema checks still run before a supported refusal is certified. Independent
    source reads require a quiescent simulator; this is not a transactional
    cross-service snapshot or continuous freshness monitor.
    """
    if type(namespace) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", namespace):
        raise ValueError("Invalid synthetic fixture namespace.")
    started = time.perf_counter()
    receipt = {"receipt_kind": "source_state_validation", "namespace": namespace,
               "validation_status": "unverified", "complete": False, "blockers": [],
               "workspace_imported": False, "synthetic": True,
               "consistency": "Sequential complete source reads; use quiescent simulators. No cross-service transaction is claimed."}
    try:
        identities = pair.verify()
        # Read all three collections before attempting even supported refusal.
        # Pagination helpers require complete terminal pages and unique IDs.
        customers = stripe_pages(pair.stripe, "customers")
        charges = stripe_pages(pair.stripe, "charges")
        issues = linear_issues(pair.linear)
        observed = _cohort(customers, charges, issues, namespace)
        tables = _normalize_snapshot(customers, charges, issues, namespace, _customer_guard_mode=True)
        blockers = _customer_blockers(observed["rows"]["stripe.customers"])
        raw_hash = _hash(observed)
        # Verifying both hashes also rejects non-finite values in observed data.
        data_hash = raw_hash if blockers else _hash(tables)
        receipt.update({"validation_status": "blocked" if blockers else "valid", "complete": True,
                        "blockers": blockers, "simulators": identities,
                        "hash_kind": OBSERVED_HASH_KIND if blockers else NORMALIZED_HASH_KIND,
                        "data_sha256": data_hash, "observed_source_sha256": raw_hash,
                        "snapshot_id": ("source-state-" if blockers else "rystic-") + data_hash[:16],
                        "source_rows": {name: len(rows) for name, rows in observed["rows"].items()}})
        result = {"receipt": receipt, "tables": None if blockers else tables, "observed_source": observed}
    except Exception as exc:
        # Do not turn a malformed/unavailable source into evidence that the
        # supported customer defect was the only reason analysis was refused.
        receipt.update({"error_type": type(exc).__name__,
                        "reason": "Source read or schema validation was incomplete; no import or supported refusal is certified."})
        result = {"receipt": receipt, "tables": None, "observed_source": None}
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    receipt["source_read_seconds"] = round(time.perf_counter() - started, 6)
    return result
