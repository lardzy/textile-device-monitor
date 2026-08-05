from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.catalog import (
    _electron_microscopy_gbt36422_definition,
    _electron_microscopy_gbt36422_image_selection_definition,
    bind_user_role,
    ensure_default_catalog,
    ensure_default_rbac,
)
from app.execution.electron_microscopy import (
    ELECTRON_NODE_TYPE,
    cached_task_snapshot,
    claim_task_snapshot_refresh,
    complete_task_snapshot_refresh,
    electron_microscopy_match,
    request_task_snapshot_refresh,
)
from app.api.execution import preview_indexed_electron_image
from app.execution.engine import (
    claim_human_task,
    claim_next_node,
    create_run,
    execute_claimed_node,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_original_record import (
    register_microscopy_original_record_executor,
)
from app.execution.models import (
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionStorageRoot,
    ExecutionTaskSnapshotCache,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowVersion,
    utcnow,
)
from app.execution.persistence import (
    enqueue_due_index_jobs,
    queue_refresh,
    register_persistence_executors,
)
from app.execution.regenerated_fiber import catalog_recommendations
from app.execution.security import hash_password
from app.execution.validation import (
    definition_checksum,
    validate_definition,
    validate_json_instance,
    workflow_contract_checksum,
)


class ElectronMicroscopyWorkflowTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        register_microscopy_original_record_executor()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.tempdir.name) / "2026-电镜"
        self.root_path.mkdir(parents=True)
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        self.user = ExecutionUser(
            username="electron-admin",
            display_name="电镜测试",
            password_hash=hash_password("electron-password"),
            role="admin",
        )
        self.db.add(self.user)
        self.db.flush()
        bind_user_role(self.db, self.user, "admin", created_by_id=self.user.id)
        self.root = ExecutionStorageRoot(
            root_id="electron_microscopy_records",
            name="电镜原始资料",
            local_path=str(self.root_path),
            access_mode="read",
            category_key="electron_microscopy",
            is_active=True,
            is_available=True,
            last_scan_finished_at=utcnow(),
        )
        self.db.add(self.root)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.tempdir.cleanup()

    def _image(self, relative_path: str) -> ExecutionFileIndexEntry:
        path = self.root_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"BM" + relative_path.encode("utf-8"))
        stat = path.stat()
        entry = ExecutionFileIndexEntry(
            storage_root_id=self.root.id,
            relative_path=relative_path,
            filename=path.name,
            extension=path.suffix.casefold(),
            file_kind="file",
            inspection_number=None,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            metadata_json={"kind": "file"},
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def _complete_snapshot(self, *, project_name: str) -> None:
        cached_task_snapshot(self.db, inspection_number="26A029794")
        row = claim_task_snapshot_refresh(self.db, bridge_id="readonly-vm")
        self.assertIsNotNone(row)
        token = row.claim_token
        complete_task_snapshot_refresh(
            self.db,
            inspection_number="26A029794",
            bridge_id="readonly-vm",
            claim_token=token,
            snapshot={
                "sample_name": "示例样品",
                "sample_names": ["示例样品"],
                "check_basis": "---",
                "projects": [
                    {
                        "task_check_item_id": "sha256:1111111111111111",
                        "check_item_id": "sha256:2222222222222222",
                        "check_item_no": "5103.5",
                        "check_item_name": project_name,
                        "check_method": "GB/T 36422-2018",
                        "check_count": 1,
                        "seq_num": 1,
                    }
                ],
            },
        )
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029794"
        )["snapshot"]
        self.assertEqual(cached["schema_version"], 3)
        self.assertEqual(cached["sample_names"], ["示例样品"])
        self.assertTrue(cached["projects"][0]["project_key"].startswith("task-project:"))

    def _execute_one(self):
        node = claim_next_node(self.db, worker_id="electron-worker")
        self.assertIsNotNone(node)
        token = node.lease_token
        self.db.commit()
        execute_claimed_node(self.db, node_run_id=node.id, lease_token=token)
        self.db.commit()
        return node

    def test_default_catalog_publishes_the_full_microscopy_workflow(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        node_types = {
            node["type"] for node in workflow.draft_definition["nodes"]
        }
        self.assertTrue(
            {
                "human.image_selection",
                "data.microscopy_record_context",
                "human.input",
                "workbook.microscopy_original_record",
                "human.confirm",
                "external.legacy_special_wool_image_upload",
                "external.legacy_special_wool_review",
            }.issubset(node_types)
        )
        self.assertEqual(
            workflow.capabilities,
            {"read": True, "write": True, "external_write": True},
        )
        print_node = next(
            node
            for node in workflow.draft_definition["nodes"]
            if node["id"] == "print-confirm"
        )
        print_schema = print_node["config"]["form_schema"]
        self.assertEqual(
            print_schema["properties"]["print_decision"]["enum"],
            ["print", "skip"],
        )
        self.assertEqual(
            print_schema["required"],
            ["print_decision", "artifact_sha256"],
        )
        self.assertEqual(
            print_schema["properties"]["print_completed"],
            {"type": "boolean"},
        )
        self.assertEqual(
            print_schema["allOf"][0]["then"]["required"],
            ["print_completed"],
        )
        self.assertNotIn("printed", print_schema["properties"])
        sha256 = "a" * 64
        self.assertTrue(
            validate_json_instance(
                print_schema,
                {
                    "print_decision": "print",
                    "print_completed": True,
                    "artifact_sha256": sha256,
                },
                path_prefix="$.data",
            ).valid
        )
        self.assertTrue(
            validate_json_instance(
                print_schema,
                {
                    "print_decision": "skip",
                    "artifact_sha256": sha256,
                },
                path_prefix="$.data",
            ).valid
        )
        self.assertFalse(
            validate_json_instance(
                print_schema,
                {
                    "print_decision": "print",
                    "artifact_sha256": sha256,
                },
                path_prefix="$.data",
            ).valid
        )
        validation = validate_definition(
            workflow.draft_definition,
            for_publish=True,
        )
        self.assertTrue(
            validation.valid,
            [issue.as_dict() for issue in validation.issues],
        )

    def test_untouched_image_only_workflow_is_upgraded_to_version_two(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        old_definition = _electron_microscopy_gbt36422_image_selection_definition()
        select_node = next(
            node for node in old_definition["nodes"]
            if node["id"] == "select-images"
        )
        select_node["input_mapping"].pop("truncated")
        old_capabilities = {"read": True, "write": False}
        version = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=1,
        ).one()
        workflow.draft_definition = old_definition
        workflow.draft_revision = 1
        workflow.published_version_number = 1
        workflow.capabilities = old_capabilities
        workflow.created_by_id = None
        workflow.updated_by_id = None
        version.definition = old_definition
        version.checksum = definition_checksum(old_definition)
        version.capabilities = old_capabilities
        version.contract_checksum = workflow_contract_checksum(
            old_definition,
            old_capabilities,
        )
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(
            workflow.capabilities,
            {"read": True, "write": True, "external_write": True},
        )
        self.assertEqual(
            self.db.query(ExecutionWorkflowVersion)
            .filter_by(workflow_id=workflow.id)
            .count(),
            2,
        )

    def test_admin_edited_image_only_workflow_is_not_overwritten(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        old_definition = _electron_microscopy_gbt36422_image_selection_definition()
        old_definition["metadata"]["name"] = "管理员保留版本"
        workflow.draft_definition = old_definition
        workflow.draft_revision = 2
        workflow.updated_by_id = self.user.id
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(
            workflow.draft_definition["metadata"]["name"],
            "管理员保留版本",
        )

    def test_system_version_two_receives_external_preflight_capability(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        definition = workflow.draft_definition
        legacy_capabilities = {"read": True, "write": True}
        workflow.draft_revision = 2
        workflow.published_version_number = 2
        workflow.capabilities = legacy_capabilities
        workflow.created_by_id = None
        workflow.updated_by_id = None
        self.db.add(
            ExecutionWorkflowVersion(
                workflow_id=workflow.id,
                version_number=2,
                schema_version="1.0",
                definition=definition,
                checksum=definition_checksum(definition),
                capabilities=legacy_capabilities,
                contract_checksum=workflow_contract_checksum(
                    definition,
                    legacy_capabilities,
                ),
                release_note="历史完整流程版本",
            )
        )
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 3)
        self.assertEqual(workflow.published_version_number, 3)
        self.assertEqual(
            workflow.capabilities,
            {"read": True, "write": True, "external_write": True},
        )
        version_three = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=3,
        ).one()
        self.assertEqual(version_three.capabilities, workflow.capabilities)

    def test_system_owned_legacy_print_contract_is_upgraded(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        legacy_definition = _electron_microscopy_gbt36422_definition(
            legacy_print_contract=True
        )
        capabilities = {"read": True, "write": True, "external_write": True}
        version_one = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=1,
        ).one()
        workflow.draft_definition = legacy_definition
        workflow.draft_revision = 1
        workflow.published_version_number = 1
        workflow.capabilities = capabilities
        workflow.created_by_id = None
        workflow.updated_by_id = None
        version_one.definition = legacy_definition
        version_one.checksum = definition_checksum(legacy_definition)
        version_one.capabilities = capabilities
        version_one.contract_checksum = workflow_contract_checksum(
            legacy_definition,
            capabilities,
        )
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(workflow.published_version_number, 2)
        print_node = next(
            node
            for node in workflow.draft_definition["nodes"]
            if node["id"] == "print-confirm"
        )
        print_schema = print_node["config"]["form_schema"]
        self.assertIn("print_decision", print_schema["properties"])
        self.assertIn("print_completed", print_schema["properties"])
        self.assertNotIn("printed", print_schema["properties"])

    def test_system_owned_print_choice_without_completion_is_upgraded(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        legacy_definition = _electron_microscopy_gbt36422_definition(
            legacy_print_choice_contract=True
        )
        capabilities = {"read": True, "write": True, "external_write": True}
        version_one = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=1,
        ).one()
        workflow.draft_definition = legacy_definition
        workflow.draft_revision = 1
        workflow.published_version_number = 1
        workflow.capabilities = capabilities
        workflow.created_by_id = None
        workflow.updated_by_id = None
        version_one.definition = legacy_definition
        version_one.checksum = definition_checksum(legacy_definition)
        version_one.capabilities = capabilities
        version_one.contract_checksum = workflow_contract_checksum(
            legacy_definition,
            capabilities,
        )
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 2)
        print_node = next(
            node
            for node in workflow.draft_definition["nodes"]
            if node["id"] == "print-confirm"
        )
        print_schema = print_node["config"]["form_schema"]
        self.assertIn("print_completed", print_schema["properties"])
        self.assertIn("allOf", print_schema)

    def test_system_owned_head_v3_project_contract_is_upgraded(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        image_selection = (
            _electron_microscopy_gbt36422_image_selection_definition()
        )
        legacy_head = _electron_microscopy_gbt36422_definition(
            legacy_print_contract=True,
            legacy_project_contract=True,
        )
        capabilities = {"read": True, "write": True, "external_write": True}
        version_two_capabilities = {"read": True, "write": True}
        version_one = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=1,
        ).one()
        version_one.definition = image_selection
        version_one.checksum = definition_checksum(image_selection)
        version_one.capabilities = {"read": True, "write": False}
        version_one.contract_checksum = workflow_contract_checksum(
            image_selection, version_one.capabilities
        )
        for version_number, version_capabilities in (
            (2, version_two_capabilities),
            (3, capabilities),
        ):
            self.db.add(
                ExecutionWorkflowVersion(
                    workflow_id=workflow.id,
                    version_number=version_number,
                    schema_version="1.0",
                    definition=legacy_head,
                    checksum=definition_checksum(legacy_head),
                    capabilities=version_capabilities,
                    contract_checksum=workflow_contract_checksum(
                        legacy_head, version_capabilities
                    ),
                    release_note=(
                        "原始记录完整流程"
                        if version_number == 2
                        else "HEAD 外部预检能力版本"
                    ),
                )
            )
        workflow.draft_definition = legacy_head
        workflow.draft_revision = 3
        workflow.published_version_number = 3
        workflow.capabilities = capabilities
        workflow.created_by_id = None
        workflow.updated_by_id = None
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 4)
        self.assertEqual(workflow.published_version_number, 4)
        upload_node = next(
            node
            for node in workflow.draft_definition["nodes"]
            if node["id"] == "upload-record"
        )
        self.assertEqual(
            upload_node["input_mapping"]["selected_project_key"],
            "$.nodes.record-input.output.selected_project_key",
        )
        self.assertEqual(
            upload_node["input_mapping"]["selected_project"],
            "$.nodes.record-input.output.selected_project",
        )

    def test_admin_edited_head_v3_contract_is_not_upgraded(self):
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        legacy_head = _electron_microscopy_gbt36422_definition(
            legacy_print_contract=True,
            legacy_project_contract=True,
        )
        capabilities = {"read": True, "write": True, "external_write": True}
        version_one = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=1,
        ).one()
        version_one.definition = (
            _electron_microscopy_gbt36422_image_selection_definition()
        )
        version_one.checksum = definition_checksum(version_one.definition)
        version_one.capabilities = {"read": True, "write": False}
        version_one.contract_checksum = workflow_contract_checksum(
            version_one.definition,
            version_one.capabilities,
        )
        for version_number, version_capabilities in (
            (2, {"read": True, "write": True}),
            (3, capabilities),
        ):
            self.db.add(
                ExecutionWorkflowVersion(
                    workflow_id=workflow.id,
                    version_number=version_number,
                    schema_version="1.0",
                    definition=legacy_head,
                    checksum=definition_checksum(legacy_head),
                    capabilities=version_capabilities,
                    contract_checksum=workflow_contract_checksum(
                        legacy_head, version_capabilities
                    ),
                    release_note="历史系统版本",
                )
            )
        edited = dict(legacy_head)
        edited["metadata"] = {
            **legacy_head["metadata"],
            "name": "管理员保留的微观形貌流程",
        }
        workflow.draft_definition = edited
        workflow.draft_revision = 3
        workflow.published_version_number = 3
        workflow.capabilities = capabilities
        workflow.updated_by_id = self.user.id
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.draft_revision, 3)
        self.assertEqual(workflow.published_version_number, 3)
        self.assertEqual(
            workflow.draft_definition["metadata"]["name"],
            "管理员保留的微观形貌流程",
        )
        self.assertEqual(
            self.db.query(ExecutionWorkflowVersion)
            .filter_by(workflow_id=workflow.id)
            .count(),
            3,
        )

    def test_recommendation_scores_folder_and_same_project_task_facts(self):
        self._image("26A029794-lisy/纵面/IMAGE01.BMP")
        self._image("26A029794-补拍/横截面/image02.JpEg")
        self._image("OTHER/纵面/26A029794-note.png")
        self._complete_snapshot(project_name="纤维微观形貌")

        match = electron_microscopy_match(
            self.db, inspection_number="26A029794"
        )
        self.assertTrue(match["full_match"])
        self.assertEqual(match["folder_match_count"], 2)
        self.assertEqual(match["image_count"], 2)
        self.assertEqual(
            set(match["matched_conditions"]),
            {"source_root", "folder", "task_item_name", "test_method"},
        )
        self.assertTrue(
            all(item["preview_url"].endswith("/preview") for item in match["images"])
        )

        recommendations, _ = catalog_recommendations(
            self.db,
            inspection_number="26A029794",
            preferred_categories=["electron_microscopy"],
            include_hidden=False,
        )
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        item = next(
            value for value in recommendations if value["workflow_id"] == workflow.id
        )
        self.assertEqual(item["score"], 5)
        self.assertTrue(item["full_match"])
        self.assertEqual(item["task_cache_state"], "ready")

        ensure_default_catalog(self.db)
        self.db.commit()
        self.assertEqual(
            self.db.query(ExecutionWorkflow)
            .filter_by(slug="electron-microscopy-gbt36422")
            .count(),
            1,
        )

    def test_task_cache_queues_once_uses_token_and_refreshes_when_stale(self):
        first = cached_task_snapshot(self.db, inspection_number="26A029794")
        second = cached_task_snapshot(self.db, inspection_number="26a029794")
        self.assertTrue(first["refresh_queued"])
        self.assertFalse(second["refresh_queued"])
        self.assertEqual(
            self.db.query(ExecutionTaskSnapshotCache).count(),
            1,
        )
        row = claim_task_snapshot_refresh(self.db, bridge_id="bridge-a")
        self.assertEqual(row.inspection_number, "26A029794")
        self.assertIsNotNone(row.claim_token)
        lease_seconds = (row.claim_expires_at - utcnow()).total_seconds()
        self.assertGreaterEqual(lease_seconds, 175)
        self.assertLessEqual(lease_seconds, 180)
        active_token = row.claim_token
        _same, forced = request_task_snapshot_refresh(
            self.db, inspection_number="26A029794", force=True
        )
        self.assertFalse(forced)
        self.assertEqual(row.claim_token, active_token)
        active = cached_task_snapshot(self.db, inspection_number="26A029794")
        self.assertFalse(active["refresh_queued"])
        with self.assertRaises(ExecutionApiError) as raised:
            complete_task_snapshot_refresh(
                self.db,
                inspection_number="26A029794",
                bridge_id="bridge-a",
                claim_token="00000000-0000-0000-0000-000000000000",
                snapshot={"projects": []},
            )
        self.assertEqual(raised.exception.code, "task_snapshot_claim_lost")
        complete_task_snapshot_refresh(
            self.db,
            inspection_number="26A029794",
            bridge_id="bridge-a",
            claim_token=row.claim_token,
            snapshot={"projects": []},
        )
        row.expires_at = utcnow() - timedelta(seconds=1)
        row.status = "ready"
        self.db.commit()
        stale = cached_task_snapshot(self.db, inspection_number="26A029794")
        again = cached_task_snapshot(self.db, inspection_number="26A029794")
        self.assertEqual(stale["cache_state"], "stale")
        self.assertTrue(stale["refresh_queued"])
        self.assertFalse(again["refresh_queued"])

    def test_v2_snapshot_is_hidden_and_queued_for_contract_refresh(self):
        row = ExecutionTaskSnapshotCache(
            inspection_number="26A029796",
            status="ready",
            snapshot={"schema_version": 2, "projects": []},
            fetched_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
        self.db.add(row)
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029796"
        )

        self.assertEqual(cached["cache_state"], "pending")
        self.assertIsNone(cached["snapshot"])
        self.assertTrue(cached["refresh_queued"])
        self.assertEqual(row.status, "queued")

    def test_v3_microscopy_snapshot_without_public_ids_is_refreshed(self):
        row = ExecutionTaskSnapshotCache(
            inspection_number="26A029797",
            status="ready",
            snapshot={
                "schema_version": 3,
                "projects": [
                    {
                        "check_item_name": "纤维微观形貌",
                        "check_method": "GB/T 36422-2018",
                    }
                ],
            },
            fetched_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
        self.db.add(row)
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029797"
        )

        self.assertEqual(cached["cache_state"], "pending")
        self.assertIsNone(cached["snapshot"])
        self.assertTrue(cached["refresh_queued"])

    def test_bridge_completion_rejects_microscopy_project_without_public_ids(self):
        cached_task_snapshot(self.db, inspection_number="26A029798")
        row = claim_task_snapshot_refresh(self.db, bridge_id="bridge-invalid")

        with self.assertRaises(ExecutionApiError) as raised:
            complete_task_snapshot_refresh(
                self.db,
                inspection_number="26A029798",
                bridge_id="bridge-invalid",
                claim_token=row.claim_token,
                snapshot={
                    "schema_version": 3,
                    "projects": [
                        {
                            "check_item_name": "纤维微观形貌",
                            "check_method": "GB/T 36422-2018",
                        }
                    ],
                },
            )

        self.assertEqual(
            raised.exception.code, "task_snapshot_project_identity_missing"
        )

    def test_other_project_snapshot_remains_compatible_without_public_ids(self):
        cached_task_snapshot(self.db, inspection_number="26A029799")
        row = claim_task_snapshot_refresh(self.db, bridge_id="bridge-other")
        complete_task_snapshot_refresh(
            self.db,
            inspection_number="26A029799",
            bridge_id="bridge-other",
            claim_token=row.claim_token,
            snapshot={
                "schema_version": 2,
                "projects": [
                    {
                        "check_item_name": "纤维平均直径",
                        "check_method": "其它方法",
                    }
                ],
            },
        )
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029799"
        )
        self.assertEqual(cached["cache_state"], "ready")
        self.assertEqual(cached["snapshot"]["schema_version"], 3)
        self.assertIsNone(
            cached["snapshot"]["projects"][0]["task_check_item_id"]
        )

    def test_task_name_and_method_cannot_be_combined_across_projects(self):
        self._image("26A029795/纵面/one.png")
        cached_task_snapshot(self.db, inspection_number="26A029795")
        row = claim_task_snapshot_refresh(self.db, bridge_id="bridge-split")
        complete_task_snapshot_refresh(
            self.db,
            inspection_number="26A029795",
            bridge_id="bridge-split",
            claim_token=row.claim_token,
            snapshot={
                "projects": [
                    {
                        "task_check_item_id": "sha256:3333333333333333",
                        "check_item_id": "sha256:4444444444444444",
                        "check_item_name": "纤维微观形貌",
                        "check_method": "按客户要求",
                    },
                    {
                        "check_item_name": "纤维平均直径",
                        "check_method": "GB/T 36422-2018",
                    },
                ]
            },
        )
        match = electron_microscopy_match(
            self.db, inspection_number="26A029795"
        )
        self.assertFalse(match["full_match"])
        self.assertFalse(
            {"task_item_name", "test_method"}.issubset(
                set(match["matched_conditions"])
            )
        )

    def test_index_preview_checks_fingerprint_and_image_allowlist(self):
        entry = self._image("26A029794-lisy/纵面/preview.BMP")
        self.db.commit()
        response = preview_indexed_electron_image(entry.id, None, self.db)
        self.assertEqual(response.media_type, "image/bmp")
        (self.root_path / entry.relative_path).write_bytes(b"changed")
        with self.assertRaises(ExecutionApiError) as raised:
            preview_indexed_electron_image(entry.id, None, self.db)
        self.assertEqual(raised.exception.code, "indexed_image_stale")

    def test_default_workflow_accepts_controlled_project_alias_and_continues_to_record_input(self):
        first = self._image("26A029794-lisy/纵面/one.bmp")
        second = self._image("26A029794-补拍/横截面/two.PNG")
        self._complete_snapshot(project_name="膜平面形貌")
        workflow = self.db.query(ExecutionWorkflow).filter_by(
            slug="electron-microscopy-gbt36422"
        ).one()
        run, duplicate = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="26A029794",
            input_data={},
            global_data={},
            idempotency_key="electron-gbt36422-run",
        )
        self.db.commit()
        self.assertFalse(duplicate)
        self._execute_one()  # start
        discover = self._execute_one()
        self.assertEqual(discover.output_data["task_validation_state"], "matched")
        self.assertEqual(discover.output_data["missing_conditions"], [])
        self.assertFalse(discover.output_data["truncated"])
        self._execute_one()  # human task
        task = self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).one()
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        with self.assertRaises(ExecutionApiError) as raised:
            submit_human_task(
                self.db,
                task_id=task.id,
                expected_revision=task.revision,
                data={
                    "selected_folder_ids": [
                        task.node_run.input_data["folders"][0]["id"]
                    ],
                    "selected_image_ids": [second.id],
                },
                actor=self.user,
            )
        self.assertEqual(
            raised.exception.code,
            "image_candidate_outside_selected_folders",
        )
        selected = next(
            item
            for item in task.node_run.input_data["images"]
            if item["id"] == first.id
        )
        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={
                "selected_folder_ids": [selected["folder_id"]],
                "selected_image_ids": [first.id],
            },
            actor=self.user,
        )
        self.db.commit()
        self._execute_one()  # prepare record context
        self._execute_one()  # create record-input human task
        self.db.refresh(run)
        self.assertEqual(run.status, "waiting_human")
        record_task = (
            self.db.query(ExecutionHumanTask)
            .filter_by(run_id=run.id, status="open")
            .one()
        )
        self.assertEqual(
            record_task.node_run.input_data["record_context"]["task_kind"],
            "microscopy_record_input",
        )
        self.assertEqual(
            record_task.node_run.input_data["record_context"][
                "selected_image_ids"
            ],
            [first.id],
        )

    def test_numbered_folder_images_are_prioritized_before_result_limit(self):
        target = self._image("26A029794-result/deep/target.bmp")
        target.modified_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for index in range(3):
            decoy = self._image(
                f"archive/other/26A029794-note-{index}.bmp"
            )
            decoy.modified_at = datetime(
                2026, 1, index + 1, tzinfo=timezone.utc
            )
        self.db.commit()

        with patch(
            "app.execution.electron_microscopy.MAX_INDEXED_IMAGES", 2
        ):
            match = electron_microscopy_match(
                self.db, inspection_number="26A029794"
            )

        self.assertEqual(match["folder_match_count"], 1)
        self.assertEqual(match["image_count"], 1)
        self.assertEqual(match["images"][0]["id"], target.id)
        self.assertFalse(match["truncated"])

    def test_electron_refresh_scans_all_nested_levels(self):
        job, _ = queue_refresh(
            self.db,
            root_id="electron_microscopy_records",
            actor=self.user,
            max_depth=1,
        )
        self.assertIsNone(job.max_depth)
        job.status = "completed"
        self.root.last_scan_finished_at = None
        self.db.commit()
        created = enqueue_due_index_jobs(self.db, interval_seconds=10)
        self.assertGreaterEqual(created, 1)
        queued = self.db.query(type(job)).filter_by(status="queued").one()
        self.assertIsNone(queued.max_depth)


if __name__ == "__main__":
    unittest.main()
