"""Complete source validation using explicit fake pairs; no runtime or model."""
from copy import deepcopy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from contextbase.rystic import collect_snapshot, normalize_snapshot, sync_snapshot
from contextbase.rystic_recipe import NAMESPACE
from contextbase.rystic_validation import (
    NORMALIZED_HASH_KIND, OBSERVED_HASH_KIND, capture_validation,
)
from contextbase.workspace import Workspace, canonical
from test_rystic import SnapshotPair, issue_page


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def duplicate_mapping(pair, with_orphan=True):
    pair.customers[0]["metadata"]["account_id"] = pair.customers[1]["metadata"]["account_id"]
    if with_orphan:
        original = deepcopy(pair.issues[0])
        original.update(id="issue_002", identifier="SYN-2",
                        description=f"contextbase_account_id=customer_002\ncontextbase_fixture={NAMESPACE}")
        pair.issues.append(original)


class PagedStripe:
    def __init__(self, pair):
        self.pair, self.calls = pair, []

    def request(self, method, path, query=None):
        self.calls.append((method, path, deepcopy(query)))
        rows = getattr(self.pair, path.rsplit("/", 1)[1])
        offset = 0
        if query.get("starting_after"):
            offset = next(index + 1 for index, item in enumerate(rows) if item["id"] == query["starting_after"])
        return {"data": deepcopy(rows[offset:offset + 1]), "has_more": offset + 1 < len(rows)}


class PagedLinear:
    def __init__(self, pair):
        self.pair, self.calls = pair, []

    def graphql(self, query, variables=None):
        self.calls.append((query, deepcopy(variables)))
        offset = int(variables.get("after") or 0)
        return issue_page(deepcopy(self.pair.issues[offset:offset + 1]),
                          more=offset + 1 < len(self.pair.issues), cursor=str(offset + 1))


