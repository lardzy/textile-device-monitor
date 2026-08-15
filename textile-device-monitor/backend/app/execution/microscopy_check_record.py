from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import xlrd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.electron_microscopy import ELECTRON_TEST_METHOD
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_families import microscopy_family_from_config
from app.execution.microscopy_original_record import (
    _normalized_text,
    _safe_inspection_number,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.models import ExecutionArtifact
from app.execution.persistence import build_file_gateway, storage_root_by_key
from app.execution.storage import (
    ArtifactRef,
    StorageError,
    fingerprint_file,
    fsync_file,
)


MICROSCOPY_CHECK_RECORD_NODE_TYPE = "workbook.microscopy_check_record"
MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION = (
    "gbt36422-2018-microscopy-check-record-v3"
)
MICROSCOPY_CHECK_RECORD_SHEET_NAME = "Sheet1"
MICROSCOPY_CHECK_RECORD_MEDIA_TYPE = "application/vnd.ms-excel"
STAGING_ROOT_ID = "execution_staging"
OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MICROSCOPY_CHECK_RECORD_CELLS = (
    "AS4",
    "Z7",
    "I8",
    "I9",
    "I10",
    "I11",
    "G12",
    "G13",
    # Legacy collector feed cells from OriginalKeyDataConfig.  They must hold
    # final literal values: the legacy collector reads raw cell values and
    # third-party BIFF8 writers do not guarantee recalculated formula caches.
    "BI7",
    "BK7",
    "BI8",
    "BI9",
    "BI10",
    "BI11",
    "BI12",
    "BI13",
)


def _optional_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_value(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _normalized_text(value).casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "是",
        "需要",
        "判否",
    }


def microscopy_check_record_cells(
    *,
    inspection_number: object,
    sample_identification: object = None,
    test_method: object = None,
    check_item_name: object = None,
    judgement_required: object = None,
    judgement_basis: object = None,
    indicator_requirement: object = None,
    test_result: object = None,
    remark: object = None,
    judgement: object = None,
) -> dict[str, str]:
    """Build the audited cell payload for the old CheckRecord template.

    Empty values are written deliberately so a template placeholder can never
    leak into a saved registration.  When the task explicitly does not require
    judgement, all four judgement-related fields stay empty.  The hidden
    OriginalKeyDataConfig feed cells (BI7/BK7/BI8..BI13) always receive final
    literal values because the legacy collector reads raw cell values.
    """

    normalized_number = _safe_inspection_number(inspection_number)
    normalized_method = _normalized_text(test_method) or ELECTRON_TEST_METHOD
    if normalized_method != ELECTRON_TEST_METHOD:
        raise ExecutionApiError(
            422,
            "microscopy_check_record_method_mismatch",
            "检验记录登记模板仅支持 GB/T 36422-2018 纤维微观形貌项目",
            details={"test_method": normalized_method},
        )

    normalized_item_name = _normalized_text(check_item_name) or "纤维微观形貌"
    normalized_identification = _normalized_text(sample_identification)
    judgement_enabled = (
        None if judgement_required is None else _truthy_flag(judgement_required)
    )
    judgement_values = {
        "I9": _normalized_text(judgement_basis),
        "I10": _normalized_text(indicator_requirement),
        "I11": _normalized_text(test_result),
        "G13": _normalized_text(judgement),
    }
    if judgement_enabled is True and any(
        not value for value in judgement_values.values()
    ):
        raise ExecutionApiError(
            422,
            "microscopy_check_record_judgement_fields_required",
            "任务单要求判定，请填写判定依据、指标要求、测试结果和判定",
        )
    if judgement_enabled is False:
        judgement_values = {cell: "" for cell in judgement_values}

    normalized_remark = _normalized_text(remark)
    return {
        "AS4": normalized_number,
        "Z7": normalized_identification,
        "I8": normalized_method,
        **judgement_values,
        "G12": normalized_remark,
        # OriginalKeyDataConfig feed cells.  The legacy collector concatenates
        # BI7++BK7 for the key-result CheckItemName and reads BI8..BI13 for the
        # remaining 类别 fields, so each one receives its final literal value.
        "BI7": normalized_item_name,
        "BK7": normalized_identification,
        "BI8": normalized_method,
        "BI9": judgement_values["I9"],
        "BI10": judgement_values["I10"],
        "BI11": judgement_values["I11"],
        "BI12": normalized_remark,
        "BI13": judgement_values["G13"],
    }


def _cell_payload(input_data: dict[str, Any], inspection_number: str) -> dict[str, str]:
    project = _optional_mapping(input_data.get("selected_project"))
    task = _optional_mapping(input_data.get("task"))
    return microscopy_check_record_cells(
        inspection_number=inspection_number,
        sample_identification=_first_value(
            input_data.get("sample_identification"),
            input_data.get("sample_identity"),
            project.get("sample_identification"),
            project.get("sample_identify"),
            project.get("sample_identity"),
        ),
        test_method=_first_value(
            input_data.get("test_method"),
            input_data.get("check_method"),
            project.get("test_method"),
            project.get("check_method"),
            ELECTRON_TEST_METHOD,
        ),
        check_item_name=_first_value(
            input_data.get("check_item_name"),
            project.get("check_item_name"),
            project.get("item_name"),
        ),
        judgement_required=_first_value(
            input_data.get("judgement_required"),
            project.get("judgement_required"),
            project.get("give_judgement"),
        ),
        judgement_basis=_first_value(
            input_data.get("judgement_basis"),
            input_data.get("judge_basis"),
            project.get("judgement_basis"),
            project.get("judge_basis"),
            task.get("check_basis"),
        ),
        indicator_requirement=_first_value(
            input_data.get("indicator_requirement"),
            input_data.get("requirement"),
            project.get("indicator_requirement"),
            project.get("requirement"),
        ),
        test_result=_first_value(
            input_data.get("test_result"),
            project.get("test_result"),
        ),
        remark=_first_value(
            input_data.get("remark"),
        ),
        judgement=_first_value(
            input_data.get("judgement"),
            project.get("judgement"),
        ),
    )


def _image_count(input_data: dict[str, Any]) -> int:
    declared = input_data.get("image_count")
    selected = input_data.get("selected_image_ids")
    selected_count = len(selected) if isinstance(selected, list) else None
    if declared is None:
        declared = selected_count
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise ExecutionApiError(
            422,
            "microscopy_check_record_image_count_required",
            "生成检验记录登记工作簿前必须提供实际选图数量",
        )
    if selected_count is not None and selected_count != declared:
        raise ExecutionApiError(
            409,
            "microscopy_check_record_image_count_mismatch",
            "选图数量与原始记录输出不一致，请重新运行生成节点",
            details={
                "image_count": declared,
                "selected_image_count": selected_count,
            },
        )
    return declared


def _template_path(binding: dict[str, Any]) -> Path:
    return Path(__file__).resolve().parent / "templates" / str(
        binding["local_asset_name"]
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cell_coordinates(cell: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
    if match is None:
        raise ValueError("invalid_cell_address")
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - ord("A") + 1
    return int(match.group(2)) - 1, column - 1


def _build_biff_edits(cells: dict[str, str]) -> list["CellEdit"]:
    """Translate the audited cell values into surgical BIFF edits.

    Only cells that actually receive a value are touched; empty judgement or
    identity fields keep the template's original records byte-for-byte.  The
    hidden 类别 feed cells (BI8 and the BI9..BI13 formula caches) mirror the
    visible cells, exactly like a desktop recalculation.
    """
    from app.execution.biff_patch import CellEdit

    def cell(address: str) -> tuple[int, int]:
        row_text = ""
        column_text = ""
        for character in address:
            if character.isdigit():
                row_text += character
            else:
                column_text += character
        column = 0
        for character in column_text:
            column = column * 26 + ord(character) - ord("A") + 1
        return int(row_text) - 1, column - 1

    edits: list[CellEdit] = []
    row, column = cell("AS4")
    edits.append(CellEdit(row, column, "number", float(cells["AS4"])))

    method = cells["I8"]
    row, column = cell("BI8")
    edits.append(CellEdit(row, column, "text", method))
    row, column = cell("I8")
    edits.append(CellEdit(row, column, "cached_string", method))

    for visible, mirror in (
        ("Z7", "BK7"),
        ("I9", "BI9"),
        ("I10", "BI10"),
        ("I11", "BI11"),
        ("G12", "BI12"),
        ("G13", "BI13"),
    ):
        value = cells[visible]
        if not value:
            continue
        row, column = cell(visible)
        edits.append(CellEdit(row, column, "text", value))
        row, column = cell(mirror)
        edits.append(CellEdit(row, column, "cached_string", value))
    return edits


def _verify_patched_workbook(
    path: Path,
    *,
    template: Path,
    cells: dict[str, str],
    expected_cells: list[str],
) -> dict[str, Any]:
    """Re-read the patched workbook and prove template faithfulness.

    The desktop client keeps every template formula with a corrected cached
    result, so the patch must too: values are checked through xlrd, every
    formula's expression bytes must survive, only the declared cells may
    change, and every other stream of the compound file stays byte-identical.
    """
    from app.execution.biff_patch import (
        formula_cells,
        ole_read,
        verify_patch_scope,
    )

    patched_ole = ole_read(path)
    template_ole = ole_read(template)
    patched_workbook = next(
        stream.data for stream in patched_ole.streams if stream.name == "Workbook"
    )
    template_workbook = next(
        stream.data for stream in template_ole.streams if stream.name == "Workbook"
    )
    for template_stream in template_ole.streams:
        if template_stream.name == "Workbook":
            continue
        patched_stream = next(
            (stream for stream in patched_ole.streams if stream.name == template_stream.name),
            None,
        )
        if patched_stream is None or patched_stream.data != template_stream.data:
            raise ExecutionApiError(
                500,
                "microscopy_check_record_verification_failed",
                "登记工作簿的非工作簿流与模板不一致",
            )

    formulas_before = formula_cells(template_workbook)
    formulas_after = formula_cells(patched_workbook)
    if len(formulas_before) != len(formulas_after) or any(
        formulas_after.get(address, (None,))[0] != rpn
        for address, (rpn, _) in formulas_before.items()
    ):
        raise ExecutionApiError(
            500,
            "microscopy_check_record_verification_failed",
            "登记工作簿的模板公式未完整保留",
        )

    scope = verify_patch_scope(
        template_workbook,
        patched_workbook,
        max_changed_records=3 * len(_build_biff_edits(cells)) + 6,
    )
    if not scope["within_bound"]:
        raise ExecutionApiError(
            500,
            "microscopy_check_record_verification_failed",
            "登记工作簿的改动超出声明范围",
            details={
                "changed_records": scope["changed_records"],
                "violations": scope["violations"][:4],
            },
        )

    workbook = None
    actual_cells: dict[str, Any] = {}
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        if MICROSCOPY_CHECK_RECORD_SHEET_NAME not in workbook.sheet_names():
            raise ValueError("sheet_missing")
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        for cell in expected_cells:
            row, column = _cell_coordinates(cell)
            if row >= sheet.nrows or column >= sheet.ncols:
                actual_cells[cell] = "" if cell != "AS4" else None
            else:
                value = sheet.cell_value(row, column)
                if cell == "AS4" and isinstance(value, float) and value.is_integer():
                    actual_cells[cell] = str(int(value))
                elif cell == "AS4":
                    actual_cells[cell] = value
                else:
                    actual_cells[cell] = _normalized_text(value)
    except (OSError, xlrd.XLRDError, IndexError, ValueError) as exc:
        raise ExecutionApiError(
            500,
            "microscopy_check_record_verification_failed",
            "无法重读生成后的检验记录登记工作簿",
        ) from exc
    finally:
        if workbook is not None:
            workbook.release_resources()

    mismatches: dict[str, dict[str, Any]] = {}
    for cell in expected_cells:
        expected = cells.get(cell, "")
        actual = actual_cells.get(cell)
        if cell == "AS4":
            actual_number = None
            try:
                actual_number = float(actual) if actual is not None else None
            except (TypeError, ValueError):
                actual_number = None
            if actual_number != float(expected):
                mismatches[cell] = {"expected": expected, "actual": actual}
            continue
        if actual != expected:
            mismatches[cell] = {"expected": expected, "actual": actual}
    if mismatches:
        raise ExecutionApiError(
            500,
            "microscopy_check_record_verification_failed",
            "生成后的检验记录登记工作簿内容与预期不一致",
            details={"cell_mismatches": mismatches},
        )
    return {
        "verified": True,
        "sheet_name": MICROSCOPY_CHECK_RECORD_SHEET_NAME,
        "checked_cells": sorted(expected_cells),
        "cells": actual_cells,
        "formulas_preserved": len(formulas_before),
        "changed_records": scope["changed_records"],
        "streams_preserved": True,
        "ole_header": True,
        "size_bytes": path.stat().st_size,
    }
def _request_digest(
    *,
    inspection_number: str,
    image_count: int,
    cells: dict[str, str],
    template_binding: dict[str, Any],
) -> str:
    payload = {
        "generator_version": MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
        "inspection_number": inspection_number,
        "image_count": image_count,
        "cells": cells,
        "template_binding": template_binding,
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


def _existing_artifact(
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
    try:
        path = build_file_gateway(db).resolve(
            ArtifactRef(STAGING_ROOT_ID, relative_path),
            expected_type="file",
        )
        current = fingerprint_file(path)
    except (StorageError, OSError):
        return None
    if current.sha256 != artifact.content_sha256 or current.size != artifact.size_bytes:
        return None
    return artifact


def _result(
    artifact: ExecutionArtifact,
    *,
    inspection_number: str,
    image_count: int,
    template_binding: dict[str, Any],
    verification: dict[str, Any],
    reused: bool,
) -> dict[str, Any]:
    check_record = _artifact_output(artifact)
    metadata = artifact.metadata_json or {}
    expected_key_identities = list(
        metadata.get("expected_key_identities") or []
    )
    return {
        "artifact_id": artifact.id,
        "check_record": check_record,
        "legacy_registration_workbook": check_record,
        "source_inspection_number": inspection_number,
        "inspection_number": inspection_number,
        "image_count": image_count,
        "template_binding": template_binding,
        "expected_key_identities": expected_key_identities,
        "verification": verification,
        "reused": reused,
    }


def microscopy_check_record_executor(context) -> dict[str, Any]:
    """Generate the Sheet1 workbook consumed by CheckRecord registration."""

    node = getattr(context, "node", None)
    family = microscopy_family_from_config(
        (node or {}).get("config") if isinstance(node, dict) else None
    )
    input_data = context.input_data or {}
    inspection_number = _safe_inspection_number(
        input_data.get("inspection_number") or context.run.inspection_number
    )
    image_count = _image_count(input_data)
    template_binding = resolve_microscopy_legacy_template_binding(
        image_count,
        declared_binding=input_data.get("template_binding"),
        family=family,
    )
    cells = _cell_payload(input_data, inspection_number)
    request_digest = _request_digest(
        inspection_number=inspection_number,
        image_count=image_count,
        cells=cells,
        template_binding=template_binding,
    )
    filename = (
        f"{inspection_number}-{family.check_record_filename_segment}"
        "-检验记录登记.xls"
    )
    relative_path = (
        f"check-records/{context.run.id}/{context.node_run.id}/{filename}"
    )
    existing = _existing_artifact(
        context.db,
        run_id=context.run.id,
        node_run_id=context.node_run.id,
        relative_path=relative_path,
        request_digest=request_digest,
    )
    if existing is not None:
        metadata = existing.metadata_json or {}
        return _result(
            existing,
            inspection_number=inspection_number,
            image_count=image_count,
            template_binding=template_binding,
            verification=metadata.get("verification") or {},
            reused=True,
        )

    template = _template_path(template_binding)
    expected_template_sha = str(template_binding["local_asset_sha256"])
    if not template.is_file() or _sha256(template) != expected_template_sha:
        raise ExecutionApiError(
            503,
            "microscopy_legacy_template_asset_invalid",
            f"旧系统{family.check_item_name}模板资产缺失或版本校验失败",
            details={
                "image_count": image_count,
                "local_asset_name": template_binding["local_asset_name"],
            },
        )

    gateway = build_file_gateway(context.db)
    target_ref = ArtifactRef(STAGING_ROOT_ID, relative_path)
    target = gateway.resolve(target_ref, must_exist=False, for_write=True)
    gateway.ensure_parent(target_ref)
    if target.exists():
        raise ExecutionApiError(
            409,
            "artifact_file_conflict",
            "检验记录登记暂存路径已存在但没有匹配的制品记录",
        )

    with tempfile.TemporaryDirectory(
        prefix="microscopy-check-record-",
        dir=str(target.parent),
    ) as temporary_directory:
        working = Path(temporary_directory) / "working.xls"
        shutil.copyfile(template, working)
        if _sha256(working) != expected_template_sha:
            raise ExecutionApiError(
                500,
                "microscopy_check_record_template_copy_failed",
                "检验记录登记模板工作副本校验失败",
            )
        from app.execution.biff_patch import patch_workbook_file

        patch_workbook_file(working, working, _build_biff_edits(cells))
        verification = _verify_patched_workbook(
            working,
            template=template,
            cells=cells,
            expected_cells=list(cells),
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
        media_type=MICROSCOPY_CHECK_RECORD_MEDIA_TYPE,
        size_bytes=fingerprint.size,
        content_sha256=fingerprint.sha256,
        immutable=True,
        metadata_json={
            "modified_ns": fingerprint.modified_ns,
            "request_digest": request_digest,
            "generator_version": MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
            "template_binding": template_binding,
            "template_sha256": expected_template_sha,
            "image_count": image_count,
            "sheet_name": MICROSCOPY_CHECK_RECORD_SHEET_NAME,
            # Every approved microscopy template maps BI7++BK7. BI7 is the
            # item name and BK7 is =IF(Z7="","",Z7), so the legacy
            # collector's single key-result SampleIdentity is exactly Z7.
            "expected_key_identities": [cells["Z7"]],
            "verification": verification,
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
            "检验记录登记制品并发登记冲突，请重试",
        ) from exc
    return _result(
        artifact,
        inspection_number=inspection_number,
        image_count=image_count,
        template_binding=template_binding,
        verification=verification,
        reused=False,
    )
