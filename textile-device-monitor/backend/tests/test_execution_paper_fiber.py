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
from app.execution.catalog import bind_user_role, ensure_default_catalog, ensure_default_rbac
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
    ExecutionExternalOperation,
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














    def _run_to_registration(self, **project_fields):
        self._index(self._xls("26W006701/record.xls", "木浆 100"))
        self._task_snapshot("26W006701")
        cached = self.db.query(ExecutionTaskSnapshotCache).one()
        snapshot = deepcopy(cached.snapshot)
        snapshot["projects"][0].update(project_fields)
        cached.snapshot = snapshot
        self.db.commit()
        run = self._paper_run("paper-before-write")
        for expected in ("start", "query", "select"):
            self.assertEqual(self._execute_one().node_id, expected)
        return run







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
