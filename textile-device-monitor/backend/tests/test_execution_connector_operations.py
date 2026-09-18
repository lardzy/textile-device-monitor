"""Standalone submission uses the same Bridge, receipts and recovery as Runs."""

import hashlib
from datetime import timedelta

import pytest

from app.config import settings
from app.execution.engine import expire_stale_external_attempts
from app.execution.external_operations import (
    GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES, LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
    _canonical_checksum, bridge_external_operation, claim_approved_external_operation,
    complete_external_attempt, fail_external_attempt, heartbeat_external_attempt,
    public_external_reconciliation_context, reconcile_external_operation,
    record_external_attempt_stage,
)
from app.execution.models import (
    ExecutionAuditLog, ExecutionCredential, ExecutionExternalOperation, ExecutionHumanTask,
    ExecutionRun, ExecutionTaskSnapshotCache, utcnow,
)
from tests.test_execution_domain_services import NUMBER, task_snapshot
from tests.test_execution_workflow_replacement import environment, request


@pytest.fixture
def operation_env(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED", True)
    task_snapshot(env)
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    project = {**cache.snapshot["projects"][0], "check_item_no": "51.113K", "seq_num": 1}
    project["project_key"] = "task-project:" + hashlib.sha256("\0".join(str(project[k]) for k in (
        "task_check_item_id", "check_item_id", "check_item_no", "check_item_name", "check_method", "seq_num",
    )).encode()).hexdigest()[:24]
    cache.snapshot = {**cache.snapshot, "projects": [project]}
    credential = ExecutionCredential(user_id=env.admin.id, system_key="legacy_inspection",
                                     account_name="test-operator", encrypted_secret="unused", revision=1)
    env.db.add(credential)
    env.db.commit()
    env.credential = credential
    env.payload = {
        "operation_ref": "legacy_fibrecheck.check_record.generic_entry@1",
        "credential_id": credential.id, "idempotency_key": "direct-submit-one",
        "input": {"inspection_number": NUMBER, "project_key": project["project_key"],
                  "result_value": "木浆、竹浆", "expected_existing_register_count": 0, "sample_identity": "正面"},
    }
    return env


def submit(env, *, status=202, **changes):
    return request(env, "POST", "v1/connector-operations", {**env.payload, **changes}, status=status)


def claim(env):
    env.db.expire_all()
    result = claim_approved_external_operation(env.db, bridge_id="p4-writer", account_name="test-operator",
                                              supported_operation_types={LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION})
    env.db.commit()
    assert result is not None
    return result


def receipt(operation):
    return {
        "schema_version": 1, "receipt_type": LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
        "operation_id": operation.id, "payload_checksum": operation.payload_checksum,
        "target_sample_number": NUMBER, "stages": list(GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES),
        "reconciliation_required": False, "task_project": operation.request_summary["task_project"],
        "final_entry": {"package_schema_version": 2, "expected_existing_register_count": 0,
                        "resulting_register_count": 1, "detail_count": 1, "key_result_count": 1,
                        "record_id": "sha256:" + "1" * 16, "proofed": False},
    }


def test_direct_submit_claim_complete_and_repeated_request_without_run(operation_env):
    env = operation_env
    first = submit(env)["operation"]
    assert first["status"] == "approved" and first["run_id"] is None
    assert first["approval"]["expires_at"] is None
    assert first["remote_write_performed"] is False
    assert submit(env, status=200)["duplicate"] is True
    assert env.db.query(ExecutionRun).count() == env.db.query(ExecutionHumanTask).count() == 0
    assert env.db.query(ExecutionExternalOperation).count() == 1
    operation, attempt, credential = claim(env)
    bridge = bridge_external_operation(operation, credential=credential)
    assert bridge["machine_payload"]["generic_record"]["details"][0]["real_value"] == "木浆、竹浆"
    # Retain the existing Bridge package validator, rather than a second Writer.
    from tests.test_execution_paper_external_operations import _BRIDGE_MODULE as bridge_module
    bridge_module.validate_generic_final_entry_machine_payload(bridge, bridge["request_summary"])
    heartbeat_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer")
    env.db.commit()
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer",
                                  stage="generic_projection_verified")
    env.db.commit()
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=receipt(operation))
    env.db.commit()
    final = request(env, "GET", f"v1/connector-operations/{operation.id}")
    assert final["status"] == "completed" and final["remote_write_performed"] is True
    assert final["result"]["final_entry"]["record_id"] == "sha256:" + "1" * 16
    assert final["attempts"][0]["status"] == "completed"
    assert submit(env, status=200)["operation"]["status"] == "completed"
    assert env.db.query(ExecutionAuditLog).filter_by(resource_id=operation.id).count() >= 3


