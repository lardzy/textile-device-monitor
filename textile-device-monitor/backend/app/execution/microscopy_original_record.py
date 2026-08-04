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
MICROSCOPY_TEMPLATE_SHA256 = (
    "d2b70e114cb85c89e4961ba2acb1b0cb451656f7a12b5dd5bdbacba1d50f88ba"
)
MICROSCOPY_SHEET_NAME = "微观形貌"
MICROSCOPY_PRINT_AREA = "$A$1:$L$37"
MICROSCOPY_MEDIA_TYPE = "application/vnd.ms-excel"
STAGING_ROOT_ID = "execution_staging"
MAX_SELECTED_IMAGES = 10

# LibreOffice uses 1/100 mm for drawing coordinates. These limits are the
# measured printable image canvas of A4:L32 in the approved template.
CANVAS_WIDTH = 21_600
CANVAS_HEIGHT = 11_700
IMAGE_GAP = 250
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


def _microscopy_record_context_executor(context) -> dict[str, Any]:
    """Turn cached task facts and image-selection output into a UI contract."""

    input_data = context.input_data or {}
    inspection_number = _safe_inspection_number(
        input_data.get("inspection_number") or context.run.inspection_number
    )
    task_snapshot = input_data.get("task")
    task_snapshot = task_snapshot if isinstance(task_snapshot, dict) else {}
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
    expected_image_count: int,
    expected_image_aspect_ratios: Sequence[float],
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
        or uno_result.get("image_count") != expected_image_count
        or not isinstance(placements, list)
        or len(placements) != expected_image_count
        or not isinstance(canvas, dict)
        or uno_result.get("print_area_verified") is not True
    ):
        raise ExecutionApiError(
            500,
            "workbook_verification_failed",
            "生成后的原始记录内容与预期不一致",
            details={"cell_mismatches": mismatches},
        )
    canvas_width = int(canvas.get("width") or 0)
    canvas_height = int(canvas.get("height") or 0)
    for placement in placements:
        x = int(placement.get("x") or 0)
        y = int(placement.get("y") or 0)
        width = int(placement.get("width") or 0)
        height = int(placement.get("height") or 0)
        if (
            width <= 0
            or height <= 0
            or x < 0
            or y < 0
            or x + width > canvas_width + 2
            or y + height > canvas_height + 2
        ):
            raise ExecutionApiError(
                500,
                "workbook_verification_failed",
                "生成后的图片尺寸或位置超出模板边界",
            )
    single_image_max_fill_verified = expected_image_count != 1
    if expected_image_count == 1:
        single_image_max_fill_verified = (
            len(expected_image_aspect_ratios) == 1
            and _single_image_geometry_matches(
                placements[0],
                canvas_width=canvas_width,
                canvas_height=canvas_height,
                expected_aspect_ratio=float(expected_image_aspect_ratios[0]),
            )
        )
        if not single_image_max_fill_verified:
            raise ExecutionApiError(
                500,
                "workbook_verification_failed",
                "生成后的单张图片未保持比例填满模板图片区域",
                details={
                    "canvas": canvas,
                    "image": placements[0],
                    "expected_aspect_ratio": (
                        float(expected_image_aspect_ratios[0])
                        if expected_image_aspect_ratios
                        else None
                    ),
                },
            )
    return {
        "verified": True,
        "sheet_name": MICROSCOPY_SHEET_NAME,
        "checked_cells": sorted(expected_cells),
        "image_count": expected_image_count,
        "images": placements,
        "canvas": canvas,
        "print_area": MICROSCOPY_PRINT_AREA,
        "print_area_verified": True,
        "single_image_max_fill_verified": single_image_max_fill_verified,
        "ole_header": True,
        "size_bytes": size,
    }


def _request_digest(
    *,
    inspection_number: str,
    cells: dict[str, str],
    images: Sequence[tuple[ExecutionFileIndexEntry, Path]],
) -> str:
    payload = {
        "template_version": MICROSCOPY_TEMPLATE_VERSION,
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
    task_snapshot = input_data.get("task")
    task_snapshot = task_snapshot if isinstance(task_snapshot, dict) else {}
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
    request_digest = _request_digest(
        inspection_number=inspection_number,
        cells=cells,
        images=selected,
    )
    filename = f"{inspection_number}-图片-纤维微观形貌原始记录.xls"
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
        payload = {
            "workbook_path": str(working),
            "sheet_name": MICROSCOPY_SHEET_NAME,
            "print_area": MICROSCOPY_PRINT_AREA,
            "cells": cells,
            "canvas": {
                "range": "A4:L32",
                "max_width": CANVAS_WIDTH,
                "max_height": CANVAS_HEIGHT,
            },
            "images": [
                {
                    "path": str(prepared[index].prepared_path),
                    **placement.as_dict(),
                }
                for index, placement in enumerate(placements)
            ],
        }
        payload_path = temporary / "payload.json"
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        uno_result = _run_uno_writer(payload_path)
        verification = _verify_generated_workbook(
            working,
            expected_cells=cells,
            expected_image_count=len(prepared),
            expected_image_aspect_ratios=[
                item.aspect_ratio for item in prepared
            ],
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
