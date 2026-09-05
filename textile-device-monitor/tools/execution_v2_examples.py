#!/usr/bin/env python3
"""Check or explicitly refresh deterministic Execution v2 example Releases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
EXAMPLE_ROOT = REPOSITORY_ROOT / "docs" / "execution-v2" / "examples"
sys.path.insert(0, str(BACKEND_ROOT))

from app.execution.v2.examples import (  # noqa: E402
    build_controlled_xlsx_write_canary_release,
    build_native_human_file_selection_smoke_release,
    build_readonly_file_query_smoke_release,
)


BUILDERS = {
    "v2-controlled-xlsx-write-canary.json": (
        build_controlled_xlsx_write_canary_release
    ),
    "v2-native-human-file-selection-smoke.json": (
        build_native_human_file_selection_smoke_release
    ),
    "v2-readonly-file-query-smoke.json": build_readonly_file_query_smoke_release,
}


def _render(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    stale: list[Path] = []
    for name, builder in BUILDERS.items():
        path = EXAMPLE_ROOT / name
        expected = _render(builder())
        if path.is_file() and path.read_bytes() == expected:
            continue
        stale.append(path)
        if args.update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    if stale and not args.update:
        print("Execution v2 examples are stale; run with --update", file=sys.stderr)
        return 1
    print(
        f"{'updated' if stale else 'checked'} {len(BUILDERS)} Execution v2 examples"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
