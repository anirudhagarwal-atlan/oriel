"""Noncredential source parameters exposed by the local connector workflow."""

import json
import re

_RESERVED_PARAMETER = re.compile(r"token|secret|password|credential|authorization|api.?key", re.I)
_ROUTING = {"query", "mutation", "script", "code", "path", "url", "endpoint", "base_url", "method", "headers", "auth", "resources"}


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
    selected = resources or [name for name, resource in declared.items() if resource["selected"]]
    needed = set()

    def include(name, stack):
        if name not in declared or name in stack:
            raise ValueError("Invalid or cyclic resource selection.")
        needed.add(name)
        parent = declared[name]["declared"].get("parent")
        parent = parent.get("resource") if isinstance(parent, dict) else parent
        if parent:
            include(parent, stack | {name})
    for name in selected:
        include(name, set())
    if set(params) - needed:
        raise ValueError("Parameters must address selected resources or their dependencies.")
    for name in needed:
        _apply_parameters(_resource_endpoint(entry, declared[name]), params.get(name, {}))
