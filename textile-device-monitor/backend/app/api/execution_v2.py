"""Management API for immutable Execution v2 workflow releases."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.api.execution import AuthContext, ExecutionRoute, permission
from app.execution.errors import ExecutionApiError, not_found
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionNodeRun,
    ExecutionReleasePreflight,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionWorkflowActivationReceipt,
    ExecutionWorkflowRelease,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    utcnow,
)
from app.execution.release_v2 import (
    apply_release,
    export_version_release,
    preflight_release,
    preview_v1_migration,
    publish_release,
    receipt_view,
    release_view,
    rollback_workflow,
    put_deployment_binding,
)
from app.execution.schemas import (
    WorkflowReleaseApplyRequest,
    WorkflowReleaseBindingRequest,
    WorkflowReleasePreflightRequest,
    WorkflowReleasePublishRequest,
    WorkflowReleaseRollbackRequest,
    WorkflowV1MigrationPreviewRequest,
)
from app.execution.v2.registry import (
    get_installed_registry,
    list_node_specs,
    registry_revision,
    resolve_node_spec,
)


router = APIRouter(
    prefix="/execution/v2",
    tags=["execution-v2"],
    route_class=ExecutionRoute,
)


def _public_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return value.public_dict()


def _release_or_404(db: Session, release_id: str) -> ExecutionWorkflowRelease:
    release = db.get(ExecutionWorkflowRelease, release_id)
    if release is None:
        raise not_found("Workflow Release", release_id)
    return release


@router.get("/node-specs")
def node_specs(
    node_type: Optional[str] = Query(default=None, alias="type"),
    _auth: AuthContext = Depends(permission("workflow.design")),
):
    items = [_public_value(item) for item in list_node_specs()]
    if node_type:
        items = [item for item in items if item.get("type") == node_type]
    return {"items": items}


@router.get("/node-specs/{node_type}/{type_version}")
def node_spec(
    node_type: str,
    type_version: int,
    contract_digest: Optional[str] = Query(default=None),
    _auth: AuthContext = Depends(permission("workflow.design")),
):
    try:
        return _public_value(
            resolve_node_spec(node_type, type_version, contract_digest)
        )
    except (LookupError, ValueError) as exc:
        raise ExecutionApiError(
            404,
            "node_spec_not_found",
            "NodeSpec 不存在",
            details={"type": node_type, "type_version": type_version},
        ) from exc


@router.get("/packs")
def packs(_auth: AuthContext = Depends(permission("workflow.design"))):
    return {
        "items": [
            _public_value(item)
            for item in get_installed_registry().list_packs()
        ]
    }


@router.get("/assets")
def assets(_auth: AuthContext = Depends(permission("workflow.design"))):
    return {
        "items": [
            _public_value(item)
            for item in get_installed_registry().list_assets()
        ]
    }


@router.get("/monitoring")
def monitoring(
    _auth: AuthContext = Depends(permission("audit.read")),
    db: Session = Depends(get_db),
):
    """Expose the P1 rollout signals from installed and persisted facts."""

    installed_packs = [
        _public_value(item)
        for item in get_installed_registry().list_packs()
    ]
    installed_specs = [_public_value(item) for item in list_node_specs()]
    specs_by_pack: dict[str, list[dict[str, Any]]] = {}
    for spec in installed_specs:
        specs_by_pack.setdefault(str(spec.get("pack_id")), []).append(spec)
    pack_readiness = []
    for pack in installed_packs:
        owned = specs_by_pack.get(str(pack.get("pack_id")), [])
        publishable = [item for item in owned if item.get("publishable")]
        ready_count = sum(item.get("ready") is True for item in publishable)
        pack_readiness.append(
            {
                "pack_id": pack.get("pack_id"),
                "pack_version": pack.get("pack_version"),
                "distribution_digest": pack.get("distribution_digest"),
                "manifest_ready": pack.get("ready") is True,
                "publishable_node_count": len(publishable),
                "ready_publishable_node_count": ready_count,
                "ready": bool(
                    pack.get("ready") is True
                    and ready_count == len(publishable)
                ),
            }
        )

    issue_codes: Counter[str] = Counter()
    preflight_scopes: Counter[str] = Counter()
    content_valid_count = 0
    publish_ready_count = 0
    preflights = db.query(ExecutionReleasePreflight).all()
    for row in preflights:
        report = row.report or {}
        preflight_scopes[row.scope] += 1
        content_valid_count += int(report.get("content_valid") is True)
        publish_ready_count += int(report.get("publish_ready") is True)
        for issue in report.get("issues") or []:
            code = issue.get("code") if isinstance(issue, dict) else None
            if code:
                issue_codes[str(code)] += 1

    heartbeat_threshold = utcnow() - timedelta(
        seconds=int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS)
    )
    live_bindings = {
        digest
        for (digest,) in (
            db.query(
                ExecutionWorkerNodeCapability.execution_binding_digest
            )
            .join(
                ExecutionWorkerHeartbeat,
                ExecutionWorkerHeartbeat.worker_id
                == ExecutionWorkerNodeCapability.worker_id,
            )
            .filter(
                ExecutionWorkerNodeCapability.ready.is_(True),
                ExecutionWorkerHeartbeat.status == "running",
                ExecutionWorkerHeartbeat.last_seen_at >= heartbeat_threshold,
                ExecutionWorkerHeartbeat.protocol_version.like("2.%"),
            )
            .distinct()
            .all()
        )
    }
    ready_v2_nodes = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.status == "ready",
            ExecutionNodeRun.execution_binding_digest.is_not(None),
        )
        .all()
    )
    unavailable_bindings: Counter[str] = Counter(
        str(node.execution_binding_digest)
        for node in ready_v2_nodes
        if node.execution_binding_digest not in live_bindings
    )
    run_statuses = Counter(
        status
        for (status,) in db.query(ExecutionRun.status)
        .filter(ExecutionRun.release_id.is_not(None))
        .all()
    )
    receipt_actions = Counter(
        action
        for (action,) in db.query(ExecutionWorkflowActivationReceipt.action)
        .all()
    )
    shadow_mismatch_count = (
        db.query(ExecutionAuditLog)
        .filter(
            ExecutionAuditLog.action
            == "execution_v2.claim.shadow_mismatch"
        )
        .count()
    )
    return {
        "observed_at": utcnow().isoformat(),
        "registry_revision": registry_revision(),
        "pack_readiness": sorted(
            pack_readiness, key=lambda item: str(item["pack_id"])
        ),
        "preflights": {
            "total": len(preflights),
            "by_scope": dict(sorted(preflight_scopes.items())),
            "content_valid": content_valid_count,
            "publish_ready": publish_ready_count,
            "issue_codes": dict(sorted(issue_codes.items())),
        },
        "shadow_mismatch": {"count": shadow_mismatch_count},
        "node_capability_unavailable": {
            "ready_v2_node_count": len(ready_v2_nodes),
            "unavailable_node_count": sum(unavailable_bindings.values()),
            "by_execution_binding_digest": dict(
                sorted(unavailable_bindings.items())
            ),
        },
        "v2_runs": {
            "total": sum(run_statuses.values()),
            "by_status": dict(sorted(run_statuses.items())),
        },
        "activation_receipts": {
            "total": sum(receipt_actions.values()),
            "by_action": dict(sorted(receipt_actions.items())),
        },
    }


@router.post("/workflow-releases/preflight")
def content_preflight(
    payload: WorkflowReleasePreflightRequest,
    auth: AuthContext = Depends(permission("workflow.design", csrf=True)),
    db: Session = Depends(get_db),
):
    report = preflight_release(
        db,
        document=payload.document,
        actor=auth.user,
        scope="content",
    )
    db.commit()
    return report


@router.post("/workflow-releases/apply", status_code=201)
def apply_staged_release(
    payload: WorkflowReleaseApplyRequest,
    auth: AuthContext = Depends(permission("workflow.design", csrf=True)),
    db: Session = Depends(get_db),
):
    release = apply_release(
        db,
        document=payload.document,
        preflight_token=payload.preflight_token,
        actor=auth.user,
    )
    db.commit()
    return release_view(db, release)


@router.get("/workflow-releases/{release_id}")
def get_release(
    release_id: str,
    _auth: AuthContext = Depends(permission("workflow.design")),
    db: Session = Depends(get_db),
):
    value = release_view(db, _release_or_404(db, release_id))
    value["required_bindings"] = [
        {
            "kind": "root_slot",
            "slot_id": slot["slot_id"],
            "access": slot["access"],
            "required": slot["required"],
        }
        for slot in value["document"]["resources"].get("root_slots") or []
    ]
    return value


def _frontend_root_bindings(
    db: Session,
    release: ExecutionWorkflowRelease,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    declared = {
        slot["slot_id"]: slot
        for slot in release.portable_document["resources"].get("root_slots")
        or []
    }
    root_bindings: dict[str, Any] = {}
    for index, item in enumerate(rows):
        if not isinstance(item, dict) or set(item) - {
            "slot",
            "slot_id",
            "storage_root_id",
            "root_id",
            "role",
        }:
            raise ExecutionApiError(
                422,
                "deployment_binding_invalid",
                "root_bindings 项不合法",
                details={"index": index},
            )
        slot_id = str(item.get("slot") or item.get("slot_id") or "")
        if slot_id not in declared:
            raise ExecutionApiError(
                422,
                "deployment_binding_unknown_slot",
                "root binding 引用了未声明的 slot",
                details={"slot_id": slot_id},
            )
        if item.get("role") not in {None, declared[slot_id]["access"]}:
            raise ExecutionApiError(
                422,
                "root_access_mismatch",
                "root binding role 与 slot access 不一致",
            )
        storage_identity = item.get("storage_root_id") or item.get("root_id")
        if storage_identity in {None, ""}:
            continue
        root = db.get(ExecutionStorageRoot, str(storage_identity))
        if root is None:
            root = (
                db.query(ExecutionStorageRoot)
                .filter(ExecutionStorageRoot.root_id == str(storage_identity))
                .one_or_none()
            )
        if root is None:
            raise not_found("存储根", str(storage_identity))
        root_bindings[slot_id] = {
            "root_id": root.root_id,
            "revision": int(root.binding_revision or 1),
        }
    return {
        "root_slots": root_bindings,
        "credential_slots": {},
        "role_slots": {},
        "rule_slots": {},
    }


def _binding_view(binding) -> dict[str, Any]:
    root_bindings = [
        {
            "slot": slot_id,
            "storage_root_id": value["storage_root_id"],
            "root_id": value["root_id"],
            "role": None,
            "revision": value["revision"],
        }
        for slot_id, value in sorted(
            (binding.binding.get("root_slots") or {}).items()
        )
    ]
    return {
        "id": binding.id,
        "environment": binding.environment,
        "revision": binding.revision,
        "digest": binding.digest,
        "bindings": binding.binding,
        "root_bindings": root_bindings,
        "created_at": binding.created_at.isoformat(),
    }


@router.put("/workflow-releases/{release_id}/deployment-binding")
def update_deployment_binding(
    release_id: str,
    payload: WorkflowReleaseBindingRequest,
    auth: AuthContext = Depends(permission("workflow.publish", csrf=True)),
    db: Session = Depends(get_db),
):
    release = _release_or_404(db, release_id)
    bindings = payload.bindings
    if payload.root_bindings is not None:
        bindings = _frontend_root_bindings(db, release, payload.root_bindings)
    environment = payload.environment
    if environment == "default":
        environment = settings.EXECUTION_ENVIRONMENT_ID
    binding = put_deployment_binding(
        db,
        release_id=release.id,
        environment=environment,
        expected_revision=payload.expected_revision,
        bindings=bindings or {},
        actor=auth.user,
    )
    db.commit()
    return {"deployment_binding": _binding_view(binding)}


@router.post("/workflow-releases/{release_id}/preflight")
def staged_preflight(
    release_id: str,
    auth: AuthContext = Depends(permission("workflow.publish", csrf=True)),
    db: Session = Depends(get_db),
):
    release = _release_or_404(db, release_id)
    report = preflight_release(
        db,
        document=release.portable_document,
        actor=auth.user,
        release=release,
        scope="publish",
    )
    db.commit()
    return report


@router.post("/workflow-releases/{release_id}/publish")
def publish_staged_release(
    release_id: str,
    payload: WorkflowReleasePublishRequest,
    auth: AuthContext = Depends(permission("workflow.publish", csrf=True)),
    db: Session = Depends(get_db),
):
    release, version, receipt = publish_release(
        db,
        release_id=release_id,
        preflight_token=payload.preflight_token,
        actor=auth.user,
        reason=payload.reason,
    )
    db.commit()
    value = release_view(db, release)
    value.update(
        {
            "local_version": version.version_number,
            "deployed_contract_checksum": version.deployed_contract_checksum,
            "activation": receipt_view(receipt),
        }
    )
    return value


@router.get("/workflows/{workflow_id}/versions/{local_version}/export")
def export_release(
    workflow_id: str,
    local_version: int,
    _auth: AuthContext = Depends(permission("workflow.design")),
    db: Session = Depends(get_db),
):
    return export_version_release(
        db, workflow_id=workflow_id, local_version=local_version
    )


@router.post("/workflows/{workflow_id}/rollback")
def rollback_release(
    workflow_id: str,
    payload: WorkflowReleaseRollbackRequest,
    auth: AuthContext = Depends(permission("workflow.publish", csrf=True)),
    db: Session = Depends(get_db),
):
    receipt = rollback_workflow(
        db,
        workflow_id=workflow_id,
        target_local_version=payload.target_local_version,
        reason=payload.reason,
        actor=auth.user,
    )
    db.commit()
    return {"activation": receipt_view(receipt)}


@router.post("/migrations/v1/preview")
def migration_preview(
    payload: WorkflowV1MigrationPreviewRequest,
    auth: AuthContext = Depends(permission("workflow.design", csrf=True)),
    db: Session = Depends(get_db),
):
    return preview_v1_migration(
        db,
        workflow_id=payload.workflow_id,
        source=payload.source,
        actor=auth.user,
    )
