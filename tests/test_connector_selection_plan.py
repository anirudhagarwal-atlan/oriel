"""Static fetch declarations only; no credentials, runtime or extraction."""
import builtins
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from contextbase.connectors.policy import selection_plan, validate_selection


CATALOG = Path(__file__).resolve().parents[1] / "contextbase/connectors/catalog.json"
FIELDS = {"source_type", "execution_family", "requested_resources", "selected_outputs",
          "required_fetch_resources", "dependency_edges", "default_selection_used", "status", "limitations"}


def resource(name, parent=None, *, selected=True):
    declared = {} if parent is None else {"parent": parent}
    return {"name": name, "selected": selected, "declared": declared}


def entry(resources, *, family="dlt_rest"):
    return {"name": "synthetic_source", "execution_family": family, "resources": resources}


class SelectionPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = {item["name"]: item for item in json.loads(CATALOG.read_text())["connectors"]}

    def test_actual_discord_messages_include_two_unselected_fetch_ancestors(self):
        result = selection_plan(self.catalog["discord"], ["messages"])
        self.assertEqual(set(result), FIELDS)
        self.assertEqual(result["selected_outputs"], ["messages"])
        self.assertEqual(result["required_fetch_resources"], ["channels", "guilds", "messages"])
        self.assertEqual(result["dependency_edges"], [
            {"resource": "channels", "requires": "guilds"},
            {"resource": "messages", "requires": "channels"}])
        self.assertEqual(result["status"], "declaration_derived")
        self.assertFalse(result["default_selection_used"])

    def test_actual_github_dictionary_parent_and_selected_false_default(self):
        explicit = selection_plan(self.catalog["github"], ["pull_requests"])
        self.assertEqual(explicit["selected_outputs"], ["pull_requests"])
        self.assertEqual(explicit["required_fetch_resources"], ["pull_requests", "repos"])
        self.assertEqual(explicit["dependency_edges"], [{"resource": "pull_requests", "requires": "repos"}])
        default = selection_plan(self.catalog["github"], [])
        self.assertTrue(default["default_selection_used"])
        self.assertEqual(default["requested_resources"], [])
        self.assertEqual(default["selected_outputs"], ["issues", "repos"])
        self.assertNotIn("pull_requests", default["required_fetch_resources"])

    def test_default_child_keeps_unselected_parent_and_excludes_unused_resources(self):
        declaration = entry([resource("parent", selected=False), resource("child", "parent"),
                             resource("unused", selected=False)])
        result = selection_plan(declaration, [])
        self.assertEqual(result["selected_outputs"], ["child"])
        self.assertEqual(result["required_fetch_resources"], ["child", "parent"])

    def test_shared_parent_closure_is_unique_and_deterministic(self):
        declaration = entry([resource("root", selected=False), resource("middle", "root", selected=False),
                             resource("first", "middle"), resource("second", {"resource": "middle", "field": "id"})])
        requested = ["second", "first"]
        result = selection_plan(declaration, requested)
        reverse = selection_plan(entry(list(reversed(declaration["resources"]))), requested)
        self.assertEqual(result, reverse)
        self.assertEqual(result["requested_resources"], requested)
        self.assertEqual(result["selected_outputs"], ["first", "second"])
        self.assertEqual(result["required_fetch_resources"], ["first", "middle", "root", "second"])
        self.assertEqual(len(result["dependency_edges"]), 3)

    def test_unknown_resource_parent_and_reachable_cycles_refuse(self):
        graphs = [(entry([resource("root")]), ["unknown"]),
                  (entry([resource("child", "unknown")]), ["child"]),
                  (entry([resource("a", "a")]), ["a"]),
                  (entry([resource("a", "b"), resource("b", "a")]), ["a"])]
        for declaration, selected in graphs:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                selection_plan(declaration, selected)
            with self.assertRaises(ValueError):
                validate_selection(declaration, selected, {})

    def test_invalid_parent_shapes_refuse_in_planning_and_selection_validation(self):
        for parent in (False, 0, [], {}, {"field": "id"}, {"resource": None}, {"resource": []}, "", "../outside"):
            declaration = entry([resource("child", parent)])
            for operation in (lambda: selection_plan(declaration, ["child"]),
                              lambda: validate_selection(declaration, ["child"], {})):
                with self.subTest(parent=parent), self.assertRaises(ValueError):
                    operation()

    def test_shared_closure_validates_required_parent_parameters(self):
        parent = resource("parent", selected=False)
        parent["declared"]["endpoint"] = {"params": {"since": None}}
        declaration = entry([parent, resource("child", "parent"), resource("unused", selected=False)])
        with self.assertRaises(ValueError):
            validate_selection(declaration, ["child"], {})
        validate_selection(declaration, ["child"], {"parent": {"since": "2026-01-01"}})
        with self.assertRaisesRegex(ValueError, "dependencies"):
            validate_selection(declaration, ["child"], {"unused": {}})

    def test_empty_rest_selection_cannot_claim_complete_fetch_scope(self):
        for declaration in (entry([]), entry([resource("optional", selected=False)])):
            with self.assertRaises(ValueError):
                selection_plan(declaration, [])

    def test_graphql_has_known_outputs_but_unknown_fetch_scope(self):
        result = selection_plan(self.catalog["linear"], ["issues"])
        self.assertEqual(result["selected_outputs"], ["issues"])
        self.assertIsNone(result["required_fetch_resources"])
        self.assertEqual(result["dependency_edges"], [])
        self.assertEqual(result["status"], "fetch_scope_unknown")
        with self.assertRaises(ValueError):
            selection_plan(self.catalog["linear"], ["unknown_resource"])
        default = selection_plan(self.catalog["linear"], [])
        self.assertEqual(default["selected_outputs"], sorted(r["name"] for r in self.catalog["linear"]["resources"] if r["selected"]))
        self.assertEqual(default["status"], "fetch_scope_unknown")

    def test_dynamic_and_file_families_do_not_invent_defaults_or_fetch_dependencies(self):
        for source in ("stripe", "salesforce", "sqlite", "csv"):
            with self.subTest(source=source):
                default = selection_plan(self.catalog[source], [])
                self.assertIsNone(default["selected_outputs"])
                self.assertIsNone(default["required_fetch_resources"])
                explicit = selection_plan(self.catalog[source], ["records"])
                self.assertEqual(explicit["selected_outputs"], ["records"])
                self.assertEqual(explicit["status"], "fetch_scope_unknown")

    def test_bounded_malformed_and_duplicate_metadata_and_requests_refuse(self):
        valid = entry([resource("root")])
        for requested in (None, "root", ["root", "root"], [1], ["../root"], ["x" * 129], ["r" + str(n) for n in range(257)]):
            with self.subTest(requested_type=type(requested).__name__), self.assertRaises(ValueError):
                selection_plan(valid, requested)
        bad = [None, {**valid, "execution_family": []}, {**valid, "execution_family": "unknown"},
               {**valid, "name": "../outside"}, {**valid, "resources": {}},
               entry([resource("root"), resource("root")]), entry([resource("../outside")]),
               entry([{"name": "root", "selected": "false", "declared": {}}]),
               entry([{"name": "root", "declared": []}]),
               entry([resource("r" + str(n)) for n in range(257)])]
        for declaration in bad:
            with self.subTest(declaration_type=type(declaration).__name__), self.assertRaises(ValueError):
                selection_plan(declaration, [])

    def test_result_is_detached_and_excludes_routing_parameters_and_payloads(self):
        declaration = entry([resource("child", {"resource": "parent", "field": "private_field"}), resource("parent")])
        declaration.update(credential_specs=[{"name": "api_key", "env_var": "SYNTHETIC_TOKEN"}], params={"since": "private-value"})
        declaration["resources"][0]["declared"].update(endpoint={"base_url": "https://example.invalid/private"}, payload="private-row")
        before = copy.deepcopy(declaration)
        requested = ["child"]
        result = selection_plan(declaration, requested)
        encoded = json.dumps(result, allow_nan=False)
        for text in ("private", "SYNTHETIC_TOKEN", "api_key", "https://"):
            self.assertNotIn(text, encoded)
        self.assertIn("no access was authorized", encoded)
        self.assertEqual(declaration, before)
        result["requested_resources"].append("other")
        result["limitations"].clear()
        self.assertEqual(requested, ["child"])
        self.assertTrue(selection_plan(declaration, requested)["limitations"])

    def test_planner_imports_no_runtime_or_source_implementation(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith(("dlt", "duckdb", "requests", "yaml")) or "upstream" in name:
                raise AssertionError("Runtime import attempted")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded):
            for source in ("discord", "github", "linear", "stripe", "sqlite"):
                selection_plan(self.catalog[source], [])


if __name__ == "__main__":
    unittest.main()
