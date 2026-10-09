"""Offline adapter contracts; dlt/HTTP are doubles, never live vendor evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from contextbase.connectors.upstream import yaml_source as adapter


def find_values(path, value):
    if path == "$":
        return [value]
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return []
        current = current[key]
    return [current]


class Response:
    def __init__(self, body=None, *, raw=None, status=200, headers=None):
        self._content = json.dumps(body).encode() if raw is None else raw
        self.status_code = status
        self.headers = headers or {}
        self.closed = False
    @property
    def content(self): return self._content
    def iter_content(self, chunk_size):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]
    def json(self): return json.loads(self._content)
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError("synthetic response error")
    def close(self): self.closed = True


class StreamingResponse(Response):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks
        self.chunks_read = 0
        self._content = False
    @property
    def content(self):
        raise AssertionError("The adapter must not trigger eager body buffering.")
    def iter_content(self, chunk_size):
        self.requested_chunk_size = chunk_size
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


class BasePaginator:
    def __init__(self): self._has_next_page = True
    @property
    def has_next_page(self): return self._has_next_page


class NativePaginator(BasePaginator):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.next = None
    def init_request(self, request): pass
    def update_state(self, response, data=None):
        self.next = response.json().get("next") if isinstance(response.json(), dict) else None
        self._has_next_page = self.next is not None and self.config["type"] != "single_page"
    def update_request(self, request):
        request.params = {**(request.params or {}), "cursor": self.next}


def fake_modules():
    sources = types.ModuleType("dlt.sources")
    sources.DltResource = object
    dlt = types.ModuleType("dlt")
    dlt.source = lambda function=None, **kwargs: function if function else lambda fn: fn
    dlt.resource = lambda fn, **kwargs: types.SimpleNamespace(function=fn, **kwargs)
    return {
        "dlt": dlt, "dlt.sources": sources,
        "dlt.sources.helpers.rest_client.paginators": types.SimpleNamespace(BasePaginator=BasePaginator),
        "dlt.sources.rest_api.config_setup": types.SimpleNamespace(create_paginator=lambda config: NativePaginator(config)),
        "dlt.common.jsonpath": types.SimpleNamespace(find_values=find_values),
        "requests": types.SimpleNamespace(post=lambda *args, **kwargs: None),
    }


def configuration(resources=None):
    return {"name": "example", "client": {"base_url": "https://api.example.invalid/", "paginator": "single_page",
            "auth": {"type": "bearer", "token": "{api_key}"}},
            "resources": resources or [{"name": "events", "endpoint": {"path": "events"}}]}


class TranslationTests(unittest.TestCase):
    def test_explicit_disabled_child_includes_unselected_dependency(self):
        config = configuration([
            {"name": "repos", "endpoint": {"path": "repos"}},
            {"name": "pulls", "selected": False, "parent": {"resource": "repos", "field": "full_name", "param": "repo"},
             "endpoint": {"path": "repos/{repo}/pulls"}},
        ])
        result = adapter._translate_config(config, {"api_key": "synthetic"}, ["pulls"])
        parent, child = result["resources"]
        self.assertFalse(parent["selected"])
        self.assertTrue(child["selected"])
        self.assertEqual(child["endpoint"]["params"]["repo"], {"type": "resolve", "resource": "repos", "field": "full_name"})
        self.assertEqual([r["name"] for r in adapter._translate_config(config, {"api_key": "synthetic"})["resources"]], ["repos"])

    def test_nested_string_parent_variants(self):
        cases = [({"parent": "accounts", "parent_key": "id", "endpoint": {"path": "accounts/{accountId}/transactions", "path_params": {"accountId": "{parent.id}"}}}, "accountId"),
                 ({"parent": "accounts", "parent_key": "id", "endpoint": {"path": "accounts/{list_id}/members"}}, "list_id"),
                 ({"parent": "accounts", "parent_key": "guild_id", "endpoint": {"path": "accounts/{guild_id}/channels"}}, "guild_id")]
        for declaration, parameter in cases:
            with self.subTest(parameter=parameter):
                config = configuration([{"name": "accounts", "endpoint": {"path": "accounts"}}, {"name": "children", **declaration}])
                result = adapter._translate_config(config, {"api_key": "synthetic"}, ["children"])
                self.assertEqual(result["resources"][1]["endpoint"]["params"][parameter], {"type": "resolve", "resource": "accounts", "field": "id"})

    def test_missing_cycles_and_unknown_selection_refused(self):
        for parent in ("missing", "events"):
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                adapter._translate_config(configuration([{"name": "events", "parent": parent, "endpoint": {"path": "{id}"}}]), {"api_key": "x"})
        with self.assertRaises(ValueError):
            adapter._translate_config(configuration(), {"api_key": "x"}, ["missing"])

    def test_endpoint_auth_and_body_are_effective(self):
        config = configuration([{"name": "events", "auth": "alternate", "endpoint": {"path": "search", "method": "POST", "json": {"filter": {"account": "{account_id}"}}, "paginator": "none"}}])
        config["auth_methods"] = {"alternate": {"type": "http_basic", "username": "{account_id}", "password": "{api_secret}"}}
        result = adapter._translate_config(config, {"api_key": "first", "account_id": "account_001", "api_secret": "second"})
        endpoint = result["resources"][0]["endpoint"]
        self.assertEqual(endpoint["auth"], {"type": "http_basic", "username": "account_001", "password": "second"})
        self.assertEqual(endpoint["json"], {"filter": {"account": "account_001"}})
        self.assertEqual(endpoint["paginator"], {"type": "single_page"})
        self.assertNotIn("_auth_method", result["resources"][0])

    def test_custom_header_and_unresolved_auth(self):
        self.assertEqual(adapter._build_auth({"type": "custom_header", "header_name": "Authorization", "token": "{key}", "header_prefix": "Bot "}, {"key": "synthetic"}),
                         {"type": "api_key", "location": "header", "name": "Authorization", "api_key": "Bot synthetic"})
        with self.assertRaises(ValueError): adapter._build_auth({"type": "bearer", "token": "{missing}"}, {})

    def test_parameter_overrides_fill_only_declared_selected_parameters(self):
        config = configuration([{"name": "events", "endpoint": {"path": "events", "params": {"from_date": None}}},
                                {"name": "other", "endpoint": {"path": "other", "params": {"required": None}}}])
        result = adapter._translate_config(config, {"api_key": "x"}, ["events"], {"events": {"from_date": "2026-01-01"}})
        self.assertEqual(result["resources"][0]["endpoint"]["params"], {"from_date": "2026-01-01"})
        self.assertIsNone(config["resources"][0]["endpoint"]["params"]["from_date"])
        for value in ({}, {"events": {"missing": 1}}, {"events": {"from_date": None}}, {"other": {"required": 1}}, {"events": {"token": "synthetic"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter._translate_config(config, {"api_key": "x"}, ["events"], value)

    def test_null_empty_params_mapping_normalized(self):
        result = adapter._translate_config(configuration([{"name": "events", "endpoint": {"path": "events", "params": None}}]), {"api_key": "x"})
        self.assertEqual(result["resources"][0]["endpoint"]["params"], {})

    def test_datadog_monitor_page_index_does_not_skip_pages(self):
        config = configuration([{"name": "monitors", "endpoint": {"path": "api/v1/monitor", "params": {"page_size": 1000}}}])
        config["name"] = "datadog"
        config["client"]["paginator"] = {"type": "offset", "limit": 100, "limit_param": "page_size", "offset_param": "page"}
        result = adapter._translate_config(config, {"api_key": "synthetic"})
        self.assertEqual(result["resources"][0]["endpoint"]["paginator"], {"type": "page_number", "page_param": "page", "base_page": 0})

    def test_body_content_type_and_incremental_param_translation(self):
        config = configuration([{"name": "events", "endpoint": {"path": "events", "method": "POST", "body": {"limit": 5}, "content_type": "application/json"}, "incremental": {"cursor_path": "updated", "param": "since"}}])
        endpoint = adapter._translate_config(config, {"api_key": "x"})["resources"][0]["endpoint"]
        self.assertEqual(endpoint["json"], {"limit": 5})
        self.assertEqual(endpoint["headers"]["Content-Type"], "application/json")
        self.assertEqual(endpoint["incremental"], {"cursor_path": "updated", "start_param": "since"})

    def test_unknown_endpoint_features_and_write_methods_fail_closed(self):
        for endpoint in ({"path": "x", "method": "DELETE"}, {"path": "x", "unimplemented": True}):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                adapter._translate_config(configuration([{"name": "events", "endpoint": endpoint}]), {"api_key": "x"})

    def test_jsonl_hook_parses_records_and_rejects_errors_bounds(self):
        hook = adapter._response_hook("jsonl", "$")
        with patch.dict(sys.modules, fake_modules()):
            response = hook(Response(raw=b'{"id":1}\n\n{"id":2}\n'))
            self.assertEqual(response.json(), [{"id": 1}, {"id": 2}])
            for raw in (b'{"id":1}\nnot-json', b'[]\n', b'null'):
                with self.subTest(raw=raw), self.assertRaises(ValueError): hook(Response(raw=raw))
            with patch.object(adapter, "MAX_JSONL_ROWS", 2), self.assertRaises(ValueError):
                hook(Response(raw=b'{"id":1}\n{"id":2}\n'))
            with self.assertRaises(RuntimeError): hook(Response(raw=b'{}', status=500))

    def test_missing_response_selector_fails_but_empty_array_is_valid(self):
        with patch.dict(sys.modules, fake_modules()):
            hook = adapter._response_hook(selector="items")
            self.assertEqual(hook(Response({"items": []})).json(), {"items": []})
            with self.assertRaises(ValueError): hook(Response({"wrong": []}))

    def test_response_hook_refuses_redirect_before_following_location(self):
        response = Response({}, status=302, headers={"Location": "https://other.example.invalid/"})
        with self.assertRaisesRegex(ValueError, "redirect"):
            adapter._response_hook()(response)
        self.assertTrue(response.closed)

    def test_rest_streaming_bound_stops_and_closes_before_full_body_read(self):
        response = StreamingResponse([b"1234", b"5678", b"overflow", b"must-not-be-read"])
        with patch.object(adapter, "MAX_RESPONSE_BYTES", 8), self.assertRaisesRegex(ValueError, "bound"):
            adapter._response_hook()(response)
        self.assertEqual(response.chunks_read, 3)
        self.assertLessEqual(response.requested_chunk_size, 9)
        self.assertTrue(response.closed)
        self.assertIs(response._content, False)

    def test_rest_streaming_success_preserves_buffer_for_dlt_json_processing(self):
        response = StreamingResponse([b'{"items":', b'[{"id":1}]}'])
        with patch.dict(sys.modules, fake_modules()):
            result = adapter._response_hook(selector="items")(response)
        self.assertEqual(result.json(), {"items": [{"id": 1}]})
        self.assertTrue(result._content_consumed)

    def test_empty_query_envelope_is_explicitly_supported(self):
        with patch.dict(sys.modules, fake_modules()):
            hook = adapter._response_hook(selector="QueryResponse.Invoice", empty_query_response=True)
            self.assertEqual(hook(Response({"QueryResponse": {}})).json(), {"QueryResponse": {"Invoice": []}})
            with self.assertRaises(ValueError): hook(Response({"Fault": {}}))

    def test_build_source_passes_runtime_objects_and_selected_dependency(self):
        modules = fake_modules()
        observed = {}
        def build(config, **kwargs): observed.update(config=config, kwargs=kwargs); return config
        modules["dlt.sources.rest_api"] = types.SimpleNamespace(rest_api_source=build)
        with patch.dict(sys.modules, modules), patch.object(adapter, "load_yaml_config", return_value=configuration()):
            adapter.build_dlt_source("example", {"api_key": "synthetic"}, ["events"])
        endpoint = observed["config"]["resources"][0]["endpoint"]
        self.assertIsInstance(endpoint["paginator"], BasePaginator)
        self.assertTrue(callable(endpoint["response_actions"][0]))


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules, fake_modules())
        self.modules.start()
        self.addCleanup(self.modules.stop)
    def request(self): return types.SimpleNamespace(url="https://api.example.invalid/items", params={}, json=None, data=None)
    def test_cursor_repetition_and_cross_origin_are_refused(self):
        paginator = adapter._runtime_paginator({"type": "cursor"}, {})
        request = self.request()
        paginator.init_request(request)
        paginator.update_state(Response({"next": "one"}), [{"id": 1}])
        paginator.update_request(request)
        paginator.update_state(Response({"next": "one"}), [{"id": 2}])
        with self.assertRaisesRegex(ValueError, "repeated"): paginator.update_request(request)
        paginator.init_request(request)
        request.url = "https://other.example.invalid/items"
        paginator.update_state(Response({"next": "two"}), [{"id": 2}])
        with self.assertRaisesRegex(ValueError, "origin"): paginator.update_request(request)
    def test_cursor_completion_flag_requires_present_cursor(self):
        paginator = adapter._runtime_paginator({"type": "cursor", "has_more_path": "more"}, {})
        paginator.init_request(self.request())
        for body in ({"more": True}, {"more": True, "next": None}, {"next": "x"}):
            with self.subTest(body=body), self.assertRaises(ValueError): paginator.update_state(Response(body), [{"id": 1}])

    def test_link_origin_change_is_refused_before_next_dispatch(self):
        for destination in ("https://other.example.invalid/items", "http://api.example.invalid/items", "https://api.example.invalid:8443/items"):
            class LinkPaginator(NativePaginator):
                def update_request(self, request): request.url = destination
            with self.subTest(destination=destination), patch.object(sys.modules["dlt.sources.rest_api.config_setup"], "create_paginator", side_effect=LinkPaginator):
                paginator = adapter._runtime_paginator({"type": "json_link"}, {})
                request = self.request()
                paginator.init_request(request)
                dispatched = [request.url]
                paginator.update_state(Response({"next": destination}), [{"id": 1}])
                with self.assertRaisesRegex(ValueError, "origin"):
                    paginator.update_request(request)
                    dispatched.append(request.url)
                self.assertEqual(dispatched, ["https://api.example.invalid/items"])
    def test_page_bound_fails_not_truncates(self):
        paginator = adapter._runtime_paginator({"type": "cursor"}, {})
        paginator.init_request(self.request())
        with patch.object(adapter, "MAX_PAGES", 1), self.assertRaisesRegex(ValueError, "bound"):
            paginator.update_state(Response({"next": "one"}), [{"id": 1}])
    def test_header_page_total_and_missing_header(self):
        paginator = adapter._runtime_paginator({"type": "page_number", "base_page": 1, "total_pages_header": "X-Pages"}, {})
        request = self.request()
        paginator.init_request(request)
        self.assertEqual(request.params["page"], 1)
        paginator.update_state(Response([], headers={"X-Pages": "2"}), [{"id": 1}])
        paginator.update_request(request)
        self.assertEqual(request.params["page"], 2)
        paginator.update_state(Response([], headers={"X-Pages": "2"}), [{"id": 2}])
        self.assertFalse(paginator.has_next_page)
        paginator.init_request(self.request())
        with self.assertRaises(ValueError): paginator.update_state(Response([]), [])
    def test_session_paginator_continuation_and_total_validation(self):
        paginator = adapter._runtime_paginator({"type": "session_page"}, {})
        request = self.request()
        paginator.init_request(request)
        self.assertEqual(request.data, {"page": 0})
        paginator.update_state(Response({"page": 0, "page_size": 2, "total": 3, "session_id": "session-one"}), [{"id": 1}, {"id": 2}])
        paginator.update_request(request)
        self.assertEqual(request.data, {"page": 1, "session_id": "session-one"})
        paginator.update_state(Response({"page": 1, "page_size": 2, "total": 3, "session_id": "session-one"}), [{"id": 3}])
        self.assertFalse(paginator.has_next_page)
        paginator.init_request(self.request())
        with self.assertRaisesRegex(ValueError, "omitted"):
            paginator.update_state(Response({"page": 0, "page_size": 2, "total": 3, "session_id": "session-one"}), [{"id": 1}])
    def test_session_paginator_refuses_changed_session(self):
        paginator = adapter._runtime_paginator({"type": "session_page"}, {})
        request = self.request(); paginator.init_request(request)
        paginator.update_state(Response({"page": 0, "page_size": 1, "total": 2, "session_id": "one"}), [{"id": 1}])
        paginator.update_request(request)
        with self.assertRaisesRegex(ValueError, "changed"):
            paginator.update_state(Response({"page": 1, "page_size": 1, "total": 2, "session_id": "two"}), [{"id": 2}])
    def test_post_body_page_size_and_native_controls(self):
        endpoint = {"method": "POST", "json": {"query_filter": "active"}}
        paginator = adapter._runtime_paginator({"type": "page_number", "page_param": "page", "page_size_param": "size", "page_size": 50}, endpoint)
        self.assertEqual(endpoint["json"]["size"], 50)
        self.assertEqual(paginator.inner.config["page_body_path"], "page")
        self.assertNotIn("page_size", paginator.inner.config)

    def test_range_paginators_do_not_invent_total_contract(self):
        for config in ({"type": "page_number"}, {"type": "offset", "limit": 2},
                       {"type": "offset", "limit": 2, "has_more_path": "more"}):
            with self.subTest(config=config):
                paginator = adapter._runtime_paginator(config, {})
                self.assertIsNone(paginator.inner.config["total_path"])
        paginator = adapter._runtime_paginator({"type": "page_number", "total_path": "meta.pages"}, {})
        self.assertEqual(paginator.inner.config["total_path"], "meta.pages")

    def test_query_body_offsets_are_advanced_in_the_body(self):
        endpoint = {"method": "POST", "data": "SELECT * FROM Invoice", "params": {"maxResults": 2}}
        paginator = adapter._runtime_paginator({"type": "offset", "offset": 1, "offset_param": "startPosition", "limit_param": "maxResults"}, endpoint)
        request = self.request(); request.data = endpoint["data"]
        paginator.init_request(request)
        self.assertEqual(request.data, "SELECT * FROM Invoice STARTPOSITION 1 MAXRESULTS 2")
        self.assertEqual(endpoint["params"], {})
        paginator.update_state(Response({}), [{"id": 1}, {"id": 2}])
        paginator.update_request(request)
        self.assertEqual(request.data, "SELECT * FROM Invoice STARTPOSITION 3 MAXRESULTS 2")
        paginator.update_state(Response({}), [{"id": 3}])
        self.assertFalse(paginator.has_next_page)

    def test_initial_sync_requires_completion_marker(self):
        config = adapter._build_paginator({"type": "cursor", "cursor_param": "__next_page_url__", "cursor_path": "nextPageUrl", "next_sync_url_path": "nextSyncUrl"})
        self.assertEqual(config, {"type": "json_link", "next_url_path": "nextPageUrl", "completion_url_path": "nextSyncUrl"})
        paginator = adapter._runtime_paginator(config, {})
        paginator.init_request(self.request())
        with self.assertRaisesRegex(ValueError, "completion"):
            paginator.update_state(Response({}), [])
        paginator.init_request(self.request())
        paginator.update_state(Response({"nextSyncUrl": "https://api.example.invalid/sync?cursor=opaque"}), [])
        self.assertFalse(paginator.has_next_page)


class GraphQLTests(unittest.TestCase):
    def setUp(self):
        modules = fake_modules()
        self.http = modules["requests"]
        path = Path(adapter.__file__).parent / "sources/graphql.py"
        spec = importlib.util.spec_from_file_location("_tested_connector_graphql", path)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules): spec.loader.exec_module(self.module)
    def body(self, nodes, more=False, cursor=None):
        return {"data": {"items": {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}
    def run_pages(self, bodies, **options):
        responses = [Response(body) for body in bodies]
        with patch.object(self.http, "post", side_effect=responses) as post:
            rows = list(self.module._paginate("https://api.example.invalid/graphql", {"Authorization": "synthetic"}, "query($cursor:String){items{id}}", {}, "data.items.nodes", "data.items.pageInfo", **options))
        return rows, responses, post.call_args_list
    def test_two_pages_and_empty_terminal_page(self):
        rows, responses, calls = self.run_pages([self.body([{"id": 1}], True, "one"), self.body([])])
        self.assertEqual(rows, [{"id": 1}])
        self.assertEqual(calls[1].kwargs["json"]["variables"], {"cursor": "one"})
        self.assertFalse(calls[0].kwargs["allow_redirects"])
        self.assertTrue(calls[0].kwargs["stream"])
        self.assertTrue(all(response.closed for response in responses))
    def test_missing_cursor_repeated_cursor_and_missing_data_fail(self):
        cases = [[self.body([{"id": 1}], True, None)],
                 [self.body([{"id": 1}], True, "one"), self.body([{"id": 2}], True, "one")],
                 [{"data": {}}], [{"data": {"items": {"nodes": []}}}],
                 [self.body([], True, "one")]]
        for bodies in cases:
            with self.subTest(bodies=bodies), self.assertRaises(RuntimeError): self.run_pages(bodies)
    def test_error_message_does_not_echo_graphql_errors(self):
        with self.assertRaises(RuntimeError) as error:
            self.run_pages([{"errors": [{"message": "synthetic-private-value"}]}])
        self.assertNotIn("synthetic-private-value", str(error.exception))
    def test_page_bound_and_mutation_refused(self):
        with self.assertRaisesRegex(RuntimeError, "bound"):
            self.run_pages([self.body([{"id": 1}], True, "one")], max_pages=1)
        with patch.object(self.http, "post") as post, self.assertRaises(ValueError):
            list(self.module._paginate("https://example.invalid", {}, "mutation { change }", {}, "data", None))
        post.assert_not_called()
    def test_non_success_response_closed(self):
        response = Response({}, status=302)
        with patch.object(self.http, "post", return_value=response), self.assertRaises(RuntimeError):
            list(self.module._paginate("https://example.invalid", {}, "{ items }", {}, "data", None))
        self.assertTrue(response.closed)

    def test_graphql_streaming_bound_stops_before_full_response_read(self):
        response = StreamingResponse([b"1234", b"5678", b"overflow", b"must-not-be-read"])
        with patch.object(self.http, "post", return_value=response) as post, patch.object(self.module, "MAX_RESPONSE_BYTES", 8), self.assertRaisesRegex(RuntimeError, "bound"):
            list(self.module._paginate("https://example.invalid", {}, "{ items }", {}, "data", None))
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(response.chunks_read, 3)
        self.assertTrue(response.closed)
        self.assertIs(response._content, False)

    def test_declared_page_resources_continue_until_short_page(self):
        query = "query($page: Int) { boards(limit: 2, page: $page, state: active) { id } }"
        responses = [Response({"data": {"boards": [{"id": "a"}, {"id": "b"}]}}),
                     Response({"data": {"boards": [{"id": "c"}]}})]
        with patch.object(self.http, "post", side_effect=responses) as post:
            resource = self.module._make_resource("boards", "https://example.invalid", {}, query, "data.boards", None, {}, "id")
            self.assertEqual(list(resource.function()), [{"id": "a"}, {"id": "b"}, {"id": "c"}])
        self.assertEqual([call.kwargs["json"]["variables"] for call in post.call_args_list], [{"page": 1}, {"page": 2}])
        self.assertTrue(all(response.closed for response in responses))

    def test_page_adapter_refuses_repetition_bound_and_owned_variables(self):
        query = "query($page: Int) { boards(limit: 1, page: $page) { id } }"
        response = {"data": {"boards": [{"id": "a"}]}}
        with patch.object(self.http, "post", side_effect=[Response(response), Response(response)]), self.assertRaisesRegex(RuntimeError, "repeated"):
            list(self.module._paginate_pages("https://example.invalid", {}, query, {}, "data.boards"))
        with patch.object(self.http, "post", return_value=Response(response)), patch.object(self.module, "MAX_PAGES", 1), self.assertRaisesRegex(RuntimeError, "bound"):
            list(self.module._paginate_pages("https://example.invalid", {}, query, {}, "data.boards"))
        with patch.object(self.http, "post") as post, self.assertRaises(ValueError):
            list(self.module._paginate_pages("https://example.invalid", {}, query, {"page": 2}, "data.boards"))
        post.assert_not_called()

    def board_query(self):
        return "query($page: Int) { boards(limit: 2, page: $page) { id items_page(limit: 1) { cursor items { id name board { id name } } } } }"

    def item(self, item_id, board_id):
        return {"id": item_id, "name": "synthetic", "board": {"id": board_id, "name": "synthetic"}}

    def test_board_items_flatten_follow_cursors_and_paginate_boards(self):
        one, two, three = self.item("one", "board_a"), self.item("two", "board_a"), self.item("three", "board_b")
        responses = [Response({"data": {"boards": [
            {"id": "board_a", "items_page": {"cursor": "next-a", "items": [one]}},
            {"id": "board_b", "items_page": {"cursor": None, "items": [three]}}]}}),
            Response({"data": {"next_items_page": {"cursor": None, "items": [two]}}}),
            Response({"data": {"boards": []}})]
        with patch.object(self.http, "post", side_effect=responses) as post:
            resource = self.module._make_resource("items", "https://example.invalid", {}, self.board_query(), "data.boards", None, {}, "id")
            self.assertEqual(list(resource.function()), [one, two, three])
        calls = post.call_args_list
        self.assertEqual(calls[0].kwargs["json"]["variables"], {"page": 1})
        self.assertEqual(calls[1].kwargs["json"]["variables"], {"cursor": "next-a"})
        self.assertIn("next_items_page(limit: 1, cursor: $cursor)", calls[1].kwargs["json"]["query"])
        self.assertIn("board { id name }", calls[1].kwargs["json"]["query"])
        self.assertEqual(calls[2].kwargs["json"]["variables"], {"page": 2})
        self.assertTrue(all(response.closed for response in responses))

    def test_board_items_refuse_missing_cursor_wrong_board_and_repetition(self):
        one = self.item("one", "board_a")
        for page in ({"items": [one]}, {"cursor": "", "items": [one]},
                     {"cursor": None, "items": [self.item("one", "wrong")]},
                     {"cursor": "next", "items": []}):
            with self.subTest(page=page), patch.object(self.http, "post", return_value=Response({"data": {"boards": [{"id": "board_a", "items_page": page}]}})), self.assertRaises(RuntimeError):
                list(self.module._paginate_board_items("https://example.invalid", {}, self.board_query(), {}, "data.boards"))
        responses = [Response({"data": {"boards": [{"id": "board_a", "items_page": {"cursor": "next", "items": [one]}}]}}),
                     Response({"data": {"next_items_page": {"cursor": "next", "items": [self.item("two", "board_a")]}}})]
        with patch.object(self.http, "post", side_effect=responses), self.assertRaisesRegex(RuntimeError, "repeated"):
            list(self.module._paginate_board_items("https://example.invalid", {}, self.board_query(), {}, "data.boards"))

    def test_board_items_share_total_request_bound(self):
        response = Response({"data": {"boards": [{"id": "board_a", "items_page": {"cursor": "next", "items": [self.item("one", "board_a")]}}]}})
        with patch.object(self.http, "post", return_value=response) as post, patch.object(self.module, "MAX_PAGES", 1), self.assertRaisesRegex(RuntimeError, "bound"):
            list(self.module._paginate_board_items("https://example.invalid", {}, self.board_query(), {}, "data.boards"))
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__": unittest.main()
