from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict
from app.execution.index_metadata import inspection_numbers_in_text
from app.execution.models import (
    ExecutionFileIndexEntry,
    ExecutionStorageRoot,
    ExecutionTaskSnapshotCache,
    utcnow,
)
from app.execution.registry import node_registry


ELECTRON_ROOT_ID = "electron_microscopy_records"
ELECTRON_NODE_TYPE = "file.electron_microscopy_gbt36422"
ELECTRON_PROJECT_NAME_ALIASES = frozenset({"纤维微观形貌", "膜平面形貌"})
ELECTRON_TEST_METHOD = "GB/T 36422-2018"
ELECTRON_IMAGE_SUFFIXES = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
MAX_INDEXED_IMAGES = 2_000
TASK_SNAPSHOT_SCHEMA_VERSION = 5
PUBLIC_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{16}$")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _normalized_fact(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )


def _is_complete_inspection_number(value: str) -> bool:
    matches = inspection_numbers_in_text(value)
    return len(matches) == 1 and matches[0].casefold() == value.casefold()


def _task_cache_key(value: object) -> str:
    return str(value or "").strip().upper()


def _escaped_contains(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_"
    ) + "%"


def _is_microscopy_project(value: dict[str, Any]) -> bool:
    return _normalized_fact(value.get("check_item_name")) in {
        _normalized_fact(alias) for alias in ELECTRON_PROJECT_NAME_ALIASES
    }


def _public_identifier(value: object) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized if PUBLIC_ID_PATTERN.fullmatch(normalized) else None


def _snapshot_contract_is_current(snapshot: object) -> bool:
    """Only expose snapshots that satisfy the current public identity contract."""

    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("schema_version") != TASK_SNAPSHOT_SCHEMA_VERSION:
        return False
    projects = snapshot.get("projects")
    if not isinstance(projects, list):
        return False
    occupied_numbers = snapshot.get("special_wool_occupied_numbers")
    if not isinstance(occupied_numbers, list) or not all(
        isinstance(value, str) for value in occupied_numbers
    ):
        return False
    for project in projects:
        if not isinstance(project, dict):
            return False
        register_count = project.get("register_count")
        if (
            not isinstance(register_count, int)
            or isinstance(register_count, bool)
            or register_count < 0
        ):
            return False
        if _is_microscopy_project(project) and not all(
            _public_identifier(project.get(key))
            for key in ("task_check_item_id", "check_item_id")
        ):
            return False
    return True


