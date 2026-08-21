#!/usr/bin/env python3
"""Check or explicitly update the isolated Execution v2 P0 snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
SNAPSHOT_PATH = (
    BACKEND_ROOT
    / "app"
    / "execution"
    / "v2"
    / "resources"
    / "snapshots"
    / "execution-v1-contracts.json"
)
sys.path.insert(0, str(BACKEND_ROOT))

from app.execution.v2.snapshots import render_contract_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check the deterministic P0 snapshot. Generation uses only an "
            "in-memory database and installed package resources."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="compare without writing (default)",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help="explicitly replace the checked-in snapshot",
    )
    args = parser.parse_args()
    expected = render_contract_snapshot()
    if args.update:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_bytes(expected)
        print(f"updated {SNAPSHOT_PATH.relative_to(REPOSITORY_ROOT)}")
        return 0
    if not SNAPSHOT_PATH.is_file():
        print(
            f"snapshot missing: {SNAPSHOT_PATH.relative_to(REPOSITORY_ROOT)}; "
            "run with --update",
            file=sys.stderr,
        )
        return 1
    actual = SNAPSHOT_PATH.read_bytes()
    if actual != expected:
        print(
            "Execution v2 contract snapshot is stale; inspect the contract "
            "change, then run with --update",
            file=sys.stderr,
        )
        return 1
    print(f"ok {SNAPSHOT_PATH.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
