"""Maintenance settlement must preserve unknown writes and never resume a Run."""
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.execution.external_operations import _remote_business_key, public_external_operation
from app.execution.models import (
    ExecutionAuditLog, ExecutionCategory, ExecutionEdgeRun, ExecutionEvent,
    ExecutionExternalAttempt, ExecutionExternalOperation, ExecutionNodeAttempt,
    ExecutionNodeRun, ExecutionOutbox, ExecutionRun, ExecutionUser, ExecutionWorkflow, utcnow,
)

spec = importlib.util.spec_from_file_location("manual_cancel_settlement", Path(__file__).parents[2] / "tools" / "settle_cancelled_external_run.py")
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


@pytest.fixture
def env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        admin = ExecutionUser(username="admin", display_name="Admin", password_hash="unused", role="admin")
        category = ExecutionCategory(key="maintenance", name="Maintenance")
        db.add_all([admin, category]); db.flush()
        workflow = ExecutionWorkflow(slug="maintenance", name="Maintenance", category_id=category.id, draft_definition={})
        db.add(workflow); db.flush()
        run = ExecutionRun(workflow_id=workflow.id, created_by_id=admin.id, idempotency_key="cancel-test",
                           inspection_number="26W007347", status="cancel_pending", definition_snapshot={},
                           definition_checksum="a" * 64, contract_checksum="b" * 64)
        db.add(run); db.flush()
        node = ExecutionNodeRun(run_id=run.id, node_id="upload", node_type="external.operation", node_name="Upload",
                                status="waiting_external", attempt_count=1, output_data={"preserved": "node"})
        downstream = ExecutionNodeRun(run_id=run.id, node_id="review", node_type="external.operation", node_name="Review", status="cancelled")
        db.add_all([node, downstream]); db.flush()
        db.add(ExecutionEdgeRun(run_id=run.id, edge_id="upload-review", source_node_id="upload", target_node_id="review"))
        node_attempt = ExecutionNodeAttempt(node_run_id=node.id, attempt_number=1, worker_id="worker", lease_token="old-token", status="waiting_external")
        operation = ExecutionExternalOperation(operation_key="test", run_id=run.id, node_run_id=node.id,
            connector_key="legacy_fibrecheck", credential_revision=1, account_scope_key="account",
            remote_business_key=_remote_business_key("26W007347"), status="reconciliation_required",
            payload_checksum="c" * 64, request_summary={"target_sample_number": "26W007347"},
            preflight_expires_at=utcnow(), attempt_count=1, fence_token="original-fence",
            receipt={"partial": "preserve"}, verification={"source": "preserve"}, error_code="lease_expired",
            error_message="lease expired")
        db.add_all([operation, node_attempt]); db.flush()
        attempt = ExecutionExternalAttempt(operation_id=operation.id, attempt_no=1, bridge_id="writer", status="failed",
            current_stage="main_record_save_started", lease_expires_at=utcnow() - timedelta(minutes=5),
            checkpoints=[{"stage": "file_copy_verified"}], stdout_summary="keep original log", error_code="lease_expired",
            finished_at=utcnow() - timedelta(minutes=4))
        db.add(attempt); db.commit()
        args = dict(run_id=run.id, operation_id=operation.id, attempt_id=attempt.id, payload_checksum=operation.payload_checksum,
                    admin_id=admin.id, note="用户已手工处理原项，并手动上传 -1；原 Writer 已退出，不重传。")
        yield SimpleNamespace(db=db, admin=admin, run=run, node=node, downstream=downstream, node_attempt=node_attempt,
                              operation=operation, attempt=attempt, args=args)
    engine.dispose()


