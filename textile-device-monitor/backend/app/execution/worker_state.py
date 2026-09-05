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
from app.execution.models import (
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    utcnow,
)


def default_worker_id() -> str:
    configured = os.getenv("EXECUTION_WORKER_ID", "").strip()
    if configured:
        return configured
    if hasattr(os, "uname"):
        return os.uname().nodename
    # Windows 没有 os.uname；COMPUTERNAME 是原生主机名来源
    return os.environ.get("COMPUTERNAME", "").strip() or "worker-local"


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
    capability_document: dict | None = None,
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
        # The exact capability rows reference the heartbeat.  Flush the parent
        # first so this works with databases that enforce foreign keys eagerly.
        db.flush()
    heartbeat.status = status
    heartbeat.last_seen_at = now
    heartbeat.last_error = last_error
    if capability_document is not None:
        protocol_version = str(
            capability_document.get("protocol_version") or ""
        ).strip()
        engine_version = str(
            capability_document.get("engine_version") or ""
        ).strip()
        capability_digest = str(
            capability_document.get("capability_digest") or ""
        ).strip()
        nodes = capability_document.get("nodes")
        if (
            not protocol_version
            or not engine_version
            or len(capability_digest) != 64
            or not isinstance(nodes, list)
        ):
            raise RuntimeError("invalid_execution_worker_capability_document")

        previous_digest = heartbeat.capability_digest
        heartbeat.protocol_version = protocol_version
        heartbeat.engine_version = engine_version
        heartbeat.capability_digest = capability_digest
        heartbeat.capabilities = {
            "dag": True,
            "file_index": True,
            "controlled_write": True,
            "outbox": True,
            "execution_v2": True,
            "node_binding_count": len(nodes),
        }
        if previous_digest != capability_digest:
            db.query(ExecutionWorkerNodeCapability).filter(
                ExecutionWorkerNodeCapability.worker_id == worker_id
            ).delete(synchronize_session=False)
            for node in nodes:
                if not isinstance(node, dict):
                    raise RuntimeError(
                        "invalid_execution_worker_capability_node"
                    )
                binding_digest = str(
                    node.get("execution_binding_digest") or ""
                ).strip()
                if len(binding_digest) != 64:
                    raise RuntimeError(
                        "invalid_execution_worker_capability_binding"
                    )
                db.add(
                    ExecutionWorkerNodeCapability(
                        worker_id=worker_id,
                        execution_binding_digest=binding_digest,
                        node_type=str(node.get("type") or ""),
                        node_type_version=int(node.get("type_version") or 0),
                        contract_digest=str(
                            node.get("contract_digest") or ""
                        ),
                        implementation_digest=str(
                            node.get("implementation_digest") or ""
                        ),
                        pack_id=str(node.get("pack_id") or ""),
                        pack_version=str(node.get("pack_version") or ""),
                        execution_kind=str(
                            node.get("execution_kind") or ""
                        ),
                        ready=bool(node.get("ready")),
                    )
                )
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
