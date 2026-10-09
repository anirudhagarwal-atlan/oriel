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
import threading


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
        if not isinstance(request, dict) or set(request) not in (
                _FIELDS, _FIELDS | {"request_budget"},
                _FIELDS | {"request_budget", "fetch_scope", "fetch_scope_sha256"}):
            raise WorkerError("invalid_request")
        if "request_budget" in request:
            from .budget import validate_limits
            request["request_budget"] = validate_limits(request["request_budget"])
            from . import describe
            if describe(request.get("source_type"))["execution_family"] not in {"dlt_rest", "dlt_graphql"}:
                raise WorkerError("request_budget_unsupported")
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
        if "fetch_scope" in request:
            from .fetch_scope import FetchScopeError, compile_scope, plan_hash
            try:
                plan = compile_scope(request["source_type"], resources, request["params"], request["fetch_scope"])
                if request["fetch_scope_sha256"] != plan_hash(plan):
                    raise WorkerError("fetch_scope_failed")
            except FetchScopeError:
                raise WorkerError("fetch_scope_failed") from None
            request["compiled_fetch_scope"] = plan
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


def _budgeted_session(budget, *, max_response_bytes=None, fetch_scope=None):
    """One shared transport; no hidden urllib3 retries or automatic redirects.

    Counting occurs after request preparation and immediately before the sole
    transport attempt. Explicit subsequent attempts would reserve again. Static
    supported auth makes no separate token-fetch requests. dlt copies source
    config, so deepcopy must retain this same counter and deadline.
    """
    import requests
    from .upstream.yaml_source import _buffer_response, MAX_RESPONSE_BYTES
    response_limit = MAX_RESPONSE_BYTES if max_response_bytes is None else max_response_bytes

    class BudgetSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False
            self._send_lock = threading.RLock()
            self.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            self.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

        def __deepcopy__(self, memo):
            return self

        def prepare_request(self, request):
            prepared = super().prepare_request(request)
            if fetch_scope is not None:
                fetch_scope.prepare_request(request, prepared)
            return prepared

        def send(self, prepared, **kwargs):
            with self._send_lock:
                if fetch_scope is not None:
                    fetch_scope.before_dispatch(prepared)
                reservation = budget.reserve_request()
                request_id = reservation["request_id"]
                try:
                    def buffer_body(response, *args, **options):
                        # Keep the durable reservation pending through body
                        # consumption even when GraphQL requests stream=True.
                        # Parent hard termination covers a blocked next chunk.
                        _buffer_response(response, response_limit, deadline_check=budget.remaining_seconds)
                        budget.remaining_seconds()
                        return response

                    prepared.hooks["response"] = [buffer_body, *prepared.hooks.get("response", [])]
                    remaining = budget.remaining_seconds()
                    timeout = kwargs.get("timeout")
                    if isinstance(timeout, tuple):
                        kwargs["timeout"] = tuple(min(value, remaining) if isinstance(value, (int, float))
                                                  and not isinstance(value, bool) and value > 0 else remaining for value in timeout)
                    else:
                        kwargs["timeout"] = min(timeout, remaining) if isinstance(timeout, (int, float)) \
                            and not isinstance(timeout, bool) and timeout > 0 else remaining
                    kwargs["allow_redirects"] = False
                    response = super().send(prepared, **kwargs)
                except requests.exceptions.Timeout:
                    budget.complete_request(request_id, outcome="timeout")
                    raise
                except BaseException:
                    budget.complete_request(request_id, outcome="failed")
                    raise
                summary = budget.complete_request(request_id, outcome="succeeded" if 200 <= response.status_code < 300 else "failed",
                                                  status_code=response.status_code)
                if summary["status"] != "active":
                    response.close()
                    raise WorkerError("request_budget_failed")
                return response

    return BudgetSession()


