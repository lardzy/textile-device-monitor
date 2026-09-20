"""Corrections retain exact identity, authorship and untouched business fields."""

from copy import deepcopy

import pytest

from app.execution.connector_records import record_fingerprint
from app.execution.connector_updates import GENERIC_UPDATE, UPDATE_OPERATION, update_receipt, updated_record_matches
from app.execution.external_operations import (
    bridge_external_operation, claim_approved_external_operation, complete_external_attempt,
    fail_external_attempt, record_external_attempt_stage,
)
from app.execution.models import ExecutionExternalOperation, ExecutionTaskSnapshotCache, utcnow
from tests.test_execution_connector_operations import operation_env, submit
from tests.test_execution_domain_services import NUMBER
from tests.test_execution_workflow_replacement import environment, request


def example_record(project, number):
    register_id, record_id = "sha256:" + "a" * 16, "sha256:" + "b" * 16
    before = {
        "record_ref": "check-record:" + register_id, "record_kind": "generic",
        "register": {"ID": register_id, "SampleNo": number, "CheckItemID": project["check_item_id"],
                     "OriginalRecordID": record_id, "CreateUser": "sha256:" + "c" * 16,
                     "CheckUser": "sha256:" + "c" * 16, "CreateTime": "2026-09-01T08:00:00",
                     "LastUpdateTime": "2026-09-01T08:00:00", "ProofTime": None, "ProofUser": None,
                     "SampleIdentity": "正面"},
        "generic_record": {"ID": record_id, "CheckRecordRegisterID": register_id, "SampleNo": number,
                           "CheckItemID": project["check_item_id"], "CheckItemName": project["check_item_name"],
                           "TestMethod": "GB/T 4688-2020", "Grade": "保留原等级", "Unit": None,
                           "SampleDescription": "正面", "Remark": "原备注", "JudgeBasis": None,
                           "TotalJudge": None, "StandardType": None, "AttachInfo": None,
                           "ReportCheckItemName": project["check_item_name"], "CheckUser": None},
        "details": [{"ID": "sha256:" + "d" * 16, "CurrencyItemRecordNewID": record_id, "SeqNum": 1,
                     "RealValue": "木浆", "StandardValue": None, "RealLocation": None, "StandardLocation": None}],
        "key_results": [{"SampleNo": number, "CheckItemID": project["check_item_id"], "OriginalRecordID": record_id,
                         "CheckResult": "木浆", "StandardValue": None, "SampleIdentity": "正面", "Remark": "原备注",
                         "MeasureUnit": None, "JudgeBasis": None, "Judgement": None}],
        "list_data": [], "other_data": [], "association_issues": [],
    }
    before["content_fingerprint"] = record_fingerprint(before)
    return before


@pytest.fixture
def update_env(operation_env):
    env = operation_env
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    project = cache.snapshot["projects"][0]
    before = example_record(project, NUMBER)
    cache.check_records = {"schema_version": 1, "records": [before]}
    env.db.commit()
    env.before = before
    env.payload.update(operation_ref=GENERIC_UPDATE, input={
        "inspection_number": NUMBER, "project_key": project["project_key"], "record_ref": before["record_ref"],
        "expected_content_fingerprint": before["content_fingerprint"],
        "changes": {"result_value": "木浆、竹浆", "remark": "复查后更正"},
    })
    return env


def after_record(env):
    record = deepcopy(env.before)
    record["generic_record"]["Remark"] = "复查后更正"
    record["register"].update(LastUpdateTime="2026-09-20T08:00:00", ProofTime="2026-09-20T08:00:00",
                              ProofUser="sha256:" + "e" * 16)
    record["details"][0].update(ID="sha256:" + "f" * 16, RealValue="木浆、竹浆")
    record["key_results"][0].update(CheckResult="木浆、竹浆", Remark="复查后更正")
    record["content_fingerprint"] = record_fingerprint(record)
    return record


def claim_update(env):
    result = claim_approved_external_operation(env.db, bridge_id="update-writer", account_name="test-operator",
                                              supported_operation_types={UPDATE_OPERATION})
    env.db.commit()
    assert result is not None
    return result


