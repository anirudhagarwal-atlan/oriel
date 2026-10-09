"""GraphQL read resources with bounded Relay and declared page pagination.

Supports any GraphQL API that uses the Relay connection pattern
(nodes/pageInfo/hasNextPage/endCursor). Each resource config specifies
a query with a $cursor variable, a data_path to extract results, and
a cursor_path to find pagination info.
"""

from __future__ import annotations

from typing import Any, Iterable
import re

import dlt
import requests
from dlt.sources import DltResource
from contextbase.connectors.upstream.yaml_source import _buffer_response


def _traverse(obj: dict, dotted_path: str) -> Any:
    """Navigate a nested dict by dot-separated keys.

    >>> _traverse({"a": {"b": [1, 2]}}, "a.b")
    [1, 2]
    """
    for key in dotted_path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)  # type: ignore[assignment]
    return obj


MAX_PAGES = 10_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _paginate(
    endpoint: str,
    headers: dict[str, str],
    query: str,
    variables: dict[str, Any],
    data_path: str,
    cursor_path: str | None,
    *,
    max_pages: int = MAX_PAGES,
) -> Iterable[dict]:
    """Read complete Relay pages, failing closed on malformed continuation."""
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("GraphQL page bound is invalid.")
    operation = re.sub(r"(?m)^\s*#.*$", "", query).lstrip()
    if not (operation.startswith("{") or re.match(r"query(?:\s|\(|\{)", operation)):
        raise ValueError("Only a declared GraphQL query can be extracted.")
    if "cursor" in variables:
        raise ValueError("GraphQL pagination owns its cursor variable.")
    cursor = None
    seen = set()
    for page in range(max_pages):
        request_variables = dict(variables)
        if cursor is not None:
            request_variables["cursor"] = cursor
        body = _request(endpoint, headers, query, request_variables)
        nodes = _traverse(body, data_path)
        if nodes is None:
            raise RuntimeError("GraphQL source omitted the declared data path.")
        if isinstance(nodes, dict) and not cursor_path:
            nodes = [nodes]
        _records(nodes)
        more = False
        next_cursor = None
        if cursor_path:
            info = _traverse(body, cursor_path)
            if not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
                raise RuntimeError("GraphQL source omitted pagination completion evidence.")
            more = info["hasNextPage"]
            if more:
                next_cursor = info.get("endCursor")
                if not nodes or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                    raise RuntimeError("GraphQL source has incomplete or repeated pagination.")
                if page + 1 >= max_pages:
                    raise RuntimeError("GraphQL pagination exceeded the completeness bound.")
                seen.add(next_cursor)
        yield from nodes
        if not more:
            return
        cursor = next_cursor


def _request(endpoint, headers, query, variables):
    operation = re.sub(r"(?m)^\s*#.*$", "", query).lstrip()
    if not (operation.startswith("{") or re.match(r"query(?:\s|\(|\{)", operation)):
        raise ValueError("Only a declared GraphQL query can be extracted.")
    response = requests.post(endpoint, json={"query": query, "variables": variables},
                             headers=headers, timeout=60, allow_redirects=False, stream=True)
    try:
        if not 200 <= response.status_code < 300:
            raise RuntimeError("GraphQL source returned a non-success status.")
        try:
            _buffer_response(response, MAX_RESPONSE_BYTES)
        except ValueError:
            raise RuntimeError("GraphQL source response exceeded the extraction bound.") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("GraphQL source returned invalid JSON.") from None
        if not isinstance(body, dict) or body.get("errors"):
            raise RuntimeError("GraphQL source returned errors or an invalid envelope.")
        return body
    finally:
        response.close()


def _records(nodes, limit=None):
    if (not isinstance(nodes, list) or any(not isinstance(item, dict) for item in nodes)
            or (limit is not None and len(nodes) > limit)):
        raise RuntimeError("GraphQL source returned invalid records.")


def _page_limit(query, data_path):
    """Recognize the catalog's explicit page/limit contract, never infer a limit."""
    root = data_path.removeprefix("data.")
    if root not in {"workspaces", "boards", "updates", "users"}:
        raise ValueError("GraphQL page pagination is not declared for this resource.")
    match = re.search(r"\b" + root + r"\(\s*limit:\s*(\d+),\s*page:\s*\$page(?:\s*[,\)])", query)
    if not match or not 1 <= int(match[1]) <= 1000:
        raise ValueError("GraphQL page pagination requires a declared bounded limit.")
    return int(match[1])


def _unique_records(nodes, seen):
    for node in nodes:
        identity = node.get("id")
        if (not isinstance(identity, (str, int)) or isinstance(identity, bool)
                or identity == "" or str(identity) in seen):
            raise RuntimeError("GraphQL page records have missing or repeated identity.")
        seen.add(str(identity))


def _paginate_pages(endpoint, headers, query, variables, data_path, *, budget=None):
    if "page" in variables or "cursor" in variables:
        raise ValueError("GraphQL pagination owns its page and cursor variables.")
    limit = _page_limit(query, data_path)
    remaining = budget if budget is not None else [MAX_PAGES]
    page = 1
    seen = set()
    while True:
        body = _bounded_request(endpoint, headers, query, {**variables, "page": page}, remaining)
        nodes = _traverse(body, data_path)
        _records(nodes, limit)
        _unique_records(nodes, seen)
        if len(nodes) == limit and remaining[0] <= 0:
            raise RuntimeError("GraphQL pagination exceeded the completeness bound.")
        yield from nodes
        if len(nodes) < limit:
            return
        page += 1


