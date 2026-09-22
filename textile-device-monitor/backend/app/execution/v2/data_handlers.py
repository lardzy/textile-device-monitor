"""Small JSON operations. More involved calculations belong in data.python."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter

from jsonschema import Draft202012Validator

from app.execution.errors import ExecutionApiError


def select_value(data, expression):
    if not isinstance(expression, dict) or "path" not in expression:
        return deepcopy(expression.get("value") if isinstance(expression, dict) and set(expression) == {"value"} else expression)
    current = data
    try:
        path = expression["path"].removeprefix("#")
        if path and not path.startswith("/"):
            raise ValueError("字段路径必须是 JSON Pointer，如 #/sample/name")
        for key in path.split("/")[1:]:
            key = key.replace("~1", "/").replace("~0", "~")
            current = current[int(key)] if isinstance(current, list) else current[key]
    except (KeyError, IndexError, TypeError):
        if "default" not in expression:
            raise ExecutionApiError(422, "data_field_missing", f"输入缺少字段：{expression['path']}")
        current = expression["default"]
    return deepcopy(current)


def transform(context):
    value = deepcopy(context.input_data["data"])
    for step in context.node["config"]["steps"]:
        operation = step["op"]
        if operation == "select":
            value = select_value(value, {"path": step["path"]})
        elif operation in {"project", "map"}:
            def project(item):
                return {key: select_value(item, expression) for key, expression in step["fields"].items()}
            value = [project(item) for item in value] if operation == "map" else project(value)
        elif operation == "filter":
            value = [item for item in value if select_value(item, {"path": step["path"], "default": None}) == step["equals"]]
        elif operation == "sort":
            value = sorted(value, key=lambda item: select_value(item, {"path": step["path"]}), reverse=step.get("descending", False))
        elif operation == "unique":
            seen = set()
            result = []
            for item in value:
                key = json.dumps(select_value(item, {"path": step.get("path", "")}), sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
            value = result
        elif operation == "limit":
            value = value[:step["count"]]
        else:
            raise ExecutionApiError(422, "data_operation_unknown", f"未知转换：{operation}")
    return {"data": value}


def check_local_schema(schema):
    Draft202012Validator.check_schema(schema)
    def visit(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {"$ref", "$dynamicRef"} and not str(value).startswith("#"):
                    raise ValueError("数据节点仅支持内联或本地 JSON Schema 引用")
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)
    visit(schema)


def validate(context):
    config, data = context.node["config"], context.input_data["data"]
    check_local_schema(config["schema"])
    errors = [{"path": list(error.absolute_path), "message": error.message}
              for error in Draft202012Validator(config["schema"]).iter_errors(data)]
    if errors and config.get("fail_on_error", True):
        raise ExecutionApiError(422, "data_validation_failed", "数据不符合规则", details={"errors": errors})
    return {"data": data, "valid": not errors, "errors": errors}


def python_test(config, inputs):
    """The same execution path serves trials and Worker runs; diagnostics stay outside outputs."""
    from app.execution.v2.python_runner import validate_source
    started = perf_counter()
    report = {"passed": False, "output": None, "logs": "", "error": None}
    stage = "config"
    try:
        if config.get("runtime_version", 1) != 1:
            raise ValueError("不支持的 Python 运行版本")
        timeout = config.get("timeout_seconds", 5)
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("执行时间必须为 1–30 秒")
        for direction in ("input", "output"):
            check_local_schema(config[f"{direction}_schema"])
        stage = "code"
        validate_source(config["code"])
        stage = "input"
        Draft202012Validator(config["input_schema"]).validate(inputs)
        payload = json.dumps({"code": config["code"], "inputs": inputs}, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("Python 节点输入不能超过 1 MiB")
        stage = "code"
        with tempfile.TemporaryDirectory(prefix="workflow-python-") as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(Path(__file__).with_name("python_runner.py"))],
                input=payload, text=True, capture_output=True, cwd=directory,
                env={}, timeout=timeout, check=False,
            )
        response = json.loads(result.stdout) if result.stdout else {}
        report["logs"] = response.get("logs", "")[:16384]
        if result.returncode:
            report["error"] = response.get("error") or {"stage": stage, "type": "ProcessError", "message": result.stderr[:4000] or "计算进程退出", "line": None}
        else:
            stage = "output"
            Draft202012Validator(config["output_schema"]).validate(response["output"])
            report.update(passed=True, output=response["output"])
    except subprocess.TimeoutExpired:
        report["error"] = {"stage": "timeout", "type": "Timeout", "message": "Python 计算超过执行时间", "line": None}
    except Exception as exc:
        report["error"] = {"stage": stage, "type": type(exc).__name__, "message": str(exc)[:4000], "line": getattr(exc, "lineno", None)}
    report["duration_ms"] = round((perf_counter() - started) * 1000, 2)
    return report


def python_compute(context):
    result = python_test(context.node["config"], context.input_data)
    if not result["passed"]:
        raise ExecutionApiError(422, "python_timeout" if result["error"]["stage"] == "timeout" else "python_execution_failed", result["error"]["message"], details=result["error"])
    return result["output"]


NATIVE_HANDLERS = {("data.transform", 1): transform, ("data.validate", 1): validate, ("data.python", 1): python_compute}
