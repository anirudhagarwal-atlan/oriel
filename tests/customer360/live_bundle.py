"""Private, bounded offline intake for conditional live-evidence declarations.

This loader executes no adapters and authenticates no claimed source origin.
It checks actual local bytes, then invokes the existing conditional checker.
It requires POSIX owner and no-follow descriptor controls; unsupported systems
are refused before opening any files.
The independently selected capability file is pinned by its supplied byte hash;
the access contract cannot choose or approve it. Proof bytes and submitted rows
are never returned. The 10-second limit is cooperative between bounded reads,
parsing and validation, not a hard cancellation guarantee for an OS file read.

Main ``bundle.json``: {spec, documents:{contract,evidence,extractions,references},
proof_artifacts:[...]}. Document references are {file,sha256,bytes}, with fixed
``<role>.json`` names. Proof names are ``artifacts/<sha256>.bin``.
The separate ``capabilities.json`` or ``reviewed-capabilities.json`` contains
{spec,capabilities,evidence,proof_artifacts}; its artifacts are relative to its
own private parent directory. Both manifests use the same reference shape.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from . import live_contract


BUNDLE_SPEC = "oriel.customer360.live-evidence-bundle/v1"
CAPABILITY_SPEC = "oriel.customer360.reviewed-capabilities/v1"
REPORT_SPEC = "oriel.customer360.live-bundle-inspection/v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CAPABILITY_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PROOF_ARTIFACTS = 128
MAX_CAPABILITIES = 64
MAX_SECONDS = 10
READ_CHUNK_BYTES = 64 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ROLES = ("contract", "evidence", "extractions", "references")


class LiveBundleError(ValueError):
    """Only fixed safe codes; never submitted values, paths or OS exceptions."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise LiveBundleError(code)


def _shape(value, keys, code="invalid_bundle_manifest"):
    _require(type(value) is dict and set(value) == set(keys.split()), code)


def _hash(value):
    _require(type(value) is str and _HASH.fullmatch(value), "invalid_bundle_hash")
    return value


def _private(info, *, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_uid == os.getuid()
             and (directory or info.st_nlink == 1), "unsafe_bundle_path")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _controls():
    _require(os.name == "posix"
             and all(type(getattr(os, name, None)) is int for name in
                     ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
             and all(callable(getattr(os, name, None)) for name in
                     ("getuid", "open", "close", "fstat", "stat", "read"))
             and os.open in getattr(os, "supports_dir_fd", ())
             and os.stat in getattr(os, "supports_dir_fd", ())
             and os.stat in getattr(os, "supports_follow_symlinks", ()),
             "unsupported_bundle_platform")


def _open_directory(path):
    parts = path.parts
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        _private(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "bundle_json_invalid")
        result[key] = value
    return result


def _integer(value):
    _require(len(value.lstrip("-")) <= 200, "bundle_json_invalid")
    return int(value)


def _no_float(_value):
    raise LiveBundleError("bundle_json_invalid")


class _Reader:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.total = 0
        self.files = []
        self.artifacts = 0

    def check(self):
        _require(time.monotonic() < self.deadline, "bundle_deadline_exceeded")

    def read(self, root, name, scope, *, maximum, digest=None, expected_bytes=None, decode=True):
        self.check()
        parent, close_parent, descriptor = root, False, None
        try:
            if name.startswith("artifacts/"):
                parent = os.open("artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                close_parent = True
                _private(os.fstat(parent), directory=True)
                filename = name.removeprefix("artifacts/")
            else:
                filename = name
            _require("/" not in filename and filename not in ("", ".", ".."), "unsafe_bundle_path")
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(descriptor)
            _private(info)
            _require(info.st_size <= maximum and self.total + info.st_size <= MAX_TOTAL_BYTES, "bundle_limit_exceeded")
            if expected_bytes is not None:
                _require(info.st_size == expected_bytes, "bundle_size_mismatch")
            chunks, consumed, sha = [], 0, hashlib.sha256()
            while True:
                self.check()
                chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum + 1 - consumed))
                if not chunk:
                    break
                consumed += len(chunk)
                self.total += len(chunk)
                _require(consumed <= maximum and self.total <= MAX_TOTAL_BYTES, "bundle_limit_exceeded")
                sha.update(chunk)
                if decode:
                    chunks.append(chunk)
            _require(consumed == info.st_size, "bundle_file_changed")
            after = os.fstat(descriptor)
            current = os.stat(filename, dir_fd=parent, follow_symlinks=False)
            _require(_identity(info) == _identity(after) == _identity(current), "bundle_file_changed")
            actual = sha.hexdigest()
            _require(digest is None or actual == digest, "bundle_hash_mismatch")
            self.files.append({"scope": scope, "name": name, "sha256": actual, "bytes": consumed})
            self.check()
            if not decode:
                return None, actual
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_pairs,
                                   parse_float=_no_float, parse_constant=_no_float, parse_int=_integer)
                live_contract._json(value)
            except LiveBundleError:
                raise
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                raise LiveBundleError("bundle_json_invalid") from None
            self.check()
            return value, actual
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if close_parent:
                os.close(parent)


