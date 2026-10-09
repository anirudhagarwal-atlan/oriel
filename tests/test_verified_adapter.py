"""Controlled adapter contracts; no dlt/vendor imports or source calls."""
import ast
from contextlib import nullcontext
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from contextbase.connectors.verified_adapter import (
    ADAPTER_CREDENTIAL_SPECS, SQL_PARAMETER_NAMES, build_verified_source, sql_parameters,
)


ROOT = Path(__file__).resolve().parents[1] / "contextbase/connectors"


class Credential:
    @classmethod
    def from_init_value(cls, values):
        obj = cls()
        obj.values = dict(values)
        for key, value in values.items():
            setattr(obj, key, value)
        return obj

    def is_partial(self):
        return False

    def is_resolved(self):
        return True


class DictionaryProvider:
    def __init__(self):
        self.values = {}

    def set_value(self, key, value, pipeline, *sections):
        self.values[(pipeline, *sections, key)] = value


def dependencies(name):
    exports = {
        "sources.salesforce.helpers.client": {"SecurityTokenAuth": Credential},
        "sources.kafka.helpers": {"KafkaCredentials": Credential},
        "dlt.common.configuration.specs": {"AwsCredentials": Credential},
        "dlt.sources.credentials": {"GcpServiceAccountCredentials": Credential},
        "dlt.common.configuration.providers.dictionary": {"DictionaryProvider": DictionaryProvider},
    }
    if name not in exports:
        raise AssertionError("Unexpected optional import: " + name)
    return SimpleNamespace(**exports[name])


def pinned_factory(source_name):
    """Enforce the real copied function's argument names without importing it."""
    manifest = json.loads((ROOT / "verified/manifest.json").read_text())
    path, name = manifest["sources"][source_name]["import_path"].rsplit(".", 1)
    module = ROOT / "verified" / Path(*path.split("."))
    file = module / "__init__.py" if module.is_dir() else module.with_suffix(".py")
    fn = next(node for node in ast.parse(file.read_text()).body
              if isinstance(node, ast.FunctionDef) and node.name == name)
    args = fn.args.args
    required_count = len(args) - len(fn.args.defaults)
    signature = inspect.Signature([
        inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=inspect.Parameter.empty if i < required_count else None)
        for i, arg in enumerate(args)
    ])
    calls = []
    def factory(**kwargs):
        signature.bind(**kwargs)
        calls.append(kwargs)
        return SimpleNamespace(name=name, section=path.split(".")[-1], resources={})
    factory.calls = calls
    return factory


