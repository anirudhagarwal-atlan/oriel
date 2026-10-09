"""Explicit file selections over dlt filesystem listings, with bounded readers.

No optional dependency, credential store or filesystem client is touched at
import time. The worker must isolate dlt's configuration providers before use.
Tests inject listing/client/dlt doubles; that does not establish native runtime
or authenticated vendor compatibility. Parquet uses the existing local DuckDB
reader with only one private downloaded file allowed for external access.
"""
from __future__ import annotations

import base64
import csv
from copy import deepcopy
import datetime as dt
from functools import lru_cache
import fnmatch
import gzip
import hashlib
import hmac
import importlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit


FILESYSTEM_PARAMETER_NAMES = (
    "tables", "files_per_page", "chunk_size", "max_file_bytes", "max_files",
    "region_name", "sftp_host_key_sha256",
)


def _field(name, env, secret=True, required=True):
    return {"name": name, "env_var": env, "cli_flag": "--" + name.replace("_", "-"),
            "prompt": name.replace("_", " "), "secret": secret, "required": required}


FILESYSTEM_CREDENTIAL_SPECS = {
    "s3": [_field("bucket_url", "S3_BUCKET_URL", False),
           _field("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
           _field("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
           _field("aws_session_token", "AWS_SESSION_TOKEN", required=False)],
    "gcs": [_field("bucket_url", "GCS_BUCKET_URL", False),
            _field("credentials", "GCS_SERVICE_ACCOUNT_JSON")],
    "azure": [_field("bucket_url", "AZURE_STORAGE_URL", False),
              _field("azure_storage_account_name", "AZURE_STORAGE_ACCOUNT_NAME", False),
              _field("azure_storage_account_key", "AZURE_STORAGE_ACCOUNT_KEY")],
    "sftp": [_field("bucket_url", "SFTP_URL", False),
             _field("sftp_username", "SFTP_USERNAME", False),
             _field("sftp_password", "SFTP_PASSWORD")],
}

FILESYSTEM_CONTRACT = {
    "spec": "contextbase.filesystem-adapter/v1",
    "formats": ["csv", "jsonl", "parquet"],
    "table_selection": "tables maps resource names to exact file_glob and format fields",
    "required_parameters": ["tables"],
    "parameters": list(FILESYSTEM_PARAMETER_NAMES),
    "credentials": FILESYSTEM_CREDENTIAL_SPECS,
    "gcs_credentials": "Explicit service-account JSON value; file paths and ambient credentials are refused",
    "s3_region": "Explicit region_name, default us-east-1; no ambient region lookup",
    "sftp_trust": "Required OpenSSH SHA256 host-key fingerprint; no agent, key-file or known_hosts fallback",
    "azure_url": "Use az://container/path (or abfs/abfss without container@host); account identity is supplied separately",
    "csv_types": "CSV fields remain text; JSONL and Parquet preserve supported value types",
    "empty_selection": "Missing files or a selected table with no rows fails extraction",
    "gzip": "CSV/JSONL gzip bytes and decompressed content are each bounded by max_file_bytes",
    "provenance_columns": ["contextbase_file_url", "contextbase_file_sha256",
                           "contextbase_file_row_number", "contextbase_file_modified_at",
                           "contextbase_source_bucket_url"],
    "runtime_verification": "not_performed",
}

_TABLE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SCHEMES = {"s3": {"s3"}, "gcs": {"gs", "gcs"}, "azure": {"az", "abfs", "abfss"}, "sftp": {"sftp"}}
_LIMITS = {"files_per_page": (100, 1, 1000), "chunk_size": (1000, 1, 10000),
           "max_file_bytes": (64 * 1024 * 1024, 1, 128 * 1024 * 1024), "max_files": (1000, 1, 10000)}
_PROVENANCE = frozenset(FILESYSTEM_CONTRACT["provenance_columns"])


def _fail(message):
    raise ValueError(message)


def _relative(value, *, pattern=False):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value)):
        _fail("File selections must be relative paths within the configured bucket.")
    decoded = unquote(value)
    if decoded != value and pattern:
        _fail("File globs must use literal relative paths, without URL escapes.")
    if len(decoded.split("/")) > 128 or any(part in ("", ".", "..") for part in decoded.split("/")):
        _fail("File selections cannot traverse or leave the configured bucket.")
    return decoded


def _url(value, source_type):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith(" ") or any(ord(c) < 32 for c in value)):
        _fail("An explicit filesystem URL is required.")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        _fail("Invalid filesystem URL port.")
    if (parsed.scheme not in _SCHEMES[source_type] or not parsed.hostname
            or any(char.isspace() for char in parsed.netloc)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in value):
        _fail("Filesystem URL must match the source type and contain no credentials or query.")
    if port == 0:
        _fail("Invalid filesystem URL port.")
    if source_type != "sftp" and port is not None:
        _fail("Object-storage URLs cannot override network ports.")
    path = unquote(parsed.path)
    if any(part in (".", "..") for part in path.split("/")) or any(ord(c) < 32 for c in path):
        _fail("Filesystem URL cannot contain traversal or control characters.")
    return parsed


