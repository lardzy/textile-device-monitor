from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.events import append_audit_log, append_run_event
from app.execution.external_operations import (
    LEGACY_REGENERATED_COUNT_NODE,
    LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
    LEGACY_SPECIAL_WOOL_REVIEW_NODE,
    prepare_legacy_regenerated_count_operation,
    prepare_legacy_special_wool_image_operation,
    prepare_legacy_special_wool_review_operation,
    settle_external_attempt_failure,
)
from app.execution.models import (
    ExecutionEdgeRun,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionFileMutation,
    ExecutionFileIndexEntry,
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
    utcnow,
)
from app.execution.persistence import build_file_gateway
from app.execution.registry import node_registry
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
    mapping = node.get("input_mapping") or {}
    resolved = _resolve_value(mapping, _run_context(db, run))
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
    members = candidate.get("files")
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


def _normalize_human_submission(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    data: dict[str, Any],
) -> dict[str, Any]:
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
        return {
            **data,
            "selected_folder_ids": selected_folder_ids,
            "selected_image_ids": [str(item["id"]) for item in normalized_images],
            "selected_images": normalized_images,
            "primary_image_id": primary_image_id,
            "primary_image": primary_image,
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
                    422,
                    "microscopy_sample_identity_invalid",
                    "请选择任务单中提供的样品识别",
                )
            sample_identity = submitted_identity
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
            if len(basis_options) == 1:
                judge_basis = basis_options[0]
            elif basis_options:
                if submitted_basis not in basis_options:
                    raise ExecutionApiError(
                        422,
                        "microscopy_judge_basis_invalid",
                        "请选择任务单中提供的判定依据",
                    )
                judge_basis = submitted_basis
            else:
                judge_basis = submitted_basis or None
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
            judgement = None

        return {
            "selected_project_key": selected_key,
            "selected_project": selected_project,
            "sample_name": sample_name,
            "sample_identity": sample_identity,
            "judgement_required": judgement_required,
            "judge_basis": judge_basis,
            "judgement": judgement,
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
            input_data=normalized_inputs,
            global_data=normalized_globals,
        )
        return existing, True
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
        input_data=normalized_inputs,
        global_data=normalized_globals,
    )
    db.add(run)
    db.flush()

    for node in definition["nodes"]:
        node_run = ExecutionNodeRun(
            run_id=run.id,
            node_id=node["id"],
            node_type=node["type"],
            node_type_version=node.get("type_version", 1),
            node_name=node.get("name") or node["type"],
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

    candidate = (
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
        .order_by(ExecutionNodeRun.ready_at.asc(), ExecutionNodeRun.created_at.asc())
        .first()
    )
    if candidate is None:
        return None

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
    if node_run.node_type == "branch.condition":
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
    operation remains durable across Worker/server restarts and requires a
    separate, explicit approval API call.
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
        LEGACY_REGENERATED_COUNT_NODE: (
            prepare_legacy_regenerated_count_operation
        ),
        LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE: (
            prepare_legacy_special_wool_image_operation
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
        "requires_final_approval": True,
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
) -> ExecutionNodeRun:
    run, node_run = _lock_run_and_node(db, node_run_id)
    _ensure_run_accepts_result(run)
    if node_run.status != "running" or node_run.lease_token != lease_token:
        raise conflict(
            "node_lease_lost",
            "节点租约已失效，当前结果不会被接受",
            node_id=node_run.node_id,
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
    if node_run.node_type == "variables.set":
        values = node_run.output_data.get("globals")
        if isinstance(values, dict):
            # Merge only after acquiring the run lock. Mutating context.run in
            # the executor allowed parallel variable nodes to overwrite each
            # other's updates before either completion obtained the lock.
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
    _resolve_outgoing_edges(db, node_run)
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
    form_schema = config.get("form_schema") or {}
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
    candidate_role = config.get("candidate_role")
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
    task.title = config.get("title") or context.node_run.node_name
    task.description = config.get("description")
    task.form_schema = form_schema
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
    node_type = context.node_run.node_type
    if node_type in {"core.start", "parallel.split", "parallel.join", "branch.condition"}:
        return context.input_data
    if node_type == "variables.set":
        values = _resolve_value(
            (context.node.get("config") or {}).get("values") or {},
            _run_context(context.db, context.run),
        )
        return {"globals": values}
    if node_type == "result.aggregate":
        return context.input_data
    if node_type == "core.end":
        return context.input_data
    raise ExecutionApiError(
        503,
        "node_capability_unavailable",
        f"节点“{context.node_run.node_name}”尚未配置运行适配器",
        details={
            "node_id": context.node_run.node_id,
            "node_type": context.node_run.node_type,
        },
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
    try:
        input_data = _node_input(db, node_run.run, node)
        _assert_declared_root_refs(node_run.run, input_data)
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
        if node_run.node_type in HUMAN_NODE_TYPES:
            _create_human_task(db, context)
            return
        if node_run.node_type in {
            LEGACY_REGENERATED_COUNT_NODE,
            LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE,
            LEGACY_SPECIAL_WOOL_REVIEW_NODE,
        }:
            _prepare_external_operation_wait(db, context)
            return
        executor = node_registry.executor(
            node_run.node_type,
            node_run.node_type_version,
        )
        output = (
            executor(context)
            if executor is not None
            else _execute_builtin(context)
        )
        if not isinstance(output, dict):
            output = {"value": output}
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
        complete_node(
            db,
            node_run_id=node_run.id,
            lease_token=lease_token,
            output_data=output,
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
    if task.status != "claimed" or task.claimed_by_id != actor.id:
        raise conflict("human_task_not_owned", "请先领取该人工任务")
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
    if task.status != "claimed" or task.claimed_by_id != actor.id:
        raise conflict("human_task_not_owned", "请先领取该人工任务")
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    _raise_payload_validation(
        schema=task.form_schema or {},
        value=data,
        path_prefix="$.data",
        code="human_task_input_invalid",
        message="人工任务输入校验失败",
    )
    normalized_data = _normalize_human_submission(
        db,
        run=run,
        node_run=node_run,
        data=data,
    )
    task.status = "completed"
    task.result_data = normalized_data
    task.completed_by_id = actor.id
    task.completed_at = utcnow()
    task.revision += 1
    previous_run_status = run.status
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
    if task.status != "claimed" or task.claimed_by_id != actor.id:
        raise conflict("human_task_not_owned", "请先领取该人工任务")
    if node_run.status != "waiting_human":
        raise conflict(
            "human_task_closed",
            "人工任务对应节点已不再等待处理",
            status=node_run.status,
        )
    task.status = "rejected"
    task.result_data = {"reason": reason}
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
