#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _mkprop(name, value):
    from com.sun.star.beans import PropertyValue

    prop = PropertyValue()
    prop.Name = name
    prop.Value = value
    return prop


def _connect_uno(port: int, timeout_sec: int = 20):
    import uno

    start = time.time()
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    last_exc = None
    while time.time() - start < timeout_sec:
        try:
            return resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
            )
        except Exception as exc:  # pragma: no cover - UNO exception type varies
            last_exc = exc
            time.sleep(0.2)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("uno_connect_timeout")


def _write_cells(
    excel_path: Path,
    folder_name: str,
    class_names: list[str],
    rows: list[dict[str, int]],
    port: int,
) -> None:
    import uno

    ctx = _connect_uno(port)
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    url = uno.systemPathToFileUrl(str(excel_path))
    doc = desktop.loadComponentFromURL(url, "_blank", 0, (_mkprop("Hidden", True),))
    try:
        raw = doc.Sheets.getByName("原始数据")
        report = doc.Sheets.getByName("截面统计报告1")

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
            area_px = int(item.get("area_px", 0))
            raw.getCellByPosition(13, row_idx).setValue(float(max(0, class_id)))
            raw.getCellByPosition(14, row_idx).setValue(float(max(0, area_px)))

        doc.store()
    finally:
        doc.close(True)


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

    port = random.randint(21000, 31000)
    profile_dir = Path(tempfile.mkdtemp(prefix="lo-profile-")).resolve()
    profile_url = f"file://{profile_dir.as_posix()}"
    cmd = [
        "soffice",
        "--headless",
        f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ServiceManager",
        "--norestore",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--nologo",
        f"-env:UserInstallation={profile_url}",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _write_cells(excel_path, folder_name, class_names, rows, port)
        return 0
    except Exception as exc:
        print(f"uno_write_failed:{exc}", file=sys.stderr)
        return 3
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
            proc.wait(timeout=2)
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
