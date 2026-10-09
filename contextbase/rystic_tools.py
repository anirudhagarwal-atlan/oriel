"""Read-only MCP tools for the local Rystic Stripe and Linear simulators.

These tools deliberately expose source queries rather than a precomputed answer.
The local DuckDB and direct-source workflows read the same context artifact.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

from .tools import Toolkit, definition
from .rystic import ISSUE_FIELDS


MAX_QUERY_BYTES = 32_768
MAX_VARIABLE_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_NESTING = 64
RESOURCES = ("customers", "charges", "refunds", "payment_intents")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
_FILTER_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[(?:[A-Za-z0-9_]+)?\]){0,3}\Z")
_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
VERIFICATION_POLICIES = ("per_call", "evaluation_session")

# This is the source contract actually consumed by collect_snapshot, rather than
# an invented exhaustive API schema or a prepared answer. Exposing it gives the
# direct-source agent the same ability to discover fields as DuckDB's catalog.
SOURCE_METADATA = {
    "scope": "Observed fields used by the verified synthetic snapshot adapter; not an exhaustive production API schema.",
    "stripe": {
        "collections": {
            "customers": {"id": "string", "name": "string", "metadata.contextbase_fixture": "string",
                          "metadata.account_id": "string", "metadata.is_internal": "string: true or false"},
            "charges": {"id": "string", "customer": "string: customer id", "amount": "integer cents",
                        "amount_refunded": "integer cents", "currency": "string", "paid": "boolean",
                        "captured": "boolean", "status": "string", "disputed": "boolean",
                        "created": "integer Unix seconds"},
        },
        "other_readable_resources": ["refunds", "payment_intents"],
        "list_response": {"data": "array of objects", "has_more": "boolean"},
        "pagination": "Request limit up to 100; when has_more=true, set starting_after to the final returned object's id and continue until false.",
        "examples": [
            {"tool": "stripe_list", "arguments": {"resource": "customers", "limit": 100}},
            {"tool": "stripe_list", "arguments": {"resource": "charges", "limit": 100}},
        ],
    },
    "linear": {
        "issues_arguments": {"first": "integer page size, observed up to 100", "after": "string cursor or null",
                             "includeArchived": "boolean; true includes archived records as in the workspace snapshot"},
        "issue_fields": {"id": "string", "identifier": "string", "title": "string", "description": "string or null",
                         "priority": "integer", "state.id": "string", "state.name": "string", "state.type": "string",
                         "assignee": "object or null", "assignee.id": "string", "assignee.name": "string", "updatedAt": "string timestamp"},
        "response": {"issues.nodes": "array of selected issues", "issues.pageInfo.hasNextPage": "boolean",
                     "issues.pageInfo.endCursor": "string cursor or null"},
        "pagination": "Use first=100 and includeArchived=true; carry pageInfo.endCursor into after while hasNextPage=true. The tool returns GraphQL data directly, without an additional data wrapper.",
        "example": {"tool": "linear_query", "arguments": {
            "query": "query SourceIssues($after: String) { issues(first: 100, after: $after, includeArchived: true) { nodes { " + ISSUE_FIELDS + " } pageInfo { hasNextPage endCursor } } }",
            "variables": {"after": None},
        }},
    },
}

DEFINITIONS = {
    "sources": definition("sources", "Read source schema metadata, observed pagination/query examples, verified simulator versions, identity verification policy, and simulation limitations. No production credentials or source writes."),
    "context": definition("context", "Read the same approved or draft business context available to the DuckDB workflow. Unresolved rules are not approved definitions."),
    "stripe_list": definition("stripe_list", "Read a Stripe simulator collection with cursor pagination and scalar API query filters. Filter support depends on the simulator; do not assume production Stripe fidelity. Use has_more and the last returned ID to paginate.", {
        "resource": {"type": "string", "enum": list(RESOURCES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "starting_after": {"type": "string", "maxLength": 200},
        "filters": {"type": "object", "description": "Source API query fields with scalar values; bracket keys such as created[gte] are allowed. limit and starting_after are separate arguments."},
    }, ["resource"]),
    "stripe_get": definition("stripe_get", "Retrieve one object from an allowed Stripe simulator resource by its exact ID.", {
        "resource": {"type": "string", "enum": list(RESOURCES)},
        "id": {"type": "string", "maxLength": 200},
    }, ["resource", "id"]),
    "linear_query": definition("linear_query", "Run a read-only GraphQL query against the Linear simulator. Full supported query shapes, aliases, fragments, projection and variables are allowed. Mutations and subscriptions are rejected across the entire document. Rystic 0.0.1 documents issues.filter as ignored; inspect results and apply required predicates yourself. Query limit 32 KiB; variables 64 KiB; response 1 MiB.", {
        "query": {"type": "string", "maxLength": MAX_QUERY_BYTES},
        "variables": {"type": "object"},
    }, ["query"]),
}


def _json_size(value, limit, message):
    """Validate JSON and reject excessive depth before encoding untrusted input."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if depth > MAX_NESTING or nodes > 100_000:
            raise ValueError(message)
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise ValueError(message)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif current is not None and type(current) not in (str, int, float, bool):
            raise ValueError(message)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError(message)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError(message) from None
    if len(encoded) > limit:
        raise ValueError(message)


