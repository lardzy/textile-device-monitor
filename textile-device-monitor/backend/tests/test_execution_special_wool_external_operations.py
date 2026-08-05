from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
    LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_REVIEW_NODE,
    SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES,
    _operation_stage_profile,
    allocate_legacy_sample_number,
    approve_prepared_external_operation,
    prepare_legacy_special_wool_image_operation,
    prepare_legacy_special_wool_review_operation,
    validate_external_receipt,
    validate_special_wool_machine_observation,
)
from app.execution.models import (
    ExecutionArtifact,
    ExecutionCategory,
    ExecutionCredential,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
)


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class SpecialWoolExternalOperationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.temporary = tempfile.TemporaryDirectory()
        self.staging = Path(self.temporary.name) / "staging"
        self.staging.mkdir()

        self.user = ExecutionUser(
            username="operator",
            display_name="李检验员",
            password_hash="not-used",
        )
        category = ExecutionCategory(key="electron", name="电镜")
        self.root = ExecutionStorageRoot(
            root_id="execution_staging",
            name="执行暂存区",
            local_path=str(self.staging),
            access_mode="write",
            is_active=True,
            is_available=True,
        )
        self.db.add_all([self.user, category, self.root])
        self.db.flush()
        self.workflow = ExecutionWorkflow(
            slug="special-wool-image-test",
            category_id=category.id,
            name="微观形貌测试",
            draft_definition={},
            capabilities={"external_write": True},
            created_by_id=self.user.id,
            updated_by_id=self.user.id,
        )
        self.credential = ExecutionCredential(
            user_id=self.user.id,
            system_key="legacy_inspection",
            account_name="legacy-user",
            encrypted_secret="encrypted",
        )
        self.db.add_all([self.workflow, self.credential])
        self.db.flush()
        self.run = ExecutionRun(
            workflow_id=self.workflow.id,
            created_by_id=self.user.id,
            idempotency_key="special-wool-image-run",
            inspection_number="260061860",
            definition_snapshot={
                "credential_slots": [
                    {
                        "name": "legacy_account",
                        "system_key": "legacy_inspection",
                    }
                ]
            },
            definition_checksum="d" * 64,
            capabilities_snapshot={"external_write": True},
            contract_checksum="c" * 64,
            input_data={},
        )
        self.db.add(self.run)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def _artifact(self) -> tuple[ExecutionArtifact, dict]:
        filename = "260061860-图片-纤维微观形貌原始记录.xls"
        content = OLE_MAGIC + b"controlled-test-original-record"
        path = self.staging / filename
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        artifact = ExecutionArtifact(
            run_id=self.run.id,
            storage_root_id=self.root.id,
            relative_path=filename,
            filename=filename,
            role="working",
            media_type="application/vnd.ms-excel",
            size_bytes=len(content),
            content_sha256=digest,
            immutable=True,
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact, {
            "artifact_id": artifact.id,
            "root_id": self.root.root_id,
            "relative_path": filename,
            "filename": filename,
            "content_sha256": digest,
        }

    def _node_run(self, node_type: str, node_id: str) -> ExecutionNodeRun:
        row = ExecutionNodeRun(
            run_id=self.run.id,
            node_id=node_id,
            node_type=node_type,
            node_type_version=1,
            node_name=node_id,
            status="running",
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _project_input(self, *, name: str = "纤维微观形貌") -> dict:
        project = {
            "project_key": "task-project:" + "a" * 24,
            "task_check_item_id": "sha256:" + "1" * 16,
            "check_item_id": "sha256:" + "2" * 16,
            "check_item_no": "5103.5",
            "check_item_name": name,
            "check_method": "GB/T 36422-2018",
            "seq_num": 1,
        }
        return {
            "selected_project_key": project["project_key"],
            "selected_project": project,
        }

    def _image_receipt(self, operation) -> dict:
        source = operation.request_summary["files"][0]
        main_id = "sha256:" + "3" * 16
        return {
            "schema_version": 1,
            "receipt_type": "legacy_special_wool_image_upload",
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": operation.request_summary[
                "target_sample_number"
            ],
            "source_artifact": {
                key: source[key]
                for key in (
                    "artifact_id",
                    "filename",
                    "size_bytes",
                    "content_sha256",
                )
            },
            "task_project": dict(operation.request_summary["task_project"]),
            "server_file": {
                "filename": source["filename"],
                "size_bytes": source["size_bytes"],
                "content_sha256": source["content_sha256"],
            },
            "main_record": {
                "id": main_id,
                "field_fingerprint": "4" * 64,
                "create_user": "sha256:" + "5" * 16,
                "create_time": "2026-08-05T08:00:00",
            },
            "picture_records": [
                {
                    "id": "sha256:" + "6" * 16,
                    "main_id": main_id,
                    "check_item_id": operation.request_summary[
                        "task_project"
                    ]["check_item_id"],
                    "field_fingerprint": "7" * 64,
                    "filename": source["filename"],
                    "create_time": "2026-08-05T08:00:00",
                }
            ],
            "readback": {
                "main_count": 1,
                "picture_count": 1,
                "mismatches": [],
                "verified_at": "2026-08-05T08:00:01Z",
            },
            "stages": [
                "authenticated",
                "permission_verified",
                "remote_state_verified",
                "task_project_verified",
                "file_copy_ready",
                "file_copy_started",
                "file_copy_verified",
                "main_record_save_started",
                "main_record_verified",
                "picture_child_verified",
            ],
            "reconciliation_required": False,
        }

    def test_suffix_allocation_is_deterministic(self):
        self.assertEqual(
            allocate_legacy_sample_number(
                "260061860",
                {"260061860", "260061860-1", "260061860-3"},
            ),
            "260061860-2",
        )
        with self.assertRaises(ExecutionApiError) as captured:
            allocate_legacy_sample_number("../260061860", set())
        self.assertEqual(
            captured.exception.code,
            "external_target_sample_number_invalid",
        )

    def test_image_preflight_binds_server_artifact_and_stays_disabled(self):
        artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload",
        )
        operation, reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        self.assertFalse(reused)
        summary = operation.request_summary
        self.assertEqual(
            summary["operation_type"],
            LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        )
        self.assertEqual(summary["inspector"], self.user.display_name)
        self.assertEqual(summary["business_fields"], {
            "fiber_category": "图片",
            "inspection_method": "",
            "inspection_item": "图片",
            "inspection_copies": 1,
            "review_item": "图片",
            "review_copies": 1,
        })
        self.assertEqual(summary["files"][0]["artifact_id"], artifact.id)
        self.assertEqual(
            summary["task_project"]["task_check_item_id"],
            "sha256:" + "1" * 16,
        )
        self.assertFalse(summary["execution_capability"]["available"])
        self.assertTrue(
            summary["target_allocation"][
                "legacy_readonly_verification_required"
            ]
        )
        duplicate, reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        self.assertTrue(reused)
        self.assertEqual(duplicate.id, operation.id)
        self.assertEqual(
            duplicate.request_summary["target_sample_number"],
            summary["target_sample_number"],
        )

        with self.assertRaises(ExecutionApiError) as captured:
            approve_prepared_external_operation(
                self.db,
                operation=operation,
                run=self.run,
                actor=self.user,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=summary["target_sample_number"],
            )
        self.assertEqual(
            captured.exception.code,
            "legacy_special_wool_image_write_unverified",
        )
        self.assertEqual(operation.status, "prepared")

    def test_image_preflight_rejects_missing_or_wrong_task_project(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-invalid-project",
        )
        with self.assertRaises(ExecutionApiError) as missing:
            prepare_legacy_special_wool_image_operation(
                self.db,
                run=self.run,
                node_run=node_run,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={"original_record": original_record},
            )
        self.assertEqual(
            missing.exception.code,
            "legacy_special_wool_task_project_required",
        )
        wrong = self._project_input()
        wrong["selected_project"] = {
            **wrong["selected_project"],
            "check_method": "按客户要求",
        }
        with self.assertRaises(ExecutionApiError) as mismatch:
            prepare_legacy_special_wool_image_operation(
                self.db,
                run=self.run,
                node_run=node_run,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={"original_record": original_record, **wrong},
            )
        self.assertEqual(
            mismatch.exception.code,
            "legacy_special_wool_task_project_method_mismatch",
        )

    def test_machine_observation_and_receipt_are_strict_and_bound(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-machine-contract",
        )
        operation, _ = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        project = {
            **operation.request_summary["task_project"],
            "match_count": 1,
        }
        observation = {
            "schema_version": 1,
            "observation_type": (
                "legacy_special_wool_image_upload_dry_run"
            ),
            "mode": "read_only",
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "generated_at": "2026-08-05T08:00:00Z",
            "observation_checksum": "8" * 64,
            "source_inspection_number": self.run.inspection_number,
            "target_sample_number": operation.request_summary[
                "target_sample_number"
            ],
            "target_family": {
                "base_number": self.run.inspection_number,
                "occupied_numbers": [],
                "ignored_numbers": [],
                "candidate_number": self.run.inspection_number,
                "candidate_exact_count": 0,
                "unique_sample_number_constraint": None,
            },
            "task_project": project,
            "picture_readback": {
                "main_count": 0,
                "picture_count": 0,
                "records": [],
            },
            "write_performed": False,
            "ready_for_write": False,
        }
        self.assertIs(
            validate_special_wool_machine_observation(
                operation, observation
            ),
            observation,
        )
        invalid_observation = {**observation, "unexpected": True}
        with self.assertRaises(ExecutionApiError) as invalid:
            validate_special_wool_machine_observation(
                operation, invalid_observation
            )
        self.assertEqual(
            invalid.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )

        receipt = self._image_receipt(operation)
        self.assertIs(validate_external_receipt(operation, receipt), receipt)
        changed = {
            **receipt,
            "task_project": {
                **receipt["task_project"],
                "check_method": "OTHER",
            },
        }
        with self.assertRaises(ExecutionApiError):
            validate_external_receipt(operation, changed)

    def test_review_uses_independent_stage_profile_and_completed_upload(self):
        _artifact, original_record = self._artifact()
        upload_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload",
        )
        upload, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=upload_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(name="膜平面形貌"),
            },
        )
        upload.status = "completed"
        upload.receipt = self._image_receipt(upload)
        review_node = self._node_run(
            LEGACY_SPECIAL_WOOL_REVIEW_NODE,
            "review",
        )
        review, reused = prepare_legacy_special_wool_review_operation(
            self.db,
            run=self.run,
            node_run=review_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        self.assertFalse(reused)
        stages, boundary, verified = _operation_stage_profile(review)
        self.assertEqual(stages, SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES)
        self.assertEqual(boundary, "review_save_started")
        self.assertEqual(verified, "review_children_verified")
        self.assertFalse(
            review.request_summary["execution_capability"]["available"]
        )


if __name__ == "__main__":
    unittest.main()