def test_update_submit_claim_readback_keeps_ids_and_does_not_add_run(update_env):
    env = update_env
    first = submit(env)["operation"]
    assert first["request_summary"]["before_values"]["result_value"] == "木浆"
    assert first["request_summary"]["after_values"]["sample_identity"] == "正面"
    assert first["run_id"] is None and submit(env, status=200)["duplicate"]
    # An old Bridge cannot receive an update package.
    assert claim_approved_external_operation(env.db, bridge_id="old", account_name="test-operator") is None
    operation, attempt, credential = claim_update(env)
    from tests.test_execution_paper_external_operations import _BRIDGE_MODULE as bridge_module
    view = bridge_external_operation(operation, credential=credential)
    package = bridge_module.validate_generic_update_package(view, view["request_summary"])
    assert package["before"] == env.before
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="update-writer", stage="update_verified")
    env.db.commit()
    measured = after_record(env)
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="update-writer",
                              receipt=update_receipt(operation, measured))
    env.db.commit()
    result = request(env, "GET", f"v1/connector-operations/{operation.id}")
    assert result["status"] == "completed" and result["result"]["record"] == measured
    assert env.db.query(ExecutionExternalOperation).count() == 1


@pytest.mark.parametrize("field", ["expected_content_fingerprint", "record_ref", "project_key"])
def test_update_rejects_stale_or_wrong_target(update_env, field):
    env = update_env
    value = {"expected_content_fingerprint": "0" * 64, "record_ref": "check-record:sha256:" + "0" * 16,
             "project_key": "task-project:" + "0" * 24}[field]
    submit(env, input={**env.payload["input"], field: value}, status=409)
    assert env.db.query(ExecutionExternalOperation).count() == 0


@pytest.mark.parametrize("changes", [{}, {"sql": "update"}, {"result_value": ""}, {"result_value": "   "}])
def test_update_rejects_unbounded_or_empty_changes(update_env, changes):
    submit(update_env, input={**update_env.payload["input"], "changes": changes}, status=422)


@pytest.mark.parametrize("section,field,value", [
    ("register", "CreateUser", "sha256:" + "0" * 16),
    ("register", "ID", "sha256:" + "0" * 16),
    ("generic_record", "Grade", "丢失原值"), ("generic_record", "CheckItemID", "different"),
    ("key_results", "CheckResult", "旧值"), ("details", "RealValue", "错误"),
])
def test_wrong_or_partial_readback_cannot_complete(update_env, section, field, value):
    env = update_env
    submit(env)
    operation, attempt, _ = claim_update(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="update-writer", stage="update_verified")
    measured = after_record(env)
    row = measured[section][0] if isinstance(measured[section], list) else measured[section]
    row[field] = value
    measured["content_fingerprint"] = record_fingerprint(measured)
    # The direct validator is tested independently of Bridge-token configuration.
    from app.execution.connector_updates import validate_update_receipt
    from app.execution.errors import ExecutionApiError
    with pytest.raises(ExecutionApiError):
        validate_update_receipt(operation, update_receipt(operation, measured))
    assert operation.status == "in_progress"


def test_partial_projection_is_distinguished_from_a_new_manual_edit(update_env):
    env = update_env
    submit(env)
    operation = env.db.query(ExecutionExternalOperation).one()
    measured = after_record(env)
    measured["key_results"] = []
    measured["content_fingerprint"] = record_fingerprint(measured)
    assert updated_record_matches(operation.request_summary, measured, projection=False)
    assert not updated_record_matches(operation.request_summary, measured)
    measured["generic_record"]["Grade"] = "他人修改"
    measured["content_fingerprint"] = record_fingerprint(measured)
    assert not updated_record_matches(operation.request_summary, measured, projection=False)


def interrupt_update(env, *, cancel=False):
    submit(env)
    operation, attempt, _ = claim_update(env)
    if cancel:
        request(env, "POST", f"v1/connector-operations/{operation.id}/cancel")
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="update-writer",
                          stage="update_started", error_code="connection_lost")
    env.db.commit()
    assert operation.status == "reconciliation_required"
    return operation, attempt


def fresh_read(env, record):
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    cache.status = "ready"
    cache.fetched_at = utcnow()
    cache.revision += 1
    cache.check_records = {"schema_version": 1, "records": [record] if record else []}
    env.db.commit()


@pytest.mark.parametrize("outcome,decision,status", [
    ("complete", "completed_from_readback", "completed"),
    ("unchanged", "retry_unchanged_record", "approved"),
    ("partial", "resume_projection", "approved"),
    ("manual_edit", "needs_attention", "reconciliation_required"),
    ("missing", "needs_attention", "reconciliation_required"),
])
def test_automatic_readback_completes_retries_or_repairs_only_exact_target(update_env, outcome, decision, status):
    from app.execution.connector_recovery import recover_connector_updates
    env = update_env
    operation, attempt = interrupt_update(env)
    checksum, frozen = operation.payload_checksum, deepcopy(operation.request_summary)
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    assert recover_connector_updates(env.db) == 0  # Old cached contents cannot settle a write.
    record = deepcopy(env.before) if outcome == "unchanged" else after_record(env)
    if outcome == "partial":
        record["key_results"] = []
    elif outcome == "manual_edit":
        record["generic_record"]["Grade"] = "另一操作员新值"
    record["content_fingerprint"] = record_fingerprint(record)
    fresh_read(env, None if outcome == "missing" else record)
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    assert operation.status == status
    assert operation.verification["automatic_recovery"]["decision"] == decision
    state = request(env, "GET", f"v1/connector-operations/{operation.id}")
    assert state["recovery"]["decision"] == decision
    assert operation.request_summary == frozen and operation.payload_checksum == checksum
    assert attempt.status == "failed"  # The original failed attempt is still history.
    if outcome in {"partial", "unchanged"}:
        operation, next_attempt, credential = claim_update(env)
        assert next_attempt.attempt_no == 2
        package = bridge_external_operation(operation, credential=credential)["machine_payload"]
        assert (package.get("resume_from") == record) is (outcome == "partial")


