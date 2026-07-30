from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict
from app.execution.events import append_audit_log, append_run_event
from app.execution.models import (
    ExecutionCredential,
    ExecutionExternalOperation,
    ExecutionFileIndexEntry,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    utcnow,
)
from app.execution.persistence import build_file_gateway
from app.execution.storage import ArtifactRef, StorageError
from app.execution.workbook_format import (
    WorkbookFormat,
    detect_workbook_format,
)


LEGACY_REGENERATED_COUNT_NODE = (
    "external.legacy_regenerated_fiber_count_upload"
)
LEGACY_CONNECTOR_KEY = "legacy_fibrecheck"
LEGACY_CREDENTIAL_SYSTEM = "legacy_inspection"
LEGACY_REMOTE_MODULE = (
    "inspection_record_registration:special_fiber:inspection"
)
OPERATION_KEY_PREFIX = "legacy-regenerated-count-upload:v1"
ACTIVE_REMOTE_OPERATION_STATUSES = (
    "prepared",
    "approved",
    "in_progress",
    "reconciliation_required",
)


def _canonical_checksum(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _scoped_hash(scope: str, *parts: str) -> str:
    payload = json.dumps(
        [scope, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _account_scope_key(account_name: str) -> str:
    normalized = _normalized_identity(account_name)
    if not normalized:
        raise ExecutionApiError(
            422,
            "legacy_credential_account_missing",
            "旧检务系统凭据必须配置账号名称",
        )
    return _scoped_hash(
        "external-account-scope:v1",
        LEGACY_CONNECTOR_KEY,
        normalized,
    )


def _remote_business_key(sample_number: str) -> str:
    normalized = _normalized_identity(sample_number)
    if not normalized:
        raise ExecutionApiError(
            422,
            "external_target_sample_number_missing",
            "旧系统上传预检缺少目标样品编号",
        )
    return _scoped_hash(
        "external-remote-business:v1",
        LEGACY_CONNECTOR_KEY,
        LEGACY_REMOTE_MODULE,
        normalized,
    )


def lock_legacy_remote_business_scope(
    db: Session,
    *,
    sample_number: str,
) -> str:
    """Serialize one target sample before either operation or file row locks.

    PostgreSQL's transaction-scoped advisory lock also covers the moment before
    an operation row exists.  SQLite unit tests remain single-process and rely
    on the partial unique index as their final conflict fence.
    """

    remote_business_key = _remote_business_key(sample_number)
    if db.get_bind().dialect.name == "postgresql":
        lock_key = int.from_bytes(
            bytes.fromhex(remote_business_key)[:8],
            byteorder="big",
            signed=True,
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    return remote_business_key


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return _aware_utc(value).isoformat() if value is not None else None


def _source_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _read_count_inspector(snapshot: Path) -> str:
    try:
        workbook_format = detect_workbook_format(snapshot)
        if workbook_format is WorkbookFormat.OLE:
            try:
                import xlrd
            except ImportError as exc:
                raise ExecutionApiError(
                    503,
                    "legacy_workbook_reader_unavailable",
                    "服务端缺少旧版 Excel 读取组件",
                ) from exc
            workbook = xlrd.open_workbook(
                filename=str(snapshot),
                on_demand=True,
            )
            try:
                if "根数法报告1" not in workbook.sheet_names():
                    raise ExecutionApiError(
                        422,
                        "external_count_worksheet_missing",
                        "所选文件缺少根数法报告1工作表",
                    )
                value = workbook.sheet_by_name("根数法报告1").cell_value(
                    7,
                    8,
                )
            finally:
                workbook.release_resources()
        elif workbook_format is WorkbookFormat.OOXML:
            with snapshot.open("rb") as stream:
                workbook = load_workbook(
                    stream,
                    read_only=True,
                    data_only=True,
                    keep_links=False,
                )
                try:
                    if "根数法报告1" not in workbook.sheetnames:
                        raise ExecutionApiError(
                            422,
                            "external_count_worksheet_missing",
                            "所选文件缺少根数法报告1工作表",
                        )
                    value = workbook["根数法报告1"]["I8"].value
                finally:
                    workbook.close()
        else:
            raise ExecutionApiError(
                415,
                "external_workbook_format_unsupported",
                "无法识别所选工作簿的实际文件格式",
            )
    except ExecutionApiError:
        raise
    except Exception as exc:
        raise ExecutionApiError(
            422,
            "external_workbook_read_failed",
            "读取旧系统上传预检字段失败",
            details={"error_type": type(exc).__name__},
        ) from exc
    return value.strip() if isinstance(value, str) else ""


def _stable_snapshot_summary(
    path: Path,
    *,
    expected_fingerprint: str,
    candidate_id: str,
) -> tuple[int, str, str]:
    if _source_fingerprint(path) != expected_fingerprint:
        raise conflict(
            "external_source_file_changed",
            "所选原始记录已发生变化，请重新读取并选择",
            candidate_id=candidate_id,
        )
    with tempfile.TemporaryDirectory(
        prefix="execution-external-preflight-"
    ) as directory:
        snapshot = Path(directory) / f"source{path.suffix.casefold()}"
        try:
            shutil.copyfile(path, snapshot)
        except OSError as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "无法创建原始记录的稳定预检副本",
                candidate_id=candidate_id,
            ) from exc
        if _source_fingerprint(path) != expected_fingerprint:
            raise conflict(
                "external_source_file_changed",
                "复制预检副本期间原始记录发生变化，请重试",
                candidate_id=candidate_id,
            )
        expected_size = int(expected_fingerprint.split(":", 1)[0])
        if snapshot.stat().st_size != expected_size:
            raise conflict(
                "external_source_file_changed",
                "预检副本大小与索引不一致，请重新查询",
                candidate_id=candidate_id,
            )
        digest = hashlib.sha256()
        with snapshot.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        inspector_name = _read_count_inspector(snapshot)
        if _source_fingerprint(path) != expected_fingerprint:
            raise conflict(
                "external_source_file_changed",
                "读取预检副本期间原始记录发生变化，请重试",
                candidate_id=candidate_id,
            )
        return expected_size, digest.hexdigest(), inspector_name


def _credential_for_node(
    db: Session,
    *,
    run: ExecutionRun,
    node: dict[str, Any],
) -> ExecutionCredential:
    slot_name = str((node.get("config") or {}).get("credential_slot") or "")
    slot = next(
        (
            item
            for item in (run.definition_snapshot or {}).get(
                "credential_slots",
                [],
            )
            if isinstance(item, dict) and item.get("name") == slot_name
        ),
        None,
    )
    if (
        slot is None
        or slot.get("system_key") != LEGACY_CREDENTIAL_SYSTEM
    ):
        raise ExecutionApiError(
            422,
            "legacy_credential_slot_invalid",
            "旧检务系统节点未绑定有效的旧系统凭据槽位",
        )
    credential = (
        db.query(ExecutionCredential)
        .filter(
            ExecutionCredential.user_id == run.created_by_id,
            ExecutionCredential.system_key == LEGACY_CREDENTIAL_SYSTEM,
            ExecutionCredential.is_active.is_(True),
        )
        .one_or_none()
    )
    if credential is None:
        raise ExecutionApiError(
            422,
            "legacy_credential_missing",
            "请先在执行系统账号中配置旧检务系统凭据",
        )
    return credential


def _selected_file_rows(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    selected = input_data.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise ExecutionApiError(
            422,
            "external_selected_files_required",
            "旧系统上传前必须先完成人工文件选择",
        )
    primary_file_id = str(input_data.get("primary_file_id") or "")
    if not primary_file_id:
        raise ExecutionApiError(
            422,
            "external_primary_file_required",
            "旧系统上传前必须指定主单",
        )

    declared_roots = {
        str(item.get("root_id"))
        for item in (run.definition_snapshot or {}).get("root_slots", [])
        if isinstance(item, dict) and item.get("access", "read") == "read"
    }
    gateway = build_file_gateway(db)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    inspector_names: dict[str, str] = {}
    primary_inspector: str | None = None

    for raw in selected:
        if not isinstance(raw, dict):
            raise ExecutionApiError(
                409,
                "external_selected_file_invalid",
                "所选文件不是服务器签发的文件对象",
            )
        candidate_id = str(raw.get("id") or "")
        if not candidate_id or candidate_id in seen:
            raise ExecutionApiError(
                409,
                "external_selected_file_invalid",
                "所选文件标识缺失或重复",
            )
        row = (
            db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
            .join(
                ExecutionStorageRoot,
                ExecutionStorageRoot.id
                == ExecutionFileIndexEntry.storage_root_id,
            )
            .filter(ExecutionFileIndexEntry.id == candidate_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise conflict(
                "external_selected_file_stale",
                "所选文件已不在索引中，请重新查询",
                candidate_id=candidate_id,
            )
        entry, root = row
        if (
            entry.missing_since is not None
            or root.root_id != "regenerated_fiber_records"
            or root.root_id not in declared_roots
            or raw.get("root_id") != root.root_id
            or raw.get("relative_path") != entry.relative_path
            or raw.get("fingerprint") != entry.fingerprint
            or raw.get("read_status") != "succeeded"
        ):
            raise conflict(
                "external_selected_file_stale",
                "所选文件已变化或不属于再生纤根数法流程",
                candidate_id=candidate_id,
            )
        result = raw.get("result")
        if (
            not isinstance(result, dict)
            or result.get("method") != "count"
            or result.get("worksheet") != "根数法报告1"
        ):
            raise ExecutionApiError(
                422,
                "external_count_result_invalid",
                "所选文件不是根数法报告1的读取结果",
                details={"candidate_id": candidate_id},
            )
        try:
            path = gateway.resolve(
                ArtifactRef(root.root_id, entry.relative_path),
                expected_type="file",
            )
            size_bytes, content_sha256, inspector_name = (
                _stable_snapshot_summary(
                    path,
                    expected_fingerprint=entry.fingerprint,
                    candidate_id=candidate_id,
                )
            )
        except (StorageError, OSError) as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "所选文件当前不可读取，请重新查询",
                candidate_id=candidate_id,
            ) from exc
        if not inspector_name:
            raise ExecutionApiError(
                422,
                "external_inspector_missing",
                "所选文件的根数法报告1!I8未读取到检验员",
                details={"candidate_id": candidate_id},
            )
        inspector_names.setdefault(inspector_name.casefold(), inspector_name)
        if entry.id == primary_file_id:
            primary_inspector = inspector_name
        rows.append(
            {
                "id": entry.id,
                "root_id": root.root_id,
                "relative_path": entry.relative_path,
                "filename": entry.filename,
                "fingerprint": entry.fingerprint,
                "content_sha256": content_sha256,
                "size_bytes": size_bytes,
                "is_primary": entry.id == primary_file_id,
            }
        )
        seen.add(candidate_id)

    if primary_file_id not in seen:
        raise ExecutionApiError(
            422,
            "external_primary_file_not_selected",
            "主单必须属于本次已选择的文件",
        )
    if len(inspector_names) != 1:
        raise ExecutionApiError(
            422,
            "external_inspector_conflict",
            "多个原始记录中的检验员不一致，不能自动生成上传预检单",
            details={"inspector_count": len(inspector_names)},
        )
    rows.sort(
        key=lambda item: (
            not item["is_primary"],
            item["relative_path"].casefold(),
            item["id"],
        )
    )
    if primary_inspector is None:
        raise ExecutionApiError(
            422,
            "external_primary_inspector_missing",
            "未能从主单稳定快照读取检验员",
        )
    return rows, primary_inspector


def _bound_credential_for_approval(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
) -> ExecutionCredential:
    credential = (
        db.query(ExecutionCredential)
        .filter(ExecutionCredential.id == operation.credential_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        credential is None
        or not credential.is_active
        or credential.user_id != run.created_by_id
        or credential.system_key != LEGACY_CREDENTIAL_SYSTEM
        or credential.revision != operation.credential_revision
    ):
        raise conflict(
            "external_operation_credential_changed",
            "旧检务系统凭据已变化，请重新生成并核对预检单",
            operation_id=operation.id,
        )
    if _account_scope_key(credential.account_name or "") != (
        operation.account_scope_key
    ):
        raise conflict(
            "external_operation_credential_changed",
            "旧检务系统账号已变化，请重新生成并核对预检单",
            operation_id=operation.id,
        )
    return credential


def _ensure_preflight_not_expired(
    operation: ExecutionExternalOperation,
    *,
    now: datetime,
) -> None:
    expires_at = operation.preflight_expires_at
    if expires_at is None or _aware_utc(expires_at) <= _aware_utc(now):
        raise conflict(
            "external_operation_preflight_expired",
            "预检单已过期，系统将自动结束本次运行，请重新执行",
            operation_id=operation.id,
        )


def _ensure_approval_not_expired(
    operation: ExecutionExternalOperation,
    *,
    now: datetime,
) -> None:
    expires_at = operation.approval_expires_at
    if expires_at is None or _aware_utc(expires_at) <= _aware_utc(now):
        raise conflict(
            "external_operation_approval_expired",
            "本次批准已过期，系统将自动结束本次运行，请重新执行并再次确认",
            operation_id=operation.id,
        )


def _reverify_operation_sources(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    summary = operation.request_summary or {}
    files = summary.get("files")
    expected_inspector = summary.get("inspector")
    if (
        not isinstance(files, list)
        or not files
        or not isinstance(expected_inspector, str)
        or not expected_inspector
    ):
        raise conflict(
            "external_operation_preflight_invalid",
            "预检单缺少完整的源文件核对信息，请重新运行流程",
            operation_id=operation.id,
        )

    gateway = build_file_gateway(db)
    for expected in files:
        if not isinstance(expected, dict):
            raise conflict(
                "external_operation_preflight_invalid",
                "预检单中的源文件信息无效，请重新运行流程",
                operation_id=operation.id,
            )
        candidate_id = str(expected.get("id") or "")
        row = (
            db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
            .join(
                ExecutionStorageRoot,
                ExecutionStorageRoot.id
                == ExecutionFileIndexEntry.storage_root_id,
            )
            .filter(ExecutionFileIndexEntry.id == candidate_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise conflict(
                "external_source_file_changed",
                "预检单中的原始记录已不在索引中，请重新运行流程",
                candidate_id=candidate_id,
            )
        entry, root = row
        expected_fingerprint = str(expected.get("fingerprint") or "")
        if (
            not candidate_id
            or entry.missing_since is not None
            or root.root_id != expected.get("root_id")
            or entry.relative_path != expected.get("relative_path")
            or entry.fingerprint != expected_fingerprint
        ):
            raise conflict(
                "external_source_file_changed",
                "预检后原始记录索引已变化，请重新运行流程",
                candidate_id=candidate_id,
            )
        try:
            path = gateway.resolve(
                ArtifactRef(root.root_id, entry.relative_path),
                expected_type="file",
            )
            size_bytes, content_sha256, inspector_name = (
                _stable_snapshot_summary(
                    path,
                    expected_fingerprint=expected_fingerprint,
                    candidate_id=candidate_id,
                )
            )
        except ExecutionApiError:
            raise
        except (StorageError, OSError, ValueError) as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "批准前无法重新读取原始记录，请稍后重试",
                candidate_id=candidate_id,
            ) from exc

        changed_fields: list[str] = []
        if size_bytes != expected.get("size_bytes"):
            changed_fields.append("size_bytes")
        if content_sha256 != expected.get("content_sha256"):
            changed_fields.append("content_sha256")
        if inspector_name != expected_inspector:
            changed_fields.append("inspector")
        if changed_fields:
            raise conflict(
                "external_source_file_changed",
                "预检后原始记录内容已变化，请重新运行流程",
                candidate_id=candidate_id,
                changed_fields=changed_fields,
            )


def prepare_legacy_regenerated_count_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Create a durable preflight record without contacting FibreCheck."""

    if node_run.node_type != LEGACY_REGENERATED_COUNT_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    remote_business_key = lock_legacy_remote_business_scope(
        db,
        sample_number=run.inspection_number,
    )
    files, inspector_name = _selected_file_rows(
        db,
        run=run,
        input_data=input_data,
    )
    credential_revision = int(credential.revision)
    request_summary = {
        "schema_version": 1,
        "operation_type": "legacy_regenerated_fiber_count_upload",
        "target_sample_number": run.inspection_number.strip(),
        "business_fields": {
            "fiber_category": "棉再生纤",
            "inspection_method": "定量",
            "inspection_item": "棉再生纤定量-根数法",
            "inspection_copies": 1,
        },
        "inspector": inspector_name,
        "files": files,
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": True,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
        },
    }
    payload_checksum = _canonical_checksum(
        {
            "request_summary": request_summary,
            "credential_binding": {
                "credential_id": credential.id,
                "credential_revision": credential_revision,
                "account_scope_key": account_scope_key,
                "remote_business_key": remote_business_key,
            },
        }
    )
    operation_key = hashlib.sha256(
        (
            f"{OPERATION_KEY_PREFIX}:{run.id}:{node_run.node_id}"
        ).encode("utf-8")
    ).hexdigest()
    prepared_at = utcnow()
    preflight_expires_at = prepared_at + timedelta(
        minutes=settings.EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES
    )
    existing = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.node_run_id == node_run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if existing is None:
        existing = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.operation_key == operation_key,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
    if existing is not None:
        if (
            existing.run_id != run.id
            or existing.node_run_id != node_run.id
            or existing.connector_key != LEGACY_CONNECTOR_KEY
            or existing.credential_id != credential.id
            or existing.credential_revision != credential_revision
            or existing.account_scope_key != account_scope_key
            or existing.remote_business_key != remote_business_key
            or existing.payload_checksum != payload_checksum
        ):
            raise conflict(
                "external_operation_idempotency_conflict",
                "该外部操作幂等键已绑定另一份预检内容",
                operation_id=existing.id,
            )
        return existing, True

    blocking_operation = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.remote_business_key
            == remote_business_key,
            ExecutionExternalOperation.status.in_(
                ACTIVE_REMOTE_OPERATION_STATUSES
            ),
        )
        .order_by(ExecutionExternalOperation.created_at.asc())
        .with_for_update()
        .first()
    )
    if blocking_operation is not None:
        raise conflict(
            "external_remote_business_conflict",
            "同一样品已有待处理的旧系统操作，请先处理或取消原流程",
            operation_id=blocking_operation.id,
        )

    operation = ExecutionExternalOperation(
        operation_key=operation_key,
        run_id=run.id,
        node_run_id=node_run.id,
        connector_key=LEGACY_CONNECTOR_KEY,
        credential_id=credential.id,
        credential_revision=credential_revision,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        status="prepared",
        payload_checksum=payload_checksum,
        request_summary=request_summary,
        preflight_expires_at=preflight_expires_at,
    )
    try:
        with db.begin_nested():
            db.add(operation)
            db.flush()
    except IntegrityError as exc:
        blocker = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.remote_business_key
                == remote_business_key,
                ExecutionExternalOperation.status.in_(
                    ACTIVE_REMOTE_OPERATION_STATUSES
                ),
            )
            .order_by(ExecutionExternalOperation.created_at.asc())
            .first()
        )
        if blocker is not None:
            raise conflict(
                "external_remote_business_conflict",
                "同一样品已有待处理的旧系统操作，请先处理或取消原流程",
                operation_id=blocker.id,
            ) from exc
        raise conflict(
            "external_operation_idempotency_conflict",
            "该外部操作已被并发创建，请刷新后重试",
        ) from exc
    return operation, False


def approve_prepared_external_operation(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
    actor: ExecutionUser,
    payload_checksum: str,
    confirmed_sample_number: str,
    note: str | None = None,
) -> tuple[ExecutionExternalOperation, bool]:
    if payload_checksum != operation.payload_checksum:
        raise conflict(
            "external_operation_payload_changed",
            "预检内容已变化，请刷新并重新核对后再确认",
            operation_id=operation.id,
        )
    target_sample_number = str(
        (operation.request_summary or {}).get("target_sample_number") or ""
    )
    if confirmed_sample_number != target_sample_number:
        raise conflict(
            "external_operation_sample_confirmation_mismatch",
            "确认的样品编号与预检单不一致，请重新核对",
            operation_id=operation.id,
        )
    if operation.run_id != run.id:
        raise conflict(
            "external_operation_run_changed",
            "预检单与当前流程运行不一致，请刷新后重试",
            operation_id=operation.id,
        )
    if operation.status not in {"prepared", "approved"}:
        raise conflict(
            "external_operation_not_approvable",
            "当前外部操作状态不能批准",
            operation_id=operation.id,
            status=operation.status,
        )

    now = utcnow()
    if operation.status == "prepared":
        _ensure_preflight_not_expired(operation, now=now)
    else:
        _ensure_approval_not_expired(operation, now=now)
    _bound_credential_for_approval(
        db,
        operation=operation,
        run=run,
    )
    _reverify_operation_sources(db, operation=operation)
    if operation.status == "approved":
        return operation, True

    operation.status = "approved"
    operation.approved_by_id = actor.id
    operation.approved_at = now
    operation.approval_expires_at = now + timedelta(
        minutes=settings.EXECUTION_EXTERNAL_APPROVAL_TTL_MINUTES
    )
    operation.approval_note = note.strip() if note and note.strip() else None
    node_run = db.get(ExecutionNodeRun, operation.node_run_id)
    if node_run is not None and node_run.status == "waiting_external":
        output = dict(node_run.output_data or {})
        output["status"] = "approved"
        output["approved_at"] = now.isoformat()
        output["approval_expires_at"] = (
            operation.approval_expires_at.isoformat()
        )
        node_run.output_data = output
    append_run_event(
        db,
        run_id=operation.run_id,
        event_type="external_operation.approved",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "operation_id": operation.id,
            "node_id": node_run.node_id if node_run is not None else None,
            "status": operation.status,
            "payload_checksum": operation.payload_checksum,
            "approval_expires_at": (
                operation.approval_expires_at.isoformat()
            ),
            "remote_write_performed": False,
        },
    )
    append_audit_log(
        db,
        action="external_operation.approve",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        actor_user_id=actor.id,
        details={
            "run_id": operation.run_id,
            "payload_checksum": operation.payload_checksum,
            "approval_expires_at": (
                operation.approval_expires_at.isoformat()
            ),
            "remote_write_performed": False,
        },
    )
    return operation, False


def public_external_operation(
    operation: ExecutionExternalOperation,
) -> dict[str, Any]:
    summary = operation.request_summary or {}
    business_fields = summary.get("business_fields")
    files = summary.get("files")
    public_files = [
        {
            key: item.get(key)
            for key in (
                "id",
                "root_id",
                "relative_path",
                "filename",
                "fingerprint",
                "content_sha256",
                "size_bytes",
                "is_primary",
            )
        }
        for item in files
        if isinstance(item, dict)
    ] if isinstance(files, list) else []
    public_summary = {
        "schema_version": summary.get("schema_version"),
        "operation_type": summary.get("operation_type"),
        "target_sample_number": summary.get("target_sample_number"),
        "business_fields": {
            key: business_fields.get(key)
            for key in (
                "fiber_category",
                "inspection_method",
                "inspection_item",
                "inspection_copies",
            )
        } if isinstance(business_fields, dict) else {},
        "inspector": summary.get("inspector"),
        "files": public_files,
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": True,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
        },
    }
    return {
        "id": operation.id,
        "operation_key": operation.operation_key,
        "run_id": operation.run_id,
        "node_run_id": operation.node_run_id,
        "connector_key": operation.connector_key,
        "status": operation.status,
        "payload_checksum": operation.payload_checksum,
        "request_summary": public_summary,
        "preflight_expires_at": _isoformat(
            operation.preflight_expires_at
        ),
        "approval": {
            "approved_by_id": operation.approved_by_id,
            "approved_at": _isoformat(operation.approved_at),
            "expires_at": _isoformat(operation.approval_expires_at),
            "note": operation.approval_note,
        },
        "remote_write_performed": False,
        "error": (
            {
                "code": operation.error_code,
                "message": operation.error_message,
            }
            if operation.error_code
            else None
        ),
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
    }
