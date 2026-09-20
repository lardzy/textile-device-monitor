"""Automatic read/compare/resume for exact in-place corrections.

Uses the existing read-only Bridge cache and operation attempts. A failed
attempt remains historical evidence; a new attempt never creates a new record.
"""

from copy import deepcopy
from datetime import datetime, timezone

from app.execution.connector_updates import UPDATE_OPERATION, update_receipt, updated_record_matches
from app.execution.electron_microscopy import request_task_snapshot_refresh
from app.execution.events import append_audit_log
from app.execution.external_operations import lock_operation_context
from app.execution.models import ExecutionExternalOperation, ExecutionTaskSnapshotCache, utcnow


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def recover_connector_updates(db) -> int:
    changed = 0
    rows = (db.query(ExecutionExternalOperation).filter_by(status="reconciliation_required")
            .filter(ExecutionExternalOperation.request_summary["operation_type"].as_string() == UPDATE_OPERATION)
            .order_by(ExecutionExternalOperation.created_at).limit(20).all())
    for candidate in rows:
        if candidate.request_summary.get("operation_type") != UPDATE_OPERATION:
            continue
        run, node_run = lock_operation_context(db, run_id=candidate.run_id,
                                              node_run_id=candidate.node_run_id, skip_locked=True)
        if candidate.run_id is not None and (run is None or node_run is None):
            continue
        operation = (db.query(ExecutionExternalOperation).filter_by(id=candidate.id)
                     .populate_existing().with_for_update(skip_locked=True).one_or_none())
        if operation is None or operation.status != "reconciliation_required":
            continue
        attempt = max(operation.attempts, key=lambda item: item.attempt_no, default=None)
        if attempt is None or attempt.status != "failed":
            continue
        verification = deepcopy(operation.verification or {})
        if run is not None and run.status in {"cancelled", "cancel_pending", "failure_pending", "failed"}:
            verification["cancel_requested"] = True
        state = verification.get("automatic_recovery", {})
        number = operation.request_summary["target_sample_number"]
        if state.get("attempt_id") != attempt.id:
            requested_at = utcnow()
            cache, _ = request_task_snapshot_refresh(db, inspection_number=number, force=True, include_check_records=True)
            verification["automatic_recovery"] = {"attempt_id": attempt.id, "requested_at": requested_at.isoformat(),
                                                   "decision": "reading", "cache_revision": None,
                                                   "read_attempts": 1,
                                                   "wait_for_previous_read": cache.status == "running"}
            operation.verification = verification
            changed += 1
            continue
        cache = db.get(ExecutionTaskSnapshotCache, number)
        if cache is not None and cache.status == "failed" and state.get("cache_revision") != cache.revision:
            state["cache_revision"] = cache.revision
            if state.get("read_attempts", 1) < 3:
                request_task_snapshot_refresh(db, inspection_number=number, force=True, include_check_records=True)
                state.update(read_attempts=state.get("read_attempts", 1) + 1, decision="reading")
            else:
                state["decision"] = "needs_attention"
                operation.error_message = "自动读取检务记录暂未成功；连接恢复后刷新该记录即可继续核对"
            verification["automatic_recovery"] = state
            operation.verification = verification
            changed += 1
            continue
        if state.get("wait_for_previous_read"):
            if cache is not None and cache.status != "running":
                requested_at = utcnow()
                request_task_snapshot_refresh(db, inspection_number=number, force=True, include_check_records=True)
                state.update(requested_at=requested_at.isoformat(), wait_for_previous_read=False)
                verification["automatic_recovery"] = state
                operation.verification = verification
                changed += 1
            continue
        if (cache is None or cache.fetched_at is None or cache.check_records is None
                or cache.status != "ready"
                or _utc(cache.fetched_at) < datetime.fromisoformat(state["requested_at"])
                or state.get("cache_revision") == cache.revision):
            continue
        record = next((r for r in cache.check_records["records"]
                       if r["record_ref"] == operation.request_summary["record_ref"]), None)
        state.update(cache_revision=cache.revision, checked_at=utcnow().isoformat(), observation=deepcopy(record))
        summary = operation.request_summary
        if updated_record_matches(summary, record):
            operation.status = "completed"
            operation.receipt = update_receipt(operation, record)
            operation.remote_record_id = summary["record_ref"]
            operation.completed_at = utcnow()
            operation.error_code = operation.error_message = None
            state["decision"] = "completed_from_readback"
        elif (record and record.get("content_fingerprint") == summary["expected_content_fingerprint"]
              and (operation.attempt_count < 3 or verification.get("cancel_requested"))):
            operation.status = "cancelled" if verification.get("cancel_requested") else "approved"
            operation.error_code = operation.error_message = None
            state["decision"] = "cancelled_without_change" if operation.status == "cancelled" else "retry_unchanged_record"
            if operation.status == "cancelled":
                operation.completed_at = utcnow()
                verification["reconciliation"] = {"action": "confirm_no_side_effect", "source": "automatic_readback"}
        elif (updated_record_matches(summary, record, projection=False) and operation.attempt_count < 3
              and not verification.get("cancel_requested")):
            operation.status = "approved"
            operation.error_code = operation.error_message = None
            state["decision"] = "resume_projection"
        else:
            state["decision"] = "needs_attention"
            operation.error_message = "已自动读取原记录；存在其他修改或已达到重试次数，请查看当前记录后处理"
        verification["automatic_recovery"] = state
        operation.verification = verification
        if node_run is not None and node_run.status == "waiting_external":
            from app.execution.engine import complete_external_node, cancel_external_waiting_node

            if operation.status == "completed":
                complete_external_node(db, node_run_id=node_run.id, output_data={
                    "operation_id": operation.id, "status": "completed", "receipt": operation.receipt,
                })
            elif operation.status == "cancelled":
                cancel_external_waiting_node(db, node_run_id=node_run.id,
                                             error_code="run_cancelled", error_message="已核对原记录未改变")
        append_audit_log(db, action="connector_operation.automatic_recovery",
                         resource_type="execution_external_operation", resource_id=operation.id,
                         details={"attempt_id": attempt.id, "record_ref": summary["record_ref"],
                                  "decision": state["decision"], "cache_revision": cache.revision})
        changed += 1
    return changed
