"""Native domain release execution, portable roots and partial v1 migration."""

from copy import deepcopy
from contextlib import nullcontext
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import xlrd

from app.execution.models import ExecutionArtifact, ExecutionHumanTask, ExecutionNodeRun, ExecutionProjectRule
from app.execution.project_rules import PAPER_FIBER_RULE_KEY, microscopy_rule_key, resolve_rule
from app.execution.v2.domain_record_handlers import original_record
from app.execution.v2.examples import build_domain_records_smoke_release, _seal
from tests.test_execution_domain_services import (
    NUMBER, domain_env, image_files, paper_file, task_snapshot,
)
from tests.test_execution_microscopy_original_record import _fake_uno_writer
from tests.test_execution_v2_regenerated_fiber import node_output, stage_candidate
from tests.test_execution_workflow_replacement import (
    environment, drain, preview, publish, request, source_workflow, waiting_task,
)


def native_release(env, domain="microscopy", *, family="microscopy", custom_roots=False, check_item_name=None):
    document = build_domain_records_smoke_release(domain)
    microscopy = domain == "microscopy"
    source_key = "electron_microscopy_records" if microscopy else "paper_fiber_records"
    if custom_roots:
        # The native contract must use the binding, not its old hard-coded root.
        env.roots[source_key].root_id = "local_records"
        env.roots["execution_staging"].root_id = "local_workbooks"
        env.db.commit()
    root_id = env.roots[source_key].root_id
    rule = resolve_rule(env.db, microscopy_rule_key(family) if microscopy else PAPER_FIBER_RULE_KEY)
    bindings = {"root_slots": {"source": {"root_id": root_id}},
                "rule_slots": {"record_match": {"rule_key": rule.key, "revision": rule.revision}},
                "role_slots": {}, "credential_slots": {}}
    if microscopy:
        bindings["root_slots"]["staging"] = {"root_id": env.roots["execution_staging"].root_id}
        for node in document["definition"]["nodes"]:
            if "record_family" in node["config"]:
                node["config"]["record_family"] = family
            if node["id"] == "check" and check_item_name:
                node["input_mapping"]["check_item_name"] = check_item_name
    release = stage_candidate(env, {"candidate": _seal(document), "binding_suggestions": bindings})
    return publish(env, release)


def start(env, release, *, microscopy=True):
    return request(env, "POST", "v1/runs", {
        "workflow_id": release["workflow_id"], "inspection_number": NUMBER,
        "input_data": {"sample_name": "棉布"} if microscopy else {},
        "global_data": {}, "idempotency_key": str(uuid4()),
    }, status=201)["run"]


def select_all(env, run_id):
    task = waiting_task(env, run_id)
    items = request(env, "GET", f"v1/human-tasks/{task['id']}")["node_run"]["input_data"]["items"]
    request(env, "POST", f"v1/human-tasks/{task['id']}/submit", {
        "revision": task["revision"], "data": {"selected_ids": [item["id"] for item in items], "primary_id": items[0]["id"]},
    })
    return items


@pytest.mark.parametrize("family,count,custom", [("microscopy", 1, False), ("microscopy", 2, True), ("cross_section", 3, True)])
def test_native_microscopy_select_render_download_and_reuse(domain_env, family, count, custom):
    env = domain_env
    image_files(env, count)
    task_snapshot(env, family)
    item_name = "纤维微观形貌" if family == "microscopy" else "纤维横截面"
    release = native_release(env, family=family, custom_roots=custom, check_item_name=item_name)
    run = start(env, release)
    writer = (nullcontext() if os.environ.get("EXECUTION_RUN_UNO_INTEGRATION_TESTS") == "1"
              else patch("app.execution.microscopy_original_record._run_uno_writer", side_effect=_fake_uno_writer))
    with writer:
        drain(env)
        if count > 1:
            selected = select_all(env, run["id"])
            assert selected[0]["metadata"]["preview_url"]
            drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == "completed", [
        (node.node_id, node.error_code, node.error_message)
        for node in env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"]).all()
    ]
    assert env.db.query(ExecutionHumanTask).filter_by(run_id=run["id"]).count() == (count > 1)
    original = node_output(env, run["id"], "original")
    check = node_output(env, run["id"], "check")
    assert original["image_count"] == check["image_count"] == count
    for key in ("original_record", "registration_workbook"):
        reference = detail["output_data"][key]
        assert reference["root_id"] == env.roots["execution_staging"].root_id
        response = env.client.get(reference["download_url"])
        assert response.status_code == 200 and response.content.startswith(b"\xd0\xcf\x11\xe0")
    sheet = xlrd.open_workbook(file_contents=response.content).sheet_by_name("Sheet1")
    assert sheet.cell_value(6, 60) == item_name
    node = env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"], node_id="original").one()
    # Replaying the same node reuses its immutable artifact without UNO.
    definition = next(n for n in detail["definition"]["nodes"] if n["id"] == "original")
    with patch("app.execution.microscopy_original_record._run_uno_writer") as writer:
        repeated = original_record(SimpleNamespace(db=env.db, run=SimpleNamespace(id=run["id"]), node_run=node, node=definition, input_data=node.input_data))
    assert repeated["reused"] and repeated["artifact_id"] == original["artifact_id"]
    writer.assert_not_called()
    assert env.db.query(ExecutionArtifact).filter_by(run_id=run["id"]).count() == 2


