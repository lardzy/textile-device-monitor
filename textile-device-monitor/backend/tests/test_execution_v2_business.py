"""Complete native domain migration and one-form submission acceptance."""

from copy import deepcopy
import hashlib
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.execution.v2.domain_record_migration import WORKFLOW_SLUGS
from app.config import settings
from app.execution.external_operations import claim_approved_external_operation, complete_external_attempt, record_external_attempt_stage, _operation_stage_profile
from app.execution.models import ExecutionCredential, ExecutionExternalOperation, ExecutionHumanTask, ExecutionNodeRun, ExecutionStorageRoot, ExecutionTaskSnapshotCache
from tests.test_execution_domain_services import domain_env, image_files, paper_file, task_snapshot, NUMBER
from tests.test_execution_microscopy_original_record import _fake_uno_writer
from tests.test_execution_v2_regenerated_fiber import stage_candidate
from tests.test_execution_workflow_replacement import environment, source_workflow, preview, request, publish, drain


@pytest.mark.parametrize("slug", sorted(WORKFLOW_SLUGS))
def test_complete_domain_candidate_has_no_business_compatibility_nodes(environment, slug):
    env = environment
    source = source_workflow(env, slug)
    before = deepcopy(source.draft_definition)
    result = preview(env, slug, profile="native_p4")
    assert result["content_valid"], result.get("issues")
    assert result["migration_status"] == "p4_complete", result.get("blockers")
    assert result == preview(env, slug, profile="native_p4")
    nodes = result["candidate"]["definition"]["nodes"]
    assert sum(n["type"] == "human.form" for n in nodes) == 1
    assert not any(n["type"].startswith("external.legacy") for n in nodes)
    assert sum(n["type"] == "external.operation" for n in nodes) == 3
    env.db.refresh(source)
    assert source.draft_definition == before


def operation_receipt(operation):
    from tests.test_execution_paper_external_operations import PaperExternalOperationTests
    from tests.test_execution_special_wool_external_operations import SpecialWoolExternalOperationTests
    from tests.test_execution_connector_operations import receipt
    kind = operation.request_summary["operation_type"]
    if kind == "legacy_special_wool_qualitative_upload":
        return PaperExternalOperationTests._upload_receipt(None, operation)
    if kind == "legacy_special_wool_image_upload":
        return SpecialWoolExternalOperationTests._image_receipt(None, operation)
    if kind in {"legacy_special_wool_qualitative_review", "legacy_special_wool_review"}:
        result = PaperExternalOperationTests._review_receipt(None, operation)
        result.update(receipt_type=kind, stages=list(_operation_stage_profile(operation)[0]))
        return result
    if kind == "legacy_microscopy_check_record_entry":
        return SpecialWoolExternalOperationTests._final_entry_receipt(None, operation)
    return receipt(operation)


