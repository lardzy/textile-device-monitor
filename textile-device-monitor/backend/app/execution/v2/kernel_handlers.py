"""Small, versioned kernel handler surface included in Pack digests.

Release validation, API code, worker claiming and the engine dispatcher are
deliberately outside this module.  Pack implementation digests therefore move
only when executable node semantics move.
"""

from __future__ import annotations

from typing import Any

from app.execution.errors import ExecutionApiError


def execute_compat_builtin(context: Any) -> dict[str, Any]:
    node_type = context.node_run.node_type
    if node_type in {
        "core.start",
        "parallel.split",
        "parallel.join",
        "branch.condition",
        "result.aggregate",
        "core.end",
    }:
        return context.input_data
    if node_type == "variables.set":
        # variables.set remains on its established engine path because its
        # mapping resolver is part of the frozen v1 runtime contract.
        resolver = getattr(context, "resolve_run_value", None)
        if resolver is None:
            raise ExecutionApiError(
                503,
                "node_capability_unavailable",
                "variables.set 缺少 v1 mapping resolver",
            )
        values = resolver((context.node.get("config") or {}).get("values") or {})
        return {"globals": values}
    raise ExecutionApiError(
        503,
        "node_capability_unavailable",
        f"节点“{context.node_run.node_name}”尚未配置运行适配器",
        details={
            "node_id": context.node_run.node_id,
            "node_type": context.node_run.node_type,
        },
    )
