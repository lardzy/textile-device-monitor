from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.api.execution import (
    _inline_raster_media_type,
    _validate_workbook_preview_archive,
)
from app.execution.errors import ExecutionApiError


def test_inline_preview_rejects_svg_and_uses_extension_not_stored_media_type():
    assert _inline_raster_media_type(".svg") is None
    assert _inline_raster_media_type(".html") is None
    assert _inline_raster_media_type(".PNG") == "image/png"


def test_workbook_preview_rejects_invalid_zip(tmp_path: Path):
    workbook = tmp_path / "broken.xlsx"
    workbook.write_bytes(b"not-a-zip")

    with pytest.raises(ExecutionApiError) as caught:
        _validate_workbook_preview_archive(workbook)

    assert caught.value.status_code == 422
    assert caught.value.code == "workbook_preview_invalid_archive"


def test_workbook_preview_rejects_high_compression_ratio(tmp_path: Path):
    workbook = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(
        workbook,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("xl/sharedStrings.xml", b"A" * (11 * 1024 * 1024))

    with pytest.raises(ExecutionApiError) as caught:
        _validate_workbook_preview_archive(workbook)

    assert caught.value.status_code == 413
    assert caught.value.code == "workbook_preview_archive_ratio_exceeded"


def test_workbook_preview_accepts_small_ooxml_archive(tmp_path: Path):
    workbook = tmp_path / "small.xlsx"
    with zipfile.ZipFile(
        workbook,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("[Content_Types].xml", b"<Types />")
        archive.writestr("xl/workbook.xml", b"<workbook />")

    _validate_workbook_preview_archive(workbook)
