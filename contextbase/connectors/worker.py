"""Private, one-shot extraction worker; source values never enter its result.

The parent owns authorization, connector feature validation, timeout/termination,
and materialization into the real workspace. This module never imports the
upstream application, config store, sync engine, scheduler, or annotation agent.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import functools
import importlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


MAX_REQUEST_BYTES = 128 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_RESOURCE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PROCESS_ENV = frozenset({"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                          "SYSTEMROOT", "SystemRoot", "WINDIR", "SSL_CERT_FILE",
                          "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"})
_FIELDS = frozenset({"source_type", "name", "credential_env", "params", "resources",
                     "staging_database", "work_dir"})


class WorkerError(ValueError):
    """An allowlisted error code, never an underlying exception message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerError("invalid_request")
        result[key] = value
    return result


def _private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > MAX_REQUEST_BYTES):
            raise WorkerError("invalid_request")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(MAX_REQUEST_BYTES + 1)
        if len(value) > MAX_REQUEST_BYTES:
            raise WorkerError("invalid_request")
        return value
    finally:
        os.close(fd)


def _plain_json(value, depth=0):
    if depth > 16:
        raise WorkerError("invalid_request")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _plain_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _plain_json(item, depth + 1)
        return
    raise WorkerError("invalid_request")


def _inside(path_value, root: Path) -> Path:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise WorkerError("invalid_request")
    path = Path(path_value)
    if path.is_symlink() or path.parent.resolve() != root:
        raise WorkerError("invalid_request")
    return root / path.name


def _validate_request(request_path: Path, result_path: Path) -> dict:
    try:
        request = json.loads(_private_file(request_path), object_pairs_hook=_json_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(WorkerError("invalid_request")))
        if not isinstance(request, dict) or set(request) != _FIELDS:
            raise WorkerError("invalid_request")
        for key in ("source_type", "name"):
            if not isinstance(request[key], str) or not _IDENTIFIER.fullmatch(request[key]):
                raise WorkerError("invalid_request")
        root_given = request["work_dir"]
        if not isinstance(root_given, str) or not Path(root_given).is_absolute():
            raise WorkerError("invalid_request")
        root_path = Path(root_given)
        info = root_path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise WorkerError("invalid_request")
        root = root_path.resolve(strict=True)
        request_file = _inside(str(request_path.absolute()), root)
        result_file = _inside(str(result_path.absolute()), root)
        database = _inside(request["staging_database"], root)
        if len({request_file, result_file, database}) != 3:
            raise WorkerError("invalid_request")
        if database.exists() or Path(str(database) + ".wal").exists() or result_file.exists():
            raise WorkerError("output_exists")
        refs = request["credential_env"]
        if not isinstance(refs, dict) or len(refs) > 40:
            raise WorkerError("invalid_request")
        for field, env_name in refs.items():
            if (not isinstance(field, str) or not _RESOURCE.fullmatch(field)
                    or not isinstance(env_name, str) or not _ENVIRONMENT.fullmatch(env_name)
                    or env_name in _PROCESS_ENV):
                raise WorkerError("invalid_request")
        resources = request["resources"]
        if (not isinstance(resources, list) or len(resources) > 256
                or any(not isinstance(name, str) or not _RESOURCE.fullmatch(name) for name in resources)
                or len(set(resources)) != len(resources)):
            raise WorkerError("invalid_request")
        if not isinstance(request["params"], dict):
            raise WorkerError("invalid_request")
        _plain_json(request["params"])
        return {**request, "work_dir": root, "staging_database": database,
                "request_path": request_file, "result_path": result_file}
    except WorkerError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise WorkerError("invalid_request") from None


@contextlib.contextmanager
def _silenced():
    """Suppress both Python output and native/subprocess file descriptors."""
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    saved = []
    try:
        with open(os.devnull, "w") as sink:
            for fd in (1, 2):
                saved.append((fd, os.dup(fd)))
                os.dup2(sink.fileno(), fd)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    finally:
        for fd, original in reversed(saved):
            os.dup2(original, fd)
            os.close(original)
        logging.disable(previous_disable)


