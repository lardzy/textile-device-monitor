from copy import deepcopy

from app.execution.v2.designer import compile_document, starter_document
from app.execution.v2.examples import build_connector_operation_smoke_release
from workflow_native_helpers import environment, request


def test_compile_rebuilds_native_dependencies_and_digest_without_mutating_input():
    document = starter_document()
    before = deepcopy(document)
    document["definition"]["nodes"].append({"id": "form", "type": "human.form", "type_version": 1, "name": "输入",
        "config": {"title": "输入", "form_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "additionalProperties": False}}, "input_mapping": {}})
    document["definition"]["edges"] = [
        {"id": "a", "source": "start", "target": "form", "join_policy": "all"},
        {"id": "b", "source": "form", "target": "end", "join_policy": "all"},
    ]
    result = compile_document(document)
    assert result["content_valid"], result["issues"]
    assert result == compile_document(document)
    assert len(document["dependencies"]["node_types"]) == len(before["dependencies"]["node_types"])
    assert result["document"]["integrity"]["digest"] != before["integrity"]["digest"]
    assert any(n["type"] == "human.form" for n in result["document"]["dependencies"]["node_types"])


def test_compile_connector_keeps_exact_operation_bindings():
    result = compile_document(build_connector_operation_smoke_release())
    assert result["content_valid"], result["issues"]
    assert result["document"]["dependencies"]["connectors"][0]["operations"]
    from app.execution.release_v2 import _resolve_dependencies
    dependency = result["document"]["dependencies"]["connectors"][0]
    assert dependency["version_range"].startswith(">=")
    assert "contract_digest" not in dependency["operations"][0]
    lock, _ = _resolve_dependencies(result["document"], [])
    assert lock["connectors"][0]["operations"][0]["contract_digest"]
    exact = compile_document(build_connector_operation_smoke_release(), exact=True)
    assert exact["document"]["dependencies"]["connectors"][0]["operations"][0]["contract_digest"]


def test_catalog_and_compilation_api(environment):
    catalog = request(environment, "GET", "v2/designer/catalog")
    assert {item["template_id"] for item in catalog["templates"]} == {"paper-fiber-v2", "fiber-microscopy-v2"}
    assert all(n["source"] == "resource" for n in catalog["node_specs"])
    result = request(environment, "POST", "v2/designer/compile", {"document": catalog["starter"]})
    assert result["content_valid"], result["issues"]
    bad = deepcopy(catalog["starter"])
    bad["definition"]["edges"].append({"id": "cycle", "source": "end", "target": "start", "join_policy": "all"})
    result = request(environment, "POST", "v2/designer/compile", {"document": bad})
    assert not result["content_valid"]


def test_designed_native_graph_publishes_runs_and_survives_version_rollback(environment):
    from app.execution.models import ExecutionHumanTask
    from workflow_native_helpers import stage, publish, run, drain

    env = environment
    document = starter_document()
    document["definition"]["nodes"].insert(1, {"id": "form", "type": "human.form", "type_version": 1, "name": "备注",
        "config": {"title": "填写备注", "form_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}}, "input_mapping": {}})
    document["definition"]["edges"] = [{"id": "a", "source": "start", "target": "form", "join_policy": "all"},
        {"id": "b", "source": "form", "target": "end", "join_policy": "all"}]
    document["definition"]["output_schema"] = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}
    document["definition"]["nodes"][-1]["input_mapping"] = {"note": "$.nodes.form.output.note"}
    compiled = request(env, "POST", "v2/designer/compile", {"document": document})
    assert compiled["content_valid"], compiled["issues"]
    first = publish(env, stage(env, compiled["document"]))
    task_run = run(env, first["workflow_id"])
    drain(env)
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=task_run["id"], status="open").one()
    next_document = deepcopy(compiled["document"])
    next_document["release"]["release_version"] = 2
    next_document["definition"]["nodes"][-1]["input_mapping"] = {"note": "新版本"}
    second = compile_document(next_document)
    publish(env, stage(env, second["document"]))
    # An outstanding form keeps its original graph after editing and publishing.
    request(env, "POST", f"v1/human-tasks/{task.id}/submit", {"revision": task.revision, "data": {"note": "原始版本"}})
    drain(env)
    detail = request(env, "GET", f"v1/runs/{task_run['id']}")
    assert detail["status"] == "completed"
    assert detail["output_data"] == {"note": "原始版本"}

    request(env, "POST", f"v2/workflows/{first['workflow_id']}/rollback", {"target_local_version": 1, "reason": "设计器版本回退验收"})
    replay = run(env, first["workflow_id"])
    drain(env)
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=replay["id"], status="open").one()
    request(env, "POST", f"v1/human-tasks/{task.id}/submit", {"revision": task.revision, "data": {"note": "回退后"}})
    drain(env)
    assert request(env, "GET", f"v1/runs/{replay['id']}")["output_data"] == {"note": "回退后"}


def test_templates_remain_available_when_a_suggested_slug_is_occupied(environment):
    doc = starter_document()
    doc['release']['slug'] = 'paper-fiber-v2'
    request(environment, 'POST', 'v2/designer/drafts', {'document': doc, 'bindings': {}}, status=201)
    catalog = request(environment, 'GET', 'v2/designer/catalog')
    assert len(catalog['templates']) == 2
    assert next(t for t in catalog['templates'] if t['template_id']=='paper-fiber-v2')['candidate']['release']['slug'] == 'paper-fiber-v2-2'


def test_catalog_does_not_depend_on_legacy_workflow_rows(environment):
    from app.execution.models import ExecutionWorkflow

    for workflow in environment.db.query(ExecutionWorkflow).all():
        workflow.slug = "local-" + workflow.slug
        workflow.management_mode = "release_v2"
    environment.db.commit()
    catalog = request(environment, "GET", "v2/designer/catalog")
    assert {item["template_id"] for item in catalog["templates"]} == {"paper-fiber-v2", "fiber-microscopy-v2"}
    assert all(item["workflow_id"] is None and item["replacement_source"] is None for item in catalog["templates"])
    for item in catalog["templates"]:
        assert compile_document(item["candidate"])["content_valid"]
