from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import DeviceStateEvent, DeviceTaskState
from app.services.device_tracking import TaskStateSnapshot


def get_task_state(db: Session, device_id: int) -> Optional[DeviceTaskState]:
    return db.query(DeviceTaskState).filter(DeviceTaskState.device_id == device_id).first()


def get_or_create_task_state(db: Session, device_id: int) -> DeviceTaskState:
    state = get_task_state(db, device_id)
    if state:
        return state

    state = DeviceTaskState(device_id=device_id)
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def snapshot_task_state(state: Optional[DeviceTaskState]) -> TaskStateSnapshot:
    if state is None:
        return TaskStateSnapshot()
    return TaskStateSnapshot(
        task_key=state.task_key,
        task_name=state.task_name,
        observed_in_progress=bool(state.observed_in_progress),
        last_status=state.last_status,
        last_progress=state.last_progress,
    )


def save_task_state(
    db: Session,
    state: DeviceTaskState,
    snapshot: TaskStateSnapshot,
) -> DeviceTaskState:
    state.task_key = snapshot.task_key
    state.task_name = snapshot.task_name
    state.observed_in_progress = snapshot.observed_in_progress
    state.last_status = snapshot.last_status
    state.last_progress = snapshot.last_progress
    db.commit()
    db.refresh(state)
    return state


def create_state_event(
    db: Session,
    *,
    device_id: int,
    event_type: str,
    status: str,
    task_key: Optional[str],
    task_name: Optional[str],
    task_progress: Optional[int],
    occurred_at: Optional[datetime] = None,
) -> DeviceStateEvent:
    event_data = dict(
        device_id=device_id,
        event_type=event_type,
        status=status,
        task_key=task_key,
        task_name=task_name,
        task_progress=task_progress,
    )
    if occurred_at is not None:
        event_data["occurred_at"] = occurred_at
    event = DeviceStateEvent(**event_data)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_latest_state_event_before(
    db: Session,
    *,
    device_id: int,
    before: datetime,
) -> Optional[DeviceStateEvent]:
    return (
        db.query(DeviceStateEvent)
        .filter(
            DeviceStateEvent.device_id == device_id,
            DeviceStateEvent.occurred_at < before,
        )
        .order_by(DeviceStateEvent.occurred_at.desc(), DeviceStateEvent.id.desc())
        .first()
    )


def get_state_events_in_range(
    db: Session,
    *,
    device_id: int,
    start_at: datetime,
    end_at: datetime,
) -> list[DeviceStateEvent]:
    return (
        db.query(DeviceStateEvent)
        .filter(
            DeviceStateEvent.device_id == device_id,
            DeviceStateEvent.occurred_at >= start_at,
            DeviceStateEvent.occurred_at <= end_at,
        )
        .order_by(DeviceStateEvent.occurred_at.asc(), DeviceStateEvent.id.asc())
        .all()
    )


def count_state_events(
    db: Session,
    *,
    device_id: int,
    event_type: str,
    start_at: datetime,
    end_at: datetime,
) -> int:
    return (
        db.query(DeviceStateEvent)
        .filter(
            DeviceStateEvent.device_id == device_id,
            DeviceStateEvent.event_type == event_type,
            DeviceStateEvent.occurred_at >= start_at,
            DeviceStateEvent.occurred_at <= end_at,
        )
        .count()
    )
