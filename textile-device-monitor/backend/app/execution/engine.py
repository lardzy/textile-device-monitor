from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import time
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, object_session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.electron_microscopy import (
    _task_project_conditions,
    cached_task_snapshot,
    request_task_snapshot_refresh,
)
from app.execution.microscopy_families import microscopy_family_from_config
from app.execution.project_rules import microscopy_rule_key, resolve_rule
from app.execution.events import append_audit_log, append_run_event
from app.execution.external_operations import (
    LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE,
    LEGACY_REGENERATED_COUNT_NODE,
    LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_REVIEW_NODE,
    approve_prepared_external_operation,
    prepare_legacy_generic_check_record_entry_operation,
    prepare_legacy_microscopy_check_record_entry_operation,
    prepare_legacy_regenerated_count_operation,
    prepare_legacy_special_wool_image_operation,
    prepare_legacy_special_wool_qualitative_review_operation,
    prepare_legacy_special_wool_qualitative_upload_operation,
    prepare_legacy_special_wool_review_operation,
    settle_external_attempt_failure,
)
from app.execution.models import (
    ExecutionEdgeRun,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionFileMutation,
    ExecutionFileIndexEntry,
    ExecutionHumanApprovalReceipt,
    ExecutionHumanTask,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionRole,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionUserRole,
    ExecutionWorkflow,
    ExecutionWorkflowVersion,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    utcnow,
)
from app.execution.persistence import build_file_gateway
from app.execution.registry import node_registry
from app.execution.report_image_placement import (
    auto_complete_report_image_placement,
    is_deferred_report_image_placement,
    normalize_report_image_placement_submission,
    report_image_placement_node_config,
    report_image_placement_form_schema,
)
from app.execution.storage import ArtifactRef, FileGateway, StorageError
from app.execution.validation import (
    definition_checksum,
    runtime_definition,
    validate_definition,
    validate_json_instance,
    workflow_contract_checksum,
)


RUN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
NODE_TERMINAL_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}
HUMAN_NODE_TYPES = {
    "input.form",
    "human.file_selection",
    "human.image_selection",
    "human.input",
    "human.confirm",
}
EXTERNAL_NODE_TYPES = {
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE,
    LEGACY_REGENERATED_COUNT_NODE,
    LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE,
    LEGACY_SPECIAL_WOOL_REVIEW_NODE,
    LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE,
}
UNSETTLED_EXTERNAL_OPERATION_STATUSES = {
    "in_progress",
    "cancel_pending",
    "reconciliation_required",
}
RUN_RESULT_ACCEPTING_STATUSES = {
    "queued",
    "running",
    "waiting_human",
    "waiting_external",
    "paused",
    # A physical publish that was already claimed is an uninterruptible
    # critical section. Cancellation waits for its durable receipt or failure.
    "cancel_pending",
    # A sibling failed after publish had already entered its physical critical
    # section. Keep accepting only that publish result until it is reconciled.
    "failure_pending",
}
NATIVE_APPROVAL_RECEIPT_TTL = timedelta(hours=1)


logger = logging.getLogger(__name__)


def _deadline_reached(deadline: datetime | None, now: datetime) -> bool:
    if deadline is None:
        return False
    normalized_deadline = (
        deadline.replace(tzinfo=timezone.utc)
        if deadline.tzinfo is None
        else deadline.astimezone(timezone.utc)
    )
    normalized_now = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    return normalized_deadline <= normalized_now


@dataclass
class NodeExecutionContext:
    db: Session
    run: ExecutionRun
    node_run: ExecutionNodeRun
    node: dict[str, Any]
    input_data: dict[str, Any]
    worker_id: str
    lease_token: str


def _lock_run(db: Session, run_id: str) -> ExecutionRun:
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.id == run_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise not_found("流程运行", run_id)
    return run


def _lock_run_and_node(
    db: Session,
    node_run_id: str,
) -> tuple[ExecutionRun, ExecutionNodeRun]:
    row = (
        db.query(ExecutionNodeRun.run_id)
        .filter(ExecutionNodeRun.id == node_run_id)
        .one_or_none()
    )
    if row is None:
        raise not_found("节点运行", node_run_id)
    run = _lock_run(db, row[0])
    node_run = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == node_run_id,
            ExecutionNodeRun.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if node_run is None:
        raise not_found("节点运行", node_run_id)
    return run, node_run


def _lock_run_task_and_node(
    db: Session,
    task_id: str,
) -> tuple[ExecutionRun, ExecutionHumanTask, ExecutionNodeRun]:
    db.flush()
    row = (
        db.query(ExecutionHumanTask.run_id)
        .filter(ExecutionHumanTask.id == task_id)
        .one_or_none()
    )
    if row is None:
        raise not_found("人工任务", task_id)
    run = _lock_run(db, row[0])
    task = (
        db.query(ExecutionHumanTask)
        .filter(
            ExecutionHumanTask.id == task_id,
            ExecutionHumanTask.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        raise not_found("人工任务", task_id)
    node_run = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == task.node_run_id,
            ExecutionNodeRun.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one()
    )
    return run, task, node_run


def _ensure_run_accepts_result(run: ExecutionRun) -> None:
    if run.status not in RUN_RESULT_ACCEPTING_STATUSES:
        raise conflict(
            "run_closed",
            "流程运行已结束，当前结果不会被接受",
            run_id=run.id,
            status=run.status,
        )


def can_user_handle_human_task(
    db: Session,
    *,
    task: ExecutionHumanTask,
    user: ExecutionUser,
) -> bool:
    if user.role == "admin" or task.claimed_by_id == user.id:
        return True
    if task.assigned_user_id is not None:
        return task.assigned_user_id == user.id
    if task.candidate_role_key:
        return (
            db.query(ExecutionUserRole.id)
            .join(
                ExecutionRole,
                ExecutionRole.id == ExecutionUserRole.role_id,
            )
            .filter(
                ExecutionUserRole.user_id == user.id,
                ExecutionRole.key == task.candidate_role_key,
            )
            .first()
            is not None
        )
    run = db.get(ExecutionRun, task.run_id)
    return run is not None and run.created_by_id == user.id


def _definition_node_map(run: ExecutionRun) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in run.definition_snapshot["nodes"]}


def _lookup_path(
    root: dict[str, Any],
    expression: str,
    *,
    strict: bool = False,
) -> Any:
    if expression == "$":
        return root
    if not expression.startswith("$."):
        return expression
    current: Any = root
    for part in expression[2:].split("."):
        if isinstance(current, dict):
            if part not in current:
                if strict:
                    raise ExecutionApiError(
                        422,
                        "mapping_value_missing",
                        "输入映射引用的数据不存在",
                        details={"expression": expression},
                    )
                return None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                if strict:
                    raise ExecutionApiError(
                        422,
                        "mapping_value_missing",
                        "输入映射引用的列表位置不存在",
                        details={"expression": expression},
                    )
                return None
            current = current[index]
        else:
            if strict:
                raise ExecutionApiError(
                    422,
                    "mapping_value_missing",
                    "输入映射引用的数据路径不存在",
                    details={"expression": expression},
                )
            return None
    return current


def _resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$."):
        return _lookup_path(
            context,
            value,
            strict=value.startswith(("$.nodes.", "$.run.")),
        )
    if isinstance(value, dict):
        return {key: _resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, context) for item in value]
    return value


def _run_context(db: Session, run: ExecutionRun) -> dict[str, Any]:
    node_outputs = {
        node.node_id: {"output": node.output_data, "status": node.status}
        for node in db.query(ExecutionNodeRun)
        .filter(ExecutionNodeRun.run_id == run.id)
        .all()
    }
    return {
        "inputs": run.input_data,
        "globals": run.global_data,
        "nodes": node_outputs,
        "run": {
            "id": run.id,
            "inspection_number": run.inspection_number,
            "mode": run.mode,
        },
    }


def _node_input(
    db: Session,
    run: ExecutionRun,
    node: dict[str, Any],
) -> dict[str, Any]:
    # A v2 start node is the typed workflow input boundary.  Its immutable
    # effective schema is the Release input schema, so it must validate and
    # forward the already-normalized Run input instead of resolving an empty
    # input_mapping.  Historical v1 start-node behaviour remains unchanged.
    if run.release_id is not None and node.get("type") == "core.start":
        return deepcopy(run.input_data or {})
    mapping = node.get("input_mapping") or {}
    context = _run_context(db, run)
    node_type = node_registry.get(
        str(node.get("type") or ""),
        int(node.get("type_version") or 1),
    )
    input_schema = node_type.input_schema if node_type is not None else {}
    declared_properties = input_schema.get("properties") or {}
    required_properties = set(input_schema.get("required") or [])

    # A missing upstream path is still an error by default.  Only a top-level
    # input explicitly declared as optional by the versioned node contract may
    # resolve to None.  This keeps immutable, already-published runs compatible
    # when an upstream business object legitimately omits an optional field
    # (for example a task without judgement requirements), without weakening
    # required artifact and identity bindings.
    if isinstance(mapping, dict):
        resolved = {}
        for key, value in mapping.items():
            try:
                resolved[key] = _resolve_value(value, context)
            except ExecutionApiError as exc:
                is_declared_optional = (
                    exc.code == "mapping_value_missing"
                    and key in declared_properties
                    and key not in required_properties
                )
                if not is_declared_optional:
                    raise
                resolved[key] = None
    else:
        resolved = _resolve_value(mapping, context)
    return resolved if isinstance(resolved, dict) else {"value": resolved}