class RysticValidationTests(unittest.TestCase):
    def test_valid_capture_hash_matches_existing_normalization_and_import(self):
        pair = SnapshotPair()
        tables, _ = collect_snapshot(pair, NAMESPACE)
        self.assertEqual(normalize_snapshot(pair.customers, pair.charges, pair.issues, NAMESPACE), tables)
        capture = capture_validation(pair, NAMESPACE)
        receipt = capture["receipt"]
        self.assertEqual(receipt["validation_status"], "valid")
        self.assertTrue(receipt["complete"])
        self.assertFalse(receipt["workspace_imported"])
        self.assertEqual(receipt["receipt_kind"], "source_state_validation")
        self.assertEqual(receipt["hash_kind"], NORMALIZED_HASH_KIND)
        self.assertEqual(receipt["blockers"], [])
        self.assertEqual(capture["tables"], tables)
        self.assertEqual(receipt["data_sha256"], digest(tables))
        self.assertEqual(receipt["observed_source_sha256"], digest(capture["observed_source"]))
        with tempfile.TemporaryDirectory() as directory:
            imported = sync_snapshot(Workspace(directory), pair, NAMESPACE)
            self.assertEqual(receipt["data_sha256"], imported["data_sha256"])
            self.assertEqual(receipt["snapshot_id"], imported["snapshot_id"])
        self.assertNotIn("refresh_mode", receipt)

    def test_strict_collect_retains_early_policy_and_identity_refusal(self):
        for defect in ("missing_policy", "duplicate_mapping"):
            pair = SnapshotPair()
            if defect == "missing_policy":
                del pair.customers[1]["metadata"]["is_internal"]
            else:
                duplicate_mapping(pair)
            with self.assertRaises(ValueError):
                collect_snapshot(pair, NAMESPACE)
            self.assertEqual([path for _, path, _ in pair.stripe.calls], ["/v1/customers"])
            self.assertEqual(pair.linear.calls, [])

    def test_duplicate_identity_blocks_only_after_complete_read_and_preserves_issue_mapping(self):
        pair = SnapshotPair()
        duplicate_mapping(pair)
        original = deepcopy(pair.issues[-1])
        capture = capture_validation(pair, NAMESPACE)
        receipt = capture["receipt"]
        self.assertEqual(receipt["validation_status"], "blocked")
        self.assertTrue(receipt["complete"])
        self.assertIsNone(capture["tables"])
        self.assertEqual(receipt["hash_kind"], OBSERVED_HASH_KIND)
        self.assertEqual(receipt["data_sha256"], digest(capture["observed_source"]))
        self.assertEqual(receipt["data_sha256"], receipt["observed_source_sha256"])
        self.assertEqual(receipt["blockers"], [
            {"source": "stripe.customers", "record_id": "cus_001", "field": "metadata.account_id", "reason": "ambiguous_identity"},
            {"source": "stripe.customers", "record_id": "cus_002", "field": "metadata.account_id", "reason": "ambiguous_identity"},
        ])
        self.assertEqual([path for _, path, _ in pair.stripe.calls], ["/v1/customers", "/v1/charges"])
        self.assertEqual(len(pair.linear.calls), 1)
        observed_issue = next(row for row in capture["observed_source"]["rows"]["linear.issues"] if row["id"] == "issue_002")
        self.assertEqual(observed_issue, original)
        self.assertEqual(pair.issues[-1], original)

    def test_absent_and_null_customer_policy_produce_exact_supported_blockers(self):
        for missing in (True, False):
            pair = SnapshotPair()
            if missing:
                del pair.customers[1]["metadata"]["is_internal"]
            else:
                pair.customers[1]["metadata"]["is_internal"] = None
            capture = capture_validation(pair, NAMESPACE)
            self.assertEqual(capture["receipt"]["validation_status"], "blocked")
            self.assertEqual(capture["receipt"]["blockers"], [{
                "source": "stripe.customers", "record_id": "cus_001", "field": "metadata.is_internal", "reason": "missing_required_input",
            }])
            self.assertIsNone(capture["tables"])

    def test_complete_blocked_hash_includes_unaffected_scoped_records_and_excludes_other_worlds(self):
        pair = SnapshotPair()
        del pair.customers[1]["metadata"]["is_internal"]
        before = capture_validation(pair, NAMESPACE)
        pair.charges[0]["amount"] += 1  # EUR/internal rows still belong to the complete cohort.
        after = capture_validation(pair, NAMESPACE)
        self.assertNotEqual(before["receipt"]["data_sha256"], after["receipt"]["data_sha256"])
        pair.charges[2]["amount"] += 99  # Unrelated simulator world is outside this cohort.
        unrelated_change = capture_validation(pair, NAMESPACE)
        self.assertEqual(after["receipt"]["data_sha256"], unrelated_change["receipt"]["data_sha256"])
        raw = json.dumps(after["observed_source"])
        self.assertNotIn("cus_unrelated", raw)
        self.assertNotIn("issue_unrelated", raw)
        self.assertEqual(after["receipt"]["source_rows"], {"stripe.customers": 2, "stripe.charges": 2, "linear.issues": 1})

    def test_raw_hash_binds_full_observed_objects_while_valid_hash_retains_normalized_meaning(self):
        pair = SnapshotPair()
        before = capture_validation(pair, NAMESPACE)
        pair.customers[1]["metadata"]["synthetic_note"] = "Extra returned source field"
        after = capture_validation(pair, NAMESPACE)
        self.assertEqual(before["receipt"]["data_sha256"], after["receipt"]["data_sha256"])
        self.assertNotEqual(before["receipt"]["observed_source_sha256"], after["receipt"]["observed_source_sha256"])

    def test_schema_corruption_is_unverified_even_when_supported_blocker_is_visible(self):
        corruptions = [lambda pair: pair.charges[1].pop("paid"),
                       lambda pair: pair.charges[1].update(amount=10001.0),
                       lambda pair: pair.charges[1].update(amount_refunded=10002),
                       lambda pair: pair.issues[0]["state"].pop("type"),
                       lambda pair: pair.issues[0].pop("updatedAt"),
                       lambda pair: pair.issues[0].update(description=f"contextbase_account_id=customer_001\ncontextbase_account_id=customer_002\ncontextbase_fixture={NAMESPACE}"),
                       lambda pair: pair.customers[0]["metadata"].update(is_internal="unknown"),
                       lambda pair: pair.customers[0].update(extra=float("inf"))]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                pair = SnapshotPair()
                duplicate_mapping(pair)
                del pair.customers[1]["metadata"]["is_internal"]
                corrupt(pair)
                capture = capture_validation(pair, NAMESPACE)
                self.assertEqual(capture["receipt"]["validation_status"], "unverified")
                self.assertFalse(capture["receipt"]["complete"])
                self.assertEqual(capture["receipt"]["blockers"], [])
                self.assertNotIn("data_sha256", capture["receipt"])
                self.assertNotIn("snapshot_id", capture["receipt"])
                self.assertIsNone(capture["tables"])
                self.assertIsNone(capture["observed_source"])

    def test_orphan_issue_without_duplicate_customer_identity_is_not_supported_abstention(self):
        pair = SnapshotPair()
        del pair.customers[1]["metadata"]["is_internal"]
        pair.issues[0]["description"] = f"contextbase_account_id=missing_account\ncontextbase_fixture={NAMESPACE}"
        capture = capture_validation(pair, NAMESPACE)
        self.assertEqual(capture["receipt"]["validation_status"], "unverified")
        self.assertEqual(capture["receipt"]["blockers"], [])
        self.assertNotIn("data_sha256", capture["receipt"])

    def test_transport_or_version_failure_is_not_a_confident_refusal_and_never_leaks_error_body(self):
        for source in ("stripe", "linear", "verify"):
            pair = SnapshotPair()
            del pair.customers[1]["metadata"]["is_internal"]
            target = pair.stripe if source == "stripe" else pair.linear if source == "linear" else pair
            method = "request" if source == "stripe" else "graphql" if source == "linear" else "verify"
            with patch.object(target, method, side_effect=ValueError("synthetic_private_error_body")):
                capture = capture_validation(pair, NAMESPACE)
            self.assertEqual(capture["receipt"]["validation_status"], "unverified")
            self.assertFalse(capture["receipt"]["complete"])
            self.assertNotIn("data_sha256", capture["receipt"])
            self.assertEqual(capture["receipt"]["blockers"], [])
            self.assertNotIn("synthetic_private_error_body", json.dumps(capture))

    def test_all_pages_are_required_and_page_order_does_not_change_hash(self):
        pair = SnapshotPair()
        duplicate_mapping(pair)
        ordinary = capture_validation(pair, NAMESPACE)
        pair.stripe, pair.linear = PagedStripe(pair), PagedLinear(pair)
        paged = capture_validation(pair, NAMESPACE)
        self.assertEqual(ordinary["receipt"]["data_sha256"], paged["receipt"]["data_sha256"])
        self.assertEqual(len(pair.stripe.calls), len(pair.customers) + len(pair.charges))
        self.assertEqual(len(pair.linear.calls), len(pair.issues))
        pair.customers.reverse()
        pair.charges.reverse()
        pair.issues.reverse()
        reordered = capture_validation(pair, NAMESPACE)
        self.assertEqual(paged["receipt"]["data_sha256"], reordered["receipt"]["data_sha256"])

    def test_incomplete_pagination_does_not_certify_visible_blocker(self):
        pair = SnapshotPair()
        del pair.customers[1]["metadata"]["is_internal"]
        with patch.object(pair.linear, "graphql", return_value={"issues": {"nodes": pair.issues}}):
            capture = capture_validation(pair, NAMESPACE)
        self.assertEqual(capture["receipt"]["validation_status"], "unverified")
        self.assertNotIn("data_sha256", capture["receipt"])
        self.assertEqual(capture["receipt"]["blockers"], [])

    def test_invalid_namespace_is_rejected_before_any_source_read(self):
        pair = SnapshotPair()
        with self.assertRaises(ValueError):
            capture_validation(pair, "bad/namespace")
        self.assertEqual(pair.verifications, 0)
        self.assertEqual(pair.stripe.calls, [])
        self.assertEqual(pair.linear.calls, [])


if __name__ == "__main__":
    unittest.main()
