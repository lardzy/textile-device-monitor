"""Isolated LibreOffice/UNO writer for old CheckRecord ``Sheet1`` templates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def _property(name, value):
    import uno

    item = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    item.Name = name
    item.Value = value
    return item


def _file_url(path):
    import uno

    return uno.systemPathToFileUrl(str(Path(path).resolve()))


def _connect(pipe_name, timeout=30.0):
    import uno

    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    address = "uno:pipe,name=%s;urp;StarOffice.ComponentContext" % pipe_name
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return resolver.resolve(address)
        except Exception as exc:  # pyuno exposes runtime-specific exceptions
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError("libreoffice_connection_timeout") from last_error


def _write(payload):
    pipe_name = "microscopy_check_record_%s" % uuid4().hex
    with tempfile.TemporaryDirectory(
        prefix="microscopy-check-record-lo-profile-"
    ) as profile:
        command = [
            os.environ.get("EXECUTION_SOFFICE_BIN", "soffice"),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            "-env:UserInstallation=%s" % _file_url(profile),
            "--accept=pipe,name=%s;urp;StarOffice.ServiceManager" % pipe_name,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        document = None
        reopened = None
        try:
            context = _connect(pipe_name)
            desktop = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", context
            )
            workbook_url = _file_url(payload["workbook_path"])
            document = desktop.loadComponentFromURL(
                workbook_url,
                "_blank",
                0,
                (
                    _property("Hidden", True),
                    _property("ReadOnly", False),
                ),
            )
            if document is None:
                raise RuntimeError("workbook_open_failed")
            sheet = document.Sheets.getByName(payload["sheet_name"])
            for cell, value in payload["cells"].items():
                sheet.getCellRangeByName(cell).String = str(value or "")
            document.calculateAll()
            document.store()
            document.close(True)
            document = None

            reopened = desktop.loadComponentFromURL(
                workbook_url,
                "_blank",
                0,
                (_property("Hidden", True), _property("ReadOnly", True)),
            )
            if reopened is None:
                raise RuntimeError("workbook_reopen_failed")
            sheet = reopened.Sheets.getByName(payload["sheet_name"])
            actual_cells = {
                cell: sheet.getCellRangeByName(cell).String
                for cell in payload["cells"]
            }
            expected_cells = {
                cell: str(value or "") for cell, value in payload["cells"].items()
            }
            return {
                "verified": actual_cells == expected_cells,
                "sheet_name": payload["sheet_name"],
                "cells": actual_cells,
                "recalculated": True,
                "reopened": True,
            }
        finally:
            if reopened is not None:
                try:
                    reopened.close(True)
                except Exception:
                    pass
            if document is not None:
                try:
                    document.close(True)
                except Exception:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: microscopy_check_record_uno.py PAYLOAD.json")
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(_write(payload), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv)
