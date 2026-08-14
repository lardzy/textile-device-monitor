from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
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
    NodeExecutionContext,
    _auto_complete_paper_existing_record_decision,
    _auto_complete_paper_judgement,
    _normalize_human_submission,
    _paper_existing_record_form_schema,
    _paper_judgement_form_schema,
    _reopen_paper_judgement_for_standard_value,
    claim_human_task,
    claim_next_node,
    create_run,
    effective_human_task_form_schema,
    execute_claimed_node,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionEdgeRun,
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionNodeAttempt,
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
    _paper_fiber_executor,
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

    def _xls(
        self,
        relative_path: str,
        value: object,
        *,
        sheet="Sheet1",
        m32: object = None,
    ) -> Path:
        path = self.root_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = xlwt.Workbook()
        worksheet = workbook.add_sheet(sheet)
        if value is not None:
            worksheet.write(31, 22, value)
        if m32 is not None:
            worksheet.write(31, 12, m32)
        workbook.save(str(path))
        return path

    def _xlsx(
        self,
        relative_path: str,
        value: object,
        *,
        sheet="Sheet1",
        m32: object = None,
    ) -> Path:
        path = self.root_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = sheet
        worksheet["W32"] = value
        if m32 is not None:
            worksheet["M32"] = m32
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
                    "schema_version": 5,
                    "inspection_number": number,
                    "sample_name": None,
                    "sample_names": [],
                    "check_basis": None,
                    "projects": [
                        {**project, "register_count": project.get("register_count", 0)}
                        for project in projects
                    ]
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
                            "register_count": 0,
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
        ole = self._xls("26W006687/result.xls", "木浆 100", m32="100%木浆")
        ole_profile = _read_result_profile(ole)
        self.assertEqual(ole_profile["status"], "matched")
        self.assertEqual(ole_profile["qualitative_result"], "木浆 100")
        self.assertEqual(ole_profile["m32_value"], "100%木浆")
        self.assertTrue(ole_profile["contains_standalone_100"])
        self.assertEqual(ole_profile["unit"], "%")

        ooxml = self._xlsx("26W006701/result.xlsx", "草浆、木浆、竹浆")
        ooxml_profile = _read_result_profile(ooxml)
        self.assertEqual(ooxml_profile["status"], "matched")
        self.assertEqual(
            ooxml_profile["qualitative_result"],
            "草浆、木浆、竹浆",
        )
        # 未写 M32 时数据源为空，不影响结果读取
        self.assertIsNone(ooxml_profile["m32_value"])
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
                    "m32_value": "",
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
            [node["id"] for node in definition["nodes"][3:]],
            [
                "upload-record",
                "review-record",
                "registration-decision",
                "registration-branch",
                "judgement-input",
                "register-result",
                "end",
                "cancelled-end",
            ],
        )
        nodes_by_id = {
            node["id"]: node
            for node in definition["nodes"]
        }
        self.assertTrue(
            all(
                nodes_by_id[node_id]["type"].startswith("external.")
                for node_id in (
                    "upload-record",
                    "review-record",
                    "register-result",
                )
            )
        )
        self.assertNotIn(
            "human.confirm",
            {node["type"] for node in definition["nodes"]},
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
                "m32_value": "",
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

    def _failed_query_run(self, idempotency_key: str):
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
            idempotency_key=idempotency_key,
        )
        self.db.commit()
        self._execute_one()  # start
        self._execute_one()  # query
        query = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="query")
            .one()
        )
        self.db.refresh(run)
        return run, query

    def test_query_fails_fast_with_task_snapshot_pending_when_cache_missing(
        self,
    ):
        self._index(self._xls("26W006701/record.xls", "木浆 100"))
        self.db.commit()

        # 等待时长置 0，直接走超时兜底路径
        with patch.object(settings, "EXECUTION_TASK_SNAPSHOT_WAIT_SECONDS", 0):
            run, query = self._failed_query_run("paper-query-snapshot-pending")

        self.assertEqual(query.status, "failed")
        self.assertEqual(query.error_code, "task_snapshot_pending")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "task_snapshot_pending")

    def _query_context(self, run):
        node_run = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="query")
            .one()
        )
        node = {
            "id": "query",
            "type": PAPER_FIBER_NODE_TYPE,
            "type_version": 1,
            "config": {"limit": 6, "require_full_task_match": True},
        }
        return NodeExecutionContext(
            db=self.db,
            run=run,
            node_run=node_run,
            node=node,
            input_data={"inspection_number": "26W006701"},
            worker_id="paper-worker",
            lease_token="",
        )

    def test_query_waits_for_pending_snapshot_and_continues_when_ready(self):
        run = self._paper_run("paper-query-wait-ready")
        context = self._query_context(run)
        pending_match = {
            "matched_conditions": ["source_root", "folder"],
            "task_cache_state": "pending",
            "candidates": [{"id": "file-1"}],
            "task_snapshot": None,
            "matched_task_project": None,
        }
        ready_match = {
            "matched_conditions": [
                "source_root",
                "folder",
                "task_item_name",
                "test_method",
            ],
            "task_cache_state": "ready",
            "candidates": [{"id": "file-1"}],
            "task_snapshot": {"projects": []},
            "matched_task_project": {"project_key": "task-project:paper-test"},
        }
        with (
            patch(
                "app.execution.paper_fiber.paper_fiber_match",
                side_effect=[pending_match, ready_match],
            ) as match_mock,
            patch("time.sleep"),
        ):
            output = _paper_fiber_executor(context)

        self.assertEqual(match_mock.call_count, 2)
        self.assertEqual(
            output["matched_task_project"]["project_key"],
            "task-project:paper-test",
        )
        self.assertEqual(output["task_cache_state"], "ready")

    def test_query_snapshot_wait_exits_early_when_run_cancelled(self):
        run = self._paper_run("paper-query-wait-cancelled")
        run.status = "cancel_pending"
        self.db.commit()
        context = self._query_context(run)
        pending_match = {
            "matched_conditions": ["source_root", "folder"],
            "task_cache_state": "pending",
            "candidates": [{"id": "file-1"}],
            "task_snapshot": None,
            "matched_task_project": None,
        }
        with (
            patch(
                "app.execution.paper_fiber.paper_fiber_match",
                return_value=pending_match,
            ) as match_mock,
            patch("time.sleep") as sleep_mock,
        ):
            with self.assertRaises(ExecutionApiError) as raised:
                _paper_fiber_executor(context)

        self.assertEqual(raised.exception.code, "task_snapshot_pending")
        self.assertEqual(match_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_query_fails_fast_with_task_snapshot_unavailable_when_refresh_failed(
        self,
    ):
        self._index(self._xls("26W006701/record.xls", "木浆 100"))
        self.db.add(
            ExecutionTaskSnapshotCache(
                inspection_number="26W006701",
                status="failed",
                snapshot=None,
                error_code="bridge_timeout",
                refresh_requested_at=utcnow(),
            )
        )
        self.db.commit()

        run, query = self._failed_query_run("paper-query-snapshot-failed")

        self.assertEqual(query.status, "failed")
        self.assertEqual(query.error_code, "task_snapshot_unavailable")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "task_snapshot_unavailable")

    def test_query_fails_fast_with_rule_not_matched_when_task_lacks_project(
        self,
    ):
        self._index(self._xls("26W006701/record.xls", "木浆 100"))
        self._task_snapshot("26W006701", projects=[])
        self.db.commit()

        run, query = self._failed_query_run("paper-query-rule-not-matched")

        self.assertEqual(query.status, "failed")
        self.assertEqual(query.error_code, "paper_fiber_rule_not_matched")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "paper_fiber_rule_not_matched")

    def _judgement_context(self, run, input_data):
        node_run = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="judgement-input")
            .one()
        )
        node = {
            "id": "judgement-input",
            "type": "human.input",
            "config": {
                "title": "确认纸类定性判定信息",
                "paper_judgement": True,
            },
        }
        node_run.input_data = input_data
        self.db.flush()
        return NodeExecutionContext(
            db=self.db,
            run=run,
            node_run=node_run,
            node=node,
            input_data=input_data,
            worker_id="paper-worker",
            lease_token="",
        )

    def _registration_context(
        self,
        run,
        selected_project,
        *,
        allow_multi_copy_over_capacity=False,
    ):
        node_run = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="registration-decision")
            .one()
        )
        input_data = {
            "selected_project": selected_project,
            "selected_project_key": selected_project["project_key"],
            "task": {"schema_version": 5, "projects": [selected_project]},
        }
        node_run.input_data = input_data
        self.db.flush()
        config = {"paper_existing_record_decision": True}
        if allow_multi_copy_over_capacity:
            config["allow_multi_copy_over_capacity"] = True
        return NodeExecutionContext(
            db=self.db,
            run=run,
            node_run=node_run,
            node={
                "id": "registration-decision",
                "type": "human.input",
                "config": config,
            },
            input_data=input_data,
            worker_id="paper-worker",
            lease_token="",
        )

    def _paper_run(self, idempotency_key):
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
            idempotency_key=idempotency_key,
        )
        self.db.commit()
        return run

    def test_one_copy_existing_record_pauses_for_append_or_cancel(self):
        run = self._paper_run("paper-existing-record-decision")
        project = {
            "project_key": "task-project:paper-existing",
            "check_count": 1,
            "register_count": 2,
        }
        context = self._registration_context(run, project)

        with patch(
            "app.execution.engine._refresh_paper_registration_context"
        ) as refresh:
            self.assertIsNone(
                _auto_complete_paper_existing_record_decision(context)
            )
        refresh.assert_called_once_with(context)
        schema = _paper_existing_record_form_schema(context)
        self.assertEqual(
            schema["properties"]["existing_record_action"]["enum"],
            ["append", "cancel"],
        )
        self.assertEqual(
            schema["properties"]["existing_record_action"]["enumNames"],
            ["直接新增", "取消"],
        )

        appended = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={"existing_record_action": "append"},
        )
        self.assertFalse(appended["registration_cancelled"])
        self.assertEqual(appended["expected_existing_register_count"], 2)
        cancelled = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={"existing_record_action": "cancel"},
        )
        self.assertTrue(cancelled["registration_cancelled"])

    def test_multi_copy_project_with_capacity_continues_automatically(self):
        run = self._paper_run("paper-multi-copy-capacity")
        project = {
            "project_key": "task-project:paper-multi",
            "check_count": 3,
            "register_count": 1,
        }
        context = self._registration_context(run, project)

        with patch(
            "app.execution.engine._refresh_paper_registration_context"
        ) as refresh:
            output = _auto_complete_paper_existing_record_decision(context)
        refresh.assert_called_once_with(context)
        self.assertEqual(output["existing_record_action"], "continue")
        self.assertEqual(output["expected_existing_register_count"], 1)
        self.assertEqual(
            output["auto_submit_reason"], "registration_capacity_available"
        )

        project["register_count"] = 3
        context = self._registration_context(run, project)
        refreshed = dict(project, register_count=3)

        def _apply_refresh(ctx):
            ctx.input_data = {
                **ctx.input_data,
                "selected_project": refreshed,
                "task": {"schema_version": 5, "projects": [refreshed]},
            }
            ctx.node_run.input_data = ctx.input_data

        with patch(
            "app.execution.engine._refresh_paper_registration_context",
            side_effect=_apply_refresh,
        ) as refresh:
            output = _auto_complete_paper_existing_record_decision(context)
        refresh.assert_called_once_with(context)
        self.assertEqual(output["expected_existing_register_count"], 3)
        self.assertEqual(
            output["auto_submit_reason"],
            "multi_copy_capacity_is_informational",
        )

    def test_microscopy_multi_copy_legacy_snapshot_requires_fresh_snapshot(self):
        run = self._paper_run("microscopy-multi-copy-over-capacity")
        project = {
            "project_key": "task-project:microscopy-multi",
            "check_count": 4,
            "register_count": 4,
        }
        # Older published workflow snapshots do not carry the optional
        # allow_multi_copy_over_capacity marker.  Runtime behavior must still
        # follow the same refresh-before-entry contract.
        context = self._registration_context(run, project)

        with patch(
            "app.execution.engine._refresh_paper_registration_context",
            side_effect=ExecutionApiError(
                422,
                "paper_registration_snapshot_pending",
                "snapshot bridge unavailable",
            ),
        ):
            with self.assertRaises(ExecutionApiError) as raised:
                _auto_complete_paper_existing_record_decision(context)
        self.assertEqual(
            raised.exception.code, "paper_registration_snapshot_pending"
        )

    def test_judgement_node_auto_completes_when_task_waives_judgement(self):
        self._task_snapshot("26W006701")
        self.db.commit()
        run = self._paper_run("paper-judgement-auto")
        context = self._judgement_context(
            run,
            {
                "selected_project": {"give_judgement": 0},
                "task": {"check_basis": "按客户要求"},
            },
        )
        output = _auto_complete_paper_judgement(context)
        self.assertIsNotNone(output)
        self.assertIs(output["judgement_required"], False)
        self.assertIsNone(output["judge_basis"])
        self.assertIsNone(output["judgement"])
        self.assertIsNone(output["standard_value"])
        self.assertEqual(
            output["auto_submit_reason"], "judgement_not_required"
        )

    def test_judgement_node_waits_with_dynamic_form_when_required(self):
        self._task_snapshot("26W006701")
        self.db.commit()
        run = self._paper_run("paper-judgement-manual")
        context = self._judgement_context(
            run,
            {
                "selected_project": {"give_judgement": 1},
                "task": {"check_basis": "按客户要求，GB/T 4688-2020、企业标准"},
            },
        )
        # 要求判定时不自动完成，必须创建人工任务
        self.assertIsNone(_auto_complete_paper_judgement(context))
        schema = _paper_judgement_form_schema(context)
        self.assertEqual(
            schema["properties"]["judge_basis"]["enum"],
            ["按客户要求", "GB/T 4688-2020", "企业标准"],
        )
        self.assertEqual(
            schema["properties"]["judgement"]["enum"], ["符合", "不符合"]
        )
        # 选择节点尚未完成：标准值字段存在但无默认值与数据源
        standard_field = schema["properties"]["standard_value"]
        self.assertEqual(standard_field["title"], "标准值与允差")
        self.assertNotIn("default", standard_field)
        self.assertNotIn("x-copy-sources", standard_field)
        self.assertEqual(
            schema["required"], ["judge_basis", "judgement", "standard_value"]
        )

    def test_judgement_form_falls_back_to_free_text_without_basis(self):
        run = self._paper_run("paper-judgement-no-basis")
        context = self._judgement_context(
            run,
            {
                "selected_project": {"give_judgement": 1},
                "task": {"check_basis": None},
            },
        )
        schema = _paper_judgement_form_schema(context)
        basis_field = schema["properties"]["judge_basis"]
        self.assertNotIn("enum", basis_field)
        self.assertEqual(basis_field["minLength"], 1)

    def test_sample_identities_support_all_delimiters_and_warn_on_count_mismatch(self):
        run = self._paper_run("paper-sample-identities")
        context = self._judgement_context(
            run,
            {
                "selected_project": {
                    "give_judgement": 0,
                    "check_count": 3,
                    "sample_identify": "正面，反面,中层、夹层",
                },
                "task": {"check_basis": None},
            },
        )

        self.assertIsNone(_auto_complete_paper_judgement(context))
        schema = _paper_judgement_form_schema(context)
        self.assertEqual(
            schema["properties"]["sample_identity"]["x-suggestions"],
            ["正面", "反面", "中层", "夹层"],
        )
        self.assertIn("检测份数为 3", schema["x-warning"])
        self.assertEqual(schema["required"], ["sample_identity"])

    def test_single_sample_identity_is_prefilled_and_requires_confirmation(self):
        run = self._paper_run("paper-single-sample-identity")
        context = self._judgement_context(
            run,
            {
                "selected_project": {
                    "give_judgement": 0,
                    "check_count": 1,
                    "sample_identify": " 正面 ",
                },
                "task": {"check_basis": None},
            },
        )
        schema = _paper_judgement_form_schema(context)
        identity = schema["properties"]["sample_identity"]
        self.assertEqual(identity["default"], "正面")
        self.assertEqual(identity["const"], "正面")
        self.assertTrue(identity["readOnly"])

        with self.assertRaises(ExecutionApiError) as raised:
            _normalize_human_submission(
                self.db,
                run=run,
                node_run=context.node_run,
                data={"sample_identity": "正面"},
            )
        self.assertEqual(
            raised.exception.code,
            "paper_sample_identity_confirmation_required",
        )
        output = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={
                "sample_identity": "正面",
                "sample_identity_confirmed": True,
            },
        )
        self.assertEqual(output["sample_identity"], "正面")
        self.assertEqual(output["sample_identity_options"], ["正面"])
        self.assertFalse(output["judgement_required"])

    def test_judgement_submission_is_shaped_with_uniform_keys(self):
        run = self._paper_run("paper-judgement-shape")
        context = self._judgement_context(
            run,
            {
                "selected_project": {"give_judgement": 1},
                "task": {"check_basis": "按客户要求"},
            },
        )
        output = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={
                "judge_basis": " 按客户要求 ",
                "judgement": "符合",
                "standard_value": " 定性，100%木浆 ",
            },
        )
        self.assertEqual(
            output,
            {
                "judgement_required": True,
                "judge_basis": "按客户要求",
                "judgement": "符合",
                "standard_value": "定性，100%木浆",
                "sample_identity": None,
                "sample_identity_confirmed": False,
                "sample_identity_options": [],
                "identity_count_mismatch": False,
            },
        )

    def test_judgement_submission_requires_standard_value(self):
        run = self._paper_run("paper-judgement-no-std")
        context = self._judgement_context(
            run,
            {
                "selected_project": {"give_judgement": 1},
                "task": {"check_basis": "按客户要求"},
            },
        )
        with self.assertRaises(ExecutionApiError) as raised:
            _normalize_human_submission(
                self.db,
                run=run,
                node_run=context.node_run,
                data={"judge_basis": "按客户要求", "judgement": "符合"},
            )
        self.assertEqual(
            raised.exception.code, "paper_standard_value_required"
        )

    def test_judgement_form_offers_w32_default_and_copy_sources(self):
        run = self._paper_run("paper-judgement-sources")
        selection = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="select")
            .one()
        )
        selection.status = "succeeded"
        selection.output_data = {
            "primary_file": {
                "result": {
                    "w32_value": "木浆 100",
                    "m32_value": "100%木浆",
                }
            }
        }
        self.db.flush()
        context = self._judgement_context(
            run,
            {
                "selected_project": {
                    "give_judgement": 1,
                    "remark": "定性，100%木浆",
                },
                "task": {"check_basis": "按客户要求"},
            },
        )
        schema = _paper_judgement_form_schema(context)
        standard_field = schema["properties"]["standard_value"]
        # 默认沿用 W32 同文结果，人工可按数据源改写
        self.assertEqual(standard_field["default"], "木浆 100")
        self.assertEqual(
            standard_field["x-copy-sources"],
            [
                {"label": "任务单说明列", "text": "定性，100%木浆"},
                {"label": "Sheet1!M32", "text": "100%木浆"},
            ],
        )

    def test_open_legacy_judgement_task_uses_current_strict_schema(self):
        run = self._paper_run("paper-judgement-open-upgrade")
        selection = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="select")
            .one()
        )
        selection.status = "succeeded"
        selection.output_data = {
            "primary_file": {
                "result": {
                    "w32_value": "木浆 100",
                    "m32_value": "100%木浆",
                }
            }
        }
        judgement = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="judgement-input")
            .one()
        )
        judgement.status = "waiting_human"
        judgement.input_data = {
            "selected_project": {
                "give_judgement": 1,
                "remark": "定性，100%木浆",
            },
            "task": {"check_basis": "按客户要求"},
        }
        legacy_schema = {
            "type": "object",
            "properties": {
                "judge_basis": {"type": "string"},
                "judgement": {
                    "type": "string",
                    "enum": ["符合", "不符合"],
                },
            },
            "required": ["judge_basis", "judgement"],
            "additionalProperties": False,
        }
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=judgement.id,
            title="确认纸类定性判定信息",
            form_schema=legacy_schema,
            status="open",
            assigned_user_id=self.user.id,
        )
        self.db.add(task)
        self.db.flush()

        current_schema = effective_human_task_form_schema(task)
        self.assertEqual(
            current_schema["required"],
            ["judge_basis", "judgement", "standard_value"],
        )
        self.assertEqual(
            current_schema["properties"]["standard_value"]["default"],
            "木浆 100",
        )
        completed = submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={
                "judge_basis": "按客户要求",
                "judgement": "符合",
                "standard_value": "定性，100%木浆",
            },
            actor=self.user,
        )
        self.assertEqual(
            completed.result_data["standard_value"],
            "定性，100%木浆",
        )
        self.assertIn(
            "standard_value",
            completed.form_schema["properties"],
        )

    def test_completed_legacy_judgement_reopens_before_final_entry(self):
        run = self._paper_run("paper-judgement-completed-upgrade")
        selection = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="select")
            .one()
        )
        selection.status = "succeeded"
        selection.output_data = {
            "primary_file": {
                "result": {
                    "w32_value": "木浆 100",
                    "m32_value": "100%木浆",
                }
            }
        }
        selected_project = {
            "give_judgement": 1,
            "remark": "定性，100%木浆",
        }
        judgement = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="judgement-input")
            .one()
        )
        judgement.status = "succeeded"
        judgement.input_data = {
            "selected_project": selected_project,
            "task": {"check_basis": "按客户要求"},
        }
        judgement.output_data = {
            "judgement_required": True,
            "judge_basis": "按客户要求",
            "judgement": "符合",
        }
        judgement.finished_at = utcnow()
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=judgement.id,
            title="确认纸类定性判定信息",
            form_schema={
                "type": "object",
                "properties": {
                    "judge_basis": {"type": "string"},
                    "judgement": {"type": "string"},
                },
                "required": ["judge_basis", "judgement"],
                "additionalProperties": False,
            },
            result_data=dict(judgement.output_data),
            status="completed",
            assigned_user_id=self.user.id,
            completed_by_id=self.user.id,
            completed_at=utcnow(),
        )
        self.db.add(task)
        edge = (
            self.db.query(ExecutionEdgeRun)
            .filter_by(
                run_id=run.id,
                source_node_id="judgement-input",
                target_node_id="register-result",
            )
            .one()
        )
        edge.status = "selected"
        edge.resolved_at = utcnow()
        entry = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="register-result")
            .one()
        )
        entry.status = "running"
        entry.attempt_count = 1
        entry.lease_owner = "paper-worker"
        entry.lease_token = "entry-lease"
        entry.lease_expires_at = utcnow() + timedelta(seconds=30)
        entry.started_at = utcnow()
        entry_input = {
            "selected_project": selected_project,
            "judgement_input": dict(judgement.output_data),
        }
        entry.input_data = entry_input
        self.db.add(
            ExecutionNodeAttempt(
                node_run_id=entry.id,
                attempt_number=1,
                worker_id="paper-worker",
                lease_token="entry-lease",
                status="running",
                input_data=entry_input,
            )
        )
        run.status = "running"
        self.db.flush()
        entry_definition = next(
            node
            for node in run.definition_snapshot["nodes"]
            if node["id"] == "register-result"
        )
        context = NodeExecutionContext(
            db=self.db,
            run=run,
            node_run=entry,
            node=entry_definition,
            input_data=entry_input,
            worker_id="paper-worker",
            lease_token="entry-lease",
        )

        self.assertTrue(
            _reopen_paper_judgement_for_standard_value(self.db, context)
        )
        self.assertEqual(judgement.status, "waiting_human")
        self.assertEqual(task.status, "open")
        self.assertEqual(
            task.draft_data,
            {"judge_basis": "按客户要求", "judgement": "符合"},
        )
        self.assertEqual(entry.status, "pending")
        self.assertEqual(edge.status, "pending")
        self.assertEqual(run.status, "waiting_human")

        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={
                "judge_basis": "按客户要求",
                "judgement": "符合",
                "standard_value": "定性，100%木浆",
            },
            actor=self.user,
        )
        self.assertEqual(judgement.status, "succeeded")
        self.assertEqual(entry.status, "ready")
        self.assertEqual(edge.status, "selected")

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
        # 模拟升级前的线上库：草稿与历史版本均为系统发布过的定义
        # （只读首版校验和现场计算，完整首版/单候选版为已知校验和），
        # 未被人为修改时应跟随代码升级出新版本。
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        read_only = _paper_fiber_gbt4688_qualitative_readonly_definition()
        known_v1_full = (
            "7b09325ffef43642d1f53a81540d8fcc4209482a66e82f00df6024b8d5a9e8d1"
        )
        version_one = next(
            version for version in workflow.versions
            if version.version_number == 1
        )
        workflow.draft_definition = read_only
        version_one.definition = read_only
        version_one.checksum = known_v1_full
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(workflow.draft_revision, 2)
        self.assertEqual(len(workflow.versions), 2)
        node_ids = {
            node["id"] for node in workflow.draft_definition["nodes"]
        }
        self.assertIn("judgement-input", node_ids)
        upgraded_select = next(
            node for node in workflow.draft_definition["nodes"]
            if node["id"] == "select"
        )
        self.assertIs(
            upgraded_select["config"]["auto_submit_single_candidate"], True
        )
        # 旧版本保持原样，历史运行不受影响
        self.assertNotIn(
            "judgement-input",
            {node["id"] for node in version_one.definition["nodes"]},
        )

    def test_known_two_version_history_upgrades_increments_revision(self):
        # 模拟当前线上库：v1（完整首版）+ v2（单候选自动提交）均为已知
        # 系统定义，升级应追加 v3 且草稿修订号递增而不是回写。
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug=PAPER_FIBER_WORKFLOW_SLUG)
            .one()
        )
        read_only = _paper_fiber_gbt4688_qualitative_readonly_definition()
        known_v1_full = (
            "7b09325ffef43642d1f53a81540d8fcc4209482a66e82f00df6024b8d5a9e8d1"
        )
        known_v2_auto_submit = (
            "30f80623f97c9e15f9ac6ad865addeb43eecda70b8a1c8f15f292f9559e738e3"
        )
        version_one = next(
            version for version in workflow.versions
            if version.version_number == 1
        )
        version_one.definition = read_only
        version_one.checksum = known_v1_full
        self.db.add(
            ExecutionWorkflowVersion(
                workflow_id=workflow.id,
                version_number=2,
                schema_version="1.0",
                definition=read_only,
                checksum=known_v2_auto_submit,
                capabilities=deepcopy(workflow.capabilities),
                contract_checksum=workflow_contract_checksum(
                    read_only, workflow.capabilities
                ),
                release_note="历史单候选版本",
            )
        )
        workflow.draft_definition = read_only
        workflow.draft_revision = 2
        workflow.published_version_number = 2
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(workflow.published_version_number, 3)
        self.assertEqual(workflow.draft_revision, 3)
        self.assertEqual(len(workflow.versions), 3)
        latest = next(
            version for version in workflow.versions
            if version.version_number == 3
        )
        self.assertEqual(
            latest.checksum,
            definition_checksum(_paper_fiber_gbt4688_qualitative_definition()),
        )
        self.assertIn(
            "judgement-input",
            {node["id"] for node in workflow.draft_definition["nodes"]},
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
