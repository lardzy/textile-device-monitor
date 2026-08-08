#!/usr/bin/env python3
"""按编号刷新/查询旧系统任务快照（execution_api_260111037.py 的通用版）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_api_260111037 import API_BASE, checked, login  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"refresh", "status"}:
        print("usage: snapshot_tool.py refresh|status <inspection_number>", file=sys.stderr)
        return 2
    action, number = sys.argv[1], sys.argv[2]
    session, csrf = login()
    if action == "refresh":
        payload = checked(
            session.post(
                f"{API_BASE}/task-snapshots/{number}/refresh?force=true",
                headers={"X-CSRF-Token": csrf},
                timeout=15,
            )
        )
    else:
        payload = checked(
            session.get(f"{API_BASE}/task-snapshots/{number}/status", timeout=15)
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
