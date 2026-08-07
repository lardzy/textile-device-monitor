from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import xlrd
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.electron_microscopy import (
    ELECTRON_IMAGE_SUFFIXES,
    ELECTRON_PROJECT_NAME_ALIASES,
    ELECTRON_ROOT_ID,
    ELECTRON_TEST_METHOD,
    cached_task_snapshot,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionArtifact,
    ExecutionFileIndexEntry,
    ExecutionStorageRoot,
)
from app.execution.persistence import build_file_gateway, storage_root_by_key
from app.execution.registry import node_registry
from app.execution.storage import (
    ArtifactRef,
    StorageError,
    fingerprint_file,
    fsync_file,
)


MICROSCOPY_ORIGINAL_RECORD_NODE_TYPE = "workbook.microscopy_original_record"
MICROSCOPY_RECORD_CONTEXT_NODE_TYPE = "data.microscopy_record_context"
MICROSCOPY_TEMPLATE_VERSION = "gbt36422-2018-microscopy-original-record-v1"
MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME = (
    "39-8B-纤维形状截面定量试验-2026.xls"
)
MICROSCOPY_TEMPLATE_SHA256 = (
    "d2b70e114cb85c89e4961ba2acb1b0cb451656f7a12b5dd5bdbacba1d50f88ba"
)
MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION = (
    "gbt36422-2018-legacy-template-binding-v1"
)
MICROSCOPY_LEGACY_TEMPLATE_BINDINGS: dict[int, dict[str, Any]] = {
    1: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 1,
        "legacy_template_name": "微观形貌.xls",
        "local_asset_name": "gbt36422-2018-microscopy-1-image-v1.xls",
        "local_asset_sha256": (
            "b169fb5cf236004058f0666ee28979836266168311172efc01c7ec7d5906c6fe"
        ),
        "mapping_config_sha256": (
            "a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e"
        ),
    },
    2: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 2,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-2张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-2-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23"
        ),
    },
    3: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 3,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-3张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-3-images-v1.xls",
        "local_asset_sha256": (
            "4a4e7b69a5dba7684fe837779929b4d093fe509cbf398ae37114cd22071b70b4"
        ),
        "mapping_config_sha256": (
            "43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268"
        ),
    },
    5: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 5,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-5张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-5-images-v1.xls",
        "local_asset_sha256": (
            "3b148fd8ccbb28b8fe8e60ceee0e4898c144c1fbe15b8a451ed44e0d2141d38d"
        ),
        "mapping_config_sha256": (
            "d21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d"
        ),
    },
    6: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 6,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-6张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-6-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304"
        ),
    },
    7: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 7,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-7张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-7-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01"
        ),
    },
    10: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 10,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-10张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-10-images-v1.xls",
        "local_asset_sha256": (
            "85627e8ca9824274fe61f13276b390a75288a119987053e3a244157ee0f46e8c"
        ),
        "mapping_config_sha256": (
            "976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74"
        ),
    },
}
MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS = tuple(
    sorted(MICROSCOPY_LEGACY_TEMPLATE_BINDINGS)
)
MICROSCOPY_SHEET_NAME = "微观形貌"
MICROSCOPY_PRINT_AREA = "$A$1:$L$37"
MICROSCOPY_MEDIA_TYPE = "application/vnd.ms-excel"
STAGING_ROOT_ID = "execution_staging"
MAX_SELECTED_IMAGES = 10

