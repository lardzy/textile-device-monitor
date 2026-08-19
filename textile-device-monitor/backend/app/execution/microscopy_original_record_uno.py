"""Isolated LibreOffice/UNO writer for the microscopy .xls template.

This script intentionally depends only on the system Python ``uno`` package.
The execution worker invokes it as a subprocess so the application venv does
not need to mix Debian's pyuno extension with pip's Python installation.
"""

from __future__ import annotations

import json
import os
import re
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
        except Exception as exc:  # pyuno exposes runtime-specific exception types
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError("libreoffice_connection_timeout") from last_error


def _canvas_geometry(sheet, max_width, max_height):
    first = sheet.getCellByPosition(0, 3)  # A4
    origin_x = int(first.Position.X)
    origin_y = int(first.Position.Y)
    width = sum(int(sheet.Columns.getByIndex(index).Width) for index in range(12))
    height = sum(int(sheet.Rows.getByIndex(index).Height) for index in range(3, 32))
    width = min(width, int(max_width))
    height = min(height, int(max_height))
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid_template_canvas")
    return origin_x, origin_y, width, height


def _graphic_shapes(draw_page):
    result = []
    for index in range(draw_page.Count):
        shape = draw_page.getByIndex(index)
        try:
            is_graphic = shape.supportsService(
                "com.sun.star.drawing.GraphicObjectShape"
            )
        except Exception:
            is_graphic = False
        if is_graphic:
            result.append(shape)
    return result


def _set_print_area(sheet, range_name):
    # The approved legacy template contains a worksheet-scoped, ordinary
    # ``Print_Area`` name.  LibreOffice otherwise preserves that name and also
    # emits Excel's built-in print-area name, leaving two conflicting names in
    # the generated .xls file.  Remove only the ordinary worksheet-level name
    # before setting the built-in print area through XPrintAreas.
    named_ranges = sheet.NamedRanges
    for name in tuple(named_ranges.ElementNames):
        if str(name).casefold() == "print_area":
            named_ranges.removeByName(name)
    target = sheet.getCellRangeByName(range_name).RangeAddress
    sheet.setPrintAreas((target,))


def _print_area_matches(sheet, range_name):
    expected = sheet.getCellRangeByName(range_name).RangeAddress
    actual = sheet.getPrintAreas()
    if len(actual) != 1:
        return False
    current = actual[0]
    return (
        int(current.Sheet) == int(expected.Sheet)
        and int(current.StartColumn) == int(expected.StartColumn)
        and int(current.StartRow) == int(expected.StartRow)
        and int(current.EndColumn) == int(expected.EndColumn)
        and int(current.EndRow) == int(expected.EndRow)
    )


def _ordinary_print_area_removed(sheet):
    return all(
        str(name).casefold() != "print_area"
        for name in tuple(sheet.NamedRanges.ElementNames)
    )


def _shape_image_index(shape):
    prefix = "microscopy_image_"
    name = str(getattr(shape, "Name", "") or "")
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix) :])
    except ValueError:
        return None


def _encode_horizontal(value, document_scale, biff_excel_x_scale):
    return round(int(value) * float(document_scale) * float(biff_excel_x_scale))


def _decode_horizontal(value, biff_excel_x_scale):
    return round(int(value) / float(biff_excel_x_scale))