def test_preview_is_read_only_and_execute_preserves_unknown_write(env):
    db = env.db
    original_finished = env.attempt.finished_at
    preview = maintenance.settle(db, **env.args)
    assert preview["execute"] is False and preview["remote_write_performed"] is None
    db.commit()
    assert env.run.status == "cancel_pending" and env.operation.status == "reconciliation_required"
    assert db.query(ExecutionEvent).count() == db.query(ExecutionAuditLog).count() == 0
    result = maintenance.settle(db, **env.args, execute=True, writer_stopped=True)
    db.commit()
    assert result["remote_io_performed"] is False
    assert env.run.status == env.node.status == env.operation.status == env.node_attempt.status == "cancelled"
    assert env.run.finished_at and env.downstream.status == "cancelled" and env.downstream.attempt_count == 0
    assert env.operation.fence_token is None
    assert env.operation.receipt == {"partial": "preserve"}
    assert env.operation.verification["source"] == "preserve"
    assert env.operation.verification["manual_cancel_settlement"]["remote_write_performed"] is None
    assert env.attempt.status == "failed" and env.attempt.error_code == "lease_expired"
    assert env.attempt.finished_at == original_finished
    assert env.attempt.checkpoints == [{"stage": "file_copy_verified"}]
    assert env.attempt.stdout_summary == "keep original log"
    assert env.node.output_data["preserved"] == "node"
    assert public_external_operation(env.operation)["remote_write_performed"] is None
    assert db.query(ExecutionEvent).count() == db.query(ExecutionOutbox).count() == 3
    assert db.query(ExecutionAuditLog).one().details["note"] == env.args["note"]
    assert all(event.payload["remote_write_performed"] is None for event in db.query(ExecutionEvent))
    assert db.query(ExecutionRun).count() == db.query(ExecutionExternalOperation).count() == db.query(ExecutionExternalAttempt).count() == 1
    assert maintenance.settle(db, **env.args, execute=True, writer_stopped=True)["duplicate"] is True
    db.commit()
    assert db.query(ExecutionEvent).count() == 3


@pytest.mark.parametrize("change,match", [
    ({"payload_checksum": "d" * 64}, "摘要"),
    ({"attempt_id": "different-attempt"}, "最新尝试"),
    ({"admin_id": "missing-admin"}, "管理员"),
    ({"note": " "}, "人工处理"),
])
def test_rejects_wrong_identity_and_missing_evidence(env, change, match):
    with pytest.raises(ValueError, match=match):
        maintenance.settle(env.db, **{**env.args, **change}, execute=True, writer_stopped=True)
    assert env.run.status == "cancel_pending"


def test_execute_requires_writer_stop_attestation(env):
    with pytest.raises(ValueError, match="Writer"):
        maintenance.settle(env.db, **env.args, execute=True)


@pytest.mark.parametrize("mutation,match", [
    (lambda e: setattr(e.admin, "role", "user"), "管理员"),
    (lambda e: setattr(e.run, "status", "running"), "取消中"),
    (lambda e: setattr(e.attempt, "error_code", "other_failure"), "租约过期"),
    (lambda e: setattr(e.operation, "lease_owner", "live-writer"), "租约"),
    (lambda e: setattr(e.attempt, "lease_expires_at", utcnow() + timedelta(minutes=1)), "租约"),
    (lambda e: setattr(e.downstream, "status", "pending"), "未结束节点"),
    (lambda e: setattr(e.downstream, "status", "succeeded"), "下游节点"),
    (lambda e: setattr(e.operation, "verification", {"reconciliation": {"action": "confirm_completed"}}), "已有对账"),
])
def test_rejects_changed_state_and_late_receipt_races(env, mutation, match):
    mutation(env)
    env.db.commit()
    with pytest.raises(ValueError, match=match):
        maintenance.settle(env.db, **env.args, execute=True, writer_stopped=True)
    assert env.db.query(ExecutionEvent).count() == 0
    assert env.db.query(ExecutionAuditLog).count() == 0


def test_newer_attempt_cannot_be_cancelled_with_old_evidence(env):
    env.db.add(ExecutionExternalAttempt(operation_id=env.operation.id, attempt_no=2, bridge_id="writer", status="claimed"))
    env.operation.attempt_count = 2
    env.db.commit()
    with pytest.raises(ValueError, match="最新尝试"):
        maintenance.settle(env.db, **env.args, execute=True, writer_stopped=True)


def test_another_unsettled_operation_blocks_settlement(env):
    env.db.add(ExecutionExternalOperation(operation_key="another", run_id=env.run.id,
        connector_key="legacy_fibrecheck", credential_revision=1, account_scope_key="account",
        remote_business_key="different", status="approved", payload_checksum="d" * 64,
        request_summary={}, preflight_expires_at=utcnow()))
    env.db.commit()
    with pytest.raises(ValueError, match="其他未结束外部操作"):
        maintenance.settle(env.db, **env.args, execute=True, writer_stopped=True)


def test_running_child_blocks_settlement(env):
    env.db.add(ExecutionRun(workflow_id=env.run.workflow_id, created_by_id=env.admin.id, idempotency_key="child",
        parent_run_id=env.run.id, inspection_number=env.run.inspection_number, status="running",
        definition_snapshot={}, definition_checksum="e" * 64, contract_checksum="f" * 64))
    env.db.commit()
    with pytest.raises(ValueError, match="子运行"):
        maintenance.settle(env.db, **env.args, execute=True, writer_stopped=True)