# LibreOffice uses 1/100 mm for drawing coordinates. These limits are the
# measured printable image canvas starting at the top-left corner of A4 in the
# approved template (Excel measurement: 21.6 cm x 11.7 cm).
CANVAS_WIDTH = 21_600
CANVAS_HEIGHT = 11_700
# Images are packed edge to edge; no gap is required between neighbours.
IMAGE_GAP = 0
# Excel interprets the horizontal ClientAnchor units written by LibreOffice's
# legacy BIFF8 exporter through the column widths LibreOffice computes from
# the workbook default font.  The factor stays 1.0 as long as the runtime
# provides SimSun (宋体): LibreOffice and Excel then derive identical column
# widths.  Without SimSun, LibreOffice substitutes DejaVu metrics and Excel
# renders shapes 1.2745x narrower than LibreOffice encodes them; the previous
# compensation factor is kept here for reference.  The UNO receipt decodes
# x/width back to logical canvas coordinates for checks.
MICROSCOPY_BIFF_EXCEL_X_SCALE = 1.0
# LibreOffice's legacy .xls writer rounds drawing coordinates in 1/100 mm and
# may move an anchored shape by a few units after reopening.  One percent of
# the image canvas is a deliberately small, format-aware acceptance window.
PERSISTED_GEOMETRY_TOLERANCE_RATIO = 0.01
PERSISTED_ASPECT_RATIO_TOLERANCE = 0.01
OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_MULTI_VALUE_SEPARATOR = re.compile(r"[，,、]+")
_BASIS_SEPARATOR = re.compile(r"[\r\n；;，,、]+")
_FALLBACK_NAME_SEPARATOR = re.compile(
    r"[\s,，、;；:/\\|（）()\[\]【】{}<>《》]+"
)
_ASCII_HYPHENATED = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+")
_SAFE_INSPECTION_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


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


def _normalized_text(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )


