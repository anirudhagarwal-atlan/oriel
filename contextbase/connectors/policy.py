"""Noncredential source parameters exposed by the local connector workflow."""

import json
import re

_RESERVED_PARAMETER = re.compile(r"token|secret|password|credential|authorization|api.?key", re.I)
_ROUTING = {"query", "mutation", "script", "code", "path", "url", "endpoint", "base_url", "method", "headers", "auth", "resources"}
_RESOURCE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_EXECUTION_FAMILIES = {"dlt_rest", "dlt_graphql", "dlt_verified", "dlt_sql", "dlt_filesystem", "file"}
_PLAN_LIMITATIONS = [
    "Declaration-derived metadata only; no extraction was executed and no access was authorized.",
    "This plan does not verify endpoint, principal, field, history, record, pagination or runtime scope.",
]


_VERIFIED = {
    "airtable": "base_id table_names",
    "bing_webmaster": "site_urls",
    "facebook_ads": "chunk_size request_timeout app_api_version",
    "freshdesk": "endpoints per_page",
    "google_analytics": "property_id queries start_date rows_per_page",
    "google_sheets": "spreadsheet_url_or_id range_names get_sheets get_named_ranges max_api_retries",
    "hubspot": "include_history soft_delete include_custom_props properties",
    "jira": "page_size",
    "kafka": "topics batch_size batch_timeout start_from end_time",
    "kinesis": "stream_name initial_at_timestamp max_number_of_messages milliseconds_behind_latest parse_json chunk_size",
    "matomo": "live_events_site_id initial_load_past_days visit_timeout_seconds visit_max_duration_seconds get_live_event_visitors",
    "mongodb": "database collection_names parallel limit projection",
    "mux": "limit",
    "notion": "database_ids",
    "personio": "items_per_page",
    "pipedrive": "since_timestamp",
    "shopify": "api_version start_date end_date created_at_min items_per_page order_status",
    "slack": "page_size start_date end_date selected_channels table_per_channel replies include_private_channels",
    "strapi": "endpoints",
    "stripe": "endpoints start_date end_date",
    "workable": "start_date load_details",
    "zendesk": "load_all pivot_ticket_fields start_date end_date",
}


def parameter_names(entry):
    family = entry["execution_family"]
    if family == "dlt_verified":
        names = _VERIFIED.get(entry["name"], "").split()
    elif family == "dlt_sql":
        from .verified_adapter import SQL_PARAMETER_NAMES
        names = SQL_PARAMETER_NAMES
    elif family == "dlt_filesystem":
        from .filesystem_adapter import FILESYSTEM_PARAMETER_NAMES
        names = FILESYSTEM_PARAMETER_NAMES
    elif family == "dlt_rest":
        names = [name for name, schema in resource_parameter_schema(entry).items() if schema["allowed"]]
    else:
        names = []
    credential_fields = {item["name"] for item in entry["credential_specs"]}
    return sorted(set(names) - credential_fields)


def _resource_endpoint(entry, resource):
    from .upstream.yaml_source import _merge
    return _merge(entry.get("resource_defaults", {}).get("endpoint", {}), resource["declared"].get("endpoint", {}))


def resource_parameter_schema(entry):
    """Expose only declared query/body fields; never auth or endpoint overrides."""
    if entry["execution_family"] != "dlt_rest":
        return {}
    schemas = {}
    for resource in entry["resources"]:
        endpoint = _resource_endpoint(entry, resource)
        fields = {}
        for location in ("params", "json", "body"):
            values = endpoint.get(location)
            if isinstance(values, dict):
                for key, value in values.items():
                    fields.setdefault(key, []).append(value)
        allowed = sorted(key for key, values in fields.items() if len(values) == 1
                         and not _RESERVED_PARAMETER.search(key) and key.lower() not in _ROUTING)
        schemas[resource["name"]] = {"allowed": allowed, "required": sorted(key for key in allowed if fields[key][0] is None)}
    return schemas


