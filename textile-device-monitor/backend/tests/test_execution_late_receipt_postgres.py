"""Real row-lock races for late Bridge completion in a disposable database."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url


url = make_url(os.environ["TEST_DATABASE_URL"])
if url.get_backend_name() != "postgresql" or not url.database.endswith("_test"):
    pytest.skip("requires a disposable PostgreSQL *_test database", allow_module_level=True)

from app.database import SessionLocal
from app.execution.engine import expire_stale_external_attempts, set_run_control_status
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES,
    GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT,
    LEGACY_CONNECTOR_KEY,
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
    _canonical_checksum,
    complete_external_attempt,
    lock_legacy_remote_business_scope,
    reconcile_external_operation,
)
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionCategory,
    ExecutionEdgeRun,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionUser,
    ExecutionWorkflow,
    utcnow,
)


@pytest.fixture
def expired_operation():
    """Already executed, synthetically verified result; no remote I/O occurs."""
    with SessionLocal() as db:
        now = utcnow()
        number = "26" + str(uuid4().int % 10000000).zfill(7)
        admin = ExecutionUser(
            username="late-admin-" + uuid4().hex, display_name="isolated test",
            role="admin", password_hash="not-a-login", is_active=True,
        )
        project = {
            "project_key": "task-project:" + "a" * 24,
            "task_check_item_id": "sha256:" + "b" * 16,
            "check_item_id": "sha256:" + "c" * 16,
            "check_item_no": "51.113K", "check_item_name": "纸浆纤维鉴别",
            "check_method": "GB/T 4688-2020", "seq_num": 1, "check_count": 1,
        }
        summary = {
            "operation_type": LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
            "target_sample_number": number, "task_project": project,
            "final_entry_package": {
                "schema_version": 4, "expected_existing_register_count": 0,
                "generic_record": {"details": [{"real_value": "木浆"}]},
            },
            "final_entry_summary": {
                "expected_existing_register_count": 0, "resulting_register_count": 1,
                "expected_task_check_count": 1,
            },
        }
        op = ExecutionExternalOperation(
            connector_key=LEGACY_CONNECTOR_KEY, operation_key=uuid4().hex,
            credential_revision=1, account_scope_key=uuid4().hex,
            remote_business_key=lock_legacy_remote_business_scope(db, sample_number=number),
            status="in_progress", payload_checksum="d" * 64,
            request_summary=summary, preflight_expires_at=now + timedelta(hours=1),
            attempt_count=1, lease_owner="late-writer",
            lease_expires_at=now - timedelta(minutes=2),
        )
        db.add_all([admin, op])
        db.flush()
        attempt = ExecutionExternalAttempt(
            operation_id=op.id, attempt_no=1, bridge_id="late-writer",
            status="in_progress", current_stage="generic_projection_verified",
            lease_expires_at=op.lease_expires_at,
        )
        db.add(attempt)
        db.commit()
        document = {
            "schema_version": 1, "receipt_type": LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
            "operation_id": op.id, "payload_checksum": op.payload_checksum,
            "target_sample_number": number, "task_project": project,
            "stages": list(GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES),
            "reconciliation_required": False,
            "final_entry": {
                "package_schema_version": 4, "expected_existing_register_count": 0,
                "resulting_register_count": 1, "detail_count": 1, "key_result_count": 1,
                "record_id": "sha256:" + "1" * 16, "proofed": False,
            },
        }
        yield {"operation_id": op.id, "attempt_id": attempt.id, "admin_id": admin.id,
               "number": number, "receipt": document, "summary": summary}


def _complete(db, fixture):
    complete_external_attempt(
        db, attempt_id=fixture["attempt_id"], bridge_id="late-writer",
        receipt=fixture["receipt"],
    )


def test_late_completion_and_expiry_sweep_serialize(expired_operation):
    data = expired_operation
    barrier = threading.Barrier(2)

    def compete(sweep):
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            result = expire_stale_external_attempts(db) if sweep else _complete(db, data)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(compete, sweep) for sweep in (True, False)]
        for result in results:
            result.result(timeout=15)
    with SessionLocal() as db:
        assert db.get(ExecutionExternalOperation, data["operation_id"]).status == "completed"
        assert db.get(ExecutionExternalAttempt, data["attempt_id"]).status == "completed"
        assert db.query(ExecutionAuditLog).filter_by(
            resource_id=data["operation_id"], action="external_operation.complete",
        ).count() == 1


def test_two_late_complete_requests_are_idempotent(expired_operation):
    data = expired_operation
    with SessionLocal() as db:
        assert expire_stale_external_attempts(db) == 1
        db.commit()
    barrier = threading.Barrier(2)

    def finish():
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            _complete(db, data)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(finish) for _ in range(2)]
        for result in results:
            result.result(timeout=15)
    with SessionLocal() as db:
        assert db.query(ExecutionExternalAttempt).filter_by(operation_id=data["operation_id"]).count() == 1
        assert db.query(ExecutionAuditLog).filter_by(
            resource_id=data["operation_id"], action="external_operation.complete",
        ).count() == 1


@pytest.mark.parametrize("manual_first", [False, True])
def test_late_receipt_cannot_overwrite_concurrent_manual_resolution(expired_operation, manual_first):
    data = expired_operation
    with SessionLocal() as db:
        assert expire_stale_external_attempts(db) == 1
        db.commit()

    def finish(db, manual):
        if not manual:
            _complete(db, data)
            return
        reconcile_external_operation(
            db, operation_id=data["operation_id"], actor=db.get(ExecutionUser, data["admin_id"]),
            action="confirm_completed", attempt_id=data["attempt_id"],
            payload_checksum=data["receipt"]["payload_checksum"],
            confirmed_sample_number=data["number"], note="isolated verified record",
            evidence={
                "evidence_contract": GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT,
                "checked_at": utcnow().isoformat(),
                "final_entry_summary_checksum": _canonical_checksum(data["summary"]["final_entry_summary"]),
                "expected_existing_register_count": 0, "actual_register_count": 1,
                "actual_detail_count": 1, "actual_key_result_count": 1, "actual_proofed_count": 0,
                "writer_stage": "generic_projection_verified", "resulting_register_count": 1,
                "remote_record_id": "sha256:" + "1" * 16,
            },
        )

    waiting = threading.Event()

    def second():
        with SessionLocal() as db:
            waiting.set()
            try:
                finish(db, not manual_first)
                db.commit()
                return "completed"
            except ExecutionApiError as exc:
                db.rollback()
                return exc.code

    # Hold the first transition uncommitted while the second uses its own
    # connection. Both directions must recheck state after the row lock.
    with SessionLocal() as first, ThreadPoolExecutor(max_workers=1) as pool:
        finish(first, manual_first)
        result = pool.submit(second)
        assert waiting.wait(timeout=5)
        assert not result.done()
        first.commit()
        assert result.result(timeout=15) == (
            "external_attempt_not_active" if manual_first else "external_reconciliation_not_required"
        )
    with SessionLocal() as db:
        op = db.get(ExecutionExternalOperation, data["operation_id"])
        assert op.status == "completed"
        assert bool(op.verification.get("reconciliation")) == manual_first
        assert db.query(ExecutionAuditLog).filter_by(
            resource_id=op.id, action="external_operation.complete",
        ).count() == (0 if manual_first else 1)


@pytest.mark.parametrize("cancel_first", [False, True])
def test_cancel_and_late_completion_finish_cancelled_without_downstream_execution(expired_operation, cancel_first):
    data = expired_operation
    with SessionLocal() as db:
        category = ExecutionCategory(key=uuid4().hex, name="late receipt test")
        db.add(category)
        db.flush()
        definition = {
            "schema_version": "1.0",
            "nodes": [{"id": "external", "type": "external.test"}, {"id": "end", "type": "end"}],
            "edges": [{"id": "finish", "source": "external", "target": "end"}],
        }
        workflow = ExecutionWorkflow(
            slug=uuid4().hex, category_id=category.id, name="late receipt test",
            draft_definition=definition, created_by_id=data["admin_id"],
        )
        db.add(workflow)
        db.flush()
        run = ExecutionRun(
            workflow_id=workflow.id, created_by_id=data["admin_id"],
            idempotency_key=uuid4().hex, inspection_number=data["number"],
            status="waiting_external", definition_snapshot=definition,
            definition_checksum="1" * 64, contract_checksum="2" * 64,
        )
        db.add(run)
        db.flush()
        external = ExecutionNodeRun(
            run_id=run.id, node_id="external", node_type="external.test",
            node_name="writer", status="waiting_external", attempt_count=1,
        )
        end = ExecutionNodeRun(
            run_id=run.id, node_id="end", node_type="end", node_name="end", status="pending",
        )
        db.add_all([external, end])
        db.flush()
        db.add(ExecutionEdgeRun(
            run_id=run.id, edge_id="finish", source_node_id="external", target_node_id="end",
        ))
        operation = db.get(ExecutionExternalOperation, data["operation_id"])
        operation.run_id, operation.node_run_id = run.id, external.id
        db.commit()
        run_id, end_id = run.id, end.id
        assert expire_stale_external_attempts(db) == 1
        db.commit()

    def finish(db, cancel):
        if cancel:
            set_run_control_status(
                db, run_id=run_id, action="cancel", actor=db.get(ExecutionUser, data["admin_id"]),
            )
        else:
            _complete(db, data)

    waiting = threading.Event()

    def second():
        with SessionLocal() as db:
            waiting.set()
            finish(db, not cancel_first)
            db.commit()

    with SessionLocal() as first, ThreadPoolExecutor(max_workers=1) as pool:
        finish(first, cancel_first)
        result = pool.submit(second)
        assert waiting.wait(timeout=5)
        assert not result.done()
        first.commit()
        result.result(timeout=15)
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).status == "cancelled"
        end = db.get(ExecutionNodeRun, end_id)
        assert end.status == "cancelled" and end.attempt_count == 0
        assert db.get(ExecutionExternalOperation, data["operation_id"]).status == "completed"
        assert db.query(ExecutionExternalAttempt).filter_by(operation_id=data["operation_id"]).count() == 1