def _stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_text(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def split_multi_value_options(value: object) -> list[str]:
    """Split sample-identification choices without losing stable ordering."""

    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return []
    return _stable_unique(_MULTI_VALUE_SEPARATOR.split(raw))


def split_judgement_basis_options(value: object) -> list[str]:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return []
    return _stable_unique(_BASIS_SEPARATOR.split(raw))


def _fallback_sample_name_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for part in _FALLBACK_NAME_SEPARATOR.split(value):
        part = _normalized_text(part)
        if not part:
            continue
        tokens.append(part)
        # Product names such as "Surgicel-Fibrillar" are common. Keep the
        # exact source string and additionally expose its meaningful pieces.
        if _ASCII_HYPHENATED.fullmatch(part):
            tokens.extend(value for value in re.split(r"[-_]", part) if value)
    return _stable_unique(tokens)


def sample_name_v1_candidates(
    value: object,
    *,
    jieba_cut: Optional[Callable[[str], Iterable[str]]] = None,
) -> dict[str, Any]:
    """Return auditable sample-name candidates with a deterministic fallback.

    The full task-system value is always the first candidate.  A pinned local
    jieba installation is preferred for Chinese token suggestions, but its
    absence never changes the availability of the workflow.
    """

    if isinstance(value, (list, tuple)):
        phrases = _stable_unique(str(item or "") for item in value)
    else:
        phrases = split_multi_value_options(value)
    if not phrases:
        return {
            "version": "sample-name-v1",
            "source": "empty",
            "original": "",
            "candidates": [],
            "selection_required": False,
            "automatic_value": None,
        }

    source = "fallback"
    if jieba_cut is None:
        try:
            import jieba  # type: ignore

            jieba_cut = lambda text: jieba.cut(text, cut_all=False)
        except ImportError:
            jieba_cut = None

    suggested: list[str] = []
    if jieba_cut is not None:
        try:
            suggested = _stable_unique(
                token for phrase in phrases for token in jieba_cut(phrase)
            )
            source = "jieba"
        except Exception:
            # A corrupt optional dictionary must not block record generation.
            suggested = []
            source = "fallback"
    fallback = _stable_unique(
        token for phrase in phrases for token in _fallback_sample_name_tokens(phrase)
    )
    # Comma-separated task names represent business choices. Keep those full
    # phrases first, then append segmentation suggestions for the user.
    candidates = _stable_unique([*phrases, *suggested, *fallback])
    return {
        "version": "sample-name-v1",
        "source": source,
        "original": "，".join(phrases),
        "candidates": candidates,
        "selection_required": len(candidates) > 1,
        "automatic_value": candidates[0] if len(candidates) == 1 else None,
    }


def _truthy_judgement_flag(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    normalized = _normalized_text(value).casefold()
    return normalized not in {"", "0", "false", "no", "none", "否", "不判"}


def _matching_projects(
    task_snapshot: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    aliases = {_normalized_text(value) for value in ELECTRON_PROJECT_NAME_ALIASES}
    matches: list[dict[str, Any]] = []
    for raw_project in (task_snapshot or {}).get("projects") or []:
        if not isinstance(raw_project, dict):
            continue
        project = dict(raw_project)
        if (
            _normalized_text(project.get("check_item_name")) in aliases
            and
            _normalized_text(project.get("check_method"))
            == _normalized_text(ELECTRON_TEST_METHOD)
        ):
            matches.append(project)
    return matches


def _relevant_project(task_snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
    matches = _matching_projects(task_snapshot)
    return matches[0] if matches else {}


def prepare_original_record_choices(
    task_snapshot: Optional[dict[str, Any]],
    *,
    jieba_cut: Optional[Callable[[str], Iterable[str]]] = None,
) -> dict[str, Any]:
    """Build the human-form choices without accessing Oracle or shared files."""

    snapshot = task_snapshot or {}
    project = _relevant_project(snapshot)
    raw_sample_names = snapshot.get("sample_names")
    if not raw_sample_names:
        raw_sample_names = snapshot.get("sample_name")
    sample_name = sample_name_v1_candidates(raw_sample_names, jieba_cut=jieba_cut)
    identifications = split_multi_value_options(project.get("sample_identify"))
    judgement_required = _truthy_judgement_flag(project.get("give_judgement"))
    basis_options = split_judgement_basis_options(snapshot.get("check_basis"))
    return {
        "sample_name": sample_name,
        "sample_identification": {
            "options": identifications,
            "selection_required": len(identifications) > 1,
            "automatic_value": (
                identifications[0] if len(identifications) == 1 else None
            ),
        },
        "judgement_required": judgement_required,
        "judgement_basis": {
            "options": basis_options if judgement_required else [],
            "selection_required": judgement_required and len(basis_options) > 1,
            "automatic_value": (
                basis_options[0]
                if judgement_required and len(basis_options) == 1
                else None
            ),
        },
        "project": project,
    }


def _current_task_snapshot(context, input_data: dict[str, Any]) -> dict[str, Any]:
    """Resolve a task snapshot for both current and already-running definitions."""

    task_snapshot = input_data.get("task")
    if isinstance(task_snapshot, dict) and task_snapshot:
        return task_snapshot
    cached = cached_task_snapshot(
        context.db,
        inspection_number=(
            input_data.get("inspection_number") or context.run.inspection_number
        ),
    )
    task_snapshot = cached.get("snapshot")
    if not isinstance(task_snapshot, dict) or not task_snapshot:
        raise ExecutionApiError(
            409,
            "microscopy_task_snapshot_not_ready",
            "旧系统任务信息尚未读取完成，请检查 Windows 只读读取服务后重试",
            details={"cache_state": cached.get("cache_state") or "pending"},
        )
    return task_snapshot


def _microscopy_record_context_executor(context) -> dict[str, Any]:
    """Turn cached task facts and image-selection output into a UI contract."""

    input_data = context.input_data or {}
    inspection_number = _safe_inspection_number(
        input_data.get("inspection_number") or context.run.inspection_number
    )
    task_snapshot = _current_task_snapshot(context, input_data)
    choices = prepare_original_record_choices(task_snapshot)
    selected_ids = input_data.get("selected_image_ids")
    if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 10:
        raise ExecutionApiError(
            422, "selected_images_required", "请选择 1 至 10 张图片"
        )
    selected_ids = list(
        dict.fromkeys(str(value) for value in selected_ids if value)
    )
    if not 1 <= len(selected_ids) <= 10:
        raise ExecutionApiError(
            422, "selected_images_required", "请选择 1 至 10 张图片"
        )
    template_binding = resolve_microscopy_legacy_template_binding(
        len(selected_ids)
    )
    projects = []
    for project in _matching_projects(task_snapshot):
        normalized_project = dict(project)
        # Keep legacy field names and add the frontend's established alias.
        normalized_project.setdefault(
            "test_method", normalized_project.get("check_method")
        )
        projects.append(normalized_project)
    if not projects:
        raise ExecutionApiError(
            422,
            "microscopy_task_project_not_found",
            "任务单中没有纤维微观形貌 GB/T 36422-2018 检测项目",
        )
    return {
        "task_kind": "microscopy_record_input",
        "inspection_number": inspection_number,
        "selected_image_ids": selected_ids,
        "selected_images": input_data.get("selected_images") or [],
        "template_binding": template_binding,
        "projects": projects,
        "sample_name_analysis": choices["sample_name"],
        "sample_identification_options": choices["sample_identification"][
            "options"
        ],
        "sample_identification_selection_required": choices[
            "sample_identification"
        ]["selection_required"],
        "sample_identification_automatic_value": choices[
            "sample_identification"
        ]["automatic_value"],
        "check_basis_options": choices["judgement_basis"]["options"],
        "check_basis_selection_required": choices["judgement_basis"][
            "selection_required"
        ],
        "check_basis_automatic_value": choices["judgement_basis"][
            "automatic_value"
        ],
        "judgement_required": choices["judgement_required"],
    }


def layout_images(
    aspect_ratios: Sequence[float],
    *,
    canvas_width: int = CANVAS_WIDTH,
    canvas_height: int = CANVAS_HEIGHT,
    gap: int = IMAGE_GAP,
) -> list[ImagePlacement]:
    """Lay out 1-10 images as a centered adaptive grid without distortion."""

    count = len(aspect_ratios)
    if not 1 <= count <= MAX_SELECTED_IMAGES:
        raise ValueError("image_count_must_be_between_1_and_10")
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
        canvas_width / float(CANVAS_WIDTH),
        canvas_height / float(CANVAS_HEIGHT),
    )
    offset_x = (canvas_width - int(CANVAS_WIDTH * scale)) // 2
    offset_y = (canvas_height - int(CANVAS_HEIGHT * scale)) // 2
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
            if overlap_width > 2 and overlap_height > 2:
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


def _template_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "templates"
        / f"{MICROSCOPY_TEMPLATE_VERSION}.xls"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_template_asset_path(binding: dict[str, Any]) -> Path:
    return Path(__file__).resolve().parent / "templates" / str(
        binding["local_asset_name"]
    )


def resolve_microscopy_legacy_template_binding(
    image_count: object,
    *,
    declared_binding: object = None,
) -> dict[str, Any]:
    """Resolve and verify the old-system template selected by image count.

    These assets describe which Sheet1 template the old system must select
    during final registration.  They are deliberately separate from
    ``_template_path()``, which remains the source for the generated printable
    original record.
    """

    if isinstance(image_count, bool) or not isinstance(image_count, int):
        binding = None
    else:
        binding = MICROSCOPY_LEGACY_TEMPLATE_BINDINGS.get(image_count)
    if binding is None:
        raise ExecutionApiError(
            422,
            "microscopy_template_image_count_unsupported",
            "旧系统没有与当前选图数量对应的微观形貌 Excel 模板",
            details={
                "image_count": image_count,
                "supported_image_counts": list(
                    MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS
                ),
            },
        )

    resolved = dict(binding)
    asset_path = _legacy_template_asset_path(resolved)
    if (
        not asset_path.is_file()
        or _sha256(asset_path) != resolved["local_asset_sha256"]
    ):
        raise ExecutionApiError(
            503,
            "microscopy_legacy_template_asset_invalid",
            "旧系统微观形貌模板资产缺失或版本校验失败",
            details={
                "image_count": image_count,
                "local_asset_name": resolved["local_asset_name"],
            },
        )

    if declared_binding is not None:
        mismatch = not isinstance(declared_binding, dict) or any(
            declared_binding.get(key) != expected
            for key, expected in resolved.items()
        )
        if mismatch:
            raise ExecutionApiError(
                409,
                "microscopy_template_binding_mismatch",
                "微观形貌模板绑定已变化，请刷新流程后重试",
                details={
                    "image_count": image_count,
                    "expected_binding": resolved,
                },
            )
    return resolved


def _safe_inspection_number(value: object) -> str:
    normalized = _normalized_text(value)
    if not _SAFE_INSPECTION_NUMBER.fullmatch(normalized):
        raise ExecutionApiError(
            422,
            "inspection_number_invalid",
            "检验编号格式不合法",
        )
    return normalized


def _cell_payload(
    *,
    inspection_number: str,
    sample_name: object,
    sample_identification: object,
    judgement_required: bool,
    judgement_basis: object,
    judgement: object,
) -> dict[str, str]:
    name = _normalized_text(sample_name)
    if not name:
        raise ExecutionApiError(422, "sample_name_required", "请选择样品名称")
    identification = _normalized_text(sample_identification)
    basis = _normalized_text(judgement_basis)
    decision = _normalized_text(judgement)
    if judgement_required and not decision:
        raise ExecutionApiError(422, "judgement_required", "请选择判定结果")
    return {
        "B2": inspection_number,
        "B3": name,
        "L3": identification,
        "B33": basis if judgement_required else "",
        # I33=指标要求、B34=测试结果、B35=备注 are deliberately blank in v1.
        "I33": "",
        "B34": "",
        "I34": decision if judgement_required else "",
        "B35": "",
    }


def _resolve_selected_images(
    db: Session,
    *,
    selected_image_ids: object,
    offered_images: object,
) -> list[tuple[ExecutionFileIndexEntry, Path]]:
    if not isinstance(selected_image_ids, list):
        raise ExecutionApiError(
            422, "selected_images_required", "请选择 1 至 10 张图片"
        )
    ids = list(dict.fromkeys(str(value) for value in selected_image_ids if value))
    if not 1 <= len(ids) <= MAX_SELECTED_IMAGES:
        raise ExecutionApiError(
            422, "image_selection_count_invalid", "请选择 1 至 10 张图片"
        )
    offered_by_id = {
        str(item.get("id")): item
        for item in (offered_images if isinstance(offered_images, list) else [])
        if isinstance(item, dict) and item.get("id")
    }
    rows = (
        db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionFileIndexEntry.storage_root_id,
        )
        .filter(ExecutionFileIndexEntry.id.in_(ids))
        .all()
    )
    rows_by_id = {entry.id: (entry, root) for entry, root in rows}
    gateway = build_file_gateway(db)
    resolved: list[tuple[ExecutionFileIndexEntry, Path]] = []
    for image_id in ids:
        row = rows_by_id.get(image_id)
        candidate = offered_by_id.get(image_id)
        if row is None:
            raise ExecutionApiError(
                409,
                "image_candidate_stale",
                "所选图片已不在索引中，请重新选择",
                details={"image_id": image_id},
            )
        entry, root = row
        if (
            entry.missing_since is not None
            or root.root_id != ELECTRON_ROOT_ID
            or entry.extension.casefold() not in ELECTRON_IMAGE_SUFFIXES
        ):
            raise ExecutionApiError(
                409,
                "image_candidate_stale",
                "所选图片已变化或不属于电镜数据根",
                details={"image_id": image_id},
            )
        if candidate is not None and (
            candidate.get("root_id") != root.root_id
            or candidate.get("relative_path") != entry.relative_path
            or candidate.get("fingerprint") != entry.fingerprint
        ):
            raise ExecutionApiError(
                409,
                "image_candidate_stale",
                "所选图片信息已变化，请重新选择",
                details={"image_id": image_id},
            )
        try:
            path = gateway.resolve(
                ArtifactRef(root.root_id, entry.relative_path),
                expected_type="file",
            )
            stat = path.stat()
        except (StorageError, OSError) as exc:
            raise ExecutionApiError(
                409,
                "image_candidate_stale",
                "所选图片当前不可读取，请重新选择",
                details={"image_id": image_id},
            ) from exc
        if f"{stat.st_size}:{stat.st_mtime_ns}" != entry.fingerprint:
            raise ExecutionApiError(
                409,
                "image_candidate_stale",
                "所选图片已发生变化，请重新选择",
                details={"image_id": image_id},
            )
        resolved.append((entry, path))
    return resolved


def _prepare_images(
    images: Sequence[tuple[ExecutionFileIndexEntry, Path]],
    directory: Path,
) -> list[PreparedImage]:
    prepared: list[PreparedImage] = []
    Image.MAX_IMAGE_PIXELS = 100_000_000
    for index, (entry, path) in enumerate(images):
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
                details={"image_id": entry.id},
            ) from exc
        if width <= 0 or height <= 0:
            raise ExecutionApiError(
                422,
                "selected_image_invalid",
                "所选图片尺寸无效",
                details={"image_id": entry.id},
            )
        prepared.append(
            PreparedImage(
                source_id=entry.id,
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


def _verify_generated_workbook(
    path: Path,
    *,
    expected_cells: dict[str, str],
    expected_images: Sequence[dict[str, Any]],
    uno_result: dict[str, Any],
) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(len(OLE_COMPOUND_FILE_MAGIC))
    except OSError as exc:
        raise ExecutionApiError(
            500, "workbook_verification_failed", "生成的原始记录不可读取"
        ) from exc
    if size <= len(OLE_COMPOUND_FILE_MAGIC) or header != OLE_COMPOUND_FILE_MAGIC:
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "生成的原始记录不是有效的 Excel 97-2003 文件",
        )
    actual_cells: dict[str, str] = {}
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_SHEET_NAME)
        for cell, expected in expected_cells.items():
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
            if match is None:
                raise ValueError("invalid_expected_cell")
            column = 0
            for character in match.group(1):
                column = column * 26 + ord(character) - ord("A") + 1
            actual_cells[cell] = _normalized_text(
                sheet.cell_value(int(match.group(2)) - 1, column - 1)
            )
    except (OSError, xlrd.XLRDError, IndexError, ValueError) as exc:
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "无法重读生成后的原始记录",
        ) from exc
    finally:
        try:
            workbook.release_resources()
        except (NameError, AttributeError):
            pass

    mismatches = {
        cell: {"expected": expected, "actual": actual_cells.get(cell)}
        for cell, expected in expected_cells.items()
        if _normalized_text(expected) != actual_cells.get(cell)
    }
    placements = uno_result.get("images")
    canvas = uno_result.get("canvas")
    if (
        mismatches
        or uno_result.get("image_count") != len(expected_images)
        or not isinstance(placements, list)
        or len(placements) != len(expected_images)
        or not isinstance(canvas, dict)
        or uno_result.get("print_area_verified") is not True
        or uno_result.get("ordinary_print_area_removed") is not True
        or not math.isclose(
            float(canvas.get("biff_excel_x_scale") or 0),
            MICROSCOPY_BIFF_EXCEL_X_SCALE,
            rel_tol=0,
            abs_tol=1e-9,
        )
    ):
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "生成后的原始记录内容与预期不一致",
            details={"cell_mismatches": mismatches},
        )
    canvas_width = int(canvas.get("width") or 0)
    canvas_height = int(canvas.get("height") or 0)
    geometry = _persisted_images_geometry(
        placements,
        expected_images=expected_images,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    if not geometry["verified"]:
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "生成后的图片比例、尺寸或位置与预期不一致",
            details={"geometry": geometry},
        )
    single_image_max_fill_verified = len(expected_images) != 1
    if len(expected_images) == 1:
        single_image_max_fill_verified = _single_image_geometry_matches(
            placements[0],
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            expected_aspect_ratio=float(expected_images[0]["aspect_ratio"]),
        )
        if not single_image_max_fill_verified:
            raise ExecutionApiError(
                500,
                "workbook_verification_failed",
                "生成后的单张图片未保持比例填满模板图片区域",
                details={
                    "canvas": canvas,
                    "image": placements[0],
                    "expected_aspect_ratio": float(
                        expected_images[0]["aspect_ratio"]
                    ),
                },
            )
    return {
        "verified": True,
        "sheet_name": MICROSCOPY_SHEET_NAME,
        "checked_cells": sorted(expected_cells),
        "image_count": len(expected_images),
        "images": placements,
        "canvas": canvas,
        "print_area": MICROSCOPY_PRINT_AREA,
        "print_area_verified": True,
        "ordinary_print_area_removed": True,
        "all_images_geometry_verified": True,
        "multi_image_non_overlap_verified": geometry["non_overlapping"],
        "single_image_max_fill_verified": single_image_max_fill_verified,
        "ole_header": True,
        "size_bytes": size,
    }


