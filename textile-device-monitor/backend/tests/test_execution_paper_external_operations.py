from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.external_operations import (
    GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES,
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE,
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
    PAPER_FIBER_PROJECT_NAME,
    PAPER_FIBER_TEST_METHOD,
    SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES,
    SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES,
    _paper_result_value,
    approve_prepared_external_operation,
    bridge_external_operation,
    prepare_legacy_generic_check_record_entry_operation,
    prepare_legacy_special_wool_qualitative_review_operation,
    prepare_legacy_special_wool_qualitative_upload_operation,
    validate_external_receipt,
)
from app.execution.models import (
    ExecutionCategory,
    ExecutionCredential,
    ExecutionFileIndexEntry,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionTaskSnapshotCache,
    ExecutionUser,
    ExecutionWorkflow,
)


class PaperExternalOperationTests(unittest.TestCase):
    def test_paper_result_unit_only_uses_standalone_100_token(self):
        cases = (
            ("木浆 100", True, "%"),
            ("草浆、木浆", False, ""),
            ("100.5", False, ""),
            ("1000", False, ""),
        )
        for value, contains_standalone_100, unit in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    _paper_result_value(
                        {
                            "result": {
                                "worksheet": "Sheet1",
                                "cell": "W32",
                                "w32_value": value,
                                "contains_standalone_100": (
                                    contains_standalone_100
                                ),
                                "unit": unit,
                            }
                        }
                    ),
                    (value, unit),
                )

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name)
        self.user = ExecutionUser(
            username="paper-user",
            display_name="纸纤维检验员",
            password_hash="not-used",
        )
        category = ExecutionCategory(key="paper", name="纸纤维")
        self.root = ExecutionStorageRoot(
            root_id="paper_fiber_records",
            name="纸纤维原始记录",
            local_path=str(self.source),
            access_mode="read",
            is_active=True,
            is_available=True,
        )
        self.db.add_all([self.user, category, self.root])
        self.db.flush()
        workflow = ExecutionWorkflow(
            slug="paper-external-test",
            category_id=category.id,
            name="纸纤维外部操作测试",
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
        self.db.add_all([workflow, self.credential])
        self.db.flush()
        self.run = ExecutionRun(
            workflow_id=workflow.id,
            created_by_id=self.user.id,
            idempotency_key="paper-external-run",
            inspection_number="26W006687",
            definition_snapshot={
                "root_slots": [
                    {"root_id": "paper_fiber_records", "access": "read"}
                ],
                "credential_slots": [
                    {
                        "name": "legacy_account",
                        "system_key": "legacy_inspection",
                    }
                ],
            },
            definition_checksum="d" * 64,
            capabilities_snapshot={"external_write": True},
            contract_checksum="c" * 64,
            input_data={},
        )
        self.db.add(self.run)
        self.db.flush()
        project = self._project()
        self.db.add(
            ExecutionTaskSnapshotCache(
                inspection_number=self.run.inspection_number,
                status="ready",
                snapshot={
                    "schema_version": 4,
                    "inspection_number": self.run.inspection_number,
                    "projects": [project],
                    "special_wool_occupied_numbers": [],
                },
                fetched_at=datetime.now(timezone.utc),
                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        )
        self.path = self.source / "纸纤维鉴别原始记录.xls"
        self.path.write_bytes(b"paper-fiber-source-workbook")
        stat = self.path.stat()
        self.entry = ExecutionFileIndexEntry(
            storage_root_id=self.root.id,
            relative_path=self.path.name,
            filename=self.path.name,
            extension=".xls",
            file_kind="file",
            inspection_number=self.run.inspection_number,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            metadata_json={},
            scan_generation=1,
        )
        self.db.add(self.entry)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _project(self):
        project = {
            "task_check_item_id": "sha256:" + "1" * 16,
            "check_item_id": "sha256:" + "2" * 16,
            "check_item_no": "PAPER-QUAL",
            "check_item_name": PAPER_FIBER_PROJECT_NAME,
            "check_method": PAPER_FIBER_TEST_METHOD,
            "seq_num": 2,
            "check_count": 1,
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
        return project

    def _node(self, node_type, node_id):
        node = ExecutionNodeRun(
            run_id=self.run.id,
            node_id=node_id,
            node_type=node_type,
            node_type_version=1,
            node_name=node_id,
            status="running",
        )
        self.db.add(node)
        self.db.flush()
        return node

    def _input(self, value="100"):
        project = self._project()
        return {
            "selected_project_key": project["project_key"],
            "selected_project": project,
            "selected_files": [
                {
                    "id": self.entry.id,
                    "root_id": self.root.root_id,
                    "relative_path": self.entry.relative_path,
                    "fingerprint": self.entry.fingerprint,
                    "read_status": "succeeded",
                    "result": {
                        "worksheet": "Sheet1",
                        "cell": "W32",
                        "w32_value": value,
                        "qualitative_result": value,
                        "contains_standalone_100": value == "100",
                        "unit": "%" if value == "100" else "",
                    },
                }
            ],
            "primary_file_id": self.entry.id,
        }

    def _node_config(self):
        return {"config": {"credential_slot": "legacy_account"}}

    def _upload_receipt(self, operation):
        summary = operation.request_summary
        source = summary["files"][0]
        return {
            "schema_version": 1,
            "receipt_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": summary["target_sample_number"],
            "target_filename": summary["target_filename"],
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
            "server_file": {
                "filename": summary["target_filename"],
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
                "file_path": summary["target_filename"],
            },
            "picture_count": 0,
            "readback": {
                "main_count": 1,
                "picture_count": 0,
                "mismatches": [],
                "verified_at": "2026-08-05T08:30:01Z",
                "target_filename": summary["target_filename"],
            },
            "stages": [
                {"stage": stage}
                for stage in SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES
            ],
            "reconciliation_required": False,
        }

    def _review_receipt(self, operation):
        summary = operation.request_summary
        source = summary["source_operation"]
        return {
            "schema_version": 1,
            "receipt_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
            "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": summary["target_sample_number"],
            "source_upload": {
                "operation_id": source["operation_id"],
                "receipt_checksum": source["receipt_checksum"],
                "main_id": source["main_id"],
            },
            "main_record": {
                "id": source["main_id"],
                "review_user": "sha256:" + "6" * 16,
                "review_time": "2026-08-05T08:31:00Z",
                "pre_fingerprint": "7" * 64,
                "post_fingerprint": "8" * 64,
            },
            "children": {
                "picture_count": 0,
                "before_fingerprint": "9" * 64,
                "after_fingerprint": "9" * 64,
                "unchanged": True,
            },
            "readback": {
                "main_count": 1,
                "mismatches": [],
                "verified_at": "2026-08-05T08:31:01Z",
            },
            "stages": [
                {"stage": stage}
                for stage in SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES
            ],
            "reconciliation_required": False,
        }

    def test_prepares_upload_review_and_generic_entry_contracts(self):
        upload_node = self._node(
            LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload"
        )
        upload, reused = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=upload_node,
            node=self._node_config(),
            input_data=self._input("100"),
        )
        self.assertFalse(reused)
        summary = upload.request_summary
        self.assertEqual(summary["business_fields"]["inspection_item"], "棉再生纤定性")
        self.assertEqual(summary["result_contract"], {
            "worksheet": "Sheet1", "cell": "W32", "value": "100", "unit": "%"
        })
        self.assertEqual(
            summary["target_filename"],
            f"26W006687-{self.entry.filename}",
        )
        upload.receipt = self._upload_receipt(upload)
        validate_external_receipt(upload, upload.receipt)
        upload.status = "completed"

        review_node = self._node(
            LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE, "review"
        )
        review, _ = prepare_legacy_special_wool_qualitative_review_operation(
            self.db,
            run=self.run,
            node_run=review_node,
            node=self._node_config(),
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        review_bridge = bridge_external_operation(
            review, credential=self.credential
        )
        self.assertEqual(
            review_bridge["machine_payload"],
            {
                "schema_version": 1,
                "operation_type": (
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
                ),
                "target_sample_number": review.request_summary[
                    "target_sample_number"
                ],
                "source_upload": review.request_summary[
                    "source_operation"
                ],
            },
        )
        review.receipt = self._review_receipt(review)
        validate_external_receipt(review, review.receipt)
        review.status = "completed"

        entry_node = self._node(LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE, "entry")
        project = self._project()
        entry, _ = prepare_legacy_generic_check_record_entry_operation(
            self.db,
            run=self.run,
            node_run=entry_node,
            node=self._node_config(),
            input_data={
                "selected_project_key": project["project_key"],
                "selected_project": project,
                "review_result": {"operation_id": review.id},
            },
        )
        payload = entry.request_summary["final_entry_package"]
        self.assertEqual(entry.request_summary["operation_type"], LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["operation_type"], "generic_item_record")
        self.assertEqual(payload["generic_record"]["header"]["unit"], "%")
        self.assertEqual(
            payload["generic_record"]["header"]["report_check_item_name"],
            "",
        )
        entry_bridge = bridge_external_operation(
            entry, credential=self.credential
        )
        self.assertEqual(
            entry_bridge["machine_payload"]["generic_record"]["header"][
                "report_check_item_name"
            ],
            "",
        )
        self.assertEqual(payload["generic_record"]["details"][0]["real_value"], "100")
        self.assertFalse(entry.request_summary["safety"]["proof_required"])
        self.assertEqual(
            entry.request_summary["machine_contract"]["proof_required"], False
        )
        self.assertEqual(
            GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES[-2],
            "generic_projection_verified",
        )

    def _completed_review(self, value="100"):
        upload, _ = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload"
            ),
            node=self._node_config(),
            input_data=self._input(value),
        )
        upload.receipt = self._upload_receipt(upload)
        validate_external_receipt(upload, upload.receipt)
        upload.status = "completed"
        review, _ = prepare_legacy_special_wool_qualitative_review_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE, "review"
            ),
            node=self._node_config(),
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        review.receipt = self._review_receipt(review)
        validate_external_receipt(review, review.receipt)
        review.status = "completed"
        return review

    def _prepare_generic_entry(self, project, *, judgement_input=None):
        review = self._completed_review()
        input_data = {
            "selected_project_key": project["project_key"],
            "selected_project": project,
            "review_result": {"operation_id": review.id},
        }
        if judgement_input is not None:
            input_data["judgement_input"] = judgement_input
        entry, _ = prepare_legacy_generic_check_record_entry_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE, "entry"
            ),
            node=self._node_config(),
            input_data=input_data,
        )
        return entry

    def test_generic_entry_judgement_variant_writes_full_judgement_fields(self):
        project = self._project()
        project["give_judgement"] = 1
        entry = self._prepare_generic_entry(
            project,
            judgement_input={"judge_basis": "按客户要求", "judgement": "符合"},
        )
        payload = entry.request_summary["final_entry_package"]
        header = payload["generic_record"]["header"]
        detail = payload["generic_record"]["details"][0]
        self.assertEqual(header["judge_basis"], "按客户要求")
        self.assertEqual(header["total_judge"], "符合")
        self.assertEqual(
            header["report_check_item_name"], PAPER_FIBER_PROJECT_NAME
        )
        self.assertEqual(header["unit"], "%")
        self.assertEqual(header["test_method"], PAPER_FIBER_TEST_METHOD)
        self.assertEqual(detail["standard_value"], "100")
        self.assertEqual(detail["real_value"], "100")
        self.assertTrue(
            entry.request_summary["final_entry_summary"]["judgement_required"]
        )
        # give_judgement 属于辅助信息，不进入 task_project 严格契约
        self.assertNotIn("give_judgement", payload["task_project"])

    def test_generic_entry_judgement_required_but_missing_conflicts(self):
        project = self._project()
        project["give_judgement"] = 1
        with self.assertRaises(ExecutionApiError) as raised:
            self._prepare_generic_entry(project)
        self.assertEqual(
            raised.exception.code, "paper_fiber_judgement_required"
        )

    def test_generic_entry_without_judgement_ignores_stray_values(self):
        project = self._project()
        entry = self._prepare_generic_entry(
            project,
            judgement_input={"judge_basis": "按客户要求", "judgement": "符合"},
        )
        payload = entry.request_summary["final_entry_package"]
        header = payload["generic_record"]["header"]
        detail = payload["generic_record"]["details"][0]
        self.assertEqual(header["judge_basis"], "")
        self.assertEqual(header["total_judge"], "")
        self.assertEqual(header["report_check_item_name"], "")
        self.assertEqual(detail["standard_value"], "")
        self.assertFalse(
            entry.request_summary["final_entry_summary"]["judgement_required"]
        )

    def test_non_standalone_100_has_no_percent_unit(self):
        data = self._input("棉100，粘纤0")
        data["selected_files"][0]["result"]["contains_standalone_100"] = False
        data["selected_files"][0]["result"]["unit"] = ""
        upload, _ = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload-text"
            ),
            node=self._node_config(),
            input_data=data,
        )
        self.assertEqual(upload.request_summary["result_contract"]["unit"], "")

    def test_qualitative_review_approve_uses_paper_source_reverifier(self):
        upload, _ = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload"
            ),
            node=self._node_config(),
            input_data=self._input("100"),
        )
        upload.receipt = self._upload_receipt(upload)
        validate_external_receipt(upload, upload.receipt)
        upload.status = "completed"

        review, _ = prepare_legacy_special_wool_qualitative_review_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE, "review"
            ),
            node=self._node_config(),
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        from app.config import settings

        with patch.object(
            settings, "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED", True
        ):
            approved, _ = approve_prepared_external_operation(
                self.db,
                operation=review,
                run=self.run,
                actor=self.user,
                payload_checksum=review.payload_checksum,
                confirmed_sample_number=review.request_summary[
                    "target_sample_number"
                ],
            )
        self.assertEqual(approved.status, "approved")

    def test_generic_entry_prepare_accepts_controlled_override(self):
        upload, _ = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload"
            ),
            node=self._node_config(),
            input_data=self._input("100"),
        )
        upload.receipt = self._upload_receipt(upload)
        validate_external_receipt(upload, upload.receipt)
        upload.status = "completed"
        review, _ = prepare_legacy_special_wool_qualitative_review_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE, "review"
            ),
            node=self._node_config(),
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        review.receipt = self._review_receipt(review)
        validate_external_receipt(review, review.receipt)
        review.status = "completed"

        project = self._project()
        override_input = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": self.run.inspection_number,
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "既有 1 条登记，受控追加 1 条",
        }
        from app.config import settings

        with patch.object(
            settings,
            "EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO",
            self.run.inspection_number,
        ):
            entry, _ = prepare_legacy_generic_check_record_entry_operation(
                self.db,
                run=self.run,
                node_run=self._node(
                    LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE, "entry"
                ),
                node=self._node_config(),
                input_data={
                    "selected_project_key": project["project_key"],
                    "selected_project": project,
                    "review_result": {"operation_id": review.id},
                    "controlled_test_override": override_input,
                },
            )
        summary = entry.request_summary["final_entry_summary"]
        self.assertEqual(summary["expected_existing_register_count"], 1)
        self.assertEqual(summary["resulting_register_count"], 2)
        self.assertTrue(summary["controlled_test"])
        package = entry.request_summary["final_entry_package"]
        self.assertEqual(package["expected_existing_register_count"], 1)
        self.assertEqual(
            package["controlled_test_override"]["target_sample_number"],
            self.run.inspection_number,
        )

    def test_generic_entry_receipt_accepts_bound_override(self):
        upload, _ = prepare_legacy_special_wool_qualitative_upload_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE, "upload"
            ),
            node=self._node_config(),
            input_data=self._input("100"),
        )
        upload.receipt = self._upload_receipt(upload)
        validate_external_receipt(upload, upload.receipt)
        upload.status = "completed"
        review, _ = prepare_legacy_special_wool_qualitative_review_operation(
            self.db,
            run=self.run,
            node_run=self._node(
                LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE, "review"
            ),
            node=self._node_config(),
            input_data={"upload_result": {"operation_id": upload.id}},
        )
        review.receipt = self._review_receipt(review)
        validate_external_receipt(review, review.receipt)
        review.status = "completed"

        project = self._project()
        override_input = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": self.run.inspection_number,
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "既有 1 条登记，受控追加 1 条",
        }
        from app.config import settings

        with patch.object(
            settings,
            "EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO",
            self.run.inspection_number,
        ):
            entry, _ = prepare_legacy_generic_check_record_entry_operation(
                self.db,
                run=self.run,
                node_run=self._node(
                    LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE, "entry"
                ),
                node=self._node_config(),
                input_data={
                    "selected_project_key": project["project_key"],
                    "selected_project": project,
                    "review_result": {"operation_id": review.id},
                    "controlled_test_override": override_input,
                },
            )
        receipt = {
            "schema_version": 1,
            "receipt_type": "legacy_generic_check_record_entry",
            "operation_id": entry.id,
            "payload_checksum": entry.payload_checksum,
            "target_sample_number": entry.request_summary[
                "target_sample_number"
            ],
            "task_project": dict(entry.request_summary["task_project"]),
            "final_entry": {
                "package_schema_version": 2,
                "expected_existing_register_count": 1,
                "resulting_register_count": 2,
                "detail_count": 1,
                "key_result_count": 1,
                "record_id": "sha256:" + "a" * 16,
                "proofed": False,
            },
            "controlled_test_override": {
                "active": True,
                "applied": True,
                "kind": override_input["kind"],
                "target_sample_number": override_input[
                    "target_sample_number"
                ],
                "expected_task_check_count": 1,
                "expected_existing_register_count": 1,
                "resulting_register_count": 2,
            },
            "stages": [
                {"stage": stage, "at": "2026-08-06T00:00:00+00:00"}
                for stage in GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES
            ],
            "reconciliation_required": False,
        }
        validate_external_receipt(entry, receipt)

        missing_override = {
            key: value for key, value in receipt.items() if key != "controlled_test_override"
        }
        with self.assertRaises(Exception):
            validate_external_receipt(entry, missing_override)

        mismatched = json.loads(json.dumps(receipt))
        mismatched["controlled_test_override"]["resulting_register_count"] = 3
        with self.assertRaises(Exception):
            validate_external_receipt(entry, mismatched)


if __name__ == "__main__":
    unittest.main()