@pytest.mark.parametrize("suffix", [".xls", ".xlsx"])
@pytest.mark.parametrize("count", [1, 2])
def test_native_paper_query_and_existing_result_cards(domain_env, suffix, count):
    env = domain_env
    paper_file(env, suffix)
    if count == 2:
        paper_file(env, ".xlsx" if suffix == ".xls" else ".xls", "针叶木浆 80; 阔叶木浆 20")
    task_snapshot(env)
    release = native_release(env, "paper-fiber", custom_roots=True)
    run = start(env, release, microscopy=False)
    drain(env)
    query = node_output(env, run["id"], "query")
    assert query["count"] == count and query["task_validation_state"] == "matched"
    assert all(item["root_id"] == "local_records" for item in query["items"])
    assert query["items"][0]["metadata"]["presentation"] == "result_file"
    if count == 2:
        task = waiting_task(env, run["id"])
        first = query["items"][0]["id"]
        request(env, "POST", f"v1/human-tasks/{task['id']}/submit", {"revision": task["revision"], "data": {"selected_ids": [first], "primary_id": first}})
        drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == "completed", detail
    assert detail["output_data"]["primary_item"]["metadata"]["result"]["m32_value"] == "标准值"
    assert env.db.query(ExecutionArtifact).filter_by(run_id=run["id"]).count() == 0


def test_native_source_change_does_not_generate_an_artifact(domain_env):
    env = domain_env
    image_files(env, 2)
    task_snapshot(env, "microscopy")
    release = native_release(env)
    run = start(env, release)
    drain(env)
    candidates = node_output(env, run["id"], "query")["images"]
    select_all(env, run["id"])
    (Path(env.roots["electron_microscopy_records"].local_path) / candidates[0]["relative_path"]).write_bytes(b"changed")
    drain(env)
    node = env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"], node_id="original").one()
    assert node.error_code == "image_candidate_stale"
    assert env.db.query(ExecutionArtifact).filter_by(run_id=run["id"]).count() == 0


def test_native_query_does_not_silently_use_a_revised_rule(domain_env):
    env = domain_env
    image_files(env)
    task_snapshot(env, "microscopy")
    release = native_release(env)
    run = start(env, release)
    rule = env.db.query(ExecutionProjectRule).filter_by(rule_key=microscopy_rule_key("microscopy")).one()
    rule.revision += 1
    env.db.commit()
    drain(env)
    env.db.expire_all()
    query = env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"], node_id="query").one()
    assert query.error_code == "deployment_binding_stale"
    assert env.db.query(ExecutionHumanTask).filter_by(run_id=run["id"]).count() == 0


def test_domain_migration_uses_types_and_mapping_instead_of_node_ids(domain_env):
    env = domain_env
    source = source_workflow(env, "electron-microscopy-gbt36422")
    original = deepcopy(source.draft_definition)
    # The local adapter has no dependency on node IDs. Legacy external nodes
    # still impose their own business-form IDs until the P4 connector migration.
    names = {node["id"]: "custom-" + node["id"] for node in original["nodes"]
             if node["type"] in {"file.electron_microscopy_gbt36422",
                                 "workbook.microscopy_original_record", "workbook.microscopy_check_record"}}

    def rename(value):
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        if isinstance(value, str):
            for old, new in names.items():
                value = value.replace(f"$.nodes.{old}.", f"$.nodes.{new}.")
            return names.get(value, value)
        return value

    source.draft_definition = rename(original)
    env.db.commit()
    result = preview(env, source.slug, source="draft", profile="native_p3")
    assert result["content_valid"] and result["migration_status"] == "p3_partial", result.get("issues")
    assert len(result["transformations"]) == 3
    assert all(item["source_node_id"].startswith("custom-") for item in result["transformations"])
    parked = deepcopy(source.draft_definition)
    next(node for node in parked["nodes"] if node["type"] == "file.electron_microscopy_gbt36422")["disabled"] = True
    source.draft_definition = parked
    env.db.commit()
    partial = preview(env, source.slug, source="draft", profile="native_p3")
    assert "custom-discover" in partial["diff"]["excluded_node_ids"]
    assert not any(item["source_node_id"] == "custom-generate-record" for item in partial["transformations"])
    assert not partial["publish_ready"]


@pytest.mark.parametrize("slug,native_count", [
    ("electron-microscopy-gbt36422", 3), ("electron-cross-section-gbt36422", 3),
    ("paper-fiber-gbt4688-2020-qualitative", 1),
])
def test_migration_replaces_local_nodes_and_keeps_external_blockers(domain_env, slug, native_count):
    env = domain_env
    source = source_workflow(env, slug)
    before = deepcopy(source.draft_definition)
    result = preview(env, slug, profile="native_p3")
    assert result == preview(env, slug, profile="native_p3")
    assert result["content_valid"], result
    assert result["migration_status"] == "p3_partial", result
    assert result["native_node_count"] == native_count
    assert not result["publish_ready"] and any(item["phase"] == "P4" for item in result["blockers"])
    assert result["candidate"]["definition"]["edges"]
    env.db.refresh(source)
    assert source.draft_definition == before
    assert all(node["config"].get("rule_slot") for node in result["candidate"]["definition"]["nodes"] if node["type"] in {"microscopy.image_candidates", "paper_fiber.find_records"})
    for node in result["candidate"]["definition"]["nodes"]:
        if node["type"] == "microscopy.check_record.render":
            assert node["input_mapping"]["check_item_name"].endswith(".selected_project.check_item_name")