def validate_filesystem_parameters(params, resource_names=None, *, source_type=None):
    """Pure validation suitable for onboarding before credentials are resolved."""
    if not isinstance(params, dict) or set(params) - set(FILESYSTEM_PARAMETER_NAMES):
        _fail("Unsupported filesystem parameters.")
    tables = params.get("tables")
    if not isinstance(tables, dict) or not 1 <= len(tables) <= 256:
        _fail("Provide an explicit nonempty tables mapping for file contents.")
    for name, selection in tables.items():
        if not isinstance(name, str) or not _TABLE.fullmatch(name) or name.startswith("_dlt_"):
            _fail("Filesystem table names must be lowercase identifiers.")
        if not isinstance(selection, dict) or set(selection) != {"file_glob", "format"}:
            _fail("Each filesystem table requires exactly file_glob and format.")
        _relative(selection["file_glob"], pattern=True)
        if selection["format"] not in FILESYSTEM_CONTRACT["formats"]:
            _fail("Filesystem table format must be csv, jsonl or parquet.")
    if resource_names is None:
        selected = list(tables)
    else:
        if (not isinstance(resource_names, list) or not resource_names
                or any(not isinstance(name, str) for name in resource_names)
                or len(set(resource_names)) != len(resource_names)
                or set(resource_names) - set(tables)):
            _fail("Select only declared filesystem table resources, without duplicates.")
        selected = list(resource_names)
    result = {"tables": deepcopy(tables), "selected": selected}
    for name, (default, lower, upper) in _LIMITS.items():
        value = params.get(name, default)
        if type(value) is not int or not lower <= value <= upper:
            _fail("Filesystem limits must be bounded positive integers.")
        result[name] = value
    region = params.get("region_name", "us-east-1")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        _fail("Invalid explicit S3 region.")
    if source_type is not None and source_type != "s3" and "region_name" in params:
        _fail("region_name applies only to S3.")
    result["region_name"] = region
    fingerprint = params.get("sftp_host_key_sha256")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint)):
        _fail("Supply a valid OpenSSH SHA256 host-key fingerprint.")
    if source_type == "sftp" and fingerprint is None:
        _fail("SFTP requires an explicit sftp_host_key_sha256 trust decision.")
    if source_type is not None and source_type != "sftp" and fingerprint is not None:
        _fail("sftp_host_key_sha256 applies only to SFTP.")
    result["sftp_host_key_sha256"] = fingerprint
    return result


def _credentials(source_type, credentials):
    if source_type not in FILESYSTEM_CREDENTIAL_SPECS or not isinstance(credentials, dict):
        _fail("Unsupported filesystem source or missing explicit credentials.")
    fields = FILESYSTEM_CREDENTIAL_SPECS[source_type]
    if set(credentials) - {item["name"] for item in fields}:
        _fail("Unexpected filesystem credential fields.")
    for field in fields:
        value = credentials.get(field["name"])
        if value is None and not field["required"]:
            continue
        if not isinstance(value, str) or not value.strip():
            _fail("Required filesystem credentials must be explicit nonempty values.")
    values = deepcopy(credentials)
    parsed = _url(values["bucket_url"], source_type)
    if source_type == "gcs":
        try:
            token = json.loads(values["credentials"], object_pairs_hook=_unique_object)
        except (ValueError, TypeError):
            _fail("GCS requires service-account JSON itself, never a credentials-file path.")
        required = ("project_id", "private_key", "client_email", "token_uri")
        if (not isinstance(token, dict) or token.get("type") != "service_account"
                or any(not isinstance(token.get(k), str) or not token[k].strip() for k in required)
                or token["token_uri"] != "https://oauth2.googleapis.com/token"):
            _fail("GCS requires complete explicit service-account JSON with the standard token endpoint.")
        values["credentials"] = token
    return values, parsed


def _host_key_policy(paramiko, fingerprint):
    class PinnedHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            observed = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            if not hmac.compare_digest(observed, fingerprint):
                _fail("SFTP server host key does not match the explicit fingerprint.")
    return PinnedHostKey()


