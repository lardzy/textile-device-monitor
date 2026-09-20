"""Offline sequential fixture runner; never dispatches file, human or Connector IO.

Uses the engine's value mapping and actual data handlers. This checks portable
calculations and mocked contracts, not Worker scheduling or external integration.
Graphs needing branch/join semantics must be tested through the real Worker.
"""

from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from app.execution.engine import _lookup_path, _resolve_value
from app.execution.errors import ExecutionApiError
from app.execution.release_v2 import _resolve_dependencies
from app.execution.v2.data_handlers import NATIVE_HANDLERS
from app.execution.v2.designer import compile_document


def _sequence(definition):
    nodes = {node["id"]: node for node in definition["nodes"]}
    incoming, outgoing = {key: [] for key in nodes}, {key: [] for key in nodes}
    for edge in definition["edges"]:
        if edge.get("condition") not in (None, ""):
            raise ValueError("离线样例暂不执行条件分支，请通过 Worker 测试此流程")
        incoming[edge["target"]].append(edge["source"])
        outgoing[edge["source"]].append(edge["target"])
    if any(len(value) > 1 for value in [*incoming.values(), *outgoing.values()]):
        raise ValueError("离线样例暂不执行并行与汇合，请通过 Worker 测试此流程")
    starts = [key for key, value in incoming.items() if not value]
    if len(starts) != 1:
        raise ValueError("离线样例需要一个连通的顺序流程")
    result, key = [], starts[0]
    while key is not None and key not in {node["id"] for node in result}:
        result.append(nodes[key])
        key = next(iter(outgoing[key]), None)
    if len(result) != len(nodes) or key is not None:
        raise ValueError("流程包含未连接节点或循环")
    return result


def _assertion(assertion, context):
    try:
        actual = _lookup_path(context, assertion["path"], strict=True)
        expected, operator = assertion.get("value"), assertion["operator"]
        passed = {"exists": lambda: True, "eq": lambda: actual == expected,
                  "ne": lambda: actual != expected, "contains": lambda: expected in actual}[operator]()
    except (ExecutionApiError, TypeError):
        actual, passed = None, False
    return {**assertion, "actual": actual, "passed": bool(passed)}


def run_fixtures(document):
    compiled = compile_document(document)
    report = {"content_valid": compiled["content_valid"], "issues": compiled["issues"],
              "mode": "offline_sequential", "passed": False, "items": []}
    if not compiled["content_valid"]:
        return report
    document = compiled["document"]
    if not document["fixtures"]:
        report.update(passed=None, message="尚未配置样例")
        return report
    try:
        sequence = _sequence(document["definition"])
    except ValueError as exc:
        report["issues"].append({"level": "error", "message": str(exc), "path": "$.definition.edges"})
        return report
    lock, _computed = _resolve_dependencies(document, [])
    bindings = {item["node_id"]: item for item in lock["node_instances"]}
    deadline = monotonic() + 30
    for fixture in document["fixtures"]:
        item = {"fixture_id": fixture["fixture_id"], "name": fixture["name"], "passed": False, "assertions": [], "nodes": {}}
        report["items"].append(item)
        try:
            if not fixture["assertions"]:
                raise ValueError("每个样例至少需要一项断言")
            mocks = {mock["node_id"]: mock for mock in fixture["mocks"]}
            if len(mocks) != len(fixture["mocks"]) or set(mocks) - set(bindings):
                raise ValueError("样例 Mock 节点重复或不存在")
            if any(mock["outcome"] == "skipped" for mock in mocks.values()):
                raise ValueError("离线样例不模拟跳过与传播语义，请通过 Worker 测试")
            definition = document["definition"]
            globals_data = {**definition.get("global_defaults", {}), **fixture["globals"]}
            Draft202012Validator(definition["input_schema"]).validate(fixture["inputs"])
            Draft202012Validator(definition["global_schema"]).validate(globals_data)
            context = {"inputs": fixture["inputs"], "globals": globals_data, "nodes": item["nodes"],
                       "outputs": {}, "run": {"status": "running", "mode": "test", "inspection_number": fixture["inputs"].get("inspection_number", "")}}
            for original in sequence:
                node = deepcopy(original)
                identity, node_id = (node["type"], node["type_version"]), node["id"]
                if monotonic() >= deadline:
                    raise ValueError("整组样例超过 30 秒预算")
                if identity not in NATIVE_HANDLERS and identity not in {("core.start", 2), ("core.end", 2)} and node_id not in mocks:
                    raise ValueError(f"{node_id} 需要显式 Mock；离线样例不访问文件、人工待办或外部系统")
                result = {"status": "succeeded", "output": {}, "mocked": node_id in mocks}
                item["nodes"][node_id] = result
                try:
                    inputs = fixture["inputs"] if identity == ("core.start", 2) else _resolve_value(node["input_mapping"], context)
                    Draft202012Validator(bindings[node_id]["effective_input_schema"]).validate(inputs)
                    if node_id in mocks:
                        result.update(status=mocks[node_id]["outcome"], output=deepcopy(mocks[node_id]["output"]))
                        if mocks[node_id].get("error_code"):
                            result["error_code"] = mocks[node_id]["error_code"]
                    elif identity in {("core.start", 2), ("core.end", 2)}:
                        result["output"] = deepcopy(inputs)
                    else:
                        if identity == ("data.python", 1):
                            node["config"]["timeout_seconds"] = max(.01, min(node["config"].get("timeout_seconds", 5), deadline - monotonic()))
                        result["output"] = NATIVE_HANDLERS[identity](SimpleNamespace(node=node, input_data=inputs))
                    if result["status"] == "succeeded":
                        Draft202012Validator(bindings[node_id]["effective_output_schema"]).validate(result["output"])
                except Exception as exc:
                    result.update(status="failed", error_code=getattr(exc, "code", "fixture_node_failed"), error=str(exc)[:2000])
                if result["status"] != "succeeded":
                    context["run"]["status"] = "failed"
                    break
                if identity == ("core.end", 2):
                    context["outputs"] = result["output"]
            else:
                context["run"]["status"] = "completed"
            item["assertions"] = [_assertion(assertion, context) for assertion in fixture["assertions"]]
            item["passed"] = all(assertion["passed"] for assertion in item["assertions"])
            item["run"] = context["run"]
            item["outputs"] = context["outputs"]
        except Exception as exc:
            item["error"] = str(exc)[:2000]
    report["passed"] = all(item["passed"] for item in report["items"])
    return report
