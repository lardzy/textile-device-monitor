from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.execution.catalog import (
    bind_user_role,
    create_workflow,
    ensure_default_rbac,
    publish_workflow,
)
from app.execution.engine import (
    claim_human_task,
    claim_next_node,
    create_run,
    execute_claimed_node,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionCategory,
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionStorageRoot,
    ExecutionUser,
)
from app.execution.persistence import (
    claim_index_job,
    electron_groups_from_index,
    ensure_storage_roots,
    process_index_job,
    queue_refresh,
    register_persistence_executors,
    search_index,
)
from app.execution.security import hash_password


class ExecutionPersistentIndexTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.source = root / "source"
        self.runtime = root / "runtime"
        self.publish = root / "publish"
        for path in (
            self.source / "2026-特种毛",
            self.source / "2026-麻棉",
            self.source / "2026-电镜",
            self.runtime,
            self.publish,
        ):
            path.mkdir(parents=True)
        (self.source / "2026-特种毛" / "260001_record.xlsx").write_bytes(
            b"test"
        )
        for suffix in (".SIF", ".bmp", ".txt"):
            (self.source / "2026-电镜" / f"260001{suffix}").write_bytes(
                suffix.encode()
            )

        self.original_settings = (
            settings.EXECUTION_SOURCE_ROOT,
            settings.EXECUTION_RUNTIME_ROOT,
            settings.EXECUTION_PUBLISH_ROOT,
        )
        settings.EXECUTION_SOURCE_ROOT = str(self.source)
        settings.EXECUTION_RUNTIME_ROOT = str(self.runtime)
        settings.EXECUTION_PUBLISH_ROOT = str(self.publish)
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        self.user = ExecutionUser(
            username="indexer",
            display_name="索引测试",
            password_hash=hash_password("index-password"),
            role="admin",
        )
        self.db.add(self.user)
        self.db.flush()
        bind_user_role(self.db, self.user, "admin", created_by_id=self.user.id)
        ensure_storage_roots(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        (
            settings.EXECUTION_SOURCE_ROOT,
            settings.EXECUTION_RUNTIME_ROOT,
            settings.EXECUTION_PUBLISH_ROOT,
        ) = self.original_settings
        self.tempdir.cleanup()

    def _refresh(self, root_id):
        job, duplicate = queue_refresh(
            self.db,
            root_id=root_id,
            actor=self.user,
        )
        self.assertFalse(duplicate)
        self.db.commit()
        claimed = claim_index_job(self.db, worker_id="index-worker")
        self.assertEqual(claimed.id, job.id)
        token = claimed.lease_token
        self.db.commit()
        process_index_job(self.db, claimed.id, token)
        self.db.commit()
        return claimed

    def _execute_one(self):
        node = claim_next_node(
            self.db,
            worker_id="index-flow-worker",
            lease_seconds=30,
        )
        self.assertIsNotNone(node)
        node_id = node.id
        token = node.lease_token
        self.db.commit()
        execute_claimed_node(self.db, node_id, token)
        self.db.commit()

    def test_scan_is_persisted_and_search_never_rescans(self):
        job = self._refresh("special_wool_records")
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.total_count, 1)
        results = search_index(
            self.db,
            inspection_number="260001",
            root_ids=["special_wool_records"],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["relative_path"], "260001_record.xlsx")

        self.db.close()
        self.db = self.Session()
        restored = search_index(
            self.db,
            inspection_number="260001",
            root_ids=["special_wool_records"],
        )
        self.assertEqual(restored, results)

        # SQL LIKE 通配符必须按普通检验编号字符处理，不能放大查询范围。
        self.assertEqual(
            search_index(
                self.db,
                inspection_number="260%",
                root_ids=["special_wool_records"],
            ),
            [],
        )

    def test_search_and_refresh_never_expose_runtime_or_publish_roots(self):
        staging_root = (
            self.db.query(ExecutionStorageRoot)
            .filter_by(root_id="execution_staging")
            .one()
        )
        self.db.add(
            ExecutionFileIndexEntry(
                storage_root_id=staging_root.id,
                relative_path="other-user/260001-secret.xlsx",
                filename="260001-secret.xlsx",
                extension=".xlsx",
                file_kind="workbook",
                inspection_number="260001",
                size_bytes=10,
                modified_at=staging_root.updated_at,
                fingerprint="10:1",
                metadata_json={},
            )
        )
        self.db.commit()

        self.assertEqual(
            search_index(
                self.db,
                inspection_number="260001",
                root_ids=["execution_staging"],
            ),
            [],
        )
        with self.assertRaises(ExecutionApiError) as captured:
            queue_refresh(
                self.db,
                root_id="execution_staging",
                actor=self.user,
            )
        self.assertEqual(
            captured.exception.code,
            "storage_root_not_indexable",
        )

    def test_clean_rescan_marks_missing_entries_without_deleting_history(self):
        self._refresh("special_wool_records")
        (self.source / "2026-特种毛" / "260001_record.xlsx").unlink()
        self._refresh("special_wool_records")
        self.assertEqual(
            search_index(
                self.db,
                inspection_number="260001",
                root_ids=["special_wool_records"],
            ),
            [],
        )
        entry = self.db.query(ExecutionFileIndexEntry).one()
        self.assertIsNotNone(entry.missing_since)

    def test_electron_three_file_group_is_built_from_database_index(self):
        self._refresh("electron_microscopy_records")
        groups = electron_groups_from_index(
            self.db,
            root_ids=["electron_microscopy_records"],
            inspection_number="260001",
        )
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["complete"])
        self.assertTrue(groups[0]["id"].startswith("electron:"))
        self.assertEqual(len(groups[0]["files"]), 3)
        self.assertTrue(
            all(
                item.get("id") and item.get("fingerprint")
                for item in groups[0]["files"]
            )
        )

    def test_electron_human_selection_is_rebuilt_from_server_candidates(self):
        self._refresh("electron_microscopy_records")
        category = ExecutionCategory(
            key="electron-selection-test",
            name="电镜选择测试",
        )
        self.db.add(category)
        self.db.flush()
        definition = {
            "schema_version": "1.0",
            "metadata": {"name": "电镜选择"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "inspection_number": {"type": "string"},
                },
                "required": ["inspection_number"],
            },
            "global_schema": {"type": "object", "properties": {}},
            "root_slots": [
                {
                    "name": "source",
                    "root_id": "electron_microscopy_records",
                    "access": "read",
                }
            ],
            "credential_slots": [],
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "name": "开始",
                    "config": {},
                },
                {
                    "id": "group",
                    "type": "electron.group",
                    "name": "采集分组",
                    "config": {
                        "root_id": "electron_microscopy_records",
                        "recent_days": 7,
                        "limit": 6,
                    },
                    "input_mapping": {
                        "inspection_number":
                            "$.inputs.inspection_number"
                    },
                },
                {
                    "id": "select",
                    "type": "human.file_selection",
                    "name": "人工选择",
                    "config": {"allow_multiple": True},
                    "input_mapping": {
                        "groups": "$.nodes.group.output.groups"
                    },
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "name": "结束",
                    "config": {},
                    "input_mapping": {
                        "selection": "$.nodes.select.output"
                    },
                },
            ],
            "edges": [
                {"source": "start", "target": "group"},
                {"source": "group", "target": "select"},
                {"source": "select", "target": "end"},
            ],
        }
        workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="electron-selection-test",
            category_id=category.id,
            name="电镜选择测试",
            description=None,
            definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=workflow.id,
            expected_revision=1,
            actor=self.user,
            release_note="test",
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="260001",
            input_data={},
            global_data={},
            idempotency_key="electron-selection-run",
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        self._execute_one()
        task = (
            self.db.query(ExecutionHumanTask)
            .filter_by(run_id=run.id)
            .one()
        )
        group = task.node_run.input_data["groups"][0]
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()

        with self.assertRaises(ExecutionApiError) as captured:
            submit_human_task(
                self.db,
                task_id=task.id,
                expected_revision=task.revision,
                data={"selected_files": ["electron:not-offered"]},
                actor=self.user,
            )
        self.assertEqual(
            captured.exception.code,
            "file_candidate_not_offered",
        )

        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={
                "selected_files": [
                    {
                        "id": group["id"],
                        "root_id": "forged-root",
                        "relative_path": "../../forged",
                    }
                ]
            },
            actor=self.user,
        )
        self.db.commit()
        self.assertEqual(
            task.result_data["selected_files"][0]["id"],
            group["id"],
        )
        self.assertEqual(
            {
                item["root_id"]
                for item in task.result_data["selected_files"][0]["files"]
            },
            {"electron_microscopy_records"},
        )


if __name__ == "__main__":
    unittest.main()