def _tokens(query):
    """Lex GraphQL without treating comments or quoted text as operations."""
    tokens = []
    position = 0
    while position < len(query):
        char = query[position]
        if char in " \t\r\n,\ufeff":
            position += 1
        elif char == "#":
            end = position + 1
            while end < len(query) and query[end] not in "\r\n":
                end += 1
            position = end
        elif query.startswith('"""', position):
            position += 3
            while position < len(query):
                if query.startswith('\\"""', position):
                    position += 4
                elif query.startswith('"""', position):
                    position += 3
                    tokens.append(("string", ""))
                    break
                else:
                    position += 1
            else:
                raise ValueError("Invalid GraphQL string.")
        elif char == '"':
            position += 1
            while position < len(query):
                char = query[position]
                if char == "\\":
                    position += 2
                elif char == '"':
                    position += 1
                    tokens.append(("string", ""))
                    break
                elif char in "\r\n":
                    raise ValueError("Invalid GraphQL string.")
                else:
                    position += 1
            else:
                raise ValueError("Invalid GraphQL string.")
        elif query.startswith("...", position):
            tokens.append(("punct", "..."))
            position += 3
        elif char in "!$&():=@[]{|}":
            tokens.append(("punct", char))
            position += 1
        else:
            match = _NAME.match(query, position)
            if match:
                tokens.append(("name", match.group()))
            else:
                match = _NUMBER.match(query, position)
                if not match:
                    raise ValueError("Invalid GraphQL document.")
                tokens.append(("number", match.group()))
            position = match.end()
    return tokens


