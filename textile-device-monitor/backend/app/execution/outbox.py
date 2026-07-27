from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.models import ExecutionEvent, ExecutionOutbox, utcnow


logger = logging.getLogger(__name__)


def claim_outbox_record(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int = 60,
) -> Optional[ExecutionOutbox]:
    """Lease one committed outbox row without blocking another worker."""

    now = utcnow()
    record = (
        db.query(ExecutionOutbox)
        .filter(
            ExecutionOutbox.dispatched_at.is_(None),
            ExecutionOutbox.dead_lettered_at.is_(None),
            ExecutionOutbox.available_at <= now,
            or_(
                ExecutionOutbox.lease_owner.is_(None),
                ExecutionOutbox.lease_expires_at.is_(None),
                ExecutionOutbox.lease_expires_at < now,
            ),
        )
        .order_by(ExecutionOutbox.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if record is None:
        return None
    record.lease_owner = worker_id
    record.lease_expires_at = now + timedelta(
        seconds=max(int(lease_seconds), 10)
    )
    record.attempts += 1
    db.flush()
    return record


def dispatch_outbox_record(
    db: Session,
    *,
    record_id: int,
    worker_id: str,
) -> bool:
    """Durably acknowledge the local event feed materialization.

    ExecutionEvent is the current SSE source and is inserted in the same
    transaction as this row. Verifying that durable event is therefore the
    first concrete outbox sink; future external connector sinks can be added
    here without changing workflow transactions.
    """

    record = (
        db.query(ExecutionOutbox)
        .filter(
            ExecutionOutbox.id == record_id,
            ExecutionOutbox.lease_owner == worker_id,
            or_(
                ExecutionOutbox.lease_expires_at.is_(None),
                ExecutionOutbox.lease_expires_at >= utcnow(),
            ),
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if (
        record is None
        or record.dispatched_at is not None
        or record.dead_lettered_at is not None
    ):
        return False
    event = (
        db.query(ExecutionEvent.id)
        .filter(ExecutionEvent.event_id == record.event_id)
        .one_or_none()
    )
    if event is None:
        raise RuntimeError("outbox_event_missing")
    record.dispatched_at = utcnow()
    record.lease_owner = None
    record.lease_expires_at = None
    record.last_error = None
    return True


def fail_outbox_record(
    db: Session,
    *,
    record_id: int,
    worker_id: str,
    error: Exception,
    max_attempts: Optional[int] = None,
) -> bool:
    record = (
        db.query(ExecutionOutbox)
        .filter(ExecutionOutbox.id == record_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if (
        record is None
        or record.dispatched_at is not None
        or record.dead_lettered_at is not None
        or record.lease_owner != worker_id
    ):
        return False
    limit = max(
        int(
            max_attempts
            if max_attempts is not None
            else getattr(settings, "EXECUTION_OUTBOX_MAX_ATTEMPTS", 10)
        ),
        1,
    )
    message = f"{type(error).__name__}: {error}"
    record.last_error = message[:4000]
    record.lease_owner = None
    record.lease_expires_at = None
    if record.attempts >= limit:
        record.dead_lettered_at = utcnow()
        logger.error(
            "Execution outbox record %s moved to dead letter after %s attempts",
            record.id,
            record.attempts,
        )
    else:
        retry_seconds = min(2 ** min(record.attempts, 10), 900)
        record.available_at = utcnow() + timedelta(seconds=retry_seconds)
    return True


def cleanup_outbox(
    db: Session,
    *,
    retention_days: Optional[int] = None,
) -> int:
    days = max(
        int(
            retention_days
            if retention_days is not None
            else getattr(settings, "EXECUTION_OUTBOX_RETENTION_DAYS", 7)
        ),
        1,
    )
    cutoff = utcnow() - timedelta(days=days)
    return (
        db.query(ExecutionOutbox)
        .filter(
            ExecutionOutbox.created_at < cutoff,
            or_(
                ExecutionOutbox.dispatched_at.is_not(None),
                ExecutionOutbox.dead_lettered_at.is_not(None),
            ),
        )
        .delete(synchronize_session=False)
    )
