"""An equal policy-only context view for the controlled Rystic evaluation.

Product context and its review evidence remain unchanged on disk. Evaluations
receive the approved recipe's fixed policy plus version identifiers, without
per-fixture examples, observed answers, or execution-result hashes. The view
does not establish that an agent queried fresh data; the runner must report
that limitation separately from answer correctness.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .context import load_context
from .rystic_recipe import NET_EXPOSED_SQL
from .rystic_workflow import METRIC_NAME, RULES

VIEW_SPEC = "contextbase.evaluation-context/v1"
CONTEXT_DESCRIPTION = (
    "Read the approved synthetic recipe policy, exact SQL and context/metric "
    "versions. This evaluation view omits examples, observed results and "
    "execution-result hashes equally for both arms. It does not establish "
    "current source freshness or provide a previously computed answer."
)


def context_view(workspace):
    """Validate the approved recipe and return only explicitly allowed fields."""
    context = load_context(workspace)
    metrics = context.get("metrics")
    # Early approved contexts retained synthetic=False at the container level
    # even when the single reviewed recipe metric was synthetic. The view's
    # label describes this fixed policy, not permission to send source data.
    # The runner separately validates the actual synthetic snapshot and consent.
    if (type(context.get("synthetic")) is not bool or context.get("status") != "approved"
            or not isinstance(metrics, list) or len(metrics) != 1):
        raise ValueError("Evaluation context requires the single approved synthetic Rystic recipe.")
    metric = metrics[0]
    if not isinstance(metric, dict):
        raise ValueError("Evaluation context requires the single approved synthetic Rystic recipe.")
    sql = metric.get("sql")
    if (metric.get("name") != METRIC_NAME or metric.get("status") != "approved"
            or metric.get("synthetic") is not True or metric.get("approval_kind") != "local_operator_review"
            or metric.get("rules") != RULES or not isinstance(sql, str)
            or sql.strip().removesuffix(";").rstrip() != NET_EXPOSED_SQL.strip()):
        raise ValueError("Evaluation context must use the exact reviewed Rystic recipe SQL and rules.")
    context_version = context.get("context_version")
    if not isinstance(context_version, str) or not re.fullmatch(r"local-[a-f0-9]{16}", context_version):
        raise ValueError("Evaluation context requires a locally approved context version identifier.")
    version = metric.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("Evaluation context requires a positive integer metric version.")
    return {
        "spec_version": VIEW_SPEC,
        "context_version": context_version,
        "status": "approved",
        "synthetic": True,
        "approval_kind": "local_operator_review",
        "policy_source": "Explicitly authored and reviewed synthetic Rystic recipe rules.",
        "observation_policy": "Fixture observations, examples and execution-result hashes are omitted; source freshness is not asserted.",
        "metrics": [{"name": METRIC_NAME, "version": version, "status": "approved", "synthetic": True,
                     "sql": NET_EXPOSED_SQL, "rules": list(RULES)}],
    }


class EvaluationContextToolkit:
    """Give both arms equal policy and refresh evidence; block stale SQL reads."""

    def __init__(self, workspace, base_toolkit):
        self.workspace = workspace
        self.base_toolkit = base_toolkit
        context_view(workspace)  # Fail before serving a malformed evaluation policy.
        if "context" not in {item.get("name") for item in base_toolkit.definitions()}:
            raise ValueError("Evaluation context requires a toolkit that already exposes context.")

    def definitions(self):
        from .refresh_state import REFRESH_STATUS_TOOL
        definitions = deepcopy(self.base_toolkit.definitions())
        for index, item in enumerate(definitions):
            if item.get("name") == "context":
                item["description"] = CONTEXT_DESCRIPTION
            elif item.get("name") == "refresh_status":
                definitions[index] = deepcopy(REFRESH_STATUS_TOOL)
        if not any(item.get("name") == "refresh_status" for item in definitions):
            definitions.append(deepcopy(REFRESH_STATUS_TOOL))
        return definitions

    def call(self, name, arguments=None):
        if name == "refresh_status":
            if arguments is not None and (not isinstance(arguments, dict) or arguments):
                raise ValueError("The refresh_status tool accepts no arguments.")
            from .refresh_state import refresh_status
            return refresh_status(self.workspace)
        if name == "context":
            if arguments is not None and (not isinstance(arguments, dict) or arguments):
                raise ValueError("The context tool accepts no arguments.")
            return context_view(self.workspace)
        if isinstance(name, str) and name in {"catalog", "query", "list_records", "metric"}:
            from .refresh_state import require_readable_snapshot
            require_readable_snapshot(self.workspace)
        return self.base_toolkit.call(name, arguments)
