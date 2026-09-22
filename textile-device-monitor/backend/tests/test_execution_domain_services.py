"""Shared workbook and readonly domain services with explicit rule fixtures."""
from project_rule_fixtures import install_rule_fixtures

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import xlrd
import xlwt
from openpyxl import Workbook
from PIL import Image

from app.api import execution as api
from app.execution import paper_fiber
from app.execution.electron_microscopy import cached_task_snapshot
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_check_record import microscopy_check_record_executor
from app.execution.microscopy_families import MICROSCOPY_RECORD_FAMILIES
from app.execution.microscopy_original_record import _microscopy_original_record_executor
from app.execution.models import (
    ExecutionArtifact, ExecutionFileIndexEntry, ExecutionRun,
    ExecutionStorageRoot, ExecutionTaskSnapshotCache,
)
from app.execution.project_rules import resolve_rule
from tests.test_execution_microscopy_original_record import _fake_uno_writer
from workflow_native_helpers import environment, request

NUMBER = "26W006701"


@pytest.fixture
def domain_env(environment):
    env = environment
    install_rule_fixtures(env.db)
    env.db.commit()
    return env


def index_file(env, root_id, path):
    stat = path.stat()
    row = ExecutionFileIndexEntry(
        storage_root_id=env.roots[root_id].id,
        relative_path=path.relative_to(env.roots[root_id].local_path).as_posix(),
        filename=path.name, extension=path.suffix, file_kind="file",
        inspection_number=NUMBER, size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}", scan_generation=1,
    )
    env.db.add(row)
    env.db.commit()
    return row


def paper_file(env, suffix=".xlsx", value="木浆 100"):
    path = Path(env.roots["paper_fiber_records"].local_path) / NUMBER / f"record{suffix}"
    path.parent.mkdir(exist_ok=True)
    if suffix == ".xlsx":
        book = Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet["W32"], sheet["M32"] = value, "标准值"
        book.save(path)
        book.close()
    else:
        book = xlwt.Workbook()
        sheet = book.add_sheet("Sheet1")
        sheet.write(31, 22, value)
        sheet.write(31, 12, "标准值")
        book.save(str(path))
    return index_file(env, "paper_fiber_records", path)


def image_files(env, count=1):
    for i in range(count):
        path = Path(env.roots["electron_microscopy_records"].local_path) / NUMBER / f"image-{i}.png"
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (60 + i * 20, 40), "red").save(path)
        index_file(env, "electron_microscopy_records", path)


