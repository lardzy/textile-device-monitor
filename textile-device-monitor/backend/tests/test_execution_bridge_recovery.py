"""Stopped Writer handoff isolates one operation without blocking the bridge."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.config import settings
from app.execution.external_operations import (
    complete_external_attempt, record_external_attempt_stage, fail_external_attempt,
    BRIDGE_ACTIVE_WRITE_STATUSES,
)
from app.execution.models import ExecutionExternalAttempt, ExecutionExternalOperation, ExecutionTaskSnapshotCache, utcnow
from native_io_helpers import NUMBER
from tests.test_execution_connector_operations import operation_env, submit, claim, receipt
from workflow_native_helpers import environment


@pytest.fixture
def bridge_env(operation_env, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_TOKEN", "test-bridge-key")
    operation_env.client.headers["X-Execution-Bridge-Key"] = "test-bridge-key"
    return operation_env


def recover(env, attempt, operation, *, expected=200, **values):
    response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt.id}/recover", json={
        "bridge_id": "p4-writer", "operation_id": operation.id, "process_exited": True, **values,
    })
    assert response.status_code == expected, response.text
    env.db.expire_all()
    return response.json()


def test_recover_stopped_writer_before_write_fails_without_retry_even_expired(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_write_ready")
    attempt.lease_expires_at = operation.lease_expires_at = utcnow() - timedelta(minutes=5)
    env.db.commit()
    result = recover(env, attempt, operation, error_code="writer_exited", message="Excel unavailable")
    assert result["released"] is True and result["reason"] == "isolated"
    assert result["operation"]["status"] == "failed"
    assert result["operation"]["remote_write_performed"] is False
    assert attempt.status == "failed" and operation.lease_owner is None
    assert operation.attempt_count == 1
    assert recover(env, attempt, operation)["reason"] == "already_settled"
    assert operation.status == "failed"


def test_reconciliation_isolates_sample_but_does_not_occupy_bridge_capacity(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started")
    env.db.commit()
    # A stale local stage must not move a possibly written operation backwards.
    result = recover(env, attempt, operation, stage="authenticated", error_code="writer_crash")
    assert result["operation"]["status"] == "reconciliation_required"
    assert result["attempt"]["current_stage"] == "generic_save_started"
    assert "reconciliation_required" not in BRIDGE_ACTIVE_WRITE_STATUSES
    submit(env, idempotency_key="same-sample-still-fenced", status=409)

    other_number = "26W006702"
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    env.db.add(ExecutionTaskSnapshotCache(inspection_number=other_number, status="ready",
        snapshot={**deepcopy(cache.snapshot), "inspection_number": other_number}, fetched_at=utcnow(), expires_at=utcnow()+timedelta(minutes=15)))
    env.db.commit()
    second = submit(env, idempotency_key="different-number", input={**env.payload["input"], "inspection_number": other_number})["operation"]
    next_operation, next_attempt, _ = claim(env)
    assert next_operation.id == second["id"]
    assert next_attempt.id != attempt.id
    assert operation.status == "reconciliation_required" and operation.attempt_count == 1


def test_existing_reconciliation_releases_pending_and_keeps_full_exception(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started", error_code="lease_expired", message="lease expired")
    env.db.commit()
    details = {"exception_type": "System.Runtime.InteropServices.COMException", "hresult": "0x800A03EC", "stack_trace": "inner failure\n" * 300}
    message = "完整异常\n" * 700
    result = recover(env, attempt, operation, error_code="COMException", message=message, diagnostics=details)
    assert result["released"] and result["reason"] == "already_settled"
    assert result["attempt"]["error"]["message"] == message
    assert result["attempt"]["error"]["diagnostics"] == details
    assert result["operation"]["error"]["diagnostics"] == details
    assert operation.error_message == message
    assert operation.status == "reconciliation_required"
    assert operation.attempt_count == 1


def test_completed_receipt_is_unchanged_by_pending_handoff(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_projection_verified")
    env.db.commit()
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=receipt(operation))
    env.db.commit()
    original = deepcopy(operation.receipt)
    result = recover(env, attempt, operation, error_code="stale_local_marker", message="do not replace successful receipt")
    assert result["released"] and result["reason"] == "already_settled"
    assert operation.status == "completed" and operation.receipt == original
    assert attempt.error_code is None


@pytest.mark.parametrize("change,status", [({"bridge_id": "wrong-bridge"},409), ({"operation_id": "wrong-operation"},409), ({"process_exited": False},422)])
def test_recover_requires_owner_identity_and_stopped_process(bridge_env, change, status):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    recover(env, attempt, operation, expected=status, **change)
    assert operation.status == "in_progress" and attempt.status == "in_progress"


def test_missing_attempt_can_release_marker_but_requires_bridge_authentication(bridge_env):
    env = bridge_env
    path = "/api/execution/v1/external-bridge/attempts/removed-attempt/recover"
    payload = {"bridge_id": "p4-writer", "operation_id": "removed-operation", "process_exited": True}
    response = env.client.post(path, json=payload)
    assert response.status_code == 200 and response.json() == {"released": True, "reason": "not_found", "attempt": None, "operation": None}
    response = env.client.post(path, json=payload, headers={"X-Execution-Bridge-Key": "wrong"})
    assert response.status_code == 401


def test_normal_failure_endpoint_accepts_full_diagnostics(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    message = "System.Runtime.InteropServices.COMException\n" * 100
    diagnostics = {"exception_type": "COMException", "inner_exception": {"message": "Excel collection failed"}, "trace": "original stack"}
    response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt.id}/fail", json={
        "bridge_id": "p4-writer", "stage": "generic_save_started", "error_code": "COMException", "message": message, "diagnostics": diagnostics,
    })
    assert response.status_code == 200, response.text
    assert response.json()["attempt"]["error"] == {"code": "COMException", "message": message, "diagnostics": diagnostics}
    env.db.expire_all()
    assert attempt.error_message == message
    persisted = env.client.get(f"/api/execution/v1/external-operations/{operation.id}")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["error"]["diagnostics"] == diagnostics


@pytest.mark.parametrize("report_path", ["recover", "fail", "cancel_after_fail"])
def test_stopped_unknown_writer_finishes_child_and_parent_cancellation(bridge_env, report_path):
    from app.execution.engine import expire_stale_external_attempts, set_run_control_status
    from app.execution.models import ExecutionCategory, ExecutionNodeRun, ExecutionRun, ExecutionWorkflow, ExecutionWorkflowVersion
    from app.execution.v2.group_handlers import settle_cancelled_batches
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    category = env.db.query(ExecutionCategory).first()
    workflow = ExecutionWorkflow(slug="recover-group", name="Recovery", category_id=category.id, draft_definition={})
    env.db.add(workflow)
    env.db.flush()
    version = ExecutionWorkflowVersion(workflow_id=workflow.id, version_number=1, schema_version="1.0", definition={}, checksum="a"*64, contract_checksum="b"*64)
    env.db.add(version)
    env.db.flush()
    common = dict(workflow_id=workflow.id, workflow_version_id=version.id, created_by_id=env.admin.id,
        inspection_number=NUMBER, definition_snapshot={}, definition_checksum="a"*64, contract_checksum="b"*64)
    parent = ExecutionRun(**common, idempotency_key="parent", status="running" if report_path == "cancel_after_fail" else "cancel_pending")
    env.db.add(parent)
    env.db.flush()
    child = ExecutionRun(**common, idempotency_key="child", status="waiting_external" if report_path == "cancel_after_fail" else "cancel_pending", parent_run_id=parent.id)
    env.db.add(child)
    env.db.flush()
    node = ExecutionNodeRun(run_id=child.id, node_id="entry", node_type="external.operation", node_name="Entry", status="waiting_external")
    env.db.add(node)
    env.db.flush()
    operation.run_id, operation.node_run_id = child.id, node.id
    env.db.commit()
    if report_path == "recover":
        record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started")
        attempt.lease_expires_at = operation.lease_expires_at = utcnow() - timedelta(minutes=5)
        env.db.commit()
        assert expire_stale_external_attempts(env.db) == 1
        env.db.commit()
        assert operation.status == "reconciliation_required" and child.status == "cancel_pending"
        assert recover(env, attempt, operation)["released"]
    else:
        response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt.id}/fail", json={
            "bridge_id": "p4-writer", "stage": "generic_save_started", "error_code": "COMException", "message": "Excel failed"})
        assert response.status_code == 200, response.text
        env.db.expire_all()
        if report_path == "cancel_after_fail":
            assert child.status == "waiting_external"
            set_run_control_status(env.db, run_id=parent.id, action="cancel", actor=env.admin)
            env.db.commit()
    assert operation.status == "reconciliation_required"
    assert child.status == "cancelled" and node.status == "cancelled"
    assert child.output_data["external_side_effect_unknown"] is True
    if report_path != "cancel_after_fail":
        assert child.output_data["external_side_effect_resolutions"][-1]["remote_write_performed"] is None
        assert child.output_data["cancelled_after_side_effect"] is None
    # Parent settlement runs in the ordinary maintenance loop, not under a
    # child->parent lock inside the Bridge callback.
    if parent.status == "cancel_pending":
        assert settle_cancelled_batches(env.db)
    env.db.commit()
    assert parent.status == "cancelled"
    assert operation.status == "reconciliation_required" and operation.attempt_count == 1


def test_recover_completes_saved_success_receipt_after_lost_response_and_expiry(bridge_env):
    from app.execution.engine import expire_stale_external_attempts
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started")
    attempt.lease_expires_at = operation.lease_expires_at = utcnow() - timedelta(minutes=5)
    env.db.commit()
    assert expire_stale_external_attempts(env.db) == 1
    env.db.commit()
    assert operation.status == "reconciliation_required"
    payload = receipt(operation)
    result = recover(env, attempt, operation, stage="generic_projection_verified", receipt=payload, stdout_summary="saved successful Writer result")
    assert result["released"] and result["reason"] == "receipt_completed"
    assert operation.status == "completed" and attempt.status == "completed"
    assert operation.receipt == payload and operation.attempt_count == 1
    assert recover(env, attempt, operation, receipt=payload)["reason"] == "already_settled"


def test_rejected_saved_receipt_is_preserved_and_does_not_poison_bridge(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started")
    env.db.commit()
    bad_receipt = {**receipt(operation), "payload_checksum": "0" * 64}
    result = recover(env, attempt, operation, receipt=bad_receipt)
    assert result["released"] and result["reason"] == "isolated"
    assert operation.status == "reconciliation_required"
    assert result["attempt"]["error"]["diagnostics"]["receipt_recovery_error"]["code"]
    assert any(item.get("receipt") == bad_receipt for item in attempt.checkpoints)


def test_unrecognized_pending_stage_does_not_block_handoff_or_rewind_write(bridge_env):
    env = bridge_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_save_started")
    env.db.commit()
    result = recover(env, attempt, operation, stage="old-client-failed")
    assert result["released"]
    assert operation.status == "reconciliation_required"
    assert attempt.current_stage == "generic_save_started"
    assert result["attempt"]["error"]["diagnostics"]["unrecognized_pending_stage"] == "old-client-failed"


def test_old_process_exit_confirmation_cannot_settle_a_newer_attempt():
    from app.execution.engine import _external_write_still_uninterruptible
    operation = ExecutionExternalOperation(status="reconciliation_required", attempt_count=2,
        verification={"stopped_writer_recovery": {"process_exited": True, "attempt_no": 1}})
    assert _external_write_still_uninterruptible(operation)
    operation.attempt_count = 1
    assert not _external_write_still_uninterruptible(operation)
    operation.status = "in_progress"
    assert _external_write_still_uninterruptible(operation)