def test_recovery_does_not_treat_an_inflight_prewrite_snapshot_as_fresh(update_env):
    from app.execution.connector_recovery import recover_connector_updates
    from datetime import timedelta
    env = update_env
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    cache.status, cache.claim_expires_at = "running", utcnow() + timedelta(minutes=1)
    env.db.commit()
    operation, _ = interrupt_update(env)
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    fresh_read(env, after_record(env))  # This read started before interruption.
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    assert operation.status == "reconciliation_required" and cache.status == "queued"
    fresh_read(env, after_record(env))
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    assert operation.status == "completed"


@pytest.mark.parametrize("cancel_before_failure", [True, False])
def test_cancelled_unknown_update_never_restarts_a_write(update_env, cancel_before_failure):
    from app.execution.connector_recovery import recover_connector_updates
    env = update_env
    operation, _ = interrupt_update(env, cancel=cancel_before_failure)
    if not cancel_before_failure:
        result = request(env, "POST", f"v1/connector-operations/{operation.id}/cancel")
        assert result["cancel_requested"] is True
    recover_connector_updates(env.db)
    env.db.commit()
    fresh_read(env, env.before)
    recover_connector_updates(env.db)
    env.db.commit()
    assert operation.status == "cancelled"
    assert operation.verification["automatic_recovery"]["decision"] == "cancelled_without_change"


def test_update_recovery_retry_budget_does_not_unlock_an_unknown_record(update_env):
    from app.execution.connector_recovery import recover_connector_updates
    env = update_env
    operation, _ = interrupt_update(env)
    operation.attempt_count = 3
    env.db.commit()
    recover_connector_updates(env.db)
    env.db.commit()
    fresh_read(env, env.before)
    recover_connector_updates(env.db)
    env.db.commit()
    assert operation.status == "reconciliation_required"
    assert operation.verification["automatic_recovery"]["decision"] == "needs_attention"


def test_read_failures_retry_three_times_then_accept_a_later_refresh(update_env):
    from app.execution.connector_recovery import recover_connector_updates
    env = update_env
    operation, _ = interrupt_update(env)
    recover_connector_updates(env.db)
    env.db.commit()
    for index in range(3):
        cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
        cache.status = "failed"
        cache.revision += 1
        env.db.commit()
        assert recover_connector_updates(env.db) == 1
        env.db.commit()
        assert cache.status == ("queued" if index < 2 else "failed")
    assert operation.verification["automatic_recovery"]["decision"] == "needs_attention"
    assert recover_connector_updates(env.db) == 0
    fresh_read(env, after_record(env))
    recover_connector_updates(env.db)
    env.db.commit()
    assert operation.status == "completed"


def test_already_matching_noop_does_not_claim_a_remote_change(update_env):
    from app.execution.connector_recovery import recover_connector_updates
    env = update_env
    env.payload["input"]["changes"] = {"result_value": "木浆"}
    operation, _ = interrupt_update(env)
    recover_connector_updates(env.db)
    env.db.commit()
    fresh_read(env, env.before)
    recover_connector_updates(env.db)
    env.db.commit()
    result = request(env, "GET", f"v1/connector-operations/{operation.id}")
    assert result["status"] == "completed" and result["remote_write_performed"] is False


def test_generic_count_attestation_cannot_settle_an_exact_correction(update_env):
    from app.execution.external_operations import reconcile_external_operation
    from app.execution.errors import ExecutionApiError
    env = update_env
    operation, attempt = interrupt_update(env)
    with pytest.raises(ExecutionApiError) as error:
        reconcile_external_operation(env.db, operation_id=operation.id, actor=env.admin,
            action="confirm_no_side_effect", attempt_id=attempt.id, payload_checksum=operation.payload_checksum,
            confirmed_sample_number=NUMBER, note="仅凭记录数量不代表没有修改", evidence={"exact_record_count": 0})
    assert error.value.code == "connector_update_uses_record_readback"
