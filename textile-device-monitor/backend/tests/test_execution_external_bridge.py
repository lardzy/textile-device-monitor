from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.execution import (
    AuthContext,
    approve_external_operation,
    claim_external_bridge_operation,
    complete_external_bridge_attempt,
    external_operation_reconciliation_context,
    fail_external_bridge_attempt,
    heartbeat_external_bridge_attempt,
    reconcile_external_operation_result,
    record_external_bridge_attempt_stage,
)
from app.config import settings
from app.database import Base
from app.execution.catalog import create_workflow, publish_workflow
from app.execution.engine import (
    claim_human_task,
    claim_next_node,
    create_run,
    execute_claimed_node,
    expire_stale_external_attempts,
    fail_node,
    set_run_control_status,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    BRIDGE_STDOUT_LIMIT,
    LEGACY_REGENERATED_COUNT_NODE,
)
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionCategory,
    ExecutionCredential,
    ExecutionEvent,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionStorageRoot,
    ExecutionUser,
    utcnow,
)
from app.execution.persistence import register_persistence_executors
from app.execution.schemas import (
    ExternalBridgeClaimRequest,
    ExternalBridgeCompleteRequest,
    ExternalBridgeFailRequest,
    ExternalBridgeHeartbeatRequest,
    ExternalBridgeStageRequest,
    ExternalOperationApprovalRequest,
    ExternalOperationReconciliationRequest,
)


BRIDGE_TOKEN = "bridge-test-token"
BRIDGE_ID = "bridge-01"


def external_definition() -> dict:
    return {
        "schema_version": "1.0",
        "metadata": {"name": "旧系统根数法上传预检"},
        "input_schema": {
            "type": "object",
            "properties": {
                "inspection_number": {"type": "string"},
                "target_sample_number": {
                    "type": "string",
                    "title": "目标样品编号",
                },
                "files": {"type": "array", "minItems": 1},
            },
            "required": ["inspection_number", "files"],
            "additionalProperties": False,
        },
        "global_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "root_slots": [
            {
                "root_id": "regenerated_fiber_records",
                "access": "read",
            }
        ],
        "credential_slots": [
            {
                "name": "legacy_account",
                "system_key": "legacy_inspection",
            }
        ],
        "nodes": [
            {
                "id": "start",
                "type": "core.start",
                "name": "开始",
                "config": {},
            },
            {
                "id": "read",
                "type": "result.regenerated_fiber_count_method",
                "name": "读取结果",
                "config": {},
                "input_mapping": {"files": "$.inputs.files"},
            },
            {
                "id": "select",
                "type": "human.file_selection",
                "name": "选择文件",
                "config": {
                    "require_primary": True,
                    "allow_multiple": True,
                    "allow_primary": True,
                },
                "input_mapping": {
                    "files": "$.nodes.read.output.files",
                },
            },
            {
                "id": "upload",
                "type": LEGACY_REGENERATED_COUNT_NODE,
                "name": "旧系统上传-再生纤-根数法",
                "config": {
                    "credential_slot": "legacy_account",
                    "selection_node_id": "select",
                },
                "input_mapping": {
                    "selected_files": (
                        "$.nodes.select.output.selected_files"
                    ),
                    "primary_file_id": (
                        "$.nodes.select.output.primary_file_id"
                    ),
                    "primary_file": (
                        "$.nodes.select.output.primary_file"
                    ),
                },
            },
            {
                "id": "end",
                "type": "core.end",
                "name": "结束",
                "config": {},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "read"},
            {"id": "e2", "source": "read", "target": "select"},
            {"id": "e3", "source": "select", "target": "upload"},
            {"id": "e4", "source": "upload", "target": "end"},
        ],
    }


class ExecutionExternalBridgeTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir()
        self.user = ExecutionUser(
            username="operator",
            display_name="操作员",
            password_hash="not-used",
            role="user",
        )
        self.admin = ExecutionUser(
            username="reconciliation-admin",
            display_name="对账管理员",
            password_hash="not-used",
            role="admin",
        )
        self.category = ExecutionCategory(
            key="regenerated",
            name="再生纤",
            sort_order=1,
        )
        self.root = ExecutionStorageRoot(
            root_id="regenerated_fiber_records",
            name="再生纤原始记录",
            local_path=str(self.source),
            access_mode="read",
            category_key="regenerated_fiber",
            is_active=True,
            is_available=True,
        )
        self.db.add_all([self.user, self.admin, self.category, self.root])
        self.db.flush()
        self.credential = ExecutionCredential(
            user_id=self.user.id,
            system_key="legacy_inspection",
            account_name="masked-account",
            encrypted_secret="encrypted-secret-never-returned",
            is_active=True,
        )
        self.db.add(self.credential)
        self.workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="legacy-count-preflight",
            category_id=self.category.id,
            name="旧系统根数法上传预检",
            description=None,
            definition=external_definition(),
            capabilities={
                "read": True,
                "external_write": True,
            },
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=self.workflow.id,
            expected_revision=1,
            actor=self.user,
            release_note="test",
        )
        self.db.commit()
        self.token_patch = patch.object(
            settings,
            "EXECUTION_BRIDGE_TOKEN",
            BRIDGE_TOKEN,
        )
        self.token_patch.start()
        self.enabled_patch = patch.object(
            settings,
            "EXECUTION_BRIDGE_ENABLED",
            True,
        )
        self.enabled_patch.start()

    def tearDown(self):
        self.enabled_patch.stop()
        self.token_patch.stop()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _workbook(
        self,
        *,
        filename: str = "260187115-根数法.xlsx",
        inspector: str = "真实检验员",
    ) -> tuple[Path, ExecutionFileIndexEntry, dict]:
        path = self.source / filename
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "根数法报告1"
        sheet["I8"] = inspector
        workbook.save(path)
        stat = path.stat()
        entry = ExecutionFileIndexEntry(
            storage_root_id=self.root.id,
            relative_path=path.name,
            filename=path.name,
            extension=path.suffix,
            file_kind="file",
            inspection_number="260187115",
            size_bytes=stat.st_size,
            modified_at=self.root.created_at,
            fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            metadata_json={},
            scan_generation=1,
        )
        self.db.add(entry)
        self.db.flush()
        candidate = {
            "id": entry.id,
            "root_id": self.root.root_id,
            "relative_path": entry.relative_path,
            "name": entry.filename,
            "fingerprint": entry.fingerprint,
        }
        return path, entry, candidate

    def _execute_one(self, expected_node_id: str):
        node = claim_next_node(
            self.db,
            worker_id=f"worker-{expected_node_id}",
            lease_seconds=30,
        )
        self.assertIsNotNone(node)
        self.assertEqual(node.node_id, expected_node_id)
        node_id = node.id
        lease_token = node.lease_token
        self.db.commit()
        execute_claimed_node(self.db, node_id, lease_token)
        self.db.commit()

    def _prepare_run(
        self,
        *,
        candidates: list[dict],
        selected_ids: list[str],
        primary_file_id: str,
        idempotency_key: str | None = None,
        target_sample_number: str | None = None,
    ):
        run, _duplicate = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260187115",
            input_data={"files": candidates},
            global_data={},
            idempotency_key=(
                idempotency_key
                or f"bridge-{len(candidates)}-{primary_file_id}"
            ),
            target_sample_number=target_sample_number,
        )
        self.db.commit()
        self._execute_one("start")
        self._execute_one("read")
        self._execute_one("select")
        task = (
            self.db.query(ExecutionHumanTask)
            .filter(ExecutionHumanTask.run_id == run.id)
            .one()
        )
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={
                "selected_files": selected_ids,
                "primary_file_id": primary_file_id,
            },
            actor=self.user,
        )
        self.db.commit()
        self._execute_one("upload")
        self.db.refresh(run)
        return run

    def _approve(self, operation):
        auth = AuthContext(session=None, user=self.user)
        approve_external_operation(
            operation.id,
            ExternalOperationApprovalRequest(
                approved=True,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=(
                    operation.request_summary["target_sample_number"]
                ),
            ),
            auth=auth,
            db=self.db,
        )
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")
        return operation

    def _approved_run(
        self,
        *,
        idempotency_key: str | None = None,
        filename: str = "260187115-根数法.xlsx",
        target_sample_number: str | None = None,
    ):
        _path, entry, candidate = self._workbook(filename=filename)
        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key=idempotency_key,
            target_sample_number=target_sample_number,
        )
        operation = (
            self.db.query(ExecutionExternalOperation)
            .filter(ExecutionExternalOperation.run_id == run.id)
            .one()
        )
        self._approve(operation)
        return run, operation

    def _claim(
        self,
        bridge_id: str = BRIDGE_ID,
        account_name: str = "masked-account",
    ):
        return claim_external_bridge_operation(
            ExternalBridgeClaimRequest(
                bridge_id=bridge_id,
                account_name=account_name,
            ),
            db=self.db,
            x_execution_bridge_key=BRIDGE_TOKEN,
        )

    def _heartbeat(
        self,
        attempt_id: str,
        *,
        stage: str | None = None,
        stdout_append: str | None = None,
        bridge_id: str = BRIDGE_ID,
    ):
        return heartbeat_external_bridge_attempt(
            attempt_id,
            ExternalBridgeHeartbeatRequest(
                bridge_id=bridge_id,
                stage=stage,
                stdout_append=stdout_append,
            ),
            db=self.db,
            x_execution_bridge_key=BRIDGE_TOKEN,
        )

    def _stage(
        self,
        attempt_id: str,
        stage: str,
        *,
        detail: str | None = None,
        bridge_id: str = BRIDGE_ID,
    ):
        return record_external_bridge_attempt_stage(
            attempt_id,
            ExternalBridgeStageRequest(
                bridge_id=bridge_id,
                stage=stage,
                detail=detail,
            ),
            db=self.db,
            x_execution_bridge_key=BRIDGE_TOKEN,
        )

    def _stages_until_verified(self, attempt_id: str):
        for stage in (
            "authenticated",
            "permission_verified",
            "remote_absence_verified",
            "file_copy_ready",
            "file_copy_started",
            "file_copy_verified",
            "main_record_save_started",
            "main_record_verified",
        ):
            self._stage(attempt_id, stage)

    def _complete(
        self,
        attempt_id: str,
        receipt: dict,
        *,
        bridge_id: str = BRIDGE_ID,
    ):
        return complete_external_bridge_attempt(
            attempt_id,
            ExternalBridgeCompleteRequest(
                bridge_id=bridge_id,
                receipt=receipt,
            ),
            db=self.db,
            x_execution_bridge_key=BRIDGE_TOKEN,
        )

    def _fail(
        self,
        attempt_id: str,
        stage: str,
        *,
        error_code: str | None = None,
        message: str | None = None,
        bridge_id: str = BRIDGE_ID,
    ):
        return fail_external_bridge_attempt(
            attempt_id,
            ExternalBridgeFailRequest(
                bridge_id=bridge_id,
                stage=stage,
                error_code=error_code,
                message=message,
            ),
            db=self.db,
            x_execution_bridge_key=BRIDGE_TOKEN,
        )

    def _upload_node(self, run):
        return (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="upload")
            .one()
        )

    def _fail_running_sibling(
        self,
        run,
        *,
        error_code="parallel_branch_failed",
        error_message="并行兄弟节点失败",
    ):
        sibling = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="end")
            .one()
        )
        lease_token = "parallel-sibling-lease"
        sibling.status = "running"
        sibling.attempt_count = 1
        sibling.lease_owner = "parallel-worker"
        sibling.lease_token = lease_token
        sibling.lease_expires_at = utcnow() + timedelta(minutes=5)
        self.db.add(
            ExecutionNodeAttempt(
                node_run_id=sibling.id,
                attempt_number=1,
                worker_id="parallel-worker",
                lease_token=lease_token,
                status="running",
            )
        )
        self.db.flush()
        fail_node(
            self.db,
            node_run_id=sibling.id,
            lease_token=lease_token,
            error_code=error_code,
            error_message=error_message,
        )
        self.db.commit()
        self.db.refresh(run)
        return sibling

    def _reconciliation_required_run(self, *, cancel_pending=False):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_started")
        if cancel_pending:
            set_run_control_status(
                self.db,
                run_id=run.id,
                action="cancel",
                actor=self.user,
            )
            self.db.commit()
        self._fail(
            attempt_id,
            "file_copy_started",
            error_code="transport_result_unknown",
            message="写入边界后的响应丢失",
        )
        self.db.refresh(run)
        self.db.refresh(operation)
        self.assertEqual(operation.status, "reconciliation_required")
        return run, operation, attempt_id

    def _reconciliation_payload(
        self,
        operation,
        attempt_id,
        *,
        action,
        evidence=None,
        note="已使用只读探针逐项核对",
    ):
        source_sha256 = operation.request_summary["files"][0][
            "content_sha256"
        ]
        if evidence is None and action == "confirm_completed":
            evidence = {
                "checked_at": utcnow(),
                "exact_record_count": 1,
                "remote_record_id": "manual-record-260187115",
                "business_fields_match": True,
                "inspector_match": True,
                "target_file_count": 1,
                "remote_file_sha256": source_sha256,
            }
        elif evidence is None:
            evidence = {
                "checked_at": utcnow(),
                "exact_record_count": 0,
                "contains_record_count": 0,
                "target_file_count": 0,
            }
        return ExternalOperationReconciliationRequest(
            action=action,
            attempt_id=attempt_id,
            payload_checksum=operation.payload_checksum,
            confirmed_sample_number=operation.request_summary[
                "target_sample_number"
            ],
            note=note,
            evidence=evidence,
        )

    def _reconcile(self, operation, payload, *, actor=None):
        return reconcile_external_operation_result(
            operation.id,
            payload,
            auth=AuthContext(session=None, user=actor or self.admin),
            db=self.db,
        )

    def test_claim_flips_state_and_returns_full_package(self):
        run, operation = self._approved_run()

        result = self._claim()

        self.assertTrue(result["claimed"])
        attempt_view = result["attempt"]
        self.assertEqual(attempt_view["operation_id"], operation.id)
        self.assertEqual(attempt_view["attempt_no"], 1)
        self.assertEqual(attempt_view["bridge_id"], BRIDGE_ID)
        self.assertEqual(attempt_view["status"], "in_progress")
        self.assertIsNone(attempt_view["current_stage"])
        self.assertEqual(attempt_view["checkpoints"], [])
        self.assertIsNotNone(attempt_view["lease_expires_at"])

        operation_view = result["operation"]
        self.assertEqual(operation_view["status"], "in_progress")
        files = operation_view["request_summary"]["files"]
        self.assertEqual(len(files), 1)
        file_entry = files[0]
        for key in (
            "root_id",
            "relative_path",
            "filename",
            "fingerprint",
            "content_sha256",
            "size_bytes",
            "is_primary",
        ):
            self.assertIn(key, file_entry)
        self.assertEqual(file_entry["root_id"], "regenerated_fiber_records")
        self.assertEqual(file_entry["filename"], "260187115-根数法.xlsx")
        self.assertTrue(file_entry["is_primary"])
        self.assertEqual(
            operation_view["credential"]["account_name"],
            "masked-account",
        )
        self.assertNotIn("encrypted-secret-never-returned", str(result))

        self.db.refresh(operation)
        self.assertEqual(operation.status, "in_progress")
        self.assertEqual(operation.lease_owner, BRIDGE_ID)
        self.assertIsNotNone(operation.lease_expires_at)
        self.assertEqual(operation.attempt_count, 1)
        attempt = self.db.query(ExecutionExternalAttempt).one()
        self.assertEqual(attempt.status, "in_progress")
        self.assertEqual(attempt.attempt_no, 1)
        self.assertIsNotNone(attempt.started_at)
        self.assertEqual(run.status, "waiting_external")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        event = (
            self.db.query(ExecutionEvent)
            .filter_by(
                run_id=run.id,
                event_type="external_operation.claimed",
            )
            .one()
        )
        self.assertEqual(event.actor_type, "bridge")
        self.assertEqual(event.actor_id, BRIDGE_ID)
        self.assertEqual(event.payload["attempt_id"], attempt.id)
        audit = (
            self.db.query(ExecutionAuditLog)
            .filter_by(
                action="external_operation.claim",
                resource_id=operation.id,
            )
            .one()
        )
        self.assertEqual(audit.details["attempt_no"], 1)
        self.assertNotIn(
            "encrypted-secret-never-returned",
            str(audit.details),
        )

    def test_claim_without_approved_operation_returns_false(self):
        self.assertEqual(self._claim(), {"claimed": False})

    def test_claim_is_bound_to_requested_credential_account(self):
        _run, operation = self._approved_run()

        self.assertEqual(
            self._claim(account_name="another-account"),
            {"claimed": False},
        )
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")
        self.assertEqual(self.db.query(ExecutionExternalAttempt).count(), 0)

        claimed = self._claim(account_name="  MASKED-ACCOUNT  ")
        self.assertTrue(claimed["claimed"])
        self.assertEqual(
            claimed["operation"]["credential"]["account_name"],
            "masked-account",
        )

    def test_global_capacity_declines_second_claim_while_one_is_in_progress(self):
        self._approved_run(idempotency_key="bridge-capacity-first")
        self._approved_run(
            idempotency_key="bridge-capacity-second",
            filename="260187115-第二份-根数法.xlsx",
            target_sample_number="260187115-2",
        )

        self.assertTrue(self._claim()["claimed"])
        self.assertEqual(self._claim(), {"claimed": False})
        self.assertEqual(
            sorted(
                item.status
                for item in self.db.query(ExecutionExternalOperation).all()
            ),
            ["approved", "in_progress"],
        )
        self.assertEqual(self.db.query(ExecutionExternalAttempt).count(), 1)

    def test_claim_skips_expired_approval(self):
        _run, operation = self._approved_run()
        operation.approval_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()

        self.assertEqual(self._claim(), {"claimed": False})
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")
        self.assertEqual(self.db.query(ExecutionExternalAttempt).count(), 0)

    def test_claim_accepts_automatic_approval_without_countdown(self):
        _run, operation = self._approved_run()
        operation.approval_expires_at = None
        self.db.commit()

        claimed = self._claim()

        self.assertTrue(claimed["claimed"])
        self.assertIsNone(
            claimed["operation"]["approval"]["expires_at"]
        )
        self.db.refresh(operation)
        self.assertEqual(operation.status, "in_progress")

    def test_claim_rejects_credential_revision_drift(self):
        _run, operation = self._approved_run()
        self.credential.revision += 1
        self.credential.encrypted_secret = "rotated-secret"
        self.db.commit()

        with self.assertRaises(ExecutionApiError) as changed:
            self._claim()

        self.assertEqual(
            changed.exception.code,
            "external_operation_credential_changed",
        )
        self.assertEqual(changed.exception.status_code, 409)
        self.db.rollback()
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")
        self.assertEqual(self.db.query(ExecutionExternalAttempt).count(), 0)

    def test_claim_rejects_source_file_drift(self):
        _run, operation = self._approved_run()
        path = self.source / "260187115-根数法.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "根数法报告1"
        sheet["I8"] = "另一位检验员"
        workbook.save(path)

        with self.assertRaises(ExecutionApiError) as changed:
            self._claim()

        self.assertEqual(
            changed.exception.code,
            "external_source_file_changed",
        )
        self.db.rollback()
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")

    def test_heartbeat_extends_lease_and_appends_checkpoint(self):
        _run, operation = self._approved_run()
        claimed = self._claim()
        attempt_id = claimed["attempt"]["id"]
        first_lease = claimed["attempt"]["lease_expires_at"]

        heartbeat = self._heartbeat(
            attempt_id,
            stage="authenticated",
            stdout_append="登录旧系统成功\n",
        )

        self.assertFalse(heartbeat["abort_requested"])
        attempt_view = heartbeat["attempt"]
        self.assertEqual(attempt_view["current_stage"], "authenticated")
        self.assertEqual(len(attempt_view["checkpoints"]), 1)
        checkpoint = attempt_view["checkpoints"][0]
        self.assertEqual(checkpoint["stage"], "authenticated")
        self.assertIn("at", checkpoint)
        self.assertGreaterEqual(
            attempt_view["lease_expires_at"],
            first_lease,
        )
        self.assertEqual(
            attempt_view["stdout_summary"],
            "登录旧系统成功\n",
        )

        flooded = self._heartbeat(
            attempt_id,
            stdout_append="x" * (BRIDGE_STDOUT_LIMIT + 1000),
        )
        self.assertEqual(
            len(flooded["attempt"]["stdout_summary"]),
            BRIDGE_STDOUT_LIMIT,
        )
        self.db.refresh(operation)
        attempt = self.db.get(ExecutionExternalAttempt, attempt_id)
        self.assertEqual(
            attempt.lease_expires_at,
            operation.lease_expires_at,
        )

    def test_stage_endpoint_records_detail(self):
        _run, _operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]

        result = self._stage(
            attempt_id,
            "permission_verified",
            detail="账号具备特殊纤维权限",
        )

        attempt_view = result["attempt"]
        self.assertEqual(attempt_view["current_stage"], "permission_verified")
        self.assertEqual(
            attempt_view["checkpoints"][0]["detail"],
            "账号具备特殊纤维权限",
        )
        self.assertFalse(result["abort_requested"])

        with self.assertRaises(ExecutionApiError) as invalid:
            self._stage(attempt_id, "not_a_stage")
        self.assertEqual(
            invalid.exception.code,
            "external_attempt_stage_invalid",
        )
        self.db.rollback()

    def test_attempt_current_stage_is_monotonic_across_stage_and_heartbeat(self):
        _run, _operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]

        ready = self._stage(attempt_id, "file_copy_ready")
        self.assertEqual(
            ready["attempt"]["current_stage"],
            "file_copy_ready",
        )
        stale_heartbeat = self._heartbeat(
            attempt_id,
            stage="permission_verified",
        )
        self.assertEqual(
            stale_heartbeat["attempt"]["current_stage"],
            "file_copy_ready",
        )
        stale_stage = self._stage(attempt_id, "authenticated")
        self.assertEqual(
            stale_stage["attempt"]["current_stage"],
            "file_copy_ready",
        )
        self.assertEqual(
            [
                item["stage"]
                for item in stale_stage["attempt"]["checkpoints"]
            ],
            [
                "file_copy_ready",
                "permission_verified",
                "authenticated",
            ],
        )

    def test_fail_before_copy_returns_operation_to_approved(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_ready")

        failed = self._fail(
            attempt_id,
            "file_copy_ready",
            error_code="legacy_permission_denied",
            message="账号无特殊纤维模块权限",
        )

        self.assertEqual(failed["attempt"]["status"], "failed")
        self.assertEqual(
            failed["attempt"]["error"]["code"],
            "legacy_permission_denied",
        )
        self.assertEqual(failed["operation"]["status"], "approved")
        self.db.refresh(operation)
        self.assertEqual(operation.status, "approved")
        self.assertIsNone(operation.lease_owner)
        self.assertEqual(run.status, "waiting_external")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        reclaimed = self._claim()
        self.assertTrue(reclaimed["claimed"])
        self.assertEqual(reclaimed["attempt"]["attempt_no"], 2)
        self.assertNotEqual(reclaimed["attempt"]["id"], attempt_id)

    def test_deterministic_prewrite_failures_stop_after_max_attempts(self):
        run, operation = self._approved_run(
            idempotency_key="external-attempt-cap",
        )
        for round_no in range(1, 6):
            claimed = self._claim()
            self.assertTrue(claimed["claimed"])
            attempt_id = claimed["attempt"]["id"]
            self._fail(
                attempt_id,
                "remote_absence_verified",
                error_code="target_allocation_stale",
            )
            self.db.refresh(operation)
            if round_no < 5:
                self.assertEqual(operation.status, "approved")
                self.assertEqual(run.status, "waiting_external")

        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.error_code, "target_allocation_stale")
        self.assertIn("已停止自动重试", operation.error_message)
        node = self._upload_node(run)
        self.assertEqual(node.status, "failed")
        self.assertEqual(node.error_code, "external_attempts_exhausted")
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        # 已达上限的操作不再可被领取，事件时间线可见收尾原因
        self.assertFalse(self._claim()["claimed"])
        event = (
            self.db.query(ExecutionEvent)
            .filter_by(run_id=run.id, event_type="external_operation.attempt_failed")
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        self.assertEqual(event.payload["settlement_reason"], "attempts_exhausted")

    def test_fail_after_copy_requires_reconciliation_and_keeps_fence(self):
        _path, entry, candidate = self._workbook()
        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="bridge-reconcile-first",
        )
        operation = (
            self.db.query(ExecutionExternalOperation)
            .filter(ExecutionExternalOperation.run_id == run.id)
            .one()
        )
        self._approve(operation)
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_started")

        failed = self._fail(
            attempt_id,
            "authenticated",
            error_code="sftp_link_broken",
            message="文件传输中断",
        )

        self.assertEqual(failed["attempt"]["status"], "failed")
        self.assertEqual(
            failed["attempt"]["current_stage"],
            "file_copy_started",
        )
        self.assertEqual(
            failed["operation"]["status"],
            "reconciliation_required",
        )
        self.assertIsNone(failed["operation"]["remote_write_performed"])
        self.db.refresh(operation)
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(operation.error_code, "sftp_link_broken")
        self.assertEqual(run.status, "waiting_external")
        self.assertEqual(self._upload_node(run).status, "waiting_external")
        self.assertEqual(self._claim(), {"claimed": False})

        event = (
            self.db.query(ExecutionEvent)
            .filter_by(
                run_id=run.id,
                event_type="external_operation.attempt_failed",
            )
            .one()
        )
        self.assertTrue(event.payload["reconciliation_required"])
        self.assertIsNone(event.payload["remote_write_performed"])
        audit = (
            self.db.query(ExecutionAuditLog)
            .filter_by(
                action="external_operation.fail_attempt",
                resource_id=operation.id,
            )
            .one()
        )
        self.assertIsNone(audit.details["remote_write_performed"])

        # 同一样品的业务围栏在对账前不得释放。
        second = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="bridge-reconcile-second",
        )
        self.assertEqual(second.status, "failed")
        self.assertEqual(
            self._upload_node(second).error_code,
            "external_remote_business_conflict",
        )

    def test_complete_advances_node_run_and_stores_receipt(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stages_until_verified(attempt_id)
        receipt = {
            "stage": "completed",
            "remote_record_id": "RC-260187115",
            "uploaded_files": ["260187115-根数法.xlsx"],
        }

        completed = self._complete(attempt_id, receipt)

        self.assertEqual(completed["attempt"]["status"], "completed")
        self.assertEqual(completed["attempt"]["exit_code"], 0)
        self.assertEqual(completed["operation"]["status"], "completed")
        self.assertTrue(completed["operation"]["remote_write_performed"])
        self.db.refresh(operation)
        self.assertEqual(operation.status, "completed")
        self.assertEqual(operation.receipt, receipt)
        self.assertIsNotNone(operation.completed_at)
        self.assertIsNone(operation.lease_owner)

        node = self._upload_node(run)
        self.assertEqual(node.status, "succeeded")
        self.assertEqual(node.output_data["status"], "completed")
        self.assertEqual(node.output_data["receipt"], receipt)
        node_attempt = (
            self.db.query(ExecutionNodeAttempt)
            .filter_by(node_run_id=node.id)
            .one()
        )
        self.assertEqual(node_attempt.status, "succeeded")
        self.assertEqual(run.status, "running")

        self._execute_one("end")
        self.db.refresh(run)
        self.assertEqual(run.status, "completed")

        event = (
            self.db.query(ExecutionEvent)
            .filter_by(
                run_id=run.id,
                event_type="external_operation.completed",
            )
            .one()
        )
        self.assertEqual(event.payload["attempt_id"], attempt_id)
        audit = (
            self.db.query(ExecutionAuditLog)
            .filter_by(
                action="external_operation.complete",
                resource_id=operation.id,
            )
            .one()
        )
        self.assertEqual(audit.details["attempt_no"], 1)

    def test_complete_requires_main_record_verification(self):
        _run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]

        with self.assertRaises(ExecutionApiError) as premature:
            self._complete(attempt_id, {"stage": "uploaded"})
        self.assertEqual(
            premature.exception.code,
            "external_attempt_not_verified",
        )
        self.db.rollback()

        with self.assertRaises(ExecutionApiError) as forged_receipt:
            self._complete(
                attempt_id,
                {"stage": "completed", "remote_record_id": "RC-1"},
            )
        self.assertEqual(
            forged_receipt.exception.code,
            "external_attempt_not_verified",
        )
        self.db.rollback()

        self._stages_until_verified(attempt_id)
        completed = self._complete(
            attempt_id,
            {"stage": "completed", "remote_record_id": "RC-1"},
        )
        self.assertEqual(completed["operation"]["status"], "completed")
        self.db.refresh(operation)
        self.assertEqual(operation.receipt["remote_record_id"], "RC-1")

    def test_lease_expiry_returns_pre_copy_operation_to_approved(self):
        _run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "remote_absence_verified")
        expired_at = utcnow() - timedelta(seconds=1)
        attempt = self.db.get(ExecutionExternalAttempt, attempt_id)
        attempt.lease_expires_at = expired_at
        operation.lease_expires_at = expired_at
        self.db.commit()

        self.assertEqual(expire_stale_external_attempts(self.db), 1)
        self.db.commit()
        self.db.refresh(operation)
        self.db.refresh(attempt)

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error_code, "lease_expired")
        self.assertEqual(operation.status, "approved")
        self.assertIsNone(operation.lease_expires_at)

        reclaimed = self._claim()
        self.assertTrue(reclaimed["claimed"])
        self.assertEqual(reclaimed["attempt"]["attempt_no"], 2)

    def test_lease_expiry_after_copy_requires_reconciliation(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_verified")
        expired_at = utcnow() - timedelta(seconds=1)
        attempt = self.db.get(ExecutionExternalAttempt, attempt_id)
        attempt.lease_expires_at = expired_at
        operation.lease_expires_at = expired_at
        self.db.commit()

        self.assertEqual(expire_stale_external_attempts(self.db), 1)
        self.db.commit()
        self.db.refresh(operation)
        self.db.refresh(attempt)

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error_code, "lease_expired")
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(operation.error_code, "lease_expired")
        self.assertEqual(run.status, "waiting_external")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

    def test_bridge_endpoints_require_configured_matching_token(self):
        self._approved_run()

        with patch.object(settings, "EXECUTION_BRIDGE_ENABLED", False):
            with self.assertRaises(ExecutionApiError) as disabled:
                self._claim()
        self.assertEqual(disabled.exception.status_code, 503)
        self.assertEqual(
            disabled.exception.code,
            "execution_bridge_disabled",
        )

        with patch.object(settings, "EXECUTION_BRIDGE_TOKEN", ""):
            with self.assertRaises(ExecutionApiError) as unconfigured:
                claim_external_bridge_operation(
                    ExternalBridgeClaimRequest(
                        bridge_id=BRIDGE_ID,
                        account_name="masked-account",
                    ),
                    db=self.db,
                    x_execution_bridge_key=BRIDGE_TOKEN,
                )
        self.assertEqual(unconfigured.exception.status_code, 503)
        self.assertEqual(
            unconfigured.exception.code,
            "execution_bridge_not_configured",
        )

        with self.assertRaises(ExecutionApiError) as wrong_key:
            claim_external_bridge_operation(
                ExternalBridgeClaimRequest(
                    bridge_id=BRIDGE_ID,
                    account_name="masked-account",
                ),
                db=self.db,
                x_execution_bridge_key="wrong-token",
            )
        self.assertEqual(wrong_key.exception.status_code, 401)
        self.assertEqual(
            wrong_key.exception.code,
            "execution_bridge_unauthorized",
        )

        with self.assertRaises(ExecutionApiError) as missing_key:
            claim_external_bridge_operation(
                ExternalBridgeClaimRequest(
                    bridge_id=BRIDGE_ID,
                    account_name="masked-account",
                ),
                db=self.db,
                x_execution_bridge_key=None,
            )
        self.assertEqual(missing_key.exception.status_code, 401)

        self.assertTrue(self._claim()["claimed"])

    def test_attempt_of_another_bridge_is_hidden(self):
        _run, _operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]

        with self.assertRaises(ExecutionApiError) as hidden:
            self._heartbeat(attempt_id, bridge_id="bridge-02")
        self.assertEqual(hidden.exception.status_code, 404)
        self.db.rollback()

    def test_cancel_in_progress_operation_waits_for_attempt_report(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(operation)

        self.assertEqual(operation.status, "cancel_pending")
        self.assertEqual(run.status, "cancel_pending")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        heartbeat = self._heartbeat(attempt_id)
        self.assertTrue(heartbeat["abort_requested"])

        failed = self._fail(
            attempt_id,
            "authenticated",
            error_code="abort_acknowledged",
            message="收到中止信号，未写入旧系统",
        )
        self.assertEqual(failed["attempt"]["status"], "failed")
        self.assertEqual(failed["operation"]["status"], "cancelled")
        self.db.refresh(run)
        self.db.refresh(operation)
        self.assertEqual(operation.status, "cancelled")
        self.assertEqual(operation.error_code, "run_cancelled")
        self.assertEqual(self._upload_node(run).status, "cancelled")
        self.assertEqual(run.status, "cancelled")
        self.assertIsNotNone(run.finished_at)

    def test_cancel_pending_cannot_cross_remote_write_boundary(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_ready")

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()

        with self.assertRaises(ExecutionApiError) as blocked_stage:
            self._stage(attempt_id, "file_copy_started")
        self.assertEqual(
            blocked_stage.exception.code,
            "external_attempt_cancelled_before_remote_write",
        )
        self.db.rollback()

        with self.assertRaises(ExecutionApiError) as blocked_heartbeat:
            self._heartbeat(attempt_id, stage="file_copy_verified")
        self.assertEqual(
            blocked_heartbeat.exception.code,
            "external_attempt_cancelled_before_remote_write",
        )
        self.db.rollback()

        attempt = self.db.get(ExecutionExternalAttempt, attempt_id)
        self.db.refresh(operation)
        self.assertEqual(attempt.current_stage, "file_copy_ready")
        self.assertEqual(operation.status, "cancel_pending")
        self.assertNotIn(
            "file_copy_started",
            [item["stage"] for item in (attempt.checkpoints or [])],
        )

        failed = self._fail(
            attempt_id,
            "file_copy_ready",
            error_code="abort_acknowledged",
        )
        self.assertEqual(failed["operation"]["status"], "cancelled")

    def test_cancel_then_post_copy_failure_keeps_reconciliation(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_started")

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(operation)
        self.assertEqual(operation.status, "cancel_pending")

        self._fail(
            attempt_id,
            "file_copy_started",
            error_code="sftp_link_broken",
            message="文件传输中断",
        )
        self.db.refresh(run)
        self.db.refresh(operation)
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(self._upload_node(run).status, "waiting_external")
        self.assertEqual(run.status, "cancel_pending")

    def test_admin_can_reconcile_confirmed_completion_idempotently(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        context = external_operation_reconciliation_context(
            operation.id,
            auth=AuthContext(session=None, user=self.admin),
            db=self.db,
        )
        self.assertEqual(
            context["evidence_kind"],
            "admin_attestation_v1",
        )
        self.assertIn("管理员", context["attestation_notice"])
        self.assertEqual(context["attempt"]["id"], attempt_id)
        self.assertEqual(
            context["attempt"]["current_stage"],
            "file_copy_started",
        )
        self.assertNotIn("stdout_summary", context["attempt"])

        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_completed",
        )
        resolved = self._reconcile(operation, payload)

        self.assertFalse(resolved["duplicate"])
        self.assertEqual(resolved["operation"]["status"], "completed")
        self.assertTrue(resolved["remote_write_performed"])
        self.db.refresh(operation)
        self.assertEqual(operation.status, "completed")
        self.assertEqual(
            operation.remote_record_id,
            "manual-record-260187115",
        )
        self.assertEqual(
            operation.receipt["source"],
            "manual_reconciliation",
        )
        attempt = self.db.get(ExecutionExternalAttempt, attempt_id)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(self._upload_node(run).status, "succeeded")

        duplicate = self._reconcile(operation, payload)
        self.assertTrue(duplicate["duplicate"])
        self.assertTrue(duplicate["remote_write_performed"])

        opposite = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_no_side_effect",
        )
        with self.assertRaises(ExecutionApiError) as already_resolved:
            self._reconcile(operation, opposite)
        self.assertEqual(
            already_resolved.exception.code,
            "external_reconciliation_already_resolved",
        )
        self.db.rollback()

        event = (
            self.db.query(ExecutionEvent)
            .filter_by(
                run_id=run.id,
                event_type="external_operation.reconciled",
            )
            .one()
        )
        self.assertTrue(event.payload["remote_write_performed"])
        audit = (
            self.db.query(ExecutionAuditLog)
            .filter_by(
                action="external_operation.reconcile",
                resource_id=operation.id,
            )
            .one()
        )
        self.assertEqual(audit.actor_user_id, self.admin.id)
        self.assertIn("evidence_checksum", audit.details)

    def test_confirmed_no_side_effect_fails_run_and_releases_fence(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_no_side_effect",
        )

        resolved = self._reconcile(operation, payload)

        self.assertEqual(resolved["operation"]["status"], "failed")
        self.assertFalse(resolved["remote_write_performed"])
        self.db.refresh(run)
        self.db.refresh(operation)
        self.assertEqual(run.status, "failed")
        self.assertEqual(self._upload_node(run).status, "failed")
        self.assertEqual(operation.receipt, {})
        self.assertEqual(
            operation.verification["reconciliation"]["action"],
            "confirm_no_side_effect",
        )

        source = operation.request_summary["files"][0]
        second = self._prepare_run(
            candidates=[{
                "id": source["id"],
                "root_id": source["root_id"],
                "relative_path": source["relative_path"],
                "name": source["filename"],
                "fingerprint": source["fingerprint"],
            }],
            selected_ids=[source["id"]],
            primary_file_id=source["id"],
            idempotency_key="bridge-after-no-side-effect",
        )
        self.assertEqual(second.status, "waiting_external")

    def test_cancel_pending_no_side_effect_finishes_cancelled(self):
        run, operation, attempt_id = self._reconciliation_required_run(
            cancel_pending=True
        )
        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_no_side_effect",
        )

        resolved = self._reconcile(operation, payload)

        self.assertEqual(resolved["operation"]["status"], "cancelled")
        self.assertFalse(resolved["remote_write_performed"])
        self.db.refresh(run)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(self._upload_node(run).status, "cancelled")
        self.assertFalse(
            (run.output_data or {}).get("cancelled_after_side_effect", False)
        )

    def test_cancel_pending_confirmed_completion_records_side_effect(self):
        run, operation, attempt_id = self._reconciliation_required_run(
            cancel_pending=True
        )
        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_completed",
        )

        resolved = self._reconcile(operation, payload)

        self.assertEqual(resolved["operation"]["status"], "completed")
        self.assertTrue(resolved["remote_write_performed"])
        self.db.refresh(run)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(self._upload_node(run).status, "succeeded")
        self.assertTrue(
            (run.output_data or {}).get("cancelled_after_side_effect")
        )

    def test_cancel_after_reconciliation_required_waits_for_no_effect_result(self):
        run, operation, attempt_id = self._reconciliation_required_run()

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(operation)

        self.assertEqual(run.status, "cancel_pending")
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        resolved = self._reconcile(
            operation,
            self._reconciliation_payload(
                operation,
                attempt_id,
                action="confirm_no_side_effect",
            ),
        )
        self.assertEqual(resolved["operation"]["status"], "cancelled")
        self.db.refresh(run)
        self.assertEqual(run.status, "cancelled")
        self.assertFalse(
            (run.output_data or {}).get("cancelled_after_side_effect", False)
        )

    def test_cancel_after_reconciliation_required_records_completed_write(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()

        resolved = self._reconcile(
            operation,
            self._reconciliation_payload(
                operation,
                attempt_id,
                action="confirm_completed",
            ),
        )
        self.assertEqual(resolved["operation"]["status"], "completed")
        self.db.refresh(run)
        self.assertEqual(run.status, "cancelled")
        self.assertTrue(
            (run.output_data or {}).get("cancelled_after_side_effect")
        )

    def test_sibling_failure_preserves_reconciliation_until_no_effect_result(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        self._fail_running_sibling(run)
        self.db.refresh(operation)

        self.assertEqual(run.status, "failure_pending")
        self.assertEqual(run.error_code, "parallel_branch_failed")
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        resolved = self._reconcile(
            operation,
            self._reconciliation_payload(
                operation,
                attempt_id,
                action="confirm_no_side_effect",
            ),
        )
        self.assertEqual(resolved["operation"]["status"], "failed")
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "parallel_branch_failed")
        self.assertFalse(
            (run.output_data or {}).get(
                "failed_after_external_side_effect",
                False,
            )
        )

    def test_sibling_failure_preserves_reconciliation_until_completed_result(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        self._fail_running_sibling(run)

        resolved = self._reconcile(
            operation,
            self._reconciliation_payload(
                operation,
                attempt_id,
                action="confirm_completed",
            ),
        )
        self.assertEqual(resolved["operation"]["status"], "completed")
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "parallel_branch_failed")
        self.assertTrue(
            (run.output_data or {}).get(
                "failed_after_external_side_effect"
            )
        )

    def test_sibling_failure_cancels_inflight_attempt_that_fails_prewrite(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stage(attempt_id, "file_copy_ready")

        self._fail_running_sibling(run)
        self.db.refresh(operation)
        self.assertEqual(run.status, "failure_pending")
        self.assertEqual(operation.status, "cancel_pending")
        self.assertEqual(self._upload_node(run).status, "waiting_external")

        failed = self._fail(
            attempt_id,
            "file_copy_ready",
            error_code="abort_acknowledged",
        )
        self.assertEqual(failed["operation"]["status"], "cancelled")
        self.assertEqual(
            failed["operation"]["error"]["code"],
            "run_failed",
        )
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "parallel_branch_failed")
        self.assertEqual(self._upload_node(run).error_code, "run_failed")

    def test_sibling_failure_accepts_inflight_completion_then_fails_run(self):
        run, operation = self._approved_run()
        attempt_id = self._claim()["attempt"]["id"]
        self._stages_until_verified(attempt_id)

        self._fail_running_sibling(run)
        self.db.refresh(operation)
        self.assertEqual(operation.status, "cancel_pending")

        completed = self._complete(
            attempt_id,
            {"stage": "completed", "remote_record_id": "RC-SETTLED"},
        )
        self.assertEqual(completed["operation"]["status"], "completed")
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "parallel_branch_failed")
        self.assertTrue(
            (run.output_data or {}).get(
                "failed_after_external_side_effect"
            )
        )

    def test_paused_no_side_effect_defers_run_failure(self):
        run, operation, attempt_id = self._reconciliation_required_run()
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="pause",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "paused")

        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_no_side_effect",
        )
        self._reconcile(operation, payload)

        self.db.refresh(run)
        self.assertEqual(run.status, "paused")
        self.assertEqual(self._upload_node(run).status, "failed")
        deferred = (
            self.db.query(ExecutionEvent)
            .filter_by(
                run_id=run.id,
                event_type="run.failure_deferred",
            )
            .one()
        )
        self.assertEqual(
            deferred.payload["code"],
            "external_reconciliation_no_side_effect",
        )

    def test_reconciliation_rejects_operator_and_mismatched_file_hash(self):
        _run, operation, attempt_id = self._reconciliation_required_run()
        payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_completed",
        )
        with self.assertRaises(ExecutionApiError) as denied:
            self._reconcile(operation, payload, actor=self.user)
        self.assertEqual(
            denied.exception.code,
            "external_reconciliation_admin_required",
        )
        self.db.rollback()

        invalid_payload = self._reconciliation_payload(
            operation,
            attempt_id,
            action="confirm_completed",
            evidence={
                "checked_at": utcnow(),
                "exact_record_count": 1,
                "remote_record_id": "manual-record-260187115",
                "business_fields_match": True,
                "inspector_match": True,
                "target_file_count": 1,
                "remote_file_sha256": "0" * 64,
            },
        )
        with self.assertRaises(ExecutionApiError) as incomplete:
            self._reconcile(operation, invalid_payload)
        self.assertEqual(
            incomplete.exception.code,
            "external_reconciliation_evidence_incomplete",
        )
        self.db.rollback()
        self.db.refresh(operation)
        self.assertEqual(operation.status, "reconciliation_required")

    def test_reconciliation_context_rejects_nonrequired_or_invalid_attempt(self):
        _run, approved = self._approved_run()
        with self.assertRaises(ExecutionApiError) as not_required:
            external_operation_reconciliation_context(
                approved.id,
                auth=AuthContext(session=None, user=self.admin),
                db=self.db,
            )
        self.assertEqual(
            not_required.exception.code,
            "external_reconciliation_not_required",
        )
        self.db.rollback()
        approved.status = "reconciliation_required"
        attempt = ExecutionExternalAttempt(
            operation_id=approved.id,
            attempt_no=1,
            bridge_id=BRIDGE_ID,
            status="in_progress",
            current_stage="file_copy_started",
            started_at=utcnow(),
        )
        self.db.add(attempt)
        self.db.commit()
        with self.assertRaises(ExecutionApiError) as invalid_attempt:
            external_operation_reconciliation_context(
                approved.id,
                auth=AuthContext(session=None, user=self.admin),
                db=self.db,
            )
        self.assertEqual(
            invalid_attempt.exception.code,
            "external_reconciliation_attempt_invalid",
        )


if __name__ == "__main__":
    unittest.main()