def _filesystem_client(source_type, values, options):
    """Create explicit fsspec clients; never ask an SDK to discover credentials."""
    fsspec = importlib.import_module("fsspec")
    common = {"skip_instance_cache": True, "use_listings_cache": False}
    if source_type == "s3":
        return fsspec.filesystem("s3", key=values["aws_access_key_id"], secret=values["aws_secret_access_key"],
                                 token=values.get("aws_session_token"), anon=False, use_ssl=True,
                                 client_kwargs={"region_name": options["region_name"]}, **common)
    if source_type == "gcs":
        return fsspec.filesystem("gcs", token=deepcopy(values["credentials"]),
                                 project=values["credentials"]["project_id"], access="read_only", **common)
    if source_type == "azure":
        return fsspec.filesystem("az", account_name=values["azure_storage_account_name"],
                                 account_key=values["azure_storage_account_key"], anon=False, **common)
    paramiko = importlib.import_module("paramiko")
    base = fsspec.get_filesystem_class("sftp")
    fingerprint = options["sftp_host_key_sha256"]

    class PinnedSFTP(base):
        # fsspec's default _connect installs AutoAddPolicy. Replace that path;
        # never load system/user known_hosts or SSH agent/key-file identities.
        def _connect(self):
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(_host_key_policy(paramiko, fingerprint))
            try:
                self.client.connect(self.host, **self.ssh_kwargs)
                self.ftp = self.client.open_sftp()
            except BaseException:
                self.client.close()
                raise

        def close(self):
            self.ftp.close()
            self.client.close()

    parsed = _url(values["bucket_url"], source_type)
    return PinnedSFTP(parsed.hostname, port=parsed.port or 22, username=values["sftp_username"],
                      password=values["sftp_password"], allow_agent=False, look_for_keys=False,
                      timeout=30, banner_timeout=30, auth_timeout=30, skip_instance_cache=True)


def _glob_matches(path, pattern):
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)

    return match(0, 0)


def _file_identity(item, values, source_type, selection):
    if not isinstance(item, dict) or not callable(getattr(item, "open", None)):
        _fail("Filesystem listing did not return readable file items.")
    bucket = _url(values["bucket_url"], source_type)
    file = _url(item.get("file_url"), source_type)
    prefix = unquote(bucket.path).rstrip("/") + "/"
    file_path = unquote(file.path)
    same_endpoint = file.netloc == bucket.netloc
    # dlt 1.24 _make_sftp_url drops the configured port from listed URLs.
    # Reads still use the explicitly bound client. Accept only that omission,
    # never another host or a conflicting explicit port. Keep the configured
    # bucket URL in every row so the original endpoint remains distinguishable.
    if source_type == "sftp" and file.hostname == bucket.hostname and file.port is None:
        same_endpoint = True
    if (file.scheme != bucket.scheme or not same_endpoint or not file_path.startswith(prefix)):
        _fail("Listed file is outside the configured bucket prefix.")
    relative = _relative(file_path[len(prefix):])
    if item.get("relative_path") is not None and _relative(item["relative_path"]) != relative:
        _fail("Filesystem relative path does not match its source URL.")
    if not _glob_matches(relative, selection["file_glob"]):
        _fail("Listed file does not match the explicit table selection.")
    size = item.get("size_in_bytes")
    if size is not None and (type(size) is not int or size < 0):
        _fail("Invalid filesystem file size.")
    modified = item.get("modification_date")
    if isinstance(modified, (dt.datetime, dt.date)):
        modified = modified.isoformat()
    if modified is not None and not isinstance(modified, str):
        _fail("Invalid filesystem modification timestamp.")
    return item["file_url"], size, modified


def _read_bytes(item, maximum):
    with item.open(mode="rb", compression="disable") as stream:
        raw = stream.read(maximum + 1)
    if not isinstance(raw, bytes) or len(raw) > maximum:
        _fail("File content exceeds max_file_bytes or is not a binary stream.")
    return raw


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("Duplicate object fields in file content.")
        value[key] = item
    return value


