#!/usr/bin/env python3
"""Run a JSON release's offline calculation/contract fixtures without a database."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.execution.v2.fixtures import run_fixtures

if __name__ == "__main__":
    report = run_fixtures(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] is True else 1)
