"""Fictional local bundles test intake integrity, NEVER actual live evidence.

All vendor, authorization, permission and source-origin declarations come from
SyntheticContract. Existing real files, credentials, providers and vendors are
never accessed. A successful test still reports live_gate_passed=False.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.customer360 import live_bundle
from tests.test_customer360_live_contract import SyntheticContract, stamp


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


class LiveBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.bundle = self.root / "evidence"
        self.review = self.root / "separately-reviewed"
        for directory in (self.bundle, self.review):
            directory.mkdir(mode=0o700)
            (directory / "artifacts").mkdir(mode=0o700)
        self.capability_file = self.review / "capabilities.json"
        fixture = SyntheticContract()
        self.fixture = fixture
        capability_keys = {item["evidence_sha256"] for item in fixture.adapters.values()}
        cap_evidence = {key: value for key, value in fixture.evidence.items() if key in capability_keys}
        extraction_evidence = {key: value for key, value in fixture.evidence.items() if key not in capability_keys}
        main_artifacts, cap_artifacts = [], []
        for index, (key, proof) in enumerate(fixture.evidence.items()):
            # These bytes are explicitly fictional artifacts already named by
            # the independent parser fixture, not scraped or live proof files.
            raw = encoded(["invented-artifact", index])
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(digest, proof["artifact_sha256"])
            directory, references = (self.review, cap_artifacts) if key in capability_keys else (self.bundle, main_artifacts)
            reference = write(directory / "artifacts" / (digest + ".bin"), raw)
            reference["file"] = "artifacts/" + reference["file"]
            references.append(reference)
        documents = {"contract": fixture.contract, "evidence": extraction_evidence,
                     "extractions": fixture.receipts, "references": fixture.references}
        self.manifest = {"spec": live_bundle.BUNDLE_SPEC,
                         "documents": {key: write(self.bundle / (key + ".json"), encoded(value))
                                       for key, value in documents.items()}, "proof_artifacts": main_artifacts}
        self.capabilities = {"spec": live_bundle.CAPABILITY_SPEC, "capabilities": fixture.adapters,
                             "evidence": cap_evidence, "proof_artifacts": cap_artifacts}
        self.freeze()

    def freeze(self):
        write(self.bundle / "bundle.json", encoded(self.manifest))
        self.capability_sha256 = write(self.capability_file, encoded(self.capabilities))["sha256"]

    def inspect(self, **overrides):
        arguments = {"capability_file": self.capability_file, "capability_sha256": self.capability_sha256,
                     "checked_at": stamp("12:00:00")}
        arguments.update(overrides)
        return live_bundle.inspect_bundle(self.bundle, **arguments)

    def assertFails(self, code=None, **overrides):
        with self.assertRaises(live_bundle.LiveBundleError) as caught:
            self.inspect(**overrides)
        self.assertEqual(str(caught.exception), caught.exception.code)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn("Synthetic", str(caught.exception))
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_fictional_bundle_integrity_pass_is_conditional_never_live_or_authenticated(self):
        report = self.inspect()
        self.assertEqual(report["status"], "conditionally_reconciled")
        self.assertTrue(report["integrity_verified"])
        self.assertTrue(report["conditional_reconciliation_passed"])
        self.assertFalse(report["live_gate_passed"])
        self.assertFalse(report["execution_authorized"])
        self.assertEqual(report["origin_authentication"], "operator_assertion_only")
        self.assertEqual((report["network_calls"], report["model_calls"]), (0, 0))
        self.assertEqual(report["capability_sha256"], self.capability_sha256)
        self.assertEqual(report["bundle_sha256"], hashlib.sha256((self.bundle / "bundle.json").read_bytes()).hexdigest())
        self.assertEqual(report["counts"]["extractions"], 6)
        self.assertEqual(report["counts"]["references"], 6)
        self.assertEqual(report["counts"]["proof_artifacts"], len(self.fixture.evidence))
        self.assertEqual(report["counts"]["files"], len(self.fixture.evidence) + 6)
        self.assertEqual(report["counts"]["total_bytes"], sum(item["bytes"] for item in report["files"]))
        serialized = json.dumps(report)
        for excluded in ("customer_a", "synthetic-tenant-ref", "Synthetic observed statement", "10.01", '"rows":', str(self.root)):
            self.assertNotIn(excluded, serialized)
        self.assertEqual(report, self.inspect())

    def test_actual_byte_hashes_not_only_semantic_json_hashes_are_checked(self):
        contract = self.bundle / "contract.json"
        contract.write_bytes(contract.read_bytes() + b" ")
        self.manifest["documents"]["contract"]["bytes"] += 1
        self.freeze()
        self.assertFails("bundle_hash_mismatch")

    def test_capability_pin_is_external_and_cannot_be_chosen_by_main_manifest(self):
        self.assertFails("bundle_hash_mismatch", capability_sha256="0" * 64)
        self.manifest["capabilities"] = str(self.capability_file)
        self.freeze()
        self.assertFails("invalid_bundle_manifest")

    def test_capability_file_inside_evidence_bundle_is_refused(self):
        inside = self.bundle / "capabilities.json"
        write(inside, self.capability_file.read_bytes())
        self.assertFails("capability_input_not_independent", capability_file=inside)

    def test_secret_basenames_refused_before_any_file_open(self):
        for name in (".env", ".env.test", "credentials.json", ".netrc", ".npmrc", "auth.json", "private-key.pem"):
            with self.subTest(name=name), patch.object(live_bundle.os, "open", side_effect=AssertionError("must not open")):
                self.assertFails("unsafe_capability_filename", capability_file=self.review / name)

    def test_missing_posix_controls_fail_before_any_file_open(self):
        with patch.object(live_bundle.os, "O_NOFOLLOW", None), \
                patch.object(live_bundle.os, "open", side_effect=AssertionError("must not open")):
            self.assertFails("unsupported_bundle_platform")

    def test_documents_and_artifacts_cannot_escape_fixed_names(self):
        original = copy.deepcopy(self.manifest)
        for path in ("../contract.json", "/contract.json", "nested/contract.json", "https://example.invalid/contract.json"):
            self.manifest = copy.deepcopy(original)
            self.manifest["documents"]["contract"]["file"] = path
            self.freeze()
            with self.subTest(path=path):
                self.assertFails("unsafe_bundle_path")
        self.manifest = copy.deepcopy(original)
        self.manifest["proof_artifacts"][0]["file"] = "artifacts/../contract.json"
        self.freeze()
        self.assertFails("unsafe_bundle_path")

    def test_missing_tampered_or_unlisted_proof_bytes_cannot_pass(self):
        reference = self.manifest["proof_artifacts"][0]
        path = self.bundle / reference["file"]
        original = path.read_bytes()
        path.unlink()
        self.assertFails("bundle_unavailable_or_invalid")
        write(path, b"x" * len(original))
        self.assertFails("bundle_hash_mismatch")
        write(path, original)
        self.manifest["proof_artifacts"].pop(0)
        self.freeze()
        self.assertFails("proof_artifact_coverage_mismatch")

    def test_capability_proof_artifact_is_also_verified(self):
        reference = self.capabilities["proof_artifacts"][0]
        path = self.review / reference["file"]
        original = path.read_bytes()
        path.write_bytes(b"x" * len(original))
        self.assertFails("bundle_hash_mismatch")

    def test_extra_or_duplicate_artifact_declarations_are_refused(self):
        self.manifest["proof_artifacts"].append(copy.deepcopy(self.manifest["proof_artifacts"][0]))
        self.freeze()
        self.assertFails("duplicate_proof_artifact")
        self.manifest["proof_artifacts"].pop()
        raw = b"fictional unreferenced artifact"
        digest = hashlib.sha256(raw).hexdigest()
        self.manifest["proof_artifacts"].append({"file": "artifacts/" + digest + ".bin", "sha256": digest, "bytes": len(raw)})
        self.freeze()
        self.assertFails("proof_artifact_coverage_mismatch")

    def test_strict_json_duplicate_keys_floats_nonfinite_depth_and_integer_bounds(self):
        path = self.bundle / "contract.json"
        for raw in (b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":1e3}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'[' * 14 + b'0' + b']' * 14, b'{"x":' + b'1' * 201 + b'}', b'\xff'):
            self.manifest["documents"]["contract"] = write(path, raw)
            self.freeze()
            with self.subTest(case=raw[:12]):
                self.assertFails("bundle_json_invalid")

    def test_file_byte_size_caps_apply_before_decode(self):
        with self.capability_file.open("r+b") as handle:
            handle.truncate(live_bundle.MAX_CAPABILITY_BYTES + 1)
        with patch.object(live_bundle.json, "loads", side_effect=AssertionError("must not decode")):
            self.assertFails("bundle_limit_exceeded")

    def test_total_bytes_artifact_counts_and_cooperative_deadline_are_bounded(self):
        with patch.object(live_bundle, "MAX_TOTAL_BYTES", 1):
            self.assertFails("bundle_limit_exceeded")
        with patch.object(live_bundle, "MAX_PROOF_ARTIFACTS", 18):
            self.assertFails("bundle_limit_exceeded")
        with patch.object(live_bundle.time, "monotonic", side_effect=[0.0, 11.0]):
            self.assertFails("bundle_deadline_exceeded")

    def test_private_modes_and_owner_checks_do_not_silently_chmod(self):
        self.bundle.chmod(0o755)
        self.assertFails("unsafe_bundle_path")
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o755)
        self.bundle.chmod(0o700)
        self.capability_file.chmod(0o644)
        self.assertFails("unsafe_bundle_path")
        self.capability_file.chmod(0o600)
        (self.bundle / "artifacts").chmod(0o755)
        self.assertFails("unsafe_bundle_path")
        (self.bundle / "artifacts").chmod(0o700)
        with patch.object(live_bundle.os, "getuid", return_value=os.getuid() + 1):
            self.assertFails("unsafe_bundle_path")

    def test_symlink_hardlink_fifo_and_symlink_root_are_refused(self):
        path = self.bundle / "contract.json"
        original = path.read_bytes()
        real = self.root / "synthetic-target.json"
        write(real, original)
        path.unlink()
        path.symlink_to(real)
        self.assertFails("bundle_unavailable_or_invalid")
        path.unlink()
        os.link(real, path)
        self.assertFails("unsafe_bundle_path")
        path.unlink()
        os.mkfifo(path, mode=0o600)
        self.assertFails("unsafe_bundle_path")
        path.unlink()
        write(path, original)
        alias = self.root / "alias"
        alias.symlink_to(self.bundle, target_is_directory=True)
        with self.assertRaises(live_bundle.LiveBundleError):
            live_bundle.inspect_bundle(alias, capability_file=self.capability_file,
                capability_sha256=self.capability_sha256, checked_at=stamp("12:00:00"))

    def test_valid_byte_hashes_cannot_make_inconsistent_source_rows_reconcile(self):
        rows = copy.deepcopy(self.fixture.receipts)
        rows[0]["rows"][0]["values"]["amount"] = "999.00"
        self.manifest["documents"]["extractions"] = write(self.bundle / "extractions.json", encoded(rows))
        self.freeze()
        self.assertFails("conditional_reconciliation_failed")
        self.assertFails(checked_at=stamp("14:00:00"))

    def test_fixed_errors_do_not_echo_os_paths_or_raw_failure_messages(self):
        with patch.object(live_bundle.os, "read", side_effect=OSError("synthetic-private-diagnostic-do-not-return")):
            self.assertFails("bundle_unavailable_or_invalid")


if __name__ == "__main__":
    unittest.main()
