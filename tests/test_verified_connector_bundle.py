"""Static checks of vendored source bytes; never import or run a connector."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "contextbase" / "connectors" / "verified"
PIN = "75b3ec17eab99d0079d9f61b7f47fc8b899a5738"
EXPECTED = {
    "airtable": "airtable.airtable_source",
    "asana": "asana_dlt.asana_source",
    "bing_webmaster": "bing_webmaster.page_stats",
    "facebook_ads": "facebook_ads.facebook_ads_source",
    "freshdesk": "freshdesk.freshdesk_source",
    "google_ads": "google_ads.google_ads",
    "google_analytics": "google_analytics.google_analytics",
    "google_sheets": "google_sheets.google_spreadsheet",
    "hubspot": "hubspot.hubspot",
    "jira": "jira.jira",
    "kafka": "kafka.kafka_consumer",
    "kinesis": "kinesis.kinesis_stream",
    "matomo": "matomo.matomo_visits",
    "mongodb": "mongodb.mongodb",
    "mux": "mux.assets_resource",
    "notion": "notion.notion_databases",
    "personio": "personio.personio_source",
    "pipedrive": "pipedrive.pipedrive_source",
    "salesforce": "salesforce.salesforce_source",
    "shopify": "shopify_dlt.shopify_source",
    "slack": "slack.slack_source",
    "strapi": "strapi.strapi_source",
    "stripe": "stripe_analytics.stripe_source",
    "workable": "workable.workable_source",
    "zendesk": "zendesk.zendesk_support",
}


class VerifiedConnectorBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((BUNDLE / "manifest.json").read_text())

    def test_exact_pinned_catalog_and_honest_runtime_status(self):
        manifest = self.manifest
        self.assertEqual(manifest["spec"], "contextbase.verified-connectors/v1")
        self.assertEqual(manifest["repository"], "https://github.com/dlt-hub/verified-sources")
        self.assertEqual(manifest["ref"], PIN)
        self.assertEqual(manifest["source_count"], 25)
        self.assertEqual(set(manifest["sources"]), set(EXPECTED))
        self.assertEqual(manifest["shared_source_directories"], [])
        for name, expected in EXPECTED.items():
            with self.subTest(source=name):
                source = manifest["sources"][name]
                module, factory = expected.split(".")
                self.assertEqual(source["import_path"], "sources." + expected)
                self.assertEqual(source["source_directory"], "sources/" + module)
                self.assertEqual(source["factory_name"], factory)
                self.assertEqual(source["availability"], "bundled_static")
                self.assertEqual(source["runtime_validation"], "not_performed")
        for field in ("source_imports_executed", "dependency_installations", "credential_access", "live_api_calls"):
            self.assertIs(manifest["validation"][field], False)

    def test_complete_file_inventory_hashes_and_upstream_git_objects(self):
        entries = self.manifest["files"]
        indexed = {entry["path"]: entry for entry in entries}
        self.assertEqual(len(indexed), len(entries))
        actual = {
            str(path.relative_to(BUNDLE)) for path in BUNDLE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != "manifest.json"
        }
        self.assertEqual(actual, set(indexed))
        for relative, entry in indexed.items():
            with self.subTest(path=relative):
                path = BUNDLE / relative
                self.assertFalse(path.is_symlink())
                self.assertTrue(path.resolve().is_relative_to(BUNDLE.resolve()))
                raw = path.read_bytes()
                self.assertEqual(len(raw), entry["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                if entry["origin"] == "upstream":
                    self.assertEqual(entry["upstream_path"], relative)
                    blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
                    self.assertEqual(hashlib.sha1(blob).hexdigest(), entry["git_blob_sha1"])
                else:
                    self.assertEqual(entry["origin"], "contextbase")
                    self.assertIn(relative, {"NOTICE.md", "sources/__init__.py"})
                    self.assertIsNone(entry["upstream_path"])
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), self.manifest["bundle_sha256"])

    def test_license_and_local_additions_are_explicit(self):
        license = self.manifest["license"]
        self.assertEqual(license["spdx"], "Apache-2.0")
        text = (BUNDLE / license["path"]).read_text()
        self.assertIn("Apache License", text)
        self.assertIn("Copyright 2023 The Contributors", text)
        self.assertEqual(license["upstream_metadata_spdx"], "MIT")
        self.assertIs(license["metadata_inconsistency_recorded"], True)
        notice = (BUNDLE / "NOTICE.md").read_text()
        self.assertIn(PIN, notice)
        self.assertIn("metadata inconsistency", notice)
        self.assertIn("not installed dependencies", notice)
        tree = ast.parse((BUNDLE / "sources/__init__.py").read_text())
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)
        self.assertIsInstance(tree.body[0].value, ast.Constant)

    def test_only_required_directories_and_no_configuration_or_credentials(self):
        names = {value.split(".")[0] for value in EXPECTED.values()}
        directories = {path.name for path in (BUNDLE / "sources").iterdir()
                       if path.is_dir() and path.name != "__pycache__"}
        self.assertEqual(directories, names)
        for entry in self.manifest["files"]:
            path = Path(entry["path"])
            self.assertNotIn(".dlt", path.parts)
            self.assertNotIn(".git", path.parts)
            self.assertNotIn("secrets", path.name.lower())
            self.assertFalse(path.name.startswith(".env"))

    def test_every_factory_is_present_and_signature_matches_without_import(self):
        for name, source in self.manifest["sources"].items():
            with self.subTest(source=name):
                path = BUNDLE / source["source_directory"] / "__init__.py"
                functions = {node.name: node for node in ast.parse(path.read_text()).body
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertIn(source["factory_name"], functions)
                factory = functions[source["factory_name"]]
                positional = factory.args.posonlyargs + factory.args.args
                required = len(positional) - len(factory.args.defaults)
                signature = [{"name": arg.arg, "required": index < required}
                             for index, arg in enumerate(positional)]
                signature.extend({"name": arg.arg, "required": default is None}
                                 for arg, default in zip(factory.args.kwonlyargs, factory.args.kw_defaults))
                self.assertEqual(signature, source["factory_arguments"])
                self.assertEqual(factory.args.kwarg is not None, source["accepts_extra_kwargs"])

    def test_all_static_internal_imports_have_bundled_modules(self):
        for source in self.manifest["sources"].values():
            directory = BUNDLE / source["source_directory"]
            for path in directory.rglob("*.py"):
                package = list(path.relative_to(BUNDLE).with_suffix("").parts)[:-1]
                for node in ast.walk(ast.parse(path.read_text())):
                    targets = []
                    if isinstance(node, ast.ImportFrom):
                        if node.level:
                            self.assertLessEqual(node.level, len(package))
                            parts = package[:len(package) - node.level + 1]
                            targets = [".".join(parts + (node.module.split(".") if node.module else []))]
                        elif (node.module or "").startswith("sources."):
                            targets = [node.module]
                    elif isinstance(node, ast.Import):
                        targets = [item.name for item in node.names if item.name.startswith("sources.")]
                    for target in targets:
                        resolved = BUNDLE.joinpath(*target.split("."))
                        self.assertTrue(resolved.is_dir() or resolved.with_suffix(".py").is_file(),
                                        f"{path}:{node.lineno}: {target}")

    def test_requirements_and_external_import_hints_cover_actual_source(self):
        for name, source in self.manifest["sources"].items():
            with self.subTest(source=name):
                requirements = [line.strip() for relative in source["requirements_files"]
                                for line in (BUNDLE / relative).read_text().splitlines()
                                if line.strip() and not line.lstrip().startswith("#")]
                self.assertEqual(requirements, source["requirements"])
                self.assertEqual(requirements, source["install_hints"]["declared_requirements"])
                imports = []
                for path in sorted((BUNDLE / source["source_directory"]).rglob("*.py")):
                    for node in ast.walk(ast.parse(path.read_text())):
                        modules = []
                        if isinstance(node, ast.Import):
                            modules = [item.name for item in node.names]
                        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                            modules = [node.module]
                        for module in modules:
                            if module.split(".")[0] not in sys.stdlib_module_names | {"sources"}:
                                imports.append({"module": module, "path": str(path.relative_to(BUNDLE)), "line": node.lineno})
                imports.sort(key=lambda row: (row["path"], row["line"], row["module"]))
                self.assertEqual(imports, source["external_import_evidence"])
                self.assertEqual(sorted({row["module"].split(".")[0] for row in imports}), source["external_imports"])
        sources = self.manifest["sources"]
        self.assertIn("humanize", sources["facebook_ads"]["install_hints"]["additional_runtime_distributions"])
        self.assertIn("jsonpath-ng", sources["slack"]["install_hints"]["additional_runtime_distributions"])
        self.assertEqual(sources["mongodb"]["install_hints"]["optional_distributions"], ["pymongoarrow"])
        bson = next(item for item in sources["mongodb"]["import_distribution_hints"] if item["import"] == "bson")
        self.assertEqual(bson["distributions"], ["pymongo"])


if __name__ == "__main__":
    unittest.main()
