from __future__ import annotations

import hashlib
import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.execution import (
    AuthContext,
    approve_external_operation,
    external_operation_detail,
    router,
    upsert_credential,
)
from app.database import Base
from app.execution.catalog import create_workflow, publish_workflow
from app.execution.engine import (
    claim_human_task,
    claim_next_node,
    create_run,
    execute_claimed_node,
    expire_stale_external_operations,
    set_run_control_status,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    LEGACY_REGENERATED_COUNT_NODE,
)
from app.execution.models import (
    ExecutionCategory,
    ExecutionCredential,
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
    CredentialUpsert,
    ExternalOperationApprovalRequest,
)
from app.execution.validation import validate_definition


def external_definition() -> dict:
    return {
        "schema_version": "1.0",
        "metadata": {"name": "旧系统根数法上传预检"},
        "input_schema": {
            "type": "object",
            "properties": {
                "inspection_number": {"type": "string"},
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


class ExecutionExternalOperationTests(unittest.TestCase):
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
        self.db.add_all([self.user, self.category, self.root])
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

    def tearDown(self):
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
        before_external=None,
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
                or f"external-{len(candidates)}-{primary_file_id}"
            ),
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
        if before_external is not None:
            before_external(run)
            self.db.commit()
        self._execute_one("upload")
        self.db.refresh(run)
        return run

    def test_worker_only_prepares_durable_operation_and_rereads_i8(self):
        path, entry, candidate = self._workbook()
        original_bytes = path.read_bytes()
        original_stat = path.stat()

        def forge_upstream_inspector(run):
            select = (
                self.db.query(ExecutionNodeRun)
                .filter_by(run_id=run.id, node_id="select")
                .one()
            )
            selected = deepcopy(select.output_data["selected_files"])
            selected[0]["result"]["inspector"]["name"] = "伪造上游姓名"
            select.output_data = {
                **select.output_data,
                "selected_files": selected,
                "primary_file": selected[0],
            }

        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            before_external=forge_upstream_inspector,
        )

        operation = self.db.query(ExecutionExternalOperation).one()
        node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="upload")
            .one()
        )
        attempt = (
            self.db.query(ExecutionNodeAttempt)
            .filter_by(node_run_id=node.id)
            .one()
        )
        self.assertEqual(run.status, "waiting_external")
        self.assertEqual(node.status, "waiting_external")
        self.assertEqual(attempt.status, "waiting_external")
        self.assertEqual(operation.status, "prepared")
        self.assertEqual(operation.credential_id, self.credential.id)
        self.assertEqual(
            operation.credential_revision,
            self.credential.revision,
        )
        self.assertEqual(len(operation.account_scope_key), 64)
        self.assertEqual(len(operation.remote_business_key), 64)
        self.assertIsNotNone(operation.preflight_expires_at)
        self.assertIsNone(operation.approval_expires_at)
        self.assertEqual(
            operation.request_summary["inspector"],
            "真实检验员",
        )
        self.assertEqual(
            operation.request_summary["files"][0]["content_sha256"],
            hashlib.sha256(original_bytes).hexdigest(),
        )
        self.assertFalse(
            operation.request_summary["safety"]["remote_write_performed"]
        )
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(path.stat().st_size, original_stat.st_size)
        self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertIsNone(operation.fence_token)
        self.assertEqual(operation.receipt, {})
        self.assertIsNone(operation.remote_record_id)

    def test_updating_credential_increments_revision(self):
        original_revision = self.credential.revision
        auth = AuthContext(session=None, user=self.user)

        with patch(
            "app.api.execution.encrypt_credential",
            return_value="rotated-encrypted-secret",
        ):
            upsert_credential(
                "legacy_inspection",
                CredentialUpsert(
                    account_name="masked-account",
                    secret="rotated-secret",
                ),
                auth=auth,
                db=self.db,
            )

        self.db.refresh(self.credential)
        self.assertEqual(self.credential.revision, original_revision + 1)
        self.assertEqual(
            self.credential.encrypted_secret,
            "rotated-encrypted-secret",
        )

    def test_node_contract_requires_server_issued_selection_and_legacy_slot(self):
        valid = validate_definition(external_definition(), for_publish=True)
        self.assertTrue(valid.valid, [item.as_dict() for item in valid.issues])

        invalid_definition = deepcopy(external_definition())
        upload = next(
            item
            for item in invalid_definition["nodes"]
            if item["id"] == "upload"
        )
        upload["input_mapping"]["selected_files"] = "$.inputs.files"
        invalid_definition["credential_slots"][0]["system_key"] = (
            "new_inspection"
        )
        invalid = validate_definition(
            invalid_definition,
            for_publish=True,
        )
        codes = {item.code for item in invalid.issues}
        self.assertIn("legacy_selected_files_mapping_invalid", codes)
        self.assertIn("legacy_credential_slot_invalid", codes)

    def test_preflight_uses_file_magic_when_ooxml_is_named_xls(self):
        _path, entry, candidate = self._workbook(
            filename="260187115-renamed.xls",
            inspector="后缀改名检验员",
        )

        self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )

        operation = self.db.query(ExecutionExternalOperation).one()
        self.assertEqual(operation.status, "prepared")
        self.assertEqual(
            operation.request_summary["inspector"],
            "后缀改名检验员",
        )

    def test_router_exposes_preflight_view_and_approval_only(self):
        paths = {
            (method, route.path)
            for route in router.routes
            for method in route.methods
        }
        self.assertIn(
            (
                "GET",
                "/execution/v1/runs/{run_id}/external-operations",
            ),
            paths,
        )
        self.assertIn(
            (
                "GET",
                "/execution/v1/external-operations/{operation_id}",
            ),
            paths,
        )
        self.assertIn(
            (
                "POST",
                (
                    "/execution/v1/external-operations/"
                    "{operation_id}/approve"
                ),
            ),
            paths,
        )
        self.assertFalse(
            any(
                "submit" in path and "external-operations" in path
                for _method, path in paths
            )
        )

    def test_all_selected_files_are_reread_and_inspectors_must_match(self):
        _path1, entry1, candidate1 = self._workbook(
            filename="260187115-a.xlsx",
            inspector="张三",
        )
        _path2, entry2, candidate2 = self._workbook(
            filename="260187115-b.xlsx",
            inspector="李四",
        )

        run = self._prepare_run(
            candidates=[candidate1, candidate2],
            selected_ids=[entry1.id, entry2.id],
            primary_file_id=entry1.id,
        )

        node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="upload")
            .one()
        )
        self.assertEqual(run.status, "failed")
        self.assertEqual(node.status, "failed")
        self.assertEqual(node.error_code, "external_inspector_conflict")
        self.assertEqual(
            self.db.query(ExecutionExternalOperation).count(),
            0,
        )

    def test_approval_only_changes_local_state_and_is_idempotent(self):
        _path, entry, candidate = self._workbook()
        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        auth = AuthContext(session=None, user=self.user)

        with self.assertRaises(ExecutionApiError) as stale:
            approve_external_operation(
                operation.id,
                ExternalOperationApprovalRequest(
                    approved=True,
                    payload_checksum="0" * 64,
                    confirmed_sample_number="260187115",
                ),
                auth=auth,
                db=self.db,
            )
        self.assertEqual(stale.exception.code, "external_operation_payload_changed")
        self.db.rollback()

        first = approve_external_operation(
            operation.id,
            ExternalOperationApprovalRequest(
                approved=True,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number="260187115",
                note="已核对文件与字段",
            ),
            auth=auth,
            db=self.db,
        )
        second = approve_external_operation(
            operation.id,
            ExternalOperationApprovalRequest(
                approved=True,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number="260187115",
            ),
            auth=auth,
            db=self.db,
        )

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["operation"]["status"], "approved")
        self.assertIsNotNone(
            second["operation"]["preflight_expires_at"]
        )
        self.assertIsNotNone(
            second["operation"]["approval"]["expires_at"]
        )
        approval_lifetime = (
            operation.approval_expires_at - operation.approved_at
        )
        self.assertEqual(approval_lifetime, timedelta(minutes=15))
        self.assertFalse(second["remote_write_performed"])
        self.assertEqual(run.status, "waiting_external")
        self.assertIsNone(operation.fence_token)
        self.assertEqual(operation.receipt, {})
        public = external_operation_detail(
            operation.id,
            auth=auth,
            db=self.db,
        )
        self.assertNotIn("credential_id", public)
        self.assertNotIn("credential_revision", public)
        self.assertNotIn("account_scope_key", public)
        self.assertNotIn("remote_business_key", public)
        self.assertNotIn("receipt", public)
        self.assertNotIn("lease_owner", public)
        self.assertNotIn("encrypted-secret-never-returned", str(public))
        self.assertNotIn("masked-account", str(public))

    def test_server_rejects_wrong_confirmed_sample_number(self):
        _path, entry, candidate = self._workbook()
        self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        auth = AuthContext(session=None, user=self.user)

        with self.assertRaises(ExecutionApiError) as mismatch:
            approve_external_operation(
                operation.id,
                ExternalOperationApprovalRequest(
                    approved=True,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number="260187115 ",
                ),
                auth=auth,
                db=self.db,
            )

        self.assertEqual(
            mismatch.exception.code,
            "external_operation_sample_confirmation_mismatch",
        )
        self.db.rollback()
        self.db.refresh(operation)
        self.assertEqual(operation.status, "prepared")

    def test_approval_rejects_credential_revision_drift(self):
        _path, entry, candidate = self._workbook()
        self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        self.credential.revision += 1
        self.credential.encrypted_secret = "rotated-secret"
        self.db.commit()
        auth = AuthContext(session=None, user=self.user)

        with self.assertRaises(ExecutionApiError) as changed:
            approve_external_operation(
                operation.id,
                ExternalOperationApprovalRequest(
                    approved=True,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number="260187115",
                ),
                auth=auth,
                db=self.db,
            )

        self.assertEqual(
            changed.exception.code,
            "external_operation_credential_changed",
        )
        self.db.rollback()

    def test_approval_rejects_source_file_drift(self):
        path, entry, candidate = self._workbook()
        self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "根数法报告1"
        sheet["I8"] = "另一位检验员"
        workbook.save(path)
        auth = AuthContext(session=None, user=self.user)

        with self.assertRaises(ExecutionApiError) as changed:
            approve_external_operation(
                operation.id,
                ExternalOperationApprovalRequest(
                    approved=True,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number="260187115",
                ),
                auth=auth,
                db=self.db,
            )

        self.assertEqual(
            changed.exception.code,
            "external_source_file_changed",
        )
        self.db.rollback()

    def test_approval_rejects_expired_preflight(self):
        _path, entry, candidate = self._workbook()
        self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        operation.preflight_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()
        auth = AuthContext(session=None, user=self.user)

        with self.assertRaises(ExecutionApiError) as expired:
            approve_external_operation(
                operation.id,
                ExternalOperationApprovalRequest(
                    approved=True,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number="260187115",
                ),
                auth=auth,
                db=self.db,
            )

        self.assertEqual(
            expired.exception.code,
            "external_operation_preflight_expired",
        )
        self.db.rollback()

    def test_same_remote_business_key_conflicts_across_runs(self):
        _path, entry, candidate = self._workbook()
        first = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="external-remote-conflict-first",
        )
        second = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="external-remote-conflict-second",
        )

        operations = (
            self.db.query(ExecutionExternalOperation)
            .order_by(ExecutionExternalOperation.created_at.asc())
            .all()
        )
        second_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=second.id, node_id="upload")
            .one()
        )
        self.assertEqual(first.status, "waiting_external")
        self.assertEqual(second.status, "failed")
        self.assertEqual(
            second_node.error_code,
            "external_remote_business_conflict",
        )
        self.assertEqual(len(operations), 1)

    def test_expired_preflight_is_closed_and_releases_remote_business_key(self):
        _path, entry, candidate = self._workbook()
        first = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="external-expired-first",
        )
        first_operation = self.db.query(ExecutionExternalOperation).one()
        first_operation.preflight_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()

        # Worker maintenance closes the old waiting run before scanning for
        # unrelated work, so the same business key is no longer fenced.
        self.assertIsNone(
            claim_next_node(
                self.db,
                worker_id="external-expiry-maintenance",
            )
        )
        self.db.commit()
        self.db.refresh(first)
        self.db.refresh(first_operation)
        first_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=first.id, node_id="upload")
            .one()
        )
        first_attempt = (
            self.db.query(ExecutionNodeAttempt)
            .filter_by(node_run_id=first_node.id)
            .one()
        )
        self.assertEqual(first_operation.status, "expired")
        self.assertEqual(first.status, "failed")
        self.assertEqual(first_node.status, "failed")
        self.assertEqual(first_attempt.status, "failed")

        second = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="external-expired-second",
        )
        operations = (
            self.db.query(ExecutionExternalOperation)
            .order_by(ExecutionExternalOperation.created_at.asc())
            .all()
        )
        self.assertEqual(second.status, "waiting_external")
        self.assertEqual(
            [operation.status for operation in operations],
            ["expired", "prepared"],
        )

    def test_expired_approval_is_closed_by_worker_maintenance(self):
        _path, entry, candidate = self._workbook()
        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
            idempotency_key="external-expired-approval",
        )
        operation = self.db.query(ExecutionExternalOperation).one()
        auth = AuthContext(session=None, user=self.user)
        approve_external_operation(
            operation.id,
            ExternalOperationApprovalRequest(
                approved=True,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number="260187115",
            ),
            auth=auth,
            db=self.db,
        )
        operation.approval_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()

        self.assertEqual(expire_stale_external_operations(self.db), 1)
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(operation)

        self.assertEqual(operation.status, "expired")
        self.assertEqual(
            operation.error_code,
            "external_operation_approval_expired",
        )
        self.assertEqual(run.status, "failed")

    def test_cancelling_run_revokes_prepared_operation(self):
        _path, entry, candidate = self._workbook()
        run = self._prepare_run(
            candidates=[candidate],
            selected_ids=[entry.id],
            primary_file_id=entry.id,
        )
        operation = self.db.query(ExecutionExternalOperation).one()

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(operation)

        self.assertEqual(operation.status, "cancelled")
        self.assertEqual(operation.error_code, "run_cancelled")
        self.assertIsNone(operation.fence_token)
        self.assertEqual(operation.receipt, {})


if __name__ == "__main__":
    unittest.main()
