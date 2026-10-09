"""Packaged YAML read-resource adapters for the pinned dlt REST API interface.

Configuration translation is pure. Runtime hooks use dlt's paginator/auth
implementations, with explicit completeness bounds; no write endpoints execute.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from urllib.parse import urlparse

MAX_PAGES = 10_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSONL_ROWS = 100_000
_SENSITIVE = re.compile(r"token|secret|password|credential|authorization|api.?key", re.I)
_ROUTING = {"query", "mutation", "script", "code", "path", "url", "endpoint", "base_url", "method", "headers", "auth", "resources"}


def load_yaml_config(source_name):
    from contextbase.connectors.upstream.source_config import load_source_config
    return load_source_config(source_name)


def _substitute(value, credentials):
    if isinstance(value, dict):
        return {key: _substitute(item, credentials) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, credentials) for item in value]
    if isinstance(value, str):
        return re.sub(r"\{(\w+(?:\.\w+)*)\}",
                      lambda match: str(credentials.get(match[1], match[0])) if "." not in match[1] else match[0], value)
    return value


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _parent_name(resource):
    parent = resource.get("parent")
    return parent.get("resource") if isinstance(parent, dict) else parent


def _safe_parameter(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _SENSITIVE.search(key) or key.lower() in _ROUTING:
                raise ValueError("Resource parameters cannot override authentication or routing.")
            _safe_parameter(item)
    elif isinstance(value, list):
        for item in value:
            _safe_parameter(item)
    elif isinstance(value, str) and re.search(r"://[^/\s]*@", value):
        raise ValueError("Resource parameters cannot contain credential-bearing URLs.")
    json.dumps(value, allow_nan=False)


def _apply_parameters(endpoint, overrides):
    if not isinstance(overrides, dict):
        raise ValueError("Resource parameters must be a mapping.")
    _safe_parameter(overrides)
    locations = {key: value for key, value in endpoint.items() if key in {"params", "json", "body"} and isinstance(value, dict)}
    for key, value in overrides.items():
        targets = [items for items in locations.values() if key in items]
        if len(targets) != 1 or value is None:
            raise ValueError("Override one existing non-null resource query/body parameter.")
        targets[0][key] = copy.deepcopy(value)
    for items in locations.values():
        if any(value is None for value in items.values()):
            raise ValueError("Selected resource requires values for declared null parameters.")


def _build_auth(auth, credentials):
    if auth is None or auth == "none":
        return None
    if not isinstance(auth, dict):
        raise ValueError("Authentication must be an explicit packaged mapping.")
    result = _substitute(auth, credentials)
    kind = result.get("type", "bearer")
    if kind == "custom_header":
        name = result.get("header_name", result.get("header"))
        value = result.get("header_value", result.get("header_prefix", "") + result.get("token", ""))
        if not name or not value:
            raise ValueError("Custom-header authentication is incomplete.")
        if re.search(r"\{\w+\}", value):
            raise ValueError("Connector credential reference was not resolved.")
        return {"type": "api_key", "location": "header", "name": name, "api_key": value}
    if kind not in {"bearer", "http_basic", "api_key"}:
        raise ValueError("Unsupported read-connector authentication type.")
    required = {"bearer": ("token",), "http_basic": ("username", "password"), "api_key": ("api_key",)}[kind]
    if any(key not in result or not isinstance(result[key], str) for key in required):
        raise ValueError("Connector authentication is incomplete.")
    if any(re.search(r"\{\w+\}", result[key]) for key in required):
        raise ValueError("Connector credential reference was not resolved.")
    return result


def _build_paginator(config):
    if config is None or config == "none":
        return {"type": "single_page"}
    result = {"type": config} if isinstance(config, str) else copy.deepcopy(config)
    if not isinstance(result, dict):
        raise ValueError("Paginator must be a mapping or supported name.")
    result["type"] = {"none": "single_page", "page": "page_number"}.get(result.get("type"), result.get("type", "auto"))
    for old, new in (("default_limit", "limit"), ("offset_start", "offset"), ("start_page", "base_page"), ("more_key", "has_more_path")):
        if old in result:
            if new in result and result[new] != result[old]:
                raise ValueError("Conflicting pagination settings.")
            result[new] = result.pop(old)
    if result.get("stop_condition") not in (None, "len(data) < page_size"):
        raise ValueError("Unsupported pagination stopping rule.")
    if result.get("next_page_header", "").lower() == "link":
        result.pop("next_page_header")
        result["type"] = "header_link"
        for key in ("page_param", "base_page", "page", "total_path"):
            result.pop(key, None)
    if result.get("cursor_param") == "__next_page_url__" and result.get("next_sync_url_path"):
        # Every worker starts a fresh full snapshot. Follow initial-sync pages;
        # do not claim persistent incremental-token state across worker runs.
        result = {"type": "json_link", "next_url_path": result["cursor_path"],
                  "completion_url_path": result["next_sync_url_path"]}
    return result


def _resolve_parent(resource, declared):
    parent_info = resource.pop("_parent", None)
    if parent_info is None:
        return
    name = parent_info.get("resource") if isinstance(parent_info, dict) else parent_info
    if name not in declared:
        raise ValueError("Parent resource is missing from packaged configuration.")
    endpoint = resource["endpoint"]
    path_params = endpoint.pop("path_params", {})
    bindings = {}
    if isinstance(parent_info, dict):
        bindings[parent_info["param"]] = parent_info["field"]
    elif path_params:
        for param, field in path_params.items():
            match = re.fullmatch(r"\{parent\.([\w.]+)\}", field) if isinstance(field, str) else None
            if not match:
                raise ValueError("Parent path bindings must reference an explicit parent field.")
            bindings[param] = match[1]
    else:
        placeholders = re.findall(r"\{(\w+)\}", endpoint.get("path", ""))
        key = resource.pop("_parent_key", "id")
        if len(placeholders) != 1:
            raise ValueError("Parent resource needs an unambiguous path binding.")
        # Discord's parent_key declares the path variable; the parent object's
        # primary key is id. Mailchimp declares the source key instead.
        parent_field = declared[name].get("primary_key", "id") if key == placeholders[0] else key
        if not isinstance(parent_field, str):
            raise ValueError("Parent resource requires one explicit key field.")
        bindings[placeholders[0]] = parent_field
    resource.pop("_parent_key", None)
    params = endpoint.setdefault("params", {})
    for param, field in bindings.items():
        if "{" + param + "}" not in endpoint.get("path", "") or param in params:
            raise ValueError("Parent path binding is missing or conflicts with a query parameter.")
        params[param] = {"type": "resolve", "resource": name, "field": field}


def _build_endpoint(endpoint, credentials):
    supported = {"path", "method", "params", "headers", "data_selector", "auth", "paginator", "json", "data", "body", "content_type", "base_url", "path_params", "response_format", "incremental"}
    if set(endpoint) - supported:
        raise ValueError("Unsupported endpoint configuration field.")
    result = _substitute(copy.deepcopy(endpoint), credentials)
    result["params"] = result.get("params") or {}
    if not isinstance(result["params"], dict) or not isinstance(result.get("headers", {}), dict):
        raise ValueError("Endpoint parameters and headers must be mappings.")
    if "body" in result:
        body = result.pop("body")
        target = "json" if isinstance(body, (dict, list)) else "data"
        if target in result:
            raise ValueError("Conflicting request body fields.")
        result[target] = body
    if "content_type" in result:
        result.setdefault("headers", {})["Content-Type"] = result.pop("content_type")
    if "base_url" in result:
        result["path"] = result.pop("base_url").rstrip("/") + "/" + result.get("path", "").lstrip("/")
    if "auth" in result:
        result["auth"] = _build_auth(result["auth"], {})
    if "paginator" in result:
        result["paginator"] = _build_paginator(result["paginator"])
    if result.get("method", "GET").upper() not in {"GET", "POST"}:
        raise ValueError("Read extraction supports only declared GET/POST resources.")
    return result


def _translate_config(config, credentials, resource_names=None, params=None):
    declared = {item["name"]: item for item in config.get("resources", [])}
    if len(declared) != len(config.get("resources", [])):
        raise ValueError("Resource names must be unique.")
    selected = set(resource_names) if resource_names else {name for name, item in declared.items() if item.get("selected", True)}
    if not selected or selected - set(declared):
        raise ValueError("Select existing read resources.")
    overrides = params or {}
    if not isinstance(overrides, dict):
        raise ValueError("Parameters must address selected resources or their dependencies.")
    needed = set()
    def include(name, stack):
        if name not in declared or name in stack:
            raise ValueError("Resource parent dependency is missing or cyclic.")
        needed.add(name)
        parent = _parent_name(declared[name])
        if parent:
            include(parent, stack | {name})
    for name in selected:
        include(name, set())
    if set(overrides) - needed:
        raise ValueError("Parameters must address selected resources or their dependencies.")
    client = copy.deepcopy(config.get("client", {}))
    if set(client) - {"base_url", "auth", "headers", "paginator"}:
        raise ValueError("Unsupported client configuration field.")
    client_paginator = client.pop("paginator", None)
    default_endpoint = config.get("resource_defaults", {}).get("endpoint", {})
    translated = []
    for name, resource in declared.items():
        if name not in needed:
            continue
        endpoint = _merge(default_endpoint, resource.get("endpoint", {}))
        endpoint["params"] = endpoint.get("params") or {}
        _apply_parameters(endpoint, overrides.get(name, {}))
        if "paginator" not in endpoint:
            endpoint["paginator"] = resource.get("paginator", client_paginator or "auto")
        if config.get("name") == "mixpanel" and name == "engage":
            endpoint["paginator"] = {"type": "session_page"}
            endpoint["data"] = endpoint.pop("params")
        if config.get("name") == "datadog" and name == "monitors":
            # This endpoint uses a page index; the inherited offset setting
            # would jump by page_size and silently skip whole pages.
            endpoint["paginator"] = {"type": "page_number", "page_param": "page", "base_page": 0}
        if resource.get("auth"):
            auth_ref = resource["auth"]
            endpoint["auth"] = config.get("auth_methods", {}).get(auth_ref, auth_ref)
        endpoint = _build_endpoint(endpoint, credentials)
        if resource.get("incremental"):
            incremental = copy.deepcopy(resource["incremental"])
            if "param" in incremental:
                incremental["start_param"] = incremental.pop("param")
            endpoint["incremental"] = incremental
        item = {key: copy.deepcopy(value) for key, value in config.get("resource_defaults", {}).items() if key != "endpoint"}
        item.update({key: copy.deepcopy(resource[key]) for key in ("primary_key", "write_disposition") if key in resource})
        item.update(name=name, endpoint=endpoint, selected=name in selected)
        if _parent_name(resource):
            item.update(_parent=resource["parent"], _parent_key=resource.get("parent_key", "id"))
        _resolve_parent(item, declared)
        if "path_params" in endpoint:
            raise ValueError("Path parameters need a declared parent resource.")
        translated.append(item)
    result = {"client": _substitute(client, credentials), "resources": translated}
    if "auth" in client:
        result["client"]["auth"] = _build_auth(client["auth"], credentials)
    return result


def _buffer_response(response, max_bytes, *, deadline_check=None):
    """Bound decoded bytes while reading, before requests eagerly buffers them."""
    chunks = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=min(64 * 1024, max_bytes + 1)):
            if deadline_check is not None:
                deadline_check()
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Source response exceeded the extraction bound.")
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
        return response._content
    except Exception:
        response.close()
        raise


def _response_hook(response_format="json", selector=None, *, empty_query_response=False):
    def process(response):
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            # requests invokes response hooks before following redirects.
            # In particular, never forward a custom API-key header to a
            # redirect target chosen by a source response.
            raise ValueError("Source response redirects are not allowed.")
        content = _buffer_response(response, MAX_RESPONSE_BYTES)
        if response_format == "jsonl":
            try:
                records = [json.loads(line) for line in content.splitlines() if line.strip()]
                if len(records) >= MAX_JSONL_ROWS or any(not isinstance(item, dict) for item in records):
                    raise ValueError()
                response._content = json.dumps(records, allow_nan=False).encode()
                response.encoding = "utf-8"
            except (ValueError, UnicodeError):
                raise ValueError("JSONL response is invalid or reached its completeness bound.") from None
        elif response_format != "json":
            raise ValueError("Unsupported response format.")
        if selector is not None:
            from dlt.common.jsonpath import find_values
            if not find_values(selector, response.json()):
                body = response.json()
                if empty_query_response and isinstance(body, dict) and body.get("QueryResponse") == {}:
                    field = selector.removeprefix("QueryResponse.")
                    body["QueryResponse"][field] = []
                    response._content = json.dumps(body).encode()
                else:
                    raise ValueError("Declared source response selector is missing.")
        return response
    def inspect(response, *args, **kwargs):
        try:
            return process(response)
        except Exception:
            response.close()
            raise
    return inspect


def _runtime_paginator(config, endpoint, *, fetch_scope=None, resource_name=None):
    """Wrap native dlt pagination; bounded adapters cover header/session variants."""
    from dlt.sources.helpers.rest_client.paginators import BasePaginator
    from dlt.sources.rest_api.config_setup import create_paginator
    from dlt.common.jsonpath import find_values
    settings = copy.deepcopy(config)
    kind = settings.get("type", "auto")
    size = settings.pop("page_size", None)
    size_param = settings.pop("page_size_param", None)
    if size_param and size is not None:
        endpoint.setdefault("json" if endpoint.get("method", "GET").upper() == "POST" and "json" in endpoint else "params", {}).setdefault(size_param, size)
    short_page = settings.pop("stop_condition", None) is not None
    current_path = settings.pop("current_page_path", None)
    total_header = settings.pop("total_pages_header", settings.pop("total_path_header", None))
    next_header = settings.pop("next_page_header", None)
    completion_path = settings.pop("completion_url_path", None)
    query_body = endpoint.get("data")
    query_offset = (kind == "offset" and settings.get("offset_param") == "startPosition"
                    and isinstance(query_body, str) and re.fullmatch(r"SELECT\s+[^;]+", query_body, re.I))
    if query_offset:
        settings["limit"] = settings.get("limit", endpoint.get("params", {}).get(settings.get("limit_param", "maxResults")))
        if type(settings["limit"]) is not int or not 1 <= settings["limit"] <= 1000:
            raise ValueError("Query-body pagination requires a bounded page limit.")
        for key in (settings.get("offset_param"), settings.get("limit_param")):
            endpoint.get("params", {}).pop(key, None)
    if kind == "cursor" and "limit" in settings:
        limit = settings.pop("limit")
        endpoint.setdefault("params", {}).setdefault(settings.pop("limit_param", "limit"), limit)
    # JSON POST resources carry paging controls alongside the declared body.
    if "json" in endpoint:
        for source, target in (("page_param", "page_body_path"), ("offset_param", "offset_body_path"), ("limit_param", "limit_body_path"), ("cursor_param", "cursor_body_path")):
            if source in settings:
                settings[target] = settings.pop(source)
    if kind == "offset" and "limit" not in settings:
        raise ValueError("Offset pagination requires an explicit page limit.")
    if kind in {"offset", "page_number"}:
        # dlt defaults to a 'total' field. The packaged declarations instead
        # use empty pages/explicit has-more/short-page rules unless a total
        # path is actually provided. Do not invent that response contract.
        settings.setdefault("total_path", None)
    special = kind == "session_page" or total_header or next_header or query_offset
    inner = None if special or kind == "auto" else create_paginator(settings)
    if inner is None and not special and kind != "auto":
        raise ValueError("Pagination is not implemented.")

    class BoundedPaginator(BasePaginator):
        def __init__(self):
            super().__init__()
            self.inner = copy.deepcopy(inner)
            self.pages = 0
            self.seen = set()
            self.page = settings.get("offset", 1) if query_offset else settings.get("base_page", 0 if kind == "session_page" else 1)
            self.next_page = self.page
            self.session = None
            self.total = None
            self.seen_rows = 0
            self.origin = None
        def init_request(self, request):
            # A child paginator is reused for each parent record by dlt.
            self.__init__()
            self.origin = (urlparse(request.url).scheme, urlparse(request.url).netloc)
            if self.inner:
                self.inner.init_request(request)
            elif special:
                self._set_page(request)
            self._remember(request)
            if fetch_scope is not None:
                fetch_scope.begin_chain(resource_name, request)
        def _set_page(self, request):
            if query_offset:
                request.data = query_body + f" STARTPOSITION {self.next_page} MAXRESULTS {settings['limit']}"
                return
            field = "data" if kind == "session_page" else ("json" if "json" in endpoint else "params")
            values = dict(getattr(request, field, None) or {})
            values[settings.get("page_param", settings.get("page_body_path", "page"))] = self.next_page
            if self.session is not None:
                values["session_id"] = self.session
            setattr(request, field, values)
        def _remember(self, request):
            if (urlparse(request.url).scheme, urlparse(request.url).netloc) != self.origin:
                raise ValueError("Pagination cannot change the source origin.")
            state = json.dumps([request.url, getattr(request, "params", None), getattr(request, "json", None), getattr(request, "data", None)], sort_keys=True)
            marker = hashlib.sha256(state.encode()).hexdigest()
            if marker in self.seen:
                raise ValueError("Pagination repeated a request.")
            self.seen.add(marker)
        def update_state(self, response, data=None):
            self.pages += 1
            if kind == "session_page":
                body = response.json()
                required = (body.get("page"), body.get("page_size"), body.get("total"))
                if (any(type(value) is not int for value in required) or required[0] != self.next_page
                        or required[1] <= 0 or required[2] < 0 or not isinstance(body.get("session_id"), str)
                        or not body["session_id"] or not isinstance(data, list)):
                    raise ValueError("Session pagination response is incomplete.")
                if self.session is not None and (self.session != body["session_id"] or self.total != body["total"]):
                    raise ValueError("Session pagination changed its observation.")
                self.session, self.total = body["session_id"], body["total"]
                self.seen_rows += len(data)
                self._has_next_page = self.seen_rows < self.total
                if self.seen_rows > self.total or (self._has_next_page and len(data) != body["page_size"]):
                    raise ValueError("Session pagination omitted rows.")
                self.next_page += 1
            elif query_offset:
                if not isinstance(data, list) or len(data) > settings["limit"]:
                    raise ValueError("Query pagination returned an invalid page.")
                self.next_page += settings["limit"]
                self._has_next_page = len(data) == settings["limit"]
            elif special:
                if next_header:
                    raw = response.headers.get(next_header)
                    if raw is None:
                        raise ValueError("Required pagination header is missing.")
                    self._has_next_page = bool(raw)
                    if self._has_next_page:
                        try: self.next_page = int(raw)
                        except (TypeError, ValueError): raise ValueError("Invalid pagination header.") from None
                        if self.next_page <= self.page:
                            raise ValueError("Pagination page did not advance.")
                        self.page = self.next_page
                else:
                    try: total = int(response.headers[total_header])
                    except (KeyError, TypeError, ValueError): raise ValueError("Required pagination total header is missing.") from None
                    if total < 0:
                        raise ValueError("Invalid pagination total.")
                    if total_header.lower() == "x-pw-total":
                        if not size: raise ValueError("Record-total pagination needs a page size.")
                        total = (total + size - 1) // size
                    self.next_page += 1
                    self._has_next_page = self.next_page < settings.get("base_page", 1) + total
            else:
                if self.inner is None:
                    from dlt.sources.helpers.rest_client.detector import PaginatorFactory
                    self.inner, _ = PaginatorFactory().create_paginator(response)
                    if self.inner is None:
                        raise ValueError("Cannot detect source pagination.")
                if settings.get("has_more_path"):
                    more = find_values(settings["has_more_path"], response.json())
                    if len(more) != 1 or type(more[0]) is not bool:
                        raise ValueError("Required pagination completion flag is missing.")
                    if more[0]:
                        cursors = find_values(settings.get("cursor_path", "next"), response.json())
                        if not data or (kind == "cursor" and (len(cursors) != 1 or cursors[0] in (None, ""))):
                            raise ValueError("Pagination has more rows without data or a cursor.")
                if current_path:
                    values = find_values(current_path, response.json())
                    if (len(values) != 1 or type(values[0]) is not int
                            or values[0] != getattr(self.inner, "current_value", values[0])):
                        raise ValueError("Current page evidence is missing.")
                self.inner.update_state(response, data)
                self._has_next_page = self.inner.has_next_page
                if completion_path and not self._has_next_page:
                    completion = find_values(completion_path, response.json())
                    if len(completion) != 1 or not isinstance(completion[0], str) or not completion[0]:
                        raise ValueError("Initial sync omitted its completion marker.")
                if short_page and size and len(data or []) < size:
                    self._has_next_page = False
            if self._has_next_page and self.pages >= MAX_PAGES:
                raise ValueError("Pagination exceeded the completeness bound.")
            if special and self._has_next_page and not data:
                raise ValueError("Pagination omitted rows before its declared end.")
            if fetch_scope is not None:
                fetch_scope.observe_page(resource_name, response, data, self.has_next_page)
        def update_request(self, request):
            if not self.has_next_page:
                return
            if self.inner: self.inner.update_request(request)
            else: self._set_page(request)
            self._remember(request)
    return BoundedPaginator()


def build_dlt_source(source_name, credentials, resource_names=None, params=None, *, http_session=None, fetch_scope=None):
    config = load_yaml_config(source_name)
    if config is None:
        raise ValueError("No packaged source configuration exists.")
    if fetch_scope is not None:
        fetch_scope.verify_declaration(source_name, config, resource_names, params)
    translated = _translate_config(config, credentials, resource_names, params)
    if fetch_scope is not None and http_session is None:
        raise ValueError("Fetch scope requires the guarded extraction transport.")
    if http_session is not None:
        # One explicitly injected transport across all resources/dependencies.
        # This is never a vendor parameter or a discovered ambient session.
        translated["client"]["session"] = http_session
    for resource in translated["resources"]:
        endpoint = resource["endpoint"]
        response_format = endpoint.pop("response_format", "json")
        if response_format not in {"json", "jsonl"}:
            raise ValueError("Unsupported response format.")
        if response_format == "jsonl":
            endpoint["data_selector"] = "$"
            endpoint["paginator"] = {"type": "single_page"}
        endpoint["response_actions"] = [_response_hook(response_format, endpoint.get("data_selector"),
            empty_query_response=source_name == "quickbooks" and str(endpoint.get("data_selector", "")).startswith("QueryResponse."))]
        endpoint["paginator"] = _runtime_paginator(endpoint["paginator"], endpoint,
                                                  fetch_scope=fetch_scope, resource_name=resource["name"])
    from dlt.sources.rest_api import rest_api_source
    return rest_api_source(translated, name=source_name)


def get_write_endpoints(source_name):
    config = load_yaml_config(source_name) or {}
    return config.get("write_endpoints", []) or [item for item in config.get("endpoints", []) if item.get("write", False)]
