from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.indexing import (
    IncrementalFileIndexer,
    IndexedFile,
    group_electron_microscopy_files,
)
from app.execution.index_metadata import (
    METADATA_VERSION,
    extract_index_metadata,
    inspection_numbers_in_text,
)
from app.execution.models import (
    ExecutionFileIndexEntry,
    ExecutionIndexJob,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    utcnow,
)
from app.execution.registry import node_registry
from app.execution.storage import ArtifactRef, FileGateway, StorageRoot


ROOT_LAYOUT = (
    (
        "special_wool_records",
        "特种毛原始记录",
        "2026-特种毛",
        "special_wool",
        "read",
    ),
    (
        "regenerated_fiber_records",
        "再生纤原始记录",
        "2026-再生纤",
        "regenerated_fiber",
        "read",
    ),
    (
        "hemp_cotton_records",
        "麻棉原始记录",
        "2026-麻棉",
        "hemp_cotton",
        "read",
    ),
    (
        "electron_microscopy_records",
        "电镜原始资料",
        "2026-电镜",
        "electron_microscopy",
        "read",
    ),
    (
        "paper_fiber_records",
        "纸、纸板和纸浆纤维鉴别分析原始记录",
        "08-其他/2022-纸、纸板和纸浆纤维鉴别分析",
        "other",
        "read",
    ),
)


def ensure_storage_roots(db: Session) -> None:
    source_root = Path(
        str(getattr(settings, "EXECUTION_SOURCE_ROOT", "/data/execution/source"))
    )
    runtime_root = Path(
        str(getattr(settings, "EXECUTION_RUNTIME_ROOT", "/data/execution/runtime"))
    )
    publish_root = Path(
        str(getattr(settings, "EXECUTION_PUBLISH_ROOT", "/data/execution/publish"))
    )
    definitions = [
        *[
            (
                root_id,
                name,
                source_root / folder,
                category,
                access_mode,
            )
            for root_id, name, folder, category, access_mode in ROOT_LAYOUT
        ],
        ("execution_staging", "执行暂存区", runtime_root, None, "write"),
        ("execution_publish", "执行发布区", publish_root, None, "publish"),
    ]
    existing = {
        root.root_id: root for root in db.query(ExecutionStorageRoot).all()
    }
    for root_id, name, path, category, access_mode in definitions:
        available = path.exists() and path.is_dir()
        root = existing.get(root_id)
        if root is None:
            root = ExecutionStorageRoot(
                root_id=root_id,
                name=name,
                local_path=str(path),
                access_mode=access_mode,
                category_key=category,
            )
            db.add(root)
        else:
            # Paths are deployment configuration, never accepted from workflow
            # JSON or a user request.
            root.local_path = str(path)
            root.access_mode = access_mode
            root.category_key = category
        root.is_available = available
        root.availability_message = None if available else "目录尚未挂载或不存在"
    db.flush()
    availability_by_root = {
        root.root_id: root.is_available
        for root in db.query(ExecutionStorageRoot).all()
    }
    for workflow in db.query(ExecutionWorkflow).all():
        published_definition = next(
            (
                version.definition
                for version in workflow.versions
                if version.version_number
                == workflow.published_version_number
            ),
            None,
        )
        effective_definition = (
            published_definition
            if published_definition is not None
            else workflow.draft_definition
        )
        slots = (effective_definition or {}).get("root_slots") or []
        source_root_ids = [
            slot.get("root_id")
            for slot in slots
            if isinstance(slot, dict) and slot.get("access", "read") == "read"
        ]
        if not source_root_ids:
            continue
        missing = [
            root_id
            for root_id in source_root_ids
            if not availability_by_root.get(root_id, False)
        ]
        if missing:
            workflow.availability_code = "root_not_configured"
            workflow.availability_message = (
                f"数据根未配置：{', '.join(missing)}"
            )
        elif workflow.availability_code == "root_not_configured":
            workflow.availability_code = None
            workflow.availability_message = None


def build_file_gateway(db: Session) -> FileGateway:
    roots: list[StorageRoot] = []
    for record in (
        db.query(ExecutionStorageRoot)
        .filter(
            ExecutionStorageRoot.is_active.is_(True),
            ExecutionStorageRoot.is_available.is_(True),
        )
        .all()
    ):
        roots.append(
            StorageRoot(
                root_id=record.root_id,
                path=Path(record.local_path),
                writable=record.access_mode in {"write", "publish"},
                publishable=record.access_mode == "publish",
            )
        )
    if not roots:
        raise ExecutionApiError(
            503,
            "storage_unavailable",
            "执行系统文件根目录均不可用",
        )
    return FileGateway(roots)