def _reference(value, filename, maximum):
    _shape(value, "file sha256 bytes")
    _require(value["file"] == filename, "unsafe_bundle_path")
    _hash(value["sha256"])
    _require(type(value["bytes"]) is int and 0 <= value["bytes"] <= maximum, "bundle_limit_exceeded")
    return value


def _proofs(evidence, *, capability=False):
    _require(type(evidence) is dict and 0 < len(evidence) <= MAX_PROOF_ARTIFACTS, "invalid_bundle_evidence")
    artifacts = set()
    for key, proof in evidence.items():
        _hash(key)
        _shape(proof, "kind origin subject_sha256 recorded_at producer_ref artifact_sha256 derived_from_sha256",
               "invalid_bundle_evidence")
        _hash(proof["subject_sha256"])
        allowed = {"capability_review"} if capability else {"authorization", "scope_enforcement", "extraction", "reference"}
        _require(type(proof["kind"]) is str and proof["kind"] in allowed, "invalid_bundle_evidence")
        origin = ("controlled_runtime_verification" if capability else
                  "operator_assertion" if proof["kind"] == "authorization" else "authenticated_vendor")
        live_contract._proof(evidence, key, kind=proof["kind"], origin=origin, expected_subject=proof["subject_sha256"])
        artifacts.add(_hash(proof["artifact_sha256"]))
        artifacts.update(proof["derived_from_sha256"])
    return artifacts


def _artifacts(reader, root, declared, expected, scope):
    _require(type(declared) is list and len(declared) <= MAX_PROOF_ARTIFACTS, "invalid_bundle_manifest")
    seen = set()
    for reference in declared:
        _require(type(reference) is dict and "sha256" in reference, "invalid_bundle_manifest")
        digest = _hash(reference["sha256"])
        _reference(reference, "artifacts/" + digest + ".bin", MAX_ARTIFACT_BYTES)
        _require(digest not in seen, "duplicate_proof_artifact")
        seen.add(digest)
    _require(seen == expected, "proof_artifact_coverage_mismatch")
    reader.artifacts += len(declared)
    _require(reader.artifacts <= MAX_PROOF_ARTIFACTS, "bundle_limit_exceeded")
    for reference in sorted(declared, key=lambda item: item["file"]):
        reader.read(root, reference["file"], scope, maximum=MAX_ARTIFACT_BYTES,
                    digest=reference["sha256"], expected_bytes=reference["bytes"], decode=False)


