"""Direct operation submissions using the existing durable Bridge queue.

Callers provide values, a task project and an idempotency key. Workflow history
is optional; ownership, attempts, cancellation and receipts belong to the
operation itself. No HTTP handler performs a remote write.
"""

from copy import deepcopy
from datetime import timedelta
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.connector_updates import GENERIC_UPDATE, UPDATE_OPERATION, update_summary
from app.execution.events import append_audit_log
from app.execution.external_operations import (
    ACTIVE_REMOTE_OPERATION_STATUSES, LEGACY_CONNECTOR_KEY, LEGACY_CREDENTIAL_SYSTEM,
    _account_scope_key, _canonical_checksum, _ensure_operation_execution_available,
    _credential_for_node, _create_prepared_external_operation,
    _validated_paper_project_binding, build_generic_entry_summary,
    lock_legacy_remote_business_scope, public_external_attempt, public_external_operation,
)
from app.execution.models import (
    ExecutionCredential, ExecutionExternalOperation, ExecutionTaskSnapshotCache,
    ExecutionStorageRoot, ExecutionUser, utcnow,
)
from app.execution.project_rules import PAPER_FIBER_RULE_KEY, resolve_rule
from app.execution.adapter_packages import binding_fields, credential_system


GENERIC_ENTRY = "legacy_fibrecheck.check_record.generic_entry@1"


def operation_handler(connector_id, name, version):
    return {GENERIC_ENTRY: _generic_summary, GENERIC_UPDATE: update_summary}.get(
        f"{connector_id}.{name}@{version}"
    )


def workflow_operation_handler(connector_id, name, version):
    from app.execution import external_operations as operations

    if connector_id != "legacy_fibrecheck" or version != 1:
        return None
    return {
        "regenerated_fiber.count_upload": operations.prepare_legacy_regenerated_count_operation,
        "special_wool.image_upload": operations.prepare_legacy_special_wool_image_operation,
        "special_wool.image_review": operations.prepare_legacy_special_wool_review_operation,
        "microscopy.check_record_entry": operations.prepare_legacy_microscopy_check_record_entry_operation,
        "paper_fiber.qualitative_upload": operations.prepare_legacy_special_wool_qualitative_upload_operation,
        "paper_fiber.qualitative_review": operations.prepare_legacy_special_wool_qualitative_review_operation,
        "paper_fiber.check_record_entry": operations.prepare_legacy_generic_check_record_entry_operation,
    }.get(name)


def prepare_operation_node(context, *, operation):
    """Use the same submission service and durable queue for a frozen DAG node."""
    if context.node["config"]["operation_ref"] != operation.operation_ref:
        raise conflict("connector_operation_binding_mismatch", "操作与已发布契约不一致")
    if operation.workflow_handler is not None:
        return operation.workflow_handler(
            context.db, run=context.run, node_run=context.node_run,
            node=context.node, input_data=context.input_data,
        )
    data = context.input_data
    summary = operation.handler(context.db, data)
    remote_key = lock_legacy_remote_business_scope(context.db, sample_number=summary["target_sample_number"])
    credential = _credential_for_node(context.db, run=context.run, node=context.node, system_key=credential_system(operation.connector_id))
    summary["connector_submission"] = {
        "operation_ref": operation.operation_ref, "connector_version": operation.connector_version,
        "contract_digest": operation.contract_digest, "input": deepcopy(data),
        **binding_fields(operation),
    }
    return _create_prepared_external_operation(
        context.db, run=context.run, node_run=context.node_run, credential=credential,
        account_scope_key=_account_scope_key(credential.account_name), remote_business_key=remote_key,
        request_summary=summary, operation_key_prefix=operation.operation_ref,
    )


def operation_api_available(operation) -> bool:
    from app.execution.v2.registry import get_installed_registry

    return ((operation.handler is not None or operation.workflow_handler is not None)
            and isinstance(operation.spec.get("input_schema"), dict)
            and get_installed_registry().resolve_pack(operation.pack_id, operation.pack_version).ready)


def resolve_operation(operation_ref: str, *, connector_version="*", contract_digest=None):
    from app.execution.v2.registry import get_installed_registry, resolve_connector_reference

    registry = get_installed_registry()
    try:
        connector, name, version = resolve_connector_reference(
            operation_ref, (item.connector_id for item in registry.connectors.all()),
        )
        operation = registry.connectors.resolve_operation(
            connector, connector_version, name, version, contract_digest,
        )
    except (LookupError, ValueError) as exc:
        raise ExecutionApiError(422, "connector_operation_unavailable", "操作未安装或版本不匹配") from exc
    if not operation_api_available(operation):
        raise ExecutionApiError(422, "connector_operation_unavailable", "该操作尚不支持直接提交")
    return operation


