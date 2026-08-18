from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

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
        source_id = str(item.get("id") or "").strip()
        source_fingerprint = str(item.get("fingerprint") or "").strip()
        if (
            not source_root_id
            or not relative_path
            or not source_id
            or not source_fingerprint
        ):
            raise ExecutionApiError(
                422,
                "report_image_selection_invalid",
                "已选择图片缺少索引 ID、指纹或来源路径",
            )
        suffix = _image_suffix(item)
        stem = base_name if index == 0 else f"{base_name}-{index}"
        files.append(
            {
                "source": {
                    "id": source_id,
                    "root_id": source_root_id,
                    "relative_path": relative_path,
                    "fingerprint": source_fingerprint,
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


def _indexed_fingerprint(stat_result: os.stat_result) -> str:
    return f"{stat_result.st_size}:{stat_result.st_mtime_ns}"


def _validate_open_source(
    source: dict[str, Any],
    stat_result: os.stat_result,
) -> None:
    expected = str(source.get("fingerprint") or "")
    actual = _indexed_fingerprint(stat_result)
    if not expected or actual != expected:
        raise ExecutionApiError(
            409,
            "report_image_source_changed",
            "已选择的源图片已变化，请返回选图节点重新确认",
            details={
                "source_id": source.get("id"),
                "relative_path": source.get("relative_path"),
                "expected_fingerprint": expected,
                "actual_fingerprint": actual,
            },
        )


def _temporary_path(target_dir: Path, target_name: str, kind: str) -> Path:
    return target_dir / f".{target_name}.{uuid4().hex}.{kind}"


def _safe_unlink(path: Path) -> Optional[str]:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"{path.name}: {exc}"
    return None


def _cleanup_paths(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        error = _safe_unlink(path)
        if error:
            errors.append(error)
    return errors


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability; Windows/CIFS may reject directory fsync."""

    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resolve_source_path(
    gateway: FileGateway,
    source: dict[str, Any],
) -> Path:
    try:
        return gateway.resolve(
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


def _stage_source(
    source_path: Path,
    source: dict[str, Any],
    temporary_path: Path,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    try:
        with source_path.open("rb") as src:
            before = os.fstat(src.fileno())
            _validate_open_source(source, before)
            with temporary_path.open("xb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dst.write(chunk)
                    written += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            after = os.fstat(src.fileno())
            _validate_open_source(source, after)
            if _indexed_fingerprint(before) != _indexed_fingerprint(after):
                raise ExecutionApiError(
                    409,
                    "report_image_source_changed",
                    "复制期间源图片发生变化，未放置任何图片",
                    details={
                        "source_id": source.get("id"),
                        "relative_path": source.get("relative_path"),
                    },
                )
    except ExecutionApiError:
        _safe_unlink(temporary_path)
        raise
    except OSError as exc:
        _safe_unlink(temporary_path)
        raise ExecutionApiError(
            502,
            "report_image_write_failed",
            f"暂存报告上传图片失败：{exc}",
        ) from exc
    return written, digest.hexdigest()


def _copy_existing_for_rollback(source: Path, backup: Path) -> None:
    try:
        with source.open("rb") as src, backup.open("xb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    except OSError as exc:
        _safe_unlink(backup)
        raise ExecutionApiError(
            502,
            "report_image_write_failed",
            f"为覆盖操作保留原图片失败：{exc}",
        ) from exc


def _content_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _rollback_or_raise(
    *,
    target_states: list[dict[str, Any]],
    original_error: OSError,
) -> None:
    rollback_errors: list[str] = []
    recovery_backups: list[str] = []
    for state in reversed(target_states):
        target = state["target"]
        backup = state.get("backup")
        try:
            if state.get("committed") and isinstance(backup, Path):
                if not backup.exists():
                    raise OSError(f"rollback_backup_missing:{backup.name}")
                os.replace(backup, target)
                state["backup"] = None
                state["committed"] = False
            elif state.get("reserved"):
                target.unlink(missing_ok=True)
                state["reserved"] = False
        except OSError as exc:
            rollback_errors.append(f"{target.name}: {exc}")
            if isinstance(backup, Path) and backup.exists():
                state["preserve_backup"] = True
                recovery_backups.append(str(backup))
    if rollback_errors:
        raise ExecutionApiError(
            500,
            "report_image_reconciliation_required",
            "图片放置失败且自动回滚未完成，请根据目标路径人工核对",
            details={
                "write_error": str(original_error),
                "rollback_errors": rollback_errors,
                "recovery_backups": recovery_backups,
            },
        ) from original_error
    raise ExecutionApiError(
        502,
        "report_image_write_failed",
        f"写入报告上传图片失败，已回滚：{original_error}",
    ) from original_error


def _matching_existing_receipt(
    gateway: FileGateway,
    plan: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Recognize a fully committed prior attempt after a DB commit failure."""

    files = plan.get("files") or []
    conflicts = set(plan.get("conflicts") or [])
    if not files or conflicts != {str(item.get("target_filename") or "") for item in files}:
        return None
    try:
        target_dir = gateway.resolve(
            ArtifactRef(
                str(plan.get("target_root_id") or REPORT_IMAGE_ROOT_ID),
                str(plan.get("target_relative_dir") or ""),
            ),
            expected_type="directory",
            for_write=True,
        )
    except StorageError as exc:
        raise ExecutionApiError(
            503,
            "report_image_root_unavailable",
            "报告上传图片目标目录不可用，请检查共享盘挂载",
        ) from exc
    placed: list[dict[str, Any]] = []
    for item in files:
        source = item.get("source") or {}
        source_path = _resolve_source_path(gateway, source)
        temporary = _temporary_path(
            target_dir,
            str(item.get("target_filename") or "source"),
            "verify",
        )
        size, source_sha = _stage_source(source_path, source, temporary)
        _safe_unlink(temporary)
        target_name = str(item.get("target_filename") or "")
        target = target_dir / target_name
        try:
            target_size, target_sha = _content_fingerprint(target)
        except OSError:
            return None
        if target_size != size or target_sha != source_sha:
            return None
        placed.append(
            {
                "target_filename": target_name,
                "source_id": source.get("id"),
                "source_relative_path": source.get("relative_path"),
                "source_fingerprint": source.get("fingerprint"),
                "size_bytes": size,
                "content_sha256": source_sha,
            }
        )
    return {
        "placement_cancelled": False,
        "overwrite": False,
        "placement_reused": True,
        "target_root_id": plan.get("target_root_id"),
        "target_relative_dir": plan.get("target_relative_dir"),
        "display_directory": plan.get("display_directory"),
        "image_count": plan.get("image_count"),
        "placed_count": len(placed),
        "placed_files": placed,
        "overwritten_files": [],
        "conflicts": plan.get("conflicts") or [],
        "placed_at": datetime.now(timezone.utc).isoformat(),
    }


def execute_placement_plan(
    gateway: FileGateway,
    plan: dict[str, Any],
    *,
    overwrite: bool,
) -> dict[str, Any]:
    """Copy planned images into the target folder and return a receipt.

    Every source is copied to a hidden file and fingerprint-checked before the
    first target is changed.  The commit phase uses same-directory ``replace``
    operations and restores backups (or removes new reservations) on failure.
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

    staged: list[dict[str, Any]] = []
    target_names: set[str] = set()
    try:
        for item in plan.get("files") or []:
            source = item.get("source") or {}
            target_name = str(item.get("target_filename") or "")
            if (
                not target_name
                or Path(target_name).name != target_name
                or target_name in target_names
            ):
                raise ExecutionApiError(
                    422,
                    "report_image_target_name_invalid",
                    "报告上传图片的目标文件名无效或重复",
                )
            target_names.add(target_name)
            source_path = _resolve_source_path(gateway, source)
            temporary = _temporary_path(target_dir, target_name, "staged")
            size, content_sha256 = _stage_source(
                source_path,
                source,
                temporary,
            )
            staged.append(
                {
                    "source": source,
                    "target_name": target_name,
                    "target": target_dir / target_name,
                    "temporary": temporary,
                    "size_bytes": size,
                    "content_sha256": content_sha256,
                }
            )
    except Exception:
        _cleanup_paths(
            [item["temporary"] for item in staged if item.get("temporary")]
        )
        raise

    target_states: list[dict[str, Any]] = []
    overwritten: list[str] = []
    planned_conflicts = {
        str(name) for name in plan.get("conflicts") or []
    }
    try:
        if overwrite:
            for item in staged:
                target = item["target"]
                state: dict[str, Any] = {
                    "target": target,
                    "backup": None,
                    "reserved": False,
                    "committed": False,
                    "preserve_backup": False,
                }
                if target.exists():
                    if item["target_name"] not in planned_conflicts:
                        raise ExecutionApiError(
                            409,
                            "report_image_conflict_detected",
                            "确认后出现了新的同名文件，未覆盖任何图片；请重新确认",
                            details={"conflicts": [item["target_name"]]},
                        )
                    if not target.is_file():
                        raise OSError(f"target_not_file:{target.name}")
                    backup = _temporary_path(
                        target_dir,
                        item["target_name"],
                        "backup",
                    )
                    _copy_existing_for_rollback(target, backup)
                    state["backup"] = backup
                    overwritten.append(item["target_name"])
                else:
                    try:
                        reservation = target.open("xb")
                    except FileExistsError:
                        if item["target_name"] not in planned_conflicts:
                            raise ExecutionApiError(
                                409,
                                "report_image_conflict_detected",
                                "确认后出现了新的同名文件，未覆盖任何图片；请重新确认",
                                details={
                                    "conflicts": [item["target_name"]]
                                },
                            )
                        backup = _temporary_path(
                            target_dir,
                            item["target_name"],
                            "backup",
                        )
                        _copy_existing_for_rollback(target, backup)
                        state["backup"] = backup
                        overwritten.append(item["target_name"])
                    else:
                        # Record the reservation before flush/fsync so an I/O
                        # failure during reservation durability is rolled back.
                        state["reserved"] = True
                        target_states.append(state)
                        with reservation:
                            reservation.flush()
                            os.fsync(reservation.fileno())
                        continue
                target_states.append(state)
        else:
            for item in staged:
                target = item["target"]
                state = {
                    "target": target,
                    "backup": None,
                    "reserved": False,
                    "committed": False,
                    "preserve_backup": False,
                }
                try:
                    reservation = target.open("xb")
                except FileExistsError as exc:
                    cleanup_errors = _cleanup_paths(
                        [state["target"] for state in target_states]
                        + [entry["temporary"] for entry in staged]
                    )
                    if cleanup_errors:
                        raise ExecutionApiError(
                            500,
                            "report_image_reconciliation_required",
                            "同名竞态发生后无法完整清理预留文件，请人工核对",
                            details={"cleanup_errors": cleanup_errors},
                        ) from exc
                    raise ExecutionApiError(
                        409,
                        "report_image_conflict_detected",
                        "写入时发现新的同名文件，未放置任何图片；请重试并确认覆盖或取消",
                        details={"conflicts": [item["target_name"]]},
                    ) from exc
                state["reserved"] = True
                target_states.append(state)
                with reservation:
                    reservation.flush()
                    os.fsync(reservation.fileno())

        for index, item in enumerate(staged):
            os.replace(item["temporary"], item["target"])
            target_states[index]["committed"] = True
        _fsync_directory(target_dir)
    except ExecutionApiError as exc:
        # Preparation can already have created zero-byte reservations.  No
        # target contents have been replaced yet when an application-level
        # validation/copy error is raised, so only those reservations need to
        # be removed; backup copies are cleaned in ``finally``.
        cleanup_errors = _cleanup_paths(
            [
                state["target"]
                for state in target_states
                if state.get("reserved")
            ]
        )
        if cleanup_errors:
            raise ExecutionApiError(
                500,
                "report_image_reconciliation_required",
                "图片放置准备失败且预留文件未完整清理，请人工核对",
                details={
                    "original_error": exc.code,
                    "cleanup_errors": cleanup_errors,
                },
            ) from exc
        raise
    except OSError as exc:
        _rollback_or_raise(
            target_states=target_states,
            original_error=exc,
        )
    finally:
        _cleanup_paths(
            [item["temporary"] for item in staged]
            + [
                state["backup"]
                for state in target_states
                if isinstance(state.get("backup"), Path)
                and not state.get("preserve_backup")
            ]
        )

    placed = [
        {
            "target_filename": item["target_name"],
            "source_id": item["source"].get("id"),
            "source_relative_path": item["source"].get("relative_path"),
            "source_fingerprint": item["source"].get("fingerprint"),
            "size_bytes": item["size_bytes"],
            "content_sha256": item["content_sha256"],
        }
        for item in staged
    ]
    return {
        "placement_cancelled": False,
        "overwrite": overwrite,
        "placement_reused": False,
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


def auto_complete_report_image_placement(context) -> Optional[dict[str, Any]]:
    """无同名冲突时直接放置并自动完成；有冲突时交人工决定覆盖或取消。"""

    config = report_image_placement_node_config(context.node)
    if not config:
        return None
    input_data = dict(context.input_data or {})
    inspection_number = (
        input_data.get("inspection_number") or context.run.inspection_number
    )
    gateway = build_file_gateway(context.db)
    # The claim and audited input are durable before any CIFS traversal or
    # write.  ``commit`` releases the SQLAlchemy connection; the lease
    # heartbeat keeps ownership while pure filesystem work runs, and
    # complete_node/_create_human_task reacquire a short transaction later.
    context.db.flush()
    context.db.commit()
    plan = build_placement_plan(
        gateway,
        config=config,
        inspection_number=inspection_number,
        sample_identity=input_data.get("sample_identity"),
        selected_images=input_data.get("selected_images"),
    )
    # 计划随人工任务一起展示（可复制路径、拟放置清单、冲突列表）。
    planned_input = {**input_data, "placement_plan": plan}
    request = planned_input.get("placement_request") or {}
    action = str(request.get("placement_action") or "").strip()
    if action == "overwrite":
        confirmed_target_names = request.get("target_filenames")
        current_target_names = [
            str(item.get("target_filename") or "")
            for item in plan.get("files") or []
        ]
        confirmed_conflicts = {
            str(name) for name in request.get("conflicts") or []
        }
        current_conflicts = {
            str(name) for name in plan.get("conflicts") or []
        }
        request_matches_plan = (
            isinstance(confirmed_target_names, list)
            and confirmed_target_names == current_target_names
            and request.get("target_root_id") == plan.get("target_root_id")
            and request.get("target_relative_dir")
            == plan.get("target_relative_dir")
            and request.get("image_count") == plan.get("image_count")
            and current_conflicts.issubset(confirmed_conflicts)
        )
        if not request_matches_plan:
            # The user authorized one exact target batch and conflict set.  A
            # newly appeared same-name file must never inherit that approval.
            planned_input.pop("placement_request", None)
            context.input_data = planned_input
            context.node_run.input_data = planned_input
            return None
        receipt = execute_placement_plan(gateway, plan, overwrite=True)
        context.input_data = planned_input
        context.node_run.input_data = planned_input
        return {
            **receipt,
            "auto_submitted": True,
            "auto_submit_reason": "confirmed_overwrite",
            "placement_request": request,
        }
    if plan["conflicts"]:
        reused = _matching_existing_receipt(gateway, plan)
        if reused is not None:
            context.input_data = planned_input
            context.node_run.input_data = planned_input
            return {
                **reused,
                "auto_submitted": True,
                "auto_submit_reason": "matching_prior_attempt",
            }
        context.input_data = planned_input
        context.node_run.input_data = planned_input
        return None
    receipt = execute_placement_plan(gateway, plan, overwrite=False)
    context.input_data = planned_input
    context.node_run.input_data = planned_input
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
    """处理人工决定：取消收尾，覆盖交给 Worker 执行。"""

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
    return {
        "placement_deferred": True,
        "placement_action": "overwrite",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "target_root_id": plan.get("target_root_id"),
        "target_relative_dir": plan.get("target_relative_dir"),
        "display_directory": plan.get("display_directory"),
        "image_count": plan.get("image_count"),
        "target_filenames": [
            str(item.get("target_filename") or "")
            for item in plan.get("files") or []
        ],
        "conflicts": plan.get("conflicts") or [],
    }


def is_deferred_report_image_placement(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("placement_deferred") is True
        and value.get("placement_action") == "overwrite"
    )