def test_idempotency_conflict_business_fence_owner_and_cancel(operation_env):
    env = operation_env
    first = submit(env)["operation"]
    assert submit(env, input={**env.payload["input"], "result_value": "棉"}, status=409)["code"] == "external_operation_idempotency_conflict"
    submit(env, idempotency_key="different-key", status=409)
    assert env.user_client.get(first["status_url"]).status_code == 404
    assert env.user_client.post(first["status_url"] + "/cancel").status_code == 404
    final = request(env, "POST", f"v1/connector-operations/{first['id']}/cancel")
    assert final["status"] == "cancelled"
    assert submit(env, status=200)["operation"]["status"] == "cancelled"
    assert submit(env, idempotency_key="after-cancel")["operation"]["id"] != first["id"]


@pytest.mark.parametrize("stage,expected", [("generic_write_ready", "approved"), ("generic_save_started", "reconciliation_required")])
def test_standalone_lease_recovery_respects_write_boundary(operation_env, stage, expected):
    env = operation_env
    submit(env)
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage=stage)
    env.db.commit()
    assert expire_stale_external_attempts(env.db, now=utcnow() + timedelta(minutes=5)) == 1
    env.db.commit()
    assert operation.status == expected
    if expected == "reconciliation_required":
        assert claim_approved_external_operation(env.db, bridge_id="p4-writer", account_name="test-operator",
            supported_operation_types={LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION}) is None
        submit(env, idempotency_key="do-not-repeat-an-unknown-write", status=409)


def test_standalone_cancel_in_progress_before_write_settles_without_node(operation_env):
    env = operation_env
    submit(env)
    operation, attempt, _ = claim(env)
    request(env, "POST", f"v1/connector-operations/{operation.id}/cancel")
    env.db.expire_all()
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer",
                          stage="generic_write_ready", error_code="cancelled")
    env.db.commit()
    assert operation.status == "cancelled"


def test_standalone_unknown_write_can_be_reconciled_without_run(operation_env):
    env = operation_env
    submit(env)
    operation, attempt, _ = claim(env)
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer",
                          stage="generic_save_started", error_code="connection_lost")
    env.db.commit()
    assert request(env, "GET", f"v1/external-operations/{operation.id}/reconciliation")
    summary = operation.request_summary["final_entry_summary"]
    from app.execution.external_operations import GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT
    operation, duplicate = reconcile_external_operation(
        env.db, operation_id=operation.id, actor=env.admin, action="confirm_no_side_effect",
        attempt_id=attempt.id, payload_checksum=operation.payload_checksum,
        confirmed_sample_number=NUMBER, note="测试只读核对未产生记录",
        evidence={"evidence_contract": GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT,
                  "checked_at": utcnow().isoformat(), "final_entry_summary_checksum": _canonical_checksum(summary),
                  "expected_existing_register_count": 0, "actual_register_count": 0,
                  "actual_detail_count": 0, "actual_key_result_count": 0, "actual_proofed_count": 0,
                  "writer_stage": "generic_save_started"},
    )
    env.db.commit()
    assert operation.status == "failed" and not duplicate


@pytest.mark.parametrize("change", [
    {"operation_ref": "legacy_fibrecheck.microscopy.check_record_entry@1"},
    {"contract_digest": "0" * 64}, {"connector_version": "99.0.0"},
    {"credential_id": "other-user-credential"}, {"input": {}},
])
def test_invalid_submission_creates_no_operation(operation_env, change):
    env = operation_env
    submit(env, status=404 if "credential_id" in change else 422, **change)
    assert env.db.query(ExecutionExternalOperation).count() == 0