def storage_root_by_key(db: Session, root_id: str) -> ExecutionStorageRoot:
    root = (
        db.query(ExecutionStorageRoot)
        .filter(ExecutionStorageRoot.root_id == root_id)
        .one_or_none()
    )
    if root is None:
        raise not_found("文件根目录", root_id)
    return root


def queue_refresh(
    db: Session,
    *,
    root_id: str,
    actor: ExecutionUser,
    max_depth: Optional[int] = 1,
) -> tuple[ExecutionIndexJob, bool]:
    root = storage_root_by_key(db, root_id)
    if root_id == "electron_microscopy_records":
        # Numbered result folders can contain project-specific subfolders at
        # arbitrary depth. This remains a background scan; user searches never
        # walk the shared directory synchronously.
        max_depth = None
    if root.access_mode != "read":
        raise ExecutionApiError(
            403,
            "storage_root_not_indexable",
            "该文件根目录不允许通过文件索引接口查询或刷新",
            details={"root_id": root_id},
        )
    if not root.is_available:
        raise ExecutionApiError(
            409,
            "storage_root_unavailable",
            root.availability_message or "文件根目录不可用",
            details={"root_id": root_id},
        )
    existing = (
        db.query(ExecutionIndexJob)
        .filter(
            ExecutionIndexJob.storage_root_id == root.id,
            ExecutionIndexJob.status.in_(["queued", "running"]),
        )
        .order_by(ExecutionIndexJob.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing, True
    job = ExecutionIndexJob(
        storage_root_id=root.id,
        requested_by_id=actor.id,
        max_depth=max_depth,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
        return job, False
    except IntegrityError:
        # The partial unique index is the final arbiter when two callers race
        # after the optimistic lookup above.
        existing = (
            db.query(ExecutionIndexJob)
            .filter(
                ExecutionIndexJob.storage_root_id == root.id,
                ExecutionIndexJob.status.in_(["queued", "running"]),
            )
            .order_by(ExecutionIndexJob.created_at.desc())
            .one()
        )
        return existing, True


def enqueue_due_index_jobs(
    db: Session,
    *,
    interval_seconds: Optional[int] = None,
) -> int:
    interval = int(
        interval_seconds
        if interval_seconds is not None
        else getattr(settings, "EXECUTION_INDEX_INTERVAL_SECONDS", 300)
    )
    cutoff = utcnow() - timedelta(seconds=max(interval, 10))
    auto_root_ids = settings.execution_auto_index_root_ids()
    if not auto_root_ids:
        return 0
    created = 0
    roots = (
        db.query(ExecutionStorageRoot)
        .filter(
            ExecutionStorageRoot.is_active.is_(True),
            ExecutionStorageRoot.is_available.is_(True),
            ExecutionStorageRoot.access_mode == "read",
            ExecutionStorageRoot.root_id.in_(auto_root_ids),
        )
        .all()
    )
    for root in roots:
        if (
            root.last_scan_finished_at is not None
            and root.last_scan_finished_at.tzinfo is None
        ):
            last_finished = root.last_scan_finished_at.replace(tzinfo=timezone.utc)
        else:
            last_finished = root.last_scan_finished_at
        if last_finished is not None and last_finished > cutoff:
            continue
        active = (
            db.query(ExecutionIndexJob.id)
            .filter(
                ExecutionIndexJob.storage_root_id == root.id,
                ExecutionIndexJob.status.in_(["queued", "running"]),
            )
            .first()
        )
        if active is None:
            try:
                with db.begin_nested():
                    db.add(
                        ExecutionIndexJob(
                            storage_root_id=root.id,
                            status="queued",
                            max_depth=(
                                None
                                if root.root_id == "electron_microscopy_records"
                                else 1
                            ),
                        )
                    )
                    db.flush()
                created += 1
            except IntegrityError:
                # Another worker scheduled the same root concurrently.
                pass
    if created:
        db.flush()
    return created


def claim_index_job(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> Optional[ExecutionIndexJob]:
    now = utcnow()
    job = (
        db.query(ExecutionIndexJob)
        .filter(
            or_(
                ExecutionIndexJob.status == "queued",
                (
                    (ExecutionIndexJob.status == "running")
                    & (ExecutionIndexJob.lease_expires_at < now)
                ),
            )
        )
        .order_by(ExecutionIndexJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if job is None:
        return None
    job.status = "running"
    job.lease_owner = worker_id
    job.lease_token = str(uuid4())
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    root = db.get(ExecutionStorageRoot, job.storage_root_id)
    if root is not None:
        root.last_scan_started_at = now
    db.flush()
    return job


def renew_index_job_lease(
    db: Session,
    *,
    job_id: str,
    lease_token: str,
    lease_seconds: int = 300,
) -> bool:
    updated = (
        db.query(ExecutionIndexJob)
        .filter(
            ExecutionIndexJob.id == job_id,
            ExecutionIndexJob.status == "running",
            ExecutionIndexJob.lease_token == lease_token,
        )
        .update(
            {
                "lease_expires_at": utcnow()
                + timedelta(seconds=max(lease_seconds, 30))
            },
            synchronize_session=False,
        )
    )
    return updated == 1


def _fingerprint(item: IndexedFile) -> str:
    return f"{item.size}:{item.modified_ns}"


def _extract_inspection_number(
    item: IndexedFile,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    filename_matches = inspection_numbers_in_text(Path(item.name).stem)
    if filename_matches:
        return filename_matches[0]
    internal = (metadata or {}).get("internal_inspection_numbers")
    if isinstance(internal, list) and internal:
        return str(internal[0])
    return None


def persist_scan(
    db: Session,
    *,
    root: ExecutionStorageRoot,
    files: Iterable[IndexedFile],
    scan_complete: bool,
    scan_max_depth: Optional[int] = None,
    gateway: Optional[FileGateway] = None,
) -> tuple[int, int, int]:
    root.scan_generation += 1
    generation = root.scan_generation
    existing = {
        row.relative_path: row
        for row in db.query(ExecutionFileIndexEntry)
        .filter(ExecutionFileIndexEntry.storage_root_id == root.id)
        .all()
    }
    added = 0
    updated = 0
    seen_paths: set[str] = set()
    for item in files:
        seen_paths.add(item.ref.relative_path)
        row = existing.get(item.ref.relative_path)
        fingerprint = _fingerprint(item)
        needs_metadata = (
            row is None
            or row.fingerprint != fingerprint
            or (row.metadata_json or {}).get("metadata_version")
            != METADATA_VERSION
        )
        metadata = dict(row.metadata_json or {}) if row is not None else {}
        if needs_metadata:
            if root.root_id in {
                "regenerated_fiber_records",
                "paper_fiber_records",
            }:
                # These shared directories can contain many historical
                # workbooks. The background scan remains metadata-only;
                # specialized nodes open only index-narrowed candidates on
                # demand.
                metadata = {
                    "metadata_version": METADATA_VERSION,
                    "expected_category": root.category_key,
                    "format": item.suffix,
                    "kind": (
                        "workbook"
                        if item.suffix
                        in {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".xltm"}
                        else "file"
                    ),
                    "parse_status": "deferred",
                }
            else:
                try:
                    path = (
                        gateway.resolve(item.ref, expected_type="file")
                        if gateway is not None
                        else Path(root.local_path).joinpath(
                            *item.ref.relative_path.split("/")
                        )
                    )
                    metadata = extract_index_metadata(
                        path,
                        expected_category=root.category_key,
                    )
                except Exception as exc:
                    metadata = {
                        "metadata_version": METADATA_VERSION,
                        "expected_category": root.category_key,
                        "parse_status": "failed",
                        "parse_error": f"{type(exc).__name__}: {exc}"[:500],
                    }
        metadata["category"] = item.category
        inspection_number = _extract_inspection_number(item, metadata)
        if row is None:
            row = ExecutionFileIndexEntry(
                storage_root_id=root.id,
                relative_path=item.ref.relative_path,
                filename=item.name,
                extension=item.suffix,
                file_kind="file",
                inspection_number=inspection_number,
                group_key=str(Path(item.ref.relative_path).with_suffix("")).casefold(),
                size_bytes=item.size,
                modified_at=item.modified_at,
                fingerprint=fingerprint,
                metadata_json=metadata,
                scan_generation=generation,
            )
            db.add(row)
            added += 1
        else:
            if row.fingerprint != fingerprint or row.missing_since is not None:
                updated += 1
            row.filename = item.name
            row.extension = item.suffix
            row.inspection_number = inspection_number
            row.group_key = str(Path(item.ref.relative_path).with_suffix("")).casefold()
            row.size_bytes = item.size
            row.modified_at = item.modified_at
            row.fingerprint = fingerprint
            row.metadata_json = metadata
            row.scan_generation = generation
            row.indexed_at = utcnow()
            row.missing_since = None
    removed = 0
    if scan_complete:
        for relative_path, row in existing.items():
            file_depth = max(len(Path(relative_path).parts) - 1, 0)
            inside_scanned_scope = (
                scan_max_depth is None or file_depth <= scan_max_depth
            )
            if (
                inside_scanned_scope
                and relative_path not in seen_paths
                and row.missing_since is None
            ):
                row.missing_since = utcnow()
                removed += 1
    return added, updated, removed


def process_index_job(db: Session, job_id: str, lease_token: str) -> None:
    job = db.get(ExecutionIndexJob, job_id)
    if (
        job is None
        or job.status != "running"
        or job.lease_token != lease_token
    ):
        raise conflict("index_job_lease_lost", "索引任务租约已失效")
    root = db.get(ExecutionStorageRoot, job.storage_root_id)
    if root is None:
        raise not_found("文件根目录", job.storage_root_id)
    try:
        gateway = build_file_gateway(db)
        indexer = IncrementalFileIndexer(gateway)
        result = indexer.scan_incremental(root.root_id, max_depth=job.max_depth)
        snapshot = indexer.snapshot(root_ids=[root.root_id])
        job = (
            db.query(ExecutionIndexJob)
            .filter(ExecutionIndexJob.id == job_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if (
            job is None
            or job.status != "running"
            or job.lease_token != lease_token
        ):
            raise conflict("index_job_lease_lost", "索引任务租约已失效")
        added, updated, removed = persist_scan(
            db,
            root=root,
            files=snapshot,
            scan_complete=not result.errors,
            scan_max_depth=job.max_depth,
            gateway=gateway,
        )
        job.added_count = added
        job.updated_count = updated
        job.removed_count = removed
        job.total_count = result.total_count
        job.errors = list(result.errors)
        job.status = "completed" if not result.errors else "completed_with_errors"
        root.is_available = True
        root.availability_message = None
        root.last_scan_error = "\n".join(result.errors) if result.errors else None
    except ExecutionApiError:
        raise
    except Exception as exc:
        job.status = "failed"
        job.errors = [str(exc)]
        root.is_available = Path(root.local_path).is_dir()
        root.availability_message = None if root.is_available else "目录尚未挂载或不存在"
        root.last_scan_error = str(exc)
    finally:
        now = utcnow()
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        root.last_scan_finished_at = now


def search_index(
    db: Session,
    *,
    inspection_number: str,
    root_ids: Optional[list[str]] = None,
    category_keys: Optional[list[str]] = None,
    recent_days: Optional[int] = 7,
    limit: int = 6,
) -> list[dict[str, Any]]:
    query = inspection_number.strip()
    if not query:
        return []
    like_query = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    like_pattern = f"%{like_query}%"
    statement = (
        db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionFileIndexEntry.storage_root_id,
        )
        .filter(
            ExecutionFileIndexEntry.missing_since.is_(None),
            ExecutionStorageRoot.is_active.is_(True),
            ExecutionStorageRoot.access_mode == "read",
            or_(
                ExecutionFileIndexEntry.inspection_number.ilike(
                    like_pattern,
                    escape="\\",
                ),
                ExecutionFileIndexEntry.filename.ilike(
                    like_pattern,
                    escape="\\",
                ),
                ExecutionFileIndexEntry.relative_path.ilike(
                    like_pattern,
                    escape="\\",
                ),
            ),
        )
    )
    if root_ids:
        statement = statement.filter(ExecutionStorageRoot.root_id.in_(root_ids))
    if category_keys:
        statement = statement.filter(
            ExecutionStorageRoot.category_key.in_(category_keys)
        )
    if recent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
        statement = statement.filter(ExecutionFileIndexEntry.modified_at >= cutoff)
    rows = (
        statement.order_by(ExecutionFileIndexEntry.modified_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": entry.id,
            "root_id": root.root_id,
            "relative_path": entry.relative_path,
            "name": entry.filename,
            "suffix": entry.extension,
            "size": entry.size_bytes,
            "modified_at": entry.modified_at.isoformat(),
            "category": root.category_key,
            "fingerprint": entry.fingerprint,
            "metadata": entry.metadata_json or {},
        }
        for entry, root in rows
    ]


def electron_groups_from_index(
    db: Session,
    *,
    root_ids: Optional[list[str]] = None,
    inspection_number: Optional[str] = None,
    recent_days: Optional[int] = 7,
    limit: int = 6,
) -> list[dict[str, Any]]:
    statement = (
        db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionFileIndexEntry.storage_root_id,
        )
        .filter(
            ExecutionFileIndexEntry.missing_since.is_(None),
            ExecutionStorageRoot.is_active.is_(True),
            ExecutionStorageRoot.access_mode == "read",
            ExecutionFileIndexEntry.extension.in_([".sif", ".bmp", ".txt"]),
        )
    )
    if root_ids:
        statement = statement.filter(ExecutionStorageRoot.root_id.in_(root_ids))
    if inspection_number:
        like_query = (
            inspection_number.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        statement = statement.filter(
            ExecutionFileIndexEntry.relative_path.ilike(
                f"%{like_query}%",
                escape="\\",
            )
        )
    if recent_days is not None:
        statement = statement.filter(
            ExecutionFileIndexEntry.modified_at
            >= datetime.now(timezone.utc) - timedelta(days=recent_days)
        )
    rows = statement.all()
    indexed = [
        IndexedFile(
            ref=ArtifactRef(root.root_id, entry.relative_path),
            name=entry.filename,
            suffix=entry.extension,
            size=entry.size_bytes,
            modified_ns=int(entry.modified_at.timestamp() * 1_000_000_000),
            category=root.category_key or root.root_id,
        )
        for entry, root in rows
    ]
    entries_by_ref = {
        (root.root_id, entry.relative_path): entry
        for entry, root in rows
    }
    serialized: list[dict[str, Any]] = []
    for group in group_electron_microscopy_files(indexed)[:limit]:
        value = group.as_dict()
        identity = (
            f"{group.root_id}\0{group.parent_path}\0{group.base_name}"
        ).encode("utf-8")
        value["id"] = f"electron:{hashlib.sha256(identity).hexdigest()[:32]}"
        value["name"] = group.base_name
        value["files"] = []
        for member in (group.sif, group.bmp, group.txt):
            if member is None:
                continue
            entry = entries_by_ref[
                (member.ref.root_id, member.ref.relative_path)
            ]
            value["files"].append(
                {
                    "id": entry.id,
                    "root_id": member.ref.root_id,
                    "relative_path": member.ref.relative_path,
                    "name": entry.filename,
                    "fingerprint": entry.fingerprint,
                }
            )
        serialized.append(value)
    return serialized


def _file_query_executor(context) -> dict[str, Any]:
    config = context.node.get("config") or {}
    inspection_number = (
        context.input_data.get("inspection_number")
        or context.run.inspection_number
    )
    items = search_index(
        context.db,
        inspection_number=str(inspection_number),
        root_ids=[config["root_id"]],
        recent_days=config.get("recent_days", 7),
        limit=min(int(config.get("limit", 6)), 100),
    )
    return {"candidates": items, "count": len(items)}


def _electron_group_executor(context) -> dict[str, Any]:
    config = context.node.get("config") or {}
    groups = electron_groups_from_index(
        context.db,
        root_ids=[config["root_id"]],
        inspection_number=str(
            context.input_data.get("inspection_number")
            or context.run.inspection_number
        ),
        recent_days=config.get("recent_days", 7),
        limit=min(int(config.get("limit", 6)), 100),
    )
    return {"groups": groups, "count": len(groups)}


_EXECUTORS_REGISTERED = False


def register_persistence_executors() -> None:
    global _EXECUTORS_REGISTERED
    if _EXECUTORS_REGISTERED:
        return
    node_registry.set_executor("file.index_query", 1, _file_query_executor)
    node_registry.set_executor("electron.group", 1, _electron_group_executor)
    from app.execution.regenerated_fiber import (
        register_regenerated_fiber_executors,
    )
    from app.execution.electron_microscopy import (
        register_electron_microscopy_executors,
    )
    from app.execution.paper_fiber import register_paper_fiber_executors
    from app.execution.project_rules import register_project_rule_executors

    register_regenerated_fiber_executors()
    register_electron_microscopy_executors()
    register_paper_fiber_executors()
    register_project_rule_executors()
    _EXECUTORS_REGISTERED = True
