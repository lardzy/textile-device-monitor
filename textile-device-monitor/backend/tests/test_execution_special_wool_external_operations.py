from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
    MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES,
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
    LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
    LEGACY_SPECIAL_WOOL_REVIEW_NODE,
    SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES,
    SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES,
    _final_entry_reconciliation_expectations,
    _operation_stage_profile,
    _rearm_expired_external_operation,
    _rearm_reconciled_no_side_effect_operation,
    _validate_final_entry_reconciliation_evidence,
    allocate_legacy_sample_number,
    approve_prepared_external_operation,
    bridge_external_operation,
    prepare_legacy_microscopy_check_record_entry_operation,
    prepare_legacy_special_wool_image_operation,
    prepare_legacy_special_wool_review_operation,
    public_external_operation,
    public_external_reconciliation_context,
    reconcile_external_operation,
    validate_external_receipt,
    validate_special_wool_machine_observation,
)
from app.execution.microscopy_check_record import (
    MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
)
from app.execution.microscopy_original_record import (
    MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.models import (
    ExecutionArtifact,
    ExecutionCategory,
    ExecutionCredential,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionTaskSnapshotCache,
    ExecutionUser,
    ExecutionWorkflow,
    utcnow,
)
from app.execution.schemas import ExternalOperationReconciliationRequest


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
        project = self._project_input()["selected_project"]
        self.task_snapshot = ExecutionTaskSnapshotCache(
            inspection_number=self.run.inspection_number,
            status="ready",
            snapshot={
                "schema_version": 5,
                "inspection_number": self.run.inspection_number,
                "sample_name": "测试样品",
                "sample_names": ["测试样品"],
                "check_basis": "---",
                "projects": [{**project, "register_count": 0}],
                "special_wool_occupied_numbers": [],
            },
            fetched_at=datetime.now(timezone.utc),
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        self.db.add(self.task_snapshot)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def _artifact(self) -> tuple[ExecutionArtifact, dict]:
        filename = (
            f"{self.run.inspection_number}-"
            f"{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}"
        )
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
            metadata_json={
                "template_original_filename": (
                    MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME
                ),
            },
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

    def _expired_final_entry_operation(
        self,
        *,
        node_id: str,
        stages: list[str],
    ) -> tuple[ExecutionNodeRun, ExecutionExternalOperation]:
        now = utcnow()
        node = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            node_id,
        )
        node.attempt_count = 2
        operation = ExecutionExternalOperation(
            operation_key=hashlib.sha256(
                f"rearm:{node_id}".encode("utf-8")
            ).hexdigest(),
            run_id=self.run.id,
            node_run_id=node.id,
            connector_key="legacy_fibrecheck",
            credential_id=self.credential.id,
            credential_revision=self.credential.revision,
            account_scope_key=hashlib.sha256(
                f"account:{node_id}".encode("utf-8")
            ).hexdigest(),
            remote_business_key=hashlib.sha256(
                f"remote:{node_id}".encode("utf-8")
            ).hexdigest(),
            status="expired",
            payload_checksum=hashlib.sha256(
                f"payload:{node_id}".encode("utf-8")
            ).hexdigest(),
            request_summary={
                "operation_type": (
                    "legacy_microscopy_check_record_entry"
                ),
            },
            preflight_expires_at=now - timedelta(minutes=2),
            attempt_count=len(stages),
            error_code="external_operation_approval_expired",
            started_at=now - timedelta(minutes=3),
            completed_at=now - timedelta(minutes=1),
        )
        self.db.add(operation)
        self.db.flush()
        for attempt_no, stage in enumerate(stages, start=1):
            self.db.add(
                ExecutionExternalAttempt(
                    operation_id=operation.id,
                    attempt_no=attempt_no,
                    bridge_id=f"bridge-{node_id}-{attempt_no}",
                    status="failed",
                    current_stage=stage,
                    checkpoints=[{"stage": stage, "at": now.isoformat()}],
                    exit_code=1,
                    error_code="controlled_prewrite_failure",
                    started_at=now - timedelta(minutes=2),
                    finished_at=now - timedelta(minutes=1),
                )
            )
        self.db.flush()
        return node, operation

    def _project_input(self, *, name: str = "纤维微观形貌") -> dict:
        project = {
            "task_check_item_id": "sha256:" + "1" * 16,
            "check_item_id": "sha256:" + "2" * 16,
            "check_item_no": "5103.5",
            "check_item_name": name,
            "check_method": "GB/T 36422-2018",
            "seq_num": 1,
            "check_count": 1,
            "register_count": 0,
            "sample_identify": "纵向",
            "give_judgement": 0,
            "remark": "",
        }
        identity = "\0".join(
            str(project[key])
            for key in (
                "task_check_item_id",
                "check_item_id",
                "check_item_no",
                "check_item_name",
                "check_method",
                "seq_num",
            )
        )
        project["project_key"] = "task-project:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:24]
        return {
            "selected_project_key": project["project_key"],
            "selected_project": project,
        }

    def _image_receipt(
        self,
        operation,
        *,
        verification_mode: str = "exact_sha256",
    ) -> dict:
        source = operation.request_summary["files"][0]
        target_filename = operation.request_summary["target_filename"]
        main_id = "sha256:" + "3" * 16
        remote_sha256 = source["content_sha256"]
        changed_record_ids: list[str] = []
        changed_record_count = 0
        if verification_mode == "cfb_biff_writeaccess_only":
            remote_sha256 = hashlib.sha256(
                (source["content_sha256"] + ":remote").encode("ascii")
            ).hexdigest()
            changed_record_ids = ["0x005C"]
            changed_record_count = 1
        return {
            "schema_version": 1,
            "receipt_type": "legacy_special_wool_image_upload",
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": operation.request_summary[
                "target_sample_number"
            ],
            "target_filename": target_filename,
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
                "filename": target_filename,
                "size_bytes": source["size_bytes"],
                "content_sha256": remote_sha256,
                "verification": {
                    "mode": verification_mode,
                    "source_size_bytes": source["size_bytes"],
                    "source_content_sha256": source["content_sha256"],
                    "remote_size_bytes": source["size_bytes"],
                    "remote_content_sha256": remote_sha256,
                    "stream_paths_equal": True,
                    "stream_sizes_equal": True,
                    "non_workbook_streams_equal": True,
                    "biff_record_boundaries_equal": True,
                    "changed_record_ids": changed_record_ids,
                    "changed_record_count": changed_record_count,
                },
            },
            "main_record": {
                "id": main_id,
                "field_fingerprint": "4" * 64,
                "create_user": "sha256:" + "5" * 16,
                "create_time": "2026-08-05T08:00:00",
                "file_path": target_filename,
            },
            "picture_records": [
                {
                    "id": "sha256:" + "6" * 16,
                    "main_id": main_id,
                    "check_item_id": operation.request_summary[
                        "task_project"
                    ]["check_item_id"],
                    "field_fingerprint": "7" * 64,
                    "filename": target_filename,
                    "original_data_filename": target_filename,
                    "create_time": "2026-08-05T08:00:00",
                }
            ],
            "readback": {
                "main_count": 1,
                "picture_count": 1,
                "mismatches": [],
                "target_filename": target_filename,
                "original_data_filename": target_filename,
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

    def _reconciliation_required_image_upload(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-reconciliation",
        )
        operation, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        operation.status = "reconciliation_required"
        operation.attempt_count = 1
        node_run.status = "waiting_external"
        attempt = ExecutionExternalAttempt(
            operation_id=operation.id,
            attempt_no=1,
            bridge_id="special-wool-reconciliation-test",
            status="failed",
            current_stage="file_copy_started",
            checkpoints=[{"stage": "file_copy_started"}],
            error_code="transport_result_unknown",
            error_message="写入边界后的响应丢失",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        self.db.add(attempt)
        self.user.role = "admin"
        self.db.flush()
        return operation, attempt

    def _completed_reconciliation_evidence(
        self,
        operation,
        *,
        receipt: dict | None,
        remote_record_id: str | None = None,
    ) -> dict:
        source = operation.request_summary["files"][0]
        remote_file_sha256 = (
            receipt["server_file"]["content_sha256"]
            if isinstance(receipt, dict)
            else source["content_sha256"]
        )
        evidence = {
            "checked_at": datetime.now(timezone.utc),
            "exact_record_count": 1,
            "remote_record_id": (
                remote_record_id
                or (
                    receipt["main_record"]["id"]
                    if isinstance(receipt, dict)
                    else "sha256:" + "3" * 16
                )
            ),
            "business_fields_match": True,
            "inspector_match": True,
            "target_file_count": 1,
            "remote_file_sha256": remote_file_sha256,
        }
        if receipt is not None:
            evidence["receipt"] = receipt
        return evidence

    def _completed_review(self, *, project_input: dict | None = None):
        project_input = project_input or self._project_input()
        _artifact, original_record = self._artifact()
        upload_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-final-entry-source",
        )
        upload, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=upload_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={"original_record": original_record, **project_input},
        )
        upload.receipt = self._image_receipt(upload)
        upload.status = "completed"
        review_node = self._node_run(
            LEGACY_SPECIAL_WOOL_REVIEW_NODE,
            "review-final-entry-source",
        )
        review, _reused = prepare_legacy_special_wool_review_operation(
            self.db,
            run=self.run,
            node_run=review_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        main_id = upload.receipt["main_record"]["id"]
        children_fingerprint = "8" * 64
        review.receipt = {
            "schema_version": 1,
            "receipt_type": "legacy_special_wool_review",
            "operation_id": review.id,
            "payload_checksum": review.payload_checksum,
            "target_sample_number": review.request_summary[
                "target_sample_number"
            ],
            "source_upload": {
                "operation_id": review.request_summary["source_operation"][
                    "operation_id"
                ],
                "receipt_checksum": review.request_summary[
                    "source_operation"
                ]["receipt_checksum"],
                "main_id": main_id,
            },
            "main_record": {
                "id": main_id,
                "review_user": "sha256:" + "9" * 16,
                "review_time": "2026-08-05T08:30:00Z",
                "pre_fingerprint": "a" * 64,
                "post_fingerprint": "b" * 64,
            },
            "children": {
                "picture_count": 1,
                "before_fingerprint": children_fingerprint,
                "after_fingerprint": children_fingerprint,
                "unchanged": True,
            },
            "readback": {
                "main_count": 1,
                "mismatches": [],
                "verified_at": "2026-08-05T08:30:01Z",
            },
            "stages": list(SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES),
            "reconciliation_required": False,
        }
        review.status = "completed"
        self.db.flush()
        validate_external_receipt(review, review.receipt)
        return review

    def _check_record_artifact(self, *, sample_identity: str = "纵向"):
        binding = resolve_microscopy_legacy_template_binding(1)
        filename = "260061860-纤维微观形貌-检验记录登记.xls"
        content = OLE_MAGIC + b"controlled-check-record"
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
            metadata_json={
                "generator_version": MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
                "template_binding": binding,
                "expected_key_identities": [sample_identity],
                "verification": {
                    "cells": {
                        "AS4": "260061860",
                        "Z7": sample_identity,
                        "I8": "GB/T 36422-2018",
                    }
                },
            },
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact, binding, {
            "artifact_id": artifact.id,
            "root_id": self.root.root_id,
            "relative_path": filename,
            "filename": filename,
            "size_bytes": len(content),
            "content_sha256": digest,
        }

    def _final_entry_receipt(self, operation, *, controlled=None):
        summary = operation.request_summary
        source = summary["files"][0]
        expected_existing = summary["final_entry_package"][
            "expected_existing_register_count"
        ]
        return {
            "schema_version": 1,
            "receipt_type": "legacy_microscopy_check_record_entry",
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": summary["target_sample_number"],
            "source_artifact": {
                key: source[key]
                for key in (
                    "artifact_id",
                    "filename",
                    "size_bytes",
                    "content_sha256",
                )
            },
            "task_project": dict(summary["task_project"]),
            "template_binding": dict(summary["template_binding"]),
            "final_entry": {
                "package_schema_version": 2,
                "expected_existing_register_count": expected_existing,
                "resulting_register_count": expected_existing + 1,
                "key_result_count": 1,
                "record_id": "sha256:" + "c" * 16,
                "original_data_filename": (
                    "12345678-1234-1234-1234-123456789abc.xls"
                ),
                "content_sha256": source["content_sha256"],
                "proofed": True,
            },
            "controlled_test_override": controlled,
            "stages": list(MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES),
            "reconciliation_required": False,
        }

    def _final_entry_reconciliation_case(self):
        review = self._completed_review()
        _artifact, binding, registration_workbook = (
            self._check_record_artifact()
        )
        node = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-reconciliation",
        )
        override = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": self.run.inspection_number,
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "受控对账测试",
        }
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO",
            self.run.inspection_number,
        ):
            operation, _reused = (
                prepare_legacy_microscopy_check_record_entry_operation(
                    self.db,
                    run=self.run,
                    node_run=node,
                    node={"config": {"credential_slot": "legacy_account"}},
                    input_data={
                        "registration_workbook": registration_workbook,
                        "template_binding": binding,
                        "review_result": {"operation_id": review.id},
                        "controlled_test_override": override,
                        **self._project_input(),
                    },
                )
            )
        now = utcnow()
        operation.status = "reconciliation_required"
        operation.attempt_count = 3
        operation.error_code = "excel_com_collection_failed"
        operation.error_message = "Excel COM 0x800A03EC"
        node.status = "waiting_external"
        node.output_data = {
            "operation_id": operation.id,
            "status": "in_progress",
        }
        self.run.status = "waiting_external"
        for attempt_no, stage in (
            (1, "authenticated"),
            (2, "permission_verified"),
        ):
            self.db.add(
                ExecutionExternalAttempt(
                    operation_id=operation.id,
                    attempt_no=attempt_no,
                    bridge_id=f"final-entry-prewrite-{attempt_no}",
                    status="failed",
                    current_stage=stage,
                    checkpoints=[{"stage": stage, "at": now.isoformat()}],
                    exit_code=1,
                    error_code="controlled_prewrite_failure",
                    started_at=now - timedelta(seconds=10),
                    finished_at=now - timedelta(seconds=6),
                )
            )
        attempt = ExecutionExternalAttempt(
            operation_id=operation.id,
            attempt_no=3,
            bridge_id="final-entry-reconciliation-test",
            status="failed",
            current_stage="excel_collection_started",
            checkpoints=[
                {"stage": "authenticated", "at": now.isoformat()},
                {"stage": "permission_verified", "at": now.isoformat()},
                {
                    "stage": "excel_collection_started",
                    "at": now.isoformat(),
                },
            ],
            exit_code=1,
            error_code="excel_com_collection_failed",
            error_message="Excel COM 0x800A03EC",
            started_at=now - timedelta(seconds=5),
            finished_at=now,
        )
        self.db.add(attempt)
        self.user.role = "admin"
        self.db.flush()
        return operation, attempt, node

    def _final_entry_reconciliation_evidence(
        self,
        operation,
        attempt,
        *,
        action: str,
    ) -> dict:
        summary = operation.request_summary["final_entry_summary"]
        checksum = hashlib.sha256(
            json.dumps(
                summary,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        existing = summary["expected_existing_register_count"]
        common = {
            "evidence_contract": "microscopy_final_entry_v1",
            "checked_at": utcnow(),
            "final_entry_summary_checksum": checksum,
            "expected_existing_register_count": existing,
            "writer_stage": attempt.current_stage,
        }
        if action == "confirm_no_side_effect":
            return {
                **common,
                "actual_register_count": existing,
                "actual_file_reference_count": existing,
                "actual_key_result_count": existing,
                "actual_proofed_count": existing,
                "target_file_count": 0,
            }
        resulting = summary["resulting_register_count"]
        return {
            **common,
            "resulting_register_count": resulting,
            "actual_register_count": resulting,
            "actual_file_reference_count": resulting,
            "actual_key_result_count": resulting,
            "actual_proofed_count": resulting,
            "target_file_count": 1,
            "remote_record_id": "sha256:" + "d" * 16,
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

    def test_image_preflight_combines_remote_occupancy_and_local_fences(self):
        self.task_snapshot.snapshot = {
            **self.task_snapshot.snapshot,
            "special_wool_occupied_numbers": [self.run.inspection_number],
        }
        self.db.flush()
        _artifact, original_record = self._artifact()
        first_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-occupied-first",
        )
        first, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=first_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        self.assertEqual(
            first.request_summary["target_sample_number"],
            f"{self.run.inspection_number}-1",
        )
        self.assertEqual(
            first.request_summary["target_filename"],
            f"{self.run.inspection_number}-1-"
            f"{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}",
        )
        self.assertEqual(
            first.request_summary["target_allocation"]["occupancy_scope"],
            "legacy_task_snapshot_and_execution_operation_fences",
        )

        second_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-occupied-second",
        )
        second, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=second_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        self.assertEqual(
            second.request_summary["target_sample_number"],
            f"{self.run.inspection_number}-2",
        )
        self.assertEqual(
            second.request_summary["target_filename"],
            f"{self.run.inspection_number}-2-"
            f"{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}",
        )

    def test_image_preflight_waits_for_remote_occupancy_snapshot(self):
        self.db.delete(self.task_snapshot)
        self.db.flush()
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-no-occupancy-snapshot",
        )
        with self.assertRaises(ExecutionApiError) as captured:
            prepare_legacy_special_wool_image_operation(
                self.db,
                run=self.run,
                node_run=node_run,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={
                    "original_record": original_record,
                    **self._project_input(),
                },
            )
        self.assertEqual(
            captured.exception.code,
            "special_wool_occupancy_snapshot_required",
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
        self.assertEqual(
            summary["target_filename"],
            f"{self.run.inspection_number}-"
            f"{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}",
        )
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
            "legacy_special_wool_image_write_disabled",
        )
        self.assertEqual(operation.status, "prepared")

    def test_image_preflight_accepts_four_copy_task_project(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-four-copy-project",
        )
        project_input = self._project_input()
        project_input["selected_project"].update(
            {
                "check_count": 4,
                "sample_identify": "浴巾，枕套，床单，被套",
            }
        )

        operation, reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **project_input,
            },
        )

        self.assertFalse(reused)
        self.assertEqual(
            operation.request_summary["task_project"]["check_count"],
            4,
        )
        # A single run uploads one workbook even when the task project has
        # multiple copies; the selected identity is consumed by final entry.
        self.assertEqual(
            operation.request_summary["business_fields"]["inspection_copies"],
            1,
        )

    def test_special_wool_write_capability_is_deployment_controlled(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-enabled",
        )
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED",
            True,
        ):
            operation, _reused = prepare_legacy_special_wool_image_operation(
                self.db,
                run=self.run,
                node_run=node_run,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={
                    "original_record": original_record,
                    **self._project_input(),
                },
            )
        self.assertTrue(
            operation.request_summary["execution_capability"]["available"]
        )

        with self.assertRaises(ExecutionApiError) as disabled:
            approve_prepared_external_operation(
                self.db,
                operation=operation,
                run=self.run,
                actor=self.user,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=operation.request_summary[
                    "target_sample_number"
                ],
            )
        self.assertEqual(
            disabled.exception.code,
            "legacy_special_wool_image_write_disabled",
        )

        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED",
            True,
        ):
            approved, reused = approve_prepared_external_operation(
                self.db,
                operation=operation,
                run=self.run,
                actor=self.user,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=operation.request_summary[
                    "target_sample_number"
                ],
            )
        self.assertFalse(reused)
        self.assertEqual(approved.status, "approved")

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
        count_drift_observation = {
            **observation,
            "task_project": {
                **observation["task_project"],
                "check_count": (
                    observation["task_project"]["check_count"] + 1
                ),
            },
        }
        with self.assertRaises(ExecutionApiError) as observation_drifted:
            validate_special_wool_machine_observation(
                operation,
                count_drift_observation,
            )
        self.assertEqual(
            observation_drifted.exception.code,
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
        count_drift = {
            **receipt,
            "task_project": {
                **receipt["task_project"],
                "check_count": receipt["task_project"]["check_count"] + 1,
            },
        }
        with self.assertRaises(ExecutionApiError) as drifted:
            validate_external_receipt(operation, count_drift)
        self.assertEqual(
            drifted.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )
        wrong_filename = {
            **receipt,
            "server_file": {
                **receipt["server_file"],
                "filename": "tampered.xls",
            },
        }
        with self.assertRaises(ExecutionApiError) as filename_changed:
            validate_external_receipt(operation, wrong_filename)
        self.assertEqual(
            filename_changed.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )

    def test_renumbered_upload_receipt_is_bound_and_review_follows_actual(self):
        # 人工增删导致快照漂移时，Writer 按旧系统实况顺号写入；
        # 回执用 requested_sample_number 绑定预检单，target 为实际写入号。
        _artifact, original_record = self._artifact()
        upload_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-renumber",
        )
        upload, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=upload_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )
        requested = upload.request_summary["target_sample_number"]
        actual = f"{self.run.inspection_number}-3"
        receipt = self._image_receipt(upload)
        requested_filename = receipt["target_filename"]
        actual_filename = requested_filename.replace(requested, actual, 1)
        receipt.update(
            {
                "target_sample_number": actual,
                "requested_sample_number": requested,
                "renumbered": True,
                "target_filename": actual_filename,
            }
        )
        receipt["server_file"]["filename"] = actual_filename
        receipt["main_record"]["file_path"] = actual_filename
        receipt["picture_records"][0]["filename"] = actual_filename
        receipt["picture_records"][0][
            "original_data_filename"
        ] = actual_filename
        receipt["readback"]["target_filename"] = actual_filename
        receipt["readback"]["original_data_filename"] = actual_filename
        self.assertIs(validate_external_receipt(upload, receipt), receipt)

        for path in (
            ("target_filename",),
            ("server_file", "filename"),
            ("main_record", "file_path"),
            ("picture_records", 0, "filename"),
            ("picture_records", 0, "original_data_filename"),
            ("readback", "target_filename"),
            ("readback", "original_data_filename"),
        ):
            with self.subTest(path=path):
                stale_filename = json.loads(json.dumps(receipt))
                parent = stale_filename
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = requested_filename
                with self.assertRaises(ExecutionApiError) as rejected:
                    validate_external_receipt(upload, stale_filename)
                self.assertEqual(
                    rejected.exception.code,
                    "legacy_special_wool_machine_document_invalid",
                )

        with self.assertRaises(ExecutionApiError):
            validate_external_receipt(
                upload, {**receipt, "renumbered": False}
            )
        with self.assertRaises(ExecutionApiError):
            validate_external_receipt(
                upload,
                {
                    **receipt,
                    "requested_sample_number": (
                        f"{self.run.inspection_number}-8"
                    ),
                },
            )
        with self.assertRaises(ExecutionApiError):
            validate_external_receipt(
                upload,
                {**receipt, "target_sample_number": "26X999999-1"},
            )
        # 旧版回执（无顺号字段）仍要求与预检单完全一致
        legacy_receipt = self._image_receipt(upload)
        self.assertIs(
            validate_external_receipt(upload, legacy_receipt),
            legacy_receipt,
        )
        with self.assertRaises(ExecutionApiError):
            validate_external_receipt(
                upload,
                {**legacy_receipt, "target_sample_number": actual},
            )

        # 复核节点跟随回执中的实际写入号，而不是预检请求号
        upload.receipt = receipt
        upload.status = "completed"
        review_node = self._node_run(
            LEGACY_SPECIAL_WOOL_REVIEW_NODE,
            "review-renumber",
        )
        review, _reused = prepare_legacy_special_wool_review_operation(
            self.db,
            run=self.run,
            node_run=review_node,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        self.assertEqual(
            review.request_summary["target_sample_number"], actual
        )

    def test_renumbered_qualitative_upload_filenames_follow_actual_target(self):
        requested = self.run.inspection_number
        actual = f"{requested}-2"
        source = {
            "artifact_id": "paper-source-artifact",
            "filename": "纸纤维鉴别原始记录.xls",
            "size_bytes": 128,
            "content_sha256": "a" * 64,
        }
        project = self._project_input(
            name="纸、纸板和纸浆纤维鉴别分析"
        )["selected_project"]
        project = {
            key: project[key]
            for key in (
                "project_key",
                "task_check_item_id",
                "check_item_id",
                "check_item_no",
                "check_item_name",
                "check_method",
                "seq_num",
                "check_count",
            )
        }
        requested_filename = f"{requested}-{source['filename']}"
        actual_filename = f"{actual}-{source['filename']}"
        operation = ExecutionExternalOperation(
            id="paper-upload-renumber",
            payload_checksum="b" * 64,
            request_summary={
                "operation_type": (
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
                ),
                "target_sample_number": requested,
                "target_filename": requested_filename,
                "target_allocation": {"base_number": requested},
                "files": [source],
                "task_project": project,
            },
        )

        def receipt_for(
            target_sample_number: str,
            target_filename: str,
            *,
            include_allocation: bool,
        ) -> dict:
            receipt = {
                "schema_version": 1,
                "receipt_type": (
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
                ),
                "operation_id": operation.id,
                "payload_checksum": operation.payload_checksum,
                "target_sample_number": target_sample_number,
                "target_filename": target_filename,
                "source_artifact": dict(source),
                "task_project": dict(project),
                "server_file": {
                    "filename": target_filename,
                    "size_bytes": source["size_bytes"],
                    "content_sha256": source["content_sha256"],
                    "verification": {
                        "mode": "exact_sha256",
                        "source_size_bytes": source["size_bytes"],
                        "source_content_sha256": source["content_sha256"],
                        "remote_size_bytes": source["size_bytes"],
                        "remote_content_sha256": source["content_sha256"],
                        "stream_paths_equal": True,
                        "stream_sizes_equal": True,
                        "non_workbook_streams_equal": True,
                        "biff_record_boundaries_equal": True,
                        "changed_record_ids": [],
                        "changed_record_count": 0,
                    },
                },
                "main_record": {
                    "id": "sha256:" + "3" * 16,
                    "field_fingerprint": "4" * 64,
                    "create_user": "sha256:" + "5" * 16,
                    "create_time": "2026-08-05T08:30:00Z",
                    "file_path": target_filename,
                },
                "picture_count": 0,
                "readback": {
                    "main_count": 1,
                    "picture_count": 0,
                    "mismatches": [],
                    "verified_at": "2026-08-05T08:30:01Z",
                    "target_filename": target_filename,
                },
                "stages": list(
                    SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES
                ),
                "reconciliation_required": False,
            }
            if include_allocation:
                receipt.update(
                    {
                        "requested_sample_number": requested,
                        "renumbered": True,
                    }
                )
            return receipt

        current = receipt_for(
            actual,
            actual_filename,
            include_allocation=True,
        )
        self.assertIs(validate_external_receipt(operation, current), current)
        for path in (
            ("target_filename",),
            ("server_file", "filename"),
            ("main_record", "file_path"),
            ("readback", "target_filename"),
        ):
            with self.subTest(path=path):
                stale_filename = json.loads(json.dumps(current))
                parent = stale_filename
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = requested_filename
                with self.assertRaises(ExecutionApiError) as rejected:
                    validate_external_receipt(operation, stale_filename)
                self.assertEqual(
                    rejected.exception.code,
                    "legacy_special_wool_machine_document_invalid",
                )

        legacy = receipt_for(
            requested,
            requested_filename,
            include_allocation=False,
        )
        self.assertIs(validate_external_receipt(operation, legacy), legacy)
        with self.assertRaises(ExecutionApiError) as legacy_renumbered:
            validate_external_receipt(
                operation,
                {**legacy, "target_sample_number": actual},
            )
        self.assertEqual(
            legacy_renumbered.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )

    def test_image_receipt_original_data_filename_is_bound_and_v1_compatible(self):
        self.task_snapshot.snapshot = {
            **self.task_snapshot.snapshot,
            "special_wool_occupied_numbers": [self.run.inspection_number],
        }
        self.db.flush()
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-original-data-filename-contract",
        )
        operation, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )

        current = self._image_receipt(operation)
        self.assertEqual(
            current["target_sample_number"],
            f"{self.run.inspection_number}-1",
        )
        self.assertEqual(
            current["picture_records"][0]["original_data_filename"],
            current["target_filename"],
        )
        self.assertEqual(
            current["readback"]["original_data_filename"],
            current["target_filename"],
        )
        self.assertIs(validate_external_receipt(operation, current), current)

        legacy_v1 = self._image_receipt(operation)
        legacy_v1["picture_records"][0].pop("original_data_filename")
        legacy_v1["readback"].pop("original_data_filename")
        self.assertIs(
            validate_external_receipt(operation, legacy_v1),
            legacy_v1,
        )

        for section in ("picture_records", "readback"):
            with self.subTest(section=section):
                invalid = self._image_receipt(operation)
                if section == "picture_records":
                    invalid[section][0]["original_data_filename"] = "错误文件名.xls"
                else:
                    invalid[section]["original_data_filename"] = "错误文件名.xls"
                with self.assertRaises(ExecutionApiError) as rejected:
                    validate_external_receipt(operation, invalid)
                self.assertEqual(
                    rejected.exception.code,
                    "legacy_special_wool_machine_document_invalid",
                )

    def test_server_file_verification_accepts_only_two_strict_modes(self):
        _artifact, original_record = self._artifact()
        node_run = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-server-file-verification",
        )
        operation, _reused = prepare_legacy_special_wool_image_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "original_record": original_record,
                **self._project_input(),
            },
        )

        exact = self._image_receipt(operation)
        self.assertIs(validate_external_receipt(operation, exact), exact)
        normalized = self._image_receipt(
            operation,
            verification_mode="cfb_biff_writeaccess_only",
        )
        self.assertNotEqual(
            normalized["server_file"]["content_sha256"],
            normalized["source_artifact"]["content_sha256"],
        )
        self.assertIs(
            validate_external_receipt(operation, normalized), normalized
        )

        invalid_verifications = {
            "unknown_mode": {
                **normalized["server_file"]["verification"],
                "mode": "unknown",
            },
            "unexpected_field": {
                **normalized["server_file"]["verification"],
                "unexpected": True,
            },
            "wrong_record_id": {
                **normalized["server_file"]["verification"],
                "changed_record_ids": ["0x005D"],
            },
            "logical_stream_mismatch": {
                **normalized["server_file"]["verification"],
                "non_workbook_streams_equal": False,
            },
            "remote_hash_not_bound": {
                **normalized["server_file"]["verification"],
                "remote_content_sha256": "f" * 64,
            },
            "source_size_not_bound": {
                **normalized["server_file"]["verification"],
                "source_size_bytes": (
                    normalized["source_artifact"]["size_bytes"] + 1
                ),
            },
            "missing_writeaccess_change": {
                **normalized["server_file"]["verification"],
                "changed_record_count": 0,
            },
        }
        for name, verification in invalid_verifications.items():
            with self.subTest(name=name):
                invalid = {
                    **normalized,
                    "server_file": {
                        **normalized["server_file"],
                        "verification": verification,
                    },
                }
                with self.assertRaises(ExecutionApiError) as rejected:
                    validate_external_receipt(operation, invalid)
                self.assertEqual(
                    rejected.exception.code,
                    "legacy_special_wool_machine_document_invalid",
                )

    def test_completed_reconciliation_preserves_full_receipt_for_review(self):
        operation, attempt = self._reconciliation_required_image_upload()
        receipt = self._image_receipt(
            operation,
            verification_mode="cfb_biff_writeaccess_only",
        )
        evidence = self._completed_reconciliation_evidence(
            operation,
            receipt=receipt,
        )

        with patch(
            "app.execution.engine.complete_external_node"
        ) as complete_node:
            resolved, duplicate = reconcile_external_operation(
                self.db,
                operation_id=operation.id,
                actor=self.user,
                action="confirm_completed",
                attempt_id=attempt.id,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=operation.request_summary[
                    "target_sample_number"
                ],
                note="已逐项核对特纤主记录、图片子记录和服务器文件",
                evidence=evidence,
            )

        self.assertFalse(duplicate)
        self.assertEqual(resolved.status, "completed")
        self.assertEqual(resolved.receipt, receipt)
        self.assertEqual(
            resolved.remote_record_id,
            receipt["main_record"]["id"],
        )
        complete_node.assert_called_once()
        self.assertEqual(
            complete_node.call_args.kwargs["output_data"]["receipt"],
            receipt,
        )
        self.assertEqual(
            complete_node.call_args.kwargs["output_data"]["operation_id"],
            operation.id,
        )

        review_node = self._node_run(
            LEGACY_SPECIAL_WOOL_REVIEW_NODE,
            "review-after-reconciliation",
        )
        review, reused = prepare_legacy_special_wool_review_operation(
            self.db,
            run=self.run,
            node_run=review_node,
            node={"config": {"credential_slot": "legacy_account"}},
            # 兼容已经落库的早期对账节点输出：顶层缺少 operation_id，
            # 但完整机器回执仍携带且严格绑定该 ID。
            input_data={"upload_result": {"receipt": receipt}},
        )

        self.assertFalse(reused)
        self.assertEqual(
            review.request_summary["source_operation"]["main_id"],
            receipt["main_record"]["id"],
        )
        self.assertEqual(
            review.request_summary["source_operation"]["receipt_checksum"],
            hashlib.sha256(
                json.dumps(
                    receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_completed_reconciliation_requires_valid_bound_receipt(self):
        operation, attempt = self._reconciliation_required_image_upload()
        common = {
            "operation_id": operation.id,
            "actor": self.user,
            "action": "confirm_completed",
            "attempt_id": attempt.id,
            "payload_checksum": operation.payload_checksum,
            "confirmed_sample_number": operation.request_summary[
                "target_sample_number"
            ],
            "note": "已逐项核对特纤主记录、图片子记录和服务器文件",
        }

        missing = self._completed_reconciliation_evidence(
            operation,
            receipt=None,
        )
        with self.assertRaises(ExecutionApiError) as receipt_required:
            reconcile_external_operation(
                self.db,
                evidence=missing,
                **common,
            )
        self.assertEqual(
            receipt_required.exception.code,
            "external_reconciliation_receipt_required",
        )

        receipt = self._image_receipt(operation)
        invalid_receipt = {
            **receipt,
            "target_filename": "错误文件名.xls",
        }
        with self.assertRaises(ExecutionApiError) as receipt_invalid:
            reconcile_external_operation(
                self.db,
                evidence=self._completed_reconciliation_evidence(
                    operation,
                    receipt=invalid_receipt,
                ),
                **common,
            )
        self.assertEqual(
            receipt_invalid.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )

        with self.assertRaises(ExecutionApiError) as receipt_mismatch:
            reconcile_external_operation(
                self.db,
                evidence=self._completed_reconciliation_evidence(
                    operation,
                    receipt=receipt,
                    remote_record_id="sha256:" + "f" * 16,
                ),
                **common,
            )
        self.assertEqual(
            receipt_mismatch.exception.code,
            "external_reconciliation_receipt_mismatch",
        )

        normalized_receipt = self._image_receipt(
            operation,
            verification_mode="cfb_biff_writeaccess_only",
        )
        wrong_remote_hash = self._completed_reconciliation_evidence(
            operation,
            receipt=normalized_receipt,
        )
        wrong_remote_hash["remote_file_sha256"] = hashlib.sha256(
            b"wrong-remote-file"
        ).hexdigest()
        with self.assertRaises(ExecutionApiError) as hash_mismatch:
            reconcile_external_operation(
                self.db,
                evidence=wrong_remote_hash,
                **common,
            )
        self.assertEqual(
            hash_mismatch.exception.code,
            "external_reconciliation_receipt_mismatch",
        )
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(operation.receipt, {})

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
        expected_main_id = upload.receipt["main_record"]["id"]
        self.assertEqual(
            review.request_summary["source_operation"]["main_id"],
            expected_main_id,
        )
        bridge_view = bridge_external_operation(
            review,
            credential=self.credential,
        )
        self.assertEqual(
            bridge_view["machine_payload"]["source_upload"]["main_id"],
            expected_main_id,
        )

        children_fingerprint = "8" * 64
        receipt = {
            "schema_version": 1,
            "receipt_type": "legacy_special_wool_review",
            "operation_id": review.id,
            "payload_checksum": review.payload_checksum,
            "target_sample_number": review.request_summary[
                "target_sample_number"
            ],
            "source_upload": {
                "operation_id": review.request_summary["source_operation"][
                    "operation_id"
                ],
                "receipt_checksum": review.request_summary[
                    "source_operation"
                ]["receipt_checksum"],
                "main_id": "sha256:" + "f" * 16,
            },
            "main_record": {
                "id": "sha256:" + "f" * 16,
                "review_user": "sha256:" + "9" * 16,
                "review_time": "2026-08-05T08:30:00Z",
                "pre_fingerprint": "a" * 64,
                "post_fingerprint": "b" * 64,
            },
            "children": {
                "picture_count": 1,
                "before_fingerprint": children_fingerprint,
                "after_fingerprint": children_fingerprint,
                "unchanged": True,
            },
            "readback": {
                "main_count": 1,
                "mismatches": [],
                "verified_at": "2026-08-05T08:30:01Z",
            },
            "stages": list(SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES),
            "reconciliation_required": False,
        }
        with self.assertRaises(ExecutionApiError) as replaced:
            validate_external_receipt(review, receipt)
        self.assertEqual(
            replaced.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )

    def test_review_approval_rechecks_upload_receipt_identity(self):
        _artifact, original_record = self._artifact()
        upload_node = self._node_run(
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            "upload-before-review-approval",
        )
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED",
            True,
        ):
            upload, _reused = prepare_legacy_special_wool_image_operation(
                self.db,
                run=self.run,
                node_run=upload_node,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={
                    "original_record": original_record,
                    **self._project_input(),
                },
            )
            upload.receipt = self._image_receipt(upload)
            upload.status = "completed"
            review_node = self._node_run(
                LEGACY_SPECIAL_WOOL_REVIEW_NODE,
                "review-approval-source-recheck",
            )
            review, _reused = prepare_legacy_special_wool_review_operation(
                self.db,
                run=self.run,
                node_run=review_node,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={"upload_result": {"operation_id": upload.id}},
            )

            replacement = "sha256:" + "f" * 16
            upload.receipt["main_record"]["id"] = replacement
            upload.receipt["picture_records"][0]["main_id"] = replacement
            with self.assertRaises(ExecutionApiError) as changed:
                approve_prepared_external_operation(
                    self.db,
                    operation=review,
                    run=self.run,
                    actor=self.user,
                    payload_checksum=review.payload_checksum,
                    confirmed_sample_number=review.request_summary[
                        "target_sample_number"
                    ],
                )
        self.assertEqual(
            changed.exception.code,
            "special_wool_upload_result_changed",
        )

    def test_final_entry_rearm_uses_excel_collection_write_boundary(self):
        now = utcnow()
        safe_node, safe_operation = self._expired_final_entry_operation(
            node_id="final-entry-safe-rearm",
            stages=["authenticated", "permission_verified"],
        )
        self.assertTrue(
            _rearm_expired_external_operation(
                self.db,
                operation=safe_operation,
                run=self.run,
                node_run=safe_node,
                prepared_at=now,
                preflight_expires_at=now + timedelta(minutes=15),
            )
        )
        self.assertEqual(safe_operation.status, "prepared")
        self.assertEqual(safe_operation.attempt_count, 2)
        self.assertEqual(
            [item.current_stage for item in safe_operation.attempts],
            ["authenticated", "permission_verified"],
        )

        boundary_node, boundary_operation = (
            self._expired_final_entry_operation(
                node_id="final-entry-boundary-rearm",
                stages=["excel_collection_started"],
            )
        )
        with self.assertRaises(ExecutionApiError) as blocked:
            _rearm_expired_external_operation(
                self.db,
                operation=boundary_operation,
                run=self.run,
                node_run=boundary_node,
                prepared_at=now,
                preflight_expires_at=now + timedelta(minutes=15),
            )
        self.assertEqual(
            blocked.exception.code,
            "external_operation_not_rearmable",
        )
        self.assertEqual(
            blocked.exception.details["reason"],
            "write_boundary_reached",
        )
        self.assertEqual(
            blocked.exception.details["write_boundary"],
            "excel_collection_started",
        )
        self.assertEqual(boundary_operation.status, "expired")

    def test_final_entry_no_side_effect_reconciliation_binds_existing_state(self):
        operation, attempt, node = self._final_entry_reconciliation_case()
        context = public_external_reconciliation_context(operation)
        self.assertEqual(context["attempt"]["attempt_no"], 3)
        self.assertEqual(context["attempt"]["id"], attempt.id)
        expected = context["expected_evidence"]
        self.assertEqual(
            expected["evidence_contract"],
            "microscopy_final_entry_v1",
        )
        self.assertEqual(expected["expected_existing_register_count"], 1)
        self.assertEqual(
            expected["confirm_no_side_effect"],
            {
                "expected_existing_register_count": 1,
                "actual_register_count": 1,
                "actual_file_reference_count": 1,
                "actual_key_result_count": 1,
                "actual_proofed_count": 1,
                "target_file_count": 0,
                "writer_stage": "excel_collection_started",
                "latest_allowed_writer_stage": "excel_collection_started",
                "allowed_writer_stages": [
                    "authenticated",
                    "permission_verified",
                    "remote_state_verified",
                    "excel_write_ready",
                    "excel_collection_started",
                ],
            },
        )
        self.assertNotIn(
            "exact_record_count",
            expected["confirm_no_side_effect"],
        )
        self.assertEqual(
            expected["confirm_completed"]["resulting_register_count"],
            2,
        )
        self.assertEqual(
            expected["confirm_completed"]["actual_proofed_count"],
            2,
        )
        self.assertEqual(
            expected["confirm_completed"]["target_file_count"],
            1,
        )
        evidence = self._final_entry_reconciliation_evidence(
            operation,
            attempt,
            action="confirm_no_side_effect",
        )
        request = ExternalOperationReconciliationRequest(
            action="confirm_no_side_effect",
            attempt_id=attempt.id,
            payload_checksum=operation.payload_checksum,
            confirmed_sample_number=self.run.inspection_number,
            note=(
                "只读探针确认原有登记、文件引用、关键结果及"
                "校对状态均未变化"
            ),
            evidence=evidence,
        )
        resolved, duplicate = reconcile_external_operation(
            self.db,
            operation_id=operation.id,
            actor=self.user,
            action=request.action,
            attempt_id=request.attempt_id,
            payload_checksum=request.payload_checksum,
            confirmed_sample_number=request.confirmed_sample_number,
            note=request.note,
            evidence=request.evidence.model_dump(mode="json"),
        )
        self.assertFalse(duplicate)
        self.assertEqual(resolved.status, "failed")
        self.assertEqual(resolved.receipt, {})
        self.assertEqual(node.status, "failed")
        self.assertEqual(
            resolved.verification["reconciliation"]["evidence"][
                "actual_register_count"
            ],
            1,
        )

        node.status = "running"
        node.attempt_count = 4
        prepared_at = utcnow()
        rearmed = _rearm_reconciled_no_side_effect_operation(
            self.db,
            operation=resolved,
            run=self.run,
            node_run=node,
            prepared_at=prepared_at,
            preflight_expires_at=prepared_at + timedelta(minutes=30),
        )
        self.assertTrue(rearmed)
        self.assertEqual(resolved.status, "prepared")
        self.assertEqual(resolved.verification, {})
        self.assertIsNone(resolved.error_code)
        self.assertIsNone(resolved.completed_at)
        self.assertEqual(resolved.attempt_count, 3)

    def test_final_entry_completed_reconciliation_requires_result_and_proof(self):
        operation, attempt, node = self._final_entry_reconciliation_case()
        evidence = self._final_entry_reconciliation_evidence(
            operation,
            attempt,
            action="confirm_completed",
        )
        request = ExternalOperationReconciliationRequest(
            action="confirm_completed",
            attempt_id=attempt.id,
            payload_checksum=operation.payload_checksum,
            confirmed_sample_number=self.run.inspection_number,
            note=(
                "只读探针确认新增登记、目标文件引用、关键结果与"
                "校对状态完整"
            ),
            evidence=evidence,
        )
        resolved, duplicate = reconcile_external_operation(
            self.db,
            operation_id=operation.id,
            actor=self.user,
            action=request.action,
            attempt_id=request.attempt_id,
            payload_checksum=request.payload_checksum,
            confirmed_sample_number=request.confirmed_sample_number,
            note=request.note,
            evidence=request.evidence.model_dump(mode="json"),
        )
        self.assertFalse(duplicate)
        self.assertEqual(resolved.status, "completed")
        self.assertEqual(node.status, "succeeded")
        self.assertEqual(
            resolved.remote_record_id,
            "sha256:" + "d" * 16,
        )
        self.assertEqual(
            resolved.receipt["receipt_type"],
            "legacy_microscopy_check_record_entry_manual_reconciliation",
        )
        self.assertEqual(
            resolved.receipt["final_entry"]["resulting_register_count"],
            2,
        )
        self.assertEqual(
            resolved.receipt["final_entry"]["actual_proofed_count"],
            2,
        )
        self.assertTrue(resolved.receipt["final_entry"]["proofed"])

    def test_final_entry_reconciliation_rejects_unbound_or_late_evidence(self):
        operation, attempt, _node = self._final_entry_reconciliation_case()
        base = self._final_entry_reconciliation_evidence(
            operation,
            attempt,
            action="confirm_no_side_effect",
        )
        invalid_cases = {
            "generic_zero_contract": {
                "checked_at": utcnow(),
                "exact_record_count": 0,
                "contains_record_count": 0,
                "target_file_count": 0,
            },
            "summary_checksum": {
                **base,
                "final_entry_summary_checksum": "0" * 64,
            },
            "existing_count": {
                **base,
                "expected_existing_register_count": 0,
            },
            "register_count": {**base, "actual_register_count": 2},
            "file_reference_count": {
                **base,
                "actual_file_reference_count": 0,
            },
            "key_result_count": {
                **base,
                "actual_key_result_count": 0,
            },
            "proofed_count": {**base, "actual_proofed_count": 0},
            "target_file_count": {**base, "target_file_count": 1},
            "later_writer_stage": {
                **base,
                "writer_stage": "remote_file_verified",
            },
        }
        for name, evidence in invalid_cases.items():
            with self.subTest(name=name):
                normalized = {
                    key: (
                        value.isoformat()
                        if isinstance(value, datetime)
                        else value
                    )
                    for key, value in evidence.items()
                }
                with self.assertRaises(ExecutionApiError) as blocked:
                    _validate_final_entry_reconciliation_evidence(
                        operation,
                        attempt=attempt,
                        action="confirm_no_side_effect",
                        evidence=normalized,
                    )
                self.assertEqual(
                    blocked.exception.code,
                    "external_reconciliation_evidence_incomplete",
                )
        self.assertEqual(operation.status, "reconciliation_required")

    def test_final_entry_binds_base_number_artifact_review_and_private_payload(self):
        review = self._completed_review()
        artifact, binding, registration_workbook = self._check_record_artifact()
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry",
        )
        operation, reused = prepare_legacy_microscopy_check_record_entry_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "registration_workbook": registration_workbook,
                "template_binding": binding,
                "review_result": {"operation_id": review.id},
                **self._project_input(),
            },
        )
        self.assertFalse(reused)
        self.assertEqual(
            operation.request_summary["target_sample_number"],
            self.run.inspection_number,
        )
        package = operation.request_summary["final_entry_package"]
        self.assertEqual(package["expected_existing_register_count"], 0)
        self.assertEqual(
            package["task_project"],
            operation.request_summary["task_project"],
        )
        self.assertEqual(package["task_project"]["check_count"], 1)
        self.assertEqual(
            package["excel_record"]["expected_key_identities"], ["纵向"]
        )
        self.assertEqual(
            package["excel_record"]["register"]["sample_identity"],
            "纵向",
        )
        self.assertEqual(
            package["excel_record"]["template_name"], "微观形貌.xls"
        )
        self.assertEqual(
            operation.request_summary["files"][0]["artifact_id"], artifact.id
        )

        public = public_external_operation(operation)
        self.assertNotIn("machine_payload", public)
        self.assertNotIn("final_entry_package", public["request_summary"])
        self.assertEqual(
            set(public["request_summary"]["files"][0]),
            {
                "artifact_id",
                "root_id",
                "relative_path",
                "filename",
                "size_bytes",
                "content_sha256",
            },
        )
        self.assertEqual(
            public["request_summary"]["final_entry_summary"],
            {
                "source_review_target_sample_number": (
                    review.request_summary["target_sample_number"]
                ),
                "image_count": 1,
                "expected_task_check_count": 1,
                "expected_existing_register_count": 0,
                "resulting_register_count": 1,
                "registration_capacity_mode": (
                    "single_copy_confirmation_required"
                ),
                "registration_capacity_exceeded": False,
                "existing_record_append_confirmed": False,
                "controlled_test": False,
                "controlled_test_reason": None,
            },
        )
        bridge = bridge_external_operation(
            operation,
            credential=self.credential,
        )
        self.assertEqual(bridge["machine_payload"], package)
        stages, boundary, verified = _operation_stage_profile(operation)
        self.assertEqual(stages, MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES)
        self.assertEqual(boundary, "excel_collection_started")
        self.assertEqual(verified, "excel_proof_verified")
        normal_receipt = self._final_entry_receipt(operation)
        self.assertIs(
            validate_external_receipt(operation, normal_receipt),
            normal_receipt,
        )
        for field, changed in (
            ("task_check_item_id", "sha256:" + "9" * 16),
            ("check_item_id", "sha256:" + "8" * 16),
            ("check_method", "GB/T 36422-2018/XG1-2024"),
            ("seq_num", 2),
            ("check_count", 2),
        ):
            with self.subTest(field=field):
                changed_receipt = self._final_entry_receipt(operation)
                changed_receipt["task_project"][field] = changed
                with self.assertRaises(ExecutionApiError) as captured:
                    validate_external_receipt(operation, changed_receipt)
                self.assertEqual(
                    captured.exception.code,
                    "legacy_special_wool_machine_document_invalid",
                )

    def test_final_entry_multi_copy_uses_refreshed_capacity_and_sample_identity(self):
        project_input = self._project_input()
        project = project_input["selected_project"]
        project.update(
            {
                "check_count": 4,
                "register_count": 4,
                "sample_identify": "浴巾，枕套，床单，被套",
            }
        )
        review = self._completed_review(project_input=project_input)
        _artifact, binding, registration_workbook = self._check_record_artifact(
            sample_identity="浴巾"
        )
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-multi-copy",
        )
        operation, reused = prepare_legacy_microscopy_check_record_entry_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "registration_workbook": registration_workbook,
                "template_binding": binding,
                "review_result": {"operation_id": review.id},
                "registration_decision": {
                    "existing_record_action": "continue",
                    "registration_cancelled": False,
                    "expected_existing_register_count": 4,
                    "auto_submitted": True,
                    "auto_submit_reason": (
                        "multi_copy_capacity_is_informational"
                    ),
                },
                "record_input": {"sample_identity": "浴巾"},
                **project_input,
            },
        )
        self.assertFalse(reused)
        package = operation.request_summary["final_entry_package"]
        self.assertEqual(package["task_project"]["check_count"], 4)
        self.assertEqual(package["expected_existing_register_count"], 4)
        self.assertNotIn("existing_record_decision", package)
        self.assertEqual(
            package["excel_record"]["register"]["sample_identity"],
            "浴巾",
        )
        self.assertEqual(
            operation.request_summary["sample_identity_contract"],
            {
                "selected": "浴巾",
                "options": ["浴巾", "枕套", "床单", "被套"],
                "option_count": 4,
                "check_count": 4,
                "count_mismatch": False,
            },
        )
        self.assertEqual(
            operation.request_summary["final_entry_summary"][
                "resulting_register_count"
            ],
            5,
        )
        self.assertEqual(
            operation.request_summary["final_entry_summary"][
                "registration_capacity_mode"
            ],
            "informational_for_multi_copy",
        )
        self.assertTrue(
            operation.request_summary["final_entry_summary"][
                "registration_capacity_exceeded"
            ]
        )
        expectations = _final_entry_reconciliation_expectations(operation)
        self.assertEqual(expectations["expected_existing_register_count"], 4)
        self.assertEqual(expectations["resulting_register_count"], 5)

    def test_final_entry_rejects_check_count_changed_after_upload(self):
        project_input = self._project_input()
        project = project_input["selected_project"]
        project.update(
            {
                "check_count": 4,
                "sample_identify": "浴巾，枕套，床单，被套",
            }
        )
        review = self._completed_review(project_input=project_input)
        project["check_count"] = 3
        _artifact, binding, registration_workbook = self._check_record_artifact(
            sample_identity="浴巾"
        )
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-count-drift",
        )

        with self.assertRaises(ExecutionApiError) as drifted:
            prepare_legacy_microscopy_check_record_entry_operation(
                self.db,
                run=self.run,
                node_run=node_run,
                node={"config": {"credential_slot": "legacy_account"}},
                input_data={
                    "registration_workbook": registration_workbook,
                    "template_binding": binding,
                    "review_result": {"operation_id": review.id},
                    "registration_decision": {
                        "existing_record_action": "continue",
                        "registration_cancelled": False,
                        "expected_existing_register_count": 0,
                    },
                    "record_input": {"sample_identity": "浴巾"},
                    **project_input,
                },
            )
        self.assertEqual(
            drifted.exception.code,
            "microscopy_task_project_changed_after_upload",
        )

    def test_final_entry_approval_rechecks_uploaded_project_binding(self):
        review = self._completed_review()
        _artifact, binding, registration_workbook = self._check_record_artifact()
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-approval-project-drift",
        )
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED",
            True,
        ):
            operation, _reused = (
                prepare_legacy_microscopy_check_record_entry_operation(
                    self.db,
                    run=self.run,
                    node_run=node_run,
                    node={"config": {"credential_slot": "legacy_account"}},
                    input_data={
                        "registration_workbook": registration_workbook,
                        "template_binding": binding,
                        "review_result": {"operation_id": review.id},
                        **self._project_input(),
                    },
                )
            )
            review.request_summary = {
                **review.request_summary,
                "task_project": {
                    **review.request_summary["task_project"],
                    "check_count": 2,
                },
            }
            with self.assertRaises(ExecutionApiError) as drifted:
                approve_prepared_external_operation(
                    self.db,
                    operation=operation,
                    run=self.run,
                    actor=self.user,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number=self.run.inspection_number,
                )
        self.assertEqual(
            drifted.exception.code,
            "microscopy_final_entry_binding_changed",
        )

    def test_final_entry_single_copy_existing_record_binds_append_confirmation(self):
        review = self._completed_review()
        _artifact, binding, registration_workbook = self._check_record_artifact()
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-existing-confirmed",
        )
        project_input = self._project_input()
        project_input["selected_project"]["register_count"] = 1
        operation, reused = prepare_legacy_microscopy_check_record_entry_operation(
            self.db,
            run=self.run,
            node_run=node_run,
            node={"config": {"credential_slot": "legacy_account"}},
            input_data={
                "registration_workbook": registration_workbook,
                "template_binding": binding,
                "review_result": {"operation_id": review.id},
                "registration_decision": {
                    "existing_record_action": "append",
                    "registration_cancelled": False,
                    "expected_existing_register_count": 1,
                },
                "record_input": {
                    "sample_identity": "纵向",
                    "sample_identity_confirmed": True,
                },
                **project_input,
            },
        )
        self.assertFalse(reused)
        expected_decision = {
            "kind": "append_when_check_count_one",
            "action": "append",
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
        }
        self.assertEqual(
            operation.request_summary["final_entry_package"][
                "existing_record_decision"
            ],
            expected_decision,
        )
        receipt = self._final_entry_receipt(operation)
        receipt["existing_record_decision"] = expected_decision
        self.assertIs(validate_external_receipt(operation, receipt), receipt)

    def test_final_entry_controlled_override_and_receipt_are_fail_closed(self):
        review = self._completed_review()
        _artifact, binding, registration_workbook = self._check_record_artifact()
        node_run = self._node_run(
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
            "final-entry-controlled",
        )
        override = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": self.run.inspection_number,
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "已获准验证 CheckCount=1 时追加一条登记记录",
        }
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO",
            self.run.inspection_number,
        ), patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED",
            True,
        ):
            operation, _reused = (
                prepare_legacy_microscopy_check_record_entry_operation(
                    self.db,
                    run=self.run,
                    node_run=node_run,
                    node={"config": {"credential_slot": "legacy_account"}},
                    input_data={
                        "registration_workbook": registration_workbook,
                        "template_binding": binding,
                        "review_result": {"operation_id": review.id},
                        "controlled_test_override": override,
                        **self._project_input(),
                    },
                )
            )
        with self.assertRaises(ExecutionApiError) as disabled:
            approve_prepared_external_operation(
                self.db,
                operation=operation,
                run=self.run,
                actor=self.user,
                payload_checksum=operation.payload_checksum,
                confirmed_sample_number=self.run.inspection_number,
            )
        self.assertEqual(
            disabled.exception.code,
            "legacy_microscopy_final_entry_disabled",
        )
        with patch(
            "app.execution.external_operations.settings."
            "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED",
            True,
        ):
            with self.assertRaises(ExecutionApiError) as revoked_override:
                approve_prepared_external_operation(
                    self.db,
                    operation=operation,
                    run=self.run,
                    actor=self.user,
                    payload_checksum=operation.payload_checksum,
                    confirmed_sample_number=self.run.inspection_number,
                )
        self.assertEqual(
            revoked_override.exception.code,
            "controlled_final_entry_override_not_authorized",
        )
        self.assertEqual(
            public_external_operation(operation)["request_summary"][
                "final_entry_summary"
            ]["controlled_test_reason"],
            override["reason"],
        )
        receipt = self._final_entry_receipt(
            operation,
            controlled={
                key: value for key, value in override.items() if key != "reason"
            }
            | {"active": True, "applied": True},
        )
        self.assertIs(validate_external_receipt(operation, receipt), receipt)
        receipt["controlled_test_override"]["applied"] = False
        with self.assertRaises(ExecutionApiError) as captured:
            validate_external_receipt(operation, receipt)
        self.assertEqual(
            captured.exception.code,
            "legacy_special_wool_machine_document_invalid",
        )


if __name__ == "__main__":
    unittest.main()