def _bounded_request(endpoint, headers, query, variables, remaining):
    if remaining[0] <= 0:
        raise RuntimeError("GraphQL pagination exceeded the completeness bound.")
    remaining[0] -= 1
    return _request(endpoint, headers, query, variables)


def _item_projection(query):
    match = re.search(r"\bitems\s*\{", query)
    if not match:
        raise ValueError("Board items require an explicit item projection.")
    start = match.end()
    depth = 1
    for index in range(start, len(query)):
        depth += (query[index] == "{") - (query[index] == "}")
        if depth == 0:
            projection = query[start:index]
            # These packaged selections have no literals, directives or aliases.
            # Fail instead of manufacturing a different selection if that changes.
            if not re.fullmatch(r"[\w\s{}]+", projection):
                break
            return projection
    raise ValueError("Board item projection is not supported.")


def _paginate_board_items(endpoint, headers, query, variables, data_path):
    """Flatten initial board items and consume every per-board next_items_page."""
    if data_path != "data.boards":
        raise ValueError("Board items require the declared board data path.")
    match = re.search(r"\bitems_page\(\s*limit:\s*(\d+)\s*\)", query)
    if not match or not 1 <= int(match[1]) <= 1000:
        raise ValueError("Board items require a declared bounded limit.")
    limit = int(match[1])
    projection = _item_projection(query)
    followup = ("query($cursor: String!) { next_items_page(limit: " + str(limit)
                + ", cursor: $cursor) { cursor items {" + projection + "} } }")
    remaining = [MAX_PAGES]
    seen_items = set()
    for board in _paginate_pages(endpoint, headers, query, variables, data_path, budget=remaining):
        page = board.get("items_page")
        cursors = set()
        while True:
            if not isinstance(page, dict) or "cursor" not in page:
                raise RuntimeError("Board items omitted pagination completion evidence.")
            nodes = page.get("items")
            _records(nodes, limit)
            _unique_records(nodes, seen_items)
            if any(not isinstance(node.get("board"), dict) or str(node["board"].get("id")) != str(board["id"]) for node in nodes):
                raise RuntimeError("Board item belongs to a different or missing board.")
            cursor = page["cursor"]
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor or cursor in cursors or not nodes:
                    raise RuntimeError("Board items have incomplete or repeated pagination.")
                if remaining[0] <= 0:
                    raise RuntimeError("GraphQL pagination exceeded the completeness bound.")
                cursors.add(cursor)
            yield from nodes
            if cursor is None:
                break
            body = _bounded_request(endpoint, headers, followup, {"cursor": cursor}, remaining)
            page = _traverse(body, "data.next_items_page")


def _make_resource(
    name: str,
    endpoint: str,
    headers: dict[str, str],
    query: str,
    data_path: str,
    cursor_path: str | None,
    variables: dict[str, Any],
    primary_key: str,
) -> DltResource:
    """Build a dlt resource for a single GraphQL query."""

    def _fetch() -> Iterable[dict]:
        if re.search(r"\$page\s*:\s*Int", query):
            if cursor_path:
                raise ValueError("A resource cannot mix Relay and page pagination.")
            paginate = _paginate_board_items if name == "items" and "items_page" in query else _paginate_pages
            yield from paginate(endpoint, headers, query, variables, data_path)
            return
        yield from _paginate(endpoint, headers, query, variables, data_path, cursor_path)

    return dlt.resource(
        _fetch,
        name=name,
        write_disposition="merge",
        primary_key=primary_key,
    )


@dlt.source
def graphql_source(
    endpoint: str,
    token: str | None = None,
    resources: list[dict[str, Any]] | None = None,
    auth_prefix: str = "Bearer ",
    api_key: str | None = None,
    access_token: str | None = None,
) -> Iterable[DltResource]:
    """Generic GraphQL dlt source.

    Args:
        endpoint: GraphQL endpoint URL.
        token: Auth token. Aliases: ``api_key``, ``access_token``.
        resources: List of resource configs, each with keys:
            name, query, data_path, cursor_path (optional), variables (optional).
        auth_prefix: Prefix for the Authorization header (default "Bearer ").
        api_key: Alias for ``token``.
        access_token: Alias for ``token``.
    """
    token = token or access_token or api_key
    if not token:
        raise ValueError("GraphQL source requires a token, api_key, or access_token")
    if resources is None:
        raise ValueError("GraphQL source requires resources")

    headers = {
        "Authorization": f"{auth_prefix}{token}",
        "Content-Type": "application/json",
    }

    for res_cfg in resources:
        yield _make_resource(
            name=res_cfg["name"],
            endpoint=endpoint,
            headers=headers,
            query=res_cfg["query"],
            data_path=res_cfg["data_path"],
            cursor_path=res_cfg.get("cursor_path"),
            variables=dict(res_cfg.get("variables", {})),
            primary_key=res_cfg.get("primary_key", "id"),
        )
