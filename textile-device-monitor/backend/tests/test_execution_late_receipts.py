"""Expired writers may report verified results, but cannot regain a write lease."""

from copy import deepcopy
from datetime import timedelta

import pytest

from app.execution.engine import expire_stale_external_attempts
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    complete_external_attempt,
    heartbeat_external_attempt,
    record_external_attempt_stage,
)
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionNodeRun,
    utcnow,
)
from tests.test_execution_connector_operations import (
    claim,
    operation_env,
    receipt,
    submit,
)
from tests.test_execution_native_operations import start
from workflow_native_helpers import drain, environment, request


def expire_upload(env, *, sweep=True, stage="generic_save_started"):
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(
        env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage=stage,
    )
    env.db.commit()
    after_expiry = utcnow() + timedelta(minutes=5)
    if sweep:
        assert expire_stale_external_attempts(env.db, now=after_expiry) == 1
        env.db.commit()
    return operation, attempt, after_expiry


@pytest.mark.parametrize("sweep", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_late_verified_receipt_finishes_run_without_repeating_write(operation_env, sweep, cancel):
    env = operation_env
    run = start(env)
    drain(env)
    operation, attempt, now = expire_upload(env, sweep=sweep)
    if cancel:
        request(env, "POST", f"v1/runs/{run['id']}/cancel", {})
        env.db.expire_all()
    old_attempt_lease = attempt.lease_expires_at
    old_operation_lease = operation.lease_expires_at
    old_status = attempt.status
    record_external_attempt_stage(
        env.db, attempt_id=attempt.id, bridge_id="p4-writer",
        stage="generic_projection_verified", now=now,
    )
    env.db.commit()
    assert attempt.status == old_status
    assert attempt.lease_expires_at == old_attempt_lease
    assert operation.lease_expires_at == old_operation_lease
    with pytest.raises(ExecutionApiError):
        heartbeat_external_attempt(
            env.db, attempt_id=attempt.id, bridge_id="p4-writer", now=now,
        )
    env.db.rollback()
    result = receipt(operation)
    complete_external_attempt(
        env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=result, now=now,
    )
    env.db.commit()
    drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == ("cancelled" if cancel else "completed")
    assert operation.status == attempt.status == "completed"
    assert operation.error_code is None and attempt.error_code is None
    assert operation.attempt_count == 1
    assert env.db.query(ExecutionExternalOperation).count() == 1
    assert env.db.query(ExecutionExternalAttempt).count() == 1
    if cancel:
        end = env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"], node_id="end").one()
        assert end.status == "cancelled" and end.attempt_count == 0
    audit_count = env.db.query(ExecutionAuditLog).filter_by(
        resource_id=operation.id, action="external_operation.complete",
    ).count()
    assert audit_count == 1
    audit = env.db.query(ExecutionAuditLog).filter_by(
        resource_id=operation.id, action="external_operation.complete",
    ).one()
    assert audit.details["late_receipt"] is True
    # The server may have committed while the HTTP response was lost.
    complete_external_attempt(
        env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=result, now=now,
    )
    env.db.commit()
    assert env.db.query(ExecutionAuditLog).filter_by(
        resource_id=operation.id, action="external_operation.complete",
    ).count() == audit_count
    changed = deepcopy(result)
    changed["final_entry"]["record_id"] = "sha256:" + "2" * 16
    with pytest.raises(ExecutionApiError) as error:
        complete_external_attempt(
            env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=changed,
        )
    assert error.value.code == "external_attempt_receipt_changed"


@pytest.mark.parametrize("sweep", [False, True])
def test_late_receipt_still_requires_verified_stage_and_valid_payload(operation_env, sweep):
    env = operation_env
    submit(env)
    operation, attempt, now = expire_upload(env, sweep=sweep)
    with pytest.raises(ExecutionApiError) as error:
        complete_external_attempt(
            env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=receipt(operation), now=now,
        )
    assert error.value.code == "external_attempt_not_verified"
    env.db.rollback()
    record_external_attempt_stage(
        env.db, attempt_id=attempt.id, bridge_id="p4-writer",
        stage="generic_projection_verified", now=now,
    )
    env.db.commit()
    invalid = {**receipt(operation), "payload_checksum": "0" * 64}
    with pytest.raises(ExecutionApiError):
        complete_external_attempt(
            env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=invalid, now=now,
        )
    env.db.rollback()
    assert operation.status != "completed"


@pytest.mark.parametrize("sweep", [False, True])
@pytest.mark.parametrize("stage", ["authenticated", "generic_save_started"])
def test_late_stage_never_reissues_write_permission(operation_env, sweep, stage):
    env = operation_env
    submit(env)
    operation, attempt, now = expire_upload(env, sweep=sweep)
    with pytest.raises(ExecutionApiError) as error:
        record_external_attempt_stage(
            env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage=stage, now=now,
        )
    assert error.value.code == "external_attempt_late_prewrite_stage"
    env.db.rollback()
    assert attempt.current_stage == "generic_save_started"


@pytest.mark.parametrize("change", ["prewrite", "wrong_bridge", "other_failure", "resolved", "cancelled", "new_attempt"])
def test_late_report_cannot_override_other_attempt_or_resolution(operation_env, change):
    env = operation_env
    submit(env)
    operation, attempt, now = expire_upload(
        env, stage="generic_write_ready" if change == "prewrite" else "generic_save_started",
    )
    bridge_id = "p4-writer"
    if change == "wrong_bridge":
        bridge_id = "other-writer"
    elif change == "other_failure":
        attempt.error_code = operation.error_code = "remote_write_failed"
    elif change == "resolved":
        operation.verification = {**operation.verification, "reconciliation": {"action": "confirm_no_side_effect"}}
    elif change == "cancelled":
        operation.status = "cancelled"
    elif change == "new_attempt":
        operation.attempt_count += 1
        env.db.add(ExecutionExternalAttempt(
            operation_id=operation.id, attempt_no=operation.attempt_count,
            bridge_id=bridge_id, status="in_progress", current_stage="authenticated",
        ))
    env.db.commit()
    with pytest.raises(ExecutionApiError):
        record_external_attempt_stage(
            env.db, attempt_id=attempt.id, bridge_id=bridge_id,
            stage="generic_projection_verified", now=now,
        )
    env.db.rollback()
    with pytest.raises(ExecutionApiError):
        complete_external_attempt(
            env.db, attempt_id=attempt.id, bridge_id=bridge_id, receipt=receipt(operation), now=now,
        )
    env.db.rollback()
    assert operation.status != "completed"
