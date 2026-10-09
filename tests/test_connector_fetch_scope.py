"""Independent wire-scope contracts using prepared requests, never vendor calls.

These tests demonstrate bounded local enforcement, not vendor field/history or
permission verification. Configurations are synthetic unless explicitly noted.
"""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from contextbase.connectors import fetch_scope


ORIGIN = "http://127.0.0.1:18493"


def configuration(resources=None, *, paginator="single_page"):
    return {
        "name": "synthetic", "client": {"base_url": ORIGIN + "/v1/", "paginator": paginator},
        "resource_defaults": {"primary_key": "id", "endpoint": {"data_selector": "data"}},
        "resources": resources or [{"name": "events", "endpoint": {"path": "events", "params": {"status": "open"}}}],
    }


def catalog_entry(config):
    return {
        "name": "synthetic", "execution_family": "dlt_rest", "credential_specs": [],
        "resource_discovery": "declared", "resource_defaults": copy.deepcopy(config.get("resource_defaults", {})),
        "resources": [{"name": row["name"], "selected": row.get("selected", True), "declared": copy.deepcopy(row)}
                      for row in config["resources"]],
    }


def parent_configuration():
    return configuration([
        {"name": "accounts", "selected": False, "endpoint": {"path": "accounts"}},
        {"name": "records", "parent": {"resource": "accounts", "field": "id", "param": "account_id"},
         "endpoint": {"path": "accounts/{account_id}/records"}},
    ])


class FetchScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.serial = 0

    def tearDown(self):
        self.temporary.cleanup()

    def compile(self, config=None, *, selected=None, scope=None, params=None, entry=None):
        config = configuration() if config is None else config
        selected = ["events"] if selected is None else selected
        scope = {"origin": ORIGIN, "fetch_resources": sorted(row["name"] for row in config["resources"]),
                 "max_pages": 20, "max_rows": 100} if scope is None else scope
        with ExitStack() as stack:
            stack.enter_context(patch("contextbase.connectors.describe", return_value=copy.deepcopy(entry or catalog_entry(config))))
            stack.enter_context(patch("contextbase.connectors.upstream.yaml_source.load_yaml_config", return_value=copy.deepcopy(config)))
            return fetch_scope.compile_scope("synthetic", selected, params or {}, scope)

    def guard(self, plan=None):
        self.serial += 1
        path = self.directory / ("scope-" + str(self.serial) + ".json")
        return fetch_scope.FetchScope(path, plan or self.compile()), path

    def request(self, guard, resource="events", *, path="events", params=None, method="GET", body=None):
        values = {"status": "open"} if params is None and resource == "events" else params
        request = requests.Request(method, ORIGIN + "/v1/" + path, params=values, json=body)
        guard.begin_chain(resource, request)
        return request

    def prepare(self, guard, request):
        prepared = request.prepare()
        guard.prepare_request(request, prepared)
        return prepared

    def dispatch(self, guard, request):
        prepared = self.prepare(guard, request)
        guard.before_dispatch(prepared)
        return prepared

    def page(self, guard, request, rows, *, resource="events", has_next=False, body=None, status=200):
        prepared = self.dispatch(guard, request)
        response = requests.Response()
        response.status_code = status
        response.request = prepared
        response.url = prepared.url
        response._content = json.dumps({"data": rows} if body is None else body).encode()
        guard.observe_page(resource, response, rows, has_next)
        return response

    def test_real_intercom_declaration_compiles_without_credentials_or_source_execution(self):
        plan = fetch_scope.compile_scope("intercom", ["contacts"], {}, {
            "origin": "https://api.intercom.io", "fetch_resources": ["contacts"], "max_pages": 10, "max_rows": 100,
        })
        self.assertEqual(plan["source_type"], "intercom")
        self.assertEqual(plan["selected_outputs"], ["contacts"])
        self.assertEqual(plan["fetch_resources"], ["contacts"])
        self.assertEqual(len(fetch_scope.plan_hash(plan)), 64)
        self.assertNotIn("INTERCOM_TOKEN", json.dumps(plan))

    def test_scope_requires_exact_shape_positive_integer_limits_and_literal_origin(self):
        valid = {"origin": ORIGIN, "fetch_resources": ["events"], "max_pages": 2, "max_rows": 5}
        invalid = [{**valid, "extra": True}, {**valid, "fetch_resources": []},
                   {**valid, "fetch_resources": ["events", "events"]}, {**valid, "fetch_resources": ["events", "other"]}]
        for field in ("max_pages", "max_rows"):
            invalid.extend({**valid, field: value} for value in (True, 0, -1, 1.5, "2", None))
        invalid.extend({**valid, "origin": value} for value in (
            "http://example.invalid", ORIGIN + "/v1", ORIGIN + "?x=1", "https://user@example.invalid",
            "http://localhost:18493", "https://example.invalid#fragment",
        ))
        for scope in invalid:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.compile(scope=scope)

    def test_plan_hash_seals_static_parameters_and_declaration_without_mutating_inputs(self):
        config = configuration()
        original = copy.deepcopy(config)
        first = self.compile(config)
        same = self.compile(copy.deepcopy(config))
        changed = self.compile(config, params={"events": {"status": "closed"}})
        self.assertEqual(config, original)
        self.assertEqual(fetch_scope.plan_hash(first), fetch_scope.plan_hash(same))
        self.assertNotEqual(fetch_scope.plan_hash(first), fetch_scope.plan_hash(changed))
        config["resources"][0]["endpoint"]["path"] = "archived_events"
        self.assertNotEqual(fetch_scope.plan_hash(first), fetch_scope.plan_hash(self.compile(config)))

    def test_build_rechecks_exact_frozen_declaration_selection_and_parameters(self):
        original = configuration()
        guard, _ = self.guard(self.compile(original))
        guard.verify_declaration("synthetic", copy.deepcopy(original), ["events"], {})
        changes = [
            ("other", original, ["events"], {}),
            ("synthetic", original, [], {}),
            ("synthetic", original, ["events"], {"events": {"status": "closed"}}),
        ]
        changed = copy.deepcopy(original)
        changed["resources"][0]["endpoint"]["data_selector"] = "other_data"
        changes.append(("synthetic", changed, ["events"], {}))
        changed = copy.deepcopy(original)
        changed["client"]["paginator"] = "auto"
        changes.append(("synthetic", changed, ["events"], {}))
        for source, declaration, resources, params in changes:
            with self.subTest(source=source, declaration=declaration, resources=resources, params=params):
                guard, _ = self.guard(self.compile(original))
                with self.assertRaises(ValueError):
                    guard.verify_declaration(source, declaration, resources, params)
                self.assertNotEqual(guard.finish()["status"], "completed")

    def test_actual_builder_refuses_declaration_drift_before_translation_or_factory(self):
        from contextbase.connectors.upstream import yaml_source
        original = configuration()
        guard, _ = self.guard(self.compile(original))
        changed = copy.deepcopy(original)
        changed["resources"][0]["endpoint"]["data_selector"] = "unreviewed_data"
        with patch.object(yaml_source, "load_yaml_config", return_value=changed), \
                patch.object(yaml_source, "_translate_config", side_effect=AssertionError("source translation must not begin")) as translate:
            with self.assertRaises(ValueError):
                yaml_source.build_dlt_source("synthetic", {}, ["events"], {}, http_session=object(), fetch_scope=guard)
            translate.assert_not_called()
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_unsupported_families_and_declaration_features_refuse_before_runtime(self):
        for family in ("dlt_graphql", "dlt_verified", "dlt_sql", "dlt_filesystem", "file"):
            entry = catalog_entry(configuration())
            entry["execution_family"] = family
            with self.subTest(family=family), self.assertRaises(ValueError):
                self.compile(entry=entry)
        for paginator in ("auto", {"type": "json_link", "next_url_path": "next"},
                          {"type": "header_link"}, {"type": "session_page"}):
            with self.subTest(paginator=paginator), self.assertRaises(ValueError):
                self.compile(configuration(paginator=paginator))
        for update in ({"response_format": "jsonl"}, {"method": "DELETE"}, {"params": {"token": "{token}"}}):
            config = configuration()
            config["resources"][0]["endpoint"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.compile(config)
        config = configuration()
        config["resources"][0]["incremental"] = {"cursor_path": "updated", "param": "since"}
        with self.assertRaises(ValueError):
            self.compile(config)

    def test_single_page_success_is_private_durable_and_compact_summary_detached(self):
        guard, path = self.guard()
        self.page(guard, self.request(guard), [{"id": "event_1"}])
        full = guard.finish()
        self.assertEqual(full["status"], "completed")
        self.assertEqual(full["counts"], {"requests": 1, "pages": 1, "rows": 1, "pending": 0})
        self.assertEqual(full["coverage"]["events"]["status"], "complete")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(fetch_scope.read_scope(path), full)
        compact = fetch_scope.compact_summary(full)
        self.assertEqual(compact["counts"], full["counts"])
        self.assertEqual(compact["plan_sha256"], fetch_scope.plan_hash(self.compile()))
        compact["counts"]["rows"] = 900
        self.assertEqual(fetch_scope.read_scope(path)["counts"]["rows"], 1)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            fetch_scope.FetchScope(path, self.compile())
        self.assertEqual(path.read_bytes(), before)

    def test_scope_file_boundary_refuses_symlink_ancestor_and_final_component(self):
        guard, path = self.guard()
        self.page(guard, self.request(guard), [])
        guard.finish()
        original = path.read_bytes()
        alias = self.directory / "linked-parent"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            fetch_scope.FetchScope(alias / "new.json", self.compile())
        self.assertFalse((self.directory / "new.json").exists())
        with self.assertRaises(ValueError):
            fetch_scope.read_scope(alias / path.name)
        final = self.directory / "linked-ledger.json"
        final.symlink_to(path)
        with self.assertRaises((ValueError, OSError)):
            fetch_scope.read_scope(final)
        with self.assertRaises(FileExistsError):
            fetch_scope.FetchScope(final, self.compile())
        self.assertEqual(path.read_bytes(), original)

    def test_scope_file_boundary_refuses_hardlinks_and_shared_receipt_modes(self):
        guard, path = self.guard()
        self.page(guard, self.request(guard), [])
        finished = guard.finish()
        hardlink = self.directory / "hardlinked-ledger.json"
        os.link(path, hardlink)
        for candidate in (path, hardlink):
            with self.subTest(path=candidate.name), self.assertRaises(ValueError):
                fetch_scope.read_scope(candidate)
        hardlink.unlink()
        for mode in (0o644, 0o640, 0o400):
            path.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(ValueError):
                fetch_scope.read_scope(path)
        path.chmod(0o600)
        self.assertEqual(fetch_scope.read_scope(path), finished)
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):
            fetch_scope.read_scope(path)
        self.directory.chmod(0o700)
        self.assertEqual(fetch_scope.read_scope(path), finished)

    def test_unattributed_prepared_request_and_response_copy_are_refused(self):
        guard, _ = self.guard()
        raw = requests.Request("GET", ORIGIN + "/v1/events", params={"status": "open"}).prepare()
        with self.assertRaises(ValueError):
            guard.before_dispatch(raw)
        guard, _ = self.guard()
        request = self.request(guard)
        prepared = self.dispatch(guard, request)
        response = requests.Response()
        response.status_code = 200
        response.request = copy.copy(prepared)
        response._content = b'{"data":[]}'
        with self.assertRaises(ValueError):
            guard.observe_page("events", response, [], False)
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_static_method_origin_path_query_and_duplicate_keys_cannot_widen_after_prepare(self):
        mutations = {
            "method": lambda p: setattr(p, "method", "POST"),
            "origin": lambda p: setattr(p, "url", "https://other.example.invalid/v1/events?status=open"),
            "same_origin_path": lambda p: setattr(p, "url", ORIGIN + "/v1/admin?status=open"),
            "query_change": lambda p: setattr(p, "url", ORIGIN + "/v1/events?status=closed"),
            "query_extra": lambda p: setattr(p, "url", ORIGIN + "/v1/events?status=open&include_all=true"),
            "query_duplicate": lambda p: setattr(p, "url", ORIGIN + "/v1/events?status=open&status=closed"),
            "body": lambda p: setattr(p, "body", b'{"include_all":true}'),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                guard, _ = self.guard()
                prepared = self.prepare(guard, self.request(guard))
                mutate(prepared)
                with self.assertRaises(ValueError):
                    guard.before_dispatch(prepared)
                self.assertNotEqual(guard.finish()["status"], "completed")

    def test_duplicate_query_value_order_cannot_change_prepared_request_meaning(self):
        config = configuration()
        config["resources"][0]["endpoint"]["params"] = {"status": ["open", "closed"]}
        guard, _ = self.guard(self.compile(config))
        request = self.request(guard, params={"status": ["open", "closed"]})
        prepared = self.prepare(guard, request)
        prepared.url = ORIGIN + "/v1/events?status=closed&status=open"
        with self.assertRaises(ValueError):
            guard.before_dispatch(prepared)
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_cursor_only_accepts_exact_previous_response_value_and_preserves_static_filters(self):
        config = configuration(paginator={"type": "cursor", "cursor_param": "cursor", "cursor_path": "next"})
        guard, _ = self.guard(self.compile(config))
        request = self.request(guard)
        self.page(guard, request, [{"id": "event_1"}], has_next=True, body={"data": [{"id": "event_1"}], "next": "cursor_2"})
        request.params = {"status": "open", "cursor": "cursor_2"}
        self.page(guard, request, [{"id": "event_2"}], body={"data": [{"id": "event_2"}], "next": None})
        self.assertEqual(guard.finish()["counts"], {"requests": 2, "pages": 2, "rows": 2, "pending": 0})
        for params in ({"status": "open", "cursor": "invented"}, {"status": "closed", "cursor": "cursor_2"}):
            with self.subTest(params=params):
                guard, _ = self.guard(self.compile(config))
                request = self.request(guard)
                self.page(guard, request, [{"id": "event_1"}], has_next=True, body={"data": [{"id": "event_1"}], "next": "cursor_2"})
                request.params = params
                with self.assertRaises(ValueError):
                    self.dispatch(guard, request)

    def test_declared_cursor_response_cannot_be_false_terminal_page(self):
        config = configuration(paginator={"type": "cursor", "cursor_param": "cursor", "cursor_path": "next"})
        guard, path = self.guard(self.compile(config))
        with self.assertRaises(ValueError):
            self.page(guard, self.request(guard), [{"id": "event_1"}], has_next=False,
                      body={"data": [{"id": "event_1"}], "next": "cursor_2"})
        self.assertNotEqual(guard.finish()["status"], "completed")
        self.assertNotEqual(fetch_scope.read_scope(path)["status"], "completed")

    def test_offset_and_page_number_only_allow_declared_monotonic_steps(self):
        cases = [({"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 2},
                  {"offset": 0, "limit": 2}, {"offset": 2, "limit": 2}, {"offset": 20, "limit": 2}),
                 ({"type": "page_number", "page_param": "page", "base_page": 1},
                  {"page": 1}, {"page": 2}, {"page": 10})]
        for paginator, first, second, wrong in cases:
            with self.subTest(paginator=paginator):
                plan = self.compile(configuration(paginator=paginator))
                guard, _ = self.guard(plan)
                request = self.request(guard, params={"status": "open", **first})
                self.page(guard, request, [{"id": "event_1"}, {"id": "event_2"}], has_next=True)
                request.params = {"status": "open", **second}
                self.page(guard, request, [])
                self.assertEqual(guard.finish()["status"], "completed")
                guard, _ = self.guard(plan)
                request = self.request(guard, params={"status": "open", **first})
                self.page(guard, request, [{"id": "event_1"}], has_next=True)
                request.params = {"status": "open", **wrong}
                with self.assertRaises(ValueError):
                    self.dispatch(guard, request)

    def test_json_post_preserves_typed_filter_and_only_mutates_declared_cursor(self):
        config = configuration([{"name": "search", "endpoint": {"path": "search", "method": "POST",
            "json": {"filter": {"status": "open", "include_archived": False}},
            "paginator": {"type": "cursor", "cursor_param": "cursor", "cursor_path": "next"}}}])
        plan = self.compile(config, selected=["search"])
        guard, _ = self.guard(plan)
        body = {"filter": {"status": "open", "include_archived": False}}
        request = self.request(guard, "search", path="search", method="POST", body=body)
        self.page(guard, request, [{"id": "event_1"}], resource="search", has_next=True,
                  body={"data": [{"id": "event_1"}], "next": "next_2"})
        request.json = {**body, "cursor": "next_2"}
        self.page(guard, request, [], resource="search", body={"data": [], "next": None})
        self.assertEqual(guard.finish()["status"], "completed")
        guard, _ = self.guard(plan)
        with self.assertRaises(ValueError):
            request = self.request(guard, "search", path="search", method="POST", body={"filter": {"status": "open", "include_archived": 0}})
            self.dispatch(guard, request)

    def test_exact_fetch_closure_includes_unselected_parent(self):
        config = parent_configuration()
        plan = self.compile(config, selected=["records"])
        self.assertEqual(plan["selected_outputs"], ["records"])
        self.assertEqual(plan["fetch_resources"], ["accounts", "records"])
        for resources in (["records"], ["accounts", "records", "unrelated"]):
            with self.subTest(resources=resources), self.assertRaises(ValueError):
                self.compile(config, selected=["records"], scope={"origin": ORIGIN, "fetch_resources": resources, "max_pages": 20, "max_rows": 100})

    def test_parent_values_authorize_exact_child_chains_and_counts_do_not_reset(self):
        guard, _ = self.guard(self.compile(parent_configuration(), selected=["records"]))
        self.page(guard, self.request(guard, "accounts", path="accounts"), [{"id": "account_a"}, {"id": "account_b"}], resource="accounts")
        for account in ("account_a", "account_b"):
            self.page(guard, self.request(guard, "records", path="accounts/" + account + "/records"), [{"id": account + "_record"}], resource="records")
        finished = guard.finish()
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["counts"], {"requests": 3, "pages": 3, "rows": 4, "pending": 0})
        self.assertEqual(set(finished["coverage"]), {"accounts", "records"})

    def test_unobserved_parent_and_path_injection_cannot_authorize_child_fetch(self):
        plan = self.compile(parent_configuration(), selected=["records"])
        guard, _ = self.guard(plan)
        with self.assertRaises(ValueError):
            self.dispatch(guard, self.request(guard, "records", path="accounts/unobserved/records"))
        for value in ("../admin", "account/other", "account%2Fother", "account?scope=all"):
            with self.subTest(value=value):
                guard, _ = self.guard(plan)
                # Refusal may occur while validating parent data or beginning
                # the descendant request, but must occur before any dispatch.
                with self.assertRaises(ValueError):
                    self.page(guard, self.request(guard, "accounts", path="accounts"), [{"id": value}], resource="accounts")
                    self.dispatch(guard, self.request(guard, "records", path="accounts/" + value + "/records"))

    def test_nonempty_parent_requires_every_child_but_empty_parent_justifies_no_children(self):
        plan = self.compile(parent_configuration(), selected=["records"])
        guard, _ = self.guard(plan)
        self.page(guard, self.request(guard, "accounts", path="accounts"), [{"id": "account_a"}, {"id": "account_b"}], resource="accounts")
        self.page(guard, self.request(guard, "records", path="accounts/account_a/records"), [], resource="records")
        self.assertNotEqual(guard.finish()["status"], "completed")
        guard, _ = self.guard(plan)
        self.page(guard, self.request(guard, "accounts", path="accounts"), [], resource="accounts")
        finished = guard.finish()
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["counts"], {"requests": 1, "pages": 1, "rows": 0, "pending": 0})
        self.assertEqual(finished["coverage"]["records"]["status"], "complete")

    def test_missing_resource_nonterminal_page_and_pending_dispatch_prevent_completion(self):
        config = configuration([{"name": "events", "endpoint": {"path": "events"}}, {"name": "other", "endpoint": {"path": "other"}}])
        guard, _ = self.guard(self.compile(config, selected=["events", "other"]))
        self.page(guard, self.request(guard, params={}), [])
        self.assertNotEqual(guard.finish()["status"], "completed")
        guard, path = self.guard()
        self.dispatch(guard, self.request(guard))
        self.assertEqual(fetch_scope.read_scope(path)["counts"]["pending"], 1)
        self.assertNotEqual(guard.finish()["status"], "completed")
        config = configuration(paginator={"type": "cursor", "cursor_param": "cursor", "cursor_path": "next"})
        guard, _ = self.guard(self.compile(config))
        self.page(guard, self.request(guard), [{"id": "event_1"}], has_next=True, body={"data": [{"id": "event_1"}], "next": "next_2"})
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_one_outstanding_dispatch_per_chain_and_duplicate_response_are_refused(self):
        guard, _ = self.guard()
        request = self.request(guard)
        self.dispatch(guard, request)
        with self.assertRaises(ValueError):
            self.dispatch(guard, request)
        guard, _ = self.guard()
        response = self.page(guard, self.request(guard), [])
        with self.assertRaises(ValueError):
            guard.observe_page("events", response, [], False)
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_response_cannot_be_relabelled_as_another_declared_resource(self):
        config = configuration([
            {"name": "events", "endpoint": {"path": "events"}},
            {"name": "other", "endpoint": {"path": "other"}},
        ])
        guard, _ = self.guard(self.compile(config, selected=["events", "other"]))
        prepared = self.dispatch(guard, self.request(guard, params={}))
        response = requests.Response()
        response.status_code = 200
        response.request = prepared
        response._content = b'{"data":[]}'
        with self.assertRaises(ValueError):
            guard.observe_page("other", response, [], False)
        self.assertNotEqual(guard.finish()["status"], "completed")

    def test_page_and_row_limits_are_global_across_resources_and_parent_chains(self):
        config = parent_configuration()
        for max_pages, max_rows in ((2, 100), (20, 2)):
            with self.subTest(max_pages=max_pages, max_rows=max_rows):
                plan = self.compile(config, selected=["records"], scope={"origin": ORIGIN,
                    "fetch_resources": ["accounts", "records"], "max_pages": max_pages, "max_rows": max_rows})
                guard, path = self.guard(plan)
                with self.assertRaises(ValueError):
                    self.page(guard, self.request(guard, "accounts", path="accounts"), [{"id": "account_a"}, {"id": "account_b"}], resource="accounts")
                    for account in ("account_a", "account_b"):
                        self.page(guard, self.request(guard, "records", path="accounts/" + account + "/records"), [{"id": account}], resource="records")
                self.assertNotEqual(guard.finish()["status"], "completed")
                self.assertNotEqual(fetch_scope.read_scope(path)["status"], "completed")

    def test_failed_dispatch_preserves_pending_evidence_and_cannot_finish_successfully(self):
        guard, path = self.guard()
        self.dispatch(guard, self.request(guard))
        failed = guard.fail("worker_failed")
        self.assertNotEqual(failed["status"], "completed")
        self.assertEqual(failed["counts"]["requests"], 1)
        self.assertEqual(failed["counts"]["pending"], 1)
        self.assertNotEqual(guard.finish()["status"], "completed")
        self.assertEqual(fetch_scope.read_scope(path)["counts"]["pending"], 1)

    def test_ledger_cannot_erase_required_root_chain_to_invent_empty_success(self):
        guard, path = self.guard()
        self.assertNotEqual(guard.finish()["status"], "completed")
        stored = json.loads(path.read_text())
        # Recompute all count/coverage details coherently: this must still fail
        # because the compiled root path is independently required by the plan.
        stored["expected_paths"]["events"] = []
        stored["coverage"]["events"]["expected_chains"] = 0
        stored["coverage"]["events"]["status"] = "complete"
        stored["status"] = "completed"
        stored["stop_reason"] = None
        path.write_text(json.dumps(stored))
        with self.assertRaises(ValueError):
            fetch_scope.read_scope(path)

    def test_ledger_summary_counters_require_exact_integers(self):
        guard, path = self.guard()
        self.page(guard, self.request(guard), [{"id": "event_1"}])
        valid = guard.finish()
        locations = [("counts", name) for name in ("requests", "pages", "rows", "pending")]
        locations += [("coverage", "events", name) for name in
                      ("expected_chains", "started_chains", "completed_chains", "pages", "rows")]
        for location in locations:
            for kind in (bool, float):
                stored = copy.deepcopy(valid)
                target = stored
                for key in location[:-1]:
                    target = target[key]
                target[location[-1]] = kind(target[location[-1]])
                path.write_text(json.dumps(stored))
                with self.subTest(location=location, kind=kind.__name__), self.assertRaises(ValueError):
                    fetch_scope.read_scope(path)

    def test_completed_ledger_requires_null_stop_reason(self):
        guard, path = self.guard()
        self.page(guard, self.request(guard), [])
        valid = guard.finish()
        for reason in (False, 0, [], {}, ""):
            stored = copy.deepcopy(valid)
            stored["stop_reason"] = reason
            path.write_text(json.dumps(stored))
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                fetch_scope.read_scope(path)

    def test_nonempty_parent_cannot_be_relabelled_as_zero_expected_child_chains(self):
        guard, path = self.guard(self.compile(parent_configuration(), selected=["records"]))
        self.page(guard, self.request(guard, "accounts", path="accounts"), [{"id": "account_a"}], resource="accounts")
        self.assertNotEqual(guard.finish()["status"], "completed")
        stored = json.loads(path.read_text())
        stored["expected_paths"]["records"] = []
        stored["coverage"]["records"]["expected_chains"] = 0
        stored["coverage"]["records"]["status"] = "complete"
        stored["status"] = "completed"
        stored["stop_reason"] = None
        path.write_text(json.dumps(stored))
        with self.assertRaises(ValueError):
            fetch_scope.read_scope(path)

    def test_ledger_tampering_and_unattributed_response_do_not_create_success(self):
        guard, path = self.guard()
        response = requests.Response()
        response.status_code = 200
        response.request = requests.Request("GET", ORIGIN + "/v1/events").prepare()
        response._content = b'{"data":[]}'
        with self.assertRaises(ValueError):
            guard.observe_page("events", response, [], False)
        self.assertNotEqual(guard.finish()["status"], "completed")
        stored = json.loads(path.read_text())
        stored["counts"]["rows"] = 900
        path.write_text(json.dumps(stored))
        with self.assertRaises(ValueError):
            fetch_scope.read_scope(path)


if __name__ == "__main__":
    unittest.main()