def _generic_summary(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    number = data["inspection_number"].strip().upper()
    cached = db.get(ExecutionTaskSnapshotCache, number)
    projects = (cached.snapshot or {}).get("projects", []) if cached else []
    selected = next((p for p in projects if p.get("project_key") == data["project_key"]), None)
    if selected is None:
        raise conflict("connector_task_project_missing", "请先读取任务信息并选择登记项目")
    project_input = {"selected_project": selected, "selected_project_key": data["project_key"]}
    project = _validated_paper_project_binding(project_input, rule=resolve_rule(db, PAPER_FIBER_RULE_KEY))
    expected = data["expected_existing_register_count"]
    if selected.get("register_count") != expected:
        raise conflict("connector_registration_changed", "已有登记数量已变化，请刷新后提交")
    return build_generic_entry_summary(
        source_number=number, project=project,
        result_contract={"worksheet": "Sheet1", "cell": "W32",
                         "value": data["result_value"].strip(), "unit": data.get("unit", "")},
        input_data={
            **project_input,
            "judgement_input": {name: data.get(name, "") for name in (
                "sample_identity", "judge_basis", "judgement", "standard_value",
            )},
            "registration_decision": {
                "expected_existing_register_count": expected,
                "existing_record_action": "append" if project["check_count"] == 1 and expected else "continue",
            },
        },
    )


def submit_operation(db: Session, *, actor: ExecutionUser, request) -> tuple[ExecutionExternalOperation, bool]:
    # Serialize one user's idempotency namespace, including different samples.
    # Ten LAN users do not need a separate queue or a distributed lock service.
    db.query(ExecutionUser).filter(ExecutionUser.id == actor.id).with_for_update().one()
    key = _canonical_checksum({"owner": actor.id, "key": request.idempotency_key})
    request_checksum = _canonical_checksum(request.model_dump(mode="json", exclude={
        "idempotency_key", *({"inspection_number"} if request.inspection_number is None else set()),
    }))
    existing = db.query(ExecutionExternalOperation).filter_by(operation_key=key).one_or_none()
    if existing is not None:
        if (existing.request_summary or {}).get("connector_submission", {}).get("request_checksum") != request_checksum:
            raise conflict("external_operation_idempotency_conflict", "同一提交键不能用于不同内容")
        # Retry returns the original result even after cache/credential/Pack updates.
        return existing, True

    contract = resolve_operation(request.operation_ref, connector_version=request.connector_version,
                                 contract_digest=request.contract_digest)
    error = next(Draft202012Validator(contract.spec["input_schema"]).iter_errors(request.input), None)
    if error:
        raise ExecutionApiError(422, "connector_operation_input_invalid", "操作参数不符合契约",
                                details={"path": list(error.absolute_path), "message": error.message})
    if contract.workflow_handler is not None:
        from app.execution.connector_context import DirectOperationContext

        number = str(request.inspection_number or request.input.get("inspection_number") or "").strip().upper()
        if not number:
            raise ExecutionApiError(422, "connector_inspection_number_required", "请指定 inspection_number")
        credential = db.query(ExecutionCredential).filter_by(
            id=request.credential_id, user_id=actor.id, is_active=True, system_key=credential_system(contract.connector_id),
        ).one_or_none()
        if credential is None:
            raise not_found("检务凭据", request.credential_id)
        roots = [{"root_id": root.root_id, "access": root.access_mode} for root in db.query(ExecutionStorageRoot)
                 .filter_by(is_active=True, is_available=True).all()]
        submission = {"operation_ref": request.operation_ref, "connector_version": contract.connector_version,
            **binding_fields(contract),
            "contract_digest": contract.contract_digest, "request_checksum": request_checksum,
            "input": deepcopy(request.input), "root_slots": roots}
        context = DirectOperationContext(inspection_number=number, created_by_id=actor.id,
            input_data=request.input, operation_key=key, submission=submission, root_slots=roots, credential=credential)
        operation, reused = contract.workflow_handler(db, run=context, node_run=None, node={}, input_data=request.input)
        _ensure_operation_execution_available(operation)
        append_audit_log(db, action="connector_operation.submit", resource_type="execution_external_operation",
                         resource_id=operation.id, actor_user_id=actor.id,
                         details={"operation_ref": request.operation_ref, "payload_checksum": operation.payload_checksum,
                                  "status": operation.status, "remote_write_performed": False})
        return operation, reused
    summary = contract.handler(db, request.input)
    # Match workflow preparation's sample → credential ordering.
    remote_key = lock_legacy_remote_business_scope(db, sample_number=summary["target_sample_number"])
    credential = (db.query(ExecutionCredential).filter_by(
        id=request.credential_id, user_id=actor.id, is_active=True, system_key=credential_system(contract.connector_id),
    ).with_for_update().one_or_none())
    if credential is None:
        raise not_found("检务凭据", request.credential_id)
    summary["connector_submission"] = {
        "operation_ref": request.operation_ref, "connector_version": contract.connector_version,
        "contract_digest": contract.contract_digest, "request_checksum": request_checksum,
        **binding_fields(contract),
        "input": deepcopy(request.input),
    }
    blocker = db.query(ExecutionExternalOperation).filter(
        ExecutionExternalOperation.remote_business_key == remote_key,
        ExecutionExternalOperation.status.in_(ACTIVE_REMOTE_OPERATION_STATUSES),
    ).first()
    if blocker:
        raise conflict("external_remote_business_conflict", "该样品有未完成的写入操作", operation_id=blocker.id)
    now = utcnow()
    operation = ExecutionExternalOperation(
        operation_key=key, created_by_id=actor.id,
        connector_key=contract.connector_id, credential_id=credential.id,
        credential_revision=credential.revision, account_scope_key=_account_scope_key(credential.account_name),
        remote_business_key=remote_key, status="approved", request_summary=summary,
        payload_checksum=_canonical_checksum({"request_summary": summary, "credential_binding": {
            "credential_id": credential.id, "credential_revision": credential.revision,
            "account_scope_key": _account_scope_key(credential.account_name), "remote_business_key": remote_key,
        }}),
        approved_by_id=actor.id, approved_at=now, approval_expires_at=None,
        preflight_expires_at=now + timedelta(minutes=settings.EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES),
    )
    _ensure_operation_execution_available(operation)
    db.add(operation)
    db.flush()
    append_audit_log(db, action="connector_operation.submit", resource_type="execution_external_operation",
                     resource_id=operation.id, actor_user_id=actor.id,
                     details={"operation_ref": request.operation_ref, "payload_checksum": operation.payload_checksum,
                              "status": operation.status, "remote_write_performed": False})
    return operation, False


def operation_for_actor(db: Session, *, operation_id: str, actor: ExecutionUser, lock=False):
    query = db.query(ExecutionExternalOperation).filter_by(id=operation_id, run_id=None)
    if actor.role != "admin":
        query = query.filter_by(created_by_id=actor.id)
    if lock:
        query = query.populate_existing().with_for_update()
    operation = query.one_or_none()
    if operation is None:
        raise not_found("连接器操作", operation_id)
    return operation


def operation_view(operation: ExecutionExternalOperation) -> dict[str, Any]:
    submission = (operation.request_summary or {}).get("connector_submission", {})
    return {**public_external_operation(operation), "created_by_id": operation.created_by_id,
            **{key: submission.get(key) for key in ("operation_ref", "connector_version", "contract_digest")},
            "source_kind": "submitted_values", "result": deepcopy(operation.receipt) or None,
            "recovery": deepcopy((operation.verification or {}).get("automatic_recovery")),
            "cancel_requested": bool((operation.verification or {}).get("cancel_requested")),
            "attempts": [public_external_attempt(attempt) for attempt in operation.attempts],
            "status_url": f"/api/execution/v1/connector-operations/{operation.id}"}


def cancel_operation(db: Session, *, operation: ExecutionExternalOperation, actor: ExecutionUser):
    if operation.status == "approved":
        operation.status, operation.completed_at = "cancelled", utcnow()
    elif operation.status == "in_progress":
        operation.status = "cancel_pending"
        operation.verification = {**(operation.verification or {}), "cancel_requested": True}
    elif (operation.status == "reconciliation_required"
          and operation.request_summary.get("operation_type") == UPDATE_OPERATION):
        # Keep the target fence until readback settles it, but stop automatic writes.
        operation.verification = {**(operation.verification or {}), "cancel_requested": True}
    elif operation.status in {"cancelled", "cancel_pending"}:
        return
    else:
        raise conflict("connector_operation_not_cancellable", "操作已完成或需要核对实际写入结果")
    append_audit_log(db, action="connector_operation.cancel", resource_type="execution_external_operation",
                     resource_id=operation.id, actor_user_id=actor.id, details={"status": operation.status})
