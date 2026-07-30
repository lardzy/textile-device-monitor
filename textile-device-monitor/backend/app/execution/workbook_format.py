from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional
from zipfile import BadZipFile, ZipFile


SUPPORTED_WORKBOOK_SUFFIXES = frozenset(
    {
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlt",
        ".xltx",
        ".xltm",
    }
)

_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC_PREFIXES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)


class WorkbookFormat(str, Enum):
    OLE = "ole"
    OOXML = "ooxml"


def detect_workbook_format(path: Path) -> Optional[WorkbookFormat]:
    """Identify the workbook container from its file header.

    The extension remains an API-level allow-list, but it is not reliable
    enough to select a reader: legacy sites may rename an OOXML workbook from
    ``.xlsm`` to ``.xls`` without converting its contents.
    """

    with path.open("rb") as stream:
        header = stream.read(len(_OLE_COMPOUND_FILE_MAGIC))
    if header.startswith(_OLE_COMPOUND_FILE_MAGIC):
        return WorkbookFormat.OLE
    if header.startswith(_ZIP_MAGIC_PREFIXES):
        try:
            with ZipFile(path, "r") as archive:
                members = set(archive.namelist())
        except (BadZipFile, OSError):
            return None
        if {
            "[Content_Types].xml",
            "xl/workbook.xml",
        }.issubset(members):
            return WorkbookFormat.OOXML
    return None
