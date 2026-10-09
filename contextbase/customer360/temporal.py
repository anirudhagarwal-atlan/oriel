"""Bounded pointer selection and exact source timestamp interpretation.

These primitives infer no business meaning or vendor-specific relationships.
JSON fractional numbers decode as Decimal, preserving source precision. Callers
use the workspace's lossless JSON encoding (Decimal as exact strings). A partial
selection is never evidence of absence or of the latest available event.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re


MAX_INPUT_BYTES = 64_000
MAX_OUTPUT_BYTES = 128_000
MAX_DEPTH = 16
MAX_NODES = 10_000
MAX_MATCHES = 10_000
MAX_POINTER_BYTES = 1024
_INDEX = re.compile(r"0|[1-9][0-9]*")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?")
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MIN_SECONDS = -62_135_596_800
_MAX_SECONDS = 253_402_300_799


def validate_pointer(path, wildcard=False):
    """Validate a bounded RFC 6901 pointer and return the original string.

    A standalone ``*`` is an ordinary literal token in a concrete pointer.
    ``wildcard=True`` declares selector syntax, where it expands arrays only.
    Wildcard selectors never match object keys. URI fragments and
    percent-decoding are not supported; ``~0`` and ``~1`` are the only escapes.
    """
    if type(wildcard) is not bool or type(path) is not str:
        raise ValueError("invalid_temporal_pointer")
    try:
        if len(path.encode("utf-8")) > MAX_POINTER_BYTES:
            raise ValueError("invalid_temporal_pointer")
    except UnicodeError:
        raise ValueError("invalid_temporal_pointer") from None
    if path and not path.startswith("/"):
        raise ValueError("invalid_temporal_pointer")
    tokens = path.split("/")[1:] if path else []
    if len(tokens) > MAX_DEPTH or any(re.search(r"~(?![01])", token) for token in tokens):
        raise ValueError("invalid_temporal_pointer")
    return path


def _tokens(path):
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]] if path else []


def _pointer(tokens):
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _encoded(value):
    def exact(item):
        if type(item) is Decimal and item.is_finite():
            return str(item)
        raise ValueError("invalid_json_value")
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=exact).encode("utf-8")


def _tree_check(value):
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_NODES:
            return "node_limit"
        if depth > MAX_DEPTH:
            return "depth_limit"
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                return "invalid_json"
            if len(item) > MAX_NODES - nodes:
                return "node_limit"
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > MAX_NODES - nodes:
                return "node_limit"
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, bool, int, float, Decimal):
            return "invalid_json"
        elif type(item) is float and not math.isfinite(item):
            return "invalid_json"
        elif type(item) is Decimal and not item.is_finite():
            return "invalid_json"
    return None


def _bounded_decoded(value):
    if _tree_check(value) is not None:
        return False
    try:
        return len(_encoded(value)) <= MAX_INPUT_BYTES
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False


def resolve_pointer(value, path):
    """Return ``(found, value)`` without wildcard expansion or coercion.

    Missing paths, non-container traversal, or an oversized/invalid decoded
    document return ``(False, None)``. A present JSON null is ``(True, None)``.
    Array indices are canonical nonnegative decimals (no ``01`` or ``-``).
    """
    validate_pointer(path)
    if not _bounded_decoded(value):
        return False, None
    current = value
    for token in _tokens(path):
        if type(current) is dict:
            if token not in current:
                return False, None
            current = current[token]
        elif type(current) is list:
            if not _INDEX.fullmatch(token) or len(token) > 5 or int(token) >= len(current):
                return False, None
            current = current[int(token)]
        else:
            return False, None
    return True, current


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _no_constant(_value):
    raise ValueError("invalid_json_number")


def _lexical_depth(raw):
    depth, quoted, escaped = 0, False, False
    for character in raw:
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
            if depth > MAX_DEPTH + 1:
                return False
        elif character in "]}":
            depth -= 1
    return True


def select_leaves(raw_text, path_pattern, *, wildcard=True):
    """Select exact scalar leaves and their parents, with completeness evidence.

    Empty path selects raw text, with ``parent=None``. Other paths require
    strict JSON. Any missing branch, wrong traversal type or limit makes the
    result incomplete; a wildcard on an empty array is complete with no leaves.
    Returned parents are bounded source data, never instructions. Large repeated
    parents may reach the total output cap before the match-count limit.
    ``wildcard=False`` selects a concrete RFC 6901 path: ``*`` is a literal
    object key and cannot select array elements.
    """
    validate_pointer(path_pattern, wildcard=wildcard)

    def result(reason, matches=None):
        return {"matches": matches or [], "complete": reason is None, "reason": reason}

    if type(raw_text) is not str:
        return result("invalid_text")
    try:
        if len(raw_text.encode("utf-8")) > MAX_INPUT_BYTES:
            return result("input_limit")
    except UnicodeError:
        return result("invalid_text")
    if path_pattern == "":
        value = result(None, [{"path": "", "value": raw_text, "parent": None}])
        return value if len(_encoded(value)) <= MAX_OUTPUT_BYTES else result("output_limit")
    if not _lexical_depth(raw_text):
        return result("depth_limit")
    try:
        decoded = json.loads(raw_text, object_pairs_hook=_pairs, parse_float=Decimal,
                             parse_constant=_no_constant)
    except (ValueError, TypeError, RecursionError, OverflowError, InvalidOperation):
        return result("invalid_json")
    reason = _tree_check(decoded)
    if reason:
        return result(reason)
    tokens, matches, reason, output_bytes = _tokens(path_pattern), [], None, 128
    pending = [(decoded, None, (), 0)]
    while pending:
        value, parent, path, index = pending.pop()
        if index == len(tokens):
            if type(value) in (dict, list):
                reason = reason or "non_scalar_leaf"
                continue
            match = {"path": _pointer(path), "value": value, "parent": parent}
            try:
                size = len(_encoded(match)) + 1
            except (ValueError, TypeError, RecursionError, OverflowError):
                return result("invalid_json", matches)
            if len(matches) >= MAX_MATCHES:
                return result("match_limit", matches)
            if output_bytes + size > MAX_OUTPUT_BYTES:
                return result("output_limit", matches)
            output_bytes += size
            matches.append(match)
            continue
        token = tokens[index]
        if wildcard and token == "*":
            if type(value) is not list:
                reason = reason or "path_type_mismatch"
            else:
                pending.extend((value[position], value, (*path, str(position)), index + 1)
                               for position in range(len(value) - 1, -1, -1))
        elif type(value) is dict:
            if token not in value:
                reason = reason or "missing_path"
            else:
                pending.append((value[token], value, (*path, token), index + 1))
        elif type(value) is list:
            if not _INDEX.fullmatch(token) or len(token) > 5 or int(token) >= len(value):
                reason = reason or "missing_path"
            else:
                pending.append((value[int(token)], value, (*path, token), index + 1))
        else:
            reason = reason or "path_type_mismatch"
    return result(reason, matches)


def _unknown(reason):
    return {"status": "unknown", "unix_seconds": None, "utc": None, "reason": reason}


def _known(seconds, nanoseconds):
    if not _MIN_SECONDS <= seconds <= _MAX_SECONDS:
        return _unknown("out_of_range")
    value = _EPOCH + timedelta(seconds=seconds)
    fraction = (f"{nanoseconds:09d}".rstrip("0")) if nanoseconds else ""
    utc = (f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
           f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
           + ("." + fraction if fraction else "") + "Z")
    total = seconds * 1_000_000_000 + nanoseconds
    sign = "-" if total < 0 else ""
    integer, remainder = divmod(abs(total), 1_000_000_000)
    exact = sign + str(integer) + ("." + f"{remainder:09d}".rstrip("0") if remainder else "")
    return {"status": "known", "unix_seconds": exact, "utc": utc, "reason": None}


def parse_timestamp(value, encoding):
    """Interpret a declared timestamp format without inferred units/time zones.

    RFC3339 requires an explicit known offset, rejects leap seconds and keeps
    up to nine fraction digits. Unix values accept exact integers, Decimal or
    plain decimal strings, never bool/float; exponent notation in strings is
    not accepted. UTC output is limited to calendar years 0001 through 9999.
    Invalid source values return unknown; an unsupported encoding is a mapping
    contract error and raises ValueError. No input value appears in a reason.
    """
    if type(encoding) is not str or encoding not in {"rfc3339", "unix_seconds"}:
        raise ValueError("unsupported_timestamp_encoding")
    if value is None:
        return _unknown("missing")
    if encoding == "rfc3339":
        if type(value) is not str:
            return _unknown("invalid_type")
        if len(value) > 40:
            return _unknown("invalid_timestamp")
        match = _RFC3339.fullmatch(value)
        if match is None:
            return _unknown("invalid_timestamp")
        year, month, day, hour, minute, second = map(int, match.groups()[:6])
        fraction, offset = match.groups()[6:]
        if offset == "-00:00":
            return _unknown("unknown_offset")
        offset_seconds = 0
        if offset != "Z":
            offset_hour, offset_minute = int(offset[1:3]), int(offset[4:6])
            if offset_hour > 23 or offset_minute > 59:
                return _unknown("invalid_timestamp")
            offset_seconds = (offset_hour * 3600 + offset_minute * 60) * (-1 if offset[0] == "-" else 1)
        try:
            instant = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
            delta = instant - _EPOCH
        except (ValueError, OverflowError):
            return _unknown("invalid_timestamp")
        return _known(delta.days * 86400 + delta.seconds - offset_seconds,
                      int((fraction or "").ljust(9, "0")))
    if type(value) not in (int, Decimal, str):
        return _unknown("invalid_type")
    if type(value) is str:
        if len(value) > 64 or _DECIMAL.fullmatch(value) is None:
            return _unknown("invalid_timestamp")
        number = Decimal(value)
    elif type(value) is int:
        if not _MIN_SECONDS <= value <= _MAX_SECONDS:
            return _unknown("out_of_range")
        number = Decimal(value)
    else:
        number = value
    if not number.is_finite():
        return _unknown("invalid_timestamp")
    if number.as_tuple().exponent < -9:
        return _unknown("unsupported_precision")
    if number < _MIN_SECONDS or number >= Decimal(_MAX_SECONDS + 1):
        return _unknown("out_of_range")
    # Work directly from the decimal tuple so global decimal context precision
    # cannot round either epoch fractions or negative subsecond values.
    parts = number.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if coefficient == 0:
        return _known(0, 0)
    total = coefficient * (10 ** (parts.exponent + 9))
    if parts.sign:
        total = -total
    seconds, nanoseconds = divmod(total, 1_000_000_000)
    return _known(seconds, nanoseconds)
