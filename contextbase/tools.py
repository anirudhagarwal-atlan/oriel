"""Identical tool handlers for CLI, MCP, and controlled benchmark arms."""
from __future__ import annotations

import json

from .workspace import Workspace, identifier, table_identifier
from .refresh_state import REFRESH_STATUS_TOOL


def definition(name, description, properties=None, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}}


DEFINITIONS = {
    "refresh_status": REFRESH_STATUS_TOOL,
    "catalog": definition("catalog", "List tables, every column and type, and snapshot row counts."),
    "context": definition("context", "Read approved business rules, time boundaries, metric definitions and provenance. Returns unresolved questions when no approved context exists."),
    "query": definition("query", "Run one read-only DuckDB SELECT over the local snapshot. No filesystem or network access. Use aggregations to avoid truncation.",
                        {"sql": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["sql"]),
    "metric": definition("metric", "Execute an approved versioned metric. Available in the demo: active_arr, eligible_pipeline, at_risk_arr, active_accounts.",
                         {"name": {"type": "string"}}, ["name"]),
    "list_records": definition("list_records", "Read one source table with equality filters, column projection and offset pagination. next_offset is null on the last page. All filters combine with AND.",
                               {"table": {"type": "string"}, "filters": {"type": "object"},
                                "columns": {"type": "array", "items": {"type": "string"}},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                "offset": {"type": "integer", "minimum": 0}}, ["table"]),
}

ARMS = {"per_source": ("catalog", "context", "list_records", "refresh_status"),
        "sql": ("catalog", "query", "refresh_status"),
        "sql_context": ("catalog", "context", "query", "refresh_status"),
        "metrics": ("catalog", "context", "query", "metric", "refresh_status")}


class Toolkit:
    def __init__(self, workspace: Workspace, arm="metrics"):
        if arm not in ARMS:
            raise ValueError("Unknown tool arm.")
        self.workspace = workspace
        self.arm = arm

    def definitions(self):
        return [DEFINITIONS[name] for name in ARMS[self.arm]]

    def call(self, name: str, arguments: dict | None = None):
        if name not in ARMS[self.arm]:
            raise ValueError("Tool is not available in this arm.")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        schema = DEFINITIONS[name]["inputSchema"]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            raise ValueError("Unexpected or missing tool arguments.")
        if name == "refresh_status":
            from .refresh_state import refresh_status
            return refresh_status(self.workspace)
        if name == "catalog":
            return {"tables": self.workspace.catalog(), "freshness": "local_snapshot"}
        if name == "context":
            from .context import load_context
            if (self.workspace.root / "context" / "approved.json").exists():
                return load_context(self.workspace)
            if (self.workspace.root / "context" / "draft.json").exists():
                return load_context(self.workspace, approved=False)
            return {"status": "unconfigured", "metrics": [], "unresolved_questions": [
                "No business definitions are approved. Run context bootstrap, propose metric SQL, and request owner review before governed answers."]}
        if name == "query":
            return self.workspace.query(**arguments)
        if name == "metric":
            from .context import run_metric
            return run_metric(self.workspace, arguments["name"])
        return self.list_records(**arguments)

    def list_records(self, table, filters=None, columns=None, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset a nonnegative integer.")
        catalog = {t["schema"] + "." + t["table"]: t for t in self.workspace.catalog()}
        if table not in catalog:
            raise ValueError("Unknown source table.")
        available = [c["name"] for c in catalog[table]["columns"]]
        filters = {} if filters is None else filters
        columns = available if columns is None else columns
        if not isinstance(filters, dict) or not isinstance(columns, list) or not columns:
            raise ValueError("filters must be an object; columns must be a nonempty list.")
        if any(not isinstance(c, str) or c not in available for c in columns) or any(c not in available for c in filters):
            raise ValueError("Unknown column.")
        if any(not isinstance(v, (str, int, float, bool, type(None))) for v in filters.values()):
            raise ValueError("Filter values must be JSON scalars.")
        # These columns are verified against the catalog. Quoting supports real
        # source headers such as _dlt_id and Account Name without SQL injection.
        quote = lambda column: '"' + column.replace('"', '""') + '"'
        predicates = [quote(k) + (" IS NULL" if v is None else " = ?") for k, v in filters.items()]
        params = [v for v in filters.values() if v is not None]
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        # Order by the complete source row for stable pagination, including projected reads.
        order = ",".join(quote(c) for c in available)
        sql = f"SELECT {','.join(quote(c) for c in columns)} FROM {table_identifier(table)}{where} ORDER BY {order} LIMIT ? OFFSET ?"
        result = self.workspace.query(sql, max_rows=limit + 1, parameters=[*params, limit + 1, offset])
        has_more = len(result["rows"]) > limit
        rows = result["rows"][:limit]
        return {"table": table, "rows": rows, "row_count": len(rows),
                "next_offset": offset + limit if has_more else None, "freshness": "local_snapshot"}
