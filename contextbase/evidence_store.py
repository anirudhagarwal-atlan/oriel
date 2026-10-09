"""A private, local content-addressed store for immutable JSON evidence.

Digests identify strict canonical JSON: UTF-8 bytes from ``json.dumps`` with
sorted string keys, compact separators, ``ensure_ascii=True`` and finite JSON
numbers. Only native JSON types are accepted. Files are named ``<sha256>.json``;
callers cannot supply artifact paths or URLs.

Publication uses a fully written, fsynced temporary file and an exclusive hard
link. A concurrent writer can publish the same content, but cannot overwrite an
existing artifact through this API. An interrupted unpublished write can leave
a private temporary file; readers never use it. Directory metadata is not
fsynced, so publication is not guaranteed durable across a power loss. Hashes
prove content consistency, not the truth or origin of the stored evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat


MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _json_types(value, ancestors=None):
    """Reject coercions, non-finite numbers and cyclic containers before encoding."""
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Evidence must contain only finite JSON numbers.")
        return
    if type(value) not in (dict, list):
        raise ValueError("Evidence must contain only native JSON values.")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Evidence cannot contain cyclic JSON containers.")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("Evidence object keys must be strings.")
            items = value.values()
        else:
            items = value
        for item in items:
            _json_types(item, ancestors)
    finally:
        ancestors.remove(identity)


def _canonical(value, limit):
    try:
        _json_types(value)
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True, allow_nan=False)
        encoded = bytearray()
        for chunk in encoder.iterencode(value):
            piece = chunk.encode("utf-8")
            if len(encoded) + len(piece) > limit:
                raise ValueError("Evidence artifact exceeds the configured byte limit.")
            encoded.extend(piece)
        return bytes(encoded)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Evidence is too deeply nested or cannot be encoded as JSON.") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Evidence JSON contains a duplicate object key.")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("Evidence must contain only finite JSON numbers.")


class EvidenceStore:
    """Store canonical JSON in a private directory, without replacing artifacts.

    ``max_artifact_bytes`` is a positive integer capped at 128 MiB. Existing
    store directories must be real directories with mode 0700; existing
    artifacts must be regular files with mode 0600. The directory's identity
    is checked again for each operation, and artifact access is relative to an
    open directory descriptor. A missing digest raises ``FileNotFoundError``;
    malformed, unsafe or changed evidence raises ``ValueError``.
    """

    def __init__(self, directory, *, max_artifact_bytes=MAX_ARTIFACT_BYTES):
        if type(max_artifact_bytes) is not int or not 0 < max_artifact_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact byte limit must be an integer from 1 to 128 MiB.")
        self.directory = Path(directory).absolute()
        self.max_artifact_bytes = max_artifact_bytes
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise ValueError("Evidence store must be a real directory, not a file or symlink.") from exc
        with self._open_directory() as descriptor:
            info = os.fstat(descriptor)
            self._identity = (info.st_dev, info.st_ino)

    @contextmanager
    def _open_directory(self):
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("Evidence store must be a real directory, not a symlink.") from exc
            raise
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Evidence store directory must have mode 0700.")
            if hasattr(self, "_identity") and self._identity != (info.st_dev, info.st_ino):
                raise ValueError("Evidence store directory was replaced.")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _name(digest):
        if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
            raise ValueError("Evidence digest must be exactly 64 lowercase hexadecimal characters.")
        return digest + ".json"

    def _read(self, descriptor, digest):
        name = self._name(digest)
        try:
            artifact = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("Evidence artifact must be a regular file, not a symlink.") from exc
            raise
        try:
            info = os.fstat(artifact)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Evidence artifact must be a regular file with mode 0600.")
            if info.st_size > self.max_artifact_bytes:
                raise ValueError("Evidence artifact exceeds the configured byte limit.")
            with os.fdopen(artifact, "rb", closefd=False) as handle:
                data = handle.read(self.max_artifact_bytes + 1)
            if len(data) > self.max_artifact_bytes:
                raise ValueError("Evidence artifact exceeds the configured byte limit.")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Evidence artifact content hash does not match its digest.")
            try:
                value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                canonical = _canonical(value, self.max_artifact_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError("Evidence artifact is not strict canonical JSON.") from exc
            if canonical != data:
                raise ValueError("Evidence artifact is not strict canonical JSON.")
            return value, data
        finally:
            os.close(artifact)

    def get(self, digest):
        """Read an artifact only after validating its path, size, JSON and hash."""
        self._name(digest)
        with self._open_directory() as descriptor:
            value, _ = self._read(descriptor, digest)
            return value

    def put(self, value):
        """Publish once, or verify and reuse an already stored identical artifact."""
        data = _canonical(value, self.max_artifact_bytes)
        digest = hashlib.sha256(data).hexdigest()
        with self._open_directory() as descriptor:
            try:
                _, existing = self._read(descriptor, digest)
            except FileNotFoundError:
                pass
            else:
                if existing != data:
                    raise ValueError("Existing evidence differs from the supplied content.")
                return digest
            temporary = ".evidence-" + secrets.token_hex(16) + ".tmp"
            artifact = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=descriptor)
            try:
                os.fchmod(artifact, 0o600)
                with os.fdopen(artifact, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(artifact)
                try:
                    os.link(temporary, self._name(digest), src_dir_fd=descriptor,
                            dst_dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    pass
                _, existing = self._read(descriptor, digest)
                if existing != data:
                    raise ValueError("Existing evidence differs from the supplied content.")
                return digest
            finally:
                os.close(artifact)
                os.unlink(temporary, dir_fd=descriptor)
