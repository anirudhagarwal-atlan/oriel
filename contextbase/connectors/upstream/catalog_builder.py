"""Rebuild static connector declarations using an already installed PyYAML.

Run explicitly with a Python environment containing PyYAML. Normal Oriel
catalog reads use catalog.json and do not import this builder or source factories.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REVISION = "2dd06369fc333cd51663c27b7d47a21a7946757b"
NATIVE_EVIDENCE_PATH = "contextbase/connectors/native-verification.json"
# Deliberately source/resource scoped. A shared implementation or execution
# family does not inherit these observed local-fixture outcomes.
NATIVE_FIXTURE_SCOPES = {
    "sqlite": {"check_id": "sqlite_snapshot_lifecycle", "resources": ["accounts", "events"],
               "fixture_kind": "synthetic_local_sqlite"},
    "intercom": {"check_id": "rest_graphql_snapshot_lifecycle", "resources": ["contacts"],
                 "fixture_kind": "synthetic_loopback_rest_graphql"},
    "linear": {"check_id": "rest_graphql_snapshot_lifecycle", "resources": ["issues"],
               "fixture_kind": "synthetic_loopback_rest_graphql"},
}


def _gap(code, message, *, blocking=True):
    return {"code": code, "message": message, "blocking": blocking}


def _merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def _local_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _native_verification(name, resource=None):
    evidence = NATIVE_FIXTURE_SCOPES.get(name)
    result = {"native_dlt_integration": "not_performed", "authenticated_live": "not_performed"}
    if evidence and (resource is None or resource in evidence["resources"]):
        result.update(native_dlt_integration="passed_local_fixture",
                      native_fixture_evidence={"summary_path": NATIVE_EVIDENCE_PATH, **evidence})
    return result


def _verification(family, name, adapted_names):
    paths = ["contextbase/connectors/worker.py"]
    tests = ["tests/test_connector_worker.py"]
    scope = "Shared extraction-worker contract with controlled doubles; this factory was not individually executed."
    status = "shared_worker_only"
    if family in {"dlt_rest", "dlt_graphql"}:
        paths.append("contextbase/connectors/upstream/" + ("yaml_source.py" if family == "dlt_rest" else "sources/graphql.py"))
        tests.append("tests/test_connector_translation.py")
        scope = ("Shared REST translation/pagination contracts with controlled doubles; packaged REST declarations were translated offline. No vendor extraction."
                 if family == "dlt_rest" else
                 "Shared GraphQL projection/pagination contracts with controlled doubles; declared Linear and Monday adapters were checked offline. No vendor extraction.")
        status = "controlled_doubles"
    elif family == "dlt_filesystem":
        paths.append("contextbase/connectors/filesystem_adapter.py")
        tests.append("tests/test_filesystem_adapter.py")
        scope = "Explicit clients, selected file readers, provenance and refusal contracts with doubles; Parquet reader uses local DuckDB. No dlt/vendor extraction."
        status = "controlled_doubles"
    elif family == "dlt_sql" or name in adapted_names:
        paths += ["contextbase/connectors/verified_adapter.py", "contextbase/connectors/upstream/sources/__init__.py"]
        tests.append("tests/test_verified_adapter.py")
        scope = "Explicit argument/configuration contracts and SQL parameter forwarding with controlled doubles; SDK and actual dlt execution remain unverified."
        if name in NATIVE_FIXTURE_SCOPES:
            scope = "Explicit argument/configuration contracts and SQL parameter forwarding with controlled doubles."
        status = "controlled_doubles"
    elif family == "file":
        paths = ["contextbase/connectors/service.py"]
        tests = ["tests/test_connector_service.py"]
        scope = "Local file configure/sync/publication/bootstrap/query contract; no network connector."
        status = "local_functional"
    native = _native_verification(name)
    if name in NATIVE_FIXTURE_SCOPES:
        scope += " Actual dlt passed a synthetic local fixture only for " + ", ".join(NATIVE_FIXTURE_SCOPES[name]["resources"]) + "; other resources and live vendor compatibility remain unverified."
    if family == "file":
        native["native_dlt_integration"] = "not_applicable"
    return {"definition": "packaged", "adapter_contract": status, "scope": scope,
            "implementation_paths": paths, "test_paths": tests, **native}


def _verified_factory_limitations(entry, adapted_names):
    """Compare copied declarations with pinned factory syntax without imports."""
    module_name, function_name = entry.import_path.rsplit(".", 1)
    module_path = ROOT.parent / "verified" / Path(*module_name.split("."))
    path = module_path / "__init__.py" if module_path.is_dir() else module_path.with_suffix(".py")
    tree = ast.parse(path.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == function_name]
    if len(functions) != 1:
        raise ValueError("Expected one pinned verified factory: " + entry.import_path)
    function = functions[0]
    accepted = {argument.arg for argument in function.args.args + function.args.kwonlyargs}
    provided = {credential.name for credential in entry.credentials} | set(entry.extra_params)
    unexpected = sorted(provided - accepted) if function.args.kwarg is None else []
    limitations = []
    if unexpected:
        if entry.name not in adapted_names:
            limitations.append(_gap("verified_factory_credential_mapping_incompatible",
                                     "The pinned factory cannot accept these registry arguments: "
                                     + ", ".join(unexpected) + ". An explicit credential adapter is required."))
    if entry.name in adapted_names:
        limitations.append(_gap("verified_credential_adapter_contract_tested",
                                 "Explicit credential/configuration mapping is implemented in verified_adapter.py and checked by test_verified_adapter.py using controlled doubles; native dlt and SDK verification is pending.", blocking=False))
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "dlt" and target.attr == "resource"):
            limitations.append(_gap("verified_resource_wrapper_contract_tested",
                                     "The pinned factory returns a resource; worker.py wraps it without consuming rows. test_connector_worker.py checks selection with controlled doubles; native dlt verification is pending.", blocking=False))
            break
    if entry.name == "google_ads":
        limitations.append(_gap("google_ads_service_account_only",
                                 "This adapter accepts complete service-account JSON in an environment reference, explicit developer token, delegated email and customer ID. OAuth/file-path credential flows are not implemented.", blocking=False))
    if entry.name in {"google_sheets", "google_analytics"}:
        limitations.append(_gap("google_service_account_only",
                                 "This adapter accepts complete service-account JSON through GOOGLE_SERVICE_ACCOUNT_JSON and passes an explicitly resolved service-account object to the pinned factory. OAuth, credential-file paths and ambient application-default credentials are not supported. Native dlt/SDK verification is pending.", blocking=False))
    if entry.name == "kafka":
        limitations.append(_gap("kafka_explicit_tls_read_contract",
                                 "SSL or SASL_SSL and explicit topics/group are required. Consumer auto-commit and auto-offset-store are disabled; supported SASL mechanisms are PLAIN, SCRAM-SHA-256 and SCRAM-SHA-512.", blocking=False))
    return limitations


def _resources(config, family):
    result = []
    for resource in config.get("resources", []):
        limitations = []
        if family == "dlt_rest":
            endpoint = resource.get("endpoint", {})
            supported = {"path", "method", "params", "headers", "data_selector", "auth", "paginator", "json", "data", "body", "content_type", "base_url", "path_params", "response_format", "incremental"}
            for field in sorted(set(endpoint) - supported):
                limitations.append(_gap("endpoint_" + field + "_not_translated",
                                         f"The translator does not support endpoint.{field}."))
            for field in ("params", "headers"):
                if field in endpoint and not isinstance(endpoint[field], dict) and not (field == "params" and endpoint[field] is None):
                    limitations.append(_gap("endpoint_" + field + "_not_mapping",
                                             f"Endpoint {field} must be a mapping."))
            if (set(endpoint) & {"auth", "response_format", "json", "body", "paginator", "path_params", "base_url", "content_type"}
                    or resource.get("auth") or resource.get("parent") or resource.get("selected", True) is False
                    or endpoint.get("params", {}) is None):
                limitations.append(_gap("rest_translation_contract_tested",
                                         "Declared endpoint/auth/body/pagination/parent/selection handling has controlled adapter tests in test_connector_translation.py. Native outcomes, where present, cover only this resource's listed synthetic fixture; live source compatibility remains unverified.", blocking=False))
            defaults = config.get("resource_defaults", {}).get("endpoint", {})
            effective = _merge(defaults, endpoint)
            missing = sorted({key for location in ("params", "json", "body")
                              if isinstance(effective.get(location), dict)
                              for key, value in effective[location].items() if value is None})
            if missing:
                limitations.append(_gap("null_parameters_require_configuration",
                                         "Provide declared values for selected resources/dependencies before sync: " + ", ".join(missing), blocking=False))
        if family == "dlt_graphql" and config.get("name") == "linear" and resource["name"] in {"issues", "projects"}:
            fields = "labels.nodes" if resource["name"] == "issues" else "teams.nodes and members.nodes"
            limitations.append(_gap("nested_graphql_projection_not_exhaustive",
                                     "Top-level rows are paginated, but embedded " + fields + " are finite projections without nested pagination; do not treat them as exhaustive collections.", blocking=False))
        result.append({"name": resource["name"], "selected": resource.get("selected", True),
                       "description": resource.get("description", ""), "declared": resource,
                       "limitations": limitations, "live_validated": False,
                       "verification": _native_verification(config.get("name"), resource["name"])})
    return result


def build_catalog():
    import yaml
    adapters = _local_module("_contextbase_verified_catalog_metadata", ROOT.parent / "verified_adapter.py")
    filesystem = _local_module("_contextbase_filesystem_catalog_metadata", ROOT.parent / "filesystem_adapter.py")
    adapted_names = set(adapters.ADAPTER_CREDENTIAL_SPECS)
    # File loading bypasses parent package initialization. The adapted registry
    # itself reads only packaged YAML and performs no credential/source calls.
    module_name = "_contextbase_bundled_connector_registry"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "registry.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    previous_bytecode_setting = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    connectors = []
    configs = {}
    for path in sorted((ROOT / "sources/configs").glob("*.yaml")):
        configs[path.stem] = yaml.safe_load(path.read_text())
    for name, entry in sorted(module.SOURCES.items()):
        config = configs.get(name, {})
        if entry.graphql_config:
            family = "dlt_graphql"
        elif entry.rest_api_config:
            family = "dlt_rest"
        elif entry.import_path.startswith("sources."):
            family = "dlt_verified"
        elif "sql_database" in entry.import_path:
            family = "dlt_sql"
        elif "filesystem" in entry.import_path:
            family = "dlt_filesystem"
        else:
            raise ValueError("Unexpected source execution family: " + name)
        config_path = "sources/configs/" + name + ".yaml" if config else None
        origin_path = config_path or "registry.py"
        resources = _resources(config, family)
        limitations = []
        if family == "dlt_rest":
            limitations.append(_gap("rest_configuration_contract_tested",
                                     "Declared per-resource query/body parameters, dependency selection and read pagination are implemented and tested with controlled doubles. Required values must be supplied before sync; native evidence, where present, covers only the listed fixture resources. Live API compatibility remains unverified.", blocking=False))
        if family == "dlt_graphql":
            limitations.append(_gap("graphql_pagination_contract_tested",
                                     "Relay and declared Monday page/item continuation contracts have controlled-response tests. Native evidence, where present, covers only the listed fixture resources; live API compatibility remains unverified.", blocking=False))
        if family == "dlt_verified":
            limitations.append(_gap("requires_preinstalled_verified_source",
                                     "Requires the explicitly configured pinned local source bundle and its separately installed runtime dependencies.", blocking=False))
            limitations.extend(_verified_factory_limitations(entry, adapted_names))
        if family == "dlt_sql":
            limitations.append(_gap("sql_parameter_contract_tested",
                                     "schema, chunk_size and reflection_level are validated and forwarded before factory construction; controlled doubles cover this path. Native evidence, where present, covers only the listed SQLite fixture tables; other drivers remain unverified.", blocking=False))
        if family == "dlt_filesystem":
            limitations.append(_gap("filesystem_reader_contract_tested",
                                     "Explicit selected CSV/JSONL/Parquet readers, clients, provenance and refusal contracts have controlled tests. Missing files or zero-row selected tables fail; actual dlt/SDK extraction remains pending.", blocking=False))
            if name == "azure":
                limitations.append(_gap("azure_explicit_account_url_form",
                                         filesystem.FILESYSTEM_CONTRACT["azure_url"], blocking=False))
            if name == "sftp":
                limitations.append(_gap("sftp_explicit_host_key_required",
                                         filesystem.FILESYSTEM_CONTRACT["sftp_trust"], blocking=False))
        supports_oauth = entry.supports_oauth and name not in {"google_ads", "google_sheets", "google_analytics"}
        if supports_oauth:
            limitations.append(_gap("oauth_capability_not_authorization_flow",
                                     "The catalog flag describes token support; this adaptation provides no OAuth authorization flow.", blocking=False))
        credentials = [{**item, "required": True} for item in entry.to_dict()["credentials"]]
        if name in adapted_names:
            credentials = adapters.ADAPTER_CREDENTIAL_SPECS[name]
        if name in filesystem.FILESYSTEM_CREDENTIAL_SPECS:
            credentials = filesystem.FILESYSTEM_CREDENTIAL_SPECS[name]
        connector = {
            "name": name, "description": entry.description, "execution_family": family,
            "factory": entry.import_path, "category": entry.derive_type(),
            "credential_specs": credentials,
            "supports_oauth": supports_oauth, "credential_help": entry.credential_help,
            "resources": resources,
            "resource_discovery": "declared" if resources else "requires_configured_source",
            "declared_write_endpoints": [item.get("name") for item in config.get("write_endpoints", [])],
            "dependencies": {"base": ["dlt[duckdb,sql_database,filesystem]==1.24.0", "PyYAML==6.0.3"],
                             "additional": [entry.pip_extra] if entry.pip_extra else [],
                             "verified_source_ref": "75b3ec17eab99d0079d9f61b7f47fc8b899a5738" if family == "dlt_verified" else None,
                             "install_policy": "explicit_preinstalled_environment_only"},
            "source_metadata": {"upstream_enrichment": name in {"stripe", "hubspot", "postgres", "mysql"},
                                "adaptation_enrichment": "unavailable; bootstrap uses observed schema"},
            "provenance": {"revision": REVISION, "definition_path": "upstream/" + origin_path,
                           "definition_sha256": hashlib.sha256((ROOT / origin_path).read_bytes()).hexdigest()},
            "live_validated": False, "limitations": limitations,
            "verification": _verification(family, name, adapted_names),
        }
        if family == "dlt_rest":
            connector["resource_defaults"] = config.get("resource_defaults", {})
        if family == "dlt_filesystem":
            connector["filesystem_contract"] = filesystem.FILESYSTEM_CONTRACT
        connectors.append(connector)
    for name in ("csv", "parquet"):
        connectors.append({
            "name": name, "description": name.upper() + " files imported into the local workspace",
            "execution_family": "file", "factory": None, "category": "file",
            "credential_specs": [{"name": "path", "cli_flag": "--path", "env_var": None,
                                  "prompt": "Path to a local file", "secret": False, "required": True}],
            "supports_oauth": False, "credential_help": None,
            "resources": [], "resource_discovery": "local_file_schema", "declared_write_endpoints": [],
            "dependencies": {"base": ["duckdb"], "additional": [], "verified_source_ref": None,
                             "install_policy": "explicit_preinstalled_environment_only"},
            "source_metadata": {"upstream_enrichment": "column_name_and_type_inference",
                                "adaptation_enrichment": "bootstrap uses observed schema"},
            "provenance": {"revision": REVISION, "definition_path": "dinobase/cli.py:_list_available_connectors",
                           "upstream_factory": "dinobase.sync.sources.parquet.add_file_source"},
            "live_validated": False,
            "verification": _verification("file", name, adapted_names),
            "limitations": [_gap("local_file_adapter", "Oriel routes this entry through its existing local import boundary; upstream remote file views are not enabled.", blocking=False)],
        })
    additional_assets = []
    known = {entry["name"] for entry in connectors}
    for name, config in configs.items():
        if name not in known:
            additional_assets.append({"name": name, "status": "legacy_unimplemented_asset",
                                      "definition_path": "upstream/sources/configs/" + name + ".yaml",
                                      "description": config.get("description", ""),
                                      "reason": "Top-level endpoints/auth_methods is not handled by the registry or source factory.",
                                      "live_validated": False})
    if len(connectors) != 101 or len(known) != 101:
        raise ValueError("Pinned upstream catalog must contain exactly 101 unique built-in entries.")
    return {"spec": "contextbase.connector-catalog/v1", "connector_count": len(connectors),
            "contains_credential_values": False, "discovery_calls": 0,
            "upstream": {"project": "Dinobase", "repository": "https://github.com/kappa90/dinobase",
                         "revision": REVISION, "license": "MIT Expat", "source_manifest": "upstream/SOURCE.json"},
            "scope": "Packaged source declarations; catalog inclusion is not verified live connectivity.",
            "connectors": sorted(connectors, key=lambda item: item["name"]),
            "additional_assets": additional_assets}


if __name__ == "__main__":
    output = ROOT.parent / "catalog.json"
    output.write_text(json.dumps(build_catalog(), indent=2, sort_keys=True) + "\n")
    print(str(output))
