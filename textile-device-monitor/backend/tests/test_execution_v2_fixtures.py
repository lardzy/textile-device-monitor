from copy import deepcopy

from app.execution.v2.data_examples import build_data_python_release
from app.execution.v2.fixtures import run_fixtures
from tests.test_execution_workflow_replacement import environment, request


def test_calculation_uses_actual_python_and_reports_assertion_failure():
    document = build_data_python_release()
    result = run_fixtures(document)
    assert result["passed"], result
    assert result["items"][0]["outputs"] == {"total": 6}
    assert not result["items"][0]["nodes"]["compute"]["mocked"]
    document["fixtures"][0]["assertions"][1]["value"] = 7
    result = run_fixtures(document)
    assert not result["passed"]
    assert result["items"][0]["assertions"][1]["actual"] == 6


def test_external_steps_require_explicit_typed_mock_and_never_dispatch(monkeypatch):
    document = build_data_python_release()
    compute = document["definition"]["nodes"][1]
    compute.update(type="human.form", config={"title": "输入", "form_schema": compute["config"]["output_schema"]}, input_mapping={})
    result = run_fixtures(document)
    assert not result["passed"]
    assert "显式 Mock" in result["items"][0]["error"]
    document["fixtures"][0]["mocks"] = [{"node_id": "compute", "outcome": "succeeded", "output": {"total": 6}}]
    assert run_fixtures(document)["passed"]
    document["fixtures"][0]["mocks"][0]["output"] = {"total": "bad type"}
    assert not run_fixtures(document)["passed"]


def test_fixture_failures_missing_assertions_and_unsupported_graph_are_explicit():
    document = build_data_python_release()
    fixture = document["fixtures"][0]
    fixture["mocks"] = [{"node_id": "compute", "outcome": "failed", "output": {}, "error_code": "example_failure"}]
    fixture["assertions"] = [{"path": "$.run.status", "operator": "eq", "value": "failed"},
                             {"path": "$.nodes.compute.error_code", "operator": "eq", "value": "example_failure"}]
    assert run_fixtures(document)["passed"]
    fixture["mocks"][0]["outcome"] = "skipped"
    skipped = run_fixtures(document)
    assert not skipped["passed"] and "跳过" in skipped["items"][0]["error"]
    fixture["assertions"] = []
    assert not run_fixtures(document)["passed"]
    document["fixtures"] = []
    assert run_fixtures(document)["passed"] is None
    document = build_data_python_release()
    document["definition"]["edges"].append({"id": "parallel", "source": "start", "target": "end", "join_policy": "all"})
    result = run_fixtures(document)
    assert not result["passed"]
    assert result["issues"]


def test_fixture_api_is_readonly_and_does_not_create_runs(environment):
    from app.execution.models import ExecutionRun

    document = build_data_python_release()
    before = deepcopy(document)
    result = request(environment, "POST", "v2/designer/test", {"document": document})
    assert result["passed"], result
    assert document == before
    assert environment.db.query(ExecutionRun).count() == 0
