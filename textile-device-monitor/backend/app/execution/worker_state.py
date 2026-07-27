from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.db_migrations import expected_head_revision
from app.execution.models import ExecutionWorkerHeartbeat, utcnow


def default_worker_id() -> str:
    configured = os.getenv("EXECUTION_WORKER_ID", "").strip()
    return configured or os.uname().nodename


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def record_worker_heartbeat(
    db: Session,
    *,
    worker_id: str,
    status: str = "running",
    last_error: str | None = None,
) -> ExecutionWorkerHeartbeat:
    now = utcnow()
    heartbeat = (
        db.query(ExecutionWorkerHeartbeat)
        .filter(ExecutionWorkerHeartbeat.worker_id == worker_id)
        .with_for_update()
        .one_or_none()
    )
    if heartbeat is None:
        heartbeat = ExecutionWorkerHeartbeat(
            worker_id=worker_id,
            started_at=now,
            capabilities={
                "dag": True,
                "file_index": True,
                "controlled_write": True,
                "outbox": True,
            },
        )
        db.add(heartbeat)
    heartbeat.status = status
    heartbeat.last_seen_at = now
    heartbeat.last_error = last_error
    db.flush()
    return heartbeat


def worker_heartbeat_is_fresh(
    db: Session,
    *,
    worker_id: str | None = None,
    timeout_seconds: int | None = None,
) -> tuple[bool, ExecutionWorkerHeartbeat | None]:
    threshold = utcnow() - timedelta(
        seconds=max(
            1,
            int(
                timeout_seconds
                if timeout_seconds is not None
                else settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS
            ),
        )
    )
    query = db.query(ExecutionWorkerHeartbeat).filter(
        ExecutionWorkerHeartbeat.status == "running"
    )
    if worker_id:
        query = query.filter(ExecutionWorkerHeartbeat.worker_id == worker_id)
    heartbeat = query.order_by(
        ExecutionWorkerHeartbeat.last_seen_at.desc()
    ).first()
    if heartbeat is None:
        return False, None
    return (
        normalize_utc(heartbeat.last_seen_at) >= threshold,
        heartbeat,
    )


def database_schema_is_current(db: Session) -> tuple[bool, str | None, str]:
    expected = expected_head_revision()
    try:
        current = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except SQLAlchemyError:
        return False, None, expected
    return current == expected, current, expected


def wait_for_current_database_schema() -> None:
    deadline = time.monotonic() + max(
        1,
        int(settings.EXECUTION_WORKER_SCHEMA_WAIT_SECONDS),
    )
    last_current: str | None = None
    expected = expected_head_revision()
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            try:
                current_ok, last_current, _ = database_schema_is_current(db)
                if current_ok:
                    return
            except SQLAlchemyError:
                pass
        time.sleep(1)
    raise RuntimeError(
        "Execution worker database schema did not reach Alembic head "
        f"{expected!r}; current={last_current!r}"
    )
