"""Offline file integrity and publication tests, never benchmark observations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from contextbase.evidence_store import EvidenceStore, MAX_ARTIFACT_BYTES


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = EvidenceStore(self.root / "store")

    def artifact(self, data, *, digest=None):
        """Place intentionally untrusted bytes without going through put()."""
        digest = digest or hashlib.sha256(data).hexdigest()
        path = self.store.directory / (digest + ".json")
        path.write_bytes(data)
        path.chmod(0o600)
        return digest, path

    def test_roundtrip_canonical_hash_private_permissions_and_deduplication(self):
        value = {"z": [None, True, False, 4, -2, 1.25], "a": "caf\u00e9"}
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False).encode("utf-8")
        digest = self.store.put(value)
        path = self.store.directory / (digest + ".json")
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(path.read_bytes(), canonical)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        before = path.stat()
        self.assertEqual(self.store.put({"a": value["a"], "z": value["z"]}), digest)
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.store.get(digest), value)
        self.assertEqual(list(self.store.directory.iterdir()), [path])
        retrieved = self.store.get(digest)
        retrieved["z"].append("local edit")
        self.assertEqual(self.store.get(digest), value)

    def test_json_scalars_and_repeated_noncyclic_container_roundtrip(self):
        shared = [1, "two"]
        for value in (None, True, False, 3, 0.5, "string", [], {}, [shared, shared]):
            with self.subTest(value=value):
                self.assertEqual(self.store.get(self.store.put(value)), value)

    def test_constructor_byte_limits_and_exact_boundary(self):
        for limit in (True, False, 0, -1, 1.5, "128", None, MAX_ARTIFACT_BYTES + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                EvidenceStore(self.root / "invalid", max_artifact_bytes=limit)
        self.assertFalse((self.root / "invalid").exists())
        exact = EvidenceStore(self.root / "exact", max_artifact_bytes=4)
        digest = exact.put(None)
        self.assertIsNone(exact.get(digest))
        with self.assertRaises(ValueError):
            exact.put(False)
        small = EvidenceStore(exact.directory, max_artifact_bytes=3)
        with self.assertRaises(ValueError):
            small.get(digest)
        self.assertEqual(len(list(exact.directory.iterdir())), 1)

    def test_invalid_digest_never_becomes_a_path(self):
        for digest in (None, 3, True, "", "a"*63, "a"*65, "A"*64,
                       "../" + "a"*64, "a"*64 + ".json", "/" + "a"*64,
                       "https://example.invalid/" + "a"*64, "a"*64 + "\n"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.store.get(digest)
        with self.assertRaises(FileNotFoundError):
            self.store.get("a"*64)

    def test_non_json_values_nonfinite_numbers_and_cycles_are_rejected(self):
        class CustomInt(int):
            pass
        cyclic = []
        cyclic.append(cyclic)
        for value in (float("nan"), float("inf"), -float("inf"), {"nested": [float("nan")]},
                      (1, 2), {1: "integer key"}, {None: "null key"}, b"bytes", {1},
                      datetime(2026, 1, 1), CustomInt(1), cyclic):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                self.store.put(value)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_correct_digest_does_not_make_noncanonical_or_invalid_json_valid(self):
        for data in (b'{"b":2,"a":1}', b'{"a": 1}', b'{"a":1}\n', b'{"a":1,"a":2}',
                     b'NaN', b'Infinity', b'1e400', b'1e0', b'"caf\xc3\xa9"', b'\xff',
                     b'{invalid}', b'\xef\xbb\xbfnull'):
            with self.subTest(data=data):
                digest, _ = self.artifact(data)
                with self.assertRaises(ValueError):
                    self.store.get(digest)

    def test_corrupt_existing_artifact_is_never_overwritten_or_deduped(self):
        value = {"a": 1}
        digest = self.store.put(value)
        path = self.store.directory / (digest + ".json")
        path.write_bytes(b'{"a":2}')
        before = path.stat()
        for operation in (lambda: self.store.get(digest), lambda: self.store.put(value)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(path.read_bytes(), b'{"a":2}')
        self.assertEqual(path.stat().st_ino, before.st_ino)

    def test_symlinks_directories_and_fifo_artifacts_are_refused(self):
        value = {"a": 1}
        data = b'{"a":1}'
        digest = hashlib.sha256(data).hexdigest()
        outside = self.root / "outside.json"
        outside.write_bytes(data)
        outside.chmod(0o600)
        path = self.store.directory / (digest + ".json")
        for kind in ("symlink", "dangling_symlink", "directory", "fifo"):
            if kind == "symlink": path.symlink_to(outside)
            elif kind == "dangling_symlink": path.symlink_to(self.root / "missing")
            elif kind == "directory": path.mkdir(mode=0o600)
            else: os.mkfifo(path, mode=0o600)
            try:
                for operation in (lambda: self.store.get(digest), lambda: self.store.put(value)):
                    with self.subTest(kind=kind), self.assertRaises(ValueError):
                        operation()
            finally:
                if kind == "directory": path.rmdir()
                else: path.unlink()
        self.assertEqual(outside.read_bytes(), data)

    def test_nonprivate_directory_and_artifact_permissions_are_refused(self):
        directory = self.root / "public"
        directory.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            EvidenceStore(directory)
        digest = self.store.put({"a": 1})
        path = self.store.directory / (digest + ".json")
        path.chmod(0o644)
        for operation in (lambda: self.store.get(digest), lambda: self.store.put({"a": 1})):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_store_directory_symlinks_files_and_replacement_are_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.store.directory)
        with self.assertRaises(ValueError):
            EvidenceStore(alias)
        ordinary_file = self.root / "file"
        ordinary_file.write_text("not a directory")
        with self.assertRaises(ValueError):
            EvidenceStore(ordinary_file)
        digest = self.store.put(None)
        self.store.directory.rename(self.root / "original")
        self.store.directory.mkdir(mode=0o700)
        for operation in (lambda: self.store.get(digest), lambda: self.store.put(None)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_interruption_before_publish_leaves_no_readable_partial_artifact(self):
        value = {"a": 1}
        digest = hashlib.sha256(b'{"a":1}').hexdigest()
        for exception in (OSError("simulated fsync failure"), KeyboardInterrupt()):
            with self.subTest(kind=type(exception).__name__), \
                 patch("contextbase.evidence_store.os.fsync", side_effect=exception):
                with self.assertRaises(type(exception)):
                    self.store.put(value)
            with self.assertRaises(FileNotFoundError):
                self.store.get(digest)
            self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_publish_failure_preserves_existing_artifacts_and_removes_temp(self):
        existing = self.store.put({"existing": True})
        with patch("contextbase.evidence_store.os.link", side_effect=OSError("simulated publish failure")), \
             self.assertRaises(OSError):
            self.store.put({"new": True})
        self.assertEqual(self.store.get(existing), {"existing": True})
        self.assertEqual(len(list(self.store.directory.iterdir())), 1)

    def test_concurrent_same_content_writers_dedupe_without_partial_reads(self):
        value = {"rows": [{"id": index, "value": "synthetic"} for index in range(50)]}
        with ThreadPoolExecutor(max_workers=8) as pool:
            digests = list(pool.map(lambda _: self.store.put(value), range(24)))
        self.assertEqual(len(set(digests)), 1)
        self.assertEqual(self.store.get(digests[0]), value)
        self.assertEqual(len(list(self.store.directory.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