def validate_readonly_graphql(query):
    """Require every executable definition to be a query, including unused ones.

    This is an operation-boundary parser, not a schema validator. The simulator
    remains responsible for validating fields, types and operation selection.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty GraphQL string.")
    try:
        size = len(query.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Invalid GraphQL text.") from None
    if size > MAX_QUERY_BYTES:
        raise ValueError("GraphQL query exceeds the 32 KiB limit.")
    tokens = _tokens(query)
    position, queries = 0, 0

    def at(value):
        return position < len(tokens) and tokens[position][1] == value

    def name():
        nonlocal position
        if position >= len(tokens) or tokens[position][0] != "name":
            raise ValueError("Invalid GraphQL operation header.")
        result = tokens[position][1]
        position += 1
        return result

    def group(opening):
        nonlocal position
        if not at(opening):
            raise ValueError("Invalid GraphQL operation structure.")
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack = []
        while position < len(tokens):
            kind, value = tokens[position]
            position += 1
            if kind != "punct":
                continue
            if value in pairs:
                stack.append(pairs[value])
                if len(stack) > MAX_NESTING:
                    raise ValueError("GraphQL nesting exceeds the allowed limit.")
            elif value in pairs.values():
                if not stack or stack.pop() != value:
                    raise ValueError("Invalid GraphQL operation structure.")
                if not stack:
                    return
        raise ValueError("Incomplete GraphQL operation.")

    def directives():
        nonlocal position
        while at("@"):
            position += 1
            name()
            if at("("):
                group("(")

    while position < len(tokens):
        if at("{"):
            group("{")
            queries += 1
            continue
        operation = name()
        if operation in ("mutation", "subscription"):
            raise ValueError("Only read-only GraphQL queries are allowed; mutations and subscriptions are disabled.")
        if operation == "query":
            if position < len(tokens) and tokens[position][0] == "name":
                name()
            if at("("):
                group("(")
            directives()
            group("{")
            queries += 1
        elif operation == "fragment":
            fragment_name = name()
            if fragment_name == "on" or name() != "on":
                raise ValueError("Invalid GraphQL fragment header.")
            name()
            directives()
            group("{")
        else:
            raise ValueError("Only query and fragment definitions are allowed.")
    if not queries:
        raise ValueError("The GraphQL document must contain a query.")


class RysticToolkit:
    """Drop-in toolkit for the existing MCP JSON-RPC dispatcher."""

    def __init__(self, workspace, pair, *, verification_policy="per_call"):
        if verification_policy not in VERIFICATION_POLICIES:
            raise ValueError("verification_policy must be per_call or evaluation_session.")
        self.workspace = workspace
        self.pair = pair
        self.verification_policy = verification_policy
        self._verified_identity = None
        if verification_policy == "evaluation_session":
            # A frozen evaluation validates complete source state before and after
            # its trials. Charge startup identity reads once, within trial time;
            # never cache API payloads or avoid the runner's state validation.
            try:
                self._verified_identity = deepcopy(self.pair.verify())
            except Exception:
                raise ValueError("Simulator request failed. Check local simulator availability and supported query fields.") from None

    def definitions(self):
        from .refresh_state import REFRESH_STATUS_TOOL
        return [*DEFINITIONS.values(), deepcopy(REFRESH_STATUS_TOOL)]

    def _source_call(self, operation):
        try:
            verified = (self.pair.verify() if self._verified_identity is None else deepcopy(self._verified_identity))
            result = operation(verified)
        except Exception:
            raise ValueError("Simulator request failed. Check local simulator availability and supported query fields.") from None
        _json_size(result, MAX_RESPONSE_BYTES, "Simulator response exceeds the JSON, nesting or 1 MiB limit; use pagination and narrower projection.")
        return result

    def call(self, name, arguments=None):
        if name == "refresh_status":
            if arguments is not None and (not isinstance(arguments, dict) or arguments):
                raise ValueError("The refresh_status tool accepts no arguments.")
            from .refresh_state import refresh_status
            return refresh_status(self.workspace)
        if not isinstance(name, str) or name not in DEFINITIONS:
            raise ValueError("Unknown simulator tool.")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        schema = DEFINITIONS[name]["inputSchema"]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            raise ValueError("Unexpected or missing tool arguments.")
        if name == "context":
            return Toolkit(self.workspace).call("context")
        if name == "sources":
            return self._source_call(lambda verified: {
                "kind": "local_rystic_simulators", "sources": verified,
                "source_writes": "disabled", "production_fidelity": "not_established",
                "metadata": deepcopy(SOURCE_METADATA),
                "identity_verification": {
                    "policy": self.verification_policy,
                    "scope": ("Before every source tool call" if self.verification_policy == "per_call"
                              else "Once at server startup, included in evaluation trial time"),
                    "evaluation_requirement": "Quiescent simulator state with complete preflight and postflight identity/data checks; identity caching does not establish source freshness.",
                    "source_data_cached": False,
                },
                "limitations": [
                    "Synthetic development data; this is not production Stripe or Linear.",
                    "Simulator responses and filter semantics may differ from the real APIs; unsupported behavior is not a product benchmark result.",
                    "Rystic Linear 0.0.1 documents issues.filter as ignored. Inspect returned data and apply necessary predicates explicitly.",
                    "Stripe scalar filters are passed to the simulator; individual filter behavior must be verified before relying on it.",
                ],
            })
        if name == "linear_query":
            query = arguments["query"]
            validate_readonly_graphql(query)
            variables = arguments.get("variables", {})
            if not isinstance(variables, dict):
                raise ValueError("GraphQL variables must be an object.")
            _json_size(variables, MAX_VARIABLE_BYTES, "GraphQL variables must be finite JSON within the nesting and 64 KiB limits.")
            return self._source_call(lambda _: self.pair.linear.graphql(query, variables=variables))
        resource = arguments["resource"]
        if not isinstance(resource, str) or resource not in RESOURCES:
            raise ValueError("Unsupported Stripe resource.")
        if name == "stripe_get":
            object_id = arguments["id"]
            if not isinstance(object_id, str) or not _IDENTIFIER.fullmatch(object_id):
                raise ValueError("Stripe object ID has an invalid format.")
            return self._source_call(lambda _: self.pair.stripe.request("GET", f"/v1/{resource}/{object_id}"))
        limit = arguments.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100.")
        starting_after = arguments.get("starting_after")
        if "starting_after" in arguments and (not isinstance(starting_after, str) or not _IDENTIFIER.fullmatch(starting_after)):
            raise ValueError("starting_after must be a valid Stripe object ID.")
        filters = arguments.get("filters", {})
        if not isinstance(filters, dict) or len(filters) > 32:
            raise ValueError("filters must be an object with at most 32 scalar query fields.")
        for key, value in filters.items():
            if not isinstance(key, str) or len(key) > 128 or not _FILTER_KEY.fullmatch(key):
                raise ValueError("Stripe filter names must be simple API query fields.")
            if key in ("limit", "starting_after"):
                raise ValueError("Pass limit and starting_after as tool arguments, not filters.")
            if type(value) not in (str, int, float, bool) or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("Stripe filter values must be finite string, number or boolean scalars.")
            if isinstance(value, str) and len(value) > 4096:
                raise ValueError("Stripe filter value exceeds the allowed length.")
        _json_size(filters, MAX_VARIABLE_BYTES, "Stripe filters exceed the 64 KiB limit.")
        if starting_after and "ending_before" in filters:
            raise ValueError("Use one Stripe pagination direction per request.")
        query = {**filters, "limit": limit}
        if starting_after:
            query["starting_after"] = starting_after
        return self._source_call(lambda _: self.pair.stripe.request("GET", f"/v1/{resource}", query=query))