def resolve_node_input(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a reached node from the immutable run snapshot.

    Public staged mutation APIs use this instead of trusting caller-supplied
    file references or cell mappings.
    """

    node = _definition_node_map(run).get(node_run.node_id)
    if node is None or node.get("type") != node_run.node_type:
        raise conflict(
            "mutation_node_definition_missing",
            "运行快照缺少当前文件变更节点定义",
            node_id=node_run.node_id,
        )
    input_data = _node_input(db, run, node)
    _assert_declared_root_refs(run, input_data)
    return node, input_data


def _declared_root_ids(run: ExecutionRun) -> set[str]:
    return _declared_root_ids_from_definition(run.definition_snapshot)


def _declared_root_ids_from_definition(
    definition: dict[str, Any],
) -> set[str]:
    return {
        str(slot.get("root_id"))
        for slot in (definition.get("root_slots") or [])
        if isinstance(slot, dict) and slot.get("root_id")
    }


def _assert_root_refs_allowed(value: Any, allowed: set[str]) -> None:
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if "root_id" in item:
                root_id = str(item.get("root_id") or "")
                if root_id not in allowed:
                    raise ExecutionApiError(
                        422,
                        "artifact_root_not_declared",
                        "节点输入引用了流程未声明的文件根目录",
                        details={"root_id": root_id},
                    )
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


def _assert_declared_root_refs(run: ExecutionRun, value: Any) -> None:
    _assert_root_refs_allowed(value, _declared_root_ids(run))


def _candidate_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("candidates", "groups", "files", "items"):
        if key in value:
            nested = _candidate_items(value[key])
            if nested:
                return nested
    for nested_value in value.values():
        nested = _candidate_items(nested_value)
        if nested:
            return nested
    return []


def _validate_index_candidate(
    db: Session,
    *,
    run: ExecutionRun,
    candidate: dict[str, Any],
    gateway: FileGateway,
) -> None:
    members = candidate.get("files") or candidate.get("items")
    values = members if isinstance(members, list) and members else [candidate]
    allowed_roots = _declared_root_ids(run)
    for value in values:
        if not isinstance(value, dict) or not value.get("id"):
            raise ExecutionApiError(
                409,
                "file_candidate_invalid",
                "候选文件缺少服务器索引标识，请重新查询",
            )
        row = (
            db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
            .join(
                ExecutionStorageRoot,
                ExecutionStorageRoot.id
                == ExecutionFileIndexEntry.storage_root_id,
            )
            .filter(ExecutionFileIndexEntry.id == str(value["id"]))
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise ExecutionApiError(
                409,
                "file_candidate_stale",
                "候选文件已不在索引中，请刷新后重新选择",
            )
        entry, root = row
        if (
            entry.missing_since is not None
            or root.root_id not in allowed_roots
            or root.root_id != value.get("root_id")
            or entry.relative_path != value.get("relative_path")
            or entry.fingerprint != value.get("fingerprint")
        ):
            raise ExecutionApiError(
                409,
                "file_candidate_stale",
                "候选文件已变化或不属于本流程，请刷新后重新选择",
                details={"candidate_id": value.get("id")},
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
                "file_candidate_stale",
                "候选文件当前不可读取，请刷新后重新选择",
                details={"candidate_id": value.get("id")},
            ) from exc
        if f"{stat.st_size}:{stat.st_mtime_ns}" != entry.fingerprint:
            raise ExecutionApiError(
                409,
                "file_candidate_stale",
                "候选文件已在结果读取后发生变化，请刷新后重新选择",
                details={"candidate_id": value.get("id")},
            )


def _native_human_submission(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    task: ExecutionHumanTask | None,
    data: dict[str, Any],
    actor: ExecutionUser | None,
) -> dict[str, Any] | None:
    contract = _v2_node_instance_contract(run, node_run)
    if contract is None or contract.get("source") != "resource":
        return None
    node = _definition_node_map(run).get(node_run.node_id) or {}
    config = node.get("config") or {}
    suspension = contract.get("suspension") or {}
    resume_protocol = str(suspension.get("resume_protocol") or "")
    if (
        contract.get("execution_kind") == "automatic"
        and suspension.get("resume_protocol") == "retry_with_decision"
    ):
        if task is None:
            raise ExecutionApiError(
                500,
                "native_suspension_task_missing",
                "自动节点恢复决定缺少 HumanTask 上下文",
            )
        persisted = (node_run.input_data or {}).get("_native_suspension") or {}
        state = persisted.get("state") or {}
        decision = str(data.get("decision") or "")
        if decision not in {"overwrite", "cancel"}:
            raise ExecutionApiError(
                422,
                "file_batch_place_decision_invalid",
                "批量放置决定必须是 overwrite 或 cancel",
            )
        return {
            "_native_retry_with_decision": True,
            "decision": decision,
            "plan_digest": state.get("plan_digest"),
        }
    if resume_protocol == "native.form.v1":
        _assert_declared_root_refs(run, data)
        return dict(data)
    if resume_protocol == "native.select.v1":
        selected_values = data.get("selected_ids")
        if not isinstance(selected_values, list):
            raise ExecutionApiError(
                422,
                "human_select_ids_required",
                "选择任务只接受稳定 selected_ids",
            )
        selected_ids = list(dict.fromkeys(str(value) for value in selected_values))
        minimum = int(config.get("min_selected") or 0)
        maximum = int(config.get("max_selected") or 1)
        if not minimum <= len(selected_ids) <= maximum:
            raise ExecutionApiError(
                422,
                "human_select_count_invalid",
                "选择数量不符合节点契约",
                details={"minimum": minimum, "maximum": maximum},
            )
        offered = {
            str(item.get("id")): item
            for item in (node_run.input_data or {}).get("items") or []
            if isinstance(item, dict) and item.get("id")
        }
        if any(value not in offered for value in selected_ids):
            raise ExecutionApiError(
                409,
                "human_select_candidate_not_offered",
                "所选稳定 ID 不属于当前任务候选",
            )
        gateway = build_file_gateway(db)
        selected_items: list[dict[str, Any]] = []
        for candidate_id in selected_ids:
            candidate = offered[candidate_id]
            _validate_index_candidate(
                db,
                run=run,
                candidate=candidate,
                gateway=gateway,
            )
            selected_items.append(deepcopy(candidate))
        primary_id = data.get("primary_id")
        primary_id = str(primary_id) if primary_id not in (None, "") else None
        if primary_id is None and config.get("require_primary") and len(selected_ids) == 1:
            primary_id = selected_ids[0]
        if config.get("require_primary") and primary_id not in selected_ids:
            raise ExecutionApiError(
                422,
                "human_select_primary_invalid",
                "主项必须属于 selected_ids",
            )
        primary_item = offered.get(primary_id) if primary_id is not None else None
        from app.execution.v2.canonical import canonical_sha256

        selection = {
            "selected_ids": selected_ids,
            "selected_items": selected_items,
            "primary_id": primary_id,
            "primary_item": deepcopy(primary_item),
        }
        return {
            **selection,
            "selection_digest": canonical_sha256(selection),
        }
    if resume_protocol == "native.decision.v1":
        decision = str(data.get("decision") or "")
        allowed = {
            str(item.get("value"))
            for item in config.get("options") or []
            if isinstance(item, dict) and item.get("value")
        }
        if decision not in allowed:
            raise ExecutionApiError(
                422,
                "human_decision_invalid",
                "decision 不属于节点声明的 options",
            )
        reason = data.get("reason")
        if reason is not None:
            reason = str(reason)
        from app.execution.v2.canonical import canonical_sha256

        receipt = {"decision": decision, "reason": reason}
        return {**receipt, "decision_digest": canonical_sha256(receipt)}
    if resume_protocol == "native.approval.v1":
        if task is None or actor is None:
            raise ExecutionApiError(
                500,
                "approval_actor_context_missing",
                "批准任务缺少服务端 actor 上下文",
            )
        decision = str(data.get("decision") or "").lower()
        if decision not in {"approved", "rejected"}:
            raise ExecutionApiError(
                422,
                "human_approval_decision_invalid",
                "批准任务 decision 必须是 approved 或 rejected",
            )
        subject = (node_run.input_data or {}).get("subject") or {}
        subject_type = str(subject.get("type") or "")
        subject_digest = str(subject.get("digest") or "")
        if not subject_type or not re.fullmatch(r"[0-9a-f]{64}", subject_digest):
            raise ExecutionApiError(
                409,
                "approval_subject_invalid",
                "批准任务的 subject 快照无效",
            )
        role_keys = sorted(
            key
            for (key,) in db.query(ExecutionRole.key)
            .join(ExecutionUserRole, ExecutionUserRole.role_id == ExecutionRole.id)
            .filter(ExecutionUserRole.user_id == actor.id)
            .all()
        )
        authorization_snapshot = {
            "actor_role": actor.role,
            "role_keys": role_keys,
            "assigned_user_id": task.assigned_user_id,
            "candidate_role_key": task.candidate_role_key,
            "claimed_by_id": task.claimed_by_id,
        }
        from app.execution.v2.canonical import canonical_sha256

        created_at = utcnow()
        expires_at = created_at + NATIVE_APPROVAL_RECEIPT_TTL
        receipt_id = str(uuid4())
        payload = {
            "id": receipt_id,
            "run_id": run.id,
            "node_run_id": node_run.id,
            "human_task_id": task.id,
            "task_revision": task.revision,
            "subject_type": subject_type,
            "subject_digest": subject_digest,
            "decision": decision,
            "actor_user_id": actor.id,
            "authorization_snapshot": authorization_snapshot,
            "reason": str(data.get("reason") or "") or None,
            "expires_at": expires_at.isoformat(),
            "created_at": created_at.isoformat(),
        }
        receipt_digest = canonical_sha256(payload)
        receipt = ExecutionHumanApprovalReceipt(
            id=receipt_id,
            run_id=run.id,
            node_run_id=node_run.id,
            human_task_id=task.id,
            task_revision=task.revision,
            subject_type=subject_type,
            subject_digest=subject_digest,
            decision=decision,
            actor_user_id=actor.id,
            authorization_snapshot=authorization_snapshot,
            reason=payload["reason"],
            receipt_digest=receipt_digest,
            expires_at=expires_at,
            created_at=created_at,
        )
        db.add(receipt)
        public_receipt = {
            "id": receipt.id,
            "subject_type": subject_type,
            "subject_digest": subject_digest,
            "decision": decision,
            "actor_user_id": actor.id,
            "decided_at": created_at.isoformat(),
            "receipt_digest": receipt_digest,
        }
        if decision == "rejected":
            return {
                "_human_rejected": True,
                "reason": receipt.reason,
                "approval_receipt": public_receipt,
            }
        return {"approval_receipt": public_receipt}
    raise ExecutionApiError(
        503,
        "native_human_handler_unavailable",
        "未知的原生 Human suspension contract",
    )


def _normalize_human_submission(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    data: dict[str, Any],
    task: ExecutionHumanTask | None = None,
    actor: ExecutionUser | None = None,
) -> dict[str, Any]:
    native = _native_human_submission(
        db,
        run=run,
        node_run=node_run,
        task=task,
        data=data,
        actor=actor,
    )
    if native is not None:
        return native
    if node_run.node_type == "human.image_selection":
        selected_ids = data.get("selected_image_ids")
        if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 10:
            raise ExecutionApiError(
                422,
                "image_selection_count_invalid",
                "请选择 1 至 10 张图片",
            )
        offered_images = node_run.input_data.get("images") or []
        images_by_id = {
            str(item.get("id")): item
            for item in offered_images
            if isinstance(item, dict) and item.get("id")
        }
        folder_ids = (
            data.get("selected_folder_ids")
            or node_run.input_data.get("selected_folder_ids")
            or []
        )
        if not isinstance(folder_ids, list):
            raise ExecutionApiError(
                422, "image_folder_selection_invalid", "图片目录选择格式无效"
            )
        selected_folder_ids = list(dict.fromkeys(str(value) for value in folder_ids))
        offered_folder_ids = {
            str(item.get("id"))
            for item in (node_run.input_data.get("folders") or [])
            if isinstance(item, dict) and item.get("id")
        }
        if any(value not in offered_folder_ids for value in selected_folder_ids):
            raise ExecutionApiError(
                409,
                "image_folder_not_offered",
                "所选图片目录不在当前候选列表中",
            )
        if node_run.input_data.get("folder_selection_required") and not selected_folder_ids:
            raise ExecutionApiError(
                422, "image_folder_selection_required", "请先选择图片所在目录"
            )
        normalized_images: list[dict[str, Any]] = []
        seen: set[str] = set()
        gateway = build_file_gateway(db)
        for value in selected_ids:
            image_id = str(value)
            if image_id in seen:
                continue
            candidate = images_by_id.get(image_id)
            if candidate is None:
                raise ExecutionApiError(
                    409,
                    "image_candidate_not_offered",
                    "所选图片不在当前候选列表中",
                )
            if (
                selected_folder_ids
                and candidate.get("folder_id") not in selected_folder_ids
            ):
                raise ExecutionApiError(
                    422,
                    "image_candidate_outside_selected_folders",
                    "所选图片不属于已选择的目录",
                )
            _validate_index_candidate(
                db, run=run, candidate=candidate, gateway=gateway
            )
            normalized_images.append(candidate)
            seen.add(image_id)
        if not 1 <= len(normalized_images) <= 10:
            raise ExecutionApiError(
                422,
                "image_selection_count_invalid",
                "去除重复项后，请选择 1 至 10 张图片",
            )
        primary_image_id = data.get("primary_image_id")
        if primary_image_id is not None:
            primary_image_id = str(primary_image_id)
        elif len(normalized_images) == 1:
            primary_image_id = str(normalized_images[0]["id"])
        primary_image = next(
            (
                item
                for item in normalized_images
                if str(item.get("id")) == primary_image_id
            ),
            None,
        )
        if primary_image_id and primary_image is None:
            raise ExecutionApiError(
                422,
                "primary_image_not_selected",
                "主图必须是本次已选择的图片之一",
            )
        task_context: dict[str, Any] = {}
        if "task_validation_state" in (node_run.input_data or {}):
            task_snapshot = node_run.input_data.get("task")
            cache_state = node_run.input_data.get("task_cache_state")
            if not isinstance(task_snapshot, dict) or not task_snapshot:
                cached = cached_task_snapshot(
                    db,
                    inspection_number=run.inspection_number,
                )
                task_snapshot = cached.get("snapshot")
                cache_state = cached.get("cache_state")
            if not isinstance(task_snapshot, dict) or not task_snapshot:
                raise conflict(
                    "microscopy_task_snapshot_not_ready",
                    (
                        "旧系统任务信息尚未读取完成；图片选择会保留在当前页面，"
                        "请启动或检查 Windows 只读读取服务后重试提交"
                    ),
                    cache_state=cache_state or "pending",
                )
            matched_task_conditions = _task_project_conditions(
                task_snapshot,
                task_facts=resolve_rule(
                    db,
                    microscopy_rule_key(
                        microscopy_family_from_config(
                            {
                                "record_family": node_run.input_data.get(
                                    "record_family"
                                )
                            }
                        ).key
                    ),
                ).task_facts,
            )
            missing_task_conditions = [
                item
                for item in ("task_item_name", "test_method")
                if item not in matched_task_conditions
            ]
            task_context = {
                "task": task_snapshot,
                "task_cache_state": cache_state or "ready",
                "task_validation_state": (
                    "matched" if not missing_task_conditions else "warning"
                ),
                "missing_conditions": missing_task_conditions,
            }
        return {
            **data,
            "selected_folder_ids": selected_folder_ids,
            "selected_image_ids": [str(item["id"]) for item in normalized_images],
            "selected_images": normalized_images,
            "primary_image_id": primary_image_id,
            "primary_image": primary_image,
            **task_context,
        }
    task_kind = str(
        node_run.input_data.get("task_kind")
        or (node_run.input_data.get("record_context") or {}).get("task_kind")
        or ""
    )
    if task_kind == "microscopy_record_input":
        context = node_run.input_data.get("record_context") or node_run.input_data
        projects = context.get("projects") or []
        if not isinstance(projects, list) or not projects:
            raise ExecutionApiError(
                422,
                "microscopy_project_missing",
                "未读取到可用于生成微观形貌原始记录的检测项目",
            )
        selected_key = str(data.get("selected_project_key") or "").strip()
        selected_project = next(
            (
                value
                for index, value in enumerate(projects)
                if isinstance(value, dict)
                and str(
                    value.get("project_key")
                    or value.get("key")
                    or value.get("task_check_item_id")
                    or value.get("id")
                    or f"project-{index + 1}"
                )
                == selected_key
            ),
            None,
        )
        if selected_project is None:
            raise ExecutionApiError(
                409,
                "microscopy_project_not_offered",
                "所选检测项目不在当前任务快照中，请刷新后重试",
            )

        sample_name = " ".join(str(data.get("sample_name") or "").strip().split())
        if not sample_name:
            raise ExecutionApiError(
                422, "microscopy_sample_name_required", "请选择或填写样品名称"
            )
        if len(sample_name) > 500:
            raise ExecutionApiError(
                422, "microscopy_sample_name_too_long", "样品名称不能超过 500 个字符"
            )

        def compact_options(value: Any) -> list[str]:
            values = value if isinstance(value, list) else [value]
            result: list[str] = []
            seen: set[str] = set()
            for item in values:
                parts = (
                    re.split(r"[，,、]", item)
                    if isinstance(item, str)
                    else [item]
                )
                for part in parts:
                    text = " ".join(str(part or "").strip().split())
                    if text and text.casefold() not in seen:
                        result.append(text)
                        seen.add(text.casefold())
            return result

        identities = compact_options(
            selected_project.get("sample_identify")
            or selected_project.get("sample_identity")
            or selected_project.get("sample_identification")
            or context.get("sample_identify")
            or context.get("sample_identity")
        )
        submitted_identity = " ".join(
            str(data.get("sample_identity") or "").strip().split()
        )
        if len(identities) == 1:
            sample_identity = identities[0]
        elif identities:
            if submitted_identity not in identities:
                raise ExecutionApiError(
                    409,
                    "microscopy_sample_identity_invalid",
                    "填写的样品识别不在任务单列表中，无法对应旧系统下拉框",
                )
            sample_identity = submitted_identity
        elif submitted_identity:
            raise ExecutionApiError(
                409,
                "microscopy_sample_identity_invalid",
                "任务单未提供样品识别，不能写入旧系统下拉框",
            )
        else:
            sample_identity = None

        raw_judgement_flag = selected_project.get(
            "give_judgement", context.get("give_judgement")
        )
        judgement_required = not (
            raw_judgement_flag is None
            or raw_judgement_flag is False
            or raw_judgement_flag == 0
            or str(raw_judgement_flag).strip().casefold()
            in {"", "0", "false", "no", "否", "否定"}
        )
        basis_options = compact_options(
            selected_project.get("check_basis_options")
            or selected_project.get("judge_basis_options")
            or context.get("check_basis_options")
        )
        judgement_options = compact_options(
            selected_project.get("judgement_options")
            or context.get("judgement_options")
            or ["符合", "不符合"]
        )
        if judgement_required:
            submitted_basis = " ".join(
                str(data.get("judge_basis") or "").strip().split()
            )
            if submitted_basis:
                # 候选仅供快捷选择，允许人工改写——判定依据写入的是 Excel
                # 文本单元格，不受旧系统下拉框约束。
                judge_basis = submitted_basis
            elif len(basis_options) == 1:
                judge_basis = basis_options[0]
            else:
                raise ExecutionApiError(
                    422,
                    "microscopy_judge_basis_required",
                    "任务单要求判定，请填写判定依据",
                )
            indicator_requirement = " ".join(
                str(data.get("indicator_requirement") or "").strip().split()
            )
            if not indicator_requirement:
                raise ExecutionApiError(
                    422,
                    "microscopy_indicator_requirement_required",
                    "任务单要求判定，请填写指标要求",
                )
            test_result = " ".join(
                str(data.get("test_result") or "").strip().split()
            )
            if not test_result:
                raise ExecutionApiError(
                    422,
                    "microscopy_test_result_required",
                    "任务单要求判定，请填写测试结果",
                )
            judgement = " ".join(
                str(data.get("judgement") or "").strip().split()
            )
            if not judgement or (
                judgement_options and judgement not in judgement_options
            ):
                raise ExecutionApiError(
                    422,
                    "microscopy_judgement_invalid",
                    "请选择本次判定结果",
                )
        else:
            judge_basis = None
            indicator_requirement = None
            test_result = None
            judgement = None

        remark = " ".join(str(data.get("remark") or "").strip().split())
        if any(
            len(value or "") > 1000
            for value in (
                judge_basis,
                indicator_requirement,
                test_result,
                remark,
            )
        ):
            raise ExecutionApiError(
                422,
                "microscopy_record_field_too_long",
                "判定信息、测试结果或备注不能超过 1000 个字符",
            )
        check_count = selected_project.get("check_count")
        identity_count_mismatch = bool(
            identities
            and isinstance(check_count, int)
            and not isinstance(check_count, bool)
            and len(identities) != check_count
        )

        return {
            "selected_project_key": selected_key,
            "selected_project": selected_project,
            "sample_name": sample_name,
            "sample_identity": sample_identity,
            "sample_identity_options": identities,
            "identity_count_mismatch": identity_count_mismatch,
            "judgement_required": judgement_required,
            "judge_basis": judge_basis,
            "indicator_requirement": indicator_requirement,
            "test_result": test_result,
            "judgement": judgement,
            "remark": remark,
        }
    if task_kind == "microscopy_print_confirmation":
        artifact = node_run.input_data.get("artifact") or {}
        expected_sha = str(
            artifact.get("sha256") or artifact.get("content_sha256") or ""
        ).strip().casefold()
        submitted_sha = str(data.get("artifact_sha256") or "").strip().casefold()
        print_decision = str(data.get("print_decision") or "").strip().casefold()
        # Compatibility for human tasks created by the previously published
        # contract.  New tasks submit an explicit decision; an old
        # ``printed=true`` acknowledgement already meant printing was complete.
        legacy_print_confirmation = bool(
            not print_decision and data.get("printed") is True
        )
        if legacy_print_confirmation:
            print_decision = "print"
        if print_decision not in {"print", "skip"}:
            raise ExecutionApiError(
                422,
                "microscopy_print_confirmation_required",
                "请选择打印原始记录或暂不打印",
            )
        if not expected_sha or submitted_sha != expected_sha:
            raise ExecutionApiError(
                409,
                "microscopy_print_artifact_changed",
                "待打印原始记录已变化，请重新打开并核对",
            )
        print_requested = print_decision == "print"
        print_completed = bool(
            legacy_print_confirmation or data.get("print_completed") is True
        )
        if print_requested and not print_completed:
            raise ExecutionApiError(
                422,
                "microscopy_print_not_completed",
                "请确认已在 Excel 中完成打印",
            )
        return {
            "print_decision": print_decision,
            "print_requested": print_requested,
            "print_completed": print_requested and print_completed,
            "printed": print_requested and print_completed,
            "artifact_sha256": expected_sha,
            "artifact": artifact,
        }
    if node_run.node_type != "human.file_selection":
        _assert_declared_root_refs(run, data)
        if node_run.node_type == "human.input":
            node = _definition_node_map(run).get(node_run.node_id) or {}
            config = node.get("config") or {}
            if config.get("paper_existing_record_decision") or config.get(
                "legacy_existing_record_decision"
            ):
                selected_project = node_run.input_data.get("selected_project")
                task = node_run.input_data.get("task")
                if not isinstance(selected_project, dict) or not isinstance(
                    task, dict
                ):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_context_changed",
                        "检验记录登记上下文已变化，请重新运行流程",
                    )
                check_count = selected_project.get("check_count")
                register_count = selected_project.get("register_count")
                if (
                    not isinstance(check_count, int)
                    or isinstance(check_count, bool)
                    or check_count < 1
                    or not isinstance(register_count, int)
                    or isinstance(register_count, bool)
                    or register_count < 0
                ):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_count_invalid",
                        "旧系统返回的检测份数或已有登记数量无效，请刷新后重试",
                    )
                confirmation_required = check_count == 1 and register_count > 0
                action = str(
                    data.get("existing_record_action") or ""
                ).strip()
                if confirmation_required and action not in {"append", "cancel"}:
                    raise ExecutionApiError(
                        422,
                        "paper_existing_record_decision_required",
                        "当前项目已有登记，请选择直接新增或取消",
                    )
                if not confirmation_required:
                    action = "continue"
                return {
                    "existing_record_action": action,
                    "registration_cancelled": action == "cancel",
                    "expected_existing_register_count": register_count,
                    "selected_project": selected_project,
                    "selected_project_key": selected_project.get("project_key"),
                    "task": task,
                }
            if config.get("paper_judgement") or config.get(
                "legacy_generic_record_input"
            ):
                selected_project = node_run.input_data.get("selected_project")
                if not isinstance(selected_project, dict):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_context_changed",
                        "纸浆项目登记上下文已变化，请重新运行流程",
                    )
                identities = _compact_text_options(
                    selected_project.get("sample_identify")
                )
                sample_identity = " ".join(
                    str(data.get("sample_identity") or "").strip().split()
                )
                if identities:
                    if not sample_identity:
                        raise ExecutionApiError(
                            422,
                            "paper_sample_identity_required",
                            "请确认当前录入的样品识别",
                        )
                    if sample_identity not in identities:
                        raise ExecutionApiError(
                            409,
                            "paper_sample_identity_not_offered",
                            "填写的样品识别不在任务单列表中，无法对应旧系统下拉框",
                        )
                elif sample_identity:
                    raise ExecutionApiError(
                        409,
                        "paper_sample_identity_not_offered",
                        "任务单未提供样品识别，不能写入旧系统下拉框",
                    )

                judgement_required = _truthy_judgement_flag(
                    selected_project.get("give_judgement")
                )
                judge_basis = " ".join(
                    str(data.get("judge_basis") or "").strip().split()
                )
                judgement = " ".join(
                    str(data.get("judgement") or "").strip().split()
                )
                standard_value = " ".join(
                    str(data.get("standard_value") or "").strip().split()
                )
                standard_value_required = bool(
                    config.get(
                        "require_standard_value",
                        config.get("paper_judgement") is True,
                    )
                )
                if (
                    judgement_required
                    and standard_value_required
                    and not standard_value
                ):
                    raise ExecutionApiError(
                        422,
                        "paper_standard_value_required",
                        "请填写标准值与允差",
                    )
                if judgement_required and (not judge_basis or not judgement):
                    raise ExecutionApiError(
                        422,
                        "paper_judgement_required",
                        "请确认判定依据与判定结果",
                    )
                if not judgement_required:
                    judge_basis = ""
                    judgement = ""
                    standard_value = ""
                check_count = selected_project.get("check_count")
                identity_count_mismatch = bool(
                    identities
                    and isinstance(check_count, int)
                    and not isinstance(check_count, bool)
                    and len(identities) != check_count
                )
                return {
                    "judgement_required": judgement_required,
                    "judge_basis": judge_basis,
                    "judgement": judgement,
                    "standard_value": standard_value,
                    "sample_identity": sample_identity or None,
                    "sample_identity_options": identities,
                    "identity_count_mismatch": identity_count_mismatch,
                }
            if config.get("report_image_placement"):
                return normalize_report_image_placement_submission(
                    db,
                    run=run,
                    node_run=node_run,
                    node=node,
                    data=data,
                )
        return data
    selected = data.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise ExecutionApiError(
            422,
            "file_selection_required",
            "请至少选择一个候选文件或采集组",
        )
    candidates = _candidate_items(node_run.input_data or {})
    candidates_by_id = {
        str(candidate["id"]): candidate
        for candidate in candidates
        if candidate.get("id")
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    gateway = build_file_gateway(db)
    for submitted in selected:
        candidate_id = (
            submitted
            if isinstance(submitted, str)
            else submitted.get("id")
            if isinstance(submitted, dict)
            else None
        )
        candidate = candidates_by_id.get(str(candidate_id))
        if candidate is None:
            raise ExecutionApiError(
                409,
                "file_candidate_not_offered",
                "所选文件不在该人工任务的候选列表中",
            )
        if candidate.get("read_status") == "failed":
            raise ExecutionApiError(
                422,
                "result_file_not_selectable",
                "结果读取失败的文件不能被选用",
                details={"candidate_id": str(candidate_id)},
            )
        if str(candidate_id) in seen:
            continue
        _validate_index_candidate(
            db,
            run=run,
            candidate=candidate,
            gateway=gateway,
        )
        normalized.append(candidate)
        seen.add(str(candidate_id))

    node = _definition_node_map(run).get(node_run.node_id) or {}
    config = node.get("config") or {}
    if config.get("allow_multiple") is False and len(normalized) > 1:
        raise ExecutionApiError(
            422,
            "file_selection_multiple_not_allowed",
            "当前步骤只能选择一份文件",
        )
    require_primary = bool(config.get("require_primary"))
    primary_file_id = data.get("primary_file_id")
    if primary_file_id is not None:
        primary_file_id = str(primary_file_id)
    if primary_file_id is None and require_primary and len(normalized) == 1:
        primary_file_id = str(normalized[0]["id"])
    if require_primary and not primary_file_id:
        raise ExecutionApiError(
            422,
            "primary_file_required",
            "选择多个文件时，请指定其中一个文件作为主单",
        )
    primary_file = None
    if primary_file_id:
        primary_file = next(
            (
                candidate
                for candidate in normalized
                if str(candidate.get("id")) == primary_file_id
            ),
            None,
        )
        if primary_file is None:
            raise ExecutionApiError(
                422,
                "primary_file_not_selected",
                "主单必须是本次已选择的文件之一",
                details={"primary_file_id": primary_file_id},
            )
    return {
        **data,
        "selected_files": normalized,
        "primary_file_id": primary_file_id,
        "primary_file": primary_file,
    }


def _auto_submit_single_candidate(
    db: Session,
    context: NodeExecutionContext,
) -> Optional[dict[str, Any]]:
    """仅匹配到一份候选时直接规范化并返回节点输出，实现零人工干预。

    返回 None 表示不满足自动提交条件，仍按原路径创建人工任务：
    - 节点 config 显式开启 auto_submit_single_candidate（按流程/节点
      逐一放行，未开启的流程交互不变）；
    - 仅限 human.file_selection；
    - 表单没有任何需要人工填写的字段（无 required/properties），
      避免跳过备注、确认类输入；
    - 候选恰好一份且可读。
    规范化失败（如候选已变化）按 ExecutionApiError 上抛，由调用方
    fail_node，保持失败关闭而不是带病推进。
    """
    if context.node_run.node_type != "human.file_selection":
        return None
    config = context.node.get("config") or {}
    if not config.get("auto_submit_single_candidate"):
        return None
    form_schema = config.get("form_schema") or {}
    if form_schema.get("required") or form_schema.get("properties"):
        return None
    candidates = [
        candidate
        for candidate in _candidate_items(context.input_data or {})
        if candidate.get("id") and candidate.get("read_status") != "failed"
    ]
    if len(candidates) != 1:
        return None
    candidate_id = str(candidates[0]["id"])
    normalized = _normalize_human_submission(
        db,
        run=context.run,
        node_run=context.node_run,
        data={
            "selected_files": [candidate_id],
            "primary_file_id": candidate_id,
        },
    )
    return {
        **normalized,
        "auto_submitted": True,
        "auto_submit_reason": "single_candidate",
    }


def _paper_judgement_node_config(context: "NodeExecutionContext") -> dict[str, Any]:
    """通用登记人工确认节点的 config（保留纸类旧标记兼容性）。"""

    if context.node_run.node_type != "human.input":
        return {}
    config = context.node.get("config") or {}
    return (
        config
        if config.get("paper_judgement")
        or config.get("legacy_generic_record_input")
        else {}
    )


def _truthy_judgement_flag(value: Any) -> bool:
    return not (
        value is None
        or value is False
        or value == 0
        or str(value).strip().casefold() in {"", "0", "false", "no", "否", "否定"}
    )


def _paper_selected_record_result(context: "NodeExecutionContext") -> dict[str, Any]:
    """已选纸类原始记录的 result（含 W32/M32），供判定表单展示数据源。

    判定节点本身不映射选择节点输出（保持 DAG 定义不变），这里按流程定义
    找到 human.file_selection 节点并读取其已成功 node_run 的
    primary_file.result。节点尚未完成时返回空 dict，表单退化为无数据源。
    """

    definition = _definition_node_map(context.run)
    selection_node_ids = [
        node_id
        for node_id, node in definition.items()
        if isinstance(node, dict) and node.get("type") == "human.file_selection"
    ]
    if not selection_node_ids:
        return {}
    node_runs = (
        context.db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == context.run.id,
            ExecutionNodeRun.node_id.in_(selection_node_ids),
            ExecutionNodeRun.status == "succeeded",
        )
        .all()
    )
    for node_run in node_runs:
        primary = (node_run.output_data or {}).get("primary_file") or {}
        result = primary.get("result")
        if isinstance(result, dict) and (
            result.get("w32_value") or result.get("m32_value")
        ):
            return result
    return {}


def _compact_text_options(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        parts = re.split(r"[，,、]", item) if isinstance(item, str) else [item]
        for part in parts:
            text = " ".join(str(part or "").strip().split())
            if text and text.casefold() not in seen:
                result.append(text)
                seen.add(text.casefold())
    return result


def _paper_existing_record_node_config(
    context: "NodeExecutionContext",
) -> dict[str, Any]:
    if context.node_run.node_type != "human.input":
        return {}
    config = context.node.get("config") or {}
    return (
        config
        if config.get("paper_existing_record_decision")
        or config.get("legacy_existing_record_decision")
        else {}
    )


def _refresh_paper_registration_context(
    context: "NodeExecutionContext",
) -> None:
    """Refresh current legacy counts immediately before record entry.

    The ordinary task snapshot is cached for recommendations.  A second paper
    or microscopy run may therefore start while that cache still predates the
    first run's record entry.  Every registration decision forces a read-only
    refresh after upload/review and refuses to continue until the exact
    task-project key is present in the new snapshot, so the frozen expected
    register count equals the live count the Writer verifies.  Multi-copy
    projects then append their selected sample identity without pausing.
    """

    if not _paper_existing_record_node_config(context):
        return
    selected = context.input_data.get("selected_project")
    project_key = str(
        (selected or {}).get("project_key")
        if isinstance(selected, dict)
        else ""
    ).strip()
    if not project_key:
        raise ExecutionApiError(
            409,
            "paper_registration_context_changed",
            "检验记录项目缺少稳定任务绑定，请重新运行流程",
        )

    row, _queued = request_task_snapshot_refresh(
        context.db,
        inspection_number=context.run.inspection_number,
        force=True,
    )
    baseline_revision = int(row.revision or 0)
    # The Bridge is a separate process and cannot see this queue row until the
    # current transaction commits.  The node lease remains active while polling.
    context.db.commit()
    deadline = time.monotonic() + max(
        0, int(settings.EXECUTION_TASK_SNAPSHOT_WAIT_SECONDS)
    )
    snapshot: dict[str, Any] | None = None
    refresh_status = str(row.status or "")
    while True:
        context.db.expire_all()
        cached = cached_task_snapshot(
            context.db,
            inspection_number=context.run.inspection_number,
        )
        refresh_status = str(cached.get("refresh_status") or "")
        revision = int(cached.get("revision") or 0)
        candidate = cached.get("snapshot")
        if (
            refresh_status == "ready"
            and revision > baseline_revision
            and isinstance(candidate, dict)
        ):
            snapshot = candidate
            break
        if refresh_status == "failed":
            raise ExecutionApiError(
                422,
                "paper_registration_snapshot_failed",
                "录入前读取旧系统已有登记数量失败，请检查快照连接器后重试",
            )
        if time.monotonic() >= deadline:
            raise ExecutionApiError(
                422,
                "paper_registration_snapshot_pending",
                "录入前正在读取旧系统已有登记数量，请稍后重试该节点",
            )
        time.sleep(2)

    fresh_project = next(
        (
            value
            for value in snapshot.get("projects") or []
            if isinstance(value, dict)
            and str(value.get("project_key") or "").strip() == project_key
        ),
        None,
    )
    if fresh_project is None:
        raise ExecutionApiError(
            409,
            "paper_registration_project_changed",
            "录入前任务项目已变化，请重新运行流程",
        )
    context.input_data = {
        **context.input_data,
        "selected_project_key": project_key,
        "selected_project": dict(fresh_project),
        "task": snapshot,
    }
    context.node_run.input_data = context.input_data


def _auto_complete_paper_existing_record_decision(
    context: "NodeExecutionContext",
) -> Optional[dict[str, Any]]:
    """Pause only when a one-copy project has an existing record.

    Every registration decision first refreshes the read-only task snapshot:
    the frozen ``expected_existing_register_count`` must equal the live legacy
    count the Writer verifies at execution time, and the recommendation cache
    may still predate a previous run's record entry.  A multi-copy project
    then always appends the sample identity selected earlier in the run — its
    task count is not a hard cap on legacy record rows, so no user decision is
    needed.  For a one-copy project the decision depends on current occupancy,
    so it pauses when a record already exists.
    """

    if not _paper_existing_record_node_config(context):
        return None
    selected = context.input_data.get("selected_project") or {}
    check_count = selected.get("check_count")
    if (
        not isinstance(check_count, int)
        or isinstance(check_count, bool)
        or check_count < 1
    ):
        raise ExecutionApiError(
            409,
            "paper_registration_count_invalid",
            "旧系统返回的检测份数无效，请刷新后重试",
        )
    _refresh_paper_registration_context(context)
    selected = context.input_data.get("selected_project") or {}
    check_count = selected.get("check_count")
    register_count = selected.get("register_count")
    if (
        not isinstance(check_count, int)
        or isinstance(check_count, bool)
        or check_count < 1
        or not isinstance(register_count, int)
        or isinstance(register_count, bool)
        or register_count < 0
    ):
        raise ExecutionApiError(
            409,
            "paper_registration_count_invalid",
            "旧系统返回的检测份数或已有登记数量无效，请刷新后重试",
        )
    if check_count == 1 and register_count > 0:
        return None
    return {
        "existing_record_action": "continue",
        "registration_cancelled": False,
        "expected_existing_register_count": register_count,
        "selected_project": selected,
        "selected_project_key": selected.get("project_key"),
        "task": context.input_data.get("task"),
        "auto_submitted": True,
        "auto_submit_reason": (
            "multi_copy_capacity_is_informational"
            if register_count >= check_count
            else "registration_capacity_available"
        ),
    }


def _paper_existing_record_form_schema(
    context: "NodeExecutionContext",
) -> Optional[dict[str, Any]]:
    if not _paper_existing_record_node_config(context):
        return None
    selected = context.input_data.get("selected_project") or {}
    register_count = selected.get("register_count")
    return {
        "type": "object",
        "properties": {
            "existing_record_action": {
                "type": "string",
                "title": "当前项目已有登记，如何处理",
                "description": (
                    f"旧系统当前已有 {register_count} 条登记。"
                    "选择直接新增将再写入一条；选择取消则结束本次录入分支。"
                ),
                "enum": ["append", "cancel"],
                "enumNames": ["直接新增", "取消"],
            }
        },
        "required": ["existing_record_action"],
        "additionalProperties": False,
    }


def _auto_complete_paper_judgement(
    context: "NodeExecutionContext",
) -> Optional[dict[str, Any]]:
    """任务单未要求判定时自动完成纸类判定节点，避免无谓的人工打断。

    返回 None 表示任务单要求判定（give_judgement 为真），仍按原路径
    创建人工任务收集判定依据与判定结果。
    """

    if not _paper_judgement_node_config(context):
        return None
    input_data = context.input_data or {}
    selected_project = input_data.get("selected_project") or {}
    give_judgement = (
        selected_project.get("give_judgement")
        if isinstance(selected_project, dict)
        else None
    )
    identities = _compact_text_options(
        selected_project.get("sample_identify")
        if isinstance(selected_project, dict)
        else None
    )
    if _truthy_judgement_flag(give_judgement) or identities:
        return None
    return {
        "judgement_required": False,
        "judge_basis": None,
        "judgement": None,
        "standard_value": None,
        "sample_identity": None,
        "sample_identity_options": [],
        "identity_count_mismatch": False,
        "auto_submitted": True,
        "auto_submit_reason": "judgement_not_required",
    }


def _paper_judgement_form_schema(
    context: "NodeExecutionContext",
) -> Optional[dict[str, Any]]:
    """为纸类录入确认节点生成样品识别与判定字段。

    “标准值与允差”默认填入所选原始记录的 Sheet1!W32 结果（与人工登记的
    同文样式一致），并提供任务单说明列与 Sheet1!M32 作为可复制/填入的
    数据源。样品识别按中英文逗号及顿号拆分：单值自动填入并直接展示，
    多值提供可输入的候选列表；检测份数不一致只警告、不阻断。
    """

    if not _paper_judgement_node_config(context):
        return None
    input_data = context.input_data or {}
    task = input_data.get("task")
    check_basis = task.get("check_basis") if isinstance(task, dict) else None
    basis_options = _compact_text_options(check_basis)
    if basis_options:
        basis_field: dict[str, Any] = {
            "type": "string",
            "title": "判定依据",
            "enum": basis_options,
        }
    else:
        basis_field = {
            "type": "string",
            "title": "判定依据",
            "minLength": 1,
            "maxLength": 500,
        }
    selected_project = input_data.get("selected_project")
    selected_project = (
        selected_project if isinstance(selected_project, dict) else {}
    )
    identities = _compact_text_options(selected_project.get("sample_identify"))
    check_count = selected_project.get("check_count")
    identity_count_mismatch = bool(
        identities
        and isinstance(check_count, int)
        and not isinstance(check_count, bool)
        and len(identities) != check_count
    )
    remark = " ".join(
        str(
            selected_project.get("remark") or ""
        ).strip().split()
    )
    record_result = _paper_selected_record_result(context)
    w32_value = " ".join(
        str(record_result.get("w32_value") or "").strip().split()
    )
    m32_value = " ".join(
        str(record_result.get("m32_value") or "").strip().split()
    )
    copy_sources: list[dict[str, str]] = []
    if remark:
        copy_sources.append({"label": "任务单说明列", "text": remark})
    if m32_value:
        copy_sources.append({"label": "Sheet1!M32", "text": m32_value})
    standard_value_required = bool(
        _paper_judgement_node_config(context).get(
            "require_standard_value",
            _paper_judgement_node_config(context).get("paper_judgement")
            is True,
        )
    )
    standard_field: dict[str, Any] = {
        "type": "string",
        "title": "标准值与允差",
        "minLength": 1,
        "maxLength": 500,
        "description": (
            "默认填入 Sheet1!W32 结果；如与本单判定要求不一致，"
            "请从上方数据源复制或选词修改。"
        ),
    }
    if w32_value:
        standard_field["default"] = w32_value
    if copy_sources:
        standard_field["x-copy-sources"] = copy_sources
    properties: dict[str, Any] = {}
    required: list[str] = []
    if identities:
        identity_field: dict[str, Any] = {
            "type": "string",
            "title": "样品识别",
            "minLength": 1,
            "maxLength": 500,
            "description": (
                "必须与任务单样品识别及旧系统顶部下拉框中的一项严格一致。"
            ),
        }
        if len(identities) == 1:
            identity_field.update(
                {
                    "default": identities[0],
                    "const": identities[0],
                    "readOnly": True,
                }
            )
        else:
            identity_field["x-suggestions"] = identities
            identity_field["placeholder"] = "请选择或输入任务单中的样品识别"
        properties["sample_identity"] = identity_field
        required.append("sample_identity")

    judgement_required = _truthy_judgement_flag(
        selected_project.get("give_judgement")
    )
    if judgement_required:
        judgement_properties: dict[str, Any] = {
            "judge_basis": basis_field,
            "judgement": {
                "type": "string",
                "title": "判定结果",
                "enum": ["符合", "不符合"],
            },
        }
        if standard_value_required:
            judgement_properties["standard_value"] = standard_field
        properties.update(judgement_properties)
        required.extend(["judge_basis", "judgement"])
        if standard_value_required:
            required.append("standard_value")

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if identity_count_mismatch:
        schema["x-warning"] = (
            f"任务单检测份数为 {check_count}，样品识别拆分后为 "
            f"{len(identities)} 项；请核对后继续，本提示不会终止流程。"
        )
    return schema


def effective_human_task_form_schema(
    task: ExecutionHumanTask,
) -> dict[str, Any]:
    """Return the current paper-judgement schema for persisted open tasks.

    Human-task schemas are stored with the run.  Tasks created before the
    standard-value field was introduced therefore still carry the old strict
    two-field schema.  Regenerating only this built-in dynamic schema keeps
    those tasks usable without weakening validation for arbitrary workflows.
    """

    persisted = task.form_schema or {}
    if task.status not in {"open", "claimed"}:
        return persisted
    db = object_session(task)
    node_run = task.node_run
    run = node_run.run if node_run is not None else None
    if db is None or node_run is None or run is None:
        return persisted
    node = _definition_node_map(run).get(node_run.node_id)
    if not isinstance(node, dict):
        return persisted
    context = NodeExecutionContext(
        db=db,
        run=run,
        node_run=node_run,
        node=node,
        input_data=node_run.input_data or {},
        worker_id="human-task-schema",
        lease_token=node_run.lease_token or "",
    )
    current = _paper_judgement_form_schema(context)
    return current if current is not None else persisted


def _reopen_paper_judgement_for_standard_value(
    db: Session,
    context: "NodeExecutionContext",
) -> bool:
    """Reopen a pre-upgrade judgement task instead of mirroring W32 silently.

    The paper workflow places the judgement node immediately before the final
    record-entry node.  A run that crossed that node before this contract was
    introduced has no human-confirmed ``standard_value``.  Before preparing
    any external side effect, move that single edge back to human confirmation
    and retire the current Worker attempt.  Submitting the reopened task
    resolves the edge normally and makes the entry node runnable again.
    """

    if context.node_run.node_type != LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE:
        return False
    input_data = context.input_data or {}
    selected_project = input_data.get("selected_project")
    give_judgement = (
        selected_project.get("give_judgement")
        if isinstance(selected_project, dict)
        else None
    )
    if not _truthy_judgement_flag(give_judgement):
        return False
    judgement_input = input_data.get("judgement_input")
    if isinstance(judgement_input, dict) and str(
        judgement_input.get("standard_value") or ""
    ).strip():
        return False

    run, entry_node = _lock_run_and_node(db, context.node_run.id)
    if (
        entry_node.status != "running"
        or entry_node.lease_token != context.lease_token
    ):
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，不能重新打开判定确认任务",
            node_id=entry_node.node_id,
        )
    judgement_definitions = [
        node
        for node in (run.definition_snapshot or {}).get("nodes", [])
        if isinstance(node, dict)
        and isinstance(node.get("config"), dict)
        and node["config"].get("paper_judgement")
    ]
    if len(judgement_definitions) != 1:
        return False
    judgement_definition = judgement_definitions[0]
    judgement_node = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_id == judgement_definition.get("id"),
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if judgement_node is None or judgement_node.status != "succeeded":
        return False
    task = (
        db.query(ExecutionHumanTask)
        .filter(ExecutionHumanTask.node_run_id == judgement_node.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    edge = (
        db.query(ExecutionEdgeRun)
        .filter(
            ExecutionEdgeRun.run_id == run.id,
            ExecutionEdgeRun.source_node_id == judgement_node.node_id,
            ExecutionEdgeRun.target_node_id == entry_node.node_id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if task is None or task.status != "completed" or edge is None:
        return False

    judgement_context = NodeExecutionContext(
        db=db,
        run=run,
        node_run=judgement_node,
        node=judgement_definition,
        input_data=judgement_node.input_data or {},
        worker_id="paper-judgement-upgrade",
        lease_token="",
    )
    form_schema = _paper_judgement_form_schema(judgement_context)
    if form_schema is None:
        return False

    previous_output = (
        dict(judgement_node.output_data)
        if isinstance(judgement_node.output_data, dict)
        else {}
    )
    draft_data = {
        key: previous_output[key]
        for key in ("judge_basis", "judgement")
        if previous_output.get(key)
    }
    _finish_attempt(
        db,
        entry_node,
        lease_token=context.lease_token,
        status="cancelled",
        error_code="paper_judgement_contract_upgraded",
        error_message="等待人工补充确认标准值与允差",
    )
    entry_node.status = "pending"
    entry_node.input_data = {}
    entry_node.output_data = {}
    entry_node.error_code = None
    entry_node.error_message = None
    entry_node.ready_at = utcnow()
    entry_node.started_at = None
    entry_node.finished_at = None
    entry_node.lease_owner = None
    entry_node.lease_token = None
    entry_node.lease_expires_at = None

    edge.status = "pending"
    edge.resolved_at = None
    judgement_node.status = "waiting_human"
    judgement_node.output_data = {}
    judgement_node.error_code = None
    judgement_node.error_message = None
    judgement_node.finished_at = None

    task.status = "open"
    task.form_schema = form_schema
    task.draft_data = draft_data
    task.result_data = {}
    task.revision += 1
    task.claimed_by_id = None
    task.claimed_at = None
    task.completed_by_id = None
    task.completed_at = None
    if run.status != "paused":
        run.status = "waiting_human"
    run.finished_at = None
    append_run_event(
        db,
        run_id=run.id,
        event_type="human_task.reopened",
        payload={
            "task_id": task.id,
            "node_id": judgement_node.node_id,
            "reason": "paper_standard_value_confirmation_required",
            "reset_node_id": entry_node.node_id,
        },
    )
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.reset_for_human_confirmation",
        payload={
            "node_id": entry_node.node_id,
            "human_task_id": task.id,
            "reason": "paper_standard_value_confirmation_required",
        },
    )
    return True


def _latest_published_version(
    db: Session,
    workflow: ExecutionWorkflow,
) -> Optional[ExecutionWorkflowVersion]:
    if workflow.published_version_number is None:
        return None
    return (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow.id,
            ExecutionWorkflowVersion.version_number
            == workflow.published_version_number,
        )
        .one_or_none()
    )


def _raise_payload_validation(
    *,
    schema: dict[str, Any],
    value: Any,
    path_prefix: str,
    code: str,
    message: str,
) -> None:
    result = validate_json_instance(
        schema,
        value,
        path_prefix=path_prefix,
    )
    if not result.valid:
        raise ExecutionApiError(
            422,
            code,
            message,
            details=result.as_dict(),
        )


def _assert_idempotent_run_matches(
    existing: ExecutionRun,
    *,
    workflow_id: str,
    inspection_number: str,
    mode: str,
    definition_checksum_value: str,
    capabilities_snapshot: dict[str, Any],
    contract_checksum_value: str,
    deployed_contract_checksum_value: str | None = None,
    input_data: dict[str, Any],
    global_data: dict[str, Any],
) -> None:
    if not (
        existing.workflow_id == workflow_id
        and existing.inspection_number == inspection_number
        and existing.mode == mode
        and existing.definition_checksum == definition_checksum_value
        and (existing.capabilities_snapshot or {}) == capabilities_snapshot
        and existing.contract_checksum == contract_checksum_value
        and (
            deployed_contract_checksum_value is None
            or existing.deployed_contract_checksum
            == deployed_contract_checksum_value
        )
        and (existing.input_data or {}) == input_data
        and (existing.global_data or {}) == global_data
    ):
        raise conflict(
            "run_idempotency_conflict",
            "该幂等键已用于另一组运行参数",
            existing_run_id=existing.id,
        )


def assert_idempotent_run_matches(
    existing: ExecutionRun,
    *,
    workflow: ExecutionWorkflow,
    inspection_number: str,
    input_data: dict[str, Any],
    global_data: dict[str, Any],
    mode: str,
    definition: dict[str, Any],
    capabilities: Optional[dict[str, Any]] = None,
    target_sample_number: Optional[str] = None,
) -> None:
    inspection_number = inspection_number.strip()
    normalized_inputs = dict(input_data)
    normalized_inputs["inspection_number"] = inspection_number
    normalized_target = (target_sample_number or "").strip() or None
    supplied_target = normalized_inputs.get("target_sample_number")
    if isinstance(supplied_target, str):
        supplied_target = supplied_target.strip() or None
    if supplied_target not in (None, normalized_target):
        raise ExecutionApiError(
            422,
            "target_sample_number_mismatch",
            "input_data 中的目标样品编号与本次运行指定不一致",
        )
    if normalized_target is not None:
        normalized_inputs["target_sample_number"] = normalized_target
    runtime_capabilities = deepcopy(
        capabilities
        if capabilities is not None
        else workflow.capabilities or {}
    )
    _assert_idempotent_run_matches(
        existing,
        workflow_id=workflow.id,
        inspection_number=inspection_number,
        mode=mode,
        definition_checksum_value=definition_checksum(definition),
        capabilities_snapshot=runtime_capabilities,
        contract_checksum_value=workflow_contract_checksum(
            definition,
            runtime_capabilities,
        ),
        input_data=normalized_inputs,
        global_data=global_data,
    )


def create_run(
    db: Session,
    *,
    workflow: ExecutionWorkflow,
    actor: ExecutionUser,
    inspection_number: str,
    input_data: dict[str, Any],
    global_data: dict[str, Any],
    idempotency_key: str,
    mode: str = "live",
    draft_definition: Optional[dict[str, Any]] = None,
    target_sample_number: Optional[str] = None,
) -> tuple[ExecutionRun, bool]:
    if (
        mode == "test"
        and getattr(workflow, "management_mode", "draft_v1")
        == "release_v2"
    ):
        raise ExecutionApiError(
            409,
            "workflow_managed_by_release_v2",
            "该流程由 Workflow Release v2 管理，不能通过 v1 草稿测试接口运行",
            details={"workflow_id": workflow.id},
        )
    if (
        (not workflow.is_enabled or workflow.availability_code is not None)
        and mode != "test"
    ):
        raise ExecutionApiError(
            409,
            workflow.availability_code or "workflow_disabled",
            workflow.availability_message or "该流程当前不可运行",
        )

    version: Optional[ExecutionWorkflowVersion] = None
    if mode == "test":
        definition = runtime_definition(
            draft_definition or workflow.draft_definition
        )
        capabilities = deepcopy(workflow.capabilities or {})
        validation = validate_definition(definition)
    else:
        version = _latest_published_version(db, workflow)
        if version is None:
            raise ExecutionApiError(409, "workflow_not_published", "流程尚未发布")
        definition = version.definition
        capabilities = deepcopy(version.capabilities or {})
        if version.release_id is not None:
            from app.execution.registry import NodeRegistry, NodeType

            v2_registry = NodeRegistry()
            compatibility_node_ids: set[str] = set()
            registered: set[tuple[str, int]] = set()
            instances = {
                str(item.get("node_id")): item
                for item in (version.dependency_lock or {}).get(
                    "node_instances", []
                )
                if isinstance(item, dict) and item.get("node_id")
            }
            for node in definition.get("nodes") or []:
                contract = instances.get(str(node.get("id"))) or {}
                identity = (
                    str(node.get("type") or ""),
                    int(node.get("type_version") or 1),
                )
                if contract.get("source") != "resource":
                    compatibility_node_ids.add(str(node.get("id")))
                if identity in registered:
                    continue
                v2_registry.register(
                    NodeType(
                        identity[0],
                        identity[1],
                        str(node.get("name") or identity[0]),
                        "v2",
                        "immutable v2 runtime projection",
                        execution_kind=str(
                            contract.get("execution_kind") or "automatic"
                        ),
                        input_schema=deepcopy(
                            contract.get("effective_input_schema") or {}
                        ),
                        output_schema=deepcopy(
                            contract.get("effective_output_schema") or {}
                        ),
                        publishable=bool(contract.get("publishable", True)),
                    )
                )
                registered.add(identity)
            validation = validate_definition(
                definition,
                registry=v2_registry,
                for_publish=True,
                compatibility_node_ids=compatibility_node_ids,
            )
        else:
            validation = validate_definition(definition, for_publish=True)
    if not validation.valid:
        raise ExecutionApiError(
            422,
            "workflow_invalid",
            "流程定义校验失败",
            details=validation.as_dict(),
        )

    inspection_number = inspection_number.strip()
    normalized_inputs = dict(input_data)
    supplied_number = normalized_inputs.get("inspection_number")
    if supplied_number not in (None, inspection_number):
        raise ExecutionApiError(
            422,
            "inspection_number_mismatch",
            "input_data 中的检验编号与本次运行编号不一致",
        )
    normalized_inputs["inspection_number"] = inspection_number
    target_sample_number = (target_sample_number or "").strip() or None
    supplied_target = normalized_inputs.get("target_sample_number")
    if isinstance(supplied_target, str):
        supplied_target = supplied_target.strip() or None
    if supplied_target not in (None, target_sample_number):
        raise ExecutionApiError(
            422,
            "target_sample_number_mismatch",
            "input_data 中的目标样品编号与本次运行指定不一致",
        )
    if target_sample_number is not None:
        normalized_inputs["target_sample_number"] = target_sample_number
    normalized_globals = dict(global_data)
    _raise_payload_validation(
        schema=definition.get("input_schema") or {},
        value=normalized_inputs,
        path_prefix="$.input_data",
        code="run_input_invalid",
        message="流程必填或输入字段校验失败",
    )
    _raise_payload_validation(
        schema=definition.get("global_schema") or {},
        value=normalized_globals,
        path_prefix="$.global_data",
        code="run_globals_invalid",
        message="流程全局变量校验失败",
    )
    allowed_root_ids = _declared_root_ids_from_definition(definition)
    _assert_root_refs_allowed(normalized_inputs, allowed_root_ids)
    _assert_root_refs_allowed(normalized_globals, allowed_root_ids)
    checksum = definition_checksum(definition)
    contract_checksum = workflow_contract_checksum(definition, capabilities)
    deployed_contract_checksum = (
        version.deployed_contract_checksum if version is not None else None
    )
    existing = (
        db.query(ExecutionRun)
        .filter(
            ExecutionRun.created_by_id == actor.id,
            ExecutionRun.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )
    if existing is not None:
        _assert_idempotent_run_matches(
            existing,
            workflow_id=workflow.id,
            inspection_number=inspection_number,
            mode=mode,
            definition_checksum_value=checksum,
            capabilities_snapshot=capabilities,
            contract_checksum_value=contract_checksum,
            deployed_contract_checksum_value=deployed_contract_checksum,
            input_data=normalized_inputs,
            global_data=normalized_globals,
        )
        return existing, True
    is_v2_version = bool(version is not None and version.release_id)
    v2_dependency_lock = deepcopy(
        (version.dependency_lock or {}) if is_v2_version else {}
    )
    v2_node_instances: dict[str, dict[str, Any]] = {}
    if is_v2_version:
        raw_instances = v2_dependency_lock.get("node_instances")
        if isinstance(raw_instances, dict):
            v2_node_instances = {
                str(node_id): value
                for node_id, value in raw_instances.items()
                if isinstance(value, dict)
            }
        elif isinstance(raw_instances, list):
            v2_node_instances = {
                str(value.get("node_id")): value
                for value in raw_instances
                if isinstance(value, dict) and value.get("node_id")
            }
        missing_contract_nodes = sorted(
            node["id"]
            for node in definition["nodes"]
            if node["id"] not in v2_node_instances
        )
        if missing_contract_nodes:
            raise ExecutionApiError(
                503,
                "deployed_contract_invalid",
                "已发布的 v2 版本缺少节点执行绑定快照",
                details={"missing_node_ids": missing_contract_nodes},
            )
        invalid_binding_nodes = sorted(
            node_id
            for node_id, contract in v2_node_instances.items()
            if len(str(contract.get("execution_binding_digest") or ""))
            != 64
            or not str(contract.get("execution_kind") or "").strip()
        )
        if invalid_binding_nodes:
            raise ExecutionApiError(
                503,
                "deployed_contract_invalid",
                "已发布的 v2 版本包含无效节点执行绑定",
                details={"invalid_node_ids": invalid_binding_nodes},
            )
        from app.execution.release_v2 import rollout_profile_blockers

        profile_blockers = rollout_profile_blockers(
            list(v2_node_instances.values())
        )
        if profile_blockers:
            raise ExecutionApiError(
                409,
                "rollout_profile_blocked",
                "当前 rollout profile 不允许创建该 v2 Run",
                details={"blockers": profile_blockers},
            )

    run = ExecutionRun(
        workflow_id=workflow.id,
        workflow_version_id=version.id if version else None,
        created_by_id=actor.id,
        idempotency_key=idempotency_key,
        inspection_number=inspection_number,
        mode=mode,
        status="queued",
        definition_snapshot=definition,
        definition_checksum=checksum,
        capabilities_snapshot=capabilities,
        contract_checksum=contract_checksum,
        contract_format=(version.contract_format if version else None),
        release_id=(version.release_id if version else None),
        release_digest=(version.release_digest if version else None),
        dependency_lock=(v2_dependency_lock or None),
        dependency_lock_digest=(
            version.dependency_lock_digest if version else None
        ),
        deployment_binding_snapshot=(
            deepcopy(version.deployment_binding_snapshot)
            if version and version.deployment_binding_snapshot is not None
            else None
        ),
        deployment_binding_digest=(
            version.deployment_binding_digest if version else None
        ),
        asset_lock=(
            deepcopy(version.asset_lock)
            if version and version.asset_lock is not None
            else None
        ),
        engine_version_snapshot=(version.engine_version if version else None),
        deployed_contract_checksum=deployed_contract_checksum,
        input_data=normalized_inputs,
        global_data=normalized_globals,
    )
    db.add(run)
    db.flush()

    for node in definition["nodes"]:
        node_contract = v2_node_instances.get(node["id"]) or {}
        node_run = ExecutionNodeRun(
            run_id=run.id,
            node_id=node["id"],
            node_type=node["type"],
            node_type_version=node.get("type_version", 1),
            node_name=node.get("name") or node["type"],
            execution_kind=node_contract.get("execution_kind"),
            execution_binding_digest=node_contract.get(
                "execution_binding_digest"
            ),
            status=("ready" if node["type"] == "core.start" else "pending"),
        )
        db.add(node_run)
    for index, edge in enumerate(definition.get("edges") or []):
        db.add(
            ExecutionEdgeRun(
                run_id=run.id,
                edge_id=edge.get("id") or f"edge-{index}",
                source_node_id=edge["source"],
                target_node_id=edge["target"],
                condition=edge.get("condition"),
                join_policy=edge.get("join_policy", "all"),
            )
        )
    append_run_event(
        db,
        run_id=run.id,
        event_type="run.created",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "status": run.status,
            "inspection_number": inspection_number,
            "mode": mode,
            "release_digest": (
                version.release_digest if version is not None else None
            ),
            "deployed_contract_checksum": deployed_contract_checksum,
        },
    )
    append_audit_log(
        db,
        action="run.create",
        resource_type="execution_run",
        resource_id=run.id,
        actor_user_id=actor.id,
        details={"mode": mode, "workflow_id": workflow.id},
    )
    db.flush()
    return run, False


def expire_stale_external_operations(
    db: Session,
    *,
    now=None,
    limit: int = 20,
) -> int:
    """Close expired preflight fences without performing a remote side effect.

    Candidate discovery is intentionally lock-free.  Each transition then
    follows the engine's normal run -> node -> operation lock order so it
    cannot deadlock with pause/cancel while releasing the remote business key.
    """

    current_time = now or utcnow()
    candidates = (
        db.query(
            ExecutionExternalOperation.id,
            ExecutionExternalOperation.run_id,
            ExecutionExternalOperation.node_run_id,
        )
        .filter(
            or_(
                and_(
                    ExecutionExternalOperation.status == "prepared",
                    ExecutionExternalOperation.preflight_expires_at
                    <= current_time,
                ),
                and_(
                    ExecutionExternalOperation.status == "approved",
                    ExecutionExternalOperation.approval_expires_at.is_not(None),
                    ExecutionExternalOperation.approval_expires_at
                    <= current_time,
                ),
            )
        )
        .order_by(
            ExecutionExternalOperation.created_at.asc(),
            ExecutionExternalOperation.id.asc(),
        )
        .limit(max(1, int(limit)))
        .all()
    )
    expired_count = 0
    for candidate in candidates:
        run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.id == candidate.run_id)
            .populate_existing()
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if run is None:
            continue
        node_run = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.id == candidate.node_run_id,
                ExecutionNodeRun.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        operation = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.id == candidate.id,
                ExecutionExternalOperation.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if operation is None:
            continue

        if (
            operation.status == "prepared"
            and _deadline_reached(
                operation.preflight_expires_at,
                current_time,
            )
        ):
            error_code = "external_operation_preflight_expired"
            error_message = "外部操作预检单已过期，流程已停止，请重新运行"
        elif (
            operation.status == "approved"
            and _deadline_reached(
                operation.approval_expires_at,
                current_time,
            )
        ):
            error_code = "external_operation_approval_expired"
            error_message = "外部操作批准已过期，流程已停止，请重新运行并再次确认"
        else:
            continue

        operation.status = "expired"
        operation.error_code = error_code
        operation.error_message = error_message
        operation.completed_at = current_time
        operation.fence_token = None
        operation.lease_owner = None
        operation.lease_expires_at = None

        node_failed = (
            node_run is not None and node_run.status == "waiting_external"
        )
        if node_failed:
            node_run.status = "failed"
            node_run.error_code = error_code
            node_run.error_message = error_message
            node_run.finished_at = current_time
            attempts = (
                db.query(ExecutionNodeAttempt)
                .filter(
                    ExecutionNodeAttempt.node_run_id == node_run.id,
                    ExecutionNodeAttempt.status == "waiting_external",
                )
                .with_for_update()
                .all()
            )
            for attempt in attempts:
                attempt.status = "failed"
                attempt.error_code = error_code
                attempt.error_message = error_message
                attempt.finished_at = current_time

            if run.status not in RUN_TERMINAL_STATUSES:
                run.error_code = error_code
                run.error_message = error_message
                active_publish_nodes = _cancel_failure_siblings(
                    db,
                    run=run,
                    failed_node_id=node_run.id,
                )
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type="node.failed",
                    payload={
                        "node_id": node_run.node_id,
                        "code": error_code,
                        "message": error_message,
                    },
                )
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type=(
                        "run.failure_pending"
                        if active_publish_nodes
                        else (
                            "run.failure_deferred"
                            if run.status == "paused"
                            else "run.failed"
                        )
                    ),
                    payload={
                        "code": error_code,
                        "message": error_message,
                        "uninterruptible_node_ids": [
                            node.node_id for node in active_publish_nodes
                        ],
                    },
                )

        append_run_event(
            db,
            run_id=run.id,
            event_type="external_operation.expired",
            payload={
                "operation_id": operation.id,
                "node_id": node_run.node_id if node_run is not None else None,
                "status": operation.status,
                "code": error_code,
                "remote_write_performed": False,
            },
        )
        append_audit_log(
            db,
            action="external_operation.expire",
            resource_type="execution_external_operation",
            resource_id=operation.id,
            details={
                "run_id": run.id,
                "code": error_code,
                "remote_write_performed": False,
            },
        )
        expired_count += 1
    return expired_count


def expire_stale_external_attempts(
    db: Session,
    *,
    now=None,
    limit: int = 20,
) -> int:
    """Fail Bridge attempts whose lease expired and settle their operation.

    Candidate discovery is intentionally lock-free, mirroring
    expire_stale_external_operations.  Each transition then follows the
    normal run -> node -> operation -> attempt lock order.  The stage-aware
    settlement matches the Bridge fail endpoint: before file_copy_started
    the operation becomes claimable again (or finishes a pending
    cancellation), afterwards it requires manual reconciliation.
    """

    current_time = now or utcnow()
    candidates = (
        db.query(
            ExecutionExternalAttempt.id,
            ExecutionExternalAttempt.operation_id,
        )
        .filter(
            ExecutionExternalAttempt.status.in_(["claimed", "in_progress"]),
            ExecutionExternalAttempt.lease_expires_at.is_not(None),
            ExecutionExternalAttempt.lease_expires_at <= current_time,
        )
        .order_by(
            ExecutionExternalAttempt.created_at.asc(),
            ExecutionExternalAttempt.id.asc(),
        )
        .limit(max(1, int(limit)))
        .all()
    )
    expired_count = 0
    for candidate in candidates:
        locator = (
            db.query(
                ExecutionExternalOperation.run_id,
                ExecutionExternalOperation.node_run_id,
            )
            .filter(
                ExecutionExternalOperation.id == candidate.operation_id,
            )
            .one_or_none()
        )
        if locator is None:
            continue
        run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.id == locator.run_id)
            .populate_existing()
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if run is None:
            continue
        node_run = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.id == locator.node_run_id,
                ExecutionNodeRun.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        operation = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.id == candidate.operation_id,
                ExecutionExternalOperation.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        attempt = (
            db.query(ExecutionExternalAttempt)
            .filter(
                ExecutionExternalAttempt.id == candidate.id,
                ExecutionExternalAttempt.operation_id
                == candidate.operation_id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if (
            operation is None
            or attempt is None
            or attempt.status not in {"claimed", "in_progress"}
            or not _deadline_reached(
                attempt.lease_expires_at,
                current_time,
            )
        ):
            continue

        attempt.status = "failed"
        attempt.error_code = "lease_expired"
        attempt.error_message = "Bridge 租约已过期，执行进度中断"
        attempt.finished_at = current_time
        settle_external_attempt_failure(
            db,
            node_run=node_run,
            operation=operation,
            attempt=attempt,
            settling_run_status=run.status,
            stage=attempt.current_stage,
            error_code="lease_expired",
            error_message="Bridge 租约已过期，执行进度中断",
            now=current_time,
        )
        expired_count += 1
    return expired_count


def claim_next_node(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: Optional[int] = None,
) -> Optional[ExecutionNodeRun]:
    now = utcnow()
    if expire_stale_external_operations(db, now=now):
        # Commit maintenance before acquiring an unrelated run lock.  The
        # worker loop will immediately scan for ordinary work again.
        return None
    if expire_stale_external_attempts(db, now=now):
        # Bridge lease maintenance follows the same commit-first pattern.
        return None
    # API 驱动的物理发布没有 Worker 心跳线程，租约至少保留五分钟，
    # 避免较大的工作簿复制期间被过期回收并产生第二个发布者。
    lease_duration = max(
        int(
            lease_seconds
            if lease_seconds is not None
            else getattr(settings, "EXECUTION_WORKER_LEASE_SECONDS", 60)
        ),
        300,
    )
    max_attempts = max(
        1,
        int(getattr(settings, "EXECUTION_NODE_MAX_ATTEMPTS", 5)),
    )
    normal_run_statuses = [
        "queued",
        "running",
        "waiting_human",
        "waiting_external",
    ]
    settling_run_statuses = ["cancel_pending", "failure_pending"]
    contract_mode = str(
        getattr(settings, "EXECUTION_CONTRACT_MODE", "legacy")
    ).strip().lower()
    heartbeat_threshold = now - timedelta(
        seconds=max(
            1,
            int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS),
        )
    )
    exact_capability = (
        db.query(ExecutionWorkerNodeCapability.id)
        .join(
            ExecutionWorkerHeartbeat,
            ExecutionWorkerHeartbeat.worker_id
            == ExecutionWorkerNodeCapability.worker_id,
        )
        .filter(
            ExecutionWorkerNodeCapability.worker_id == worker_id,
            ExecutionWorkerNodeCapability.execution_binding_digest
            == ExecutionNodeRun.execution_binding_digest,
            ExecutionWorkerNodeCapability.ready.is_(True),
            ExecutionWorkerHeartbeat.status == "running",
            ExecutionWorkerHeartbeat.last_seen_at >= heartbeat_threshold,
            ExecutionWorkerHeartbeat.protocol_version.like("2.%"),
        )
        .exists()
    )
    worker_can_claim = or_(
        ExecutionNodeRun.execution_binding_digest.is_(None),
        exact_capability,
    )
    exhausted = (
        db.query(ExecutionNodeRun.id)
        .join(ExecutionRun, ExecutionRun.id == ExecutionNodeRun.run_id)
        .filter(
            or_(
                ExecutionRun.status.in_(normal_run_statuses),
                and_(
                    ExecutionRun.status.in_(settling_run_statuses),
                    ExecutionNodeRun.node_type == "artifact.publish",
                ),
            ),
            ExecutionNodeRun.status == "running",
            ExecutionNodeRun.lease_expires_at < now,
            ExecutionNodeRun.attempt_count >= max_attempts,
        )
        .order_by(ExecutionNodeRun.lease_expires_at.asc())
        .first()
    )
    if exhausted is not None:
        exhausted_run_id = (
            db.query(ExecutionNodeRun.run_id)
            .filter(ExecutionNodeRun.id == exhausted.id)
            .scalar()
        )
        if exhausted_run_id is None:
            return None
        exhausted_run = (
            db.query(ExecutionRun)
            .filter(
                ExecutionRun.id == exhausted_run_id,
                or_(
                    ExecutionRun.status.in_(normal_run_statuses),
                    ExecutionRun.status.in_(settling_run_statuses),
                ),
            )
            .populate_existing()
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if exhausted_run is None:
            return None
        exhausted_node = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.id == exhausted.id,
                ExecutionNodeRun.run_id == exhausted_run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if exhausted_node is None:
            return None

        # The initial scan is intentionally non-locking. A heartbeat can renew
        # the lease between that scan and this row lock, so all exhaustion
        # predicates must be checked again while the run/node locks are held.
        locked_now = utcnow()
        locked_lease_expires_at = exhausted_node.lease_expires_at
        comparable_now = (
            locked_now.replace(tzinfo=None)
            if (
                locked_lease_expires_at is not None
                and locked_lease_expires_at.tzinfo is None
            )
            else locked_now
        )
        if (
            exhausted_node.status != "running"
            or not exhausted_node.lease_token
            or locked_lease_expires_at is None
            or locked_lease_expires_at >= comparable_now
            or exhausted_node.attempt_count < max_attempts
            or (
                exhausted_run.status in settling_run_statuses
                and exhausted_node.node_type != "artifact.publish"
            )
        ):
            return None

        exhausted_node_type = exhausted_node.node_type
        exhausted_node_id = exhausted_node.node_id
        fail_node(
            db,
            node_run_id=exhausted_node.id,
            lease_token=exhausted_node.lease_token,
            error_code="node_retry_exhausted",
            error_message=(
                f"节点自动恢复已达到 {max_attempts} 次上限，"
                "请检查错误后由人工决定是否重试"
            ),
        )
        if exhausted_node_type == "artifact.publish":
            output_data = dict(exhausted_run.output_data or {})
            output_data["reconciliation_required"] = {
                "node_id": exhausted_node_id,
                "reason": "publish_retry_exhausted",
            }
            exhausted_run.output_data = output_data
            append_run_event(
                db,
                run_id=exhausted_run.id,
                event_type="publish.reconciliation_required",
                payload=output_data["reconciliation_required"],
            )
        # Commit the exhaustion decision before scanning for unrelated work.
        # This also avoids holding one run lock while trying to lock another.
        return None

    candidate_query = (
        db.query(ExecutionNodeRun.id, ExecutionNodeRun.run_id)
        .join(ExecutionRun, ExecutionRun.id == ExecutionNodeRun.run_id)
        .filter(
            or_(
                ExecutionRun.status.in_(normal_run_statuses),
                and_(
                    ExecutionRun.status.in_(settling_run_statuses),
                    ExecutionNodeRun.node_type == "artifact.publish",
                    ExecutionNodeRun.status == "running",
                ),
            ),
            or_(
                ExecutionNodeRun.status == "ready",
                and_(
                    ExecutionNodeRun.status == "running",
                    ExecutionNodeRun.lease_expires_at < now,
                    ExecutionNodeRun.attempt_count < max_attempts,
                ),
            ),
        )
        .order_by(
            ExecutionNodeRun.ready_at.asc(),
            ExecutionNodeRun.created_at.asc(),
        )
    )
    if contract_mode == "enforced":
        candidate_query = candidate_query.filter(worker_can_claim)
    candidate = candidate_query.first()
    if candidate is None:
        return None

    if contract_mode == "shadow":
        candidate_binding = (
            db.query(ExecutionNodeRun.execution_binding_digest)
            .filter(ExecutionNodeRun.id == candidate.id)
            .scalar()
        )
        if candidate_binding:
            shadow_match = (
                db.query(ExecutionWorkerNodeCapability.id)
                .join(
                    ExecutionWorkerHeartbeat,
                    ExecutionWorkerHeartbeat.worker_id
                    == ExecutionWorkerNodeCapability.worker_id,
                )
                .filter(
                    ExecutionWorkerNodeCapability.worker_id == worker_id,
                    ExecutionWorkerNodeCapability.execution_binding_digest
                    == candidate_binding,
                    ExecutionWorkerNodeCapability.ready.is_(True),
                    ExecutionWorkerHeartbeat.status == "running",
                    ExecutionWorkerHeartbeat.last_seen_at
                    >= heartbeat_threshold,
                    ExecutionWorkerHeartbeat.protocol_version.like("2.%"),
                )
                .first()
            )
            if shadow_match is None:
                logger.warning(
                    "execution_v2_claim_shadow_mismatch "
                    "worker_id=%s node_run_id=%s binding=%s",
                    worker_id,
                    candidate.id,
                    candidate_binding,
                )
                append_audit_log(
                    db,
                    action="execution_v2.claim.shadow_mismatch",
                    resource_type="execution_node_run",
                    resource_id=candidate.id,
                    details={
                        "worker_id": worker_id,
                        "execution_binding_digest": candidate_binding,
                    },
                )

    # Every transition within a run takes the run lock before a node/task lock.
    # This serializes parallel branch completion with pause/cancel and avoids the
    # inverse lock ordering that otherwise deadlocks with cancellation.
    run = (
        db.query(ExecutionRun)
        .filter(
            ExecutionRun.id == candidate.run_id,
            or_(
                ExecutionRun.status.in_(normal_run_statuses),
                ExecutionRun.status.in_(settling_run_statuses),
            ),
        )
        .populate_existing()
        .with_for_update(skip_locked=True)
        .one_or_none()
    )
    if run is None:
        return None
    claimable = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == candidate.id,
            ExecutionNodeRun.run_id == run.id,
            or_(
                ExecutionNodeRun.status == "ready",
                and_(
                    ExecutionNodeRun.status == "running",
                    ExecutionNodeRun.lease_expires_at < now,
                    ExecutionNodeRun.attempt_count < max_attempts,
                ),
            ),
        )
        .populate_existing()
        .with_for_update(skip_locked=True)
        .one_or_none()
    )
    if claimable is None:
        return None
    if contract_mode == "enforced" and not (
        claimable.execution_binding_digest is None
        or db.query(ExecutionWorkerNodeCapability.id)
        .join(
            ExecutionWorkerHeartbeat,
            ExecutionWorkerHeartbeat.worker_id
            == ExecutionWorkerNodeCapability.worker_id,
        )
        .filter(
            ExecutionWorkerNodeCapability.worker_id == worker_id,
            ExecutionWorkerNodeCapability.execution_binding_digest
            == claimable.execution_binding_digest,
            ExecutionWorkerNodeCapability.ready.is_(True),
            ExecutionWorkerHeartbeat.status == "running",
            ExecutionWorkerHeartbeat.last_seen_at >= heartbeat_threshold,
            ExecutionWorkerHeartbeat.protocol_version.like("2.%"),
        )
        .first()
        is not None
    ):
        # The capability set or heartbeat can change between the non-locking
        # scan and the row lock.  Leave the node ready for a compatible Worker.
        return None
    if (
        run.status in settling_run_statuses
        and (
            claimable.node_type != "artifact.publish"
            or claimable.status != "running"
        )
    ):
        return None

    if claimable.status == "running":
        previous_attempt = (
            db.query(ExecutionNodeAttempt)
            .filter(
                ExecutionNodeAttempt.node_run_id == claimable.id,
                ExecutionNodeAttempt.status == "running",
            )
            .order_by(ExecutionNodeAttempt.attempt_number.desc())
            .first()
        )
        if previous_attempt is not None:
            previous_attempt.status = "abandoned"
            previous_attempt.error_code = "worker_lease_expired"
            previous_attempt.error_message = "Worker 租约过期，节点已重新领取"
            previous_attempt.finished_at = now

    lease_token = str(uuid4())
    claimable.status = "running"
    claimable.lease_owner = worker_id
    claimable.lease_token = lease_token
    claimable.lease_expires_at = now + timedelta(seconds=lease_duration)
    claimable.started_at = claimable.started_at or now
    claimable.attempt_count += 1
    if run.status == "queued":
        run.status = "running"
    run.started_at = run.started_at or now
    attempt = ExecutionNodeAttempt(
        node_run_id=claimable.id,
        attempt_number=claimable.attempt_count,
        worker_id=worker_id,
        lease_token=lease_token,
        execution_kind=claimable.execution_kind,
        execution_binding_digest=claimable.execution_binding_digest,
        status="running",
    )
    db.add(attempt)
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.started",
        payload={
            "node_id": claimable.node_id,
            "attempt": claimable.attempt_count,
            "worker_id": worker_id,
        },
    )
    db.flush()
    return claimable


def claim_publish_node_for_api(
    db: Session,
    *,
    run_id: str,
    node_run_id: str,
    mutation_id: str,
    actor: ExecutionUser,
    lease_seconds: Optional[int] = None,
) -> tuple[ExecutionRun, ExecutionNodeRun, Optional[str], bool]:
    """Atomically claim a reached publish node for an authenticated API call.

    The committed ``running`` node is the cancellation/failure fence used by
    the worker path as well.  A succeeded node may only replay an already
    recorded result; it can never start a second physical publish.
    """

    run = _lock_run(db, run_id)
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
    if node_run is None:
        raise not_found("发布节点运行", node_run_id)
    mutation = (
        db.query(ExecutionFileMutation)
        .filter(
            ExecutionFileMutation.run_id == run.id,
            ExecutionFileMutation.mutation_id == mutation_id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if mutation is None:
        raise conflict(
            "mutation_not_prepared",
            "文件变更尚未完成预检",
        )

    replay_status = (
        mutation.status == "published"
        or (
            run.mode == "test"
            and mutation.status == "verified"
            and mutation.node_run_id == node_run.id
        )
    )
    if node_run.status == "succeeded" and replay_status:
        return run, node_run, None, True
    if node_run.status == "running":
        raise conflict(
            "mutation_publish_in_progress",
            "发布节点已由 Worker 或另一个请求领取，请稍后刷新",
            node_id=node_run.node_id,
        )
    if node_run.status != "ready":
        raise conflict(
            "mutation_node_not_reached",
            "发布节点尚未进入实际执行路径",
            node_id=node_run.node_id,
            node_status=node_run.status,
            stage="publish",
        )
    if run.status not in {
        "queued",
        "running",
        "waiting_human",
        "waiting_external",
    }:
        raise conflict(
            "mutation_run_not_active",
            "当前流程运行状态不允许开始发布",
            run_status=run.status,
        )
    if mutation.status != "verified":
        raise conflict(
            "mutation_not_verified",
            "发布前必须完成保存后重读核对",
            mutation_status=mutation.status,
        )

    now = utcnow()
    lease_duration = int(
        lease_seconds
        if lease_seconds is not None
        else getattr(settings, "EXECUTION_WORKER_LEASE_SECONDS", 60)
    )
    lease_token = str(uuid4())
    node_run.status = "running"
    node_run.lease_owner = f"api:{actor.id}"
    node_run.lease_token = lease_token
    node_run.lease_expires_at = now + timedelta(seconds=lease_duration)
    node_run.started_at = node_run.started_at or now
    node_run.attempt_count += 1
    run.started_at = run.started_at or now
    if run.status == "queued":
        run.status = "running"
    db.add(
        ExecutionNodeAttempt(
            node_run_id=node_run.id,
            attempt_number=node_run.attempt_count,
            worker_id=node_run.lease_owner,
            lease_token=lease_token,
            execution_kind=node_run.execution_kind,
            execution_binding_digest=node_run.execution_binding_digest,
            status="running",
        )
    )
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.started",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "node_id": node_run.node_id,
            "attempt": node_run.attempt_count,
            "worker_id": node_run.lease_owner,
            "trigger": "api",
        },
    )
    db.flush()
    return run, node_run, lease_token, False


def renew_node_lease(
    db: Session,
    *,
    node_run_id: str,
    lease_token: str,
    lease_seconds: Optional[int] = None,
) -> bool:
    duration = int(
        lease_seconds
        if lease_seconds is not None
        else getattr(settings, "EXECUTION_WORKER_LEASE_SECONDS", 60)
    )
    updated = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == node_run_id,
            ExecutionNodeRun.status == "running",
            ExecutionNodeRun.lease_token == lease_token,
        )
        .update(
            {"lease_expires_at": utcnow() + timedelta(seconds=duration)},
            synchronize_session=False,
        )
    )
    return updated == 1


def _evaluate_condition(condition: Any, context: dict[str, Any]) -> bool:
    if condition in (None, "", "default"):
        return False
    if isinstance(condition, bool):
        return condition
    if not isinstance(condition, dict):
        return False
    left = _lookup_path(context, condition.get("path", "$"))
    operator = condition.get("operator", "truthy")
    right = condition.get("value")
    try:
        if operator == "truthy":
            return bool(left)
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if operator == "gt":
            return left > right
        if operator == "gte":
            return left >= right
        if operator == "lt":
            return left < right
        if operator == "lte":
            return left <= right
        if operator == "in":
            return left in right
        if operator == "not_in":
            return left not in right
        if operator == "contains":
            return right in left
    except (TypeError, ValueError):
        return False
    return False


def _resolve_outgoing_edges(
    db: Session,
    node_run: ExecutionNodeRun,
    *,
    skipped: bool = False,
    selected_edge_ids: set[str] | None = None,
) -> None:
    edges = (
        db.query(ExecutionEdgeRun)
        .filter(
            ExecutionEdgeRun.run_id == node_run.run_id,
            ExecutionEdgeRun.source_node_id == node_run.node_id,
            ExecutionEdgeRun.status == "pending",
        )
        .populate_existing()
        .all()
    )
    now = utcnow()
    if skipped:
        for edge in edges:
            edge.status = "skipped"
            edge.resolved_at = now
        return

    context = _run_context(db, node_run.run)
    if selected_edge_ids is not None:
        unknown = selected_edge_ids - {edge.edge_id for edge in edges}
        if unknown:
            raise ExecutionApiError(
                500,
                "native_edge_selection_invalid",
                "受信 handler 返回了不存在的出边",
                details={"edge_ids": sorted(unknown)},
            )
        selected_ids = {
            edge.id for edge in edges if edge.edge_id in selected_edge_ids
        }
    elif node_run.node_type == "branch.condition":
        matching = [
            edge
            for edge in edges
            if edge.condition not in (None, "", "default")
            and _evaluate_condition(edge.condition, context)
        ]
        if not matching:
            defaults = [
                edge for edge in edges if edge.condition in (None, "", "default")
            ]
            matching = defaults[:1]
        selected_ids = {edge.id for edge in matching}
    else:
        selected_ids = {edge.id for edge in edges}
    for edge in edges:
        edge.status = "selected" if edge.id in selected_ids else "skipped"
        edge.resolved_at = now
    # The application Session intentionally disables autoflush. Persist edge
    # decisions before the activation query reads them back.
    db.flush()


def _activate_resolved_nodes(db: Session, run: ExecutionRun) -> None:
    """Turn resolved edge sets into ready/skipped nodes, cascading skips."""

    if run.status in RUN_TERMINAL_STATUSES:
        return
    db.flush()
    while True:
        changed = False
        pending_nodes = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.status == "pending",
            )
            .populate_existing()
            .all()
        )
        for node in pending_nodes:
            incoming = (
                db.query(ExecutionEdgeRun)
                .filter(
                    ExecutionEdgeRun.run_id == run.id,
                    ExecutionEdgeRun.target_node_id == node.node_id,
                )
                .populate_existing()
                .all()
            )
            if not incoming:
                continue
            policies = {edge.join_policy or "all" for edge in incoming}
            # Published definitions reject this state. Treat legacy/malformed
            # snapshots conservatively as "all" instead of waking early.
            join_policy = next(iter(policies)) if len(policies) == 1 else "all"
            all_resolved = all(edge.status != "pending" for edge in incoming)
            selected = [edge for edge in incoming if edge.status == "selected"]
            should_activate = bool(selected) and (
                join_policy == "any" or all_resolved
            )
            should_skip = all_resolved and not selected
            if not should_activate and not should_skip:
                continue
            if should_activate:
                node.status = "ready"
                node.ready_at = utcnow()
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type="node.ready",
                    payload={"node_id": node.node_id},
                )
            else:
                node.status = "skipped"
                node.finished_at = utcnow()
                _resolve_outgoing_edges(db, node, skipped=True)
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type="node.skipped",
                    payload={"node_id": node.node_id},
                )
            changed = True
        if not changed:
            break
        db.flush()


def _refresh_run_status(db: Session, run: ExecutionRun) -> None:
    if run.status in {"cancelled", "cancel_pending", "failure_pending", "paused"}:
        return
    statuses = [
        value
        for (value,) in db.query(ExecutionNodeRun.status)
        .filter(ExecutionNodeRun.run_id == run.id)
        .all()
    ]
    now = utcnow()
    if "failed" in statuses:
        run.status = "failed"
        run.finished_at = now
    elif "waiting_human" in statuses:
        run.status = "waiting_human"
    elif "waiting_external" in statuses:
        run.status = "waiting_external"
    elif any(status in {"ready", "running", "pending"} for status in statuses):
        run.status = "running" if run.started_at else "queued"
    elif statuses and all(status in {"succeeded", "skipped"} for status in statuses):
        reached_end = (
            db.query(ExecutionNodeRun.id)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.node_type == "core.end",
                ExecutionNodeRun.status == "succeeded",
            )
            .first()
            is not None
        )
        if reached_end:
            run.status = "completed"
        else:
            run.status = "failed"
            run.error_code = "workflow_end_not_reached"
            run.error_message = "流程所有分支均已结束，但没有到达结束节点"
        run.finished_at = now


def _cancel_prepared_external_operations(
    db: Session,
    *,
    run_id: str,
    node_run_ids: list[str],
    actor_user_id: Optional[str],
    reason: str,
) -> None:
    if not node_run_ids:
        return
    operations = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.run_id == run_id,
            ExecutionExternalOperation.node_run_id.in_(node_run_ids),
            ExecutionExternalOperation.status.in_(["prepared", "approved"]),
        )
        .order_by(ExecutionExternalOperation.id.asc())
        .with_for_update()
        .all()
    )
    now = utcnow()
    for operation in operations:
        operation.status = "cancelled"
        operation.error_code = reason
        operation.error_message = (
            "流程已取消，预检单不再允许执行"
            if reason == "run_cancelled"
            else "同一流程已有节点失败，预检单不再允许执行"
        )
        operation.completed_at = now
        append_run_event(
            db,
            run_id=run_id,
            event_type="external_operation.cancelled",
            actor_type="user" if actor_user_id else "system",
            actor_id=actor_user_id,
            payload={
                "operation_id": operation.id,
                "status": operation.status,
                "reason": reason,
                "remote_write_performed": False,
            },
        )


def _cancel_failure_siblings(
    db: Session,
    *,
    run: ExecutionRun,
    failed_node_id: str,
    actor_user_id: Optional[str] = None,
) -> list[ExecutionNodeRun]:
    """Stop reversible siblings while preserving every uncertain side effect.

    A claimed publish or external operation is an uninterruptible critical
    section. The run cannot become terminal until each one records a result;
    post-boundary external failures additionally require admin attestation.
    """

    now = utcnow()
    was_paused = run.status == "paused"
    tasks = (
        db.query(ExecutionHumanTask)
        .filter(
            ExecutionHumanTask.run_id == run.id,
            ExecutionHumanTask.status.in_(["open", "claimed"]),
        )
        .order_by(ExecutionHumanTask.id.asc())
        .with_for_update()
        .all()
    )
    for task in tasks:
        task.status = "cancelled"
        task.revision += 1
        task.completed_by_id = actor_user_id
        task.completed_at = now
        append_run_event(
            db,
            run_id=run.id,
            event_type="human_task.cancelled",
            actor_type="user" if actor_user_id else "system",
            actor_id=actor_user_id,
            payload={
                "task_id": task.id,
                "reason": "run_failed",
                "failed_node_id": failed_node_id,
            },
        )

    nodes = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.id != failed_node_id,
            ExecutionNodeRun.status.notin_(NODE_TERMINAL_STATUSES),
        )
        .order_by(ExecutionNodeRun.id.asc())
        .with_for_update()
        .all()
    )
    active_publish_nodes = [
        node
        for node in nodes
        if node.status == "running" and node.node_type == "artifact.publish"
    ]
    active_publish_ids = {node.id for node in active_publish_nodes}
    unsettled_external_operations = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.run_id == run.id,
            ExecutionExternalOperation.status.in_(
                UNSETTLED_EXTERNAL_OPERATION_STATUSES
            ),
        )
        .order_by(ExecutionExternalOperation.id.asc())
        .with_for_update()
        .all()
    )
    unsettled_external_node_ids = {
        operation.node_run_id
        for operation in unsettled_external_operations
    }
    for operation in unsettled_external_operations:
        if operation.status != "in_progress":
            continue
        # The sibling failure is a cancellation request for the Bridge. A
        # pre-boundary attempt must stop safely; a post-boundary result remains
        # fenced and transitions to reconciliation_required on failure.
        operation.status = "cancel_pending"
        append_run_event(
            db,
            run_id=run.id,
            event_type="external_operation.cancel_pending",
            actor_type="user" if actor_user_id else "system",
            actor_id=actor_user_id,
            payload={
                "operation_id": operation.id,
                "status": operation.status,
                "reason": "run_failed",
                "remote_write_performed": None,
            },
        )
    uninterruptible_ids = active_publish_ids | unsettled_external_node_ids
    uninterruptible_nodes = [
        node for node in nodes if node.id in uninterruptible_ids
    ]
    cancelled_node_ids = [
        node.id for node in nodes if node.id not in uninterruptible_ids
    ]
    if cancelled_node_ids:
        attempts = (
            db.query(ExecutionNodeAttempt)
            .filter(
                ExecutionNodeAttempt.node_run_id.in_(cancelled_node_ids),
                ExecutionNodeAttempt.status.in_(
                    ["running", "waiting_human", "waiting_external"]
                ),
            )
            .with_for_update()
            .all()
        )
        for attempt in attempts:
            attempt.status = "cancelled"
            attempt.error_code = "run_failed"
            attempt.error_message = "同一流程的其它节点已失败"
            attempt.finished_at = now
        _cancel_prepared_external_operations(
            db,
            run_id=run.id,
            node_run_ids=cancelled_node_ids,
            actor_user_id=actor_user_id,
            reason="run_failed",
        )

    for node in nodes:
        if node.id in uninterruptible_ids:
            continue
        node.status = "cancelled"
        node.finished_at = now
        node.lease_owner = None
        node.lease_token = None
        node.lease_expires_at = None

    if uninterruptible_nodes:
        run.status = "failure_pending"
        run.finished_at = None
    elif was_paused:
        run.status = "paused"
        run.finished_at = None
    else:
        run.status = "failed"
        run.finished_at = now
    return uninterruptible_nodes


def _finish_settling_publish(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    succeeded: bool,
    output_data: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Record publish reconciliation and terminalize a settling run.

    Returns True when the caller must stop normal DAG propagation.
    """

    pending_status = run.status
    if pending_status not in {"cancel_pending", "failure_pending"}:
        return False

    data = dict(run.output_data or {})
    receipts = list(data.get("publish_receipts") or [])
    errors = list(data.get("publish_errors") or [])
    if succeeded:
        receipts.append(
            {
                "node_id": node_run.node_id,
                "receipt": output_data or {},
            }
        )
        # Keep the singular key for API compatibility while first-version
        # validation guarantees at most one publish node.
        data["publish_receipt"] = output_data or {}
    else:
        errors.append(
            {
                "node_id": node_run.node_id,
                "code": error_code,
                "message": error_message,
            }
        )
    data["publish_receipts"] = receipts
    data["publish_errors"] = errors
    remaining_publish = (
        db.query(ExecutionNodeRun.id)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_type == "artifact.publish",
            ExecutionNodeRun.status == "running",
            ExecutionNodeRun.id != node_run.id,
        )
        .first()
    )
    if remaining_publish is not None:
        run.output_data = data
        return True

    remaining_external = (
        db.query(ExecutionExternalOperation.id)
        .filter(
            ExecutionExternalOperation.run_id == run.id,
            ExecutionExternalOperation.status.in_(
                UNSETTLED_EXTERNAL_OPERATION_STATUSES
            ),
        )
        .first()
    )
    if remaining_external is not None:
        # 已被 Bridge 领取的外部操作同样不可中断：运行保持待定状态，
        # 直到该尝试回报终态后按阶段收尾。
        run.output_data = data
        return True

    now = utcnow()
    any_side_effect = bool(receipts) or bool(
        data.get("external_side_effect_receipts")
    )
    if pending_status == "cancel_pending":
        run.status = "cancelled"
        data["cancelled_after_side_effect"] = any_side_effect
        event_type = "run.cancelled"
    else:
        run.status = "failed"
        data["failed_after_publish_started"] = True
        data["failed_after_external_side_effect"] = bool(
            data.get("external_side_effect_receipts")
        )
        data["failed_after_side_effect"] = any_side_effect
        event_type = "run.failed"
    run.output_data = data
    run.finished_at = now
    append_run_event(
        db,
        run_id=run.id,
        event_type=event_type,
        payload={
            "status": run.status,
            "side_effect_completed": any_side_effect,
            "publish_receipt_count": len(receipts),
            "publish_error_count": len(errors),
            "code": run.error_code,
            "message": run.error_message,
        },
    )
    return True


