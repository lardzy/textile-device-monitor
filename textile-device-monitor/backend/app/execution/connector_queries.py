"""Registered read services shared by direct callers and workflow nodes.

Queries return immediately. Cache refresh uses the existing read-only Bridge
queue; it neither creates a Run nor suspends a workflow node.
"""

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.execution.electron_microscopy import (
    cached_task_snapshot,
    request_task_snapshot_refresh,
)
from app.execution.errors import ExecutionApiError

if TYPE_CHECKING:
    from app.execution.v2.registry import InstalledQuery


@dataclass(frozen=True)
class QueryResult:
    data: dict[str, Any]
    refresh_request: dict[str, Any] | None = None


def _task_snapshot(db: Session, data: dict[str, Any]) -> QueryResult:
    number = data["inspection_number"].strip().upper()
    if data.get("refresh"):
        request_task_snapshot_refresh(db, inspection_number=number, force=True)
    cached = cached_task_snapshot(db, inspection_number=number)
    result = {
        "inspection_number": number,
        "cache_state": cached["cache_state"],
        "refresh_status": cached["refresh_status"],
        "snapshot": deepcopy(cached["snapshot"]),
        "revision": cached.get("revision"),
        "fetched_at": cached.get("fetched_at"),
        "expires_at": cached.get("expires_at"),
        "error_code": cached.get("error_code"),
    }
    return QueryResult(result, {
        "inspection_number": number,
        "status_url": f"/api/execution/v1/task-snapshots/{number}/status",
    } if cached["refresh_status"] in {"queued", "running"} else None)


def query_handler(connector_id: str, query: str, contract_version: int):
    from app.execution.connector_records import get_record, list_records
    # Only trusted installed code supplies handlers, never a Release/HTTP URL.
    return {
        ("legacy_fibrecheck", "task_snapshot.get", 1): _task_snapshot,
        ("legacy_fibrecheck", "check_record.list", 1): list_records,
        ("legacy_fibrecheck", "check_record.get", 1): get_record,
    }.get((connector_id, query, contract_version))


def resolve_query(query_ref: str, *, connector_version: str = "*", contract_digest: str | None = None):
    from app.execution.v2.registry import get_installed_registry, resolve_connector_reference

    registry = get_installed_registry()
    try:
        connector_id, name, version = resolve_connector_reference(
            query_ref, (item.connector_id for item in registry.connectors.all()),
        )
        query = registry.connectors.resolve_query(
            connector_id, connector_version, name, version, contract_digest,
        )
    except (LookupError, ValueError) as exc:
        raise ExecutionApiError(422, "connector_query_unavailable", "查询未安装或版本不匹配",
                                details={"query_ref": query_ref}) from exc
    if not query.ready:
        raise ExecutionApiError(503, "connector_query_unavailable", "查询实现尚不可用")
    return query


def execute_query(db: Session, *, query: "InstalledQuery", input_data: dict[str, Any]) -> QueryResult:
    if not query.ready or query.handler is None:
        raise ExecutionApiError(503, "connector_query_unavailable", "查询实现尚不可用")
    error = next(Draft202012Validator(query.spec["input_schema"]).iter_errors(input_data), None)
    if error is not None:
        raise ExecutionApiError(422, "connector_query_input_invalid", "查询参数不符合契约",
                                details={"path": list(error.absolute_path), "message": error.message})
    result = query.handler(db, input_data)
    if not Draft202012Validator(query.spec["output_schema"]).is_valid(result.data):
        raise ExecutionApiError(502, "connector_query_result_invalid", "查询结果不符合契约")
    return result


def execute_query_node(context, *, query: "InstalledQuery") -> dict[str, Any]:
    if context.node["config"]["query_ref"] != query.query_ref:
        raise ExecutionApiError(409, "connector_query_binding_mismatch", "查询与已发布的契约不一致")
    from app.execution.models import utcnow
    from app.execution.v2.native_handlers import NodeExecutionResult
    config = context.node["config"]
    data = deepcopy(context.input_data)
    waiting = config.get("wait_until_ready", False)
    previous = (context.node_run.output_data or {}).get("_query_wait_started")
    if waiting and previous:
        data["refresh"] = False
    result = query.handler(context.db, data)
    if not waiting or result.refresh_request is None:
        if waiting and result.data.get("error_code"):
            raise ExecutionApiError(422, "connector_query_failed", "检务查询未完成", details=result.data)
        return result.data
    started = datetime.fromisoformat(previous) if previous else utcnow()
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (utcnow() - started).total_seconds() >= config.get("wait_timeout_seconds", 120):
        raise ExecutionApiError(422, "connector_query_timeout", "等待检务查询超时，可重试此节点")
    return NodeExecutionResult(output={**result.data, "_query_wait_started": started.isoformat()}, retry_after_seconds=config.get("retry_interval_seconds", 2))


def connector_capabilities(connector_id: str) -> dict[str, Any]:
    from app.execution.connector_operations import operation_api_available
    from app.execution.v2.registry import get_installed_registry

    try:
        connector = get_installed_registry().resolve_connector(connector_id, "*")
    except LookupError as exc:
        raise ExecutionApiError(404, "connector_not_found", "连接器不存在") from exc
    return {
        **connector.public_dict(),
        "queries": [{**query.public_dict(), "direct_api_available": query.ready}
                    for query in connector.queries],
        "operations": [{**operation.public_dict(), "direct_api_available": operation_api_available(operation),
                        "availability": "direct_api" if operation_api_available(operation) else "native_workflow" if operation.ready else "unavailable"}
                       for operation in connector.operations],
    }