def _write(payload):
    import uno

    pipe_name = "microscopy_%s" % uuid4().hex
    with tempfile.TemporaryDirectory(prefix="microscopy-lo-profile-") as profile:
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
            service_manager = context.ServiceManager
            desktop = service_manager.createInstanceWithContext(
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
            _set_print_area(sheet, payload["print_area"])
            for cell, value in payload["cells"].items():
                sheet.getCellRangeByName(cell).String = str(value or "")
            # Remove the two template placeholders before adding images.
            sheet.getCellRangeByName("A4").String = ""
            sheet.getCellRangeByName("G4").String = ""
            number_formats = payload.get("number_formats") or {}
            if number_formats:
                format_supplier = document.NumberFormats
                locale = uno.createUnoStruct("com.sun.star.lang.Locale")
                for cell_name, spec in number_formats.items():
                    format_code = str(spec["format"])
                    format_key = format_supplier.queryKey(format_code, locale, False)
                    if format_key == -1:
                        format_key = format_supplier.addNew(format_code, locale)
                    sheet.getCellRangeByName(cell_name).NumberFormat = format_key
            canvas = payload["canvas"]
            origin_x, origin_y, width, height = _canvas_geometry(
                sheet, canvas["max_width"], canvas["max_height"]
            )
            scale = min(
                1.0,
                width / float(canvas["max_width"]),
                height / float(canvas["max_height"]),
            )
            offset_x = (width - int(canvas["max_width"] * scale)) // 2
            offset_y = (height - int(canvas["max_height"] * scale)) // 2
            biff_excel_x_scale = float(canvas["biff_excel_x_scale"])
            if biff_excel_x_scale <= 0:
                raise RuntimeError("invalid_biff_excel_x_scale")
            draw_page = sheet.DrawPage
            provider = service_manager.createInstanceWithContext(
                "com.sun.star.graphic.GraphicProvider", context
            )
            for image in payload["images"]:
                graphic = provider.queryGraphic(
                    (_property("URL", _file_url(image["path"])),)
                )
                if graphic is None:
                    raise RuntimeError("image_load_failed")
                shape = document.createInstance(
                    "com.sun.star.drawing.GraphicObjectShape"
                )
                shape.Graphic = graphic
                draw_page.add(shape)
                shape.Name = "microscopy_image_%d" % int(image["index"])
                shape.Anchor = sheet.getCellRangeByName("A4")
                shape.ResizeWithCell = False
                point = uno.createUnoStruct("com.sun.star.awt.Point")
                point.X = (
                    origin_x
                    + offset_x
                    + _encode_horizontal(
                        image["x"], scale, biff_excel_x_scale
                    )
                )
                point.Y = origin_y + offset_y + round(int(image["y"]) * scale)
                size = uno.createUnoStruct("com.sun.star.awt.Size")
                size.Width = max(
                    1,
                    _encode_horizontal(
                        image["width"], scale, biff_excel_x_scale
                    ),
                )
                size.Height = max(1, round(int(image["height"]) * scale))
                shape.Position = point
                # Size must be the last geometry operation. GraphicObjectShape
                # starts at 100x100, and adding/anchoring it may restore that
                # default in some LibreOffice/Excel compatibility paths.
                shape.Size = size
            document.store()
            document.close(True)
            document = None

            # Reopen the saved file and inspect the persisted objects, rather
            # than trusting the in-memory document that performed the write.
            reopened = desktop.loadComponentFromURL(
                workbook_url,
                "_blank",
                0,
                (_property("Hidden", True), _property("ReadOnly", True)),
            )
            if reopened is None:
                raise RuntimeError("workbook_reopen_failed")
            sheet = reopened.Sheets.getByName(payload["sheet_name"])
            print_area_verified = _print_area_matches(
                sheet, payload["print_area"]
            )
            ordinary_print_area_removed = _ordinary_print_area_removed(sheet)
            actual_cells = {
                cell: sheet.getCellRangeByName(cell).String
                for cell in payload["cells"]
            }
            number_format_strings = {}
            number_format_verified = True
            for cell_name, spec in number_formats.items():
                target = sheet.getCellRangeByName(cell_name)
                number_format_strings[cell_name] = reopened.NumberFormats.getByKey(
                    int(target.NumberFormat)
                ).FormatString
                # 行为验证：LibreOffice 可能规范化格式码字面量，因此比较按
                # 持久化格式渲染出的显示串，而不是格式码本身。
                rendered = str(target.String or "")
                pattern = str(spec.get("display_pattern") or "")
                if pattern and re.fullmatch(pattern, rendered) is None:
                    number_format_verified = False
            origin_x, origin_y, width, height = _canvas_geometry(
                sheet, canvas["max_width"], canvas["max_height"]
            )
            logical_scale = min(
                1.0,
                width / float(canvas["max_width"]),
                height / float(canvas["max_height"]),
            )
            logical_offset_x = (
                width - int(canvas["max_width"] * logical_scale)
            ) // 2
            expected_images = {
                int(image["index"]): image for image in payload["images"]
            }
            images = []
            for shape in _graphic_shapes(sheet.DrawPage):
                image_index = _shape_image_index(shape)
                expected = expected_images.get(image_index)
                encoded_x = int(shape.Position.X) - origin_x
                encoded_y = int(shape.Position.Y) - origin_y
                encoded_width = int(shape.Size.Width)
                encoded_height = int(shape.Size.Height)
                logical = {
                    "x": logical_offset_x
                    + _decode_horizontal(
                        encoded_x - logical_offset_x,
                        biff_excel_x_scale,
                    ),
                    "y": encoded_y,
                    "width": max(
                        1,
                        _decode_horizontal(
                            encoded_width, biff_excel_x_scale
                        ),
                    ),
                    "height": encoded_height,
                }
                encoded = {
                    "x": encoded_x,
                    "y": encoded_y,
                    "width": encoded_width,
                    "height": encoded_height,
                }
                images.append(
                    {
                        "index": image_index,
                        "source_id": (
                            str(expected.get("source_id") or "")
                            if expected is not None
                            else None
                        ),
                        # Keep the established top-level fields as logical
                        # coordinates while also making both representations
                        # explicit for audit and Windows Excel reconciliation.
                        **logical,
                        "logical": logical,
                        "encoded": encoded,
                        "resize_with_cell": bool(shape.ResizeWithCell),
                    }
                )
            expected_cells = {
                key: str(value or "") for key, value in payload["cells"].items()
            }
            verified = (
                actual_cells == expected_cells
                and len(images) == len(payload["images"])
                and print_area_verified
                and ordinary_print_area_removed
                and number_format_verified
                and all(image["index"] in expected_images for image in images)
                and len({image["index"] for image in images}) == len(images)
            )
            return {
                "verified": verified,
                "cells": actual_cells,
                "image_count": len(images),
                "images": images,
                "canvas": {
                    "width": width,
                    "height": height,
                    "biff_excel_x_scale": biff_excel_x_scale,
                },
                "print_area": payload["print_area"],
                "print_area_verified": print_area_verified,
                "ordinary_print_area_removed": ordinary_print_area_removed,
                "number_formats": number_format_strings,
                "number_format_verified": number_format_verified,
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
        raise SystemExit("usage: microscopy_original_record_uno.py PAYLOAD.json")
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(_write(payload), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv)
