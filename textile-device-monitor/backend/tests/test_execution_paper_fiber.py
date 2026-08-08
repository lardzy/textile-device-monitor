from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import xlwt
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.execution.catalog import (
    _paper_fiber_gbt4688_qualitative_definition,
    _paper_fiber_gbt4688_qualitative_readonly_definition,
    bind_user_role,
    ensure_default_catalog,
    ensure_default_rbac,
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
from app.execution.paper_fiber import (
    PAPER_FIBER_NODE_TYPE,
    PAPER_FIBER_PROFILE_VERSION,
    PAPER_FIBER_PROJECT_NAME,
    PAPER_FIBER_ROOT_ID,
    PAPER_FIBER_TEST_METHOD,
    PAPER_FIBER_WORKFLOW_SLUG,
    _read_result_profile,
    contains_standalone_100,
    paper_fiber_match,
)
from app.execution.persistence import (
    IndexedFile,
    ensure_storage_roots,
    persist_scan,
    register_persistence_executors,
)
from app.execution.regenerated_fiber import catalog_recommendations
from app.execution.security import hash_password
from app.execution.storage import ArtifactRef, FileGateway, StorageRoot
from app.execution.validation import (
    definition_checksum,
    validate_definition,
    workflow_contract_checksum,
)


class PaperFiberBackendTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.tempdir.name) / "纸类"
        self.root_path.mkdir(parents=True)
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        self.user = ExecutionUser(
            username="paper-admin",
            display_name="纸类测试",
            password_hash=hash_password("paper-password"),
            role="admin",
        )
        self.db.add(self.user)
        self.db.flush()
        bind_user_role(
            self.db,
            self.user,
            "admin",
            created_by_id=self.user.id,
        )
        self.root = ExecutionStorageRoot(
            root_id=PAPER_FIBER_ROOT_ID,
            name="纸类原始记录",
            local_path=str(self.root_path),
            access_mode="read",
            category_key="other",
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

    def _xls(self, relative_path: str, value: object, *, sheet="Sheet1") -> Path:
        path = self.root_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = xlwt.Workbook()
        worksheet = workbook.add_sheet(sheet)
        if value is not None:
            worksheet.write(31, 22, value)
        workbook.save(str(path))
        return path

    def _xlsx(
        self,
        relative_path: str,
        value: object,
        *,
        sheet="Sheet1",
    ) -> Path:
        path = self.root_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = sheet
        worksheet["W32"] = value
        workbook.save(path)
        workbook.close()
        return path

    def _index(self, path: Path) -> ExecutionFileIndexEntry:
        stat = path.stat()
        entry = ExecutionFileIndexEntry(
            storage_root_id=self.root.id,
            relative_path=path.relative_to(self.root_path).as_posix(),
            filename=path.name,
            extension=path.suffix.casefold(),
            file_kind="workbook",
            inspection_number=None,
            group_key=path.stem.casefold(),
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            metadata_json={
                "metadata_version": 1,
                "parse_status": "deferred",
            },
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def _task_snapshot(
        self,
        number: str,
        *,
        projects: list[dict[str, object]] | None = None,
    ) -> None:
        self.db.add(
            ExecutionTaskSnapshotCache(
                inspection_number=number,
                status="ready",
                snapshot={
                    "schema_version": 4,
                    "inspection_number": number,
                    "sample_name": None,
                    "sample_names": [],
                    "check_basis": None,
                    "projects": projects
                    if projects is not None
                    else [
                        {
                            "project_key": "task-project:paper-test",
                            "task_check_item_id": "sha256:1111111111111111",
                            "check_item_id": "sha256:2222222222222222",
                            "check_item_no": "51.113K",
                            "check_item_name": PAPER_FIBER_PROJECT_NAME,
                            "check_method": PAPER_FIBER_TEST_METHOD,
                            "check_count": 1,
                            "seq_num": 1,
                            "sample_identify": None,
                            "remark": None,
                            "give_judgement": 0,
                        }
                    ],
                    "special_wool_occupied_numbers": [],
                },
                fetched_at=utcnow(),
                expires_at=utcnow() + timedelta(minutes=15),
            )
        )
        self.db.flush()

    def _execute_one(self):
        node = claim_next_node(
            self.db,
            worker_id="paper-worker",
            lease_seconds=30,
        )
        self.assertIsNotNone(node)
        node_id = node.id
        lease_token = node.lease_token
        self.db.commit()
        execute_claimed_node(self.db, node_id, lease_token)
        self.db.commit()
        return node

    def test_standalone_100_detection_uses_numeric_boundaries_and_nfkc(self):
        for value in ("木浆 100", "100%木浆", "木浆 １００", 100, 100.0):
            with self.subTest(value=value):
                self.assertTrue(contains_standalone_100(value))
        for value in (
            "木浆",
            "棉100，粘纤0",
            "A100",
            "1000",
            "2100",
            "100.5",
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(contains_standalone_100(value))

    def test_reads_sheet1_w32_from_ole_and_ooxml(self):
        ole = self._xls("26W006687/result.xls", "木浆 100")
        ole_profile = _read_result_profile(ole)
        self.assertEqual(ole_profile["status"], "matched")
        self.assertEqual(ole_profile["qualitative_result"], "木浆 100")
        self.assertTrue(ole_profile["contains_standalone_100"])
        self.assertEqual(ole_profile["unit"], "%")

        ooxml = self._xlsx("26W006701/result.xlsx", "草浆、木浆、竹浆")
        ooxml_profile = _read_result_profile(ooxml)
        self.assertEqual(ooxml_profile["status"], "matched")
        self.assertEqual(
            ooxml_profile["qualitative_result"],
            "草浆、木浆、竹浆",
        )
        self.assertFalse(ooxml_profile["contains_standalone_100"])
        self.assertEqual(ooxml_profile["unit"], "")

    def test_empty_formula_and_wrong_sheet_are_not_successful_results(self):
        formula = self._xlsx("26W006701/formula.xlsx", "=1+1")
        self.assertEqual(_read_result_profile(formula)["status"], "result_empty")
        wrong_sheet = self._xls(
            "26W006701/wrong.xls",
            "木浆 100",
            sheet="结果",
        )
        self.assertEqual(
            _read_result_profile(wrong_sheet)["status"],
            "worksheet_missing",
        )

    def test_matching_uses_numbered_first_level_folder_and_same_task_project(self):
        matched = self._xls("26W006701-草木竹浆/record.xls", "木浆 100")
        direct = self._xls("26W006701-direct.xls", "不应匹配")
        deep = self._xls("26W006701-归档/旧/record.xls", "不应匹配")
        matched_entry = self._index(matched)
        self._index(direct)
        self._index(deep)
        self._task_snapshot("26W006701")
        self.db.commit()

        result = paper_fiber_match(
            self.db,
            inspection_number="26W006701",
            gateway=FileGateway(
                [StorageRoot(root_id=PAPER_FIBER_ROOT_ID, path=self.root_path)]
            ),
        )
        self.assertTrue(result["full_match"])
        self.assertEqual(
            result["matched_conditions"],
            ["source_root", "folder", "task_item_name", "test_method"],
        )
        self.assertEqual(result["folder_match_count"], 1)
        self.assertEqual(result["workbook_match_count"], 1)
        self.assertEqual([item["id"] for item in result["candidates"]], [matched_entry.id])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["folder_name"], "26W006701-草木竹浆")
        self.assertEqual(candidate["result"]["worksheet"], "Sheet1")
        self.assertEqual(candidate["result"]["cell"], "W32")
        self.assertEqual(candidate["result"]["w32_value"], "木浆 100")
        self.assertEqual(candidate["result"]["unit"], "%")
        self.assertEqual(
            result["candidate_preview"],
            {
                "name": "record.xls",
                "relative_path": "26W006701-草木竹浆/record.xls",
                "suffix": ".xls",
                "qualitative_result": "木浆 100",
                "result": {
                    "worksheet": "Sheet1",
                    "cell": "W32",
                    "w32_value": "木浆 100",
                    "unit": "%",
                },
            },
        )
        self.assertTrue(result["cache_updated"])

        second = paper_fiber_match(
            self.db,
            inspection_number="26W006701",
            gateway=FileGateway(
                [StorageRoot(root_id=PAPER_FIBER_ROOT_ID, path=self.root_path)]
            ),
        )
        self.assertFalse(second["cache_updated"])

    def test_stale_index_fingerprint_is_refreshed_before_offering_candidate(self):
        path = self._xls("26W006701/record.xls", "木浆 100")
        entry = self._index(path)
        self._task_snapshot("26W006701")
        self.db.commit()

        gateway = FileGateway(
            [StorageRoot(root_id=PAPER_FIBER_ROOT_ID, path=self.root_path)]
        )
        first = paper_fiber_match(
            self.db,
            inspection_number="26W006701",
            gateway=gateway,
        )
        self.assertEqual(first["candidates"][0]["result"]["w32_value"], "木浆 100")
        self.assertTrue(first["cache_updated"])

        # 共享盘文件被重新保存，但后台索引尚未扫描（索引指纹仍旧）
        self._xls("26W006701/record.xls", "草浆、木浆、竹浆")
        bumped = path.stat()
        os.utime(path, ns=(bumped.st_atime_ns, bumped.st_mtime_ns + 1_000_000_000))
        live = path.stat()
        live_fingerprint = f"{live.st_size}:{live.st_mtime_ns}"
        self.db.refresh(entry)
        self.assertNotEqual(entry.fingerprint, live_fingerprint)

        result = paper_fiber_match(
            self.db,
            inspection_number="26W006701",
            gateway=gateway,
        )
        candidate = result["candidates"][0]
        # 候选必须携带实时指纹，否则提交时会被 file_candidate_stale 永久拒绝
        self.assertEqual(candidate["fingerprint"], live_fingerprint)
        self.assertEqual(candidate["result"]["w32_value"], "草浆、木浆、竹浆")
        self.assertEqual(candidate["result"]["unit"], "")
        self.assertEqual(candidate["result"]["contains_standalone_100"], False)
        self.assertTrue(result["cache_updated"])
        # 生产路径由 worker 在节点结束时提交；这里显式提交后验证落库指纹
        self.db.commit()
        self.db.refresh(entry)
        self.assertEqual(entry.fingerprint, live_fingerprint)

    def test_task_name_and_method_from_different_projects_do_not_full_match(self):
        path = self._xls("26W006701/record.xls", "木浆 100")
        self._index(path)
        self._task_snapshot(
            "26W006701",
            projects=[
                {
                    "project_key": "task-project:name-only",
                    "check_item_name": PAPER_FIBER_PROJECT_NAME,
                    "check_method": "其它方法",
                },
                {
                    "project_key": "task-project:method-only",
                    "check_item_name": "其它项目",
                    "check_method": PAPER_FIBER_TEST_METHOD,
                },
            ],
        )
        self.db.commit()
        result = paper_fiber_match(
            self.db,
            inspection_number="26W006701",
            gateway=FileGateway(
                [StorageRoot(root_id=PAPER_FIBER_ROOT_ID, path=self.root_path)]
            ),
        )
        self.assertFalse(result["full_match"])
        self.assertEqual(len(set(result["matched_conditions"]) & {
            "task_item_name", "test_method"
        }), 1)

    def test_default_workflow_is_valid_idempotent_and_recommended_first(self):
        path = self._xls("26W006701/record.xls", "木浆 100")
        self._index(path)
        self._task_snapshot("26W006701")
        self.db.commit()
        ensure_default_catalog(self.db)
        self.db.commit()

        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        self.assertEqual(workflow.category.key, "other")
        self.assertEqual(len(workflow.versions), 1)
        definition = _paper_fiber_gbt4688_qualitative_definition()
        self.assertTrue(validate_definition(definition).valid)
        select = next(node for node in definition["nodes"] if node["id"] == "select")
        self.assertIs(select["config"]["allow_multiple"], False)
        self.assertEqual(
            [node["id"] for node in definition["nodes"][-4:]],
            ["upload-record", "review-record", "register-result", "end"],
        )
        self.assertEqual(
            workflow.capabilities,
            {"read": True, "write": True, "external_write": True},
        )

        items, _ = catalog_recommendations(
            self.db,
            inspection_number="26W006701",
            preferred_categories=["other"],
            include_hidden=False,
        )
        self.assertEqual(items[0]["workflow_id"], workflow.id)
        self.assertEqual(items[0]["state"], "full_match")
        self.assertEqual(items[0]["score"], 5)
        self.assertEqual(
            items[0]["matched_conditions"],
            ["category", "source_root", "folder", "task_item_name", "test_method"],
        )
        self.assertEqual(items[0]["candidate_count"], 1)
        self.assertEqual(
            items[0]["candidate_preview"]["result"],
            {
                "worksheet": "Sheet1",
                "cell": "W32",
                "w32_value": "木浆 100",
                "unit": "%",
            },
        )

    def test_single_candidate_selection_auto_submits_without_human_task(self):
        path = self._xls("26W006701/record.xls", "木浆 100")
        entry = self._index(path)
        self._task_snapshot("26W006701")
        self.db.commit()
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="26W006701",
            input_data={},
            global_data={},
            idempotency_key="paper-auto-submit-run",
        )
        self.db.commit()
        self._execute_one()  # start
        self._execute_one()  # query and W32 read
        self._execute_one()  # select：唯一候选应自动提交，不创建人工任务
        selection = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="select")
            .one()
        )
        self.assertEqual(selection.status, "succeeded")
        self.assertTrue(selection.output_data["auto_submitted"])
        self.assertEqual(
            selection.output_data["auto_submit_reason"], "single_candidate"
        )
        self.assertEqual(selection.output_data["primary_file_id"], entry.id)
        self.assertEqual(
            selection.output_data["primary_file"]["result"]["w32_value"],
            "木浆 100",
        )
        self.assertEqual(
            self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).count(),
            0,
        )
        self.db.refresh(run)
        self.assertEqual(run.status, "running")

    def test_workflow_rejects_multiple_files_and_auto_marks_single_primary(self):
        first = self._xls("26W006701/first.xls", "木浆 100")
        second = self._xls("26W006701/second.xls", "草浆、木浆")
        first_entry = self._index(first)
        second_entry = self._index(second)
        self._task_snapshot("26W006701")
        self.db.commit()
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="26W006701",
            input_data={},
            global_data={},
            idempotency_key="paper-selection-run",
        )
        self.db.commit()
        self._execute_one()  # start
        self._execute_one()  # query and W32 read
        self._execute_one()  # human task
        task = (
            self.db.query(ExecutionHumanTask)
            .filter_by(run_id=run.id)
            .one()
        )
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
                data={"selected_files": [first_entry.id, second_entry.id]},
                actor=self.user,
            )
        self.assertEqual(
            raised.exception.code,
            "file_selection_multiple_not_allowed",
        )

        self.db.rollback()
        task = self.db.get(ExecutionHumanTask, task.id)
        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={"selected_files": [first_entry.id]},
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "running")
        selection = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="select")
            .one()
        )
        self.assertEqual(selection.status, "succeeded")
        self.assertEqual(
            selection.output_data["primary_file_id"], first_entry.id
        )
        self.assertEqual(
            selection.output_data["primary_file"]["result"]["w32_value"],
            "木浆 100",
        )

    def test_untouched_readonly_catalog_is_upgraded_without_new_slug(self):
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        category_id = workflow.category_id
        for version in list(workflow.versions):
            self.db.delete(version)
        self.db.delete(workflow)
        self.db.flush()

        definition = _paper_fiber_gbt4688_qualitative_readonly_definition()
        capabilities = {"read": True, "write": False}
        workflow = ExecutionWorkflow(
            slug=PAPER_FIBER_WORKFLOW_SLUG,
            category_id=category_id,
            name="纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020",
            description="旧的只读默认流程",
            draft_definition=definition,
            draft_revision=1,
            published_version_number=1,
            capabilities=capabilities,
            required_input_count=1,
            is_enabled=True,
        )
        self.db.add(workflow)
        self.db.flush()
        self.db.add(
            ExecutionWorkflowVersion(
                workflow_id=workflow.id,
                version_number=1,
                schema_version="1.0",
                definition=definition,
                checksum=definition_checksum(definition),
                capabilities=capabilities,
                contract_checksum=workflow_contract_checksum(
                    definition, capabilities
                ),
                release_note="旧的只读默认流程",
            )
        )
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(len(workflow.versions), 2)
        self.assertEqual(
            workflow.capabilities,
            {"read": True, "write": True, "external_write": True},
        )
        self.assertIn(
            "register-result",
            {node["id"] for node in workflow.draft_definition["nodes"]},
        )

    def test_untouched_outdated_full_catalog_follows_current_definition(self):
        # 模拟升级前的线上库：v1 为无 auto_submit_single_candidate 的旧完整定义
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        outdated = _paper_fiber_gbt4688_qualitative_definition()
        select = next(node for node in outdated["nodes"] if node["id"] == "select")
        select["config"].pop("auto_submit_single_candidate", None)
        version_one = next(
            version for version in workflow.versions
            if version.version_number == 1
        )
        workflow.draft_definition = outdated
        version_one.definition = outdated
        version_one.checksum = definition_checksum(outdated)
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(len(workflow.versions), 2)
        upgraded_select = next(
            node for node in workflow.draft_definition["nodes"]
            if node["id"] == "select"
        )
        self.assertIs(
            upgraded_select["config"]["auto_submit_single_candidate"], True
        )
        # 旧版本保持原样，历史运行不受影响
        self.assertNotIn(
            "auto_submit_single_candidate",
            version_one.definition["nodes"][
                next(
                    index
                    for index, node in enumerate(version_one.definition["nodes"])
                    if node["id"] == "select"
                )
            ]["config"],
        )

    def test_background_index_defers_paper_workbook_content(self):
        path = self._xls("26W006701/record.xls", "木浆 100")
        stat = path.stat()
        item = IndexedFile(
            ref=ArtifactRef(
                PAPER_FIBER_ROOT_ID,
                "26W006701/record.xls",
            ),
            name=path.name,
            suffix=".xls",
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            category="other",
        )
        gateway = FileGateway(
            [StorageRoot(root_id=PAPER_FIBER_ROOT_ID, path=self.root_path)]
        )
        with patch(
            "app.execution.persistence.extract_index_metadata",
            side_effect=AssertionError("workbook should not be opened"),
        ):
            added, updated, removed = persist_scan(
                self.db,
                root=self.root,
                files=[item],
                scan_complete=True,
                scan_max_depth=1,
                gateway=gateway,
            )
        self.assertEqual((added, updated, removed), (1, 0, 0))
        self.db.flush()
        indexed = (
            self.db.query(ExecutionFileIndexEntry)
            .filter_by(relative_path="26W006701/record.xls")
            .one()
        )
        self.assertEqual(indexed.metadata_json["parse_status"], "deferred")

    def test_storage_root_uses_nested_shared_directory(self):
        source = Path(self.tempdir.name) / "10特纤" / "02-检验"
        paper = (
            source
            / "08-其他"
            / "2022-纸、纸板和纸浆纤维鉴别分析"
        )
        paper.mkdir(parents=True)
        runtime = Path(self.tempdir.name) / "runtime"
        publish = Path(self.tempdir.name) / "publish"
        runtime.mkdir()
        publish.mkdir()
        with (
            patch.object(settings, "EXECUTION_SOURCE_ROOT", str(source)),
            patch.object(settings, "EXECUTION_RUNTIME_ROOT", str(runtime)),
            patch.object(settings, "EXECUTION_PUBLISH_ROOT", str(publish)),
        ):
            ensure_storage_roots(self.db)
        root = (
            self.db.query(ExecutionStorageRoot)
            .filter_by(root_id=PAPER_FIBER_ROOT_ID)
            .one()
        )
        self.assertEqual(Path(root.local_path), paper)
        self.assertTrue(root.is_available)


if __name__ == "__main__":
    unittest.main()