def inspect_bundle(bundle_dir, *, capability_file, capability_sha256, checked_at):
    """Verify private local bytes and return only a sanitized conditional report.

    Capability selection/hash comes from the caller, never the access manifest.
    Only filenames capabilities.json/reviewed-capabilities.json are accepted.
    The capability file must live outside the evidence bundle. Every submitted
    proof artifact (including declared artifact lineage) is present and hashed.
    Extra unreferenced artifact declarations are refused. Unlisted local files
    are not accessed. Existing read-only files are not chmodded or modified.
    """
    directories = []
    try:
        _hash(capability_sha256)
        bundle = Path(os.path.abspath(os.fspath(bundle_dir)))
        capability = Path(os.path.abspath(os.fspath(capability_file)))
        _require(capability.name in {"capabilities.json", "reviewed-capabilities.json"}, "unsafe_capability_filename")
        _require(not capability.is_relative_to(bundle), "capability_input_not_independent")
        _controls()
        reader = _Reader()
        bundle_fd = _open_directory(bundle)
        directories.append(bundle_fd)
        capability_fd = _open_directory(capability.parent)
        directories.append(capability_fd)
        bundle_identity = os.fstat(bundle_fd)
        capability_identity = os.fstat(capability_fd)
        _require((bundle_identity.st_dev, bundle_identity.st_ino) !=
                 (capability_identity.st_dev, capability_identity.st_ino), "capability_input_not_independent")
        capabilities, cap_hash = reader.read(capability_fd, capability.name, "reviewed_capabilities",
            maximum=MAX_CAPABILITY_BYTES, digest=capability_sha256)
        _shape(capabilities, "spec capabilities evidence proof_artifacts")
        _require(capabilities["spec"] == CAPABILITY_SPEC and type(capabilities["capabilities"]) is dict
                 and 0 < len(capabilities["capabilities"]) <= MAX_CAPABILITIES, "invalid_capability_bundle")
        for key, item in capabilities["capabilities"].items():
            _hash(key)
            _require(type(item) is dict and live_contract.digest(item) == key, "invalid_capability_bundle")
        cap_artifacts = _proofs(capabilities["evidence"], capability=True)
        required_reviews = {item.get("evidence_sha256") for item in capabilities["capabilities"].values()}
        _require(required_reviews == set(capabilities["evidence"]), "capability_review_coverage_mismatch")
        _artifacts(reader, capability_fd, capabilities["proof_artifacts"], cap_artifacts, "reviewed_capabilities")
        manifest, manifest_hash = reader.read(bundle_fd, "bundle.json", "evidence_bundle", maximum=MAX_MANIFEST_BYTES)
        _shape(manifest, "spec documents proof_artifacts")
        _require(manifest["spec"] == BUNDLE_SPEC, "invalid_bundle_manifest")
        _shape(manifest["documents"], " ".join(_ROLES))
        documents = {}
        for role in _ROLES:
            reference = _reference(manifest["documents"][role], role + ".json", MAX_DOCUMENT_BYTES)
            documents[role], _ = reader.read(bundle_fd, reference["file"], "evidence_bundle", maximum=MAX_DOCUMENT_BYTES,
                digest=reference["sha256"], expected_bytes=reference["bytes"])
        artifacts = _proofs(documents["evidence"])
        _require(not set(documents["evidence"]) & set(capabilities["evidence"]), "overlapping_evidence_authority")
        _artifacts(reader, bundle_fd, manifest["proof_artifacts"], artifacts, "evidence_bundle")
        reader.check()
        try:
            reconciled = live_contract.reconcile_receipts(documents["contract"], documents["extractions"], documents["references"],
                adapter_capabilities=capabilities["capabilities"],
                evidence={**capabilities["evidence"], **documents["evidence"]}, checked_at=checked_at)
        except live_contract.LiveContractError:
            raise LiveBundleError("conditional_reconciliation_failed") from None
        reader.check()
        # Recheck named roots as well as open descriptors before reporting; a
        # renamed/replaced root cannot be attributed to the supplied path.
        for path, initial in ((bundle, bundle_identity), (capability.parent, capability_identity)):
            current = os.stat(path, follow_symlinks=False)
            _private(current, directory=True)
            _require((initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino), "bundle_file_changed")
        return {"spec": REPORT_SPEC, "status": "conditionally_reconciled", "bundle_sha256": manifest_hash,
                "capability_sha256": cap_hash, "checked_at": checked_at, "integrity_verified": True,
                "conditional_reconciliation_passed": True, "live_gate_passed": False, "execution_authorized": False,
                "origin_authentication": "operator_assertion_only", "network_calls": 0, "model_calls": 0,
                "counts": {"files": len(reader.files), "proof_artifacts": reader.artifacts, "total_bytes": reader.total,
                           "extractions": len(documents["extractions"]), "references": len(documents["references"])},
                "files": reader.files, "reconciliation_sha256": live_contract.digest(reconciled),
                "limitations": [*live_contract.LIMITATIONS,
                    "Local byte integrity does not authenticate operator declarations, artifacts or vendor origin.",
                    "The deadline is cooperative between bounded operations; it cannot interrupt a blocked OS file read."]}
    except LiveBundleError:
        raise
    except (OSError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise LiveBundleError("bundle_unavailable_or_invalid") from None
    finally:
        for descriptor in directories:
            os.close(descriptor)
