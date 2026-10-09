"""Tool connectors backed by a pinned, attributed upstream catalog."""

import json
from pathlib import Path


def catalog():
    return json.loads(Path(__file__).with_name("catalog.json").read_text())


def describe(source_type):
    from .policy import parameter_names, resource_parameter_schema
    for entry in catalog()["connectors"]:
        if entry["name"] == source_type:
            return {**entry, "parameter_names": parameter_names(entry), "resource_parameters": resource_parameter_schema(entry)}
    raise ValueError("Unknown connector. Run 'oriel source list --available'.")


def available():
    value = catalog()
    return {"count": value["connector_count"], "connectors": [
        {key: entry[key] for key in ("name", "description", "execution_family", "live_validated")}
        for entry in value["connectors"]], "upstream": value["upstream"]}