def _finite(value, depth=0):
    if depth > 32:
        _fail("File record nesting is too deep.")
    if isinstance(value, float) and not math.isfinite(value):
        _fail("File records cannot contain nonfinite numeric values.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                _fail("File records require nonempty text field names.")
            _finite(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item, depth + 1)


def _field_names(value, normalize, *, root=True):
    """Use the selected dlt schema's actual naming rules, not an approximation."""
    if isinstance(value, dict):
        names = [normalize(key) for key in value]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            _fail("File fields collide after column-name normalization.")
        if root and set(names) & {normalize(name) for name in _PROVENANCE}:
            _fail("File fields collide with reserved provenance columns.")
        for item in value.values():
            _field_names(item, normalize, root=False)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _field_names(item, normalize, root=False)


def _rows(raw, format, chunk_size):
    if format == "csv":
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, None)
        if not header or any(not key or "\x00" in key for key in header) or len(set(header)) != len(header):
            _fail("CSV requires a unique nonempty header.")
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                _fail("CSV row width does not match its header.")
            yield dict(zip(header, row))
    elif format == "jsonl":
        for line in raw.decode("utf-8-sig").splitlines():
            if line.strip():
                yield json.loads(line, object_pairs_hook=_unique_object,
                                 parse_constant=lambda _: _fail("JSONL requires finite JSON values."))
    else:
        duckdb = importlib.import_module("duckdb")
        with tempfile.TemporaryDirectory(prefix="contextbase-file-reader-") as directory:
            path = Path(directory) / "content.parquet"
            with path.open("xb") as handle:
                handle.write(raw)
            path.chmod(0o600)
            with duckdb.connect(config={"autoload_known_extensions": False, "autoinstall_known_extensions": False,
                                        "allow_persistent_secrets": False}) as conn:
                # DuckDB requires allowed_paths to be configured before closing
                # the external-access gate. No input is parsed before this SET.
                conn.execute("SET allowed_paths = ?", [[str(path)]])
                conn.execute("SET enable_external_access=false")
                cursor = conn.execute("SELECT * FROM read_parquet(?)", [str(path)])
                columns = [item[0] for item in cursor.description]
                while True:
                    batch = cursor.fetchmany(chunk_size)
                    if not batch:
                        break
                    for row in batch:
                        yield dict(zip(columns, row))


def build_filesystem_source(source_type, credentials, resource_names=None, params=None, *,
                            filesystem_factory=None, dlt_module=None, filesystem_client_factory=None):
    """Return selected content resources; clients/listings remain lazy.

    The three factory hooks are dependency-free tests only, never user params.
    Caller-provided credentials are resolved values from authorized references.
    Explicit validation occurs before importing dlt or constructing any client.
    """
    values, _ = _credentials(source_type, credentials)
    options = validate_filesystem_parameters(params, resource_names, source_type=source_type)
    dlt = dlt_module or importlib.import_module("dlt")
    listing_factory = filesystem_factory or importlib.import_module("dlt.sources.filesystem").filesystem
    client_factory = filesystem_client_factory or _filesystem_client
    count = 0
    source = None

    def records(table):
        nonlocal count
        selection = options["tables"][table]
        client = client_factory(source_type, deepcopy(values), deepcopy(options))
        seen, row_count, batch = set(), 0, []
        try:
            listing = listing_factory(bucket_url=values["bucket_url"], credentials=client,
                                      file_glob=selection["file_glob"], files_per_page=options["files_per_page"],
                                      extract_content=False, kwargs={}, client_kwargs={}, incremental=None)
            for page in listing:
                for item in (page if isinstance(page, (list, tuple)) else [page]):
                    url, listed_size, modified = _file_identity(item, values, source_type, selection)
                    if url in seen:
                        _fail("Filesystem listing contains a duplicate file.")
                    seen.add(url)
                    count += 1
                    if count > options["max_files"]:
                        _fail("Filesystem selection exceeds max_files.")
                    if listed_size is not None and listed_size > options["max_file_bytes"]:
                        _fail("Listed file exceeds max_file_bytes.")
                    raw = _read_bytes(item, options["max_file_bytes"])
                    if listed_size is not None and len(raw) != listed_size:
                        _fail("File size changed since its filesystem listing.")
                    digest = hashlib.sha256(raw).hexdigest()
                    content = raw
                    if item.get("encoding") == "gzip" or url.endswith(".gz"):
                        if selection["format"] == "parquet":
                            _fail("Externally gzip-wrapped Parquet is unsupported.")
                        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                            content = compressed.read(options["max_file_bytes"] + 1)
                        if len(content) > options["max_file_bytes"]:
                            _fail("Decompressed file exceeds max_file_bytes.")
                    for index, row in enumerate(_rows(content, selection["format"], options["chunk_size"]), 1):
                        if not isinstance(row, dict) or not row:
                            _fail("Tabular file records must be nonempty objects.")
                        _finite(row)
                        _field_names(row, source.schema.naming.normalize_identifier)
                        batch.append({**row, "contextbase_file_url": url, "contextbase_file_sha256": digest,
                                      "contextbase_file_row_number": index, "contextbase_file_modified_at": modified,
                                      "contextbase_source_bucket_url": values["bucket_url"]})
                        row_count += 1
                        if len(batch) >= options["chunk_size"]:
                            yield batch
                            batch = []
            if not seen or not row_count:
                _fail("Selected filesystem table has no files or no data rows.")
            if batch:
                yield batch
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    resources = [dlt.resource(records(name), name=name, write_disposition="replace") for name in options["selected"]]

    @dlt.source(name="contextbase_filesystem")
    def selected_files():
        return resources

    source = selected_files()
    if not callable(getattr(getattr(getattr(source, "schema", None), "naming", None), "normalize_identifier", None)):
        _fail("Filesystem source requires an explicit schema naming normalizer.")
    return source
