"""Opt-in REST wire scope and pre-yield coverage accounting.

This is not vendor authorization. It binds a declared fetch graph, exact origin
and immutable request shape to one extraction, then accounts for every page
including unselected parents. Vendor field/history semantics, permissions and
resolved network destinations still need independent enforcement/review.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_SENSITIVE = re.compile(r"token|secret|password|credential|authorization|api.?key", re.I)
_LIMIT = 16 * 1024 * 1024
_STOP_REASONS = {"worker_failed", *("fetch_scope_" + suffix for suffix in (
    "invalid", "too_large", "ledger_invalid", "declaration_changed", "unattributed_request",
    "path_changed", "duplicate_chain", "pending_request", "page_limit", "request_changed",
    "repeated_request", "unattributed_response", "response_unsupported", "response_changed",
    "row_limit", "pagination_changed", "parent_binding_missing", "parent_binding_unsupported", "incomplete"))}


class FetchScopeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _refuse(code="fetch_scope_invalid"):
    raise FetchScopeError(code)


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        _refuse()
    if len(raw) > _LIMIT:
        _refuse("fetch_scope_too_large")
    return raw


def plan_hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _origin(value):
    if type(value) is not str or len(value) > 512 or any(ord(c) <= 32 or ord(c) > 126 for c in value):
        _refuse()
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
                or parts.password is not None or parts.path or parts.query or parts.fragment
                or "\\" in value or "%" in parts.netloc or host.endswith(".")):
            _refuse()
        authority = ("[" + host + "]") if ":" in host else host
        if port is not None:
            authority += ":" + str(port)
        if value != parts.scheme + "://" + authority:
            _refuse()
        if parts.scheme == "http" and not ipaddress.ip_address(host).is_loopback:
            _refuse("fetch_scope_origin_unsupported")
    except (ValueError, UnicodeError):
        _refuse("fetch_scope_origin_unsupported")
    return value


def _url_parts(url):
    try:
        parts = urlsplit(url)
        origin = _origin(parts.scheme + "://" + parts.netloc)
        if parts.fragment or "\\" in url or any(ord(c) < 32 for c in url):
            _refuse("fetch_scope_request_changed")
        return origin, parts.path or "/", parts.query
    except (TypeError, ValueError):
        _refuse("fetch_scope_request_changed")


def _mapping(value):
    if type(value) is not dict or any(type(k) is not str or _SENSITIVE.search(k) for k in value):
        _refuse("fetch_scope_shape_unsupported")
    for item in value.values():
        if type(item) not in {str, int, bool, list}:
            _refuse("fetch_scope_shape_unsupported")
        if type(item) is list and (len(item) > 256 or any(type(v) not in {str, int, bool} for v in item)):
            _refuse("fetch_scope_shape_unsupported")
        if any("{" in str(v) or "}" in str(v) for v in (item if type(item) is list else [item])):
            _refuse("fetch_scope_shape_unsupported")
    return copy.deepcopy(value)


def _pairs(values):
    # Match requests' query rendering without resolving credentials or sending.
    return sorted(parse_qsl(urlencode(values, doseq=True), keep_blank_values=True), key=lambda pair: pair[0])


def compile_scope(source_type, resources, params, scope):
    """Compile the actual packaged REST declaration before reading credentials."""
    from . import describe
    from .policy import selection_plan, validate_parameters, validate_selection
    from .upstream.yaml_source import load_yaml_config, _translate_config

    try:
        if type(scope) is not dict or set(scope) != {"origin", "fetch_resources", "max_pages", "max_rows"}:
            _refuse()
        origin = _origin(scope["origin"])
        names = scope["fetch_resources"]
        if (type(names) is not list or not names or len(names) > 256
                or any(type(n) is not str or not _NAME.fullmatch(n) for n in names)
                or len(set(names)) != len(names)):
            _refuse()
        for name, maximum in (("max_pages", 10_000), ("max_rows", 1_000_000)):
            if type(scope[name]) is not int or not 1 <= scope[name] <= maximum:
                _refuse()
        entry = describe(source_type)
        if entry["execution_family"] != "dlt_rest":
            _refuse("fetch_scope_runtime_unsupported")
        selection = selection_plan(entry, resources)
        if sorted(names) != selection["required_fetch_resources"]:
            _refuse("fetch_scope_resource_mismatch")
        validate_parameters(entry, params)
        validate_selection(entry, resources, params)
        raw = load_yaml_config(source_type)
        if type(raw) is not dict:
            _refuse("fetch_scope_declaration_missing")
        declaration_hash = plan_hash(raw)
        public = copy.deepcopy(raw)
        # Authentication is deliberately never evaluated by this compiler.
        # Unresolved credential placeholders in routing/parameters fail below.
        public.pop("auth_methods", None)
        public.get("client", {}).pop("auth", None)
        public.get("client", {}).pop("headers", None)
        public["resources"] = [r for r in public.get("resources", []) if r.get("name") in names]
        defaults = public.get("resource_defaults", {})
        if defaults.get("parallelized") or defaults.get("incremental"):
            _refuse("fetch_scope_shape_unsupported")
        defaults.get("endpoint", {}).pop("auth", None)
        defaults.get("endpoint", {}).pop("headers", None)
        for resource in public.get("resources", []):
            if resource.get("parallelized") or resource.get("incremental"):
                _refuse("fetch_scope_shape_unsupported")
            resource.pop("auth", None)
            resource.get("endpoint", {}).pop("auth", None)
            resource.get("endpoint", {}).pop("headers", None)
        translated = _translate_config(public, {}, resources, params)
        base = translated["client"].get("base_url")
        if type(base) is not str or any(c in base for c in "{}?#\\"):
            _refuse("fetch_scope_origin_unsupported")
        plans = {}
        for resource in translated["resources"]:
            endpoint = resource["endpoint"]
            if (endpoint.get("incremental") or endpoint.get("response_format", "json") != "json"
                    or "data" in endpoint or "body" in endpoint):
                _refuse("fetch_scope_shape_unsupported")
            path = endpoint.get("path", "")
            if type(path) is not str:
                _refuse()
            url = path if "://" in path else base.rstrip("/") + "/" + path.lstrip("/")
            actual_origin, path, query = _url_parts(url)
            if actual_origin != origin or query or any(x in path for x in ("/../", "/./", "\\")):
                _refuse("fetch_scope_origin_unsupported")
            method = endpoint.get("method", "GET").upper()
            if method not in {"GET", "POST"}:
                _refuse("fetch_scope_shape_unsupported")
            query_params = copy.deepcopy(endpoint.get("params", {}))
            bindings, parents = {}, set()
            for key, value in list(query_params.items()):
                if type(value) is dict:
                    if (set(value) != {"type", "resource", "field"} or value["type"] != "resolve"
                            or value["resource"] not in names or not _PATH.fullmatch(value["field"])
                            or "{" + key + "}" not in path):
                        _refuse("fetch_scope_shape_unsupported")
                    bindings[key] = value["field"]
                    parents.add(value["resource"])
                    del query_params[key]
            if len(parents) > 1 or set(re.findall(r"\{([^{}]+)\}", path)) != set(bindings):
                _refuse("fetch_scope_shape_unsupported")
            if "{" in re.sub(r"\{[^{}]+\}", "", path) or "}" in re.sub(r"\{[^{}]+\}", "", path):
                _refuse("fetch_scope_shape_unsupported")
            query_params = _mapping(query_params)
            body = endpoint.get("json")
            if body is not None and (method != "POST" or type(body) is not dict):
                _refuse("fetch_scope_shape_unsupported")
            # Nested filters are preserved exactly, but no dynamic templates or
            # credential-looking fields may be hidden inside a JSON body.
            if body is not None:
                def check_body(value):
                    if type(value) is dict:
                        for k, v in value.items():
                            if type(k) is not str or _SENSITIVE.search(k): _refuse("fetch_scope_shape_unsupported")
                            check_body(v)
                    elif type(value) is list:
                        for v in value: check_body(v)
                    elif type(value) not in {str, int, bool, type(None)} or (type(value) is str and any(c in value for c in "{}")):
                        _refuse("fetch_scope_shape_unsupported")
                check_body(body)
            pager = copy.deepcopy(endpoint["paginator"])
            kind = pager.get("type")
            allowed = {"type", "page_size", "page_size_param", "stop_condition", "total_path", "has_more_path", "stop_after_empty_page"}
            allowed |= {"single_page": set(), "cursor": {"cursor_param", "cursor_path", "limit", "limit_param"},
                        "offset": {"offset", "offset_param", "limit", "limit_param"},
                        "page_number": {"base_page", "page", "page_param"}}.get(kind, set())
            if kind not in {"single_page", "cursor", "offset", "page_number"} or set(pager) - allowed:
                _refuse("fetch_scope_pagination_unsupported")
            container = body if body is not None else query_params
            if pager.get("page_size_param"):
                container.setdefault(pager["page_size_param"], pager.get("page_size"))
            field, initial, step, cursor_path = None, None, None, None
            if kind == "cursor":
                field, cursor_path = pager.get("cursor_param"), pager.get("cursor_path")
                if type(cursor_path) is not str or not _PATH.fullmatch(cursor_path):
                    _refuse("fetch_scope_pagination_unsupported")
                initial = container.get(field)
                if "limit" in pager:
                    container.setdefault(pager.get("limit_param", "limit"), pager["limit"])
            elif kind == "page_number":
                field, initial, step = pager.get("page_param"), pager.get("page", pager.get("base_page", 0)), 1
            elif kind == "offset":
                field, initial, step = pager.get("offset_param"), pager.get("offset", 0), pager.get("limit")
                if type(step) is not int or not 1 <= step <= 100_000 or not _NAME.fullmatch(pager.get("limit_param", "")):
                    _refuse("fetch_scope_pagination_unsupported")
                container[pager["limit_param"]] = step
            if kind != "single_page" and (type(field) is not str or not _NAME.fullmatch(field)):
                _refuse("fetch_scope_pagination_unsupported")
            if kind in {"page_number", "offset"} and (type(initial) is not int or initial < 0):
                _refuse("fetch_scope_pagination_unsupported")
            if field is not None:
                container.pop(field, None)
            plans[resource["name"]] = {"method": method, "path": path, "parent": next(iter(parents), None),
                "bindings": bindings, "query": _mapping(query_params), "json": body,
                "pagination": {"kind": kind, "field": field, "location": "json" if body is not None else "query",
                               "initial": initial, "step": step, "cursor_path": cursor_path}}
        if sorted(plans) != sorted(names):
            _refuse("fetch_scope_resource_mismatch")
        result = {"spec": "oriel.connector.fetch-plan/v1", "source_type": source_type, "scope": copy.deepcopy(scope),
                  "declaration_sha256": declaration_hash, "configuration_sha256": plan_hash({"resources": resources, "params": params}),
                  "selected_outputs": selection["selected_outputs"], "fetch_resources": sorted(names),
                  "limits": {k: scope[k] for k in ("max_pages", "max_rows")}, "resources": plans}
        _canonical(result)
        return result
    except FetchScopeError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        _refuse("fetch_scope_shape_unsupported")


def _field(value, path):
    for part in path.split("."):
        if type(value) is not dict or part not in value:
            _refuse("fetch_scope_parent_binding_missing")
        value = value[part]
    return value


def _path_hash(path):
    return hashlib.sha256(path.encode()).hexdigest()


def _private_directory(path):
    # Use the request ledger's existing directory-relative, no-follow primitive.
    from .budget import _directory, BudgetError
    try:
        return _directory(path)
    except (OSError, BudgetError):
        _refuse("fetch_scope_private_path_required")


class FetchScope:
    def __init__(self, path, plan):
        self.path = Path(path).absolute()
        self.plan = copy.deepcopy(plan)
        self._lock = threading.RLock()
        self._tokens, self._prepared = {}, {}
        self._expected = {name: set() for name in plan["fetch_resources"]}
        for name, rule in plan["resources"].items():
            if rule["parent"] is None:
                self._expected[name].add(_path_hash(rule["path"]))
        self.value = {"spec": "oriel.connector.fetch-ledger/v1", "plan": self.plan, "plan_sha256": plan_hash(plan),
                      "status": "active", "stop_reason": None, "chains": [], "requests": [],
                      "expected_paths": {}, "counts": {}, "coverage": {}}
        self._write(exclusive=True)

    def __deepcopy__(self, memo):
        return self

    def verify_declaration(self, source_type, declaration, resources, params):
        """Recheck the actual adapter input immediately before translation."""
        with self._lock:
            self._active()
            if (source_type != self.plan["source_type"] or plan_hash(declaration) != self.plan["declaration_sha256"]
                    or plan_hash({"resources": resources or [], "params": params or {}}) != self.plan["configuration_sha256"]):
                self._stop("fetch_scope_declaration_changed")

    def _write(self, *, exclusive=False):
        self.value["expected_paths"] = {name: sorted(values) for name, values in self._expected.items()}
        counts, coverage = _account(self.value)
        self.value.update(counts=counts, coverage=coverage)
        raw = _canonical(self.value)
        temporary = self.path.name if exclusive else self.path.name + ".tmp"
        directory = _private_directory(self.path)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            finally:
                os.close(fd)
            if not exclusive:
                os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            os.close(directory)

    def _stop(self, code):
        if self.value["status"] == "active":
            self.value.update(status="failed", stop_reason=code)
            self._write()
        _refuse(code)

    def _active(self):
        if self.value["status"] != "active":
            _refuse("fetch_scope_not_active")

    def begin_chain(self, resource, request):
        with self._lock:
            self._active()
            if resource not in self.plan["resources"] or id(request) in self._tokens:
                self._stop("fetch_scope_unattributed_request")
            try:
                origin, path, _ = _url_parts(request.url)
                marker = _path_hash(path)
                if origin != self.plan["scope"]["origin"] or marker not in self._expected[resource]:
                    self._stop("fetch_scope_path_changed")
                if any(c["resource"] == resource and c["path_sha256"] == marker for c in self.value["chains"]):
                    self._stop("fetch_scope_duplicate_chain")
                chain = {"id": len(self.value["chains"]) + 1, "resource": resource,
                         "path_sha256": marker, "completed": False}
                self.value["chains"].append(chain)
                self._tokens[id(request)] = {"request": request, "chain": chain, "page_value": self.plan["resources"][resource]["pagination"]["initial"]}
                self._write()
            except FetchScopeError as error:
                self._stop(error.code)

    def prepare_request(self, request, prepared):
        with self._lock:
            self._active()
            token = self._tokens.get(id(request))
            if token is None or token["request"] is not request:
                self._stop("fetch_scope_unattributed_request")
            self._prepared[id(prepared)] = {"prepared": prepared, "token": token, "dispatch": None}

    def _shape(self, prepared, token):
        rule = self.plan["resources"][token["chain"]["resource"]]
        origin, path, query = _url_parts(prepared.url)
        if (origin != self.plan["scope"]["origin"] or _path_hash(path) != token["chain"]["path_sha256"]
                or prepared.method != rule["method"] or prepared.headers.get("Host", urlsplit(origin).netloc) != urlsplit(origin).netloc):
            _refuse("fetch_scope_request_changed")
        if len(query) > 64_000 or re.search(r"%(?![a-fA-F0-9]{2})", query):
            _refuse("fetch_scope_request_changed")
        actual_query = sorted(parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=512), key=lambda pair: pair[0])
        expected_query = copy.deepcopy(rule["query"])
        expected_body = copy.deepcopy(rule["json"])
        pager = rule["pagination"]
        if pager["field"] is not None and token["page_value"] is not None:
            (expected_body if pager["location"] == "json" else expected_query)[pager["field"]] = token["page_value"]
        if actual_query != _pairs(expected_query):
            _refuse("fetch_scope_request_changed")
        if expected_body is None:
            if prepared.body not in (None, b"", ""):
                _refuse("fetch_scope_request_changed")
            body = None
        else:
            raw = prepared.body
            if not isinstance(raw, (bytes, str)) or len(raw) > 128 * 1024:
                _refuse("fetch_scope_request_changed")
            body = json.loads(raw, object_pairs_hook=_unique)
            if _canonical(body) != _canonical(expected_body):
                _refuse("fetch_scope_request_changed")
        return plan_hash({"method": prepared.method, "url": prepared.url, "body": body})

    def before_dispatch(self, prepared):
        with self._lock:
            self._active()
            item = self._prepared.get(id(prepared))
            if item is None or item["prepared"] is not prepared or item["dispatch"] is not None:
                self._stop("fetch_scope_unattributed_request")
            token = item["token"]
            if token["chain"]["completed"] or any(r["chain"] == token["chain"]["id"] and r["status"] == "pending" for r in self.value["requests"]):
                self._stop("fetch_scope_pending_request")
            if len(self.value["requests"]) >= self.plan["limits"]["max_pages"]:
                self._stop("fetch_scope_page_limit")
            try:
                fingerprint = self._shape(prepared, token)
            except (FetchScopeError, ValueError, TypeError) as error:
                self._stop(error.code if isinstance(error, FetchScopeError) else "fetch_scope_request_changed")
            if any(r["chain"] == token["chain"]["id"] and r["request_sha256"] == fingerprint for r in self.value["requests"]):
                self._stop("fetch_scope_repeated_request")
            record = {"id": len(self.value["requests"]) + 1, "chain": token["chain"]["id"],
                      "request_sha256": fingerprint, "status": "pending", "page": None}
            self.value["requests"].append(record)
            item["dispatch"] = record
            self._write()

    def observe_page(self, resource, response, data, has_next):
        with self._lock:
            self._active()
            prepared = getattr(response, "request", None)
            item = self._prepared.get(id(prepared))
            if (item is None or item["prepared"] is not prepared or item["dispatch"] is None
                    or item["token"]["chain"]["resource"] != resource or item["dispatch"]["status"] != "pending"):
                self._stop("fetch_scope_unattributed_response")
            if type(data) is not list or any(type(row) is not dict for row in data) or type(has_next) is not bool:
                self._stop("fetch_scope_response_unsupported")
            token, record = item["token"], item["dispatch"]
            try:
                if not 200 <= response.status_code < 300 or self._shape(prepared, token) != record["request_sha256"]:
                    self._stop("fetch_scope_response_changed")
                record.update(status="observed", page={"rows": len(data), "has_next": has_next, "data_sha256": plan_hash(data),
                                                        "validated": False, "child_paths": None})
                self._write()  # Retain observed excess; never hide rows by truncation.
                if self.value["counts"]["rows"] > self.plan["limits"]["max_rows"]:
                    self._stop("fetch_scope_row_limit")
                rule = self.plan["resources"][resource]
                pager = rule["pagination"]
                cursor = None
                if pager["kind"] == "cursor":
                    body = response.json()
                    for key in pager["cursor_path"].split("."):
                        body = body.get(key) if type(body) is dict else None
                    cursor = body
                    if not has_next and cursor not in (None, ""):
                        self._stop("fetch_scope_pagination_changed")
                if has_next:
                    if pager["kind"] == "single_page":
                        self._stop("fetch_scope_pagination_changed")
                    if pager["kind"] == "cursor":
                        if type(cursor) not in {str, int} or not str(cursor) or len(str(cursor)) > 4096 or cursor == token["page_value"]:
                            self._stop("fetch_scope_pagination_changed")
                        token["page_value"] = cursor
                    else:
                        token["page_value"] += pager["step"]
                child_paths = {}
                for child, child_rule in self.plan["resources"].items():
                    if child_rule["parent"] != resource:
                        continue
                    paths = set()
                    for row in data:
                        path = child_rule["path"]
                        for variable, field in child_rule["bindings"].items():
                            value = _field(row, field)
                            if (type(value) not in {str, int} or not re.fullmatch(r"[A-Za-z0-9_~.-]{1,512}", str(value))
                                    or str(value) in {".", ".."}):
                                self._stop("fetch_scope_parent_binding_unsupported")
                            path = path.replace("{" + variable + "}", str(value))
                        paths.add(_path_hash(path))
                    child_paths[child] = sorted(paths)
                for child, paths in child_paths.items():
                    self._expected[child].update(paths)
                record["page"].update(validated=True, child_paths=child_paths)
                token["chain"]["completed"] = not has_next
                self._write()
            except FetchScopeError as error:
                self._stop(error.code)
            except (TypeError, ValueError, KeyError):
                self._stop("fetch_scope_response_unsupported")

    def finish(self):
        with self._lock:
            if self.value["status"] != "active":
                return copy.deepcopy(self.value)
            self._write()
            if self.value["counts"]["pending"] or any(c["status"] != "complete" for c in self.value["coverage"].values()):
                self.value.update(status="failed", stop_reason="fetch_scope_incomplete")
            else:
                self.value.update(status="completed", stop_reason=None)
            self._write()
            return copy.deepcopy(self.value)

    def fail(self, code="worker_failed"):
        with self._lock:
            if self.value["status"] != "failed":
                # Completed page accounting cannot turn a failed load into a
                # completed extraction. No upstream exception text is retained.
                self.value.update(status="failed", stop_reason="worker_failed")
                self._write()
            return copy.deepcopy(self.value)


def _account(value):
    plan = value["plan"]
    counts = {"requests": len(value["requests"]), "pages": 0, "rows": 0, "pending": 0}
    coverage = {name: {"parent": rule["parent"], "expected_chains": len(value["expected_paths"].get(name, [])),
                      "started_chains": 0, "completed_chains": 0, "pages": 0, "rows": 0, "status": "pending"}
                for name, rule in plan["resources"].items()}
    for chain in value["chains"]:
        coverage[chain["resource"]]["started_chains"] += 1
        coverage[chain["resource"]]["completed_chains"] += int(chain["completed"])
    for request in value["requests"]:
        if request["status"] == "pending": counts["pending"] += 1
        else:
            row_count = request["page"]["rows"]
            resource = value["chains"][request["chain"] - 1]["resource"]
            counts["pages"] += 1; counts["rows"] += row_count
            coverage[resource]["pages"] += 1; coverage[resource]["rows"] += row_count
    for _ in coverage:
        for name, item in coverage.items():
            parent = item["parent"]
            if (item["started_chains"] == item["completed_chains"] == item["expected_chains"]
                    and (parent is None or coverage[parent]["status"] == "complete")):
                item["status"] = "complete"
    return counts, coverage


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: _refuse("fetch_scope_ledger_invalid")
        result[key] = value
    return result


def _validate_ledger(value):
    try:
        if (type(value) is not dict or set(value) != {"spec", "plan", "plan_sha256", "status", "stop_reason", "chains", "requests", "expected_paths", "counts", "coverage"}
                or value["spec"] != "oriel.connector.fetch-ledger/v1" or plan_hash(value["plan"]) != value["plan_sha256"]
                or value["status"] not in {"active", "completed", "failed"}):
            _refuse("fetch_scope_ledger_invalid")
        if value["status"] == "failed":
            if type(value["stop_reason"]) is not str or value["stop_reason"] not in _STOP_REASONS:
                _refuse("fetch_scope_ledger_invalid")
        elif value["stop_reason"] is not None:
            _refuse("fetch_scope_ledger_invalid")
        plan = value["plan"]
        if set(value["expected_paths"]) != set(plan["fetch_resources"]): _refuse("fetch_scope_ledger_invalid")
        for name, rule in plan["resources"].items():
            if rule["parent"] is None and value["expected_paths"][name] != [_path_hash(rule["path"])]:
                _refuse("fetch_scope_ledger_invalid")
        for hashes in value["expected_paths"].values():
            if type(hashes) is not list or hashes != sorted(set(hashes)) or any(not _SHA.fullmatch(h) for h in hashes): _refuse("fetch_scope_ledger_invalid")
        seen = set()
        for number, chain in enumerate(value["chains"], 1):
            if (set(chain) != {"id", "resource", "path_sha256", "completed"} or type(chain["id"]) is not int or chain["id"] != number
                    or type(chain["completed"]) is not bool or chain["resource"] not in plan["resources"]
                    or chain["path_sha256"] not in value["expected_paths"][chain["resource"]]
                    or (chain["resource"], chain["path_sha256"]) in seen): _refuse("fetch_scope_ledger_invalid")
            seen.add((chain["resource"], chain["path_sha256"]))
        for number, request in enumerate(value["requests"], 1):
            if (set(request) != {"id", "chain", "request_sha256", "status", "page"} or type(request["id"]) is not int or request["id"] != number
                    or type(request["chain"]) is not int or not 1 <= request["chain"] <= len(value["chains"])
                    or not _SHA.fullmatch(request["request_sha256"]) or request["status"] not in {"pending", "observed"}): _refuse("fetch_scope_ledger_invalid")
            page = request["page"]
            if request["status"] == "pending":
                if page is not None: _refuse("fetch_scope_ledger_invalid")
            elif (type(page) is not dict or set(page) != {"rows", "has_next", "data_sha256", "validated", "child_paths"}
                  or type(page["rows"]) is not int or page["rows"] < 0 or type(page["has_next"]) is not bool
                  or type(page["validated"]) is not bool or not _SHA.fullmatch(page["data_sha256"])):
                _refuse("fetch_scope_ledger_invalid")
            elif page["validated"]:
                resource = value["chains"][request["chain"] - 1]["resource"]
                children = {name for name, rule in plan["resources"].items() if rule["parent"] == resource}
                if type(page["child_paths"]) is not dict or set(page["child_paths"]) != children:
                    _refuse("fetch_scope_ledger_invalid")
                for hashes in page["child_paths"].values():
                    if (type(hashes) is not list or hashes != sorted(set(hashes)) or any(not _SHA.fullmatch(h) for h in hashes)
                            or len(hashes) > page["rows"] or bool(hashes) != bool(page["rows"])):
                        _refuse("fetch_scope_ledger_invalid")
            elif page["child_paths"] is not None:
                _refuse("fetch_scope_ledger_invalid")
        for name, rule in plan["resources"].items():
            if rule["parent"] is None:
                continue
            derived = set()
            for request in value["requests"]:
                if request["status"] == "observed" and request["page"]["validated"]:
                    derived.update(request["page"]["child_paths"].get(name, []))
            if value["expected_paths"][name] != sorted(derived): _refuse("fetch_scope_ledger_invalid")
        for chain in value["chains"]:
            calls = [r for r in value["requests"] if r["chain"] == chain["id"]]
            for index, call in enumerate(calls):
                if index < len(calls) - 1 and (call["status"] != "observed" or not call["page"]["validated"] or not call["page"]["has_next"]): _refuse("fetch_scope_ledger_invalid")
            terminal = bool(calls and calls[-1]["status"] == "observed" and calls[-1]["page"]["validated"] and not calls[-1]["page"]["has_next"])
            if chain["completed"] and not terminal: _refuse("fetch_scope_ledger_invalid")
        counts, coverage = _account(value)
        if _canonical(counts) != _canonical(value["counts"]) or _canonical(coverage) != _canonical(value["coverage"]):
            _refuse("fetch_scope_ledger_invalid")
        if value["status"] == "completed" and (counts["pending"] or counts["requests"] > plan["limits"]["max_pages"]
                or counts["rows"] > plan["limits"]["max_rows"] or any(c["status"] != "complete" for c in coverage.values())
                or any(r["page"] is None or not r["page"]["validated"] for r in value["requests"])):
            _refuse("fetch_scope_ledger_invalid")
        return value
    except FetchScopeError:
        raise
    except (KeyError, ValueError, TypeError, IndexError, RecursionError):
        _refuse("fetch_scope_ledger_invalid")


def read_scope(path):
    path = Path(path).absolute()
    directory = _private_directory(path)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or info.st_size > _LIMIT):
                _refuse("fetch_scope_ledger_invalid")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(_LIMIT + 1)
            if len(raw) > _LIMIT: _refuse("fetch_scope_ledger_invalid")
            value = json.loads(raw, object_pairs_hook=_unique)
            return _validate_ledger(value)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def compact_summary(value):
    _validate_ledger(value)
    return {"spec": "oriel.connector.fetch-scope-summary/v1", "plan_sha256": value["plan_sha256"],
            "source_type": value["plan"]["source_type"], "status": value["status"], "stop_reason": value["stop_reason"],
            "limits": copy.deepcopy(value["plan"]["limits"]), "counts": copy.deepcopy(value["counts"]),
            "coverage": copy.deepcopy(value["coverage"]), "ledger_sha256": plan_hash(value)}
