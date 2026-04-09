from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Iterable, Optional


EVENT_STATUS = "status"
EVENT_TASK_START = "task_start"
EVENT_TASK_COMPLETE = "task_complete"
EVENT_DEVICE_OFFLINE = "device_offline"
OFFLINE_STATUS = "offline"
BUSY_STATUS = "busy"


@dataclass
class TaskStateSnapshot:
    task_key: Optional[str] = None
    task_name: Optional[str] = None
    observed_in_progress: bool = False
    last_status: Optional[str] = None
    last_progress: Optional[int] = None


@dataclass
class TaskStateDecision:
    next_state: TaskStateSnapshot
    emit_task_start: bool = False
    allow_completion: bool = False


@dataclass
class StateEventSnapshot:
    occurred_at: datetime
    status: str
    event_type: str


@dataclass
class UtilizationSummary:
    total_seconds: float
    busy_seconds: float

    @property
    def utilization_rate(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return (self.busy_seconds / self.total_seconds) * 100


def normalize_task_key(task_key: Optional[str]) -> Optional[str]:
    if task_key is None:
        return None
    normalized = str(task_key).strip()
    return normalized or None


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_in_progress(status: Optional[str], task_progress: Optional[int]) -> bool:
    if status == BUSY_STATUS:
        return True
    return task_progress is not None and 0 < task_progress < 100


def advance_task_state(
    current: TaskStateSnapshot,
    *,
    status: Optional[str],
    task_key: Optional[str],
    task_name: Optional[str],
    task_progress: Optional[int],
) -> TaskStateDecision:
    normalized_key = normalize_task_key(task_key)
    next_state = TaskStateSnapshot(
        task_key=current.task_key,
        task_name=current.task_name,
        observed_in_progress=current.observed_in_progress,
        last_status=current.last_status,
        last_progress=current.last_progress,
    )

    restarted_same_key = (
        normalized_key is not None
        and current.task_key == normalized_key
        and current.last_progress == 100
        and is_in_progress(status, task_progress)
    )
    task_changed = normalized_key is not None and current.task_key != normalized_key

    if task_changed or restarted_same_key:
        next_state.task_key = normalized_key
        next_state.task_name = task_name
        next_state.observed_in_progress = False
    elif normalized_key is not None and task_name:
        next_state.task_name = task_name

    emit_task_start = False
    can_track_this_report = normalized_key is not None and next_state.task_key == normalized_key
    if can_track_this_report and is_in_progress(status, task_progress):
        if not next_state.observed_in_progress:
            emit_task_start = True
        next_state.observed_in_progress = True

    allow_completion = (
        can_track_this_report
        and task_progress == 100
        and next_state.observed_in_progress
        and current.last_progress != 100
    )
    if allow_completion:
        next_state.observed_in_progress = False

    next_state.last_status = status
    next_state.last_progress = task_progress
    return TaskStateDecision(
        next_state=next_state,
        emit_task_start=emit_task_start,
        allow_completion=allow_completion,
    )


def get_window_bounds(
    start_date: date,
    end_date: date,
    *,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    current_time = normalize_datetime(now or datetime.now(timezone.utc))
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    if end_date == current_time.date():
        end_dt = current_time
    else:
        end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    return start_dt, end_dt


def calculate_utilization(
    initial_status: str,
    events: Iterable[StateEventSnapshot],
    *,
    start_at: datetime,
    end_at: datetime,
) -> UtilizationSummary:
    window_start = normalize_datetime(start_at)
    window_end = normalize_datetime(end_at)
    if window_end <= window_start:
        return UtilizationSummary(total_seconds=0.0, busy_seconds=0.0)

    current_status = initial_status or OFFLINE_STATUS
    cursor = window_start
    busy_seconds = 0.0

    for event in sorted(events, key=lambda item: normalize_datetime(item.occurred_at)):
        event_time = normalize_datetime(event.occurred_at)
        if event_time < window_start:
            current_status = event.status or current_status
            continue
        if event_time > window_end:
            break
        if current_status == BUSY_STATUS:
            busy_seconds += (event_time - cursor).total_seconds()
        cursor = event_time
        current_status = event.status or current_status

    if current_status == BUSY_STATUS:
        busy_seconds += (window_end - cursor).total_seconds()

    total_seconds = (window_end - window_start).total_seconds()
    return UtilizationSummary(total_seconds=total_seconds, busy_seconds=busy_seconds)
