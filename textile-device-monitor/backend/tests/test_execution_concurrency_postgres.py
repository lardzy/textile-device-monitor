"""PostgreSQL-only concurrency gates for the execution system.

Run this module against a disposable PostgreSQL database whose name ends in
``_test``::

    TEST_DATABASE_URL=postgresql://.../execution_concurrency_test \
        pytest -q tests/test_execution_concurrency_postgres.py

The tests intentionally use committed, uniquely named records because each
contender must own an independent database connection.  Cleanup is scoped to
those records; this module never drops or truncates shared test tables.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
try:
    TEST_DATABASE = make_url(TEST_DATABASE_URL) if TEST_DATABASE_URL else None
except Exception:
    TEST_DATABASE = None

if (
    TEST_DATABASE is None
    or TEST_DATABASE.get_backend_name() != "postgresql"
    or not (TEST_DATABASE.database or "").endswith("_test")
):
    pytest.skip(
        "requires an explicit disposable PostgreSQL TEST_DATABASE_URL "
        "whose database name ends in _test (SQLite is intentionally skipped)",
        allow_module_level=True,
    )


from app.config import settings
from app.database import SessionLocal, engine
from app.api.execution import (
    AuthContext,
    publish_run_mutation,
)
from app.execution.engine import (
    claim_human_task,
    claim_next_node,
    complete_node,
    renew_node_lease,
    set_run_control_status,
)
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    LEGACY_REGENERATED_COUNT_OPERATION,
    _rearm_expired_external_operation,
    lock_external_bridge_claim_capacity,
    lock_legacy_remote_business_scope,
)
from app.execution.models import (
    ExecutionArtifact,
    ExecutionArtifactRelation,
    ExecutionAuditLog,
    ExecutionCategory,
    ExecutionEdgeRun,
    ExecutionEvent,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionFileMutation,
    ExecutionHumanTask,
    ExecutionIndexJob,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionOutbox,
    ExecutionPublishReceipt,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    ExecutionWorkflow,
    utcnow,
)
from app.execution.mutation_runtime import (
    plan_file_mutation,
    prepare_file_mutation,
    verify_file_mutation,
    write_file_mutation,
)
from app.execution.mutations import FileMutationService
from app.execution.persistence import queue_refresh
from app.execution.schemas import MutationPublishRequest
from app.execution.storage import ArtifactRef
from app.execution.validation import workflow_contract_checksum


if engine.dialect.name != "postgresql":
    pytest.skip(
        "the application engine is not PostgreSQL; run this module in a "
        "separate pytest process with TEST_DATABASE_URL configured",
        allow_module_level=True,
    )


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def test_external_business_scope_uses_one_transaction_lock_per_sample():
    """A second preflight/approval must wait before taking file/op locks."""

    sample_number = _unique("pg-external-sample")
    holder = SessionLocal()
    application_name = _unique("external-business-lock")[:63]
    contender_started = threading.Event()
    try:
        expected_key = lock_legacy_remote_business_scope(
            holder,
            sample_number=sample_number,
        )

        def contend_for_same_sample():
            db = SessionLocal()
            try:
                db.execute(
                    text(
                        "SELECT set_config("
                        "'application_name', :application_name, true)"
                    ),
                    {"application_name": application_name},
                )
                contender_started.set()
                result = lock_legacy_remote_business_scope(
                    db,
                    sample_number=sample_number,
                )
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contend_for_same_sample)
            assert contender_started.wait(timeout=10)
            waiting_on_lock = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                wait_event_type = holder.execute(
                    text(
                        "SELECT wait_event_type "
                        "FROM pg_stat_activity "
                        "WHERE application_name = :application_name "
                        "AND pid <> pg_backend_pid()"
                    ),
                    {"application_name": application_name},
                ).scalar()
                if wait_event_type == "Lock":
                    waiting_on_lock = True
                    break
                time.sleep(0.05)
            assert waiting_on_lock, (
                "second transaction did not wait on the sample advisory lock"
            )
            holder.commit()
            assert future.result(timeout=10) == expected_key
    finally:
        holder.rollback()
        holder.close()


def test_external_bridge_capacity_check_uses_one_transaction_lock():
    """The claim capacity fence must remain held until its transaction ends."""

    holder = SessionLocal()
    contender = SessionLocal()
    try:
        lock_external_bridge_claim_capacity(holder)
        contender.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError):
            lock_external_bridge_claim_capacity(contender)
        contender.rollback()

        holder.commit()
        lock_external_bridge_claim_capacity(contender)
        contender.rollback()
    finally:
        contender.rollback()
        contender.close()
        holder.rollback()
        holder.close()


def test_postgres_rearms_same_fence_after_two_prewrite_failures():
    """PostgreSQL preserves attempt history while rearming the locked fence."""

    setup = SessionLocal()
    ids: dict[str, str] = {}
    now = utcnow()
    try:
        user = ExecutionUser(
            username=_unique("pg-rearm-user"),
            display_name="PostgreSQL 安全重臂测试",
            password_hash="not-used-by-this-test",
            role="user",
        )
        category = ExecutionCategory(
            key=_unique("pg-rearm-category"),
            name="PostgreSQL 安全重臂测试",
        )
        setup.add_all([user, category])
        setup.flush()
        workflow = ExecutionWorkflow(
            slug=_unique("pg-rearm-workflow"),
            category_id=category.id,
            name="PostgreSQL 安全重臂测试",
            draft_definition={
                "schema_version": "1.0",
                "nodes": [],
                "edges": [],
            },
            capabilities={"external_write": True},
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        setup.add(workflow)
        setup.flush()
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-rearm-run"),
            inspection_number="PG-REARM-TEST",
            status="running",
            definition_snapshot={"schema_version": "1.0", "nodes": [], "edges": []},
            definition_checksum="1" * 64,
            capabilities_snapshot={"external_write": True},
            contract_checksum="2" * 64,
        )
        setup.add(run)
        setup.flush()
        node = ExecutionNodeRun(
            run_id=run.id,
            node_id="external",
            node_type="external.test",
            node_type_version=1,
            node_name="PostgreSQL 安全重臂测试",
            status="running",
            attempt_count=2,
            output_data={"status": "in_progress"},
        )
        setup.add(node)
        setup.flush()
        remote_key = uuid4().hex + uuid4().hex
        operation = ExecutionExternalOperation(
            operation_key=uuid4().hex + uuid4().hex,
            run_id=run.id,
            node_run_id=node.id,
            connector_key="legacy_fibrecheck",
            credential_revision=1,
            account_scope_key=uuid4().hex + uuid4().hex,
            remote_business_key=remote_key,
            status="expired",
            payload_checksum=uuid4().hex + uuid4().hex,
            request_summary={
                "operation_type": LEGACY_REGENERATED_COUNT_OPERATION,
            },
            preflight_expires_at=now - timedelta(minutes=1),
            attempt_count=2,
            error_code="external_operation_approval_expired",
            error_message="测试批准已过期",
            started_at=now - timedelta(minutes=3),
            completed_at=now - timedelta(minutes=1),
        )
        setup.add(operation)
        setup.flush()
        for attempt_no, stage in enumerate(
            ["authenticated", "permission_verified"],
            start=1,
        ):
            setup.add(
                ExecutionExternalAttempt(
                    operation_id=operation.id,
                    attempt_no=attempt_no,
                    bridge_id=f"pg-rearm-bridge-{attempt_no}",
                    status="failed",
                    current_stage=stage,
                    checkpoints=[{"stage": stage, "at": now.isoformat()}],
                    exit_code=1,
                    error_code="controlled_prewrite_failure",
                    error_message="受控写入前失败",
                    started_at=now - timedelta(minutes=2),
                    finished_at=now - timedelta(minutes=1),
                )
            )
        setup.commit()
        ids = {
            "user_id": user.id,
            "category_id": category.id,
            "workflow_id": workflow.id,
            "run_id": run.id,
            "node_id": node.id,
            "operation_id": operation.id,
        }
    finally:
        setup.close()

    try:
        db = SessionLocal()
        try:
            # Match the production lock order: run -> node -> operation;
            # the helper then locks every historical attempt.
            run = (
                db.query(ExecutionRun)
                .filter_by(id=ids["run_id"])
                .with_for_update()
                .one()
            )
            node = (
                db.query(ExecutionNodeRun)
                .filter_by(id=ids["node_id"], run_id=run.id)
                .with_for_update()
                .one()
            )
            operation = (
                db.query(ExecutionExternalOperation)
                .filter_by(id=ids["operation_id"], run_id=run.id)
                .with_for_update()
                .one()
            )
            assert _rearm_expired_external_operation(
                db,
                operation=operation,
                run=run,
                node_run=node,
                prepared_at=now,
                preflight_expires_at=now + timedelta(minutes=15),
            )
            db.commit()
        finally:
            db.close()

        verification = SessionLocal()
        try:
            operation = verification.get(
                ExecutionExternalOperation,
                ids["operation_id"],
            )
            assert operation is not None
            assert operation.status == "prepared"
            assert operation.attempt_count == 2
            assert operation.started_at is None
            assert operation.completed_at is None
            attempts = (
                verification.query(ExecutionExternalAttempt)
                .filter_by(operation_id=operation.id)
                .order_by(ExecutionExternalAttempt.attempt_no.asc())
                .all()
            )
            assert [item.status for item in attempts] == ["failed", "failed"]
            assert [item.current_stage for item in attempts] == [
                "authenticated",
                "permission_verified",
            ]
            audit = (
                verification.query(ExecutionAuditLog)
                .filter_by(
                    action="external_operation.rearm",
                    resource_id=operation.id,
                )
                .one()
            )
            assert audit.details["prior_attempt_count"] == 2
        finally:
            verification.close()
    finally:
        cleanup = SessionLocal()
        try:
            if ids:
                cleanup.query(ExecutionOutbox).filter_by(
                    aggregate_id=ids["run_id"]
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionAuditLog).filter_by(
                    resource_id=ids["operation_id"]
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionRun).filter_by(
                    id=ids["run_id"]
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionWorkflow).filter_by(
                    id=ids["workflow_id"]
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionCategory).filter_by(
                    id=ids["category_id"]
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionUser).filter_by(
                    id=ids["user_id"]
                ).delete(synchronize_session=False)
                cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


@pytest.fixture
def ready_node_case():
    """Create one committed ready node and remove only its records afterward."""

    db = SessionLocal()
    user_id = category_id = workflow_id = run_id = node_run_id = None
    try:
        user = ExecutionUser(
            username=_unique("pg-claim-user"),
            display_name="PostgreSQL 并发领取测试",
            password_hash="not-used-by-this-test",
            role="user",
        )
        category = ExecutionCategory(
            key=_unique("pg-claim-category"),
            name="PostgreSQL 并发领取测试",
        )
        db.add_all([user, category])
        db.flush()

        workflow = ExecutionWorkflow(
            slug=_unique("pg-claim-workflow"),
            category_id=category.id,
            name="PostgreSQL 并发领取测试",
            draft_definition={"schema_version": "1.0", "nodes": [], "edges": []},
            draft_revision=1,
            capabilities={"read": True},
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        db.add(workflow)
        db.flush()

        definition_snapshot = {
            "schema_version": "1.0",
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "type_version": 1,
                    "name": "开始",
                    "config": {},
                }
            ],
            "edges": [],
        }
        capabilities_snapshot = {"read": True}
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-claim-run"),
            inspection_number=_unique("pg-claim-inspection"),
            mode="test",
            status="queued",
            definition_snapshot=definition_snapshot,
            definition_checksum="0" * 64,
            capabilities_snapshot=capabilities_snapshot,
            contract_checksum=workflow_contract_checksum(
                definition_snapshot,
                capabilities_snapshot,
            ),
            input_data={},
            global_data={},
            output_data={},
        )
        db.add(run)
        db.flush()

        node_run = ExecutionNodeRun(
            run_id=run.id,
            node_id="start",
            node_type="core.start",
            node_type_version=1,
            node_name="开始",
            status="ready",
        )
        db.add(node_run)
        db.commit()

        user_id = user.id
        category_id = category.id
        workflow_id = workflow.id
        run_id = run.id
        node_run_id = node_run.id
        yield {
            "run_id": run_id,
            "node_run_id": node_run_id,
        }
    finally:
        db.close()
        cleanup = SessionLocal()
        try:
            if run_id is not None:
                cleanup.query(ExecutionOutbox).filter(
                    ExecutionOutbox.aggregate_id == run_id
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionEvent).filter(
                    ExecutionEvent.run_id == run_id
                ).delete(synchronize_session=False)
            if node_run_id is not None:
                cleanup.query(ExecutionNodeAttempt).filter(
                    ExecutionNodeAttempt.node_run_id == node_run_id
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionNodeRun).filter(
                    ExecutionNodeRun.id == node_run_id
                ).delete(synchronize_session=False)
            if run_id is not None:
                cleanup.query(ExecutionRun).filter(
                    ExecutionRun.id == run_id
                ).delete(synchronize_session=False)
            if workflow_id is not None:
                cleanup.query(ExecutionWorkflow).filter(
                    ExecutionWorkflow.id == workflow_id
                ).delete(synchronize_session=False)
            if category_id is not None:
                cleanup.query(ExecutionCategory).filter(
                    ExecutionCategory.id == category_id
                ).delete(synchronize_session=False)
            if user_id is not None:
                cleanup.query(ExecutionUser).filter(
                    ExecutionUser.id == user_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


@pytest.fixture
def v2_exact_binding_claim_case():
    """Create one v2-ready node advertised by two exact-capability Workers."""

    db = SessionLocal()
    ids: dict[str, object] = {}
    binding_digest = "a" * 64
    worker_ids = [
        _unique("pg-v2-worker-a"),
        _unique("pg-v2-worker-b"),
    ]
    try:
        user = ExecutionUser(
            username=_unique("pg-v2-claim-user"),
            display_name="PostgreSQL v2 精确领取测试",
            password_hash="not-used-by-this-test",
            role="user",
        )
        category = ExecutionCategory(
            key=_unique("pgv2c"),
            name="PostgreSQL v2 精确领取测试",
        )
        db.add_all([user, category])
        db.flush()

        workflow = ExecutionWorkflow(
            slug=_unique("pg-v2-claim-workflow"),
            category_id=category.id,
            name="PostgreSQL v2 精确领取测试",
            draft_definition={"schema_version": "1.0", "nodes": [], "edges": []},
            draft_revision=1,
            management_mode="release_v2",
            capabilities={"read": True},
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        db.add(workflow)
        db.flush()

        definition_snapshot = {
            "schema_version": "1.0",
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "type_version": 1,
                    "name": "开始",
                    "config": {},
                }
            ],
            "edges": [],
        }
        capabilities_snapshot = {"read": True}
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-v2-claim-run"),
            inspection_number=_unique("pg-v2-claim-inspection"),
            mode="test",
            status="queued",
            definition_snapshot=definition_snapshot,
            definition_checksum="0" * 64,
            capabilities_snapshot=capabilities_snapshot,
            contract_checksum=workflow_contract_checksum(
                definition_snapshot,
                capabilities_snapshot,
            ),
            deployed_contract_checksum="f" * 64,
            input_data={},
            global_data={},
            output_data={},
        )
        db.add(run)
        db.flush()

        node_run = ExecutionNodeRun(
            run_id=run.id,
            node_id="start",
            node_type="core.start",
            node_type_version=1,
            node_name="开始",
            execution_kind="automatic",
            execution_binding_digest=binding_digest,
            status="ready",
        )
        db.add(node_run)
        now = utcnow()
        for worker_id in worker_ids:
            db.add(
                ExecutionWorkerHeartbeat(
                    worker_id=worker_id,
                    status="running",
                    started_at=now,
                    last_seen_at=now,
                    capabilities={},
                    protocol_version="2.0",
                    engine_version="2.0.0",
                    capability_digest="d" * 64,
                )
            )
            db.add(
                ExecutionWorkerNodeCapability(
                    worker_id=worker_id,
                    execution_binding_digest=binding_digest,
                    node_type="core.start",
                    node_type_version=1,
                    contract_digest="b" * 64,
                    implementation_digest="c" * 64,
                    pack_id="textile.execution-kernel",
                    pack_version="2.0.0",
                    execution_kind="automatic",
                    ready=True,
                )
            )
        db.commit()

        ids = {
            "user_id": user.id,
            "category_id": category.id,
            "workflow_id": workflow.id,
            "run_id": run.id,
            "node_run_id": node_run.id,
        }
        yield {
            **ids,
            "binding_digest": binding_digest,
            "worker_ids": worker_ids,
        }
    finally:
        db.close()
        cleanup = SessionLocal()
        try:
            run_id = ids.get("run_id")
            node_run_id = ids.get("node_run_id")
            if run_id is not None:
                cleanup.query(ExecutionOutbox).filter(
                    ExecutionOutbox.aggregate_id == run_id
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionEvent).filter(
                    ExecutionEvent.run_id == run_id
                ).delete(synchronize_session=False)
            if node_run_id is not None:
                cleanup.query(ExecutionNodeAttempt).filter(
                    ExecutionNodeAttempt.node_run_id == node_run_id
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionNodeRun).filter(
                    ExecutionNodeRun.id == node_run_id
                ).delete(synchronize_session=False)
            if run_id is not None:
                cleanup.query(ExecutionRun).filter(
                    ExecutionRun.id == run_id
                ).delete(synchronize_session=False)
            workflow_id = ids.get("workflow_id")
            if workflow_id is not None:
                cleanup.query(ExecutionWorkflow).filter(
                    ExecutionWorkflow.id == workflow_id
                ).delete(synchronize_session=False)
            category_id = ids.get("category_id")
            if category_id is not None:
                cleanup.query(ExecutionCategory).filter(
                    ExecutionCategory.id == category_id
                ).delete(synchronize_session=False)
            cleanup.query(ExecutionWorkerNodeCapability).filter(
                ExecutionWorkerNodeCapability.worker_id.in_(worker_ids)
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionWorkerHeartbeat).filter(
                ExecutionWorkerHeartbeat.worker_id.in_(worker_ids)
            ).delete(synchronize_session=False)
            user_id = ids.get("user_id")
            if user_id is not None:
                cleanup.query(ExecutionUser).filter(
                    ExecutionUser.id == user_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


def test_v2_exact_binding_is_claimed_once_under_skip_locked(
    v2_exact_binding_claim_case,
    monkeypatch,
):
    """Two compatible v2 Workers still create only one claim/attempt."""

    monkeypatch.setattr(settings, "EXECUTION_CONTRACT_MODE", "enforced")
    start = threading.Barrier(2)
    first_claimed = threading.Event()
    loser_finished = threading.Event()
    release_winner = threading.Event()

    def claim(worker_id: str):
        db = SessionLocal()
        try:
            start.wait(timeout=10)
            node = claim_next_node(db, worker_id=worker_id, lease_seconds=30)
            if node is None:
                db.commit()
                loser_finished.set()
                return None
            result = {
                "node_run_id": node.id,
                "binding_digest": node.execution_binding_digest,
                "lease_token": node.lease_token,
            }
            first_claimed.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError(
                    "competing v2 Worker did not finish while the winner held "
                    "the PostgreSQL row lock"
                )
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(claim, worker_id)
            for worker_id in v2_exact_binding_claim_case["worker_ids"]
        ]
        try:
            assert first_claimed.wait(timeout=10)
            assert loser_finished.wait(timeout=10)
        finally:
            release_winner.set()
        results = [future.result(timeout=10) for future in futures]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert successful[0]["node_run_id"] == (
        v2_exact_binding_claim_case["node_run_id"]
    )
    assert successful[0]["binding_digest"] == (
        v2_exact_binding_claim_case["binding_digest"]
    )
    assert successful[0]["lease_token"]

    verification = SessionLocal()
    try:
        node = verification.get(
            ExecutionNodeRun,
            v2_exact_binding_claim_case["node_run_id"],
        )
        assert node is not None
        assert node.status == "running"
        assert node.attempt_count == 1
        attempts = (
            verification.query(ExecutionNodeAttempt)
            .filter(ExecutionNodeAttempt.node_run_id == node.id)
            .all()
        )
        assert len(attempts) == 1
        assert attempts[0].execution_kind == "automatic"
        assert attempts[0].execution_binding_digest == (
            v2_exact_binding_claim_case["binding_digest"]
        )
    finally:
        verification.close()


def test_human_task_claim_revision_is_compare_and_swap_under_row_lock():
    """Two claimers using one revision produce exactly one durable owner."""

    setup = SessionLocal()
    ids: dict[str, str] = {}
    try:
        user = ExecutionUser(
            username=_unique("pg-human-user"),
            display_name="PostgreSQL Human CAS",
            password_hash="not-used-by-this-test",
            role="admin",
        )
        category = ExecutionCategory(
            key=_unique("pg-human-category"),
            name="PostgreSQL Human CAS",
        )
        setup.add_all([user, category])
        setup.flush()
        workflow = ExecutionWorkflow(
            slug=_unique("pg-human-workflow"),
            category_id=category.id,
            name="PostgreSQL Human CAS",
            draft_definition={"schema_version": "1.0", "nodes": [], "edges": []},
            capabilities={},
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        setup.add(workflow)
        setup.flush()
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-human-run"),
            inspection_number="PG-HUMAN-CAS",
            mode="live",
            status="waiting_human",
            definition_snapshot={"schema_version": "1.0", "nodes": [], "edges": []},
            definition_checksum="1" * 64,
            capabilities_snapshot={},
            contract_checksum="2" * 64,
            input_data={},
            global_data={},
            output_data={},
        )
        setup.add(run)
        setup.flush()
        node = ExecutionNodeRun(
            run_id=run.id,
            node_id="human",
            node_type="human.input",
            node_type_version=1,
            node_name="Human",
            status="waiting_human",
        )
        setup.add(node)
        setup.flush()
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=node.id,
            title="Human CAS",
            form_schema={"type": "object", "additionalProperties": False},
            renderer_contract={},
            status="open",
            revision=1,
        )
        setup.add(task)
        setup.commit()
        ids = {
            "user_id": user.id,
            "category_id": category.id,
            "workflow_id": workflow.id,
            "run_id": run.id,
            "node_id": node.id,
            "task_id": task.id,
        }
    finally:
        setup.close()

    start = threading.Barrier(2)
    winner_locked = threading.Event()
    release_winner = threading.Event()

    def claim():
        db = SessionLocal()
        try:
            actor = db.get(ExecutionUser, ids["user_id"])
            start.wait(timeout=10)
            try:
                task = claim_human_task(
                    db,
                    task_id=ids["task_id"],
                    expected_revision=1,
                    actor=actor,
                )
            except ExecutionApiError as exc:
                db.rollback()
                return exc.code
            winner_locked.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("Human CAS contender did not reach the row lock")
            db.commit()
            return f"claimed:{task.claimed_by_id}:{task.revision}"
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim), pool.submit(claim)]
            assert winner_locked.wait(timeout=10)
            time.sleep(0.2)
            release_winner.set()
            results = [future.result(timeout=10) for future in futures]
        assert sum(value.startswith("claimed:") for value in results) == 1
        assert results.count("human_task_revision_conflict") == 1

        verification = SessionLocal()
        try:
            task = verification.get(ExecutionHumanTask, ids["task_id"])
            assert task.status == "claimed"
            assert task.claimed_by_id == ids["user_id"]
            assert task.revision == 2
        finally:
            verification.close()
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.query(ExecutionOutbox).filter(
                ExecutionOutbox.aggregate_id == ids["run_id"]
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionEvent).filter(
                ExecutionEvent.run_id == ids["run_id"]
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionHumanTask).filter_by(id=ids["task_id"]).delete()
            cleanup.query(ExecutionNodeRun).filter_by(id=ids["node_id"]).delete()
            cleanup.query(ExecutionRun).filter_by(id=ids["run_id"]).delete()
            cleanup.query(ExecutionWorkflow).filter_by(
                id=ids["workflow_id"]
            ).delete()
            cleanup.query(ExecutionCategory).filter_by(
                id=ids["category_id"]
            ).delete()
            cleanup.query(ExecutionUser).filter_by(id=ids["user_id"]).delete()
            cleanup.commit()
        finally:
            cleanup.close()


def test_first_selected_join_is_activated_once_when_edges_finish_concurrently():
    """A late selected edge must not schedule a second join execution."""

    setup = SessionLocal()
    ids: dict[str, object] = {}
    try:
        user = ExecutionUser(
            username=_unique("pg-join-user"),
            display_name="PostgreSQL join concurrency",
            password_hash="not-used-by-this-test",
            role="admin",
        )
        category = ExecutionCategory(
            key=_unique("pg-join-category"),
            name="PostgreSQL join concurrency",
        )
        setup.add_all([user, category])
        setup.flush()
        workflow = ExecutionWorkflow(
            slug=_unique("pg-join-workflow"),
            category_id=category.id,
            name="PostgreSQL join concurrency",
            draft_definition={"schema_version": "1.0", "nodes": [], "edges": []},
            capabilities={},
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        setup.add(workflow)
        setup.flush()
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-join-run"),
            inspection_number="PG-JOIN",
            mode="live",
            status="running",
            definition_snapshot={"schema_version": "1.0", "nodes": [], "edges": []},
            definition_checksum="3" * 64,
            capabilities_snapshot={},
            contract_checksum="4" * 64,
            input_data={},
            global_data={},
            output_data={},
        )
        setup.add(run)
        setup.flush()
        sources = []
        attempts = []
        for label in ("a", "b"):
            source = ExecutionNodeRun(
                run_id=run.id,
                node_id=f"source-{label}",
                node_type="data.aggregate",
                node_type_version=1,
                node_name=label,
                status="running",
                lease_owner=f"worker-{label}",
                lease_token=f"lease-{label}",
                attempt_count=1,
            )
            setup.add(source)
            setup.flush()
            sources.append(source)
            attempts.append(
                ExecutionNodeAttempt(
                    node_run_id=source.id,
                    attempt_number=1,
                    worker_id=f"worker-{label}",
                    lease_token=f"lease-{label}",
                    status="running",
                )
            )
        join = ExecutionNodeRun(
            run_id=run.id,
            node_id="join",
            node_type="flow.join",
            node_type_version=1,
            node_name="join",
            status="pending",
        )
        setup.add(join)
        setup.add_all(attempts)
        setup.add_all(
            [
                ExecutionEdgeRun(
                    run_id=run.id,
                    edge_id=f"edge-{label}",
                    source_node_id=f"source-{label}",
                    target_node_id="join",
                    status="pending",
                    join_policy="any",
                )
                for label in ("a", "b")
            ]
        )
        setup.commit()
        ids = {
            "user_id": user.id,
            "category_id": category.id,
            "workflow_id": workflow.id,
            "run_id": run.id,
            "node_ids": [source.id for source in sources] + [join.id],
            "source_ids": [source.id for source in sources],
        }
    finally:
        setup.close()

    start = threading.Barrier(2)

    def finish(node_id: str, lease_token: str):
        db = SessionLocal()
        try:
            start.wait(timeout=10)
            complete_node(
                db,
                node_run_id=node_id,
                lease_token=lease_token,
                output_data={"value": node_id},
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(finish, ids["source_ids"][0], "lease-a"),
                pool.submit(finish, ids["source_ids"][1], "lease-b"),
            ]
            for future in futures:
                future.result(timeout=10)
        verification = SessionLocal()
        try:
            join = (
                verification.query(ExecutionNodeRun)
                .filter_by(run_id=ids["run_id"], node_id="join")
                .one()
            )
            assert join.status == "ready"
            ready_events = [
                event
                for event in verification.query(ExecutionEvent)
                .filter_by(run_id=ids["run_id"], event_type="node.ready")
                .all()
                if (event.payload or {}).get("node_id") == "join"
            ]
            assert len(ready_events) == 1
        finally:
            verification.close()
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.query(ExecutionOutbox).filter(
                ExecutionOutbox.aggregate_id == ids["run_id"]
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionEvent).filter(
                ExecutionEvent.run_id == ids["run_id"]
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionEdgeRun).filter(
                ExecutionEdgeRun.run_id == ids["run_id"]
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionNodeAttempt).filter(
                ExecutionNodeAttempt.node_run_id.in_(ids["node_ids"])
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionNodeRun).filter(
                ExecutionNodeRun.id.in_(ids["node_ids"])
            ).delete(synchronize_session=False)
            cleanup.query(ExecutionRun).filter_by(id=ids["run_id"]).delete()
            cleanup.query(ExecutionWorkflow).filter_by(
                id=ids["workflow_id"]
            ).delete()
            cleanup.query(ExecutionCategory).filter_by(
                id=ids["category_id"]
            ).delete()
            cleanup.query(ExecutionUser).filter_by(id=ids["user_id"]).delete()
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture
def index_root_case():
    """Create a unique indexable root and remove only its records afterward."""

    db = SessionLocal()
    user_id = root_pk = None
    try:
        user = ExecutionUser(
            username=_unique("pg-index-user"),
            display_name="PostgreSQL 索引并发测试",
            password_hash="not-used-by-this-test",
            role="user",
        )
        root = ExecutionStorageRoot(
            root_id=_unique("pg-index-root"),
            name="PostgreSQL 索引并发测试",
            local_path="/tmp/execution-concurrency-postgres",
            access_mode="read",
            is_active=True,
            is_available=True,
        )
        db.add_all([user, root])
        db.commit()

        user_id = user.id
        root_pk = root.id
        yield {
            "user_id": user_id,
            "root_id": root.root_id,
            "root_pk": root_pk,
        }
    finally:
        db.close()
        cleanup = SessionLocal()
        try:
            if root_pk is not None:
                cleanup.query(ExecutionIndexJob).filter(
                    ExecutionIndexJob.storage_root_id == root_pk
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionStorageRoot).filter(
                    ExecutionStorageRoot.id == root_pk
                ).delete(synchronize_session=False)
            if user_id is not None:
                cleanup.query(ExecutionUser).filter(
                    ExecutionUser.id == user_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


@pytest.fixture
def publish_cancel_case(tmp_path):
    """Prepare one verified mutation whose publish node is ready."""

    source_dir = tmp_path / "source"
    staging_dir = tmp_path / "staging"
    publish_dir = tmp_path / "publish"
    for path in (source_dir, staging_dir, publish_dir):
        path.mkdir(parents=True)
    source_path = source_dir / "source.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "原始记录"
    sheet["A1"] = "before"
    workbook.save(source_path)
    workbook.close()

    db = SessionLocal()
    ids: dict[str, object] = {}
    try:
        user = ExecutionUser(
            username=_unique("pg-publish-user"),
            display_name="PostgreSQL 发布栅栏测试",
            password_hash="not-used-by-this-test",
            role="admin",
        )
        category = ExecutionCategory(
            key=_unique("pgpc"),
            name="PostgreSQL 发布栅栏测试",
        )
        source_root = ExecutionStorageRoot(
            root_id=_unique("pg-publish-source"),
            name="并发测试源目录",
            local_path=str(source_dir),
            access_mode="read",
            is_active=True,
            is_available=True,
        )
        staging_root = ExecutionStorageRoot(
            root_id="execution_staging",
            name="并发测试工作区",
            local_path=str(staging_dir),
            access_mode="write",
            is_active=True,
            is_available=True,
        )
        publish_root = ExecutionStorageRoot(
            root_id="execution_publish",
            name="并发测试发布区",
            local_path=str(publish_dir),
            access_mode="publish",
            is_active=True,
            is_available=True,
        )
        db.add_all(
            [
                user,
                category,
                source_root,
                staging_root,
                publish_root,
            ]
        )
        db.flush()

        mutation_id = _unique("pg-publish-mutation")
        working_ref = ArtifactRef(
            "execution_staging",
            f".execution-mutations/{mutation_id}/working/source.xlsx",
        )
        target_ref = ArtifactRef(
            "execution_publish",
            f"results/{mutation_id}.xlsx",
        )
        definition = {
            "schema_version": "1.0",
            "metadata": {"name": "PostgreSQL 发布栅栏测试"},
            "input_schema": {"type": "object"},
            "global_schema": {"type": "object"},
            "root_slots": [
                {
                    "name": "source",
                    "root_id": source_root.root_id,
                    "access": "read",
                },
                {
                    "name": "staging",
                    "root_id": "execution_staging",
                    "access": "write",
                },
                {
                    "name": "publish",
                    "root_id": "execution_publish",
                    "access": "publish",
                },
            ],
            "credential_slots": [],
            "nodes": [
                {
                    "id": "copy",
                    "type": "workbook.copy",
                    "type_version": 1,
                    "name": "复制",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "write",
                    "type": "workbook.write_cells",
                    "type_version": 1,
                    "name": "写入",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "verify",
                    "type": "workbook.verify",
                    "type_version": 1,
                    "name": "核对",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "confirm",
                    "type": "human.confirm",
                    "type_version": 1,
                    "name": "确认",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "publish",
                    "type": "artifact.publish",
                    "type_version": 1,
                    "name": "发布",
                    "config": {
                        "publish_root_id": "execution_publish",
                        "confirmation_node_id": "confirm",
                    },
                    "input_mapping": {
                        "mutation_id": "$.inputs.mutation_id",
                        "working_copy": "$.inputs.working_copy",
                        "target": "$.inputs.target",
                    },
                },
            ],
            "edges": [],
        }
        capabilities = {"read": True, "write": True}
        workflow = ExecutionWorkflow(
            slug=_unique("pg-publish-workflow"),
            category_id=category.id,
            name="PostgreSQL 发布栅栏测试",
            draft_definition=definition,
            draft_revision=1,
            capabilities=capabilities,
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        db.add(workflow)
        db.flush()
        run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=user.id,
            idempotency_key=_unique("pg-publish-run"),
            inspection_number=_unique("pg-publish-inspection"),
            mode="live",
            status="running",
            definition_snapshot=definition,
            definition_checksum="1" * 64,
            capabilities_snapshot=capabilities,
            contract_checksum=workflow_contract_checksum(
                definition,
                capabilities,
            ),
            input_data={
                "mutation_id": mutation_id,
                "working_copy": working_ref.as_dict(),
                "target": target_ref.as_dict(),
            },
            global_data={},
            output_data={},
            started_at=utcnow(),
        )
        db.add(run)
        db.flush()
        node_runs = {}
        for node_id, node_type, status in (
            ("copy", "workbook.copy", "succeeded"),
            ("write", "workbook.write_cells", "succeeded"),
            ("verify", "workbook.verify", "succeeded"),
            ("confirm", "human.confirm", "succeeded"),
            ("publish", "artifact.publish", "ready"),
        ):
            node = ExecutionNodeRun(
                run_id=run.id,
                node_id=node_id,
                node_type=node_type,
                node_type_version=1,
                node_name=node_id,
                status=status,
            )
            db.add(node)
            node_runs[node_id] = node
        db.flush()

        source_ref = ArtifactRef(source_root.root_id, source_path.name)
        writes = [
            {
                "sheet": "原始记录",
                "cell": "A1",
                "value": "after",
            }
        ]
        plan_file_mutation(
            db,
            run=run,
            mutation_id=mutation_id,
            source_ref=source_ref,
            node_run_id=node_runs["copy"].id,
        )
        prepare_file_mutation(
            db,
            run=run,
            mutation_id=mutation_id,
            node_run_id=node_runs["copy"].id,
        )
        write_file_mutation(
            db,
            run=run,
            mutation_id=mutation_id,
            writes=writes,
            node_run_id=node_runs["write"].id,
            expected_working_ref=working_ref,
        )
        verification = verify_file_mutation(
            db,
            run=run,
            mutation_id=mutation_id,
            target_ref=target_ref,
            writes=writes,
            node_run_id=node_runs["verify"].id,
            expected_working_ref=working_ref,
        )
        approval_context = verification["approval_context"]
        confirm = node_runs["confirm"]
        confirm.input_data = {"approval_context": approval_context}
        confirm.output_data = {"approved": True}
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=confirm.id,
            title="确认发布",
            form_schema={
                "type": "object",
                "properties": {
                    "approved": {
                        "type": "boolean",
                        "const": True,
                    }
                },
                "required": ["approved"],
            },
            status="completed",
            revision=3,
            assigned_user_id=user.id,
            claimed_by_id=user.id,
            completed_by_id=user.id,
            result_data={"approved": True},
            completed_at=utcnow(),
        )
        db.add(task)
        db.commit()

        mutation = (
            db.query(ExecutionFileMutation)
            .filter_by(mutation_id=mutation_id)
            .one()
        )
        artifact_ids = [
            row[0]
            for row in db.query(ExecutionArtifact.id)
            .filter(ExecutionArtifact.run_id == run.id)
            .all()
        ]
        ids = {
            "user_id": user.id,
            "category_id": category.id,
            "workflow_id": workflow.id,
            "run_id": run.id,
            "node_ids": [node.id for node in node_runs.values()],
            "mutation_pk": mutation.id,
            "mutation_id": mutation_id,
            "artifact_ids": artifact_ids,
            "root_ids": [
                source_root.id,
                staging_root.id,
                publish_root.id,
            ],
        }
        yield {
            **ids,
            "target": target_ref.as_dict(),
            "approval_context": approval_context,
            "target_path": publish_dir / target_ref.relative_path,
        }
    finally:
        db.close()
        cleanup = SessionLocal()
        try:
            run_id = ids.get("run_id")
            node_ids = ids.get("node_ids") or []
            mutation_pk = ids.get("mutation_pk")
            artifact_ids = ids.get("artifact_ids") or []
            user_id = ids.get("user_id")
            if run_id is not None:
                artifact_ids = list(
                    {
                        *artifact_ids,
                        *[
                            row[0]
                            for row in cleanup.query(ExecutionArtifact.id)
                            .filter(ExecutionArtifact.run_id == run_id)
                            .all()
                        ],
                    }
                )
            if run_id is not None:
                cleanup.query(ExecutionOutbox).filter(
                    ExecutionOutbox.aggregate_id == run_id
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionEvent).filter(
                    ExecutionEvent.run_id == run_id
                ).delete(synchronize_session=False)
            if user_id is not None:
                cleanup.query(ExecutionAuditLog).filter(
                    ExecutionAuditLog.actor_user_id == user_id
                ).delete(synchronize_session=False)
            if mutation_pk is not None:
                cleanup.query(ExecutionPublishReceipt).filter(
                    ExecutionPublishReceipt.mutation_id == mutation_pk
                ).delete(synchronize_session=False)
            if artifact_ids:
                cleanup.query(ExecutionArtifactRelation).filter(
                    (
                        ExecutionArtifactRelation.parent_artifact_id.in_(
                            artifact_ids
                        )
                    )
                    | (
                        ExecutionArtifactRelation.child_artifact_id.in_(
                            artifact_ids
                        )
                    )
                ).delete(synchronize_session=False)
            if mutation_pk is not None:
                cleanup.query(ExecutionFileMutation).filter(
                    ExecutionFileMutation.id == mutation_pk
                ).delete(synchronize_session=False)
            if node_ids:
                cleanup.query(ExecutionHumanTask).filter(
                    ExecutionHumanTask.node_run_id.in_(node_ids)
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionNodeAttempt).filter(
                    ExecutionNodeAttempt.node_run_id.in_(node_ids)
                ).delete(synchronize_session=False)
                cleanup.query(ExecutionNodeRun).filter(
                    ExecutionNodeRun.id.in_(node_ids)
                ).delete(synchronize_session=False)
            if run_id is not None:
                cleanup.query(ExecutionRun).filter(
                    ExecutionRun.id == run_id
                ).delete(synchronize_session=False)
            if artifact_ids:
                cleanup.query(ExecutionArtifact).filter(
                    ExecutionArtifact.id.in_(artifact_ids)
                ).delete(synchronize_session=False)
            workflow_id = ids.get("workflow_id")
            if workflow_id is not None:
                cleanup.query(ExecutionWorkflow).filter(
                    ExecutionWorkflow.id == workflow_id
                ).delete(synchronize_session=False)
            category_id = ids.get("category_id")
            if category_id is not None:
                cleanup.query(ExecutionCategory).filter(
                    ExecutionCategory.id == category_id
                ).delete(synchronize_session=False)
            root_ids = ids.get("root_ids") or []
            if root_ids:
                cleanup.query(ExecutionStorageRoot).filter(
                    ExecutionStorageRoot.id.in_(root_ids)
                ).delete(synchronize_session=False)
            if user_id is not None:
                cleanup.query(ExecutionUser).filter(
                    ExecutionUser.id == user_id
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


def test_two_workers_claim_one_ready_node_only_once(ready_node_case):
    """A locked run/node must be skipped by the competing worker."""

    start = threading.Barrier(2)
    first_claimed = threading.Event()
    loser_finished = threading.Event()
    release_winner = threading.Event()

    def claim(worker_id: str):
        db = SessionLocal()
        try:
            start.wait(timeout=10)
            node = claim_next_node(
                db,
                worker_id=worker_id,
                lease_seconds=30,
            )
            if node is None:
                db.commit()
                loser_finished.set()
                return None

            result = {
                "node_run_id": node.id,
                "attempt_count": node.attempt_count,
                "lease_token": node.lease_token,
            }
            first_claimed.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError(
                    "competing worker did not finish while the winner held "
                    "the PostgreSQL row lock"
                )
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(claim, "postgres-worker-a"),
            pool.submit(claim, "postgres-worker-b"),
        ]
        try:
            assert first_claimed.wait(timeout=10)
            assert loser_finished.wait(timeout=10)
        finally:
            release_winner.set()
        results = [future.result(timeout=10) for future in futures]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert successful[0]["node_run_id"] == ready_node_case["node_run_id"]
    assert successful[0]["attempt_count"] == 1
    assert successful[0]["lease_token"]

    verification = SessionLocal()
    try:
        node = verification.get(
            ExecutionNodeRun,
            ready_node_case["node_run_id"],
        )
        assert node is not None
        assert node.status == "running"
        assert node.attempt_count == 1
        assert node.lease_owner in {"postgres-worker-a", "postgres-worker-b"}
        attempts = (
            verification.query(ExecutionNodeAttempt)
            .filter(
                ExecutionNodeAttempt.node_run_id
                == ready_node_case["node_run_id"]
            )
            .all()
        )
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].lease_token == node.lease_token
    finally:
        verification.close()


def test_lease_renewal_wins_race_with_retry_exhaustion_scan(
    ready_node_case,
    monkeypatch,
):
    """A renewal committed after the scan must survive the locked recheck."""

    monkeypatch.setattr(settings, "EXECUTION_NODE_MAX_ATTEMPTS", 1)
    setup = SessionLocal()
    try:
        claimed = claim_next_node(
            setup,
            worker_id="postgres-original-worker",
            lease_seconds=30,
        )
        assert claimed is not None
        node_run_id = claimed.id
        lease_token = claimed.lease_token
        claimed.lease_expires_at = utcnow() - timedelta(seconds=30)
        setup.commit()
    finally:
        setup.close()

    renewal = SessionLocal()
    application_name = _unique("lease-exhaustion-race")[:63]
    try:
        assert renew_node_lease(
            renewal,
            node_run_id=node_run_id,
            lease_token=lease_token,
            lease_seconds=300,
        )

        claim_started = threading.Event()

        def scan_for_exhaustion():
            db = SessionLocal()
            try:
                db.execute(
                    text(
                        "SELECT set_config("
                        "'application_name', :application_name, true)"
                    ),
                    {"application_name": application_name},
                )
                claim_started.set()
                result = claim_next_node(
                    db,
                    worker_id="postgres-exhaustion-scanner",
                    lease_seconds=30,
                )
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(scan_for_exhaustion)
            assert claim_started.wait(timeout=10)
            waiting_on_lock = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    wait_event_type = renewal.execute(
                        text(
                            "SELECT wait_event_type "
                            "FROM pg_stat_activity "
                            "WHERE application_name = :application_name "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"application_name": application_name},
                    ).scalar()
                    if wait_event_type == "Lock":
                        waiting_on_lock = True
                        break
                    time.sleep(0.05)
                assert waiting_on_lock, (
                    "exhaustion scanner did not reach the locked recheck"
                )
            finally:
                # Release the row with the renewed future expiry. The scanner
                # must re-read it after acquiring the lock and decline failure.
                renewal.commit()
            assert future.result(timeout=10) is None
    finally:
        renewal.rollback()
        renewal.close()

    verification = SessionLocal()
    try:
        node = verification.get(ExecutionNodeRun, node_run_id)
        run = verification.get(ExecutionRun, ready_node_case["run_id"])
        assert node is not None
        assert run is not None
        assert node.status == "running"
        assert node.lease_token == lease_token
        assert node.attempt_count == 1
        assert node.error_code is None
        assert run.status == "running"
        attempts = (
            verification.query(ExecutionNodeAttempt)
            .filter(ExecutionNodeAttempt.node_run_id == node_run_id)
            .all()
        )
        assert len(attempts) == 1
        assert attempts[0].status == "running"
        assert attempts[0].lease_token == lease_token
    finally:
        verification.close()


def test_api_publish_fence_serializes_duplicate_and_cancel(
    publish_cancel_case,
    monkeypatch,
):
    """Cancel observes the durable publish fence and waits for reconciliation."""

    fence_committed = threading.Event()
    release_publish = threading.Event()
    publish_call_count = 0
    call_count_lock = threading.Lock()
    original_publish = FileMutationService.publish

    def blocked_publish(service, *args, **kwargs):
        nonlocal publish_call_count
        with call_count_lock:
            publish_call_count += 1
        # publish_file_mutation commits the run/node/mutation fence before
        # invoking this filesystem operation.
        fence_committed.set()
        if not release_publish.wait(timeout=15):
            raise AssertionError("publish race test did not release I/O")
        return original_publish(service, *args, **kwargs)

    monkeypatch.setattr(
        FileMutationService,
        "publish",
        blocked_publish,
    )
    payload = MutationPublishRequest(
        node_id="publish",
        target=publish_cancel_case["target"],
        approval_context=publish_cancel_case["approval_context"],
    )

    def publish_once():
        db = SessionLocal()
        try:
            user = db.get(ExecutionUser, publish_cancel_case["user_id"])
            return publish_run_mutation(
                publish_cancel_case["run_id"],
                payload,
                mutation_id=publish_cancel_case["mutation_id"],
                auth=AuthContext(session=None, user=user),
                db=db,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish_once)
        try:
            assert fence_committed.wait(timeout=15)
            assert not future.done()

            duplicate = SessionLocal()
            try:
                user = duplicate.get(
                    ExecutionUser,
                    publish_cancel_case["user_id"],
                )
                with pytest.raises(ExecutionApiError) as blocked:
                    publish_run_mutation(
                        publish_cancel_case["run_id"],
                        payload,
                        mutation_id=publish_cancel_case["mutation_id"],
                        auth=AuthContext(session=None, user=user),
                        db=duplicate,
                    )
                assert (
                    getattr(blocked.value, "code", None)
                    == "mutation_publish_in_progress"
                )
                duplicate.rollback()
            finally:
                duplicate.close()

            cancellation = SessionLocal()
            try:
                user = cancellation.get(
                    ExecutionUser,
                    publish_cancel_case["user_id"],
                )
                run = set_run_control_status(
                    cancellation,
                    run_id=publish_cancel_case["run_id"],
                    action="cancel",
                    actor=user,
                )
                assert run.status == "cancel_pending"
                cancellation.commit()
            finally:
                cancellation.close()
            assert not future.done()
        finally:
            release_publish.set()
        result = future.result(timeout=15)

    assert result["reused"] is False
    assert publish_call_count == 1
    verification = SessionLocal()
    try:
        run = verification.get(
            ExecutionRun,
            publish_cancel_case["run_id"],
        )
        mutation = (
            verification.query(ExecutionFileMutation)
            .filter_by(
                mutation_id=publish_cancel_case["mutation_id"],
            )
            .one()
        )
        publish_node = (
            verification.query(ExecutionNodeRun)
            .filter_by(
                run_id=run.id,
                node_id="publish",
            )
            .one()
        )
        assert run.status == "cancelled"
        assert run.output_data["cancelled_after_side_effect"] is True
        assert publish_node.status == "succeeded"
        assert mutation.status == "published"
        assert mutation.publish_fence_token is None
        assert (
            verification.query(ExecutionPublishReceipt)
            .filter_by(mutation_id=mutation.id)
            .count()
            == 1
        )
        assert publish_cancel_case["target_path"].exists()
    finally:
        verification.close()


def test_parallel_queue_refresh_keeps_one_active_job(index_root_case):
    """The partial unique index must arbitrate concurrent refresh requests."""

    start = threading.Barrier(2)

    def enqueue():
        db = SessionLocal()
        try:
            actor = db.get(ExecutionUser, index_root_case["user_id"])
            assert actor is not None
            start.wait(timeout=10)
            job, duplicate = queue_refresh(
                db,
                root_id=index_root_case["root_id"],
                actor=actor,
            )
            result = (job.id, duplicate)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(), range(2)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sorted(duplicate for _, duplicate in results) == [False, True]

    verification = SessionLocal()
    try:
        active_jobs = (
            verification.query(ExecutionIndexJob)
            .filter(
                ExecutionIndexJob.storage_root_id
                == index_root_case["root_pk"],
                ExecutionIndexJob.status.in_(["queued", "running"]),
            )
            .all()
        )
        assert len(active_jobs) == 1
    finally:
        verification.close()
