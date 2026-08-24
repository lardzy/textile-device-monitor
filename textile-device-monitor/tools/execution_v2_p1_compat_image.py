#!/usr/bin/env python3
"""Verify or build the frozen P1 compatibility Worker image.

The build context is always materialized from the exact P1 commit.  The sole
overlay is Alembic 0009, which lets the old Worker pass the schema-head guard
without changing application or Pack bytes.  Docker is invoked only with the
explicit ``--build`` flag.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


BASELINE = "d1fb01bbd12e3b020f1e27a2ecbeab20bf039ac4"
EXPECTED_CAPABILITY = {
    "baseline_commit": BASELINE,
    "protocol_version": "2.0",
    "engine_version": "2.0.0",
    "node_count": 39,
    "ready_count": 37,
    "capability_digest": (
        "7c3849d8ad705fbff9c9cc7cb706f27ef3805f38323616bdb99fc3b25688c4f1"
    ),
}

_IMAGE_CAPABILITY_SCRIPT = """
import json
from app.execution.worker import ExecutionWorker

document = ExecutionWorker(worker_id="p1-image-self-check").capability_document
print(json.dumps({
    "protocol_version": document.get("protocol_version"),
    "engine_version": document.get("engine_version"),
    "node_count": len(document.get("nodes") or []),
    "ready_count": sum(
        1 for item in (document.get("nodes") or []) if item.get("ready")
    ),
    "capability_digest": document.get("capability_digest"),
}, sort_keys=True))
""".strip()


def _run(*args: str, cwd: Path, capture: bool = False) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return completed.stdout.strip() if capture else ""


def _paths() -> tuple[Path, Path, str]:
    project_root = Path(__file__).resolve().parents[1]
    git_root = Path(
        _run("git", "rev-parse", "--show-toplevel", cwd=project_root, capture=True)
    )
    prefix = _run(
        "git",
        "rev-parse",
        "--show-prefix",
        cwd=project_root,
        capture=True,
    ).rstrip("/")
    return project_root, git_root, prefix


def verify() -> tuple[Path, Path, str]:
    project_root, git_root, prefix = _paths()
    _run("git", "cat-file", "-e", f"{BASELINE}^{{commit}}", cwd=git_root)
    snapshot_path = (
        project_root
        / "backend/app/execution/v2/resources/snapshots/p1-worker-d1fb01b.json"
    )
    actual = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if actual != EXPECTED_CAPABILITY:
        raise SystemExit("P1 capability snapshot does not match the frozen baseline")
    migration = project_root / "backend/alembic/versions/0009_execution_v2_primitives.py"
    if not migration.is_file():
        raise SystemExit("0009_execution_v2_primitives.py is missing")
    baseline_backend = f"{prefix + '/' if prefix else ''}backend"
    _run(
        "git",
        "cat-file",
        "-e",
        f"{BASELINE}:{baseline_backend}/Dockerfile",
        cwd=git_root,
    )
    return project_root, git_root, prefix


def build(tag: str) -> None:
    project_root, git_root, prefix = verify()
    with tempfile.TemporaryDirectory(prefix="execution-p1-image-") as directory:
        worktree = Path(directory) / "source"
        _run(
            "git",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            BASELINE,
            cwd=git_root,
        )
        try:
            baseline_project = worktree / prefix if prefix else worktree
            source_migration = (
                project_root
                / "backend/alembic/versions/0009_execution_v2_primitives.py"
            )
            target_migration = (
                baseline_project
                / "backend/alembic/versions/0009_execution_v2_primitives.py"
            )
            shutil.copyfile(source_migration, target_migration)
            _run(
                "docker",
                "build",
                "--tag",
                tag,
                str(baseline_project / "backend"),
                cwd=git_root,
            )
            capability = json.loads(
                _run(
                    "docker",
                    "run",
                    "--rm",
                    "--env",
                    "APP_ENV=test",
                    tag,
                    "python",
                    "-c",
                    _IMAGE_CAPABILITY_SCRIPT,
                    cwd=git_root,
                    capture=True,
                )
            )
            expected = {
                key: value
                for key, value in EXPECTED_CAPABILITY.items()
                if key != "baseline_commit"
            }
            if capability != expected:
                raise SystemExit(
                    "built P1 image capability does not match the frozen baseline"
                )
        finally:
            _run(
                "git",
                "worktree",
                "remove",
                "--force",
                str(worktree),
                cwd=git_root,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build",
        action="store_true",
        help="build the compatibility image after verification",
    )
    parser.add_argument(
        "--tag",
        default="textile-execution-worker:p1-d1fb01b",
        help="Docker image tag used only with --build",
    )
    args = parser.parse_args()
    if args.build:
        build(args.tag)
        print(f"built {args.tag}")
    else:
        verify()
        print("P1 compatibility image inputs are frozen and reproducible")


if __name__ == "__main__":
    main()
