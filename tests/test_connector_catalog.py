"""Offline checks for the complete, attributed connector declaration catalog."""
import ast
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "contextbase/connectors"
UPSTREAM = ROOT / "upstream"
EXPECTED_NAMES = set("""
airtable asana attio azure bamboohr bigcommerce bigquery bing_webmaster bitbucket
brex chargebee clickhouse clickup close cloudflare cockroachdb contentful copper
csv customerio databricks datadog deel discord duckdb_source facebook_ads figma
freshdesk front gainsight gcs github gitlab google_ads google_analytics
google_sheets greenhouse gusto helpscout hubspot hubspot_marketing intercom jira
kafka kinesis lemon_squeezy lever linear mailchimp mariadb matomo mercury mixpanel
monday mongodb mssql mux mysql netlify newrelic notion opsgenie oracle paddle
pagerduty parquet personio pipedrive plausible postgres posthog presto quickbooks
recurly redshift s3 salesforce sanity segment sendgrid sentry sftp shopify slack snowflake
square sqlite statuspage strapi stripe todoist trello trino twilio vercel vitally
woocommerce workable wordpress xero zendesk
""".split())


class ConnectorCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / "catalog.json").read_text())
        self.entries = {entry["name"]: entry for entry in self.catalog["connectors"]}

    def test_complete_pinned_catalog_has_exact_101_names_and_families(self):
        self.assertEqual(self.catalog["spec"], "contextbase.connector-catalog/v1")
        self.assertEqual(self.catalog["connector_count"], 101)
        self.assertEqual(len(self.catalog["connectors"]), 101)
        self.assertEqual(set(self.entries), EXPECTED_NAMES)
        self.assertEqual(Counter(entry["execution_family"] for entry in self.entries.values()), {
            "dlt_rest": 53, "dlt_graphql": 2, "dlt_verified": 25,
            "dlt_sql": 15, "dlt_filesystem": 4, "file": 2,
        })
        self.assertEqual(self.catalog["upstream"]["revision"], "2dd06369fc333cd51663c27b7d47a21a7946757b")
        self.assertEqual(self.entries["salesforce"]["factory"], "sources.salesforce.salesforce_source")
        self.assertEqual(self.entries["linear"]["factory"],
                         "contextbase.connectors.upstream.sources.graphql.graphql_source")

    def test_all_definitions_and_full_license_retain_byte_provenance(self):
        manifest = json.loads((UPSTREAM / "SOURCE.json").read_text())
        self.assertEqual(hashlib.sha256((UPSTREAM / "LICENSE").read_bytes()).hexdigest(),
                         "245a0452c3e22cfe3596e76662b0047214319a65d638ff0f0807cdd7fb1c755d")
        self.assertEqual(manifest["license_sha256"],
                         "245a0452c3e22cfe3596e76662b0047214319a65d638ff0f0807cdd7fb1c755d")
        yaml_names = set()
        for item in manifest["files"]:
            path = UPSTREAM / item["vendored_path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["vendored_sha256"])
            if path.suffix == ".yaml":
                yaml_names.add(path.stem)
                self.assertFalse(item["modified"])
                self.assertEqual(item["vendored_sha256"], item["upstream_sha256"])
        self.assertEqual(len(yaml_names), 56)
        declared_yaml = {name for name, entry in self.entries.items()
                         if entry["execution_family"] in {"dlt_rest", "dlt_graphql"}}
        self.assertEqual(yaml_names, declared_yaml | {"amplitude"})
        for entry in self.entries.values():
            if "definition_sha256" in entry["provenance"]:
                path = ROOT / entry["provenance"]["definition_path"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                                 entry["provenance"]["definition_sha256"])
        for item in manifest["contextbase_files"]:
            self.assertEqual(hashlib.sha256((UPSTREAM / item["path"]).read_bytes()).hexdigest(), item["sha256"])

    def test_resources_and_configuration_requirements_remain_visible(self):
        resources = [resource for entry in self.entries.values() for resource in entry["resources"]]
        self.assertEqual(len(resources), 440)
        self.assertEqual(sum(resource["selected"] is False for resource in resources), 1)
        for resource in resources:
            self.assertEqual(resource["name"], resource["declared"]["name"])
            self.assertFalse(resource["live_validated"])
        mixpanel = {resource["name"]: resource for resource in self.entries["mixpanel"]["resources"]}
        export_codes = {item["code"] for item in mixpanel["export"]["limitations"]}
        self.assertTrue({"rest_translation_contract_tested", "null_parameters_require_configuration"} <= export_codes)
        self.assertFalse(any(item["blocking"] for item in mixpanel["export"]["limitations"]))
        engage_codes = {item["code"] for item in mixpanel["engage"]["limitations"]}
        self.assertIn("rest_translation_contract_tested", engage_codes)
        self.assertFalse(any(item["blocking"] for item in mixpanel["engage"]["limitations"]))
        linear = {resource["name"]: resource for resource in self.entries["linear"]["resources"]}
        for name in ("issues", "projects"):
            self.assertIn("nested_graphql_projection_not_exhaustive", {item["code"] for item in linear[name]["limitations"]})
        legacy = self.catalog["additional_assets"]
        self.assertEqual([item["name"] for item in legacy], ["amplitude"])
        self.assertEqual(legacy[0]["status"], "legacy_unimplemented_asset")
        self.assertNotIn("amplitude", self.entries)

    def test_catalog_is_stdlib_readable_without_runtime_or_discovery(self):
        forbidden = {"dlt", "yaml", "requests", "sqlalchemy", "sources"}
        original_import = __import__
        def import_guard(name, *args, **kwargs):
            if name.split(".")[0] in forbidden:
                raise AssertionError("Catalog read imported a source runtime: " + name)
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=import_guard), \
             patch("pathlib.Path.home", side_effect=AssertionError("No home discovery")), \
             patch("socket.socket", side_effect=AssertionError("No source/network calls")), \
             patch("subprocess.Popen", side_effect=AssertionError("No installers or source workers")):
            parsed = json.loads((ROOT / "catalog.json").read_text())
        self.assertEqual(parsed["discovery_calls"], 0)
        self.assertFalse(parsed["contains_credential_values"])
        self.assertTrue(all(entry["live_validated"] is False for entry in parsed["connectors"]))
        for entry in parsed["connectors"]:
            for credential in entry["credential_specs"]:
                self.assertEqual(set(credential), {"name", "cli_flag", "env_var", "prompt", "secret", "required"})
                self.assertIsInstance(credential["secret"], bool)
                self.assertIsInstance(credential["required"], bool)
            self.assertEqual(entry["dependencies"]["install_policy"], "explicit_preinstalled_environment_only")

    def test_verified_factory_repairs_have_explicit_controlled_contract_evidence(self):
        incompatible = {}
        resource_factories = set()
        for name, entry in self.entries.items():
            if entry["execution_family"] != "dlt_verified":
                continue
            module_name, function_name = entry["factory"].rsplit(".", 1)
            module_path = ROOT / "verified" / Path(*module_name.split("."))
            path = module_path / "__init__.py" if module_path.is_dir() else module_path.with_suffix(".py")
            function = next(node for node in ast.parse(path.read_text()).body
                            if isinstance(node, ast.FunctionDef) and node.name == function_name)
            accepted = {argument.arg for argument in function.args.args + function.args.kwonlyargs}
            credentials = {item["name"] for item in entry["credential_specs"]}
            unexpected = credentials - accepted if function.args.kwarg is None else set()
            codes = {item["code"] for item in entry["limitations"]}
            if unexpected:
                incompatible[name] = unexpected
                self.assertIn("verified_credential_adapter_contract_tested", codes)
                self.assertIn("contextbase/connectors/verified_adapter.py", entry["verification"]["implementation_paths"])
                self.assertIn("tests/test_verified_adapter.py", entry["verification"]["test_paths"])
            self.assertNotIn("verified_factory_credential_mapping_incompatible", codes)
            decorators = [item.func if isinstance(item, ast.Call) else item
                          for item in function.decorator_list]
            is_resource = any(isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name)
                              and item.value.id == "dlt" and item.attr == "resource" for item in decorators)
            if is_resource:
                resource_factories.add(name)
                self.assertIn("verified_resource_wrapper_contract_tested", codes)
                self.assertIn("contextbase/connectors/worker.py", entry["verification"]["implementation_paths"])
                self.assertIn("tests/test_connector_worker.py", entry["verification"]["test_paths"])
            else:
                self.assertNotIn("verified_resource_wrapper_contract_tested", codes)
        self.assertEqual(set(incompatible), {"salesforce", "kafka", "kinesis", "asana", "google_ads"})
        self.assertEqual(resource_factories, {"kafka", "kinesis", "mux", "bing_webmaster"})
        google_ads_codes = {item["code"] for item in self.entries["google_ads"]["limitations"]}
        self.assertIn("google_ads_service_account_only", google_ads_codes)
        self.assertFalse(self.entries["google_ads"]["supports_oauth"])
        blocked_verified = {name for name, entry in self.entries.items()
                            if entry["execution_family"] == "dlt_verified"
                            and any(item["blocking"] for item in entry["limitations"])}
        self.assertEqual(blocked_verified, set())
        for entry in self.entries.values():
            if entry["execution_family"] == "dlt_sql":
                issue = next(item for item in entry["limitations"]
                             if item["code"] == "sql_parameter_contract_tested")
                self.assertFalse(issue["blocking"])

    def test_filesystem_contracts_expose_selected_content_readers_and_boundaries(self):
        filesystem = {name: entry for name, entry in self.entries.items()
                      if entry["execution_family"] == "dlt_filesystem"}
        self.assertEqual(set(filesystem), {"azure", "gcs", "s3", "sftp"})
        for entry in filesystem.values():
            self.assertEqual(entry["factory"], "dlt.sources.filesystem.filesystem")
            limitations = {item["code"]: item for item in entry["limitations"]}
            self.assertFalse(limitations["filesystem_reader_contract_tested"]["blocking"])
            self.assertEqual(entry["filesystem_contract"]["required_parameters"], ["tables"])
            self.assertEqual(entry["filesystem_contract"]["runtime_verification"], "not_performed")
            self.assertIn("contextbase/connectors/filesystem_adapter.py", entry["verification"]["implementation_paths"])
            self.assertIn("tests/test_filesystem_adapter.py", entry["verification"]["test_paths"])
            self.assertFalse(entry["live_validated"])

    def test_explicit_adapter_credential_specs_preserve_optional_and_json_contracts(self):
        from contextbase.connectors.verified_adapter import ADAPTER_CREDENTIAL_SPECS
        from contextbase.connectors.filesystem_adapter import FILESYSTEM_CREDENTIAL_SPECS
        for name, fields in {**ADAPTER_CREDENTIAL_SPECS, **FILESYSTEM_CREDENTIAL_SPECS}.items():
            self.assertEqual(self.entries[name]["credential_specs"], fields)
        google = {field["name"]: field for field in self.entries["google_ads"]["credential_specs"]}
        self.assertEqual(google["credentials"]["env_var"], "GOOGLE_SERVICE_ACCOUNT_JSON")
        self.assertTrue(google["credentials"]["secret"])
        self.assertTrue(google["customer_id"]["required"])
        for name in ("google_sheets", "google_analytics"):
            fields = {field["name"]: field for field in self.entries[name]["credential_specs"]}
            self.assertEqual(fields["credentials"]["env_var"], "GOOGLE_SERVICE_ACCOUNT_JSON")
            self.assertEqual(fields["credentials"]["cli_flag"], "--credentials")
            self.assertTrue(fields["credentials"]["secret"])
            self.assertTrue(fields["credentials"]["required"])
            self.assertFalse(self.entries[name]["supports_oauth"])
            limitations = {item["code"]: item for item in self.entries[name]["limitations"]}
            self.assertFalse(limitations["google_service_account_only"]["blocking"])
            self.assertIn("verified_credential_adapter_contract_tested", limitations)
            self.assertEqual(self.entries[name]["verification"]["adapter_contract"], "controlled_doubles")
        analytics = {field["name"]: field for field in self.entries["google_analytics"]["credential_specs"]}
        self.assertEqual(analytics["property_id"]["env_var"], "GA_PROPERTY_ID")
        self.assertFalse(analytics["property_id"]["secret"])
        self.assertTrue(analytics["property_id"]["required"])
        self.assertFalse(next(field for field in self.entries["kinesis"]["credential_specs"]
                              if field["name"] == "aws_session_token")["required"])
        self.assertFalse(next(field for field in self.entries["kafka"]["credential_specs"]
                              if field["name"] == "sasl_password")["required"])

    def test_verification_is_scoped_to_selected_native_fixtures_and_not_live(self):
        native_sources = {"sqlite", "intercom", "linear"}
        native_resources = {("intercom", "contacts"), ("linear", "issues")}
        for entry in self.entries.values():
            verification = entry["verification"]
            self.assertEqual(verification["definition"], "packaged")
            self.assertEqual(verification["authenticated_live"], "not_performed")
            self.assertEqual(verification["native_dlt_integration"],
                             "not_applicable" if entry["execution_family"] == "file" else
                             "passed_local_fixture" if entry["name"] in native_sources else "not_performed")
            self.assertFalse(entry["live_validated"])
            self.assertEqual("native_fixture_evidence" in verification, entry["name"] in native_sources)
            if entry["name"] in native_sources:
                evidence = verification["native_fixture_evidence"]
                self.assertEqual(evidence["summary_path"], "contextbase/connectors/native-verification.json")
                self.assertEqual(evidence["resources"], {"sqlite": ["accounts", "events"],
                                                        "intercom": ["contacts"], "linear": ["issues"]}[entry["name"]])
            for resource in entry["resources"]:
                selected = (entry["name"], resource["name"]) in native_resources
                self.assertEqual(resource["verification"]["native_dlt_integration"],
                                 "passed_local_fixture" if selected else "not_performed")
                self.assertEqual(resource["verification"]["authenticated_live"], "not_performed")
                self.assertFalse(resource["live_validated"])
                self.assertEqual("native_fixture_evidence" in resource["verification"], selected)
            for path in verification["implementation_paths"] + verification["test_paths"]:
                self.assertTrue((ROOT.parents[1] / path).is_file(), path)
            if entry["execution_family"] == "dlt_rest":
                self.assertIsInstance(entry["resource_defaults"], dict)
                self.assertIn("REST", verification["scope"])
            if entry["execution_family"] == "dlt_graphql":
                self.assertIn("GraphQL", verification["scope"])
                self.assertNotIn("REST", verification["scope"])
        self.assertEqual(self.entries["hubspot"]["verification"]["adapter_contract"], "shared_worker_only")
        self.assertEqual(self.entries["salesforce"]["verification"]["adapter_contract"], "controlled_doubles")

    def test_portable_native_evidence_matches_selected_catalog_scope_and_outcomes(self):
        path = ROOT / "native-verification.json"
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence["spec"], "contextbase.native-connector-evidence/v1")
        self.assertTrue(evidence["actual_dlt_executed"])
        self.assertEqual(evidence["authenticated_live"], "not_performed")
        self.assertEqual(evidence["live_vendor_calls"], 0)
        self.assertEqual(evidence["model_calls"], 0)
        self.assertFalse(evidence["performance_claim"])
        self.assertEqual(evidence["runtime_versions"]["dlt"], "1.24.0")
        for name, entry in self.entries.items():
            if entry["execution_family"] != "file":
                self.assertEqual(entry["dependencies"]["base"],
                                 ["dlt[duckdb,sql_database,filesystem]==1.24.0", "PyYAML==6.0.3"])
        checks = {check["id"]: check for check in evidence["checks"]}
        self.assertEqual(set(checks), {"sqlite_snapshot_lifecycle", "rest_graphql_snapshot_lifecycle"})
        for name in ("sqlite", "intercom", "linear"):
            reference = self.entries[name]["verification"]["native_fixture_evidence"]
            check = checks[reference["check_id"]]
            self.assertEqual(check["source_resources"][name], reference["resources"])
            self.assertEqual(check["fixture_kind"], reference["fixture_kind"])
        sql = checks["sqlite_snapshot_lifecycle"]["outcomes"]
        api = checks["rest_graphql_snapshot_lifecycle"]["outcomes"]
        self.assertEqual((sql["initial_metric_value"], sql["refreshed_metric_value"]), (10, 4))
        self.assertEqual((api["initial_metric_value"], api["refreshed_metric_value"]), (10, 15))
        self.assertTrue(api["partial_extraction_failure_retains_prior_snapshot"])
        for check in checks.values():
            self.assertEqual(check["result"], "passed")
            self.assertTrue(check["outcomes"]["approved_context_bytes_preserved"])
            self.assertEqual(check["outcomes"]["approved_metric_version_before"],
                             check["outcomes"]["approved_metric_version_after"])
            for relative in check["harness_paths"]:
                self.assertFalse(Path(relative).is_absolute())
                self.assertTrue((ROOT.parents[1] / relative).is_file())
            self.assertFalse(Path(check["receipt_reference"]["path"]).is_absolute())
            self.assertRegex(check["receipt_reference"]["sha256"], "^[0-9a-f]{64}$")
        self.assertNotIn("/Users/", path.read_text())
        self.assertNotIn('"commands"', path.read_text())
        composition = evidence["composition_checks"]
        self.assertEqual(len(composition), 1)
        self.assertEqual(composition[0]["id"], "filesystem_reader_pipeline")
        self.assertTrue(composition[0]["actual_dlt_executed"])
        self.assertEqual(composition[0]["cloud_connector_validation"], "not_performed")
        self.assertEqual(composition[0]["fixture_kind"], "synthetic_in_memory_listing_and_client_doubles")
        self.assertEqual(composition[0]["outcomes"]["network_attempts"], 0)
        for name in ("s3", "gcs", "azure", "sftp"):
            self.assertEqual(self.entries[name]["verification"]["native_dlt_integration"], "not_performed")

    def test_vendored_discovery_has_no_ambient_config_or_install_path(self):
        for relative in ("registry.py", "source_config.py", "source_fetch.py"):
            tree = ast.parse((UPSTREAM / relative).read_text())
            with self.subTest(file=relative):
                functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
                self.assertFalse({"_load_local_configs", "_get_local_configs_dir", "_fetch_source",
                                  "_install_requirements", "_install_command"} & functions)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, {"home", "expanduser", "getenv", "urlopen"})
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or "").startswith("dinobase"))
        resolver = ast.parse((UPSTREAM / "source_fetch.py").read_text())
        imports = {alias.name.split(".")[0] for node in ast.walk(resolver)
                   if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & {"subprocess", "requests", "urllib", "os"})

    def test_metadata_compatibility_does_not_call_source(self):
        tree = ast.parse((UPSTREAM / "sources/__init__.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "extract_metadata")
        namespace = {}
        isolated = ast.Module(body=[function], type_ignores=[])
        exec(compile(isolated, "<metadata compatibility>", "exec"), namespace)
        self.assertEqual(namespace["extract_metadata"]("stripe", {}, ["customers"]), {})

    def test_verified_resolver_requires_explicit_pinned_local_bundle(self):
        spec = importlib.util.spec_from_file_location("_resolver_catalog_test", UPSTREAM / "source_fetch.py")
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        with self.assertRaisesRegex(ImportError, "Configure the preinstalled"):
            resolver.ensure_verified_source("salesforce")
        previous_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), previous_path))
        with patch("pathlib.Path.home", side_effect=AssertionError("No home discovery")), \
             patch("socket.socket", side_effect=AssertionError("No network")), \
             patch("subprocess.Popen", side_effect=AssertionError("No installer")):
            resolver.configure_verified_sources(ROOT / "verified")
            verified = json.loads((ROOT / "verified/manifest.json").read_text())
            for source in verified["sources"].values():
                name = Path(source["source_directory"]).name
                self.assertEqual(resolver.ensure_verified_source(name), (ROOT / "verified").resolve())
            with self.assertRaises(ImportError):
                resolver.ensure_verified_source("not_in_pinned_bundle")

    def test_verified_resolver_rejects_changed_manifest_and_source_bytes(self):
        spec = importlib.util.spec_from_file_location("_resolver_changed_test", UPSTREAM / "source_fetch.py")
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sources/example/__init__.py"
            source.parent.mkdir(parents=True)
            source.write_text("# Synthetic source bytes; never imported.\n")
            manifest = {"spec": "contextbase.verified-connectors/v1", "repository": resolver.VERIFIED_SOURCES_REPO,
                        "ref": resolver.VERIFIED_SOURCES_REF, "sources": {"example": {"source_directory": "sources/example"}},
                        "files": [{"path": "sources/example/__init__.py", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            resolver.configure_verified_sources(root)
            source.write_text("# Changed synthetic bytes.\n")
            with self.assertRaisesRegex(ImportError, "missing or changed"):
                resolver.ensure_verified_source("example")
            path.write_text(json.dumps({**manifest, "extra": "changed"}))
            with self.assertRaisesRegex(ImportError, "missing or changed"):
                resolver.ensure_verified_source("example")


if __name__ == "__main__":
    unittest.main()