def _request_digest(
    *,
    inspection_number: str,
    cells: dict[str, str],
    images: Sequence[tuple[ExecutionFileIndexEntry, Path]],
    template_binding: dict[str, Any],
) -> str:
    payload = {
        "template_version": MICROSCOPY_TEMPLATE_VERSION,
        "template_binding": template_binding,
        "biff_excel_x_scale": MICROSCOPY_BIFF_EXCEL_X_SCALE,
        "inspection_number": inspection_number,
        "cells": cells,
        "images": [
            {"id": entry.id, "fingerprint": entry.fingerprint}
            for entry, _path in images
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _artifact_output(artifact: ExecutionArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "root_id": STAGING_ROOT_ID,
        "relative_path": artifact.relative_path,
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "content_sha256": artifact.content_sha256,
        "role": artifact.role,
        "download_url": f"/api/execution/v1/artifacts/{artifact.id}/download",
    }


def _existing_artifact_output(
    db: Session,
    *,
    run_id: str,
    node_run_id: str,
    relative_path: str,
    request_digest: str,
) -> Optional[ExecutionArtifact]:
    root = storage_root_by_key(db, STAGING_ROOT_ID)
    artifact = (
        db.query(ExecutionArtifact)
        .filter(
            ExecutionArtifact.run_id == run_id,
            ExecutionArtifact.node_run_id == node_run_id,
            ExecutionArtifact.storage_root_id == root.id,
            ExecutionArtifact.relative_path == relative_path,
            ExecutionArtifact.role == "working",
        )
        .order_by(ExecutionArtifact.created_at.desc())
        .first()
    )
    if artifact is None or (artifact.metadata_json or {}).get(
        "request_digest"
    ) != request_digest:
        return None
    gateway = build_file_gateway(db)
    try:
        path = gateway.resolve(
            ArtifactRef(STAGING_ROOT_ID, relative_path), expected_type="file"
        )
        current = fingerprint_file(path)
    except (StorageError, OSError):
        return None
    if current.sha256 != artifact.content_sha256 or current.size != artifact.size_bytes:
        return None
    return artifact


def _microscopy_original_record_executor(context) -> dict[str, Any]:
    input_data = context.input_data or {}
    inspection_number = _safe_inspection_number(
        input_data.get("inspection_number") or context.run.inspection_number
    )
    task_snapshot = _current_task_snapshot(context, input_data)
    choices = prepare_original_record_choices(task_snapshot)
    submitted_judgement_flag = input_data.get("judgement_required")
    judgement_required = (
        choices["judgement_required"]
        if submitted_judgement_flag is None
        else _truthy_judgement_flag(submitted_judgement_flag)
    )
    cells = _cell_payload(
        inspection_number=inspection_number,
        sample_name=input_data.get("sample_name"),
        sample_identification=input_data.get(
            "sample_identification", input_data.get("sample_identity")
        ),
        judgement_required=judgement_required,
        judgement_basis=input_data.get(
            "judgement_basis", input_data.get("judge_basis")
        ),
        judgement=input_data.get("judgement"),
    )
    selected = _resolve_selected_images(
        context.db,
        selected_image_ids=input_data.get("selected_image_ids"),
        offered_images=input_data.get("selected_images"),
    )
    template_binding = resolve_microscopy_legacy_template_binding(
        len(selected),
        declared_binding=input_data.get("template_binding"),
    )
    request_digest = _request_digest(
        inspection_number=inspection_number,
        cells=cells,
        images=selected,
        template_binding=template_binding,
    )
    filename = f"{inspection_number}-{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}"
    relative_path = (
        f"original-records/{context.run.id}/{context.node_run.id}/{filename}"
    )
    existing = _existing_artifact_output(
        context.db,
        run_id=context.run.id,
        node_run_id=context.node_run.id,
        relative_path=relative_path,
        request_digest=request_digest,
    )
    if existing is not None:
        original_record = _artifact_output(existing)
        return {
            "artifact_id": existing.id,
            "original_record": original_record,
            "source_inspection_number": inspection_number,
            "inspection_number": inspection_number,
            "selected_image_ids": [entry.id for entry, _path in selected],
            "image_count": len(selected),
            "template_binding": template_binding,
            "verification": (existing.metadata_json or {}).get("verification") or {},
            "print": {
                "sheet_name": MICROSCOPY_SHEET_NAME,
                "print_area": MICROSCOPY_PRINT_AREA,
                "available": True,
            },
            "reused": True,
        }

    template = _template_path()
    if not template.is_file() or _sha256(template) != MICROSCOPY_TEMPLATE_SHA256:
        raise ExecutionApiError(
            503,
            "microscopy_template_invalid",
            "微观形貌原始记录模板缺失或版本校验失败",
        )
    gateway = build_file_gateway(context.db)
    target_ref = ArtifactRef(STAGING_ROOT_ID, relative_path)
    target = gateway.resolve(target_ref, must_exist=False, for_write=True)
    gateway.ensure_parent(target_ref)
    if target.exists():
        raise ExecutionApiError(
            409,
            "artifact_file_conflict",
            "原始记录暂存路径已存在但没有匹配的制品记录",
        )

    with tempfile.TemporaryDirectory(
        prefix="microscopy-original-record-",
        dir=str(target.parent),
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        working = temporary / "working.xls"
        shutil.copyfile(template, working)
        prepared = _prepare_images(selected, temporary)
        placements = layout_images([item.aspect_ratio for item in prepared])
        payload_images = [
            {
                "path": str(prepared[index].prepared_path),
                "source_id": prepared[index].source_id,
                "aspect_ratio": prepared[index].aspect_ratio,
                **placement.as_dict(),
            }
            for index, placement in enumerate(placements)
        ]
        payload = {
            "workbook_path": str(working),
            "sheet_name": MICROSCOPY_SHEET_NAME,
            "print_area": MICROSCOPY_PRINT_AREA,
            "cells": cells,
            "canvas": {
                "range": "A4:L32",
                "max_width": CANVAS_WIDTH,
                "max_height": CANVAS_HEIGHT,
                "biff_excel_x_scale": MICROSCOPY_BIFF_EXCEL_X_SCALE,
            },
            "images": payload_images,
        }
        payload_path = temporary / "payload.json"
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        uno_result = _run_uno_writer(payload_path)
        verification = _verify_generated_workbook(
            working,
            expected_cells=cells,
            expected_images=payload_images,
            uno_result=uno_result,
        )
        os.replace(working, target)
        fsync_file(target)

    fingerprint = fingerprint_file(target)
    root = storage_root_by_key(context.db, STAGING_ROOT_ID)
    artifact = ExecutionArtifact(
        run_id=context.run.id,
        node_run_id=context.node_run.id,
        storage_root_id=root.id,
        relative_path=relative_path,
        filename=filename,
        role="working",
        media_type=MICROSCOPY_MEDIA_TYPE,
        size_bytes=fingerprint.size,
        content_sha256=fingerprint.sha256,
        immutable=True,
        metadata_json={
            "modified_ns": fingerprint.modified_ns,
            "request_digest": request_digest,
            "template_version": MICROSCOPY_TEMPLATE_VERSION,
            "template_sha256": MICROSCOPY_TEMPLATE_SHA256,
            "template_original_filename": (
                MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME
            ),
            "template_binding": template_binding,
            "biff_excel_x_scale": MICROSCOPY_BIFF_EXCEL_X_SCALE,
            "selected_image_ids": [entry.id for entry, _path in selected],
            "verification": verification,
            "sheet_name": MICROSCOPY_SHEET_NAME,
            "print_area": MICROSCOPY_PRINT_AREA,
        },
    )
    try:
        with context.db.begin_nested():
            context.db.add(artifact)
            context.db.flush()
    except IntegrityError as exc:
        target.unlink(missing_ok=True)
        raise ExecutionApiError(
            409,
            "artifact_registration_conflict",
            "原始记录制品并发登记冲突，请重试",
        ) from exc
    original_record = _artifact_output(artifact)
    return {
        "artifact_id": artifact.id,
        "original_record": original_record,
        "source_inspection_number": inspection_number,
        "inspection_number": inspection_number,
        "selected_image_ids": [entry.id for entry, _path in selected],
        "image_count": len(selected),
        "template_binding": template_binding,
        "verification": verification,
        "print": {
            "sheet_name": MICROSCOPY_SHEET_NAME,
            "print_area": MICROSCOPY_PRINT_AREA,
            "available": True,
        },
        "reused": False,
    }


def register_microscopy_original_record_executor() -> None:
    node_registry.set_executor(
        MICROSCOPY_RECORD_CONTEXT_NODE_TYPE,
        1,
        _microscopy_record_context_executor,
    )
    node_registry.set_executor(
        MICROSCOPY_ORIGINAL_RECORD_NODE_TYPE,
        1,
        _microscopy_original_record_executor,
    )