@contextlib.contextmanager
def _isolated_environment(root: Path):
    """Do not repurpose HOME; isolate dlt's own paths and ambient providers."""
    old_environment = dict(os.environ)
    old_cwd = Path.cwd()
    previous_umask = os.umask(0o077)
    previous_tempdir = tempfile.tempdir
    try:
        data_dir = root / "dlt-data"
        config_dir = root / ".dlt"
        temporary_dir = root / "temporary"
        data_dir.mkdir(mode=0o700)
        config_dir.mkdir(mode=0o700)
        temporary_dir.mkdir(mode=0o700)
        retained = {key: value for key, value in old_environment.items() if key in _PROCESS_ENV}
        os.environ.clear()
        os.environ.update(retained)
        os.environ.update({"DLT_DATA_DIR": str(data_dir), "DLT_PROJECT_DIR": str(root),
                           "DLT_DISABLE_PLUGINS": "true",
                           "RUNTIME__DLTHUB_TELEMETRY": "false", "RUNTIME__LOG_LEVEL": "CRITICAL",
                           "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                           "AWS_EC2_METADATA_DISABLED": "true", "NETRC": os.devnull,
                           "TMPDIR": str(temporary_dir), "TMP": str(temporary_dir), "TEMP": str(temporary_dir)})
        tempfile.tempdir = str(temporary_dir)
        os.chdir(root)
        yield
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environment)
        os.umask(previous_umask)
        tempfile.tempdir = previous_tempdir


def cleanup_scratch(root: Path):
    """Remove only this attempt's disposable runtime directories, never its DB."""
    for name in ("pipelines", "dlt-data", ".dlt", "temporary"):
        target = root / name
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)


def _runtime(root: Path):
    """Import optional dependencies only after process isolation is established."""
    dlt = importlib.import_module("dlt")
    # Provider internals below were reviewed against this exact wheel. A new
    # version requires another review rather than a silent fallback to defaults.
    if dlt.__version__ != "1.24.0":
        raise WorkerError("runtime_version_unsupported")
    container_module = importlib.import_module("dlt.common.configuration.container")
    context_module = importlib.import_module("dlt.common.configuration.specs.pluggable_run_context")
    run_module = importlib.import_module("dlt.common.runtime.run_context")
    dictionary_module = importlib.import_module("dlt.common.configuration.providers.dictionary")
    container = container_module.Container()
    context_type = context_module.PluggableRunContext
    if context_type in container:
        raise WorkerError("runtime_isolation_unavailable")
    provider = dictionary_module.DictionaryProvider()
    settings = {
        "providers": {"enable_airflow_secrets": False, "enable_google_secrets": False},
        "runtime": {"dlthub_telemetry": False, "log_level": "CRITICAL",
                    "http_show_error_body": False, "request_max_attempts": 1,
                    "config_files_storage_path": str(root / ".dlt")},
        "normalize": {"workers": 1}, "load": {"workers": 1},
        "extract": {"workers": 1},
    }
    for key, value in settings.items():
        provider.set_value(key, value, None)

    class PrivateRunContext(run_module.RunContext):
        @property
        def global_dir(self):
            return str(root / "dlt-data")

        @property
        def data_dir(self):
            # The default implementation eagerly evaluates global_dir(), even
            # with DLT_DATA_DIR set, probing the user's home directory.
            return str(root / "dlt-data")

        @property
        def module(self):
            return None

        def initial_providers(self):
            # No TOML/EnvironProvider: those can consult user-home TOML,
            # /run/secrets, Streamlit, Colab, or orchestration secret stores.
            return [provider]

    container[context_type] = context_type(PrivateRunContext(str(root)))
    installed = container[context_type]
    if installed.providers.providers != [provider] or installed.context.runtime_config.dlthub_telemetry:
        raise WorkerError("runtime_isolation_unavailable")
    duckdb = importlib.import_module("duckdb")
    bundle = Path(__file__).parent / "verified"
    if bundle.is_dir():
        resolver = importlib.import_module("contextbase.connectors.upstream.source_fetch")
        resolver.configure_verified_sources(bundle)
    sources = importlib.import_module("contextbase.connectors.upstream.sources")
    return dlt, duckdb, functools.partial(sources.get_source, resource_config=provider)


def _quoted(value):
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise WorkerError("invalid_output_schema")
    return '"' + value.replace('"', '""') + '"'


