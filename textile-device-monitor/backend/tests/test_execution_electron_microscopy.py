from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from project_rule_fixtures import install_rule_fixtures
from app.execution.catalog import bind_user_role, ensure_default_catalog, ensure_default_rbac
from app.execution.electron_microscopy import (
    ELECTRON_NODE_TYPE,
    cached_task_snapshot,
    claim_task_snapshot_refresh,
    complete_task_snapshot_refresh,
    electron_microscopy_match,
    request_task_snapshot_refresh,
    task_snapshot_status,
)
from app.api.execution import preview_indexed_image
from app.execution.paper_fiber import (
    PAPER_FIBER_PROJECT_NAME,
    PAPER_FIBER_TEST_METHOD,
)
from app.execution.engine import (
    _normalize_human_submission,
    claim_human_task,
    claim_next_node,
    create_run,
    execute_claimed_node,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionNodeRun,
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
)
from app.execution.regenerated_fiber import catalog_recommendations
from app.execution.security import hash_password
from app.execution.validation import (
    definition_checksum,
    validate_definition,
    workflow_contract_checksum,
)


class ElectronMicroscopyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.tempdir.name) / "2026-电镜"
        self.root_path.mkdir(parents=True)
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        install_rule_fixtures(self.db)
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
                "special_wool_occupied_numbers": ["26A029794"],
                "projects": [
                    {
                        "task_check_item_id": "sha256:1111111111111111",
                        "check_item_id": "sha256:2222222222222222",
                        "check_item_no": "5103.5",
                        "check_item_name": project_name,
                        "check_method": "GB/T 36422-2018",
                        "check_count": 1,
                        "register_count": 0,
                        "seq_num": 1,
                    }
                ],
            },
        )
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029794"
        )["snapshot"]
        self.assertEqual(cached["schema_version"], 5)
        self.assertEqual(
            cached["special_wool_occupied_numbers"], ["26A029794"]
        )
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

    def _normalize_record_input(self, project, data):
        return _normalize_human_submission(
            self.db,
            run=SimpleNamespace(),
            node_run=SimpleNamespace(
                node_type="human.input",
                input_data={
                    "record_context": {
                        "task_kind": "microscopy_record_input",
                        "projects": [project],
                        "check_basis_options": ["GB/T 36422-2018"],
                    }
                },
            ),
            data={"selected_project_key": project["project_key"], **data},
        )
















    def test_task_cache_queues_once_uses_token_and_refreshes_when_stale(self):
        first = cached_task_snapshot(self.db, inspection_number="26A029794")
        second = cached_task_snapshot(self.db, inspection_number="26a029794")
        self.assertTrue(first["refresh_queued"])
        self.assertEqual(first["refresh_status"], "queued")
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
        self.assertEqual(active["refresh_status"], "running")
        with self.assertRaises(ExecutionApiError) as raised:
            complete_task_snapshot_refresh(
                self.db,
                inspection_number="26A029794",
                bridge_id="bridge-a",
                claim_token="00000000-0000-0000-0000-000000000000",
                snapshot={
                    "projects": [],
                    "special_wool_occupied_numbers": [],
                },
            )
        self.assertEqual(raised.exception.code, "task_snapshot_claim_lost")
        complete_task_snapshot_refresh(
            self.db,
            inspection_number="26A029794",
            bridge_id="bridge-a",
            claim_token=row.claim_token,
            snapshot={
                "projects": [],
                "special_wool_occupied_numbers": [],
            },
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

    def test_v3_snapshot_is_refreshed_after_occupancy_contract_upgrade(self):
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
                    "schema_version": 5,
                    "special_wool_occupied_numbers": [],
                    "projects": [
                        {
                            "check_item_name": "纤维微观形貌",
                            "check_method": "GB/T 36422-2018",
                            "register_count": 0,
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
                "special_wool_occupied_numbers": [],
                "projects": [
                    {
                        "check_item_name": "纤维平均直径",
                        "check_method": "其它方法",
                        "register_count": 0,
                    }
                ],
            },
        )
        self.db.commit()

        cached = cached_task_snapshot(
            self.db, inspection_number="26A029799"
        )
        self.assertEqual(cached["cache_state"], "ready")
        self.assertEqual(cached["snapshot"]["schema_version"], 5)
        self.assertIsNone(
            cached["snapshot"]["projects"][0]["task_check_item_id"]
        )


    def test_index_preview_checks_fingerprint_and_image_allowlist(self):
        entry = self._image("26A029794-lisy/纵面/preview.BMP")
        self.db.commit()
        response = preview_indexed_image(entry.id, None, self.db)
        self.assertEqual(response.media_type, "image/bmp")
        (self.root_path / entry.relative_path).write_bytes(b"changed")
        with self.assertRaises(ExecutionApiError) as raised:
            preview_indexed_image(entry.id, None, self.db)
        self.assertEqual(raised.exception.code, "indexed_image_stale")



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
