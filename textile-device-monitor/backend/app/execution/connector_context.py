"""Explicit service inputs for submissions without a Workflow Run."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_

from app.execution.models import ExecutionExternalOperation, ExecutionRun


@dataclass(frozen=True)
class DirectOperationContext:
    inspection_number: str
    created_by_id: str
    input_data: dict[str, Any]
    operation_key: str
    submission: dict[str, Any]
    root_slots: list[dict[str, str]] = field(default_factory=list)
    credential: Any = None
    # Optional provenance. No synthetic Run or NodeRun is stored.
    id: None = None


def source_scope(source):
    run_id = source.run_id if isinstance(source, ExecutionExternalOperation) else source.id
    if run_id is not None:
        return ExecutionExternalOperation.run_id == run_id
    number = ((source.request_summary or {}).get("source_inspection_number")
              if isinstance(source, ExecutionExternalOperation) else source.inspection_number)
    return and_(ExecutionExternalOperation.created_by_id == source.created_by_id,
                ExecutionExternalOperation.request_summary["source_inspection_number"].as_string() == number)


def artifact_in_context(db, artifact, context):
    if context is None:
        return False
    if not isinstance(context, DirectOperationContext):
        return artifact.run_id == context.id
    source = db.get(ExecutionRun, artifact.run_id) if artifact.run_id else None
    return bool(source and source.created_by_id == context.created_by_id
                and source.inspection_number.strip().upper() == context.inspection_number)


def operation_context(db, operation):
    if operation.run_id:
        return db.get(ExecutionRun, operation.run_id)
    submission = (operation.request_summary or {}).get("connector_submission") or {}
    if not submission:
        return None
    return DirectOperationContext(
        inspection_number=operation.request_summary["source_inspection_number"], created_by_id=operation.created_by_id,
        input_data=submission["input"], operation_key=operation.operation_key, submission=submission,
        root_slots=submission.get("root_slots", []),
    )


def declared_roots(context):
    return context.root_slots if isinstance(context, DirectOperationContext) else (context.definition_snapshot or {}).get("root_slots", [])


def preparation_identity(context, node_run):
    return (ExecutionExternalOperation.operation_key == context.operation_key if isinstance(context, DirectOperationContext)
            else ExecutionExternalOperation.node_run_id == node_run.id)
