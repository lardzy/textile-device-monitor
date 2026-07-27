from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionArtifact,
    ExecutionArtifactRelation,
    ExecutionFileMutation,
    ExecutionHumanTask,
    ExecutionNodeRun,
    ExecutionPublishReceipt,
    ExecutionRole,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionUserRole,
    utcnow,
)
from app.execution.mutations import (
    CellWrite,
    FileMutationService,
    MutationError,
    validate_mutation_id,
)
from app.execution.persistence import build_file_gateway, storage_root_by_key
from app.execution.registry import node_registry
from app.execution.storage import (
    ArtifactFingerprint,
    ArtifactRef,
    StorageError,
    fingerprint_file,
)


def _mutation_service(db: Session) -> FileMutationService:
    return FileMutationService(
        build_file_gateway(db),
        staging_root_id="execution_staging",
    )


def _artifact(
    db: Session,
    *,
    run_id: str,
    node_run_id: Optional[str],
    ref: ArtifactRef,
    role: str,
    fingerprint: ArtifactFingerprint,
) -> ExecutionArtifact:
    root = storage_root_by_key(db, ref.root_id)
    existing = (
        db.query(ExecutionArtifact)
        .filter(
            ExecutionArtifact.run_id == run_id,
            ExecutionArtifact.storage_root_id == root.id,
            ExecutionArtifact.relative_path == ref.relative_path,
            ExecutionArtifact.content_sha256 == fingerprint.sha256,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    record = ExecutionArtifact(
        run_id=run_id,
        node_run_id=node_run_id,
        storage_root_id=root.id,
        relative_path=ref.relative_path,
        filename=Path(ref.relative_path).name,
        role=role,
        size_bytes=fingerprint.size,
        content_sha256=fingerprint.sha256,
        metadata_json={"modified_ns": fingerprint.modified_ns},
    )
    db.add(record)
    db.flush()
    return record


def _artifact_ref(
    db: Session,
    artifact: ExecutionArtifact,
) -> ArtifactRef:
    root = db.get(ExecutionStorageRoot, artifact.storage_root_id)
    if root is None:
        raise ExecutionApiError(
            409,
            "mutation_artifact_root_missing",
            "变更计划引用的文件根目录不存在",
        )
    return ArtifactRef(root.root_id, artifact.relative_path)


def _artifact_fingerprint(artifact: ExecutionArtifact) -> ArtifactFingerprint:
    metadata = artifact.metadata_json or {}
    return ArtifactFingerprint(
        size=int(artifact.size_bytes),
        modified_ns=int(metadata.get("modified_ns") or 0),
        sha256=artifact.content_sha256,
    )


def _fingerprint_key(fingerprint: ArtifactFingerprint) -> str:
    return (
        f"{fingerprint.size}:"
        f"{fingerprint.modified_ns}:"
        f"{fingerprint.sha256}"
    )


def _ensure_relation(
    db: Session,
    *,
    parent_artifact_id: str,
    child_artifact_id: str,
    relation_type: str,
) -> None:
    existing = (
        db.query(ExecutionArtifactRelation.id)
        .filter(
            ExecutionArtifactRelation.parent_artifact_id == parent_artifact_id,
            ExecutionArtifactRelation.child_artifact_id == child_artifact_id,
            ExecutionArtifactRelation.relation_type == relation_type,
        )
        .first()
    )
    if existing is None:
        db.add(
            ExecutionArtifactRelation(
                parent_artifact_id=parent_artifact_id,
                child_artifact_id=child_artifact_id,
                relation_type=relation_type,
            )
        )


def _source_ref(value: Any) -> ArtifactRef:
    if not isinstance(value, dict):
        raise ExecutionApiError(
            422,
            "artifact_ref_required",
            "节点输入缺少文件引用",
        )
    root_id = value.get("root_id")
    relative_path = value.get("relative_path")
    if not root_id or not relative_path:
        raise ExecutionApiError(
            422,
            "artifact_ref_required",
            "文件引用必须包含 root_id 和 relative_path",
        )
    try:
        return ArtifactRef(str(root_id), str(relative_path))
    except (ValueError, StorageError) as exc:
        raise ExecutionApiError(
            422,
            "artifact_ref_invalid",
            "文件引用不合法",
        ) from exc


def _writes(value: Any) -> list[CellWrite]:
    if not isinstance(value, list) or not value:
        raise ExecutionApiError(422, "cell_writes_required", "至少需要一个单元格写入")
    try:
        return [
            CellWrite(
                sheet=str(item["sheet"]),
                cell=str(item["cell"]),
                value=item.get("value"),
            ).normalized()
            for item in value
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionApiError(
            422,
            "cell_write_invalid",
            "单元格写入定义不合法",
        ) from exc


def _json_checksum(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _working_artifact_identity(
    db: Session,
    mutation: ExecutionFileMutation,
) -> tuple[ExecutionArtifact, ArtifactRef]:
    artifact = (
        db.get(ExecutionArtifact, mutation.working_artifact_id)
        if mutation.working_artifact_id
        else None
    )
    if artifact is None or artifact.run_id != mutation.run_id:
        raise ExecutionApiError(
            409,
            "mutation_working_artifact_missing",
            "工作副本记录不存在，请重新创建工作副本",
        )
    root = db.get(ExecutionStorageRoot, artifact.storage_root_id)
    if root is None:
        raise ExecutionApiError(
            409,
            "mutation_working_artifact_missing",
            "工作副本根目录记录不存在",
        )
    return artifact, ArtifactRef(root.root_id, artifact.relative_path)


def _require_working_copy_unchanged(
    db: Session,
    mutation: ExecutionFileMutation,
    working_ref: ArtifactRef,
) -> ExecutionArtifact:
    artifact, expected_ref = _working_artifact_identity(db, mutation)
    if working_ref != expected_ref:
        raise ExecutionApiError(
            409,
            "mutation_working_copy_mismatch",
            "当前工作副本与变更计划不一致",
        )
    gateway = build_file_gateway(db)
    current = fingerprint_file(
        gateway.resolve(working_ref, expected_type="file")
    )
    if (
        current.sha256 != artifact.content_sha256
        or current.size != artifact.size_bytes
    ):
        raise ExecutionApiError(
            409,
            "mutation_working_copy_changed",
            "工作副本在核对后发生变化，请重新执行写入与核对",
        )
    return artifact


def _approval_context(
    mutation: ExecutionFileMutation,
    working_artifact: ExecutionArtifact,
    working_ref: ArtifactRef,
    target_ref: ArtifactRef,
) -> dict[str, Any]:
    return {
        "approved": True,
        "mutation_id": mutation.mutation_id,
        "working_copy": {
            **working_ref.as_dict(),
            "content_sha256": working_artifact.content_sha256,
        },
        "target": target_ref.as_dict(),
        "change_plan_checksum": _json_checksum(mutation.change_plan or {}),
    }


def _translate_file_error(exc: Exception) -> ExecutionApiError:
    message = str(exc) or type(exc).__name__
    if "conflict" in type(exc).__name__.lower() or "changed" in message:
        return ExecutionApiError(
            409,
            "file_mutation_conflict",
            "文件状态已变化，请重新预检",
        )
    if "desktop_adapter_required" in message:
        return ExecutionApiError(
            409,
            "excel_desktop_capability_required",
            "该工作簿需要 Windows Excel Desktop 适配器处理",
        )
    return ExecutionApiError(
        422,
        "file_mutation_failed",
        "文件操作未完成",
        details={"reason": message},
    )


def _require_run_write_contract(run: ExecutionRun) -> None:
    if (run.capabilities_snapshot or {}).get("write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_write_capability_required",
            "当前流程未启用受控写入能力",
        )


def _require_run_root_access(
    run: ExecutionRun,
    *,
    root_id: str,
    access: str,
) -> None:
    declared = {
        (str(slot.get("root_id")), str(slot.get("access") or "read"))
        for slot in (run.definition_snapshot or {}).get("root_slots", [])
        if isinstance(slot, dict) and slot.get("root_id")
    }
    if (root_id, access) not in declared:
        raise ExecutionApiError(
            422,
            "mutation_root_capability_not_declared",
            "文件根目录未以所需权限声明在运行快照中",
            details={"root_id": root_id, "required_access": access},
        )


def _mutation_for_run(
    db: Session,
    *,
    run_id: str,
    mutation_id: str,
    lock: bool = False,
) -> ExecutionFileMutation:
    try:
        normalized_id = validate_mutation_id(mutation_id)
    except MutationError as exc:
        raise _translate_file_error(exc) from exc
    query = db.query(ExecutionFileMutation).filter(
        ExecutionFileMutation.mutation_id == normalized_id
    )
    if lock:
        query = query.with_for_update()
    mutation = query.one_or_none()
    if mutation is None:
        raise ExecutionApiError(
            409,
            "mutation_not_prepared",
            "文件变更尚未完成预检",
        )
    if mutation.run_id != run_id:
        raise ExecutionApiError(
            409,
            "mutation_id_conflict",
            "mutation_id 已被其他流程运行使用",
        )
    return mutation


def _planned_refs(
    db: Session,
    mutation: ExecutionFileMutation,
) -> tuple[ArtifactRef, ArtifactRef]:
    source_artifact = db.get(ExecutionArtifact, mutation.source_artifact_id)
    if source_artifact is None:
        raise ExecutionApiError(
            409,
            "mutation_source_artifact_missing",
            "变更计划的源文件记录不存在",
        )
    source_ref = _artifact_ref(db, source_artifact)
    working_value = (mutation.change_plan or {}).get("working_copy")
    if isinstance(working_value, dict):
        working_ref = _source_ref(working_value)
    elif mutation.working_artifact_id:
        working_artifact = db.get(
            ExecutionArtifact,
            mutation.working_artifact_id,
        )
        if working_artifact is None:
            raise ExecutionApiError(
                409,
                "mutation_working_artifact_missing",
                "工作副本记录不存在",
            )
        working_ref = _artifact_ref(db, working_artifact)
    else:
        working_ref = ArtifactRef(
            "execution_staging",
            (
                f".execution-mutations/{mutation.mutation_id}/working/"
                f"{source_artifact.filename}"
            ),
        )
    return source_ref, working_ref


def _copy_receipt_from_database(
    db: Session,
    mutation: ExecutionFileMutation,
    *,
    reused: bool,
) -> dict[str, Any]:
    source_artifact = db.get(ExecutionArtifact, mutation.source_artifact_id)
    working_artifact = (
        db.get(ExecutionArtifact, mutation.working_artifact_id)
        if mutation.working_artifact_id
        else None
    )
    if source_artifact is None or working_artifact is None:
        raise ExecutionApiError(
            409,
            "mutation_working_artifact_missing",
            "工作副本记录不存在，请重新创建工作副本",
        )
    return {
        "mutation_id": mutation.mutation_id,
        "source": {
            **_artifact_ref(db, source_artifact).as_dict(),
            "fingerprint": _artifact_fingerprint(source_artifact).as_dict(),
        },
        "working_copy": {
            **_artifact_ref(db, working_artifact).as_dict(),
            "fingerprint": _artifact_fingerprint(working_artifact).as_dict(),
        },
        "created_at": mutation.created_at.isoformat(),
        "reused": reused,
    }


def _record_mutation_error(
    db: Session,
    mutation: ExecutionFileMutation,
    error: ExecutionApiError,
    *,
    status: str,
) -> None:
    mutation.status = status
    mutation.error_code = error.code
    mutation.error_message = error.message
    db.flush()


def _clear_mutation_error(mutation: ExecutionFileMutation) -> None:
    mutation.error_code = None
    mutation.error_message = None


def _bind_mutation_stage(
    mutation: ExecutionFileMutation,
    *,
    stage: str,
    node_run_id: Optional[str],
    allow_create: bool,
) -> None:
    if node_run_id is None:
        raise ExecutionApiError(
            409,
            "mutation_node_binding_required",
            "文件变更阶段必须绑定实际执行节点",
            details={"stage": stage},
        )
    change_plan = dict(mutation.change_plan or {})
    bindings = dict(change_plan.get("node_bindings") or {})
    existing = bindings.get(stage)
    if existing is not None and existing != node_run_id:
        raise ExecutionApiError(
            409,
            "mutation_node_binding_conflict",
            "该文件变更阶段已绑定到另一个执行节点",
            details={
                "stage": stage,
                "expected_node_run_id": existing,
                "actual_node_run_id": node_run_id,
            },
        )
    if existing is None:
        if not allow_create:
            raise ExecutionApiError(
                409,
                "mutation_node_binding_missing",
                "既有文件变更缺少可验证的节点绑定，不能继续执行",
                details={"stage": stage},
            )
        bindings[stage] = node_run_id
        mutation.change_plan = {
            **change_plan,
            "node_bindings": bindings,
        }


def plan_file_mutation(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
    source_ref: ArtifactRef,
    node_run_id: Optional[str] = None,
    working_relative_path: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a source-bound plan before any working-copy side effect."""

    try:
        if node_run_id is None:
            raise ExecutionApiError(
                409,
                "mutation_node_binding_required",
                "文件变更预检必须绑定实际执行节点",
                details={"stage": "copy"},
            )
        _require_run_write_contract(run)
        _require_run_root_access(
            run,
            root_id=source_ref.root_id,
            access="read",
        )
        _require_run_root_access(
            run,
            root_id="execution_staging",
            access="write",
        )
        normalized_id = validate_mutation_id(mutation_id)
        existing = (
            db.query(ExecutionFileMutation)
            .filter(ExecutionFileMutation.mutation_id == normalized_id)
            .with_for_update()
            .one_or_none()
        )
        if existing is not None and existing.run_id != run.id:
            raise ExecutionApiError(
                409,
                "mutation_id_conflict",
                "mutation_id 已被其他流程运行使用",
            )

        gateway = build_file_gateway(db)
        source_path = gateway.resolve(source_ref, expected_type="file")
        source_fingerprint = fingerprint_file(source_path)
        working_ref = ArtifactRef(
            "execution_staging",
            working_relative_path
            or (
                f".execution-mutations/{normalized_id}/working/"
                f"{source_path.name}"
            ),
        )

        if existing is not None:
            _bind_mutation_stage(
                existing,
                stage="copy",
                node_run_id=node_run_id,
                allow_create=existing.status in {"planned", "failed"},
            )
            source_artifact = db.get(
                ExecutionArtifact,
                existing.source_artifact_id,
            )
            if source_artifact is None:
                raise ExecutionApiError(
                    409,
                    "mutation_source_artifact_missing",
                    "变更计划的源文件记录不存在",
                )
            existing_source_ref, existing_working_ref = _planned_refs(
                db,
                existing,
            )
            if (
                existing_source_ref != source_ref
                or existing_working_ref != working_ref
                or _artifact_fingerprint(source_artifact)
                != source_fingerprint
                or existing.source_fingerprint
                != _fingerprint_key(source_fingerprint)
            ):
                raise ExecutionApiError(
                    409,
                    "mutation_plan_conflict",
                    "mutation_id 已绑定到不同的源文件、工作副本或文件版本",
                )
            return {
                "mutation_id": existing.mutation_id,
                "status": existing.status,
                "source": {
                    **source_ref.as_dict(),
                    "fingerprint": source_fingerprint.as_dict(),
                },
                "working_copy": working_ref.as_dict(),
                "change_plan": existing.change_plan or {},
                "reused": True,
            }

        change_plan = {
            "schema_version": 1,
            "operation": "copy_on_write",
            "source": {
                **source_ref.as_dict(),
                "fingerprint": source_fingerprint.as_dict(),
            },
            "working_copy": working_ref.as_dict(),
            "node_bindings": {"copy": node_run_id},
        }
        try:
            with db.begin_nested():
                source_artifact = _artifact(
                    db,
                    run_id=run.id,
                    node_run_id=node_run_id,
                    ref=source_ref,
                    role="source",
                    fingerprint=source_fingerprint,
                )
                mutation = ExecutionFileMutation(
                    mutation_id=normalized_id,
                    run_id=run.id,
                    node_run_id=node_run_id,
                    source_artifact_id=source_artifact.id,
                    status="planned",
                    source_fingerprint=_fingerprint_key(source_fingerprint),
                    change_plan=change_plan,
                )
                db.add(mutation)
                db.flush()
        except IntegrityError:
            # A concurrent request may have created the globally unique
            # mutation_id. Re-enter validation so the same plan is reused and
            # a different run/source is rejected deterministically.
            return plan_file_mutation(
                db,
                run=run,
                mutation_id=normalized_id,
                source_ref=source_ref,
                node_run_id=node_run_id,
                working_relative_path=working_relative_path,
            )
        return {
            "mutation_id": mutation.mutation_id,
            "status": mutation.status,
            "source": {
                **source_ref.as_dict(),
                "fingerprint": source_fingerprint.as_dict(),
            },
            "working_copy": working_ref.as_dict(),
            "change_plan": change_plan,
            "reused": False,
        }
    except ExecutionApiError:
        raise
    except (MutationError, StorageError, OSError, ValueError) as exc:
        raise _translate_file_error(exc) from exc


def prepare_file_mutation(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
    node_run_id: Optional[str] = None,
) -> dict[str, Any]:
    _require_run_write_contract(run)
    _require_run_root_access(
        run,
        root_id="execution_staging",
        access="write",
    )
    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    _bind_mutation_stage(
        mutation,
        stage="copy",
        node_run_id=node_run_id,
        allow_create=mutation.status in {"planned", "failed"},
    )
    if mutation.status in {"written", "verified", "published"}:
        return _copy_receipt_from_database(db, mutation, reused=True)
    if mutation.status == "failed" and mutation.working_artifact_id:
        raise ExecutionApiError(
            409,
            "mutation_stage_conflict",
            "当前变更已进入写入阶段，不能重新创建工作副本",
        )
    source_ref, working_ref = _planned_refs(db, mutation)
    _require_run_root_access(
        run,
        root_id=source_ref.root_id,
        access="read",
    )
    try:
        receipt = _mutation_service(db).prepare_working_copy(
            mutation.mutation_id,
            source_ref,
            working_relative_path=working_ref.relative_path,
        )
        if _fingerprint_key(receipt.source_fingerprint) != mutation.source_fingerprint:
            raise ExecutionApiError(
                409,
                "mutation_source_changed",
                "源文件在预检后发生变化，请创建新的变更计划",
            )
        source_artifact = db.get(ExecutionArtifact, mutation.source_artifact_id)
        if source_artifact is None:
            raise ExecutionApiError(
                409,
                "mutation_source_artifact_missing",
                "变更计划的源文件记录不存在",
            )
        working_artifact = _artifact(
            db,
            run_id=run.id,
            node_run_id=node_run_id,
            ref=receipt.working_ref,
            role="working",
            fingerprint=receipt.working_fingerprint,
        )
        mutation.working_artifact_id = working_artifact.id
        mutation.node_run_id = node_run_id or mutation.node_run_id
        mutation.status = "prepared"
        _clear_mutation_error(mutation)
        _ensure_relation(
            db,
            parent_artifact_id=source_artifact.id,
            child_artifact_id=working_artifact.id,
            relation_type="working_copy",
        )
        db.flush()
        return receipt.as_dict()
    except ExecutionApiError as exc:
        _record_mutation_error(db, mutation, exc, status="failed")
        raise
    except (MutationError, StorageError, OSError, ValueError) as exc:
        error = _translate_file_error(exc)
        _record_mutation_error(db, mutation, error, status="failed")
        raise error from exc


def write_file_mutation(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
    writes: Any,
    node_run_id: Optional[str] = None,
    expected_working_ref: Optional[ArtifactRef] = None,
) -> dict[str, Any]:
    _require_run_write_contract(run)
    _require_run_root_access(
        run,
        root_id="execution_staging",
        access="write",
    )
    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    _bind_mutation_stage(
        mutation,
        stage="write",
        node_run_id=node_run_id,
        allow_create=mutation.status in {"prepared", "written", "failed"},
    )
    if mutation.working_artifact_id is None:
        raise ExecutionApiError(
            409,
            "mutation_not_prepared",
            "写入前必须创建工作副本",
        )
    _, working_ref = _planned_refs(db, mutation)
    if expected_working_ref is not None and expected_working_ref != working_ref:
        raise ExecutionApiError(
            409,
            "mutation_working_copy_mismatch",
            "写入文件不是该变更计划的工作副本",
        )
    normalized_writes = _writes(writes)
    planned_writes_value = (mutation.change_plan or {}).get("writes")
    if planned_writes_value is not None:
        planned_writes = _writes(planned_writes_value)
        if [item.as_dict() for item in normalized_writes] != [
            item.as_dict() for item in planned_writes
        ]:
            raise ExecutionApiError(
                409,
                "mutation_change_plan_mismatch",
                "写入内容与 mutation_id 已绑定的变更计划不一致",
            )
    try:
        previous_status = mutation.status
        receipt = _mutation_service(db).write_xlsx_cells(
            mutation.mutation_id,
            working_ref,
            normalized_writes,
        )
        previous_working_artifact_id = mutation.working_artifact_id
        updated_artifact = _artifact(
            db,
            run_id=run.id,
            node_run_id=node_run_id,
            ref=receipt.working_ref,
            role="working",
            fingerprint=receipt.after_fingerprint,
        )
        mutation.working_artifact_id = updated_artifact.id
        if previous_working_artifact_id != updated_artifact.id:
            _ensure_relation(
                db,
                parent_artifact_id=previous_working_artifact_id,
                child_artifact_id=updated_artifact.id,
                relation_type="modified_to",
            )
        if mutation.status not in {"verified", "published"}:
            mutation.status = "written"
        mutation.node_run_id = node_run_id or mutation.node_run_id
        mutation.change_plan = {
            **(mutation.change_plan or {}),
            "operation": "write_xlsx_cells",
            "writes": [write.as_dict() for write in receipt.writes],
        }
        if previous_status not in {"verified", "published"}:
            mutation.verification_result = receipt.verification.as_dict()
        _clear_mutation_error(mutation)
        db.flush()
        return receipt.as_dict()
    except ExecutionApiError:
        raise
    except (MutationError, StorageError, OSError, ValueError) as exc:
        error = _translate_file_error(exc)
        _record_mutation_error(db, mutation, error, status="failed")
        raise error from exc


def verify_file_mutation(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
    target_ref: Optional[ArtifactRef],
    writes: Any = None,
    node_run_id: Optional[str] = None,
    expected_working_ref: Optional[ArtifactRef] = None,
) -> dict[str, Any]:
    _require_run_write_contract(run)
    _require_run_root_access(
        run,
        root_id="execution_staging",
        access="write",
    )
    if target_ref is not None:
        _require_run_root_access(
            run,
            root_id=target_ref.root_id,
            access="publish",
        )
    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    _bind_mutation_stage(
        mutation,
        stage="verify",
        node_run_id=node_run_id,
        allow_create=mutation.status in {"written", "failed"},
    )
    if mutation.status not in {"written", "verified", "published", "failed"}:
        raise ExecutionApiError(
            409,
            "mutation_not_written",
            "核对前必须先按变更计划写入工作副本",
        )
    planned_writes = _writes((mutation.change_plan or {}).get("writes"))
    if writes is not None:
        supplied_writes = _writes(writes)
        if [item.as_dict() for item in supplied_writes] != [
            item.as_dict() for item in planned_writes
        ]:
            raise ExecutionApiError(
                409,
                "mutation_change_plan_mismatch",
                "核对内容与实际写入计划不一致",
            )
    working_artifact, working_ref = _working_artifact_identity(db, mutation)
    if expected_working_ref is not None and expected_working_ref != working_ref:
        raise ExecutionApiError(
            409,
            "mutation_working_copy_mismatch",
            "核对文件不是该变更计划的工作副本",
        )
    if mutation.status == "published":
        recorded = mutation.verification_result or {}
        expected_context = (
            _approval_context(
                mutation,
                working_artifact,
                working_ref,
                target_ref,
            )
            if target_ref is not None
            else None
        )
        if recorded.get("approval_context") != expected_context:
            raise ExecutionApiError(
                409,
                "mutation_already_published",
                "已发布的变更不能重新绑定到其他目标",
            )
        return recorded
    try:
        receipt = _mutation_service(db).verify_xlsx_cells(
            working_ref,
            planned_writes,
        )
        if not receipt.verified:
            error = ExecutionApiError(
                409,
                "workbook_verification_failed",
                "工作簿重读核对失败",
                details=receipt.as_dict(),
            )
            _record_mutation_error(db, mutation, error, status="failed")
            raise error
        _require_working_copy_unchanged(db, mutation, working_ref)
        mutation.status = "verified"
        mutation.node_run_id = node_run_id or mutation.node_run_id
        mutation.verification_result = {
            **receipt.as_dict(),
            "working_content_sha256": working_artifact.content_sha256,
            "change_plan_checksum": _json_checksum(mutation.change_plan or {}),
        }
        if target_ref is not None:
            mutation.verification_result["approval_context"] = (
                _approval_context(
                    mutation,
                    working_artifact,
                    working_ref,
                    target_ref,
                )
            )
        _clear_mutation_error(mutation)
        db.flush()
        return mutation.verification_result
    except ExecutionApiError as exc:
        if exc.code in {
            "mutation_working_copy_changed",
            "workbook_verification_failed",
        }:
            _record_mutation_error(db, mutation, exc, status="failed")
        raise
    except (MutationError, StorageError, OSError, ValueError) as exc:
        error = _translate_file_error(exc)
        _record_mutation_error(db, mutation, error, status="failed")
        raise error from exc


def _publish_receipt_from_database(
    db: Session,
    mutation: ExecutionFileMutation,
) -> dict[str, Any]:
    receipt = (
        db.query(ExecutionPublishReceipt)
        .filter(ExecutionPublishReceipt.mutation_id == mutation.id)
        .order_by(ExecutionPublishReceipt.published_at.asc())
        .first()
    )
    if receipt is None:
        raise ExecutionApiError(
            409,
            "publish_receipt_missing",
            "发布状态已完成，但发布回执缺失，需要人工核对",
        )
    return {
        **(receipt.details or {}),
        "content_sha256": receipt.content_sha256,
        "reused": True,
    }


def existing_publish_result(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
) -> dict[str, Any]:
    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    if run.mode == "test":
        result = (mutation.verification_result or {}).get(
            "test_publish_receipt"
        )
        if (
            mutation.node_run_id is not None
            and isinstance(result, dict)
            and result.get("dry_run") is True
        ):
            return {**result, "reused": True}
    if mutation.status == "published":
        return _publish_receipt_from_database(db, mutation)
    raise ExecutionApiError(
        409,
        "publish_receipt_missing",
        "发布节点已结束，但没有可复用的发布回执",
    )


def require_publish_confirmation(
    db: Session,
    *,
    run: ExecutionRun,
    publish_node: dict[str, Any],
    mutation_id: str,
    target_ref: ArtifactRef,
    approval_context: dict[str, Any],
) -> ExecutionHumanTask:
    """Validate the durable, authorized human approval for one publish."""

    confirmation_node_id = (publish_node.get("config") or {}).get(
        "confirmation_node_id"
    )
    confirmation = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_id == confirmation_node_id,
            ExecutionNodeRun.node_type == "human.confirm",
            ExecutionNodeRun.status == "succeeded",
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    task = (
        db.query(ExecutionHumanTask)
        .filter(
            ExecutionHumanTask.run_id == run.id,
            ExecutionHumanTask.node_run_id
            == (confirmation.id if confirmation is not None else ""),
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if (
        confirmation is None
        or task is None
        or task.status != "completed"
        or task.completed_by_id is None
        or task.completed_by_id != task.claimed_by_id
        or (confirmation.output_data or {}).get("approved") is not True
        or (task.result_data or {}).get("approved") is not True
    ):
        raise ExecutionApiError(
            409,
            "publish_confirmation_required",
            "发布前必须完成流程指定的人工确认任务",
        )

    approver = db.get(ExecutionUser, task.completed_by_id)
    authorized = bool(approver is not None and approver.is_active)
    if authorized and approver.role != "admin":
        if task.assigned_user_id is not None:
            authorized = task.assigned_user_id == approver.id
        elif task.candidate_role_key:
            authorized = (
                db.query(ExecutionUserRole.id)
                .join(
                    ExecutionRole,
                    ExecutionRole.id == ExecutionUserRole.role_id,
                )
                .filter(
                    ExecutionUserRole.user_id == approver.id,
                    ExecutionRole.key == task.candidate_role_key,
                )
                .first()
                is not None
            )
        else:
            authorized = approver.id == run.created_by_id
    if not authorized:
        raise ExecutionApiError(
            409,
            "publish_approver_not_authorized",
            "完成人工确认的账号已不具备该任务的批准资格",
        )

    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    working_artifact, working_ref = _working_artifact_identity(db, mutation)
    expected_context = _approval_context(
        mutation,
        working_artifact,
        working_ref,
        target_ref,
    )
    recorded_context = (confirmation.input_data or {}).get(
        "approval_context"
    )
    if (
        approval_context != expected_context
        or recorded_context != expected_context
        or (mutation.verification_result or {}).get("approval_context")
        != expected_context
    ):
        raise ExecutionApiError(
            409,
            "publish_confirmation_required",
            "人工确认不属于当前工作副本、变更计划或发布目标",
            details={"required_approval_context": expected_context},
        )
    return task


def _lock_publish_fence(
    db: Session,
    *,
    run_id: str,
    node_run_id: str,
    mutation_id: str,
    lease_token: str,
    require_lease: bool = True,
) -> tuple[ExecutionRun, ExecutionNodeRun, ExecutionFileMutation]:
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.id == run_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    node_run = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == node_run_id,
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_type == "artifact.publish",
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if node_run is None or (
        require_lease
        and (
            node_run.status != "running"
            or node_run.lease_token != lease_token
        )
    ):
        raise ExecutionApiError(
            409,
            "node_lease_lost",
            "发布节点租约已失效，不能开始新的物理发布",
        )
    mutation = _mutation_for_run(
        db,
        run_id=run.id,
        mutation_id=mutation_id,
        lock=True,
    )
    return run, node_run, mutation


def publish_file_mutation(
    db: Session,
    *,
    run: ExecutionRun,
    mutation_id: str,
    target_ref: ArtifactRef,
    approval_context: dict[str, Any],
    node_run_id: str,
    lease_token: str,
    publish_node: dict[str, Any],
    expected_working_ref: Optional[ArtifactRef] = None,
) -> dict[str, Any]:
    run, node_run, mutation = _lock_publish_fence(
        db,
        run_id=run.id,
        node_run_id=node_run_id,
        mutation_id=mutation_id,
        lease_token=lease_token,
    )
    _require_run_write_contract(run)
    _require_run_root_access(
        run,
        root_id="execution_staging",
        access="write",
    )
    _require_run_root_access(
        run,
        root_id=target_ref.root_id,
        access="publish",
    )
    if mutation.status not in {"verified", "publishing", "published"}:
        raise ExecutionApiError(
            409,
            "mutation_not_verified",
            "发布前必须完成保存后重读核对",
        )
    try:
        working_artifact = _require_working_copy_unchanged(
            db,
            mutation,
            _working_artifact_identity(db, mutation)[1],
        )
    except ExecutionApiError as exc:
        _record_mutation_error(db, mutation, exc, status="failed")
        raise
    working_ref = _artifact_ref(db, working_artifact)
    if expected_working_ref is not None and expected_working_ref != working_ref:
        raise ExecutionApiError(
            409,
            "mutation_working_copy_mismatch",
            "发布文件不是该变更计划的工作副本",
        )
    confirmation_task = require_publish_confirmation(
        db,
        run=run,
        publish_node=publish_node,
        mutation_id=mutation_id,
        target_ref=target_ref,
        approval_context=approval_context,
    )
    expected_approval_context = approval_context
    if run.mode == "test":
        result = {
            "dry_run": True,
            "message": "测试运行已完成核对，未向发布目录写入文件",
            "source": working_ref.as_dict(),
            "target": target_ref.as_dict(),
            "content_sha256": working_artifact.content_sha256,
            "approval_context": expected_approval_context,
        }
        mutation.node_run_id = node_run.id
        mutation.verification_result = {
            **(mutation.verification_result or {}),
            "test_publish_receipt": result,
        }
        db.flush()
        return result
    if mutation.status == "published":
        return _publish_receipt_from_database(db, mutation)

    mutation.status = "publishing"
    mutation.publish_fence_token = lease_token
    mutation.publish_started_at = utcnow()
    mutation.node_run_id = node_run.id
    _clear_mutation_error(mutation)
    db.flush()
    # The node and mutation fence must be durable before the filesystem
    # operation. Cancellation/failure will now observe a running publish node
    # and enter its settling state rather than terminalizing the run.
    db.commit()

    try:
        receipt = _mutation_service(db).publish(
            mutation_id,
            working_ref,
            target_ref,
        )
        run, node_run, mutation = _lock_publish_fence(
            db,
            run_id=run.id,
            node_run_id=node_run_id,
            mutation_id=mutation_id,
            lease_token=lease_token,
            require_lease=False,
        )
        artifact = _artifact(
            db,
            run_id=run.id,
            node_run_id=node_run.id,
            ref=receipt.published_ref,
            role="published",
            fingerprint=receipt.published_fingerprint,
        )
        existing = (
            db.query(ExecutionPublishReceipt)
            .filter(
                ExecutionPublishReceipt.mutation_id == mutation.id,
                ExecutionPublishReceipt.target_storage_root_id
                == artifact.storage_root_id,
                ExecutionPublishReceipt.target_relative_path
                == artifact.relative_path,
            )
            .one_or_none()
        )
        if existing is None:
            db.add(
                ExecutionPublishReceipt(
                    mutation_id=mutation.id,
                    artifact_id=artifact.id,
                    target_storage_root_id=artifact.storage_root_id,
                    target_relative_path=artifact.relative_path,
                    content_sha256=artifact.content_sha256,
                    published_by_id=confirmation_task.completed_by_id,
                    details=receipt.as_dict(),
                )
            )
            _ensure_relation(
                db,
                parent_artifact_id=mutation.working_artifact_id,
                child_artifact_id=artifact.id,
                relation_type="published_as",
            )
        mutation.status = "published"
        mutation.node_run_id = node_run.id
        mutation.publish_fence_token = None
        _clear_mutation_error(mutation)
        db.flush()
        return receipt.as_dict()
    except ExecutionApiError:
        raise
    except (MutationError, StorageError, OSError, ValueError) as exc:
        error = _translate_file_error(exc)
        db.rollback()
        run, node_run, mutation = _lock_publish_fence(
            db,
            run_id=run.id,
            node_run_id=node_run_id,
            mutation_id=mutation_id,
            lease_token=lease_token,
            require_lease=False,
        )
        if mutation.publish_fence_token == lease_token:
            mutation.publish_fence_token = None
            _record_mutation_error(db, mutation, error, status="verified")
        raise error from exc


def _working_ref_from_input(value: Any) -> ArtifactRef:
    if isinstance(value, dict) and "working_copy" in value:
        value = value["working_copy"]
    return _source_ref(value)


def _copy_executor(context) -> dict[str, Any]:
    config = context.node.get("config") or {}
    source = (
        context.input_data.get("source")
        or context.input_data.get("selected_file")
        or context.input_data
    )
    source_ref = _source_ref(source)
    mutation_id = str(
        context.input_data.get("mutation_id")
        or f"{context.run.id}-{context.node_run.node_id}"
    )
    plan_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        source_ref=source_ref,
        node_run_id=context.node_run.id,
        working_relative_path=config.get("working_relative_path"),
    )
    return prepare_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        node_run_id=context.node_run.id,
    )


def _write_executor(context) -> dict[str, Any]:
    working = context.input_data.get("working_copy") or context.input_data.get(
        "working_ref"
    )
    mutation_id = str(context.input_data.get("mutation_id") or "")
    if not mutation_id:
        raise ExecutionApiError(422, "mutation_id_required", "缺少 mutation_id")
    return write_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        writes=(
            context.input_data.get("writes")
            or (context.node.get("config") or {}).get("writes")
        ),
        node_run_id=context.node_run.id,
        expected_working_ref=_working_ref_from_input(working),
    )


def _verify_executor(context) -> dict[str, Any]:
    working = context.input_data.get("working_copy") or context.input_data.get(
        "working_ref"
    )
    mutation_id = str(context.input_data.get("mutation_id") or "")
    if not mutation_id:
        raise ExecutionApiError(422, "mutation_id_required", "缺少 mutation_id")
    target_value = context.input_data.get("target")
    return verify_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        target_ref=_source_ref(target_value) if target_value is not None else None,
        writes=(
            context.input_data.get("writes")
            or (context.node.get("config") or {}).get("writes")
        ),
        node_run_id=context.node_run.id,
        expected_working_ref=_working_ref_from_input(working),
    )


def _publish_executor(context) -> dict[str, Any]:
    context.db.flush()
    config = context.node.get("config") or {}
    mutation_id = str(context.input_data.get("mutation_id") or "")
    working = context.input_data.get("working_copy") or context.input_data.get(
        "working_ref"
    )
    working_ref = _working_ref_from_input(working)
    target_value = context.input_data.get("target") or {
        "root_id": config.get("publish_root_id"),
        "relative_path": context.input_data.get("target_relative_path")
        or config.get("target_relative_path"),
    }
    target_ref = _source_ref(target_value)
    if target_ref.root_id != config.get("publish_root_id"):
        raise ExecutionApiError(
            422,
            "publish_root_mismatch",
            "发布目标根与流程配置不一致",
        )
    mutation = _mutation_for_run(
        context.db,
        run_id=context.run.id,
        mutation_id=mutation_id,
    )
    if mutation.status not in {"verified", "publishing", "published"}:
        raise ExecutionApiError(
            409,
            "mutation_not_verified",
            "发布前必须完成保存后重读核对",
        )
    recorded_approval_context = (
        (mutation.verification_result or {}).get("approval_context")
    )
    return publish_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        target_ref=target_ref,
        approval_context=recorded_approval_context,
        node_run_id=context.node_run.id,
        lease_token=context.lease_token,
        publish_node=context.node,
        expected_working_ref=working_ref,
    )


_REGISTERED = False


def register_mutation_executors() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    node_registry.set_executor("workbook.copy", 1, _copy_executor)
    node_registry.set_executor("workbook.write_cells", 1, _write_executor)
    node_registry.set_executor("workbook.verify", 1, _verify_executor)
    node_registry.set_executor("artifact.publish", 1, _publish_executor)
    _REGISTERED = True
