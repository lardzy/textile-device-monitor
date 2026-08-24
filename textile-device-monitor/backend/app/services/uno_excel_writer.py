#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def _property(name, value):
    import uno

    prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    prop.Name = name
    prop.Value = value
    return prop


def _file_url(path: Path | str) -> str:
    import uno

    return uno.systemPathToFileUrl(str(Path(path).resolve()))


def _connect_uno(pipe_name: str, timeout_sec: int = 30):
    import uno

    start = time.monotonic()
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    last_exc = None
    while time.monotonic() - start < timeout_sec:
        try:
            return resolver.resolve(
                f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
            )
        except Exception as exc:  # pragma: no cover - UNO exception type varies
            last_exc = exc
            time.sleep(0.2)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("uno_connect_timeout")


def _load_document(desktop, excel_path: Path, *, read_only: bool):
    document = desktop.loadComponentFromURL(
        _file_url(excel_path),
        "_blank",
        0,
        (
            _property("Hidden", True),
            _property("ReadOnly", read_only),
        ),
    )
    if document is None:
        raise RuntimeError("workbook_open_failed")
    return document


def _write_cells(
    excel_path: Path,
    folder_name: str,
    class_names: list[str],
    rows: list[dict[str, int | float]],
    pipe_name: str,
) -> dict[str, object]:
    ctx = _connect_uno(pipe_name)
    desktop = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", ctx
    )
    document = None
    reopened = None
    try:
        document = _load_document(desktop, excel_path, read_only=False)
        raw = document.Sheets.getByName("原始数据")
        report = document.Sheets.getByName("截面统计报告1")

        raw.getCellRangeByName("BA9").setString(folder_name)
        report.getCellRangeByName("F8").setString(folder_name)

        # Fill model class names in order, keep untouched cells as-is.
        for idx, class_name in enumerate(class_names):
            raw.getCellByPosition(52 + idx, 10).setString(str(class_name))

        # Clear data range N11:O3000.
        for row_idx in range(10, 3000):
            raw.getCellByPosition(13, row_idx).setString("")
            raw.getCellByPosition(14, row_idx).setString("")

        # Fill class index + area by stable order prepared by backend.
        for idx, item in enumerate(rows):
            row_idx = 10 + idx
            class_id = int(item.get("class_id", 0))
            area_um2 = float(item.get("area_um2", item.get("area_px", 0.0)))
            raw.getCellByPosition(13, row_idx).setValue(float(max(0, class_id)))
            raw.getCellByPosition(14, row_idx).setValue(float(max(0.0, area_um2)))

        document.store()
        document.close(True)
        document = None

        # Reopen the saved workbook and verify persisted values. A successful
        # UNO call alone is not enough because a failed store can otherwise
        # leave the copied, still-empty template behind.
        reopened = _load_document(desktop, excel_path, read_only=True)
        raw = reopened.Sheets.getByName("原始数据")
        report = reopened.Sheets.getByName("截面统计报告1")

        folder_verified = (
            str(raw.getCellRangeByName("BA9").String or "") == folder_name
            and str(report.getCellRangeByName("F8").String or "") == folder_name
        )
        class_names_verified = all(
            str(raw.getCellByPosition(52 + idx, 10).String or "") == class_name
            for idx, class_name in enumerate(class_names)
        )
        rows_verified = True
        for idx, item in enumerate(rows):
            row_idx = 10 + idx
            expected_class_id = float(max(0, int(item.get("class_id", 0))))
            expected_area = float(
                max(0.0, float(item.get("area_um2", item.get("area_px", 0.0))))
            )
            actual_class_id = float(raw.getCellByPosition(13, row_idx).Value)
            actual_area = float(raw.getCellByPosition(14, row_idx).Value)
            if actual_class_id != expected_class_id or not math.isclose(
                actual_area,
                expected_area,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                rows_verified = False
                break

        next_row_cleared = True
        next_row_idx = 10 + len(rows)
        if next_row_idx < 3000:
            next_row_cleared = (
                str(raw.getCellByPosition(13, next_row_idx).String or "") == ""
                and str(raw.getCellByPosition(14, next_row_idx).String or "") == ""
            )

        verified = (
            folder_verified
            and class_names_verified
            and rows_verified
            and next_row_cleared
        )
        return {
            "verified": verified,
            "folder_verified": folder_verified,
            "class_names_verified": class_names_verified,
            "rows_verified": rows_verified,
            "next_row_cleared": next_row_cleared,
            "row_count": len(rows),
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


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: uno_excel_writer.py <payload_json>", file=sys.stderr)
        return 2

    payload_path = Path(sys.argv[1])
    if not payload_path.exists():
        print("payload_not_found", file=sys.stderr)
        return 2

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except Exception:
        print("payload_invalid", file=sys.stderr)
        return 2

    excel_path = Path(str(payload.get("excel_path") or "")).resolve()
    folder_name = str(payload.get("folder_name") or "")
    class_names = [str(item) for item in list(payload.get("class_names") or [])]
    rows = list(payload.get("rows") or [])

    if not excel_path.exists():
        print("excel_target_missing", file=sys.stderr)
        return 2

    pipe_name = f"area_{uuid4().hex}"
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="area-lo-profile-") as profile_dir:
            cmd = [
                "soffice",
                "--headless",
                "--norestore",
                "--nodefault",
                "--nofirststartwizard",
                "--nolockcheck",
                "--nologo",
                f"-env:UserInstallation={_file_url(profile_dir)}",
                f"--accept=pipe,name={pipe_name};urp;StarOffice.ServiceManager",
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result = _write_cells(
                excel_path,
                folder_name,
                class_names,
                rows,
                pipe_name,
            )
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0
    except Exception as exc:
        print(f"uno_write_failed:{exc}", file=sys.stderr)
        return 3
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
