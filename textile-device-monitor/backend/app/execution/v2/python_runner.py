"""Version 1 pure computation runner for trusted workflow authors.

This is a resource-bounded subprocess, not an OS sandbox for hostile code.
Only JSON crosses the boundary; no application/database/credential objects do.
"""

import ast
import builtins
import contextlib
import importlib
import io
import json
import sys

MODULES = {"json", "math", "re", "datetime", "decimal", "statistics", "collections", "itertools"}
BUILTINS = {"abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
            "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
            "Exception", "ValueError", "TypeError", "KeyError", "print"}


def validate_source(code):
    if not isinstance(code, str) or len(code.encode()) > 65536:
        raise ValueError("Python 源码最多 64 KiB")
    tree = ast.parse(code)
    if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body):
        raise ValueError("请定义 main(inputs)，返回 JSON 对象")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("不能访问私有名称")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("不能访问私有属性")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module]
            if getattr(node, "level", 0) or any(name not in MODULES for name in names):
                raise ValueError("仅支持计算模块：" + ", ".join(sorted(MODULES)))
    return tree


class BoundedLog(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > 16384:
            raise ValueError("计算日志超过 16 KiB")
        return super().write(value)


def run(payload):
    tree = validate_source(payload["code"])
    # Imports are loaded before user code. The import hook never resolves arbitrary modules.
    modules = {name: importlib.import_module(name) for name in MODULES}
    def allowed_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in modules or any(str(item).startswith("_") for item in (fromlist or ())):
            raise ValueError("不支持的模块或导入名称")
        return modules[name]
    namespace = {"__builtins__": {name: getattr(builtins, name) for name in BUILTINS}}
    namespace["__builtins__"]["__import__"] = allowed_import
    with contextlib.redirect_stdout(BoundedLog()):
        exec(compile(tree, "<workflow-python-v1>", "exec"), namespace)
        output = namespace["main"](payload["inputs"])
    if not isinstance(output, dict):
        raise ValueError("main(inputs) 必须返回 JSON 对象")
    encoded = json.dumps({"output": output}, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError("Python 输出超过 1 MiB")
    return encoded


if __name__ == "__main__":
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            if sys.platform == "linux":
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        except ImportError:
            pass
        print(run(json.loads(sys.stdin.read(1024 * 1024 + 1))))
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