def request_task_snapshot_refresh(
    db: Session,
    *,
    inspection_number: str,
    force: bool = False,
) -> tuple[ExecutionTaskSnapshotCache, bool]:
    """Queue one read-only legacy lookup without duplicating active requests."""

    number = _task_cache_key(inspection_number)
    if not _is_complete_inspection_number(number):
        raise ExecutionApiError(
            422,
            "inspection_number_incomplete",
            "请先输入完整检验编号后再读取旧系统任务信息",
        )
    now = utcnow()
    row = db.get(ExecutionTaskSnapshotCache, number)
    if row is None:
        row = ExecutionTaskSnapshotCache(
            inspection_number=number,
            status="queued",
            snapshot={},
            refresh_requested_at=now,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            return row, True
        except IntegrityError:
            row = db.get(ExecutionTaskSnapshotCache, number)
            if row is None:
                raise

    claim_active = (
        row.status == "running"
        and _aware(row.claim_expires_at) is not None
        and _aware(row.claim_expires_at) > now
    )
    already_queued = row.status == "queued"
    fresh = _aware(row.expires_at) is not None and _aware(row.expires_at) > now
    # An explicit refresh may bypass a fresh value, but must never revoke a
    # Bridge claim or duplicate an already queued lookup.
    if claim_active or already_queued or (fresh and not force):
        return row, False
    row.status = "queued"
    row.refresh_requested_at = now
    row.claimed_by = None
    row.claim_token = None
    row.claim_expires_at = None
    row.error_code = None
    row.error_message = None
    row.revision += 1
    db.flush()
    return row, True


def cached_task_snapshot(
    db: Session,
    *,
    inspection_number: str,
) -> dict[str, Any]:
    """Return cached facts and stale-while-refresh state for recommendations."""

    number = _task_cache_key(inspection_number)
    if not number:
        return {
            "cache_state": "empty",
            "refresh_status": "idle",
            "snapshot": None,
            "refresh_queued": False,
        }
    if not _is_complete_inspection_number(number):
        return {
            "cache_state": "incomplete_number",
            "refresh_status": "idle",
            "snapshot": None,
            "refresh_queued": False,
        }
    row = db.get(ExecutionTaskSnapshotCache, number)
    queued = False
    if row is None:
        row, queued = request_task_snapshot_refresh(
            db, inspection_number=number
        )
    now = utcnow()
    stored_snapshot = dict(row.snapshot or {}) or None
    snapshot = (
        stored_snapshot
        if _snapshot_contract_is_current(stored_snapshot)
        else None
    )
    expires_at = _aware(row.expires_at)
    retry_at = _aware(row.updated_at or row.refresh_requested_at) + timedelta(
        seconds=max(10, int(settings.EXECUTION_TASK_SNAPSHOT_RETRY_SECONDS))
    )
    retry_ready = retry_at <= now
    contract_refresh_required = stored_snapshot is not None and snapshot is None
    if contract_refresh_required and row.status not in {"queued", "running"} and (
        row.status != "failed" or retry_ready
    ):
        row, queued = request_task_snapshot_refresh(
            db, inspection_number=number, force=True
        )
    if snapshot is not None and expires_at is not None and expires_at > now:
        state = "ready"
    elif snapshot is not None:
        state = "stale_error" if row.status == "failed" else "stale"
        if row.status not in {"queued", "running"} and (
            row.status != "failed" or retry_ready
        ):
            row, queued = request_task_snapshot_refresh(
                db, inspection_number=number, force=True
            )
    elif row.status == "failed":
        state = "failed"
        if retry_ready:
            row, queued = request_task_snapshot_refresh(
                db, inspection_number=number, force=True
            )
    else:
        state = "pending"
    return {
        "cache_state": state,
        # ``cache_state`` describes whether a usable snapshot is available;
        # ``refresh_status`` describes the Bridge queue itself.  Keeping both
        # prevents a queued request from being presented as actively reading.
        "refresh_status": row.status,
        "snapshot": snapshot,
        "refresh_queued": queued,
        "revision": row.revision,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "error_code": row.error_code,
    }


def claim_task_snapshot_refresh(
    db: Session,
    *,
    bridge_id: str,
    lease_seconds: int = 180,
) -> Optional[ExecutionTaskSnapshotCache]:
    now = utcnow()
    row = (
        db.query(ExecutionTaskSnapshotCache)
        .filter(
            or_(
                ExecutionTaskSnapshotCache.status == "queued",
                (
                    (ExecutionTaskSnapshotCache.status == "running")
                    & (ExecutionTaskSnapshotCache.claim_expires_at <= now)
                ),
            )
        )
        .order_by(ExecutionTaskSnapshotCache.refresh_requested_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        return None
    row.status = "running"
    row.claimed_by = bridge_id
    row.claim_token = str(uuid4())
    row.claim_expires_at = now + timedelta(seconds=max(30, lease_seconds))
    row.error_code = None
    row.error_message = None
    db.flush()
    return row


def _normalize_snapshot(
    inspection_number: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    projects = snapshot.get("projects")
    if not isinstance(projects, list):
        raise ExecutionApiError(
            422, "task_snapshot_invalid", "任务快照缺少检测项目列表"
        )
    normalized_projects: list[dict[str, Any]] = []
    for index, value in enumerate(projects):
        if not isinstance(value, dict):
            raise ExecutionApiError(
                422, "task_snapshot_invalid", "任务快照中的检测项目格式无效"
            )
        check_item_no = value.get("check_item_no")
        check_item_name = str(value.get("check_item_name") or "").strip()
        check_method = str(
            value.get("check_method") or value.get("test_method") or ""
        ).strip()
        register_count = value.get("register_count")
        if (
            not isinstance(register_count, int)
            or isinstance(register_count, bool)
            or register_count < 0
        ):
            raise ExecutionApiError(
                422,
                "task_snapshot_invalid",
                "任务快照中的检测项目缺少有效的当前登记数量",
            )
        project_key = str(value.get("project_key") or "").strip()
        if not project_key:
            identity = "\0".join(
                str(item or "").strip()
                for item in (
                    check_item_no,
                    check_item_name,
                    check_method,
                    value.get("check_count"),
                    index,
                )
            )
            project_key = (
                "task-project:"
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            )
        task_check_item_id = _public_identifier(value.get("task_check_item_id"))
        check_item_id = _public_identifier(value.get("check_item_id"))
        if _is_microscopy_project(value) and not (
            task_check_item_id and check_item_id
        ):
            raise ExecutionApiError(
                422,
                "task_snapshot_project_identity_missing",
                "纤维微观形貌任务项目缺少完整的脱敏项目标识，请刷新旧系统任务信息",
            )
        normalized_projects.append(
            {
                "project_key": project_key[:100],
                "task_check_item_id": task_check_item_id,
                "check_item_id": check_item_id,
                "check_item_no": check_item_no,
                "check_item_name": check_item_name,
                "check_method": check_method,
                "check_count": value.get("check_count"),
                "register_count": register_count,
                "seq_num": value.get("seq_num"),
                "sample_identify": value.get("sample_identify"),
                "remark": value.get("remark"),
                "give_judgement": value.get("give_judgement"),
            }
        )
    raw_names = snapshot.get("sample_names")
    if not isinstance(raw_names, list):
        raw_names = [snapshot.get("sample_name")]
    sample_names: list[str] = []
    seen_names: set[str] = set()
    for value in raw_names:
        name = " ".join(str(value or "").strip().split())
        if name and name.casefold() not in seen_names:
            sample_names.append(name)
            seen_names.add(name.casefold())
    explicit_sample_name = " ".join(
        str(snapshot.get("sample_name") or "").strip().split()
    )
    if explicit_sample_name and explicit_sample_name.casefold() not in seen_names:
        sample_names.insert(0, explicit_sample_name)
    base_number = inspection_number.split("-", 1)[0]
    family_pattern = re.compile(
        re.escape(base_number) + r"(?:-([1-9][0-9]*))?$"
    )
    raw_occupied_numbers = snapshot.get("special_wool_occupied_numbers")
    if not isinstance(raw_occupied_numbers, list):
        raise ExecutionApiError(
            422,
            "task_snapshot_invalid",
            "任务快照缺少特种毛编号族占用事实",
        )
    occupied_numbers: list[str] = []
    seen_occupied: set[str] = set()
    for raw_value in raw_occupied_numbers:
        value = str(raw_value or "").strip().upper()
        if not family_pattern.fullmatch(value):
            raise ExecutionApiError(
                422,
                "task_snapshot_invalid",
                "任务快照中的特种毛编号族格式无效",
            )
        if value not in seen_occupied:
            occupied_numbers.append(value)
            seen_occupied.add(value)
    return {
        "schema_version": TASK_SNAPSHOT_SCHEMA_VERSION,
        "inspection_number": inspection_number,
        "sample_name": (
            explicit_sample_name
            if explicit_sample_name
            else sample_names[0]
            if len(sample_names) == 1
            else None
        ),
        "sample_names": sample_names,
        "check_basis": snapshot.get("check_basis"),
        "projects": normalized_projects,
        "special_wool_occupied_numbers": occupied_numbers,
    }


def complete_task_snapshot_refresh(
    db: Session,
    *,
    inspection_number: str,
    bridge_id: str,
    claim_token: str,
    snapshot: dict[str, Any],
) -> ExecutionTaskSnapshotCache:
    inspection_number = _task_cache_key(inspection_number)
    row = (
        db.query(ExecutionTaskSnapshotCache)
        .filter(
            ExecutionTaskSnapshotCache.inspection_number == inspection_number
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        row is None
        or row.status != "running"
        or row.claimed_by != bridge_id
        or row.claim_token != claim_token
        or _aware(row.claim_expires_at) is None
        or _aware(row.claim_expires_at) <= utcnow()
    ):
        raise conflict(
            "task_snapshot_claim_lost",
            "任务快照读取租约已失效，请重新领取",
            inspection_number=inspection_number,
        )
    now = utcnow()
    row.snapshot = _normalize_snapshot(inspection_number, snapshot)
    row.status = "ready"
    row.fetched_at = now
    row.expires_at = now + timedelta(
        minutes=max(1, int(settings.EXECUTION_TASK_SNAPSHOT_TTL_MINUTES))
    )
    row.claimed_by = None
    row.claim_token = None
    row.claim_expires_at = None
    row.error_code = None
    row.error_message = None
    row.revision += 1
    db.flush()
    return row


def fail_task_snapshot_refresh(
    db: Session,
    *,
    inspection_number: str,
    bridge_id: str,
    claim_token: str,
    error_code: str,
    message: str,
) -> ExecutionTaskSnapshotCache:
    inspection_number = _task_cache_key(inspection_number)
    row = (
        db.query(ExecutionTaskSnapshotCache)
        .filter(
            ExecutionTaskSnapshotCache.inspection_number == inspection_number
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        row is None
        or row.status != "running"
        or row.claimed_by != bridge_id
        or row.claim_token != claim_token
        or _aware(row.claim_expires_at) is None
        or _aware(row.claim_expires_at) <= utcnow()
    ):
        raise conflict(
            "task_snapshot_claim_lost",
            "任务快照读取租约已失效，请重新领取",
            inspection_number=inspection_number,
        )
    row.status = "failed"
    row.claimed_by = None
    row.claim_token = None
    row.claim_expires_at = None
    row.error_code = str(error_code or "task_snapshot_read_failed")[:100]
    row.error_message = str(message or "旧系统任务信息读取失败")[:1000]
    row.revision += 1
    db.flush()
    return row


def _folder_id(relative_folder: str) -> str:
    digest = hashlib.sha256(
        f"{ELECTRON_ROOT_ID}\0{relative_folder}".encode("utf-8")
    ).hexdigest()[:32]
    return f"electron-folder:{digest}"


def _indexed_electron_images(
    db: Session,
    *,
    inspection_number: str,
) -> dict[str, Any]:
    root = (
        db.query(ExecutionStorageRoot)
        .filter(ExecutionStorageRoot.root_id == ELECTRON_ROOT_ID)
        .one_or_none()
    )
    if root is None or not root.is_active or not root.is_available:
        return {
            "index_state": "unavailable",
            "folders": [],
            "images": [],
            "folder_match_count": 0,
            "image_count": 0,
            "truncated": False,
        }
    number = str(inspection_number or "").strip()
    if not number:
        return {
            "index_state": "ready",
            "folders": [],
            "images": [],
            "folder_match_count": 0,
            "image_count": 0,
            "truncated": False,
        }
    base_query = db.query(ExecutionFileIndexEntry).filter(
        ExecutionFileIndexEntry.storage_root_id == root.id,
        ExecutionFileIndexEntry.missing_since.is_(None),
        ExecutionFileIndexEntry.extension.in_(sorted(ELECTRON_IMAGE_SUFFIXES)),
    )
    # Prefer rows whose first path component is the numbered result folder.
    # Filtering this in SQL before LIMIT prevents many filename-only matches in
    # archival folders from displacing older images in the actual result folder.
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        first_component = func.split_part(
            ExecutionFileIndexEntry.relative_path, "/", 1
        )
    else:
        first_slash = func.instr(ExecutionFileIndexEntry.relative_path, "/")
        first_component = func.substr(
            ExecutionFileIndexEntry.relative_path, 1, first_slash - 1
        )

    def ordered_rows(query):
        return (
            query.order_by(
                ExecutionFileIndexEntry.modified_at.desc(),
                ExecutionFileIndexEntry.relative_path.asc(),
            )
            .limit(MAX_INDEXED_IMAGES + 1)
            .all()
        )

    rows = ordered_rows(
        base_query.filter(
            first_component.ilike(_escaped_contains(number), escape="\\")
        )
    )
    if not rows:
        # Some historical layouts put the number only in a direct image name
        # or a deeper section. Keep that fallback, but only when no numbered
        # top-level result folder exists.
        rows = ordered_rows(
            base_query.filter(
                ExecutionFileIndexEntry.relative_path.ilike(
                    _escaped_contains(number), escape="\\"
                )
            )
        )
    truncated = len(rows) > MAX_INDEXED_IMAGES
    rows = rows[:MAX_INDEXED_IMAGES]
    folder_rows: dict[str, list[ExecutionFileIndexEntry]] = {}
    fallback: list[ExecutionFileIndexEntry] = []
    folded_number = number.casefold()
    for entry in rows:
        parts = PurePosixPath(entry.relative_path).parts
        if len(parts) >= 2 and folded_number in parts[0].casefold():
            folder_rows.setdefault(parts[0], []).append(entry)
        elif folded_number in entry.relative_path.casefold():
            fallback.append(entry)
    chosen = [entry for values in folder_rows.values() for entry in values]
    if not folder_rows:
        chosen = fallback

    folders = []
    for folder_name, entries in folder_rows.items():
        folders.append(
            {
                "id": _folder_id(folder_name),
                "name": folder_name,
                "relative_path": folder_name,
                "image_count": len(entries),
                "modified_at": max(item.modified_at for item in entries).isoformat(),
            }
        )
    folders.sort(
        key=lambda item: (
            item["name"].casefold() != folded_number,
            item["name"].casefold(),
        )
    )
    folder_id_by_name = {
        item["relative_path"]: item["id"] for item in folders
    }
    images = []
    for entry in chosen:
        parts = PurePosixPath(entry.relative_path).parts
        folder_name = parts[0] if len(parts) >= 2 else None
        images.append(
            {
                "id": entry.id,
                "root_id": ELECTRON_ROOT_ID,
                "relative_path": entry.relative_path,
                "name": entry.filename,
                "suffix": entry.extension,
                "size": entry.size_bytes,
                "modified_at": entry.modified_at.isoformat(),
                "fingerprint": entry.fingerprint,
                "folder_id": folder_id_by_name.get(folder_name),
                "folder_name": folder_name,
                "section_path": "/".join(parts[1:-1]) if len(parts) > 2 else "",
                "preview_url": f"/api/execution/v1/files/index/{entry.id}/preview",
            }
        )
    return {
        "index_state": "ready",
        "folders": folders,
        "images": images,
        "folder_match_count": len(folders),
        "image_count": len(images),
        "truncated": truncated,
    }


def _task_project_conditions(snapshot: Optional[dict[str, Any]]) -> list[str]:
    best: list[str] = []
    for project in (snapshot or {}).get("projects") or []:
        if not isinstance(project, dict):
            continue
        conditions: list[str] = []
        if _normalized_fact(project.get("check_item_name")) in {
            _normalized_fact(value) for value in ELECTRON_PROJECT_NAME_ALIASES
        }:
            conditions.append("task_item_name")
        if _normalized_fact(project.get("check_method")) == _normalized_fact(
            ELECTRON_TEST_METHOD
        ):
            conditions.append("test_method")
        if len(conditions) > len(best):
            best = conditions
        if len(best) == 2:
            break
    return best


def task_snapshot_status(
    db: Session,
    *,
    inspection_number: str,
) -> dict[str, Any]:
    """Return a small, polling-safe status contract for the human task UI.

    Task conditions are evaluated against every built-in flow's project
    rules (electron microscopy and GB/T 4688 paper fibre) and the best
    match wins, so a paper-fibre task no longer reports its item name and
    test method as missing just because they are not microscopy aliases.
    """

    cached = cached_task_snapshot(db, inspection_number=inspection_number)
    snapshot = cached.get("snapshot")
    matched = _task_project_conditions(snapshot)
    from app.execution.paper_fiber import paper_task_project_match

    paper_conditions, _matched_project = paper_task_project_match(snapshot)
    if len(paper_conditions) > len(matched):
        matched = paper_conditions
    missing = [
        item
        for item in ("task_item_name", "test_method")
        if item not in matched
    ]
    return {
        "inspection_number": _task_cache_key(inspection_number),
        "cache_state": cached["cache_state"],
        "refresh_status": cached.get("refresh_status", "idle"),
        "snapshot_available": cached.get("snapshot") is not None,
        "matched_conditions": matched,
        "missing_conditions": missing,
        "revision": cached.get("revision"),
        "fetched_at": cached.get("fetched_at"),
        "expires_at": cached.get("expires_at"),
        "error_code": cached.get("error_code"),
        "remote_write_performed": False,
    }


def electron_microscopy_match(
    db: Session,
    *,
    inspection_number: str,
) -> dict[str, Any]:
    images = _indexed_electron_images(db, inspection_number=inspection_number)
    task = cached_task_snapshot(db, inspection_number=inspection_number)
    conditions: list[str] = []
    root_ready = images["index_state"] == "ready"
    if root_ready:
        conditions.append("source_root")
    if images["folder_match_count"] > 0:
        conditions.append("folder")
    conditions.extend(_task_project_conditions(task.get("snapshot")))
    full_match = all(
        item in conditions
        for item in ("source_root", "folder", "task_item_name", "test_method")
    )
    return {
        **images,
        "matched_conditions": conditions,
        "full_match": full_match,
        "task_cache_state": task["cache_state"],
        "task_snapshot": task.get("snapshot"),
        "cache_updated": bool(task.get("refresh_queued")),
    }


def _electron_microscopy_executor(context) -> dict[str, Any]:
    match = electron_microscopy_match(
        context.db,
        inspection_number=str(
            context.input_data.get("inspection_number")
            or context.run.inspection_number
        ),
    )
    missing = [
        item
        for item in ("source_root", "folder", "task_item_name", "test_method")
        if item not in match["matched_conditions"]
    ]
    require_full_task_match = bool(
        (context.node.get("config") or {}).get("require_full_task_match", False)
    )
    if missing and require_full_task_match:
        raise ExecutionApiError(
            422,
            "electron_microscopy_rule_not_matched",
            "当前编号不满足纤维微观形貌 GB/T 36422-2018 流程条件",
            details={"missing_conditions": missing},
        )
    if not match["images"]:
        raise ExecutionApiError(
            422,
            "electron_microscopy_images_not_found",
            "匹配目录中没有可选择的图片",
        )
    return {
        "folders": match["folders"],
        "images": match["images"],
        "folder_selection_required": len(match["folders"]) > 1,
        "selected_folder_ids": (
            [match["folders"][0]["id"]] if len(match["folders"]) == 1 else []
        ),
        "image_count": len(match["images"]),
        "truncated": match["truncated"],
        "task": match["task_snapshot"],
        "task_validation_state": (
            "matched"
            if not missing
            else (
                "pending"
                if match["task_cache_state"] in {"pending", "empty"}
                else "warning"
            )
        ),
        "task_cache_state": match["task_cache_state"],
        "missing_conditions": missing,
    }


def register_electron_microscopy_executors() -> None:
    node_registry.set_executor(
        ELECTRON_NODE_TYPE, 1, _electron_microscopy_executor
    )
