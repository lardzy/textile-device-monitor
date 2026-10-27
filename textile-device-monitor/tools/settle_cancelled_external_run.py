#!/usr/bin/env python3
"""Close a cancelled Run after an expired external attempt was handled manually.

This maintenance tool performs no remote I/O, upload, retry or deletion. It does
not assert that the original write succeeded or that it never happened. Check
that the original Bridge/Writer has stopped first, and record what was handled
manually in --note. Defaults to preview; --execute commits one audited transaction.

Run with the backend environment/PYTHONPATH and its DATABASE_URL. All four IDs
and the original payload checksum must be supplied explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

backend = Path(__file__).resolve().parents[1] / "backend"
if backend.is_dir():
    sys.path.insert(0, str(backend))

from sqlalchemy.orm import Session

from app.execution.events import append_audit_log, append_run_event
from app.execution.external_operations import (
    ACTIVE_REMOTE_OPERATION_STATUSES,
    _stage_before_remote_write,
    lock_legacy_remote_business_scope,
    lock_operation_context,
)
from app.execution.models import (
    ExecutionEdgeRun,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionHumanTask,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionUser,
    utcnow,
)


ACTION = "manual_handled_cancel"
ERROR_CODE = "external_operation_manually_settled_cancel"
TERMINAL_NODES = {"succeeded", "failed", "skipped", "cancelled"}
TERMINAL_RUNS = {"completed", "failed", "cancelled"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def settle(
    db: Session,
    *,
    run_id: str,
    operation_id: str,
    attempt_id: str,
    payload_checksum: str,
    admin_id: str,
    note: str,
    execute: bool = False,
    writer_stopped: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and optionally stage settlement; caller owns commit/rollback."""
    current_time = now or utcnow()
    note = note.strip()
    require(bool(note), "必须记录人工处理事实及停止原 Writer 的核验依据")
    require(not execute or writer_stopped, "执行前必须核对原 Bridge/Writer 已停止本次操作")
    actor = db.get(ExecutionUser, admin_id)
    require(actor is not None and actor.is_active and actor.role == "admin", "需要有效管理员 ID")
    locator = db.query(ExecutionExternalOperation.run_id, ExecutionExternalOperation.node_run_id).filter(
        ExecutionExternalOperation.id == operation_id,
    ).one_or_none()
    require(locator is not None and locator.run_id == run_id, "操作与指定运行不匹配")
    run, node = lock_operation_context(db, run_id=run_id, node_run_id=locator.node_run_id)
    require(run is not None and node is not None, "运行或等待节点不存在")
    require(run.parent_run_id is None, "仅支持独立根运行；分组运行需单独核对父级收尾")
    target = db.query(ExecutionExternalOperation.request_summary).filter(
        ExecutionExternalOperation.id == operation_id,
    ).scalar()
    target_number = str((target or {}).get("target_sample_number") or "").strip()
    require(bool(target_number), "操作缺少目标编号")
    business_key = lock_legacy_remote_business_scope(db, sample_number=target_number)
    operation = db.query(ExecutionExternalOperation).filter(
        ExecutionExternalOperation.id == operation_id,
    ).populate_existing().with_for_update().one()
    attempt = db.query(ExecutionExternalAttempt).filter(
        ExecutionExternalAttempt.operation_id == operation_id,
    ).order_by(ExecutionExternalAttempt.attempt_no.desc()).populate_existing().with_for_update().first()
    require(operation.remote_business_key == business_key, "目标业务身份不匹配")
    require(operation.connector_key == "legacy_fibrecheck", "仅支持检务连接器")
    require(operation.payload_checksum == payload_checksum, "操作摘要不匹配")
    require(attempt is not None and attempt.id == attempt_id, "执行尝试不是当前最新尝试")
    require(operation.attempt_count == attempt.attempt_no, "执行次数与最新尝试不一致")
    request = {
        "action": ACTION, "run_id": run_id, "operation_id": operation_id,
        "attempt_id": attempt_id, "payload_checksum": payload_checksum,
        "admin_id": admin_id, "note": note,
    }
    request_checksum = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    verification = dict(operation.verification or {})
    previous = verification.get("manual_cancel_settlement")
    if previous is not None:
        require(
            previous.get("request_checksum") == request_checksum
            and run.status == node.status == operation.status == "cancelled",
            "此前已用另一份结论处置，或处置状态不一致",
        )
        return {"execute": execute, "duplicate": True, **request, "status": "cancelled", "remote_write_performed": None}
    require(not verification.get("reconciliation"), "操作已有对账结论，不能覆盖")
    require(run.status == "cancel_pending" and run.finished_at is None, "运行不处于取消中")
    require(node.status == "waiting_external", "节点已不再等待外部操作")
    require(operation.status == "reconciliation_required" and operation.error_code == "lease_expired", "操作不是租约过期后的待核对状态")
    require(attempt.status == "failed" and attempt.error_code == "lease_expired", "尝试不是租约过期失败")
    require(not _stage_before_remote_write(attempt.current_stage, operation), "未越过写入边界的尝试应走正常取消流程")
    require(operation.lease_owner is None and operation.lease_expires_at is None, "操作仍有租约")
    require(attempt.lease_expires_at is None or aware(attempt.lease_expires_at) <= aware(current_time), "尝试租约仍有效")
    require(not node.lease_owner and not node.lease_token and node.lease_expires_at is None, "节点仍有租约")
    require(db.query(ExecutionExternalOperation.id).filter(
        ExecutionExternalOperation.run_id == run_id,
        ExecutionExternalOperation.id != operation_id,
        ExecutionExternalOperation.status.in_(ACTIVE_REMOTE_OPERATION_STATUSES),
    ).first() is None, "运行还有其他未结束外部操作")
    require(db.query(ExecutionRun.id).filter(
        ExecutionRun.parent_run_id == run_id,
        ExecutionRun.status.notin_(TERMINAL_RUNS),
    ).first() is None, "运行还有未结束的子运行")
    nodes = db.query(ExecutionNodeRun).filter(ExecutionNodeRun.run_id == run_id).all()
    require(all(n.id == node.id or n.status in TERMINAL_NODES for n in nodes), "运行还有未结束节点或正在发布的文件")
    require(db.query(ExecutionHumanTask.id).filter(
        ExecutionHumanTask.run_id == run_id,
        ExecutionHumanTask.status.in_({"open", "claimed"}),
    ).first() is None, "运行仍有待处理人工任务")
    edges = db.query(ExecutionEdgeRun).filter(ExecutionEdgeRun.run_id == run_id).all()
    downstream: set[str] = set()
    pending = [node.node_id]
    while pending:
        source = pending.pop()
        for edge in edges:
            if edge.source_node_id == source and edge.target_node_id not in downstream:
                downstream.add(edge.target_node_id)
                pending.append(edge.target_node_id)
    require(all(n.status == "cancelled" for n in nodes if n.node_id in downstream), "下游节点并非全部已取消")
    node_attempts = db.query(ExecutionNodeAttempt).filter(
        ExecutionNodeAttempt.node_run_id == node.id,
    ).populate_existing().with_for_update().all()
    require(all(a.status in TERMINAL_NODES | {"waiting_external"} for a in node_attempts), "节点存在活跃执行尝试")
    before = {"run": run.status, "node": node.status, "operation": operation.status, "attempt": attempt.status}
    report = {
        "execute": execute, "duplicate": False, **request,
        "target_sample_number": target_number, "before": before,
        "after": {"run": "cancelled", "node": "cancelled", "operation": "cancelled", "attempt": attempt.status},
        "remote_write_performed": None, "remote_io_performed": False,
    }
    if not execute:
        return report
    resolution = {
        **request, "request_checksum": request_checksum, "settled_at": current_time.isoformat(),
        "target_sample_number": target_number, "remote_write_performed": None,
        "original_result": "unknown_after_manual_handling", "writer_stopped_attested": True,
        "previous_status": before, "previous_error_code": operation.error_code,
        "previous_error_message": operation.error_message,
    }
    verification["manual_cancel_settlement"] = resolution
    operation.verification = verification
    operation.status = "cancelled"
    operation.completed_at = current_time
    operation.fence_token = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.error_code = ERROR_CODE
    operation.error_message = "管理员已人工处理并结束取消；原自动写入结果未作成功或未写入认定"
    node.status = "cancelled"
    node.finished_at = current_time
    node.error_code = ERROR_CODE
    node.error_message = operation.error_message
    node.output_data = {**(node.output_data or {}), "status": "cancelled", "remote_write_performed": None, "manual_cancel_settlement": resolution}
    for item in node_attempts:
        if item.status == "waiting_external":
            item.status = "cancelled"
            item.finished_at = current_time
            item.error_code = ERROR_CODE
            item.error_message = operation.error_message
    run.status = "cancelled"
    run.finished_at = current_time
    run.output_data = {**(run.output_data or {}), "manual_cancel_settlement": resolution}
    for event_type in ("external_operation.manually_cancelled", "node.cancelled", "run.cancelled"):
        append_run_event(db, run_id=run_id, event_type=event_type, actor_type="user", actor_id=admin_id, payload={
            "operation_id": operation_id, "attempt_id": attempt_id, "node_id": node.node_id,
            "status": "cancelled", "manual_handled": True, "remote_write_performed": None,
            "side_effect_completed": None, "note": note, "request_checksum": request_checksum,
        })
    append_audit_log(db, action="external_operation.manual_cancel_settlement", resource_type="execution_external_operation",
                     resource_id=operation_id, actor_user_id=admin_id, details=resolution)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run-id", "operation-id", "attempt-id", "payload-checksum", "admin-id", "note"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--writer-stopped", action="store_true", help="确认原 Writer 已停止处理本次操作")
    args = parser.parse_args()
    from app.database import engine

    with Session(engine, autoflush=False) as db:
        try:
            result = settle(db, **vars(args))
            if args.execute:
                db.commit()
            else:
                db.rollback()
        except ValueError as exc:
            db.rollback()
            parser.exit(2, f"拒绝处置：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
