from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.indexing import IncrementalFileIndexer
from app.execution.storage import (
    ArtifactRef,
    FileGateway,
    RootPermissionError,
    StorageRoot,
    UnsafePathError,
)


def _gateway(tmp_path: Path) -> tuple[FileGateway, Path, Path, Path]:
    source = tmp_path / "source"
    staging = tmp_path / "staging"
    publish = tmp_path / "publish"
    source.mkdir()
    staging.mkdir()
    publish.mkdir()
    gateway = FileGateway(
        [
            StorageRoot("source", source),
            StorageRoot("staging", staging, writable=True),
            StorageRoot("publish", publish, writable=True, publishable=True),
        ]
    )
    return gateway, source, staging, publish


@pytest.mark.parametrize(
    "relative_path",
    [
        "/etc/passwd",
        "../escape.txt",
        "folder/../escape.txt",
        "folder/%2e%2e/escape.txt",
        "folder/%252e%252e/escape.txt",
        r"\\server\share\file.xlsx",
        r"C:\data\file.xlsx",
        "C:/data/file.xlsx",
        "folder//file.xlsx",
        "folder/./file.xlsx",
    ],
)
def test_artifact_ref_rejects_unsafe_paths(relative_path: str):
    with pytest.raises(UnsafePathError):
        ArtifactRef("source", relative_path)


def test_gateway_rejects_symlink_escape(tmp_path: Path):
    gateway, source, _, _ = _gateway(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = source / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")

    with pytest.raises(UnsafePathError):
        gateway.resolve(ArtifactRef("source", "outside-link.txt"))


def test_gateway_allows_internal_symlink_but_stays_inside_root(tmp_path: Path):
    gateway, source, _, _ = _gateway(tmp_path)
    target = source / "nested" / "inside.txt"
    target.parent.mkdir()
    target.write_text("inside", encoding="utf-8")
    link = source / "inside-link.txt"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")

    assert gateway.resolve(ArtifactRef("source", "inside-link.txt")) == target.resolve()


def test_gateway_enforces_write_and_publish_capabilities(tmp_path: Path):
    gateway, _, _, _ = _gateway(tmp_path)

    with pytest.raises(RootPermissionError):
        gateway.resolve(
            ArtifactRef("source", "new.xlsx"),
            must_exist=False,
            for_write=True,
        )
    with pytest.raises(RootPermissionError):
        gateway.resolve(
            ArtifactRef("staging", "new.xlsx"),
            must_exist=False,
            for_publish=True,
        )
    assert gateway.resolve(
        ArtifactRef("publish", "new.xlsx"),
        must_exist=False,
        for_publish=True,
    ).name == "new.xlsx"


def test_incremental_scan_ignores_blacklist_temporary_and_symlinks(tmp_path: Path):
    gateway, source, _, _ = _gateway(tmp_path)
    category = source / "2026-电镜"
    category.mkdir()
    (category / "26A001.SIF").write_bytes(b"sif")
    (category / "26A001.bmp").write_bytes(b"bmp")
    (category / "26A001.txt").write_text("meta", encoding="utf-8")
    (category / "~$26A001.xlsx").write_bytes(b"temp")
    (category / ".~lock.26A001.xlsx#").write_bytes(b"lock")
    recycle = source / ".recycle"
    recycle.mkdir()
    (recycle / "hidden.xlsx").write_bytes(b"hidden")
    old = source / "旧"
    old.mkdir()
    (old / "old.xlsx").write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.xlsx").write_bytes(b"escape")
    try:
        (source / "linked").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pass

    indexer = IncrementalFileIndexer(gateway)
    result = indexer.scan_incremental("source")

    assert result.total_count == 3
    assert {item.name for item in result.added} == {
        "26A001.SIF",
        "26A001.bmp",
        "26A001.txt",
    }
    groups = indexer.electron_groups()
    assert len(groups) == 1
    assert groups[0].complete is True
    assert groups[0].base_name == "26A001"


def test_incremental_scan_reports_upserts_updates_and_removals(tmp_path: Path):
    gateway, source, _, _ = _gateway(tmp_path)
    category = source / "2026-麻棉"
    category.mkdir()
    workbook = category / "26X100.xlsx"
    workbook.write_bytes(b"v1")

    indexer = IncrementalFileIndexer(gateway)
    first = indexer.scan_incremental("source")
    assert len(first.added) == 1
    assert first.as_persistence_delta()["upserts"][0]["file_name"] == "26X100.xlsx"

    workbook.write_bytes(b"version-two")
    os.utime(workbook, None)
    second = indexer.scan_incremental("source")
    assert len(second.updated) == 1
    assert second.updated[0].size == len(b"version-two")

    workbook.unlink()
    third = indexer.scan_incremental("source")
    assert len(third.removed) == 1
    assert third.as_persistence_delta()["removed"] == [
        {"root_id": "source", "relative_path": "2026-麻棉/26X100.xlsx"}
    ]


def test_candidate_query_uses_index_without_rescanning_and_honors_defaults(
    tmp_path: Path,
):
    gateway, source, _, _ = _gateway(tmp_path)
    category = source / "2026-特种毛"
    category.mkdir()
    now = datetime.now(timezone.utc)
    for index in range(8):
        path = category / f"26X900-{index}.xlsx"
        path.write_bytes(str(index).encode())
        modified = (now - timedelta(hours=index)).timestamp()
        os.utime(path, (modified, modified))
    old_path = category / "26X900-old.xlsx"
    old_path.write_bytes(b"old")
    old_modified = (now - timedelta(days=30)).timestamp()
    os.utime(old_path, (old_modified, old_modified))

    indexer = IncrementalFileIndexer(gateway)
    indexer.scan_incremental("source")
    source.rename(tmp_path / "source-unmounted")

    candidates = indexer.find_candidates(
        "26X900",
        categories=["2026-特种毛"],
        now=now,
    )
    assert len(candidates) == 6
    assert all(item.name != "26X900-old.xlsx" for item in candidates)
    assert len(indexer.persistence_snapshot()) == 9


def test_electron_grouping_is_scoped_by_parent_directory(tmp_path: Path):
    gateway, source, _, _ = _gateway(tmp_path)
    for parent_name in ("batch-a", "batch-b"):
        parent = source / "2026-电镜" / parent_name
        parent.mkdir(parents=True)
        (parent / "same.SIF").write_bytes(b"sif")
        (parent / "same.bmp").write_bytes(b"bmp")
        (parent / "same.txt").write_bytes(b"txt")

    indexer = IncrementalFileIndexer(gateway)
    indexer.scan_incremental("source")
    groups = indexer.electron_groups()

    assert len(groups) == 2
    assert {group.parent_path for group in groups} == {
        "2026-电镜/batch-a",
        "2026-电镜/batch-b",
    }
