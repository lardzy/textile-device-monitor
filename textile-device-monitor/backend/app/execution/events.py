from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.execution.models import (
    ExecutionAuditLog,
    ExecutionEvent,
    ExecutionOutbox,
)


def append_run_event(
    db: Session,
    *,
    run_id: str,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
) -> ExecutionEvent:
    """Append the event and its outbox record in the caller's transaction."""

    event = ExecutionEvent(
        run_id=run_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload or {},
    )
    db.add(event)
    db.flush()
    db.add(
        ExecutionOutbox(
            event_id=event.event_id,
            topic=f"execution.run.{event_type}",
            aggregate_id=run_id,
            payload={
                "event_id": event.event_id,
                "sequence": event.id,
                "run_id": run_id,
                "type": event_type,
                "payload": event.payload,
            },
        )
    )
    return event


def append_audit_log(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_user_id: Optional[str] = None,
    request_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> ExecutionAuditLog:
    record = ExecutionAuditLog(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        details=details or {},
    )
    db.add(record)
    return record
