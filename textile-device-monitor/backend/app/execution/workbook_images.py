"""Shared raster layout, preparation and XLS/UNO rendering algorithms."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Optional, Sequence
from PIL import Image, ImageOps, UnidentifiedImageError
from app.execution.errors import ExecutionApiError
from app.execution.storage import fsync_file

CANVAS_WIDTH = 21600
CANVAS_HEIGHT = 11700
IMAGE_GAP = 0
PERSISTED_GEOMETRY_TOLERANCE_RATIO = 0.01
PERSISTED_ASPECT_RATIO_TOLERANCE = 0.01
PERSISTED_EDGE_TOLERANCE = 10


@dataclass(frozen=True, slots=True)
class ImagePlacement:
    index: int
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return {
            "index": self.index,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }



@dataclass(frozen=True, slots=True)
class PreparedImage:
    source_id: str
    source_path: Path
    prepared_path: Path
    width: int
    height: int

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height



def layout_images(
    aspect_ratios: Sequence[float],
    *,
    canvas_width: int = CANVAS_WIDTH,
    canvas_height: int = CANVAS_HEIGHT,
    gap: int = IMAGE_GAP,
    max_images: int = 100,
) -> list[ImagePlacement]:
    """Lay out a bounded image collection without distortion."""

    count = len(aspect_ratios)
    if not 1 <= count <= max_images:
        raise ValueError("image_count_exceeds_budget")
    if canvas_width <= 0 or canvas_height <= 0 or gap < 0:
        raise ValueError("invalid_canvas")
    ratios = tuple(float(value) for value in aspect_ratios)
    if any(not math.isfinite(value) or value <= 0 for value in ratios):
        raise ValueError("invalid_image_aspect_ratio")

    best_score: Optional[tuple[float, int, int]] = None
    best: list[ImagePlacement] = []
    for columns in range(1, count + 1):
        rows = math.ceil(count / columns)
        cell_width = (canvas_width - gap * (columns - 1)) / columns
        cell_height = (canvas_height - gap * (rows - 1)) / rows
        if cell_width <= 0 or cell_height <= 0:
            continue
        placements: list[ImagePlacement] = []
        total_area = 0.0
        index = 0
        for row in range(rows):
            items_in_row = min(columns, count - index)
            row_width = items_in_row * cell_width + (items_in_row - 1) * gap
            row_start_x = (canvas_width - row_width) / 2
            for column in range(items_in_row):
                ratio = ratios[index]
                width = min(cell_width, cell_height * ratio)
                height = width / ratio
                if height > cell_height:
                    height = cell_height
                    width = height * ratio
                x = row_start_x + column * (cell_width + gap)
                x += (cell_width - width) / 2
                y = row * (cell_height + gap) + (cell_height - height) / 2
                placement = ImagePlacement(
                    index=index,
                    x=max(0, round(x)),
                    y=max(0, round(y)),
                    width=max(1, round(width)),
                    height=max(1, round(height)),
                )
                placements.append(placement)
                total_area += placement.width * placement.height
                index += 1
        # Prefer the largest visible image area. Ties prefer fewer rows and a
        # more compact column count, keeping landscape and portrait sets usable.
        score = (total_area, -rows, -columns)
        if best_score is None or score > best_score:
            best_score = score
            best = placements
    if not best:
        raise ValueError("image_layout_unavailable")
    return best



def _single_image_geometry_matches(
    placement: dict[str, Any],
    *,
    canvas_width: int,
    canvas_height: int,
    expected_aspect_ratio: float,
) -> bool:
    """Verify the persisted single image still uses an undistorted max-contain.

    The limiting axis must touch both opposite canvas edges and the remaining
    axis must stay centred.  A small tolerance is necessary because the legacy
    .xls drawing layer rounds size and anchor coordinates when it is reopened.
    """

    if canvas_width <= 0 or canvas_height <= 0 or expected_aspect_ratio <= 0:
        return False
    x = int(placement.get("x") or 0)
    y = int(placement.get("y") or 0)
    width = int(placement.get("width") or 0)
    height = int(placement.get("height") or 0)
    if width <= 0 or height <= 0:
        return False
    actual_aspect_ratio = width / height
    if not math.isclose(
        actual_aspect_ratio,
        expected_aspect_ratio,
        rel_tol=PERSISTED_ASPECT_RATIO_TOLERANCE,
        abs_tol=0.001,
    ):
        return False

    tolerance = max(
        50,
        round(
            max(canvas_width, canvas_height)
            * PERSISTED_GEOMETRY_TOLERANCE_RATIO
        ),
    )
    fills_width = (
        abs(x) <= tolerance
        and abs((x + width) - canvas_width) <= tolerance
    )
    fills_height = (
        abs(y) <= tolerance
        and abs((y + height) - canvas_height) <= tolerance
    )
    if not fills_width and not fills_height:
        return False
    if fills_width and abs((y + height / 2) - canvas_height / 2) > tolerance:
        return False
    if fills_height and abs((x + width / 2) - canvas_width / 2) > tolerance:
        return False
    return True



def _persisted_images_geometry(
    placements: object,
    *,
    expected_images: Sequence[dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
    planned_width: int = CANVAS_WIDTH,
    planned_height: int = CANVAS_HEIGHT,
) -> dict[str, Any]:
    """Compare every reopened .xls shape with its source and planned geometry."""

    issues: list[dict[str, Any]] = []
    if canvas_width <= 0 or canvas_height <= 0:
        return {
            "verified": False,
            "issues": [{"code": "invalid_canvas"}],
            "non_overlapping": False,
        }
    if not isinstance(placements, list):
        return {
            "verified": False,
            "issues": [{"code": "invalid_persisted_images"}],
            "non_overlapping": False,
        }

    expected_by_index: dict[int, dict[str, Any]] = {}
    for expected in expected_images:
        try:
            image_index = int(expected["index"])
            source_id = str(expected["source_id"])
            aspect_ratio = float(expected["aspect_ratio"])
        except (KeyError, TypeError, ValueError):
            issues.append({"code": "invalid_expected_image"})
            continue
        if (
            image_index in expected_by_index
            or not source_id
            or not math.isfinite(aspect_ratio)
            or aspect_ratio <= 0
        ):
            issues.append(
                {"code": "invalid_expected_image", "index": image_index}
            )
            continue
        expected_by_index[image_index] = expected

    actual_by_index: dict[int, dict[str, Any]] = {}
    for placement in placements:
        if not isinstance(placement, dict):
            issues.append({"code": "invalid_persisted_image"})
            continue
        try:
            image_index = int(placement["index"])
        except (KeyError, TypeError, ValueError):
            issues.append({"code": "persisted_image_index_missing"})
            continue
        if image_index in actual_by_index:
            issues.append(
                {"code": "persisted_image_index_duplicate", "index": image_index}
            )
            continue
        actual_by_index[image_index] = placement

    if set(expected_by_index) != set(actual_by_index):
        issues.append(
            {
                "code": "persisted_image_set_mismatch",
                "expected": sorted(expected_by_index),
                "actual": sorted(actual_by_index),
            }
        )

    scale = min(
        1.0,
        canvas_width / float(planned_width),
        canvas_height / float(planned_height),
    )
    offset_x = (canvas_width - int(planned_width * scale)) // 2
    offset_y = (canvas_height - int(planned_height * scale)) // 2
    geometry_tolerance = max(
        50,
        round(
            max(canvas_width, canvas_height)
            * PERSISTED_GEOMETRY_TOLERANCE_RATIO
        ),
    )
    normalized_actual: list[dict[str, Any]] = []
    for image_index in sorted(set(expected_by_index) & set(actual_by_index)):
        expected = expected_by_index[image_index]
        actual = actual_by_index[image_index]
        source_id = str(expected["source_id"])
        actual_source_id = str(actual.get("source_id") or "")
        if actual_source_id != source_id:
            issues.append(
                {
                    "code": "persisted_image_source_mismatch",
                    "index": image_index,
                }
            )
        logical = actual.get("logical")
        if not isinstance(logical, dict):
            issues.append(
                {"code": "persisted_image_logical_geometry_missing", "index": image_index}
            )
            continue
        try:
            x = int(logical["x"])
            y = int(logical["y"])
            width = int(logical["width"])
            height = int(logical["height"])
            expected_x = offset_x + round(int(expected["x"]) * scale)
            expected_y = offset_y + round(int(expected["y"]) * scale)
            expected_width = max(1, round(int(expected["width"]) * scale))
            expected_height = max(1, round(int(expected["height"]) * scale))
            expected_aspect_ratio = float(expected["aspect_ratio"])
        except (KeyError, TypeError, ValueError):
            issues.append(
                {"code": "persisted_image_geometry_invalid", "index": image_index}
            )
            continue
        normalized_actual.append(
            {
                "index": image_index,
                "source_id": actual_source_id,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
        if (
            width <= 0
            or height <= 0
            or x < 0
            or y < 0
            or x + width > canvas_width + 2
            or y + height > canvas_height + 2
        ):
            issues.append(
                {"code": "persisted_image_out_of_bounds", "index": image_index}
            )
            continue
        if actual.get("resize_with_cell") is not False:
            issues.append(
                {"code": "persisted_image_resizes_with_cell", "index": image_index}
            )
        if not math.isclose(
            width / height,
            expected_aspect_ratio,
            rel_tol=PERSISTED_ASPECT_RATIO_TOLERANCE,
            abs_tol=0.001,
        ):
            issues.append(
                {
                    "code": "persisted_image_aspect_ratio_mismatch",
                    "index": image_index,
                    "expected": expected_aspect_ratio,
                    "actual": width / height,
                }
            )
        actual_geometry = (x, y, width, height)
        expected_geometry = (
            expected_x,
            expected_y,
            expected_width,
            expected_height,
        )
        if any(
            abs(actual_value - expected_value) > geometry_tolerance
            for actual_value, expected_value in zip(
                actual_geometry, expected_geometry
            )
        ):
            issues.append(
                {
                    "code": "persisted_image_geometry_mismatch",
                    "index": image_index,
                    "expected": expected_geometry,
                    "actual": actual_geometry,
                    "tolerance": geometry_tolerance,
                }
            )

    non_overlapping = True
    for left_position, left in enumerate(normalized_actual):
        for right in normalized_actual[left_position + 1 :]:
            overlap_width = min(
                left["x"] + left["width"], right["x"] + right["width"]
            ) - max(left["x"], right["x"])
            overlap_height = min(
                left["y"] + left["height"], right["y"] + right["height"]
            ) - max(left["y"], right["y"])
            if (
                overlap_width > PERSISTED_EDGE_TOLERANCE
                and overlap_height > PERSISTED_EDGE_TOLERANCE
            ):
                non_overlapping = False
                issues.append(
                    {
                        "code": "persisted_images_overlap",
                        "left_index": left["index"],
                        "right_index": right["index"],
                    }
                )

    return {
        "verified": not issues,
        "issues": issues,
        "non_overlapping": non_overlapping,
        "geometry_tolerance": geometry_tolerance,
        "scale": scale,
    }



def _prepare_images(
    images: Sequence[tuple[str, Path]],
    directory: Path,
) -> list[PreparedImage]:
    prepared: list[PreparedImage] = []
    Image.MAX_IMAGE_PIXELS = 100_000_000
    for index, (image_id, path) in enumerate(images):
        target = directory / f"image-{index + 1:02d}.png"
        try:
            with Image.open(path) as source:
                source.load()
                converted = ImageOps.exif_transpose(source)
                if converted.mode not in {"RGB", "RGBA"}:
                    converted = converted.convert("RGB")
                converted.save(target, format="PNG", optimize=False)
                width, height = converted.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ExecutionApiError(
                422,
                "selected_image_invalid",
                "所选图片无法读取或格式不受支持",
                details={"image_id": image_id},
            ) from exc
        if width <= 0 or height <= 0:
            raise ExecutionApiError(
                422,
                "selected_image_invalid",
                "所选图片尺寸无效",
                details={"image_id": image_id},
            )
        prepared.append(
            PreparedImage(
                source_id=image_id,
                source_path=path,
                prepared_path=target,
                width=width,
                height=height,
            )
        )
    return prepared



def _default_uno_python() -> str:
    configured = os.getenv("EXECUTION_UNO_PYTHON", "").strip()
    if configured:
        return configured
    system_python = Path("/usr/bin/python3")
    return str(system_python) if system_python.exists() else sys.executable



def _run_uno_writer(payload_path: Path) -> dict[str, Any]:
    script = Path(__file__).with_name("microscopy_original_record_uno.py")
    try:
        completed = subprocess.run(
            [_default_uno_python(), str(script), str(payload_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=150,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionApiError(
            503,
            "libreoffice_unavailable",
            "原始记录生成服务当前不可用，请稍后重试",
        ) from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise ExecutionApiError(
            503,
            "libreoffice_generation_failed",
            "LibreOffice 未能生成原始记录",
            details={"runtime_message": message},
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ExecutionApiError(
            503,
            "libreoffice_result_invalid",
            "原始记录生成服务未返回有效核对结果",
        ) from exc
    if not isinstance(result, dict) or not result.get("verified"):
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "生成后的原始记录未通过重读核对",
            details={"verification": result if isinstance(result, dict) else {}},
        )
    return result


def render_image_workbook(template, target, *, cells, selected, layout, number_formats=None):
    """All template, field and layout choices are supplied by the workflow."""
    with TemporaryDirectory(prefix="workbook-images-", dir=target.parent) as directory:
        temporary = Path(directory)
        working = temporary / 'working.xls'
        shutil.copyfile(template, working)
        prepared = _prepare_images(selected, temporary)
        placements = layout_images([item.aspect_ratio for item in prepared],
            canvas_width=layout['max_width'], canvas_height=layout['max_height'], gap=layout.get('gap', 0))
        images = [{'path': str(item.prepared_path), 'source_id': item.source_id,
                   'aspect_ratio': item.aspect_ratio, **placement.as_dict()}
                  for item, placement in zip(prepared, placements)]
        payload = {'workbook_path': str(working), 'sheet_name': layout['sheet'],
                   'print_area': layout['print_area'], 'cells': cells,
                   'number_formats': number_formats or {}, 'images': images,
                   'canvas': {'range': layout['range'], 'max_width': layout['max_width'],
                              'max_height': layout['max_height'], 'biff_excel_x_scale': layout.get('biff_excel_x_scale', 1.0)}}
        path = temporary / 'payload.json'
        path.write_text(json.dumps(payload, ensure_ascii=False))
        result = _run_uno_writer(path)
        canvas = result['canvas']
        geometry = _persisted_images_geometry(result.get('images'), expected_images=images,
            canvas_width=canvas['width'], canvas_height=canvas['height'],
            planned_width=layout['max_width'], planned_height=layout['max_height'])
        if (not geometry['verified'] or result.get('image_count') != len(images)
                or not result.get('print_area_verified') or not result.get('number_format_verified')):
            raise ExecutionApiError(422, 'render_verification_failed', '图片布局、打印范围或格式重读不一致', details={'geometry': geometry})
        os.replace(working, target)
        fsync_file(target)
        return {'cells_verified': len(cells), 'images_written': len(images),
                'geometry': geometry, 'print_area': layout['print_area'], 'uno': result}