def isolated_get_source(entry, factory):
    """Execute production dispatch with controlled optional modules, not YAML/dlt."""
    tree = ast.parse((ROOT / "upstream/sources/__init__.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "get_source")
    namespace = {
        "Any": Any, "sys": SimpleNamespace(stderr=None),
        "get_source_entry": lambda _: entry, "load_yaml_config": lambda _: None,
        "build_dlt_source": Mock(),
        "importlib": SimpleNamespace(import_module=lambda _: SimpleNamespace(factory=factory)),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<isolated production get_source>", "exec"), namespace)
    return namespace


class VerifiedAdapterTests(unittest.TestCase):
    def build(self, name, values, **kwargs):
        factory = pinned_factory(name)
        source = build_verified_source(name, factory, values, importer=dependencies, **kwargs)
        return source, factory.calls[0]

    def test_salesforce_uses_security_token_object_and_correct_username_field(self):
        _, arguments = self.build("salesforce", {"username": "synthetic-user", "password": "synthetic-password",
                                               "security_token": "synthetic-token"})
        self.assertEqual(set(arguments), {"credentials"})
        self.assertEqual(arguments["credentials"].values,
                         {"user_name": "synthetic-user", "password": "synthetic-password", "security_token": "synthetic-token"})

    def test_kafka_nested_credentials_and_explicit_topics_match_pinned_factory(self):
        values = {"bootstrap_servers": "example.invalid:9092", "group_id": "synthetic-group",
                  "security_protocol": "SASL_SSL", "sasl_mechanisms": "PLAIN",
                  "sasl_username": "synthetic-user", "sasl_password": "synthetic-password",
                  "topics": ["synthetic-topic"], "batch_size": 17}
        _, arguments = self.build("kafka", values)
        self.assertEqual(arguments["topics"], ["synthetic-topic"])
        self.assertEqual(arguments["batch_size"], 17)
        self.assertEqual(arguments["credentials"].values,
                         {key: value for key, value in values.items() if key not in {"topics", "batch_size"}})
        self.assertNotIn("bootstrap_servers", arguments)
        self.assertIn("bootstrap_servers", values)  # Input was not mutated.

    def test_kafka_refuses_missing_topics_or_partial_sasl_before_construction(self):
        base = {"bootstrap_servers": "example.invalid:9092", "group_id": "synthetic-group",
                "security_protocol": "SSL", "topics": ["synthetic-topic"]}
        for changed in ({"topics": []}, {"security_protocol": "SASL_SSL"},
                        {"security_protocol": "AUTO"}, {"security_protocol": "PLAINTEXT"},
                        {"security_protocol": "SASL_PLAINTEXT"}, {"sasl_password": "synthetic-password"}):
            with self.subTest(changed=tuple(changed)):
                factory = Mock()
                with self.assertRaises(ValueError):
                    build_verified_source("kafka", factory, {**base, **changed}, importer=dependencies)
                factory.assert_not_called()

    def test_kafka_consumer_cannot_automatically_write_offsets_and_uses_tls(self):
        consumer = Mock(return_value="controlled-consumer")
        def imports(name):
            return SimpleNamespace(Consumer=consumer) if name == "confluent_kafka" else dependencies(name)
        factory = pinned_factory("kafka")
        build_verified_source("kafka", factory, {
            "bootstrap_servers": "example.invalid:9093", "group_id": "synthetic-group",
            "security_protocol": "SASL_SSL", "sasl_mechanisms": "SCRAM-SHA-256",
            "sasl_username": "synthetic-user", "sasl_password": "synthetic-password", "topics": "synthetic-topic",
        }, importer=imports)
        consumer.assert_not_called()  # Consumer construction stays lazy.
        self.assertEqual(factory.calls[0]["credentials"].init_consumer(), "controlled-consumer")
        config = consumer.call_args.args[0]
        self.assertIs(config["enable.auto.commit"], False)
        self.assertIs(config["enable.auto.offset.store"], False)
        self.assertIs(config["enable.ssl.certificate.verification"], True)
        self.assertEqual(config["ssl.endpoint.identification.algorithm"], "https")
        self.assertEqual(config["security.protocol"], "SASL_SSL")

    def test_kinesis_passes_complete_explicit_aws_object_and_stream_parameters(self):
        values = {"stream_name": "synthetic-stream", "aws_access_key_id": "synthetic-key",
                  "aws_secret_access_key": "synthetic-secret", "region_name": "us-east-1",
                  "aws_session_token": "synthetic-session", "max_number_of_messages": 10}
        _, arguments = self.build("kinesis", values)
        self.assertEqual(arguments["stream_name"], "synthetic-stream")
        self.assertEqual(arguments["max_number_of_messages"], 10)
        self.assertEqual(arguments["credentials"].values,
                         {key: values[key] for key in ("aws_access_key_id", "aws_secret_access_key", "region_name", "aws_session_token")})
        self.assertNotIn("aws_access_key_id", arguments)
        factory = Mock()
        with self.assertRaises(ValueError):
            build_verified_source("kinesis", factory, {key: value for key, value in values.items()
                                                      if key != "region_name"}, importer=dependencies)
        factory.assert_not_called()

    def test_asana_calls_zero_argument_factory_and_retains_scoped_token_for_extraction(self):
        provider = DictionaryProvider()
        source, arguments = self.build("asana", {"access_token": "synthetic-token"}, resource_config=provider)
        self.assertEqual(arguments, {})
        # This is read after source construction, matching lazy resource config.
        scope = (None, "sources", source.section, source.name, "access_token")
        self.assertEqual(provider.values, {scope: "synthetic-token"})
        self.assertNotIn((None, "access_token"), provider.values)
        self.assertNotIn((None, "sources", "different_source", "access_token"), provider.values)

    def test_google_ads_uses_only_in_memory_client_and_explicit_resource_arguments(self):
        document = {"type": "service_account", "project_id": "synthetic-project",
                    "client_email": "service@example.invalid", "private_key": "synthetic-private-key",
                    "token_uri": "https://example.invalid/must-not-be-forwarded"}
        values = {"credentials": json.dumps(document), "dev_token": "synthetic-developer-token",
                  "impersonated_email": "operator@example.invalid", "customer_id": "1234567890"}
        native = SimpleNamespace()
        native.with_scopes = Mock(return_value=native)
        native.with_subject = Mock(return_value=native)
        credential_type = SimpleNamespace(from_service_account_info=Mock(return_value=native))
        client = object()
        client_type = Mock(return_value=client)
        resource_names = ("customers", "campaigns", "change_events", "customer_clients")
        resources = {name: Mock(return_value=SimpleNamespace(name=name)) for name in resource_names}
        source_module = SimpleNamespace(**resources, get_client=Mock(side_effect=AssertionError("No tempfile helper")))
        def decorate(**options):
            def wrapper(fn):
                return lambda: SimpleNamespace(**options, resources={resource.name: resource for resource in fn()})
            return wrapper
        def imports(name):
            return {"google.oauth2.service_account": SimpleNamespace(Credentials=credential_type),
                    "google.ads.googleads.client": SimpleNamespace(GoogleAdsClient=client_type),
                    "sources.google_ads": source_module, "dlt": SimpleNamespace(source=decorate)}[name]
        inherited_factory = Mock(side_effect=AssertionError("No inherited tempfile-writing factory"))
        with patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("No credential files")), \
             patch("builtins.open", side_effect=AssertionError("No credential files")):
            source = build_verified_source("google_ads", inherited_factory, values, importer=imports)
        self.assertEqual(credential_type.from_service_account_info.call_args.args[0], {
            "project_id": "synthetic-project", "client_email": "service@example.invalid",
            "private_key": "synthetic-private-key", "type": "service_account",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        native.with_scopes.assert_called_once_with(["https://www.googleapis.com/auth/adwords"])
        native.with_subject.assert_called_once_with("operator@example.invalid")
        client_type.assert_called_once_with(credentials=native, developer_token="synthetic-developer-token", use_proto_plus=True)
        self.assertEqual(set(source.resources), set(resource_names))
        for resource in resources.values():
            resource.assert_called_once_with(client=client, customer_id="1234567890")
        inherited_factory.assert_not_called()
        source_module.get_client.assert_not_called()
        tree = ast.parse((ROOT / "verified/sources/google_ads/__init__.py").read_text())
        for name in resource_names:
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertEqual([arg.arg for arg in function.args.args], ["client", "customer_id"])
        self.assertEqual(values["credentials"], json.dumps(document))

    def test_google_ads_refuses_files_incomplete_and_ambiguous_json_without_reading(self):
        base = {"dev_token": "synthetic-token", "impersonated_email": "operator@example.invalid", "customer_id": "1234"}
        for value in ("/never/open/credentials.json", '{"type":"service_account"}',
                      '{"type":"service_account","type":"service_account"}', '{"type":NaN}',
                      "synthetic-secret-not-json"):
            factory = Mock()
            with self.subTest(value_kind=value[:1]), patch("builtins.open", side_effect=AssertionError("No credential files")):
                with self.assertRaises(ValueError) as error:
                    build_verified_source("google_ads", factory, {**base, "credentials": value},
                                          resource_config=DictionaryProvider(), importer=dependencies)
                self.assertNotIn(value, str(error.exception))
                factory.assert_not_called()

    def test_google_union_factories_receive_explicit_service_account_and_unchanged_parameters(self):
        document = {"type": "service_account", "project_id": "synthetic-project",
                    "client_email": "synthetic@example.invalid", "private_key": "synthetic-private-key",
                    "private_key_id": "synthetic-key-id", "token_uri": "https://example.invalid/ignored"}
        cases = {
            "google_sheets": {"spreadsheet_url_or_id": "synthetic-spreadsheet", "range_names": ["Sheet1!A1:C9"],
                              "get_sheets": False, "get_named_ranges": False, "max_api_retries": 2},
            "google_analytics": {"property_id": "123456", "queries": [{"resource_name": "report",
                                  "dimensions": ["date"], "metrics": ["sessions"]}],
                                  "start_date": "2026-01-01", "rows_per_page": 200},
        }
        for name, params in cases.items():
            factory = pinned_factory(name)
            supplied = {"credentials": json.dumps(document), **params}
            before = json.loads(json.dumps(supplied))
            imports = Mock(side_effect=dependencies)
            with self.subTest(source=name), patch("builtins.open", side_effect=AssertionError("No credential files")):
                build_verified_source(name, factory, supplied, importer=imports)
            actual = factory.calls[0]
            self.assertEqual({key: value for key, value in actual.items() if key != "credentials"}, params)
            self.assertIsInstance(actual["credentials"], Credential)
            self.assertTrue(actual["credentials"].is_resolved())
            self.assertEqual(actual["credentials"].values,
                             {key: document[key] for key in ("project_id", "client_email", "private_key", "private_key_id")})
            imports.assert_called_once_with("dlt.sources.credentials")
            self.assertEqual(supplied, before)
            tree = ast.parse((ROOT / "verified/sources" / name / "__init__.py").read_text())
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                            and node.name == ("google_spreadsheet" if name == "google_sheets" else name))
            argument = next(arg for arg in function.args.args if arg.arg == "credentials")
            self.assertEqual(ast.unparse(argument.annotation),
                             "Union[GcpOAuthCredentials, GcpServiceAccountCredentials]")

    def test_google_union_credentials_refuse_partial_oauth_paths_and_ambiguous_json_before_import(self):
        full = {"type": "service_account", "project_id": "synthetic-project",
                "client_email": "synthetic@example.invalid", "private_key": "synthetic-private-key"}
        invalid = ["/never/open/credentials.json", '{"type":"service_account","type":"service_account"}',
                   '{"type":NaN}', json.dumps({**full, "type": "authorized_user"}), json.dumps([])]
        for key in ("project_id", "client_email", "private_key"):
            invalid.extend([json.dumps({k: v for k, v in full.items() if k != key}),
                            json.dumps({**full, key: " "}), json.dumps({**full, key: None})])
        for name in ("google_sheets", "google_analytics"):
            for value in invalid:
                factory, imports = Mock(), Mock(side_effect=AssertionError("No optional import"))
                with self.subTest(source=name, value_kind=value[:1]), \
                     patch("builtins.open", side_effect=AssertionError("No credential files")):
                    with self.assertRaises(ValueError) as error:
                        build_verified_source(name, factory, {"credentials": value,
                                              **({"property_id": "123456"} if name == "google_analytics" else {})},
                                              importer=imports)
                self.assertNotIn(value, str(error.exception))
                factory.assert_not_called()
                imports.assert_not_called()

    def test_google_union_rejects_partial_or_unresolved_dlt_object_before_factory(self):
        document = json.dumps({"type": "service_account", "project_id": "synthetic-project",
                               "client_email": "synthetic@example.invalid", "private_key": "synthetic-private-key"})
        for partial, resolved in ((True, False), (False, False)):
            class UnresolvedCredential(Credential):
                def is_partial(self):
                    return partial

                def is_resolved(self):
                    return resolved

            for name in ("google_sheets", "google_analytics"):
                factory = Mock()
                with self.subTest(source=name, partial=partial), self.assertRaises(ValueError):
                    build_verified_source(name, factory, {"credentials": document,
                                          **({"property_id": "123456"} if name == "google_analytics" else {})},
                                          importer=lambda _: SimpleNamespace(GcpServiceAccountCredentials=UnresolvedCredential))
                factory.assert_not_called()

    def test_resource_configuration_requires_explicit_exact_private_provider(self):
        for provider in (None, SimpleNamespace(set_value=Mock())):
            factory = Mock()
            with self.assertRaisesRegex(ValueError, "private DictionaryProvider"):
                build_verified_source("asana", factory, {"access_token": "synthetic-token"},
                                      resource_config=provider, importer=dependencies)
            factory.assert_not_called()

    def test_invalid_extra_or_credential_values_fail_before_factory(self):
        factory = Mock()
        for values in ({"access_token": "synthetic-token", "endpoint": "https://example.invalid"},
                       {"access_token": ""}, {}):
            with self.assertRaises(ValueError):
                build_verified_source("asana", factory, values, importer=dependencies)
        factory.assert_not_called()
        def failing_import(_):
            class InvalidCredential(Credential):
                @classmethod
                def from_init_value(cls, values):
                    raise ValueError(values["password"])
            return SimpleNamespace(SecurityTokenAuth=InvalidCredential)
        with self.assertRaises(ValueError) as error:
            build_verified_source("salesforce", factory, {"username": "synthetic-user", "password": "synthetic-private-value",
                                                          "security_token": "synthetic-token"}, importer=failing_import)
        self.assertNotIn("synthetic-private-value", str(error.exception))

    def test_metadata_declares_required_and_optional_fields_without_runtime(self):
        self.assertEqual(set(ADAPTER_CREDENTIAL_SPECS), {"salesforce", "kafka", "kinesis", "asana", "google_ads",
                                                       "google_sheets", "google_analytics"})
        optional = {(source, field["name"]) for source, fields in ADAPTER_CREDENTIAL_SPECS.items()
                    for field in fields if not field["required"]}
        self.assertEqual(optional, {("kafka", "sasl_mechanisms"), ("kafka", "sasl_username"),
                                    ("kafka", "sasl_password"), ("kinesis", "aws_session_token")})
        self.assertEqual(SQL_PARAMETER_NAMES, ("schema", "chunk_size", "reflection_level"))

    def test_dispatch_forwards_full_new_credentials_and_refuses_auth_parameter_override(self):
        entry = SimpleNamespace(import_path="sources.synthetic.factory", extra_params={}, credentials=[])
        factory = Mock()
        namespace = isolated_get_source(entry, factory)
        credentials = {"bootstrap_servers": "example.invalid:9092", "group_id": "synthetic-group",
                       "security_protocol": "SSL"}
        sentinel = SimpleNamespace(resources={})
        with patch("contextbase.connectors.upstream.source_fetch.ensure_verified_source"), \
             patch("contextbase.connectors.verified_adapter.build_verified_source", return_value=sentinel) as adapter:
            actual = namespace["get_source"]("kafka", credentials, params={"topics": ["synthetic-topic"]})
            self.assertIs(actual, sentinel)
            self.assertEqual(adapter.call_args.args[2], {**credentials, "topics": ["synthetic-topic"]})
            with self.assertRaises(ValueError):
                namespace["get_source"]("kafka", credentials, params={"security_protocol": "PLAINTEXT"})
            self.assertEqual(adapter.call_count, 1)

    def test_sql_parameters_reach_factory_and_reject_execution_overrides(self):
        resource = SimpleNamespace(apply_hints=Mock())
        source = SimpleNamespace(resources={"records": resource})
        def factory(*, credentials, schema, chunk_size, reflection_level):
            self.assertIs(credentials, engine)
            self.assertEqual((schema, chunk_size, reflection_level), ("synthetic_schema", 37, "minimal"))
            return source
        entry = SimpleNamespace(import_path="dlt.sources.sql_database.factory", extra_params={}, pip_extra=None)
        # Production chooses this exact path; its imported final symbol is a double.
        entry.import_path = "dlt.sources.sql_database.sql_database"
        namespace = isolated_get_source(entry, factory)
        namespace["importlib"] = SimpleNamespace(import_module=lambda _: SimpleNamespace(sql_database=factory))
        rows = SimpleNamespace(fetchall=lambda: [])
        engine = SimpleNamespace(connect=lambda: nullcontext(SimpleNamespace(execute=lambda *a, **k: rows)))
        create = Mock(return_value=engine)
        sqlalchemy = SimpleNamespace(create_engine=create, String=object, inspect=Mock(), text=lambda value: value)
        url_module = SimpleNamespace(make_url=lambda _: SimpleNamespace(drivername="sqlite", database="synthetic"))
        with patch.dict(sys.modules, {"sqlalchemy": sqlalchemy, "sqlalchemy.engine": url_module, "dlt": SimpleNamespace()}):
            actual = namespace["get_source"]("sqlite", {"credentials": "sqlite:///synthetic.db"},
                                              params={"schema": "synthetic_schema", "chunk_size": 37, "reflection_level": "minimal"})
            self.assertIs(actual, source)
            resource.apply_hints.assert_called_once_with(write_disposition="replace")
            create.reset_mock()
            with self.assertRaises(ValueError):
                namespace["get_source"]("sqlite", {"credentials": "sqlite:///synthetic.db"},
                                          params={"engine_adapter_callback": "must-not-run"})
            create.assert_not_called()
        for params in ({"credentials": "override"}, {"chunk_size": True}, {"chunk_size": 0},
                       {"schema": ""}, {"reflection_level": "unknown"}):
            with self.assertRaises(ValueError):
                sql_parameters(params)

    def test_yaml_and_filesystem_routes_forward_parameters_without_generic_imports(self):
        namespace = isolated_get_source(None, Mock(side_effect=AssertionError("No generic factory")))
        namespace["load_yaml_config"] = lambda _: {"client": {}}
        namespace["get_source"]("fixture_rest", {}, ["records"], params={"records": {"limit": 2}})
        namespace["build_dlt_source"].assert_called_once_with("fixture_rest", {}, ["records"], params={"records": {"limit": 2}})
        namespace["load_yaml_config"] = lambda _: None
        namespace["get_source_entry"] = lambda _: SimpleNamespace(import_path="dlt.sources.filesystem.filesystem")
        filesystem = Mock(return_value="synthetic-source")
        with patch.dict(sys.modules, {"contextbase.connectors.filesystem_adapter": SimpleNamespace(build_filesystem_source=filesystem)}):
            self.assertEqual(namespace["get_source"]("s3", {"bucket_url": "synthetic"}, ["records"],
                                                   params={"tables": {"records": {}}}), "synthetic-source")
        filesystem.assert_called_once_with("s3", {"bucket_url": "synthetic"}, ["records"], {"tables": {"records": {}}})


if __name__ == "__main__":
    unittest.main()
