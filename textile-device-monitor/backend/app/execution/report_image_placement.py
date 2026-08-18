from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.execution.errors import ExecutionApiError
from app.execution.persistence import build_file_gateway
from app.execution.storage import (
    ArtifactRef,
    FileGateway,
    StorageError,
    normalize_relative_path,
)


REPORT_IMAGE_ROOT_ID = "report_upload_images"
REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY = (
    "数据分析中心/3-报告上传图片/8-材料检测中心/1-微观形貌-GB T 36422"
)

# Windows 共享目录不允许出现的文件名字符；样品识别是人工确认的自由文本，
# 写入共享盘前必须收敛。
_FILENAME_ILLEGAL_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SUFFIX_PATTERN = re.compile(r"^\.[a-z0-9]{1,8}$")


def report_image_placement_node_config(node: Any) -> dict[str, Any]:
    """Return the node config when it marks a report-image placement node."""

    config = (node or {}).get("config") or {}
    if not isinstance(config, dict) or not config.get("report_image_placement"):
        return {}
    return config


def _sanitize_filename_segment(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = _FILENAME_ILLEGAL_PATTERN.sub("-", text)
    # Windows 不允许文件名以点或空格结尾。
    return text.strip().rstrip(". ").strip()


def _image_suffix(item: dict[str, Any]) -> str:
    suffix = str(item.get("suffix") or "").strip().lower()
    if _SUFFIX_PATTERN.fullmatch(suffix):
        return suffix
    relative_path = str(item.get("relative_path") or "")
    derived = (
        "." + relative_path.rsplit(".", 1)[1].lower() if "." in relative_path else ""
    )
    return derived if _SUFFIX_PATTERN.fullmatch(derived) else ""


def _display_directory(
    display_unc_base: str,
    target_directory: str,
    inspection_number: str,
) -> str:
    base = display_unc_base.rstrip("\\/")
    windows_subpath = target_directory.replace("/", "\\")
    if not base:
        # 未配置 UNC 前缀时仍给出可辨认的相对路径。
        return f"{windows_subpath}\\{inspection_number}"
    return f"{base}\\{windows_subpath}\\{inspection_number}"


def build_placement_plan(
    gateway: FileGateway,
    *,
    config: dict[str, Any],
    inspection_number: object,
    sample_identity: object,
    selected_images: object,
) -> dict[str, Any]:
    """Compute deterministic target names and current same-name conflicts.

    Naming contract: ``{编号}-{样品识别}[-序号]{后缀}``——同一次运行内第一张图
    不带序号，其后从 1 递增；样品识别为空时退化为 ``{编号}[-序号]``。
    """

    root_id = str(config.get("target_root_id") or REPORT_IMAGE_ROOT_ID).strip()
    raw_directory = str(
        config.get("target_directory") or REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
    ).strip()
    try:
        target_directory = normalize_relative_path(raw_directory)
    except StorageError as exc:
        raise ExecutionApiError(
            422,
            "report_image_target_directory_invalid",
            "图片放置目标子目录配置无效，请在流程设计中检查节点配置",
        ) from exc
    display_unc_base = str(
        getattr(
            settings,
            "EXECUTION_REPORT_IMAGE_DISPLAY_UNC",
            "",
        )
        or ""
    ).strip()

    number = " ".join(str(inspection_number or "").split())
    if not number or _FILENAME_ILLEGAL_PATTERN.search(number):
        raise ExecutionApiError(
            422,
            "report_image_inspection_number_invalid",
            "检验编号包含共享目录不允许的字符，无法生成图片文件夹",
        )
    identity = _sanitize_filename_segment(sample_identity)

    if not isinstance(selected_images, list) or not selected_images:
        raise ExecutionApiError(
            422,
            "report_image_selection_missing",
            "缺少本次运行已选择的图片，无法放置报告上传图片",
        )
    files: list[dict[str, Any]] = []
    base_name = f"{number}-{identity}" if identity else number
    for index, item in enumerate(selected_images):
        if not isinstance(item, dict):
            raise ExecutionApiError(
                422,
                "report_image_selection_invalid",
                "已选择图片的元数据格式无效",
            )
        source_root_id = str(item.get("root_id") or "").strip()
        relative_path = str(item.get("relative_path") or "").strip()
        if not source_root_id or not relative_path:
            raise ExecutionApiError(
                422,
                "report_image_selection_invalid",
                "已选择图片缺少来源根目录或相对路径",
            )
        suffix = _image_suffix(item)
        stem = base_name if index == 0 else f"{base_name}-{index}"
        files.append(
            {
                "source": {
                    "root_id": source_root_id,
                    "relative_path": relative_path,
                },
                "source_name": str(item.get("name") or ""),
                "target_filename": f"{stem}{suffix}",
                "size_bytes": item.get("size"),
            }
        )

    target_relative_dir = f"{target_directory}/{number}"
    try:
        target_dir = gateway.resolve(
            ArtifactRef(root_id, target_relative_dir),
            must_exist=False,
            for_write=True,
        )
    except StorageError as exc:
        raise ExecutionApiError(
            503,
            "report_image_root_unavailable",
            "报告上传图片共享目录不可用或未配置写入权限，请联系管理员检查挂载",
        ) from exc
    conflicts = [
        item["target_filename"]
        for item in files
        if (target_dir / item["target_filename"]).is_file()
    ]
    return {
        "inspection_number": number,
        "sample_identity": identity or None,
        "target_root_id": root_id,
        "target_relative_dir": target_relative_dir,
        "display_directory": _display_directory(
            display_unc_base,
            target_directory,
            number,
        ),
        "image_count": len(files),
        "files": files,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
    }


def execute_placement_plan(
    gateway: FileGateway,
    plan: dict[str, Any],
    *,
    overwrite: bool,
) -> dict[str, Any]:
    """Copy planned images into the target folder and return a receipt.

    ``overwrite=False`` 使用排他创建（xb），同名文件在写入瞬间出现时会以
    ``report_image_conflict_detected`` 失败，绝不静默覆盖；``overwrite=True``
    表示人工已确认覆盖同名文件。
    """

    root_id = str(plan.get("target_root_id") or REPORT_IMAGE_ROOT_ID)
    target_relative_dir = str(plan.get("target_relative_dir") or "")
    try:
        target_dir = gateway.ensure_directory(ArtifactRef(root_id, target_relative_dir))
    except StorageError as exc:
        raise ExecutionApiError(
            503,
            "report_image_root_unavailable",
            "报告上传图片共享目录不可用或无法创建编号文件夹，请检查共享盘挂载",
        ) from exc

    placed: list[dict[str, Any]] = []
    overwritten: list[str] = []
    raced_conflicts: list[str] = []
    for item in plan.get("files") or []:
        source = item.get("source") or {}
        try:
            source_path = gateway.resolve(
                ArtifactRef(
                    str(source.get("root_id") or ""),
                    str(source.get("relative_path") or ""),
                ),
                expected_type="file",
            )
        except StorageError as exc:
            raise ExecutionApiError(
                422,
                "report_image_source_missing",
                f"源图片 {source.get('relative_path') or ''} 已不在索引根目录中，请重新运行流程",
            ) from exc
        target_name = str(item.get("target_filename") or "")
        target_path = target_dir / target_name
        digest = hashlib.sha256()
        written = 0
        existed = target_path.is_file()
        try:
            with source_path.open("rb") as src:
                with target_path.open("wb" if overwrite else "xb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        dst.write(chunk)
                        written += len(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
        except FileExistsError:
            raced_conflicts.append(target_name)
            continue
        except OSError as exc:
            raise ExecutionApiError(
                502,
                "report_image_write_failed",
                f"写入报告上传图片失败：{exc}",
            ) from exc
        if existed:
            overwritten.append(target_name)
        placed.append(
            {
                "target_filename": target_name,
                "source_relative_path": source.get("relative_path"),
                "size_bytes": written,
                "content_sha256": digest.hexdigest(),
            }
        )
    if raced_conflicts:
        raise ExecutionApiError(
            409,
            "report_image_conflict_detected",
            "写入时发现新的同名文件，已停止放置；请重试该节点以重新核对并确认覆盖或取消",
            details={"conflicts": raced_conflicts},
        )
    return {
        "placement_cancelled": False,
        "overwrite": overwrite,
        "target_root_id": root_id,
        "target_relative_dir": target_relative_dir,
        "display_directory": plan.get("display_directory"),
        "image_count": plan.get("image_count"),
        "placed_count": len(placed),
        "placed_files": placed,
        "overwritten_files": overwritten,
        "conflicts": plan.get("conflicts") or [],
        "placed_at": datetime.now(timezone.utc).isoformat(),
    }


def _placement_inputs(context) -> tuple[dict[str, Any], dict[str, Any]]:
    config = report_image_placement_node_config(context.node)
    input_data = context.input_data or {}
    return config, input_data


def _plan_from_context(context, gateway: FileGateway) -> dict[str, Any]:
    config, input_data = _placement_inputs(context)
    return build_placement_plan(
        gateway,
        config=config,
        inspection_number=(
            input_data.get("inspection_number") or context.run.inspection_number
        ),
        sample_identity=input_data.get("sample_identity"),
        selected_images=input_data.get("selected_images"),
    )


def auto_complete_report_image_placement(context) -> Optional[dict[str, Any]]:
    """无同名冲突时直接放置并自动完成；有冲突时交人工决定覆盖或取消。"""

    if not report_image_placement_node_config(context.node):
        return None
    gateway = build_file_gateway(context.db)
    plan = _plan_from_context(context, gateway)
    # 计划随人工任务一起展示（可复制路径、拟放置清单、冲突列表）。
    context.input_data = {**context.input_data, "placement_plan": plan}
    context.node_run.input_data = context.input_data
    if plan["conflicts"]:
        return None
    receipt = execute_placement_plan(gateway, plan, overwrite=False)
    return {
        **receipt,
        "auto_submitted": True,
        "auto_submit_reason": "no_name_conflict",
    }


def report_image_placement_form_schema(context) -> Optional[dict[str, Any]]:
    if not report_image_placement_node_config(context.node):
        return None
    plan = (context.node_run.input_data or {}).get("placement_plan") or {}
    conflicts = plan.get("conflicts") or []
    return {
        "type": "object",
        "properties": {
            "placement_action": {
                "type": "string",
                "title": "目标文件夹已存在同名图片，如何处理",
                "description": (
                    f"目标文件夹：{plan.get('display_directory') or '—'}。"
                    f"共 {len(conflicts)} 个同名文件。选择覆盖将替换目标文件夹中"
                    "所有同名文件；选择取消则不放置任何图片，流程直接结束。"
                ),
                "enum": ["overwrite", "cancel"],
                "enumNames": ["覆盖同名文件并放置", "取消放置"],
            }
        },
        "required": ["placement_action"],
        "additionalProperties": False,
    }


def normalize_report_image_placement_submission(
    db,
    *,
    run,
    node_run,
    node: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """处理人工决定：取消直接收尾；覆盖则按当前状态重建计划后写入。"""

    config = report_image_placement_node_config(node)
    if not config:
        raise ExecutionApiError(
            409,
            "report_image_placement_context_changed",
            "图片放置节点配置已变化，请重新运行流程",
        )
    plan = (node_run.input_data or {}).get("placement_plan")
    if not isinstance(plan, dict):
        raise ExecutionApiError(
            409,
            "report_image_placement_context_changed",
            "图片放置上下文已变化，请重新运行流程",
        )
    action = str(data.get("placement_action") or "").strip()
    if action not in {"overwrite", "cancel"}:
        raise ExecutionApiError(
            422,
            "report_image_placement_decision_required",
            "请选择覆盖同名文件并放置，或取消放置",
        )
    if action == "cancel":
        return {
            "placement_cancelled": True,
            "overwrite": False,
            "target_root_id": plan.get("target_root_id"),
            "target_relative_dir": plan.get("target_relative_dir"),
            "display_directory": plan.get("display_directory"),
            "image_count": plan.get("image_count"),
            "placed_count": 0,
            "placed_files": [],
            "overwritten_files": [],
            "conflicts": plan.get("conflicts") or [],
        }
    gateway = build_file_gateway(db)
    fresh_plan = build_placement_plan(
        gateway,
        config=config,
        inspection_number=(
            (node_run.input_data or {}).get("inspection_number")
            or run.inspection_number
        ),
        sample_identity=(node_run.input_data or {}).get("sample_identity"),
        selected_images=(node_run.input_data or {}).get("selected_images"),
    )
    receipt = execute_placement_plan(gateway, fresh_plan, overwrite=True)
    receipt["conflicts"] = plan.get("conflicts") or []
    return receipt
