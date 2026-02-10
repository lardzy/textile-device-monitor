#!/usr/bin/env bash
set -euo pipefail

cd /opt/GLM-OCR

python - <<'PY'
from pathlib import Path
import re

config_path = Path("glmocr/config.yaml")
text = config_path.read_text(encoding="utf-8")

# Point GLM-OCR to vLLM service and expose HTTP on all interfaces.
text = re.sub(r"(?m)^(\s*api_host:\s*).*$", r"\g<1>vllm", text, count=1)
text = re.sub(r"(?m)^(\s*api_port:\s*).*$", r"\g<1>8080", text, count=1)
text = re.sub(r"(?m)^(\s*host:\s*).*$", r'\g<1>"0.0.0.0"', text, count=1)
text = re.sub(r"(?m)^(\s*port:\s*).*$", r"\g<1>5002", text, count=1)

config_path.write_text(text, encoding="utf-8")
PY

exec python -m glmocr.server