def _as_source(dlt, source, source_type, resource_names):
    """Normalize verified single-resource factories without consuming their data."""
    if not hasattr(source, "resources"):
        resource_type = getattr(getattr(dlt, "sources", None), "DltResource", None)
        if resource_type is None or not isinstance(source, resource_type):
            raise WorkerError("invalid_source_factory")
        resource = source

        def resources():
            yield resource

        source = dlt.source(resources, name=source_type)()
        if resource_names:
            source = source.with_resources(*resource_names)
    selected = [name for name, resource in source.resources.items() if resource.selected]
    if not selected or (resource_names and set(selected) != set(resource_names)):
        raise WorkerError("resource_selection_mismatch")
    return source, selected


def _extract(request: dict, credentials: dict) -> dict:
    dlt, duckdb, get_source = _runtime(request["work_dir"])
    try:
        source = get_source(request["source_type"], credentials,
                            resource_names=request["resources"] or None, params=request["params"])
    except ImportError:
        raise WorkerError("connector_dependency_missing") from None
    source, selected = _as_source(dlt, source, request["source_type"], request["resources"])
    pipelines = request["work_dir"] / "pipelines"
    pipelines.mkdir(mode=0o700)
    tables = []
    with duckdb.connect(str(request["staging_database"]), config={
            "enable_external_access": False, "autoload_known_extensions": False,
            "autoinstall_known_extensions": False, "allow_persistent_secrets": False,
            "allow_unsigned_extensions": False}) as conn:
        pipeline = dlt.pipeline(pipeline_name="contextbase_extract", dataset_name=request["name"],
                                destination=dlt.destinations.duckdb(conn),
                                pipelines_dir=str(pipelines), enable_runtime_trace=False,
                                restore_from_destination=False)
        # insert_values avoids file-reader SQL / extension installs in DuckDB.
        load = pipeline.run(source, loader_file_format="insert_values")
        if load is None or load.has_failed_jobs:
            raise WorkerError("extraction_incomplete")
        found = conn.execute("SELECT table_schema,table_name FROM information_schema.tables "
                             "WHERE table_schema=? AND table_type='BASE TABLE' ORDER BY table_name",
                             [request["name"]]).fetchall()
        for schema, table in found:
            if table.startswith("_dlt_"):
                continue
            columns = conn.execute("SELECT column_name,data_type FROM information_schema.columns "
                                   "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                                   [schema, table]).fetchall()
            count = conn.execute(f"SELECT count(*) FROM {_quoted(schema)}.{_quoted(table)}").fetchone()[0]
            tables.append({"schema": schema, "table": table, "row_count": count,
                           "columns": [{"name": name, "type": dtype} for name, dtype in columns]})
    if request["staging_database"].is_symlink() or not request["staging_database"].is_file():
        raise WorkerError("invalid_output_schema")
    if not tables:
        raise WorkerError("extraction_empty_schema")
    return {"status": "success", "source_type": request["source_type"], "name": request["name"],
            "resources": selected, "tables": tables}


def run_request(request_path: str | Path, result_path: str | Path) -> dict:
    """Execute once; errors contain codes only and cannot expose source details."""
    request = _validate_request(Path(request_path), Path(result_path))
    fd = os.open(request["result_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    result = {"status": "error", "error_code": "extraction_failed"}
    try:
        with _silenced():
            try:
                credentials = {}
                for field, env_name in request["credential_env"].items():
                    value = os.environ.get(env_name)
                    if not value:
                        raise WorkerError("credential_unavailable")
                    credentials[field] = value
                with _isolated_environment(request["work_dir"]):
                    try:
                        result = _extract(request, credentials)
                    finally:
                        # dlt failure packages can contain exception details.
                        # Retain only the staging database and sanitized result.
                        cleanup_scratch(request["work_dir"])
            except WorkerError as exc:
                result = {"status": "error", "error_code": exc.code}
            except ImportError:
                result = {"status": "error", "error_code": "runtime_dependency_missing"}
            except BaseException:
                # KeyboardInterrupt/SystemExit from a source are failed attempts,
                # never reasons to print an upstream traceback or retry a call.
                result = {"status": "error", "error_code": "extraction_failed"}
        result["observed_finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_RESULT_BYTES:
            result = {"status": "error", "error_code": "result_too_large",
                      "observed_finished_at": result["observed_finished_at"]}
            payload = json.dumps(result).encode("utf-8")
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
        return result
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private Oriel extraction worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_request(args.request, args.result)
        return 0 if result["status"] == "success" else 1
    except BaseException:
        return 1


if __name__ == "__main__":
    sys.exit(main())
