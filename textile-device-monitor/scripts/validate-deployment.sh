#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname -- "$script_dir")
python_bin="${DEPLOYMENT_PYTHON:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "未找到 Python：$python_bin" >&2
    exit 1
fi
if ! "$python_bin" -c "import cryptography" >/dev/null 2>&1; then
    echo "部署预检缺少依赖，请先执行：" >&2
    echo "  $python_bin -m pip install -r ${script_dir}/requirements-deployment.txt" >&2
    exit 1
fi

PYTHONPATH="${project_root}/backend${PYTHONPATH:+:${PYTHONPATH}}" \
    exec "$python_bin" -m app.deployment_validation "$@"