def _extract(request: dict, credentials: dict, *, budget=None, fetch_scope=None) -> dict:
    dlt, duckdb, get_source = _runtime(request["work_dir"])
    session = None
    if budget is not None:
        from . import describe
        family = describe(request["source_type"])["execution_family"]
        if family not in {"dlt_rest", "dlt_graphql"}:
            raise WorkerError("request_budget_unsupported")
        if fetch_scope is not None and family != "dlt_rest":
            raise WorkerError("fetch_scope_failed")
        response_limit = None
        if family == "dlt_graphql":
            from .upstream.sources.graphql import MAX_RESPONSE_BYTES
            response_limit = MAX_RESPONSE_BYTES
        session = _budgeted_session(budget, max_response_bytes=response_limit, fetch_scope=fetch_scope)
    elif fetch_scope is not None:
        raise WorkerError("fetch_scope_failed")
    try:
        extra = {"http_session": session} if session is not None else {}
        if fetch_scope is not None:
            extra["fetch_scope"] = fetch_scope
        source = get_source(request["source_type"], credentials,
                            resource_names=request["resources"] or None, params=request["params"], **extra)
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
    scope_summary = None
    if fetch_scope is not None:
        from .fetch_scope import compact_summary
        observed = fetch_scope.finish()
        if observed["status"] != "completed":
            raise WorkerError("fetch_scope_failed")
        scope_summary = compact_summary(observed)
    if budget is not None:
        if budget.finish()["status"] != "completed":
            raise WorkerError("request_budget_failed")
    return {"status": "success", "source_type": request["source_type"], "name": request["name"],
            "resources": selected, "tables": tables,
            **({"fetch_scope": scope_summary} if scope_summary is not None else {})}


def run_request(request_path: str | Path, result_path: str | Path) -> dict:
    """Execute once; errors contain codes only and cannot expose source details."""
    request = _validate_request(Path(request_path), Path(result_path))
    fd = os.open(request["result_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    result = {"status": "error", "error_code": "extraction_failed"}
    budget, fetch_scope = None, None
    scope_errors = ()
    try:
        with _silenced():
            try:
                if "request_budget" in request:
                    from .budget import ExtractionBudget
                    budget = ExtractionBudget(request["work_dir"] / "request-budget.json", request["request_budget"])
                if "fetch_scope" in request:
                    from .fetch_scope import FetchScope, FetchScopeError
                    scope_errors = (FetchScopeError,)
                    fetch_scope = FetchScope(request["work_dir"] / "fetch-scope.json", request["compiled_fetch_scope"])
                credentials = {}
                for field, env_name in request["credential_env"].items():
                    value = os.environ.get(env_name)
                    if not value:
                        raise WorkerError("credential_unavailable")
                    credentials[field] = value
                with _isolated_environment(request["work_dir"]):
                    try:
                        options = {"budget": budget} if budget is not None else {}
                        if fetch_scope is not None:
                            options["fetch_scope"] = fetch_scope
                        result = _extract(request, credentials, **options)
                    finally:
                        # dlt failure packages can contain exception details.
                        # Retain only the staging database and sanitized result.
                        cleanup_scratch(request["work_dir"])
            except WorkerError as exc:
                result = {"status": "error", "error_code": exc.code}
            except scope_errors:
                result = {"status": "error", "error_code": "fetch_scope_failed"}
            except ImportError:
                result = {"status": "error", "error_code": "runtime_dependency_missing"}
            except BaseException as exc:
                # KeyboardInterrupt/SystemExit from a source are failed attempts,
                # never reasons to print an upstream traceback or retry a call.
                result = {"status": "error", "error_code": "extraction_failed"}
                # dlt can wrap a guard exception in a pipeline-step error. Only
                # inspect exception types, never expose their source messages.
                for _ in range(16):
                    if isinstance(exc, scope_errors):
                        result["error_code"] = "fetch_scope_failed"
                        break
                    exc = exc.__cause__ or exc.__context__
                    if exc is None:
                        break
            if budget is not None:
                try:
                    from .budget import compact_summary
                    summary = budget.finish() if result["status"] == "success" else budget.fail("worker_failed")
                    if result["status"] == "success" and summary["status"] != "completed":
                        result = {"status": "error", "error_code": "request_budget_failed"}
                    result["request_budget"] = compact_summary(summary)
                except BaseException:
                    result = {"status": "error", "error_code": "request_budget_failed"}
                    from .budget import compact_summary, read_budget
                    try:
                        result["request_budget"] = compact_summary(read_budget(request["work_dir"] / "request-budget.json"))
                    except (OSError, ValueError):
                        pass
            if fetch_scope is not None:
                try:
                    from .fetch_scope import compact_summary
                    summary = fetch_scope.finish() if result["status"] == "success" else fetch_scope.fail("worker_failed")
                    if result["status"] == "success" and summary["status"] != "completed":
                        result.update(status="error", error_code="fetch_scope_failed")
                    result["fetch_scope"] = compact_summary(summary)
                except BaseException:
                    result.update(status="error", error_code="fetch_scope_failed")
                    from .fetch_scope import compact_summary, read_scope
                    try:
                        result["fetch_scope"] = compact_summary(read_scope(request["work_dir"] / "fetch-scope.json"))
                    except (OSError, ValueError):
                        pass
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
