"""Bounded structural descriptions of already scoped, mapped string values.

Previews are untrusted source data, not citation receipts or instructions. This
module makes no judgment about substance, relevance or event chronology. The
output budget uses ``json.dumps(result, ensure_ascii=True)`` encoded as UTF-8.
"""
from __future__ import annotations

import copy
import json
import math
import re


MAX_FIELDS = 8
MAX_INPUT_FIELDS = 256
MAX_JSON_CHARACTERS = 8192
MAX_DEPTH = 6
MAX_NODES = 128
MAX_ITEMS = 4
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _depth_bound(value):
    depth, quoted, escaped = 0, False, False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("json_depth_limit")
        elif character in "]}":
            depth -= 1


def _pointer(path):
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in path)


def _inspect(value):
    if value == "":
        return "empty", [], [], True
    # Inspect only a bounded prefix even when the original field is enormous.
    stripped = value[:MAX_JSON_CHARACTERS + 1].lstrip()
    if not stripped and len(value) > MAX_JSON_CHARACTERS:
        return "uninspected", [], [], False
    if not stripped.startswith(("{", "[")):
        return "plain_text", [], [], True
    if len(value) > MAX_JSON_CHARACTERS:
        return "uninspected", [], [], False
    try:
        _depth_bound(value)
        parsed = json.loads(value, object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non_json_number")))
        if not isinstance(parsed, (dict, list)):
            raise ValueError("not_a_container")
        leaves, collections, pending = [], [], [((), parsed)]
        count = 0
        while pending:
            path, item = pending.pop()
            count += 1
            if count > MAX_NODES or len(path) > MAX_DEPTH:
                raise ValueError("json_inspection_limit")
            pointer = _pointer(path)
            if isinstance(item, (dict, list)):
                collections.append({"path": pointer, "kind": "object" if isinstance(item, dict) else "array", "length": len(item)})
                children = item.items() if isinstance(item, dict) else enumerate(item)
                pending.extend(((*path, str(key)), child) for key, child in children)
            else:
                if type(item) is float and not math.isfinite(item):
                    raise ValueError("non_finite_json_number")
                kind = "string" if isinstance(item, str) else "null" if item is None else "boolean" if type(item) is bool else "number"
                # Do not turn numeric/boolean/null values into narrative text.
                leaves.append({"path": pointer, "value_type": kind, "preview": item[:80] if kind == "string" else None,
                               "truncated": kind == "string" and len(item) > 80})
        # Structural priority only: shallow scalar paths and arrays are useful
        # to navigate. Neither these orders nor key names establish relevance.
        leaves.sort(key=lambda item: (item["path"].count("/"), item["path"]))
        collections.sort(key=lambda item: (item["kind"] != "array", item["path"].count("/"), item["path"]))
        return "json_object" if isinstance(parsed, dict) else "json_array", leaves, collections, True
    except (ValueError, TypeError, RecursionError, OverflowError):
        return "uninspected", [], [], False


def describe_fields(fields, max_bytes=1200):
    """Describe <=8 mapped strings, text first, within a strict JSON byte cap.

    No additional scope is fetched. Non-string mapped values are not previewed.
    Omitted fields/content remain unknown. Malformed, oversized, excessively
    deep or wide JSON-like strings are uninspected, never treated as empty.
    ``inspection_complete`` means the returned descriptor omitted no content;
    ``truncated`` is its explicit converse, not a semantic quality score.
    """
    if type(fields) is not dict:
        raise ValueError("Supply already scoped mapped fields as an object.")
    if type(max_bytes) is not int or not 256 <= max_bytes <= 65536:
        raise ValueError("Descriptor byte budget must be an integer from 256 to 65536.")
    result = {"fields": [], "field_count": len(fields), "string_field_count": None,
              "excluded_field_count": len(fields), "excluded_content_unknown": bool(fields), "truncated": bool(fields)}
    if len(fields) > MAX_INPUT_FIELDS:
        return result
    strings = [key for key, value in fields.items() if type(value) is str]
    result["string_field_count"] = len(strings)
    selected = sorted((key for key in strings if type(key) is str and _ROLE.fullmatch(key)),
                      key=lambda key: (key != "text", key))[:MAX_FIELDS]
    inspected = {}
    for field in selected:
        value = fields[field]
        form, leaves, collections, complete = _inspect(value)
        descriptor = {"field": field, "format": form, "characters": len(value), "preview": "",
                      "leaves": [], "collections": [], "inspection_complete": False, "truncated": True}
        result["fields"].append(descriptor)
        # The shell reserves space for other fields before any large preview.
        if _size(result) > max_bytes:
            result["fields"].pop()
            continue
        inspected[field] = (leaves, collections, complete)

    # Add navigational data fairly across fields, so an empty updates array is
    # not hidden behind a large primary-text preview.
    for index in range(MAX_ITEMS):
        for descriptor in result["fields"]:
            leaves, collections, _ = inspected[descriptor["field"]]
            for key, available in (("collections", collections), ("leaves", leaves)):
                if index >= len(available):
                    continue
                descriptor[key].append(copy.deepcopy(available[index]))
                if _size(result) > max_bytes:
                    descriptor[key].pop()

    for descriptor in result["fields"]:
        value = fields[descriptor["field"]]
        bound = 96 if descriptor["format"] in {"plain_text", "empty"} else 48
        for length in range(min(bound, len(value)), -1, -1):
            descriptor["preview"] = value[:length]
            if _size(result) <= max_bytes:
                break
        leaves, collections, complete = inspected[descriptor["field"]]
        descriptor["truncated"] = (not complete or len(descriptor["preview"]) != len(value)
                                   or len(descriptor["leaves"]) != len(leaves)
                                   or len(descriptor["collections"]) != len(collections)
                                   or any(item["truncated"] for item in descriptor["leaves"]))
        descriptor["inspection_complete"] = not descriptor["truncated"]
    result["excluded_field_count"] = len(fields) - len(result["fields"])
    result["truncated"] = bool(result["excluded_field_count"] or any(item["truncated"] for item in result["fields"]))
    result["excluded_content_unknown"] = result["truncated"]
    # Final booleans/counters can change the encoding length by a few bytes.
    # Remove previews first; if necessary remove the last descriptor atomically.
    while _size(result) > max_bytes:
        last = result["fields"][-1]
        if last["preview"]:
            last["preview"] = ""
            last["truncated"], last["inspection_complete"] = True, False
        else:
            result["fields"].pop()
            result["excluded_field_count"] += 1
        result["truncated"] = result["excluded_content_unknown"] = True
    return result