def _finish_settling_external(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    side_effect_completed: bool,
    output_data: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Record an external result and settle cancel/failure once all writes stop."""

    pending_status = run.status
    if pending_status not in {"cancel_pending", "failure_pending"}:
        return False

    data = dict(run.output_data or {})
    receipts = list(data.get("external_side_effect_receipts") or [])
    resolutions = list(data.get("external_side_effect_resolutions") or [])
    if side_effect_completed:
        receipts.append(
            {
                "node_id": node_run.node_id,
                "receipt": output_data or {},
            }
        )
    else:
        resolutions.append(
            {
                "node_id": node_run.node_id,
                "code": error_code,
                "message": error_message,
                "remote_write_performed": False,
            }
        )
    data["external_side_effect_receipts"] = receipts
    data["external_side_effect_resolutions"] = resolutions

    # Tests may disable autoflush. Make the terminal operation/node visible to
    # the remaining-uninterruptible checks before deciding the run is settled.
    db.flush()
    remaining_publish = (
        db.query(ExecutionNodeRun.id)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_type == "artifact.publish",
            ExecutionNodeRun.status == "running",
        )
        .first()
    )
    remaining_external = (
        db.query(ExecutionExternalOperation.id)
        .filter(
            ExecutionExternalOperation.run_id == run.id,
            ExecutionExternalOperation.status.in_(
                UNSETTLED_EXTERNAL_OPERATION_STATUSES
            ),
        )
        .first()
    )
    if remaining_publish is not None or remaining_external is not None:
        run.output_data = data
        return True

    now = utcnow()
    any_side_effect = bool(data.get("publish_receipts")) or bool(receipts)
    if pending_status == "cancel_pending":
        run.status = "cancelled"
        data["cancelled_after_side_effect"] = any_side_effect
        event_type = "run.cancelled"
    else:
        run.status = "failed"
        data["failed_after_publish_started"] = bool(
            data.get("publish_receipts") or data.get("publish_errors")
        )
        data["failed_after_external_side_effect"] = bool(receipts)
        data["failed_after_side_effect"] = any_side_effect
        event_type = "run.failed"
    run.output_data = data
    run.finished_at = now
    append_run_event(
        db,
        run_id=run.id,
        event_type=event_type,
        payload={
            "status": run.status,
            "side_effect_completed": any_side_effect,
            "external_receipt_count": len(receipts),
            "external_resolution_count": len(resolutions),
            "code": run.error_code,
            "message": run.error_message,
        },
    )
    return True


def _finish_attempt(
    db: Session,
    node_run: ExecutionNodeRun,
    *,
    lease_token: str,
    status: str,
    output_data: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    attempt = (
        db.query(ExecutionNodeAttempt)
        .filter(
            ExecutionNodeAttempt.node_run_id == node_run.id,
            ExecutionNodeAttempt.lease_token == lease_token,
        )
        .one_or_none()
    )
    if attempt is None:
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，当前结果不会被接受",
            node_id=node_run.node_id,
        )
    attempt.status = status
    attempt.output_data = output_data or {}
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.finished_at = utcnow()


def _prepare_external_operation_wait(
    db: Session,
    context: NodeExecutionContext,
) -> ExecutionExternalOperation:
    """Fence an external request and release the ordinary Worker lease.

    This transition intentionally stops before any connector call.  The
    operation remains durable across Worker/server restarts; when the deployed
    connector capability is enabled, the server approves it immediately so
    progress never depends on a browser-side countdown.
    """

    run, node_run = _lock_run_and_node(db, context.node_run.id)
    _ensure_run_accepts_result(run)
    if (
        node_run.status != "running"
        or node_run.lease_token != context.lease_token
    ):
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，不能生成外部操作预检单",
            node_id=node_run.node_id,
        )
    context.run = run
    context.node_run = node_run
    preparers = {
        LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE: (
            prepare_legacy_generic_check_record_entry_operation
        ),
        LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE: (
            prepare_legacy_microscopy_check_record_entry_operation
        ),
        LEGACY_REGENERATED_COUNT_NODE: (
            prepare_legacy_regenerated_count_operation
        ),
        LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE: (
            prepare_legacy_special_wool_image_operation
        ),
        LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE: (
            prepare_legacy_special_wool_qualitative_upload_operation
        ),
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE: (
            prepare_legacy_special_wool_qualitative_review_operation
        ),
        LEGACY_SPECIAL_WOOL_REVIEW_NODE: (
            prepare_legacy_special_wool_review_operation
        ),
    }
    try:
        preparer = preparers[node_run.node_type]
    except KeyError as exc:
        raise ValueError("unsupported_external_node") from exc
    operation, reused = preparer(
        db,
        run=run,
        node_run=node_run,
        node=context.node,
        input_data=context.input_data,
    )
    execution_available = (
        (operation.request_summary or {})
        .get("execution_capability", {"available": True})
        .get("available")
        is not False
    )
    if (
        operation.status == "prepared"
        and execution_available
        and settings.EXECUTION_EXTERNAL_AUTO_APPROVE_ENABLED
    ):
        actor = db.get(ExecutionUser, run.created_by_id)
        if actor is None:
            raise conflict(
                "external_operation_creator_missing",
                "流程发起人已不存在，无法自动交付旧系统操作",
            )
        operation, _duplicate_approval = approve_prepared_external_operation(
            db,
            operation=operation,
            run=run,
            actor=actor,
            payload_checksum=operation.payload_checksum,
            confirmed_sample_number=str(
                (operation.request_summary or {}).get("target_sample_number")
                or ""
            ),
            note="流程预检通过后由服务端自动批准",
            automatic=True,
        )
    if operation.status not in {"prepared", "approved"}:
        raise conflict(
            "external_operation_not_waitable",
            "外部操作当前不处于可等待状态，请刷新后重新处理",
            operation_id=operation.id,
            status=operation.status,
        )
    output = {
        "operation_id": operation.id,
        "operation_key": operation.operation_key,
        "payload_checksum": operation.payload_checksum,
        "status": operation.status,
        "target_sample_number": (
            (operation.request_summary or {}).get("target_sample_number")
        ),
        "execution_capability": dict(
            (operation.request_summary or {}).get(
                "execution_capability"
            )
            or {"available": True}
        ),
        "requires_final_approval": False,
        "remote_write_performed": False,
    }
    node_run.status = "waiting_external"
    node_run.input_data = context.input_data
    node_run.output_data = output
    node_run.lease_owner = None
    node_run.lease_token = None
    node_run.lease_expires_at = None
    _finish_attempt(
        db,
        node_run,
        lease_token=context.lease_token,
        status="waiting_external",
        output_data=output,
    )
    if run.status != "paused":
        run.status = "waiting_external"
    append_run_event(
        db,
        run_id=run.id,
        event_type=(
            "external_operation.reused"
            if reused
            else "external_operation.prepared"
        ),
        payload={
            **output,
            "node_id": node_run.node_id,
        },
    )
    return operation


def _settle_cancel_pending_run(
    db: Session,
    *,
    run: ExecutionRun,
    side_effect_completed: bool = False,
) -> None:
    """Terminalize a cancelling run once nothing uninterruptible remains."""

    if run.status != "cancel_pending":
        return
    # autoflush 可能关闭（测试会话），先把操作/节点的终态落库再统计剩余
    # 不可中断节点，避免过期读取让运行永远停在 cancel_pending。
    db.flush()
    remaining_publish = (
        db.query(ExecutionNodeRun.id)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.node_type == "artifact.publish",
            ExecutionNodeRun.status == "running",
        )
        .first()
    )
    if remaining_publish is not None:
        return
    remaining_external = (
        db.query(ExecutionExternalOperation.id)
        .filter(
            ExecutionExternalOperation.run_id == run.id,
            ExecutionExternalOperation.status.in_(
                UNSETTLED_EXTERNAL_OPERATION_STATUSES
            ),
        )
        .first()
    )
    if remaining_external is not None:
        return
    now = utcnow()
    if side_effect_completed:
        data = dict(run.output_data or {})
        data["cancelled_after_side_effect"] = True
        run.output_data = data
    run.status = "cancelled"
    run.finished_at = now
    append_run_event(
        db,
        run_id=run.id,
        event_type="run.cancelled",
        payload={
            "status": run.status,
            "side_effect_completed": side_effect_completed,
        },
    )


def complete_external_node(
    db: Session,
    *,
    node_run_id: str,
    output_data: Optional[dict[str, Any]] = None,
) -> ExecutionNodeRun:
    """Complete a waiting_external node after the Bridge recorded a receipt.

    The remote write already happened by the time this runs, so the receipt
    is always recorded; only the DAG propagation is skipped when the run is
    being torn down by a cancellation.
    """

    run, node_run = _lock_run_and_node(db, node_run_id)
    if node_run.status != "waiting_external":
        raise conflict(
            "external_operation_node_not_waiting",
            "外部操作对应节点已不再等待连接器处理",
            node_id=node_run.node_id,
        )
    now = utcnow()
    node_run.status = "succeeded"
    node_run.output_data = output_data or {}
    node_run.error_code = None
    node_run.error_message = None
    node_run.finished_at = now
    attempts = (
        db.query(ExecutionNodeAttempt)
        .filter(
            ExecutionNodeAttempt.node_run_id == node_run.id,
            ExecutionNodeAttempt.status == "waiting_external",
        )
        .with_for_update()
        .all()
    )
    for attempt in attempts:
        attempt.status = "succeeded"
        attempt.output_data = node_run.output_data
        attempt.finished_at = now
    if run.status in {"cancel_pending", "failure_pending"}:
        append_run_event(
            db,
            run_id=run.id,
            event_type="node.succeeded",
            payload={
                "node_id": node_run.node_id,
                "output": node_run.output_data,
                "completed_while_settling": run.status,
            },
        )
        _finish_settling_external(
            db,
            run=run,
            node_run=node_run,
            side_effect_completed=True,
            output_data=node_run.output_data,
        )
        return node_run
    db.flush()
    _resolve_outgoing_edges(db, node_run)
    _activate_resolved_nodes(db, run)
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.succeeded",
        payload={"node_id": node_run.node_id, "output": node_run.output_data},
    )
    previous_status = run.status
    _refresh_run_status(db, run)
    if run.status != previous_status:
        append_run_event(
            db,
            run_id=run.id,
            event_type=f"run.{run.status}",
            payload={"status": run.status},
        )
    return node_run


def cancel_external_waiting_node(
    db: Session,
    *,
    node_run_id: str,
    error_code: str,
    error_message: str,
) -> ExecutionNodeRun:
    """Cancel a waiting_external node whose attempt ended before any write."""

    run, node_run = _lock_run_and_node(db, node_run_id)
    if node_run.status != "waiting_external":
        raise conflict(
            "external_operation_node_not_waiting",
            "外部操作对应节点已不再等待连接器处理",
            node_id=node_run.node_id,
        )
    now = utcnow()
    node_run.status = "cancelled"
    node_run.error_code = error_code
    node_run.error_message = error_message
    node_run.finished_at = now
    attempts = (
        db.query(ExecutionNodeAttempt)
        .filter(
            ExecutionNodeAttempt.node_run_id == node_run.id,
            ExecutionNodeAttempt.status == "waiting_external",
        )
        .with_for_update()
        .all()
    )
    for attempt in attempts:
        attempt.status = "cancelled"
        attempt.error_code = error_code
        attempt.error_message = error_message
        attempt.finished_at = now
    _finish_settling_external(
        db,
        run=run,
        node_run=node_run,
        side_effect_completed=False,
        error_code=error_code,
        error_message=error_message,
    )
    return node_run


def fail_external_waiting_node(
    db: Session,
    *,
    node_run_id: str,
    error_code: str,
    error_message: str,
    actor_user_id: Optional[str] = None,
) -> ExecutionNodeRun:
    """Fail a waiting external node after proving no remote side effect.

    This is deliberately separate from fail_node: the ordinary Worker lease
    ended when the durable external-operation fence was prepared.
    """

    run, node_run = _lock_run_and_node(db, node_run_id)
    if node_run.status != "waiting_external":
        raise conflict(
            "external_operation_node_not_waiting",
            "外部操作对应节点已不再等待连接器处理",
            node_id=node_run.node_id,
        )
    if run.status in RUN_TERMINAL_STATUSES or run.status == "cancel_pending":
        raise conflict(
            "external_reconciliation_run_not_active",
            "当前流程状态不能按未写入结果失败收尾",
            run_status=run.status,
        )

    now = utcnow()
    node_run.status = "failed"
    node_run.error_code = error_code
    node_run.error_message = error_message
    node_run.finished_at = now
    node_run.lease_owner = None
    node_run.lease_token = None
    node_run.lease_expires_at = None
    attempts = (
        db.query(ExecutionNodeAttempt)
        .filter(
            ExecutionNodeAttempt.node_run_id == node_run.id,
            ExecutionNodeAttempt.status == "waiting_external",
        )
        .with_for_update()
        .all()
    )
    for attempt in attempts:
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_message = error_message
        attempt.finished_at = now

    append_run_event(
        db,
        run_id=run.id,
        event_type="node.failed",
        actor_type="user" if actor_user_id else "system",
        actor_id=actor_user_id,
        payload={
            "node_id": node_run.node_id,
            "code": error_code,
            "message": error_message,
        },
    )
    if run.status == "failure_pending":
        # Preserve the original sibling failure as the run's terminal cause.
        _finish_settling_external(
            db,
            run=run,
            node_run=node_run,
            side_effect_completed=False,
            error_code=error_code,
            error_message=error_message,
        )
    else:
        run.error_code = error_code
        run.error_message = error_message
        uninterruptible_nodes = _cancel_failure_siblings(
            db,
            run=run,
            failed_node_id=node_run.id,
            actor_user_id=actor_user_id,
        )
        append_run_event(
            db,
            run_id=run.id,
            event_type=(
                "run.failure_pending"
                if uninterruptible_nodes
                else (
                    "run.failure_deferred"
                    if run.status == "paused"
                    else "run.failed"
                )
            ),
            actor_type="user" if actor_user_id else "system",
            actor_id=actor_user_id,
            payload={
                "code": error_code,
                "message": error_message,
                "uninterruptible_node_ids": [
                    node.node_id for node in uninterruptible_nodes
                ],
            },
        )
    return node_run


def complete_node(
    db: Session,
    *,
    node_run_id: str,
    lease_token: str,
    output_data: Optional[dict[str, Any]] = None,
    globals_patch: Optional[dict[str, Any]] = None,
    globals_conflict: Optional[str] = None,
    selected_edge_ids: Optional[set[str]] = None,
) -> ExecutionNodeRun:
    run, node_run = _lock_run_and_node(db, node_run_id)
    _ensure_run_accepts_result(run)
    if node_run.status != "running" or node_run.lease_token != lease_token:
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，当前结果不会被接受",
            node_id=node_run.node_id,
        )
    if isinstance(globals_patch, dict) and globals_conflict == "error":
        conflicts = sorted(
            set(globals_patch).intersection((run.global_data or {}).keys())
        )
        if conflicts:
            raise ExecutionApiError(
                409,
                "data_assign_conflict",
                "data.assign 将覆盖已有 globals 字段",
                details={"keys": conflicts},
            )
    now = utcnow()
    node_run.status = "succeeded"
    node_run.output_data = output_data or {}
    node_run.error_code = None
    node_run.error_message = None
    node_run.finished_at = now
    node_run.lease_owner = None
    node_run.lease_expires_at = None
    _finish_attempt(
        db,
        node_run,
        lease_token=lease_token,
        status="succeeded",
        output_data=node_run.output_data,
    )
    values = globals_patch
    if values is None and node_run.node_type == "variables.set":
        values = node_run.output_data.get("globals")
    if isinstance(values, dict):
        # Merge only after acquiring the run lock. Mutating context.run in the
        # executor allowed parallel variable nodes to overwrite each other's
        # updates before either completion obtained the lock.
        run.global_data = {**(run.global_data or {}), **values}
    if node_run.node_type == "core.end":
        run.output_data = node_run.output_data
    if run.status in {"cancel_pending", "failure_pending"}:
        append_run_event(
            db,
            run_id=node_run.run_id,
            event_type="node.succeeded",
            payload={
                "node_id": node_run.node_id,
                "output": node_run.output_data,
                "completed_while_settling": run.status,
            },
        )
        _finish_settling_publish(
            db,
            run=run,
            node_run=node_run,
            succeeded=True,
            output_data=node_run.output_data,
        )
        return node_run
    db.flush()
    _resolve_outgoing_edges(
        db,
        node_run,
        selected_edge_ids=selected_edge_ids,
    )
    _activate_resolved_nodes(db, run)
    append_run_event(
        db,
        run_id=node_run.run_id,
        event_type="node.succeeded",
        payload={"node_id": node_run.node_id, "output": node_run.output_data},
    )
    previous_status = run.status
    _refresh_run_status(db, run)
    if run.status != previous_status:
        append_run_event(
            db,
            run_id=node_run.run_id,
            event_type=f"run.{run.status}",
            payload={"status": run.status},
        )
    return node_run


def fail_node(
    db: Session,
    *,
    node_run_id: str,
    lease_token: str,
    error_code: str,
    error_message: str,
) -> ExecutionNodeRun:
    run, node_run = _lock_run_and_node(db, node_run_id)
    _ensure_run_accepts_result(run)
    if node_run.status != "running" or node_run.lease_token != lease_token:
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，当前错误不会覆盖新尝试",
            node_id=node_run.node_id,
        )
    node_run.status = "failed"
    node_run.error_code = error_code
    node_run.error_message = error_message
    node_run.finished_at = utcnow()
    node_run.lease_owner = None
    node_run.lease_expires_at = None
    _finish_attempt(
        db,
        node_run,
        lease_token=lease_token,
        status="failed",
        error_code=error_code,
        error_message=error_message,
    )
    previous_status = run.status
    if previous_status not in {"cancel_pending", "failure_pending"}:
        run.error_code = error_code
        run.error_message = error_message
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.failed",
        payload={
            "node_id": node_run.node_id,
            "code": error_code,
            "message": error_message,
        },
    )
    if previous_status in {"cancel_pending", "failure_pending"}:
        _finish_settling_publish(
            db,
            run=run,
            node_run=node_run,
            succeeded=False,
            error_code=error_code,
            error_message=error_message,
        )
    else:
        active_publish_nodes = _cancel_failure_siblings(
            db,
            run=run,
            failed_node_id=node_run.id,
        )
        append_run_event(
            db,
            run_id=run.id,
            event_type=(
                "run.failure_pending"
                if active_publish_nodes
                else (
                    "run.failure_deferred"
                    if run.status == "paused"
                    else "run.failed"
                )
            ),
            payload={
                "code": error_code,
                "message": error_message,
                "uninterruptible_node_ids": [
                    node.node_id for node in active_publish_nodes
                ],
            },
        )
    return node_run


def _create_human_task(
    db: Session,
    context: NodeExecutionContext,
    form_schema_override: Optional[dict[str, Any]] = None,
) -> ExecutionHumanTask:
    run, node_run = _lock_run_and_node(db, context.node_run.id)
    _ensure_run_accepts_result(run)
    if (
        node_run.status != "running"
        or node_run.lease_token != context.lease_token
    ):
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，不能创建人工任务",
            node_id=node_run.node_id,
        )
    context.run = run
    context.node_run = node_run
    config = context.node.get("config") or {}
    contract = _v2_node_instance_contract(run, node_run)
    native_human = bool(
        contract is not None
        and contract.get("source") == "resource"
        and contract.get("execution_kind") == "human"
    )
    native_suspension = bool(
        contract is not None
        and contract.get("source") == "resource"
        and (contract.get("suspension") or {}).get("kind") == "human_task"
    )
    native_task = native_human or native_suspension
    resume_protocol = str(
        ((contract or {}).get("suspension") or {}).get("resume_protocol") or ""
    )
    suspension_payload = (
        (context.input_data or {}).get("_native_suspension") or {}
        if native_suspension
        else {}
    )
    form_schema = (
        form_schema_override
        if form_schema_override is not None
        else config.get("form_schema") or {}
    )
    if native_human and form_schema_override is None:
        if resume_protocol == "native.select.v1":
            form_schema = {
                "type": "object",
                "properties": {
                    "selected_ids": {
                        "type": "array",
                        "minItems": int(config.get("min_selected") or 0),
                        "maxItems": int(config.get("max_selected") or 1),
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "primary_id": {"type": ["string", "null"]},
                },
                "required": ["selected_ids"],
                "additionalProperties": False,
            }
        elif resume_protocol == "native.approval.v1":
            form_schema = {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["approved", "rejected"],
                    },
                    "reason": {"type": "string", "maxLength": 2000},
                },
                "required": ["decision"],
                "additionalProperties": False,
            }
        elif resume_protocol == "native.decision.v1":
            form_schema = {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": [
                            str(item.get("value"))
                            for item in config.get("options") or []
                            if isinstance(item, dict) and item.get("value")
                        ],
                    },
                    "reason": {"type": "string", "maxLength": 2000},
                },
                "required": ["decision"],
                "additionalProperties": False,
            }
    if node_run.node_type == "human.confirm" and not form_schema:
        form_schema = {
            "type": "object",
            "properties": {
                "approved": {
                    "type": "boolean",
                    "const": True,
                    "title": "我已核对变更计划并确认发布",
                },
                "reason": {
                    "type": "string",
                    "title": "备注",
                    "maxLength": 1000,
                },
            },
            "required": ["approved"],
            "additionalProperties": False,
        }
    candidate_role = config.get("candidate_role") or config.get(
        "candidate_role_key"
    )
    if candidate_role is not None:
        candidate_role = str(candidate_role)
        role_exists = (
            db.query(ExecutionRole.id)
            .filter(ExecutionRole.key == candidate_role)
            .first()
        )
        if role_exists is None:
            raise ExecutionApiError(
                422,
                "human_task_candidate_role_invalid",
                "人工任务配置的候选角色不存在",
            )
    task = (
        db.query(ExecutionHumanTask)
        .filter(ExecutionHumanTask.node_run_id == node_run.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    reopened = task is not None
    if task is None:
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=node_run.id,
            revision=1,
        )
        db.add(task)
    else:
        task.revision += 1
    task.title = (
        suspension_payload.get("title")
        or config.get("title")
        or context.node_run.node_name
    )
    task.description = suspension_payload.get("description") or config.get(
        "description"
    )
    renderer_contract: dict[str, Any] = {}
    if native_task:
        from app.execution.v2.canonical import canonical_sha256

        renderer_contract = {
            **deepcopy(contract.get("renderer_contract") or {}),
            "node_type": node_run.node_type,
            "type_version": node_run.node_type_version,
            "contract_digest": contract.get("contract_digest"),
            "suspension": deepcopy(contract.get("suspension")),
            "submission_schema_digest": canonical_sha256(form_schema),
        }
        if suspension_payload.get("renderer_payload") is not None:
            renderer_contract["payload"] = deepcopy(
                suspension_payload.get("renderer_payload")
            )
        if reopened and task.renderer_contract not in ({}, renderer_contract):
            raise ExecutionApiError(
                409,
                "human_renderer_contract_changed",
                "既有人工任务的 renderer contract 不允许变更",
            )
    task.form_schema = form_schema
    task.renderer_contract = renderer_contract
    task.draft_data = {}
    task.result_data = {}
    task.status = "open"
    task.assigned_user_id = None if candidate_role else run.created_by_id
    task.candidate_role_key = candidate_role
    task.claimed_by_id = None
    task.claimed_at = None
    task.completed_by_id = None
    task.completed_at = None
    db.flush()
    node_run.status = "waiting_human"
    node_run.input_data = context.input_data
    node_run.lease_owner = None
    node_run.lease_expires_at = None
    _finish_attempt(
        db,
        node_run,
        lease_token=context.lease_token,
        status="waiting_human",
    )
    if run.status != "paused":
        run.status = "waiting_human"
    append_run_event(
        db,
        run_id=context.run.id,
        event_type="human_task.reopened" if reopened else "human_task.created",
        payload={
            "task_id": task.id,
            "node_id": node_run.node_id,
            "title": task.title,
        },
    )
    return task


def _execute_builtin(context: NodeExecutionContext) -> dict[str, Any]:
    from app.execution.v2.kernel_handlers import execute_compat_builtin

    context.resolve_run_value = lambda value: _resolve_value(
        value, _run_context(context.db, context.run)
    )
    return execute_compat_builtin(context)


def _v2_node_instance_contract(
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
) -> dict[str, Any] | None:
    """Return the immutable per-node contract stored with a v2 run.

    v1 rows intentionally have no execution binding and keep their historical
    validation/dispatch behavior.  A v2 node must never resolve a newer live
    NodeSpec at execution time; effective schemas therefore come from the
    dependency lock copied onto the Run.
    """

    binding_digest = getattr(node_run, "execution_binding_digest", None)
    if not binding_digest:
        return None
    lock = (
        getattr(run, "dependency_lock", None)
        or getattr(run, "dependency_lock_snapshot", None)
        or {}
    )
    instances = lock.get("node_instances") if isinstance(lock, dict) else None
    if isinstance(instances, dict):
        candidate = instances.get(node_run.node_id)
        if isinstance(candidate, dict):
            return candidate
    if isinstance(instances, list):
        for candidate in instances:
            if (
                isinstance(candidate, dict)
                and candidate.get("node_id") == node_run.node_id
            ):
                return candidate
    raise ExecutionApiError(
        503,
        "node_contract_snapshot_missing",
        "v2 节点缺少已发布的不可变契约快照",
        details={
            "node_id": node_run.node_id,
            "execution_binding_digest": binding_digest,
        },
    )


def _validate_v2_node_payload(
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    *,
    direction: str,
    value: Any,
) -> None:
    contract = _v2_node_instance_contract(run, node_run)
    if contract is None:
        return
    schema_key = (
        "effective_input_schema"
        if direction == "input"
        else "effective_output_schema"
    )
    schema = contract.get(schema_key)
    if schema is None:
        raise ExecutionApiError(
            503,
            "node_contract_snapshot_missing",
            "v2 节点缺少有效数据结构快照",
            details={"node_id": node_run.node_id, "schema": schema_key},
        )
    result = validate_json_instance(
        schema,
        value,
        path_prefix=f"$.nodes.{node_run.node_id}.{direction}",
    )
    if not result.valid:
        raise ExecutionApiError(
            422,
            "node_input_invalid" if direction == "input" else "node_output_invalid",
            "节点输入不符合已发布契约"
            if direction == "input"
            else "节点输出不符合已发布契约",
            details=result.as_dict(),
        )


def execute_claimed_node(db: Session, node_run_id: str, lease_token: str) -> None:
    node_run = db.get(ExecutionNodeRun, node_run_id)
    if node_run is None:
        raise not_found("节点运行", node_run_id)
    if node_run.status != "running" or node_run.lease_token != lease_token:
        raise conflict(
            "node_lease_lost",
            "节点租约已失效",
            node_id=node_run.node_id,
        )
    node = _definition_node_map(node_run.run).get(node_run.node_id)
    if node is None:
        fail_node(
            db,
            node_run_id=node_run.id,
            lease_token=lease_token,
            error_code="node_definition_missing",
            error_message="运行快照中缺少节点定义",
        )
        return
    globals_patch: dict[str, Any] | None = None
    globals_conflict: str | None = None
    selected_edge_ids: set[str] | None = None
    try:
        persisted_placement_request = (
            (node_run.input_data or {}).get("placement_request")
            if report_image_placement_node_config(node)
            else None
        )
        persisted_native_suspension_request = (
            (node_run.input_data or {}).get("_native_suspension_request")
            if getattr(node_run, "execution_binding_digest", None)
            else None
        )
        input_data = _node_input(db, node_run.run, node)
        if isinstance(persisted_placement_request, dict):
            # A conflict decision is collected in the API transaction, then
            # persisted on the node and executed by the Worker on the next
            # attempt.  Input mappings are recomputed on every attempt, so the
            # explicit request must be merged back after that recomputation.
            input_data = {
                **input_data,
                "placement_request": persisted_placement_request,
            }
        _assert_declared_root_refs(node_run.run, input_data)
        _validate_v2_node_payload(
            node_run.run,
            node_run,
            direction="input",
            value=input_data,
        )
        if isinstance(persisted_native_suspension_request, dict):
            # Internal resume state is appended only after portable input
            # validation.  It cannot be injected by node mapping or Release
            # JSON, yet remains durable across the next claim attempt.
            input_data = {
                **input_data,
                "_native_suspension_request": persisted_native_suspension_request,
            }
        node_run.input_data = input_data
        attempt = (
            db.query(ExecutionNodeAttempt)
            .filter(
                ExecutionNodeAttempt.node_run_id == node_run.id,
                ExecutionNodeAttempt.lease_token == lease_token,
            )
            .one()
        )
        attempt.input_data = input_data
        context = NodeExecutionContext(
            db=db,
            run=node_run.run,
            node_run=node_run,
            node=node,
            input_data=input_data,
            worker_id=node_run.lease_owner or "",
            lease_token=lease_token,
        )
        execution_kind = (
            getattr(node_run, "execution_kind", None)
            if getattr(node_run, "execution_binding_digest", None)
            else None
        )
        if execution_kind == "human" or (
            execution_kind is None and node_run.node_type in HUMAN_NODE_TYPES
        ):
            immutable_contract = _v2_node_instance_contract(
                node_run.run, node_run
            )
            native_human = bool(
                immutable_contract is not None
                and immutable_contract.get("source") == "resource"
            )
            auto_output = None
            if native_human:
                config = node.get("config") or {}
                candidates = input_data.get("items") or []
                resume_protocol = str(
                    (immutable_contract.get("suspension") or {}).get(
                        "resume_protocol"
                    )
                    or ""
                )
                if (
                    resume_protocol == "native.select.v1"
                    and config.get("auto_submit_single_candidate") is True
                    and len(candidates) == 1
                    and int(config.get("min_selected") or 0) <= 1
                    and int(config.get("max_selected") or 1) >= 1
                ):
                    candidate_id = str(candidates[0].get("id") or "")
                    auto_output = _native_human_submission(
                        db,
                        run=node_run.run,
                        node_run=node_run,
                        task=None,
                        data={
                            "selected_ids": [candidate_id],
                            "primary_id": (
                                candidate_id
                                if config.get("require_primary")
                                else None
                            ),
                        },
                        actor=None,
                    )
            else:
                auto_output = _auto_submit_single_candidate(db, context)
                if auto_output is None:
                    auto_output = _auto_complete_paper_existing_record_decision(
                        context
                    )
                if auto_output is None:
                    auto_output = _auto_complete_paper_judgement(context)
                if auto_output is None:
                    auto_output = auto_complete_report_image_placement(context)
            if auto_output is not None:
                _validate_v2_node_payload(
                    node_run.run,
                    node_run,
                    direction="output",
                    value=auto_output,
                )
                complete_node(
                    db,
                    node_run_id=node_run.id,
                    lease_token=lease_token,
                    output_data=auto_output,
                )
            else:
                form_schema = None
                if not native_human:
                    form_schema = _paper_existing_record_form_schema(context)
                    if form_schema is None:
                        form_schema = _paper_judgement_form_schema(context)
                    if form_schema is None:
                        form_schema = report_image_placement_form_schema(context)
                _create_human_task(
                    db,
                    context,
                    form_schema_override=form_schema,
                )
            return
        if execution_kind == "external_side_effect" or (
            execution_kind is None and node_run.node_type in EXTERNAL_NODE_TYPES
        ):
            if _reopen_paper_judgement_for_standard_value(db, context):
                return
            _prepare_external_operation_wait(db, context)
            return
        contract = _v2_node_instance_contract(node_run.run, node_run)
        executor = None
        if contract is not None and contract.get("source") == "resource":
            from app.execution.v2.registry import handler_for_binding

            try:
                executor = handler_for_binding(
                    node_run.execution_binding_digest
                )
            except LookupError as exc:
                raise ExecutionApiError(
                    503,
                    "node_capability_unavailable",
                    "当前 Worker 不提供节点锁定的 exact handler",
                    details={
                        "node_id": node_run.node_id,
                        "execution_binding_digest": node_run.execution_binding_digest,
                    },
                ) from exc
        else:
            executor = node_registry.executor(
                node_run.node_type,
                node_run.node_type_version,
            )
        output = (
            executor(context)
            if executor is not None
            else _execute_builtin(context)
        )
        if contract is not None and contract.get("source") == "resource":
            from app.execution.v2.native_handlers import NodeExecutionResult

            if isinstance(output, NodeExecutionResult):
                if output.suspension is not None:
                    declared_suspension = contract.get("suspension") or {}
                    requested_suspension = output.suspension
                    if (
                        declared_suspension.get("kind") != "human_task"
                        or requested_suspension.get("kind") != "human_task"
                        or requested_suspension.get("resume_protocol")
                        != declared_suspension.get("resume_protocol")
                    ):
                        raise ExecutionApiError(
                            503,
                            "native_suspension_contract_mismatch",
                            "handler 请求的 suspension 与已锁定 NodeSpec 不一致",
                        )
                    form_schema = requested_suspension.get("form_schema")
                    if not isinstance(form_schema, dict):
                        raise ExecutionApiError(
                            503,
                            "native_suspension_schema_missing",
                            "handler 未提供受限的 Human submission schema",
                        )
                    context.input_data = {
                        key: value
                        for key, value in input_data.items()
                        if key != "_native_suspension_request"
                    }
                    context.input_data["_native_suspension"] = deepcopy(
                        requested_suspension
                    )
                    _create_human_task(
                        db,
                        context,
                        form_schema_override=form_schema,
                    )
                    return
                globals_patch = output.globals_patch
                globals_conflict = output.globals_conflict
                selected_edge_ids = (
                    set(output.selected_edge_ids)
                    if output.selected_edge_ids is not None
                    else None
                )
                output = output.output
        if not isinstance(output, dict):
            output = {"value": output}
        _validate_v2_node_payload(
            node_run.run,
            node_run,
            direction="output",
            value=output,
        )
    except ExecutionApiError as exc:
        fail_node(
            db,
            node_run_id=node_run.id,
            lease_token=lease_token,
            error_code=exc.code,
            error_message=exc.message,
        )
    except Exception as exc:
        fail_node(
            db,
            node_run_id=node_run.id,
            lease_token=lease_token,
            error_code="node_execution_failed",
            error_message=str(exc),
        )
    else:
        try:
            complete_node(
                db,
                node_run_id=node_run.id,
                lease_token=lease_token,
                output_data=output,
                globals_patch=globals_patch,
                globals_conflict=globals_conflict,
                selected_edge_ids=selected_edge_ids,
            )
        except ExecutionApiError as exc:
            fail_node(
                db,
                node_run_id=node_run.id,
                lease_token=lease_token,
                error_code=exc.code,
                error_message=exc.message,
            )


def claim_human_task(
    db: Session,
    *,
    task_id: str,
    expected_revision: int,
    actor: ExecutionUser,
) -> ExecutionHumanTask:
    run, task, node_run = _lock_run_task_and_node(db, task_id)
    _ensure_run_accepts_result(run)
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    if task.revision != expected_revision:
        raise conflict(
            "human_task_revision_conflict",
            "人工任务已被更新",
            current_revision=task.revision,
        )
    if task.status not in {"open", "claimed"}:
        raise conflict("human_task_closed", "人工任务已处理", status=task.status)
    if not can_user_handle_human_task(db, task=task, user=actor):
        raise ExecutionApiError(
            403,
            "human_task_not_eligible",
            "该人工任务未分配给您或您的角色",
        )
    if task.claimed_by_id not in (None, actor.id):
        raise conflict(
            "human_task_claimed",
            "人工任务已被其他用户领取",
            claimed_by_id=task.claimed_by_id,
        )
    if task.claimed_by_id is None:
        task.claimed_by_id = actor.id
        task.claimed_at = utcnow()
        task.status = "claimed"
        task.revision += 1
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.claimed",
            actor_type="user",
            actor_id=actor.id,
            payload={"task_id": task.id, "revision": task.revision},
        )
    return task


def _ensure_human_task_ownership(
    db: Session,
    *,
    task: ExecutionHumanTask,
    actor: ExecutionUser,
) -> None:
    """确保任务由 actor 持有。

    任务处于 open 且无人领取时隐式领取（前端不再要求单独的领取步骤，
    减少一次人工点击）；已被他人领取或已处理时仍然拒绝，保留并发保护。
    """
    if task.status == "claimed" and task.claimed_by_id == actor.id:
        return
    if task.status == "open" and task.claimed_by_id is None:
        if not can_user_handle_human_task(db, task=task, user=actor):
            raise ExecutionApiError(
                403,
                "human_task_not_eligible",
                "该人工任务未分配给您或您的角色",
            )
        task.claimed_by_id = actor.id
        task.claimed_at = utcnow()
        task.status = "claimed"
        task.revision += 1
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.claimed",
            actor_type="user",
            actor_id=actor.id,
            payload={"task_id": task.id, "revision": task.revision, "implicit": True},
        )
        return
    raise conflict(
        "human_task_not_owned",
        "人工任务已由其他人员领取或已处理",
        claimed_by_id=task.claimed_by_id,
    )


def save_human_task_draft(
    db: Session,
    *,
    task_id: str,
    expected_revision: int,
    data: dict[str, Any],
    actor: ExecutionUser,
) -> ExecutionHumanTask:
    run, task, node_run = _lock_run_task_and_node(db, task_id)
    _ensure_run_accepts_result(run)
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    if task.revision != expected_revision:
        raise conflict(
            "human_task_revision_conflict",
            "人工任务草稿已被更新",
            current_revision=task.revision,
        )
    _ensure_human_task_ownership(db, task=task, actor=actor)
    task.draft_data = data
    task.revision += 1
    append_run_event(
        db,
        run_id=task.run_id,
        event_type="human_task.draft_saved",
        actor_type="user",
        actor_id=actor.id,
        payload={"task_id": task.id, "revision": task.revision},
    )
    return task


def submit_human_task(
    db: Session,
    *,
    task_id: str,
    expected_revision: int,
    data: dict[str, Any],
    actor: ExecutionUser,
) -> ExecutionHumanTask:
    run, task, node_run = _lock_run_task_and_node(db, task_id)
    _ensure_run_accepts_result(run)
    if task.revision != expected_revision:
        raise conflict(
            "human_task_revision_conflict",
            "人工任务已被更新",
            current_revision=task.revision,
        )
    _ensure_human_task_ownership(db, task=task, actor=actor)
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    form_schema = effective_human_task_form_schema(task)
    _raise_payload_validation(
        schema=form_schema,
        value=data,
        path_prefix="$.data",
        code="human_task_input_invalid",
        message="人工任务输入校验失败",
    )
    if form_schema != (task.form_schema or {}):
        # Preserve the contract that actually validated the submission for
        # later audit/history views of a task created before this upgrade.
        task.form_schema = form_schema
    normalized_data = _normalize_human_submission(
        db,
        run=run,
        node_run=node_run,
        data=data,
        task=task,
        actor=actor,
    )
    if normalized_data.pop("_human_rejected", False):
        reason = str(normalized_data.get("reason") or "人工任务已拒绝")
        task.status = "rejected"
        task.result_data = normalized_data
        task.completed_by_id = actor.id
        task.completed_at = utcnow()
        task.revision += 1
        node_run.status = "failed"
        node_run.error_code = "human_task_rejected"
        node_run.error_message = reason
        node_run.finished_at = utcnow()
        run.error_code = node_run.error_code
        run.error_message = reason
        _cancel_failure_siblings(
            db,
            run=run,
            failed_node_id=node_run.id,
            actor_user_id=actor.id,
        )
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.rejected",
            actor_type="user",
            actor_id=actor.id,
            payload={"task_id": task.id, "node_id": node_run.node_id},
        )
        return task
    if normalized_data.pop("_native_retry_with_decision", False):
        task.status = "completed"
        task.result_data = deepcopy(normalized_data)
        task.completed_by_id = actor.id
        task.completed_at = utcnow()
        task.revision += 1
        previous_run_status = run.status
        clean_input = {
            key: value
            for key, value in (node_run.input_data or {}).items()
            if key not in {
                "_native_suspension",
                "_native_suspension_request",
            }
        }
        node_run.input_data = {
            **clean_input,
            "_native_suspension_request": deepcopy(normalized_data),
        }
        node_run.status = "ready"
        node_run.output_data = {}
        node_run.ready_at = utcnow()
        node_run.finished_at = None
        node_run.error_code = None
        node_run.error_message = None
        node_run.lease_owner = None
        node_run.lease_token = None
        node_run.lease_expires_at = None
        db.flush()
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.completed",
            actor_type="user",
            actor_id=actor.id,
            payload={"task_id": task.id, "node_id": node_run.node_id},
        )
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="node.ready",
            actor_type="user",
            actor_id=actor.id,
            payload={
                "node_id": node_run.node_id,
                "reason": "native_retry_with_decision",
            },
        )
        _refresh_run_status(db, run)
        if run.status != previous_run_status:
            append_run_event(
                db,
                run_id=task.run_id,
                event_type=f"run.{run.status}",
                payload={"status": run.status},
            )
        return task
    try:
        _validate_v2_node_payload(
            run,
            node_run,
            direction="output",
            value=normalized_data,
        )
    except ExecutionApiError as exc:
        task.status = "completed_rejected_contract"
        task.result_data = normalized_data
        task.completed_by_id = actor.id
        task.completed_at = utcnow()
        task.revision += 1
        node_run.status = "failed"
        node_run.error_code = exc.code
        node_run.error_message = exc.message
        node_run.finished_at = utcnow()
        run.error_code = exc.code
        run.error_message = exc.message
        _cancel_failure_siblings(
            db,
            run=run,
            failed_node_id=node_run.id,
            actor_user_id=actor.id,
        )
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.contract_rejected",
            actor_type="user",
            actor_id=actor.id,
            payload={
                "task_id": task.id,
                "node_id": node_run.node_id,
                "code": exc.code,
            },
        )
        return task
    task.status = "completed"
    task.result_data = normalized_data
    task.completed_by_id = actor.id
    task.completed_at = utcnow()
    task.revision += 1
    previous_run_status = run.status
    if is_deferred_report_image_placement(normalized_data):
        node_run.input_data = {
            **(node_run.input_data or {}),
            "placement_request": normalized_data,
        }
        node_run.status = "ready"
        node_run.output_data = {}
        node_run.ready_at = utcnow()
        node_run.finished_at = None
        node_run.error_code = None
        node_run.error_message = None
        node_run.lease_owner = None
        node_run.lease_token = None
        node_run.lease_expires_at = None
        db.flush()
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="human_task.completed",
            actor_type="user",
            actor_id=actor.id,
            payload={"task_id": task.id, "node_id": node_run.node_id},
        )
        append_run_event(
            db,
            run_id=task.run_id,
            event_type="node.ready",
            actor_type="user",
            actor_id=actor.id,
            payload={
                "node_id": node_run.node_id,
                "reason": "report_image_placement_deferred",
            },
        )
        _refresh_run_status(db, run)
        if run.status != previous_run_status:
            append_run_event(
                db,
                run_id=task.run_id,
                event_type=f"run.{run.status}",
                payload={"status": run.status},
            )
        return task
    node_run.status = "succeeded"
    node_run.output_data = normalized_data
    node_run.finished_at = utcnow()
    db.flush()
    _resolve_outgoing_edges(db, node_run)
    _activate_resolved_nodes(db, run)
    append_run_event(
        db,
        run_id=task.run_id,
        event_type="human_task.completed",
        actor_type="user",
        actor_id=actor.id,
        payload={"task_id": task.id, "node_id": node_run.node_id},
    )
    append_run_event(
        db,
        run_id=task.run_id,
        event_type="node.succeeded",
        actor_type="user",
        actor_id=actor.id,
        payload={"node_id": node_run.node_id, "output": normalized_data},
    )
    _refresh_run_status(db, run)
    if run.status != previous_run_status:
        append_run_event(
            db,
            run_id=task.run_id,
            event_type=f"run.{run.status}",
            payload={"status": run.status},
        )
    return task


def reject_human_task(
    db: Session,
    *,
    task_id: str,
    expected_revision: int,
    reason: str,
    actor: ExecutionUser,
) -> ExecutionHumanTask:
    run, task, node_run = _lock_run_task_and_node(db, task_id)
    _ensure_run_accepts_result(run)
    if task.revision != expected_revision:
        raise conflict(
            "human_task_revision_conflict",
            "人工任务已被更新",
            current_revision=task.revision,
        )
    _ensure_human_task_ownership(db, task=task, actor=actor)
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    rejected_result: dict[str, Any] = {"reason": reason}
    contract = _v2_node_instance_contract(run, node_run)
    resume_protocol = str(
        ((contract or {}).get("suspension") or {}).get("resume_protocol") or ""
    )
    if resume_protocol == "native.approval.v1":
        native_result = _native_human_submission(
            db,
            run=run,
            node_run=node_run,
            task=task,
            data={"decision": "rejected", "reason": reason},
            actor=actor,
        )
        if native_result is not None:
            native_result.pop("_human_rejected", None)
            rejected_result = native_result
    task.status = "rejected"
    task.result_data = rejected_result
    task.completed_by_id = actor.id
    task.completed_at = utcnow()
    task.revision += 1
    node_run.status = "failed"
    node_run.error_code = "human_task_rejected"
    node_run.error_message = reason
    node_run.finished_at = utcnow()
    run.error_code = node_run.error_code
    run.error_message = reason
    active_publish_nodes = _cancel_failure_siblings(
        db,
        run=run,
        failed_node_id=node_run.id,
        actor_user_id=actor.id,
    )
    append_run_event(
        db,
        run_id=task.run_id,
        event_type="human_task.rejected",
        actor_type="user",
        actor_id=actor.id,
        payload={"task_id": task.id, "node_id": node_run.node_id, "reason": reason},
    )
    append_run_event(
        db,
        run_id=task.run_id,
        event_type="node.failed",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "node_id": node_run.node_id,
            "code": node_run.error_code,
            "message": reason,
        },
    )
    append_run_event(
        db,
        run_id=task.run_id,
        event_type=(
            "run.failure_pending"
            if active_publish_nodes
            else (
                "run.failure_deferred"
                if run.status == "paused"
                else "run.failed"
            )
        ),
        payload={
            "code": run.error_code,
            "message": reason,
            "uninterruptible_node_ids": [
                node.node_id for node in active_publish_nodes
            ],
        },
    )
    return task


def set_run_control_status(
    db: Session,
    *,
    run_id: str,
    action: str,
    actor: ExecutionUser,
) -> ExecutionRun:
    run = _lock_run(db, run_id)
    if action == "pause":
        if run.status not in {
            "queued",
            "running",
            "waiting_human",
            "waiting_external",
        }:
            raise conflict("run_not_pausable", "当前运行不能暂停", status=run.status)
        run.status = "paused"
    elif action == "resume":
        if run.status != "paused":
            raise conflict("run_not_resumable", "当前运行不是暂停状态", status=run.status)
        run.status = "running"
        run.finished_at = None
        _refresh_run_status(db, run)
    elif action == "cancel":
        if run.status in RUN_TERMINAL_STATUSES:
            return run
        if run.status in {"cancel_pending", "failure_pending"}:
            return run
        now = utcnow()
        tasks = (
            db.query(ExecutionHumanTask)
            .filter(
                ExecutionHumanTask.run_id == run.id,
                ExecutionHumanTask.status.in_(["open", "claimed"]),
            )
            .order_by(ExecutionHumanTask.id.asc())
            .with_for_update()
            .all()
        )
        for task in tasks:
            task.status = "cancelled"
            task.revision += 1
            task.completed_by_id = actor.id
            task.completed_at = now
            append_run_event(
                db,
                run_id=run.id,
                event_type="human_task.cancelled",
                actor_type="user",
                actor_id=actor.id,
                payload={"task_id": task.id},
            )
        nodes = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.status.notin_(NODE_TERMINAL_STATUSES),
            )
            .order_by(ExecutionNodeRun.id.asc())
            .with_for_update()
            .all()
        )
        active_publish_nodes = [
            node
            for node in nodes
            if node.status == "running" and node.node_type == "artifact.publish"
        ]
        active_publish_ids = {node.id for node in active_publish_nodes}
        # 已被 Bridge 领取的外部操作可能正在写入旧系统，同样不可中断：
        # 操作先转 cancel_pending，节点保持等待，由 Bridge 回报终态后再
        # 按阶段决定运行如何收尾。
        unsettled_external_operations = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.run_id == run.id,
                ExecutionExternalOperation.status.in_(
                    UNSETTLED_EXTERNAL_OPERATION_STATUSES
                ),
            )
            .order_by(ExecutionExternalOperation.id.asc())
            .with_for_update()
            .all()
        )
        unsettled_external_node_ids = {
            operation.node_run_id
            for operation in unsettled_external_operations
        }
        for operation in unsettled_external_operations:
            previous_operation_status = operation.status
            if operation.status == "in_progress":
                operation.status = "cancel_pending"
            append_run_event(
                db,
                run_id=run.id,
                event_type=(
                    "external_operation.cancel_pending"
                    if previous_operation_status == "in_progress"
                    else "external_operation.cancel_requested"
                ),
                actor_type="user",
                actor_id=actor.id,
                payload={
                    "operation_id": operation.id,
                    "status": operation.status,
                    "reason": "run_cancelled",
                    "reconciliation_required": (
                        operation.status == "reconciliation_required"
                    ),
                    # Every state preserved here may already have crossed the
                    # remote boundary; cancellation cannot prove absence.
                    "remote_write_performed": None,
                },
            )
        uninterruptible_ids = (
            active_publish_ids | unsettled_external_node_ids
        )
        cancelled_node_ids = [
            node.id for node in nodes if node.id not in uninterruptible_ids
        ]
        if cancelled_node_ids:
            attempts = (
                db.query(ExecutionNodeAttempt)
                .filter(
                    ExecutionNodeAttempt.node_run_id.in_(cancelled_node_ids),
                    ExecutionNodeAttempt.status.in_(
                        ["running", "waiting_human", "waiting_external"]
                    ),
                )
                .with_for_update()
                .all()
            )
            for attempt in attempts:
                attempt.status = "cancelled"
                attempt.error_code = "run_cancelled"
                attempt.error_message = "流程运行已取消"
                attempt.finished_at = now
            _cancel_prepared_external_operations(
                db,
                run_id=run.id,
                node_run_ids=cancelled_node_ids,
                actor_user_id=actor.id,
                reason="run_cancelled",
            )
        for node in nodes:
            if node.id in uninterruptible_ids:
                continue
            node.status = "cancelled"
            node.finished_at = now
            node.lease_owner = None
            node.lease_token = None
            node.lease_expires_at = None
        if active_publish_nodes or unsettled_external_operations:
            run.status = "cancel_pending"
            run.finished_at = None
        else:
            run.status = "cancelled"
            run.finished_at = now
    else:
        raise ValueError(f"unknown run action: {action}")
    append_run_event(
        db,
        run_id=run.id,
        event_type=f"run.{run.status}",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "status": run.status,
            "uninterruptible_node_ids": (
                [
                    node.node_id
                    for node in nodes
                    if node.id in uninterruptible_ids
                ]
                if action == "cancel"
                else []
            ),
        },
    )
    append_audit_log(
        db,
        action=f"run.{action}",
        resource_type="execution_run",
        resource_id=run.id,
        actor_user_id=actor.id,
    )
    return run


def retry_failed_node(
    db: Session,
    *,
    run_id: str,
    node_id: str,
    actor: ExecutionUser,
    reason: Optional[str] = None,
) -> ExecutionNodeRun:
    run = _lock_run(db, run_id)
    if run.status in {"cancelled", "completed", "failure_pending", "cancel_pending"}:
        raise conflict(
            "run_not_retryable",
            "已取消或已完成的流程不能重试节点",
            status=run.status,
        )
    node = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == run_id,
            ExecutionNodeRun.node_id == node_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if node is None:
        raise not_found("节点运行", node_id)
    if node.status != "failed":
        raise conflict("node_not_retryable", "只有失败节点可以重试", status=node.status)
    node.status = "ready"
    node.ready_at = utcnow()
    node.error_code = None
    node.error_message = None
    node.finished_at = None
    node.lease_owner = None
    node.lease_token = None
    node.lease_expires_at = None

    remaining_failed = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == run.id,
            ExecutionNodeRun.id != node.id,
            ExecutionNodeRun.status == "failed",
        )
        .order_by(ExecutionNodeRun.id.asc())
        .with_for_update()
        .all()
    )
    restored_node_ids: list[str] = []
    if not remaining_failed:
        # A run failure cancels every still-reversible sibling. Those nodes
        # are part of the same retry boundary: merely retrying the failed node
        # would otherwise leave its downstream nodes permanently cancelled.
        cancelled_nodes = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.status == "cancelled",
            )
            .order_by(ExecutionNodeRun.id.asc())
            .with_for_update()
            .all()
        )
        for cancelled in cancelled_nodes:
            cancelled.status = "pending"
            cancelled.ready_at = utcnow()
            cancelled.started_at = None
            cancelled.finished_at = None
            cancelled.input_data = {}
            cancelled.output_data = {}
            cancelled.error_code = None
            cancelled.error_message = None
            cancelled.lease_owner = None
            cancelled.lease_token = None
            cancelled.lease_expires_at = None
            restored_node_ids.append(cancelled.node_id)
        db.flush()
        _activate_resolved_nodes(db, run)
        if run.status != "paused":
            run.status = "running"
        run.error_code = None
        run.error_message = None
        run.finished_at = None
    else:
        # Do not resume scheduling while another failed node still requires an
        # explicit decision. The last retry request reopens the retry boundary.
        run.status = "failed"
        run.error_code = remaining_failed[0].error_code
        run.error_message = remaining_failed[0].error_message
    append_run_event(
        db,
        run_id=run.id,
        event_type="node.retry_requested",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "node_id": node.node_id,
            "reason": reason,
            "restored_node_ids": restored_node_ids,
            "remaining_failed_node_ids": [
                item.node_id for item in remaining_failed
            ],
        },
    )
    append_audit_log(
        db,
        action="node.retry",
        resource_type="execution_node_run",
        resource_id=node.id,
        actor_user_id=actor.id,
        details={
            "reason": reason,
            "restored_node_ids": restored_node_ids,
            "remaining_failed_node_ids": [
                item.node_id for item in remaining_failed
            ],
        },
    )
    return node
