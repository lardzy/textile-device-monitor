"""Small JSON operations. More involved calculations belong in data.python."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

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


def python_compute(context):
    config = context.node["config"]
    from app.execution.v2.python_runner import validate_source

    validate_source(config["code"])
    for direction in ("input", "output"):
        check_local_schema(config[f"{direction}_schema"])
    Draft202012Validator(config["input_schema"]).validate(context.input_data)
    payload = json.dumps({"code": config["code"], "inputs": context.input_data}, ensure_ascii=False, allow_nan=False)
    if len(payload.encode()) > 1024 * 1024:
        raise ExecutionApiError(422, "python_input_too_large", "Python 节点输入不能超过 1 MiB")
    with tempfile.TemporaryDirectory(prefix="workflow-python-") as directory:
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(Path(__file__).with_name("python_runner.py"))],
                input=payload, text=True, capture_output=True, cwd=directory,
                env={}, timeout=config.get("timeout_seconds", 5), check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionApiError(422, "python_timeout", "Python 计算超过执行时间") from exc
    if result.returncode:
        raise ExecutionApiError(422, "python_execution_failed", "Python 计算失败", details={"message": result.stderr[:4000]})
    response = json.loads(result.stdout)
    output = response["output"]
    Draft202012Validator(config["output_schema"]).validate(output)
    return output


NATIVE_HANDLERS = {("data.transform", 1): transform, ("data.validate", 1): validate, ("data.python", 1): python_compute}
