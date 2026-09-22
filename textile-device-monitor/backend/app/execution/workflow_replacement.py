"""Local workflow replacement; portable releases never contain database IDs.

Every change to a replacement pair locks Workflow rows in ascending ID order.
Run creation uses the same row lock before reading the active version. Existing
Runs and Human tasks continue against their immutable snapshots.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import conflict, not_found
from app.execution.events import append_audit_log
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowRelease,
    ExecutionWorkflowVersion,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    utcnow,
)
from app.execution.validation import (
    definition_checksum,
    validate_definition,
    workflow_contract_checksum,
)


def assert_not_archived(workflow: ExecutionWorkflow) -> None:
    if workflow.archived_at is not None:
        raise conflict(
            "workflow_archived",
            "该流程已归档，不能新增运行、编辑或发布",
            workflow_id=workflow.id,
        )


def is_catalog_workflow(workflow: ExecutionWorkflow) -> bool:
    return workflow.published_version_number is not None and workflow.archived_at is None and not (
        workflow.replaces_workflow_id and not workflow.is_enabled
    )


def current_version(
    db: Session, workflow: ExecutionWorkflow
) -> ExecutionWorkflowVersion:
    version = (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow.id,
            ExecutionWorkflowVersion.version_number
            == workflow.published_version_number,
        )
        .one_or_none()
    )
    if version is None:
        raise conflict(
            "workflow_not_published", "流程尚无已发布版本", workflow_id=workflow.id
        )
    return version


def source_snapshot(db: Session, workflow: ExecutionWorkflow) -> dict[str, Any]:
    version = current_version(db, workflow)
    return {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "draft_revision": workflow.draft_revision,
        "definition_checksum": version.checksum,
        "contract_checksum": version.contract_checksum,
    }


def lock_workflows(
    db: Session, workflow_ids: list[str]
) -> dict[str, ExecutionWorkflow]:
    # populate_existing is essential: the caller may have loaded the rows before
    # waiting for a concurrent publish, activation, or Run-creation transaction.
    rows = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.id.in_(workflow_ids))
        .order_by(ExecutionWorkflow.id)
        .populate_existing()
        .with_for_update()
        .all()
    )
    result = {row.id: row for row in rows}
    for workflow_id in workflow_ids:
        if workflow_id not in result:
            raise not_found("流程", workflow_id)
    return result


def _active_source(db: Session, target: ExecutionWorkflow) -> Optional[dict[str, Any]]:
    version = current_version(db, target)
    release = db.get(ExecutionWorkflowRelease, version.release_id)
    return deepcopy(release.migration_source) if release else None


def replacement_context(
    db: Session,
    *,
    document: dict[str, Any],
    release: Optional[ExecutionWorkflowRelease],
    requested: Optional[dict[str, Any]],
    lock: bool = False,
) -> Optional[dict[str, Any]]:
    """Keep the Workflow pair fixed; freeze migration evidence per Release."""
    target = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.slug == document["release"]["slug"])
        .one_or_none()
    )
    frozen = deepcopy(release.migration_source) if release is not None else None
    inherited = frozen
    if target is not None and target.replaces_workflow_id:
        inherited = inherited or _active_source(db, target)
        if inherited is None:
            raise conflict("replacement_source_missing", "接替流程缺少本地迁移来源记录")
    context = deepcopy(requested or inherited)
    if frozen and requested and requested != frozen:
        raise conflict(
            "replacement_source_changed", "已发布 Release 的迁移来源不可更改"
        )
    ids = [target.id] if target is not None else []
    if context:
        ids.append(context["workflow_id"])
    rows = lock_workflows(db, ids) if lock and ids else {}
    if target is not None:
        target = rows.get(target.id, target)
        assert_not_archived(target)
    if not context:
        # Recheck after a concurrent registration acquired the same target lock.
        if target is not None and target.replaces_workflow_id:
            raise conflict("replacement_source_changed", "接替来源已变化，请重新预检")
        return None
    source = rows.get(context["workflow_id"]) or db.get(
        ExecutionWorkflow, context["workflow_id"]
    )
    if source is None:
        raise not_found("来源流程", context["workflow_id"])
    if source.management_mode != "draft_v1" or source.id == (
        target.id if target else None
    ):
        raise conflict("replacement_source_invalid", "接替来源必须是独立的 v1 流程")
    if source.slug == document["release"]["slug"]:
        raise conflict("workflow_slug_conflict", "接替流程必须使用新的 slug")
    registered = target is not None and target.replaces_workflow_id == source.id
    if target is not None and not registered:
        raise conflict(
            "replacement_target_conflict", "该 slug 已用于其他流程，请选择新的 slug"
        )
    other = (
        db.query(ExecutionWorkflow.id)
        .filter(
            ExecutionWorkflow.replaces_workflow_id == source.id,
            ExecutionWorkflow.id != (target.id if target else ""),
        )
        .first()
    )
    if other:
        raise conflict(
            "replacement_already_registered",
            "来源流程已有接替流程",
            workflow_id=other[0],
        )
    snapshot = source_snapshot(db, source)
    if registered and context == inherited:
        # Activation/revert increment draft revisions without changing the
        # published source. Keep that original evidence, but reject a source
        # version changed since preflight even for an existing replacement.
        if any(
            snapshot[key] != context[key] for key in snapshot if key != "draft_revision"
        ):
            raise conflict(
                "replacement_source_changed", "来源发布版本已变化，请重新生成候选并预检"
            )
        _verify_source_contract(current_version(db, source))
        return context
    assert_not_archived(source)
    if snapshot != context:
        raise conflict(
            "replacement_source_changed",
            "来源版本或 revision 已变化，请重新生成候选并预检",
        )
    version = current_version(db, source)
    _verify_source_contract(version)
    if (document.get("migration") or {}).get("source_digest") != context[
        "definition_checksum"
    ]:
        raise conflict(
            "replacement_source_digest_mismatch", "Release 迁移摘要与来源版本不一致"
        )
    return context


def release_source(
    db: Session, release: ExecutionWorkflowRelease
) -> Optional[dict[str, Any]]:
    """Expose inherited local provenance while a later release is still staged."""
    if release.migration_source:
        return deepcopy(release.migration_source)
    target = (
        db.query(ExecutionWorkflow)
        .filter(
            ExecutionWorkflow.slug == release.source_slug,
            ExecutionWorkflow.replaces_workflow_id.isnot(None),
        )
        .one_or_none()
    )
    if target is None:
        return None
    inherited = _active_source(db, target)
    source = db.get(ExecutionWorkflow, target.replaces_workflow_id)
    if source is not None and source.archived_at is None:
        snapshot = source_snapshot(db, source)
        digest = (release.portable_document.get("migration") or {}).get("source_digest")
        if digest == snapshot["definition_checksum"] and (
            inherited is None or snapshot["version_id"] != inherited["version_id"]
        ):
            # A newly generated candidate can update the source version while
            # preserving the pair. This suggestion is validated at preflight.
            return snapshot
    return inherited


def _verify_source_contract(version: ExecutionWorkflowVersion) -> None:
    if (
        definition_checksum(version.definition) != version.checksum
        or workflow_contract_checksum(version.definition, version.capabilities or {})
        != version.contract_checksum
    ):
        raise conflict(
            "replacement_source_contract_invalid", "来源版本的不可变摘要不一致"
        )


def _state(db: Session, workflow: ExecutionWorkflow) -> dict[str, Any]:
    version = current_version(db, workflow)
    return {
        "workflow_id": workflow.id,
        "slug": workflow.slug,
        "name": workflow.name,
        "draft_revision": workflow.draft_revision,
        "version_id": version.id,
        "version_number": version.version_number,
        "definition_checksum": version.checksum,
        "contract_checksum": version.contract_checksum,
        "release_digest": version.release_digest,
        "deployed_contract_checksum": version.deployed_contract_checksum,
        "is_enabled": workflow.is_enabled,
        "archived_at": (
            workflow.archived_at.isoformat() if workflow.archived_at else None
        ),
        "availability_code": workflow.availability_code,
        "availability_message": workflow.availability_message,
    }


def _last_action(db: Session, target_id: str) -> Optional[ExecutionAuditLog]:
    return (
        db.query(ExecutionAuditLog)
        .filter(
            ExecutionAuditLog.resource_type == "workflow",
            ExecutionAuditLog.resource_id == target_id,
            ExecutionAuditLog.action.in_(
                ["workflow_replacement.activate", "workflow_replacement.revert"]
            ),
        )
        .order_by(ExecutionAuditLog.id.desc())
        .first()
    )


def replacement_view(db: Session, workflow_id: str) -> dict[str, Any]:
    target = db.get(ExecutionWorkflow, workflow_id)
    if target is None:
        raise not_found("流程", workflow_id)
    if not target.replaces_workflow_id:
        return {
            "status": "unregistered",
            "source": None,
            "target": _state(db, target),
            "last_action": None,
        }
    source = db.get(ExecutionWorkflow, target.replaces_workflow_id)
    last = _last_action(db, target.id)
    active = source.archived_at is not None and target.is_enabled
    return {
        "status": (
            "active"
            if active
            else "reverted" if last and last.action.endswith(".revert") else "pending"
        ),
        "internal_acceptance_only": bool(
            (current_version(db, source).capabilities or {}).get("system_test")
        ),
        "source": _state(db, source),
        "target": _state(db, target),
        "last_action": (
            {"id": last.id, "action": last.action, "details": deepcopy(last.details)}
            if last
            else None
        ),
    }


def _validate_source_readiness(
    db: Session, source: ExecutionWorkflow, version: ExecutionWorkflowVersion
) -> None:
    _verify_source_contract(version)
    validation = validate_definition(version.definition, for_publish=True)
    if not validation.valid:
        raise conflict(
            "replacement_source_unavailable",
            "旧流程定义校验失败",
            validation=validation.as_dict(),
        )
    for slot in version.definition.get("root_slots") or []:
        root = (
            db.query(ExecutionStorageRoot)
            .filter(ExecutionStorageRoot.root_id == slot.get("root_id"))
            .one_or_none()
        )
        if root is None or not root.is_active or not root.is_available:
            raise conflict(
                "replacement_source_unavailable",
                "旧流程存储根不可用",
                root_id=slot.get("root_id"),
            )
    threshold = utcnow() - timedelta(
        seconds=int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS)
    )
    for node in version.definition.get("nodes") or []:
        # Old v1 versions have no exact execution binding. A current Worker with
        # this v1 executor can accept newly created v1 Runs in enforced mode.
        ready = (
            db.query(ExecutionWorkerNodeCapability.id)
            .join(
                ExecutionWorkerHeartbeat,
                ExecutionWorkerHeartbeat.worker_id
                == ExecutionWorkerNodeCapability.worker_id,
            )
            .filter(
                ExecutionWorkerNodeCapability.node_type == node["type"],
                ExecutionWorkerNodeCapability.node_type_version
                == int(node.get("type_version") or 1),
                ExecutionWorkerNodeCapability.ready.is_(True),
                ExecutionWorkerHeartbeat.status == "running",
                ExecutionWorkerHeartbeat.last_seen_at >= threshold,
            )
            .first()
        )
        if ready is None:
            raise conflict(
                "replacement_source_unavailable",
                "缺少可执行旧流程节点的在线 Worker",
                node_id=node["id"],
            )


def switch_replacement(
    db: Session,
    *,
    workflow_id: str,
    action: str,
    expected_source_revision: int,
    expected_target_revision: int,
    expected_source_version: int,
    expected_target_version: int,
    reason: str,
    actor: ExecutionUser,
    activation_audit_id: Optional[int] = None,
) -> dict[str, Any]:
    if settings.EXECUTION_CONTRACT_MODE.strip().lower() != "enforced":
        raise conflict(
            "execution_contract_mode_not_enforced", "接替和回切要求 enforced 契约模式"
        )
    if not reason.strip():
        raise conflict("replacement_reason_required", "请填写接替或回切原因")
    target = db.get(ExecutionWorkflow, workflow_id)
    if target is None or not target.replaces_workflow_id:
        raise conflict("replacement_not_registered", "该流程尚未登记接替来源")
    pair = lock_workflows(db, [target.id, target.replaces_workflow_id])
    target = pair[workflow_id]
    source = pair[target.replaces_workflow_id]
    assert_not_archived(target)
    if (
        source.draft_revision != expected_source_revision
        or target.draft_revision != expected_target_revision
        or source.published_version_number != expected_source_version
        or target.published_version_number != expected_target_version
    ):
        raise conflict(
            "replacement_revision_changed", "双方版本或 revision 已变化，请刷新后重试"
        )
    source_version = current_version(db, source)
    target_version = current_version(db, target)
    last = _last_action(db, target.id)
    release = db.get(ExecutionWorkflowRelease, target_version.release_id)
    evidence = (
        (last.details or {}).get("migration_source")
        if action == "revert"
        and last
        and last.action == "workflow_replacement.activate"
        else release.migration_source if release else None
    )
    if not evidence or evidence["workflow_id"] != source.id:
        raise conflict("replacement_source_missing", "当前 Release 缺少接替来源证据")
    if any(
        evidence[key] != source_snapshot(db, source)[key]
        for key in (
            "workflow_id",
            "version_id",
            "version_number",
            "definition_checksum",
            "contract_checksum",
        )
    ):
        raise conflict("replacement_source_changed", "旧流程发布版本与迁移来源不一致")
    _verify_source_contract(source_version)
    before = {"source": _state(db, source), "target": _state(db, target)}
    if action == "activate":
        assert_not_archived(source)
        if target.is_enabled:
            raise conflict("replacement_already_active", "接替流程已启用")
        if (source_version.capabilities or {}).get("system_test"):
            raise conflict(
                "replacement_internal_acceptance_only",
                "受控 Excel 流程仅用于内部验收，不接替生产入口",
            )
        if target.availability_code:
            raise conflict(
                "replacement_target_unavailable",
                target.availability_message or "接替流程不可用",
            )
        from app.execution.release_v2 import _validate_rollback_target

        _validate_rollback_target(db, target_version)
        source.archived_at = utcnow()
        source.is_enabled = False
        target.is_enabled = True
    elif action == "revert":
        if (
            last is None
            or last.action != "workflow_replacement.activate"
            or last.id != activation_audit_id
            or source.archived_at is None
            or not target.is_enabled
        ):
            raise conflict(
                "replacement_activation_changed",
                "当前接替回执或状态已变化，请刷新后重试",
            )
        prior = last.details["before"]["source"]
        if prior["is_enabled"] and prior["availability_code"] is None:
            _validate_source_readiness(db, source, source_version)
        source.archived_at = None
        source.is_enabled = prior["is_enabled"]
        source.availability_code = prior["availability_code"]
        source.availability_message = prior["availability_message"]
        target.is_enabled = False
    else:
        raise ValueError("unknown replacement action")
    for workflow in (source, target):
        workflow.draft_revision += 1
        workflow.updated_by_id = actor.id
    receipt = append_audit_log(
        db,
        action=f"workflow_replacement.{action}",
        resource_type="workflow",
        resource_id=target.id,
        actor_user_id=actor.id,
        details={
            "reason": reason.strip(),
            "migration_source": deepcopy(evidence),
            "activation_audit_id": activation_audit_id if action == "revert" else None,
            "before": before,
            "after": {"source": _state(db, source), "target": _state(db, target)},
        },
    )
    db.flush()
    return {"audit_id": receipt.id, "action": action, **replacement_view(db, target.id)}