def validate_parameters(entry, params):
    if not isinstance(params, dict) or set(params) - set(parameter_names(entry)):
        raise ValueError("Use only the noncredential parameters listed by 'source describe'.")

    def inspect(value, depth=0):
        if depth > 12:
            raise ValueError("Source parameters are too deeply nested.")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or re.search(r"token|secret|password|credential|authorization|api.?key", key, re.I):
                    raise ValueError("Supply credentials through environment references, not parameters.")
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
        elif isinstance(value, str) and re.search(r"://[^/\s]*@", value):
            raise ValueError("URLs containing credentials cannot be saved as parameters.")
    inspect(params)
    try:
        raw = json.dumps(params, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("Source parameters must contain finite JSON values.") from None
    if len(raw.encode()) > 64_000:
        raise ValueError("Source parameters exceed the configuration size limit.")
    if entry["execution_family"] == "dlt_rest":
        from .upstream.yaml_source import _apply_parameters
        by_name = {resource["name"]: resource for resource in entry["resources"]}
        schemas = resource_parameter_schema(entry)
        for name, overrides in params.items():
            if not isinstance(overrides, dict) or set(overrides) - set(schemas[name]["allowed"]):
                raise ValueError("Use only declared noncredential fields in each resource's parameter object.")
            _apply_parameters(_resource_endpoint(entry, by_name[name]), overrides)
    elif entry["execution_family"] == "dlt_sql":
        from .verified_adapter import sql_parameters
        sql_parameters(params)
    elif entry["execution_family"] == "dlt_filesystem":
        from .filesystem_adapter import validate_filesystem_parameters
        validate_filesystem_parameters(params, source_type=entry["name"])
    return params


def _selection_catalog(entry, resources):
    if (type(entry) is not dict or type(entry.get("name")) is not str
            or not _SOURCE_NAME.fullmatch(entry["name"])
            or type(entry.get("execution_family")) is not str
            or entry.get("execution_family") not in _EXECUTION_FAMILIES):
        raise ValueError("Invalid connector selection metadata.")
    if (type(resources) is not list or len(resources) > 256
            or any(type(name) is not str or not _RESOURCE_NAME.fullmatch(name) for name in resources)
            or len(set(resources)) != len(resources)):
        raise ValueError("Select unique bounded resource names.")
    listed = entry.get("resources")
    if type(listed) is not list or len(listed) > 256:
        raise ValueError("Invalid connector resource declarations.")
    declared = {}
    for resource in listed:
        if (type(resource) is not dict or type(resource.get("name")) is not str
                or not _RESOURCE_NAME.fullmatch(resource["name"])
                or resource["name"] in declared
                or type(resource.get("selected", True)) is not bool):
            raise ValueError("Invalid connector resource declarations.")
        declared[resource["name"]] = resource
    if (declared or entry.get("resource_discovery") == "declared" or entry["execution_family"] == "dlt_rest") and set(resources) - set(declared):
        raise ValueError("Unknown selected resource.")
    return declared


def _rest_dependencies(declared, selected):
    needed, visiting, edges = set(), set(), set()

    def include(name):
        if name not in declared or name in visiting:
            raise ValueError("Invalid or cyclic resource selection.")
        if name in needed:
            return
        visiting.add(name)
        definition = declared[name].get("declared")
        if type(definition) is not dict:
            raise ValueError("Invalid resource parent declaration.")
        parent = definition.get("parent")
        if parent is not None:
            parent = parent.get("resource") if type(parent) is dict else parent
            if type(parent) is not str or not _RESOURCE_NAME.fullmatch(parent):
                raise ValueError("Invalid resource parent declaration.")
            edges.add((name, parent))
            include(parent)
        visiting.remove(name)
        needed.add(name)

    for name in selected:
        include(name)
    return sorted(needed), [{"resource": child, "requires": parent} for child, parent in sorted(edges)]


def selection_plan(entry, resources):
    """Describe declared output/fetch selection without importing any runtime.

    An empty request uses catalog defaults. REST parent closure describes all
    declared fetch dependencies, including unselected parents. Other families
    deliberately report unknown fetch scope even when output names are known.
    Returned metadata is detached and contains no parameters or source values.
    """
    declared = _selection_catalog(entry, resources)
    family = entry["execution_family"]
    defaults_known = bool(declared) or entry.get("resource_discovery") == "declared" or family == "dlt_rest"
    selected = sorted(resources) if resources else None
    if not resources and defaults_known:
        selected = sorted(name for name, resource in declared.items() if resource.get("selected", True))
    needed, edges, limitations = None, [], list(_PLAN_LIMITATIONS)
    if family == "dlt_rest":
        if not selected:
            raise ValueError("Select existing read resources.")
        needed, edges = _rest_dependencies(declared, selected)
        status = "declaration_derived"
    else:
        status = "fetch_scope_unknown"
        limitations.append("This connector family has no verified static fetch dependency graph; runtime discovery is not performed.")
    return {"source_type": entry["name"], "execution_family": family,
            "requested_resources": list(resources), "selected_outputs": selected,
            "required_fetch_resources": needed, "dependency_edges": edges,
            "default_selection_used": not resources, "status": status, "limitations": limitations}


def validate_selection(entry, resources, params):
    """Check completeness before the worker can resolve credential values."""
    if entry["execution_family"] == "dlt_filesystem":
        from .filesystem_adapter import validate_filesystem_parameters
        validate_filesystem_parameters(params, resources or None, source_type=entry["name"])
        return
    if entry["execution_family"] != "dlt_rest":
        return
    from .upstream.yaml_source import _apply_parameters
    declared = {resource["name"]: resource for resource in entry["resources"]}
    needed = set(selection_plan(entry, resources)["required_fetch_resources"])
    if set(params) - needed:
        raise ValueError("Parameters must address selected resources or their dependencies.")
    for name in needed:
        _apply_parameters(_resource_endpoint(entry, declared[name]), params.get(name, {}))