def task_snapshot(env, family="paper"):
    item = (paper_fiber.PAPER_FIBER_PROJECT_NAME if family == "paper"
            else MICROSCOPY_RECORD_FAMILIES[family].check_item_name)
    method = "GB/T 4688-2020" if family == "paper" else "GB/T 36422-2018"
    snapshot = {
        "schema_version": 5, "inspection_number": NUMBER,
        "sample_names": ["棉布"], "check_basis": method,
        "special_wool_occupied_numbers": [],
        "projects": [{
            "project_key": "task-project:domain-test",
            "task_check_item_id": "sha256:1111111111111111",
            "check_item_id": "sha256:2222222222222222",
            "check_item_name": item, "check_method": method,
            "check_count": 1, "register_count": 0, "sample_identify": "正面",
            "give_judgement": 0,
        }],
    }
    env.db.add(ExecutionTaskSnapshotCache(
        inspection_number=NUMBER, status="ready", snapshot=snapshot,
        fetched_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    env.db.commit()
    return snapshot


def no_execution_records(env):
    assert env.db.query(ExecutionRun).count() == 0
    assert env.db.query(ExecutionArtifact).count() == 0


@pytest.mark.parametrize("family", ["microscopy", "cross_section"])
def test_microscopy_query_exposes_images_and_choices(domain_env, family):
    env = domain_env
    image_files(env, 2)
    task_snapshot(env, family)
    result = request(env, "POST", "v1/microscopy/query", {
        "inspection_number": NUMBER, "record_family": family,
    })
    assert result["full_match"] and result["image_count"] == 2
    assert result["record_choices"]["sample_identification"]["automatic_value"] == "正面"
    assert result["supported_image_counts"] == sorted(MICROSCOPY_RECORD_FAMILIES[family].template_bindings)
    preview = env.client.get(result["images"][0]["preview_url"])
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/")
    no_execution_records(env)


@pytest.mark.parametrize("suffix", [".xls", ".xlsx"])
def test_paper_query_keeps_result_and_task_identity(domain_env, suffix):
    env = domain_env
    entry = paper_file(env, suffix)
    task_snapshot(env)
    result = request(env, "POST", "v1/paper-fiber/query", {"inspection_number": NUMBER})
    candidate = result["candidates"][0]
    assert result["full_match"] and candidate["id"] == entry.id
    assert candidate["result"]["w32_value"] == "木浆 100"
    assert candidate["result"]["m32_value"] == "标准值"
    assert candidate["result"]["contains_standalone_100"]
    assert result["matched_task_project"]["project_key"] == "task-project:domain-test"
    no_execution_records(env)


@pytest.mark.parametrize("domain", ["microscopy", "paper-fiber"])
def test_pending_query_returns_without_waiting_or_duplicate_refresh(domain_env, domain):
    env = domain_env
    paper_file(env) if domain == "paper-fiber" else image_files(env)
    with patch("time.sleep") as sleep:
        for _ in range(2):
            result = request(env, "POST", f"v1/{domain}/query", {"inspection_number": NUMBER})
            assert result["task_cache_state"] == "pending"
        sleep.assert_not_called()
    assert env.db.query(ExecutionTaskSnapshotCache).count() == 1
    no_execution_records(env)


def test_paper_worker_polls_task_without_repeating_file_scan(domain_env):
    env = domain_env
    paper_file(env)
    task_snapshot(env)
    ready = cached_task_snapshot(env.db, inspection_number=NUMBER)
    context = SimpleNamespace(
        db=env.db, run=SimpleNamespace(inspection_number=NUMBER, status="running"),
        node={"config": {"require_full_task_match": True}}, input_data={},
    )
    with (
        patch.object(paper_fiber, "cached_task_snapshot", side_effect=[
            {"cache_state": "pending", "snapshot": None}, ready,
        ]),
        patch.object(paper_fiber, "find_paper_fiber_records", wraps=paper_fiber.find_paper_fiber_records) as scan,
        patch("time.sleep"),
    ):
        result = paper_fiber._paper_fiber_executor(context)
    assert result["task_validation_state"] == "matched"
    assert scan.call_count == 1


def test_paper_wait_discards_local_candidates_when_rule_changes(domain_env):
    env = domain_env
    paper_file(env)
    task_snapshot(env)
    rule = resolve_rule(env.db, paper_fiber.PAPER_FIBER_RULE_KEY)
    before = paper_fiber.paper_fiber_match(env.db, inspection_number=NUMBER, rule=rule)
    assert before["full_match"] and before["candidates"]
    disabled = replace(rule, enabled=False, revision=rule.revision + 1)
    after = paper_fiber.paper_fiber_match(
        env.db, inspection_number=NUMBER, rule=disabled, records=before,
    )
    assert after["candidates"] == [] and not after["full_match"]
    assert after["task_cache_state"] == "disabled" and after["task_snapshot"] is None
    with pytest.raises(ExecutionApiError, match="流程条件"):
        paper_fiber.paper_fiber_candidates(after, require_full_task_match=True)


COUNTS = [(family.key, count) for family in MICROSCOPY_RECORD_FAMILIES.values()
          for count in family.template_bindings]






def test_download_failure_cleans_partial_file_and_keeps_domain_errors(domain_env):
    env = domain_env
    paths = []
    def fail_render(target, **_kwargs):
        paths.append(target.parent)
        target.write_bytes(b"partial")
        raise ExecutionApiError(503, "test_writer_unavailable", "模拟生成失败")
    payload = {"inspection_number": NUMBER, "image_count": 1, "judgement_required": False}
    with patch.object(api, "render_check_record_workbook", side_effect=fail_render):
        response = env.client.post("/api/execution/v1/microscopy/render/check-record", json=payload)
    assert response.status_code == 503
    assert paths and all(not path.exists() for path in paths)
    bad = env.client.post("/api/execution/v1/microscopy/render/check-record", json={
        **payload, "record_family": "cross_section", "image_count": 4,
    })
    assert bad.status_code == 422
    assert bad.json()["code"] == "microscopy_template_image_count_unsupported"
    no_execution_records(env)