@pytest.mark.parametrize("family", ["paper_fiber", "microscopy", "cross_section"])
@pytest.mark.parametrize("judgement", [False, True])
def test_full_native_business_api_worker_and_bridge_receipts(domain_env, monkeypatch, family, judgement):
    env = domain_env
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED", True)
    paper = family == "paper_fiber"
    slug = "paper-fiber-gbt4688-2020-qualitative" if paper else "electron-cross-section-gbt36422" if family == "cross_section" else "electron-microscopy-gbt36422"
    task_snapshot(env, "paper" if paper else family)
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    project = {**cache.snapshot["projects"][0], "check_item_no": "51.113K" if paper else "5103.426" if family == "cross_section" else "5103.5", "seq_num": 1, "give_judgement": int(judgement)}
    project["project_key"] = "task-project:" + hashlib.sha256("\0".join(str(project[k]) for k in (
        "task_check_item_id", "check_item_id", "check_item_no", "check_item_name", "check_method", "seq_num",
    )).encode()).hexdigest()[:24]
    cache.snapshot = {**cache.snapshot, "projects": [project]}
    credential = ExecutionCredential(user_id=env.admin.id, system_key="legacy_inspection", account_name="business-tester", encrypted_secret="unused")
    target = env.path / "report_upload_images"
    target.mkdir()
    env.db.add_all([credential, ExecutionStorageRoot(root_id="report_upload_images", name="报告图片", local_path=str(target), access_mode="write", is_active=True, is_available=True)])
    env.db.commit()
    if paper:
        paper_file(env, ".xls")
    else:
        image_files(env, 1)
    source_workflow(env, slug)
    migration = preview(env, slug, profile="native_p4")
    for slot in migration["binding_suggestions"]["credential_slots"]:
        migration["binding_suggestions"]["credential_slots"][slot] = {"credential_id": credential.id, "revision": credential.revision}
    release = publish(env, stage_candidate(env, migration))
    run = request(env, "POST", "v1/runs", {"workflow_id": release["workflow_id"], "inspection_number": NUMBER,
        "input_data": {}, "idempotency_key": str(uuid4())}, status=201)["run"]
    completed = 0
    with patch("app.execution.microscopy_original_record._run_uno_writer", side_effect=_fake_uno_writer):
        for _ in range(15):
            drain(env)
            env.db.expire_all()
            detail = request(env, "GET", f"v1/runs/{run['id']}")
            if detail["status"] == "completed":
                break
            assert detail["status"] in {"waiting_human", "waiting_external"}, [(n.node_id, n.status, n.error_code, n.error_message) for n in env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"], status="failed").all()]
            if detail["status"] == "waiting_human":
                task = env.db.query(ExecutionHumanTask).filter_by(run_id=run["id"], status="open").one()
                node = env.db.get(ExecutionNodeRun, task.node_run_id)
                data = {**node.input_data["defaults"], "selected_project_key": project["project_key"],
                        **({"sample_name": "棉布"} if not paper else {})}
                if judgement:
                    data.update(judge_basis=project["check_method"], judgement="符合", **({"standard_value": "木浆 100"} if paper else {"indicator_requirement": "清晰", "test_result": "表面形貌清晰"}))
                request(env, "POST", f"v1/human-tasks/{task.id}/submit", {"revision": task.revision, "data": data})
                continue
            pending = env.db.query(ExecutionExternalOperation).filter_by(run_id=run["id"], status="approved").one()
            claimed = claim_approved_external_operation(env.db, bridge_id="business-writer", account_name="business-tester", supported_operation_types={pending.request_summary["operation_type"]})
            assert claimed is not None
            operation, attempt, _credential = claimed
            env.db.commit()
            record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="business-writer", stage=_operation_stage_profile(operation)[2])
            env.db.commit()
            complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="business-writer", receipt=operation_receipt(operation))
            env.db.commit()
            completed += 1
        else:
            raise AssertionError("business workflow did not finish")
    assert completed == 3
    assert detail["output_data"]["registration"]["status"] == "completed"
    if not paper:
        assert detail["output_data"]["report_images"]["placed_count"] == 1


def test_all_nine_builtins_have_complete_native_candidates(environment):
    from app.execution.release_v2 import P2_COMPLETE_WORKFLOW_SLUGS
    from app.execution.v2.regenerated_fiber_migration import WORKFLOW_METHODS

    slugs = set(P2_COMPLETE_WORKFLOW_SLUGS) | set(WORKFLOW_METHODS) | set(WORKFLOW_SLUGS)
    assert len(slugs) == 9
    for slug in sorted(slugs):
        migration = preview(environment, slug, profile="native_p4")
        assert migration["content_valid"], (slug, migration.get("issues"))
        assert migration["compatibility_node_count"] == 0
        assert migration["migration_status"] == "p4_complete", (slug, migration.get("blockers"))


def test_custom_business_input_is_never_silently_removed(environment):
    env = environment
    source = source_workflow(env, "paper-fiber-gbt4688-2020-qualitative")
    definition = deepcopy(source.draft_definition)
    definition["nodes"].append({"id": "custom", "type": "human.input", "type_version": 1, "name": "客户特别要求",
                                "config": {}, "input_mapping": {}})
    source.draft_definition = definition
    env.db.commit()
    migration = preview(env, source.slug, source="draft", profile="native_p4")
    assert migration["migration_status"] != "p4_complete"
    assert any(issue.get("node_id") == "custom" for issue in migration["blockers"])


@pytest.mark.parametrize("value", [False, {"properties": []}, {"required": 1}])
def test_runtime_form_rejects_malformed_conditions_without_crashing(value):
    from app.execution.release_v2 import runtime_form_schema_issues

    schema = {"type": "object", "properties": {}, "additionalProperties": False,
              "allOf": [{"if": {"properties": {}}, "then": value}]}
    assert runtime_form_schema_issues(schema)
