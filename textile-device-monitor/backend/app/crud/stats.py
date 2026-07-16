from __future__ import annotations

from bisect import bisect_right
from collections import deque
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.crud import device_tracking as tracking_crud
from app.crud.queue import get_queue_count
from app.models import Device, DeviceStateEvent, DeviceStatusHistory, Statistic
from app.schemas import Statistic as StatisticSchema
from app.services.device_tracking import (
    BUSY_STATUS,
    EVENT_TASK_COMPLETE,
    EVENT_TASK_START,
    OFFLINE_STATUS,
    StateEventSnapshot,
    calculate_utilization,
    get_stats_timezone,
    get_window_bounds,
    normalize_datetime,
)


VALID_STAT_TYPES = frozenset({"daily", "weekly", "monthly"})


def _task_event_identity(event: DeviceStateEvent) -> str:
    """Return a stable identity for pairing starts and completions in one window."""
    task_key = str(event.task_key or "").strip()
    if task_key:
        return f"key:{task_key}"
    task_name = str(event.task_name or "").strip()
    if task_name:
        return f"name:{task_name}"
    return "unkeyed"


def calculate_completion_cohort(events: list[DeviceStateEvent]) -> tuple[int, int]:
    """Count tasks started in the window and those completed in the same window.

    Completion events whose start happened before the window remain part of the
    completion-volume metric, but do not distort the completion-rate cohort.
    """
    pending_starts: dict[str, int] = {}
    started_tasks = 0
    completed_started_tasks = 0
    ordered_events = sorted(
        events,
        key=lambda event: (normalize_datetime(event.occurred_at), event.id or 0),
    )
    for event in ordered_events:
        identity = _task_event_identity(event)
        if event.event_type == EVENT_TASK_START:
            started_tasks += 1
            pending_starts[identity] = pending_starts.get(identity, 0) + 1
            continue
        if event.event_type != EVENT_TASK_COMPLETE:
            continue
        pending_count = pending_starts.get(identity, 0)
        if pending_count <= 0:
            continue
        completed_started_tasks += 1
        if pending_count == 1:
            pending_starts.pop(identity, None)
        else:
            pending_starts[identity] = pending_count - 1
    return started_tasks, completed_started_tasks


def calculate_completion_cohort_buckets(
    events_by_device: dict[int, list[DeviceStateEvent]],
    periods: list[tuple[datetime, datetime]],
) -> list[tuple[int, int]]:
    """Pair across the whole range and attribute completion to the start bucket."""
    if not periods:
        return []

    period_starts = [normalize_datetime(period[0]) for period in periods]
    period_ends = [normalize_datetime(period[1]) for period in periods]
    started = [0 for _ in periods]
    completed = [0 for _ in periods]

    for device_events in events_by_device.values():
        pending_starts: dict[str, deque[int]] = {}
        for event in sorted(
            device_events,
            key=lambda item: (normalize_datetime(item.occurred_at), item.id or 0),
        ):
            occurred_at = normalize_datetime(event.occurred_at)
            period_index = bisect_right(period_starts, occurred_at) - 1
            if (
                period_index < 0
                or period_index >= len(periods)
                or occurred_at >= period_ends[period_index]
            ):
                continue

            identity = _task_event_identity(event)
            if event.event_type == EVENT_TASK_START:
                started[period_index] += 1
                pending_starts.setdefault(identity, deque()).append(period_index)
                continue
            if event.event_type != EVENT_TASK_COMPLETE:
                continue
            pending = pending_starts.get(identity)
            if not pending:
                continue
            start_period_index = pending.popleft()
            completed[start_period_index] += 1
            if not pending:
                pending_starts.pop(identity, None)

    return list(zip(started, completed))


def create_daily_statistic(
    db: Session, device_id: int, stat_date: date, data: dict
) -> StatisticSchema:
    stat = Statistic(
        device_id=device_id, stat_date=stat_date, stat_type="daily", **data
    )
    db.add(stat)
    db.commit()
    db.refresh(stat)
    return stat


def get_statistics(
    db: Session,
    device_id: Optional[int],
    stat_type: str,
    start_date: date,
    end_date: date,
) -> List[StatisticSchema]:
    query = db.query(Statistic).filter(
        Statistic.stat_type == stat_type,
        Statistic.stat_date >= start_date,
        Statistic.stat_date <= end_date,
    )

    if device_id:
        query = query.filter(Statistic.device_id == device_id)

    return query.order_by(Statistic.stat_date).all()


def get_realtime_stats(db: Session, *, now: Optional[datetime] = None) -> Dict:
    total_devices = db.query(Device).count()
    online_devices = db.query(Device).filter(Device.status != "offline").count()
    idle_devices = db.query(Device).filter(Device.status == "idle").count()
    busy_devices = db.query(Device).filter(Device.status == "busy").count()
    maintenance_devices = (
        db.query(Device).filter(Device.status == "maintenance").count()
    )
    error_devices = db.query(Device).filter(Device.status == "error").count()

    # “今日”必须按统计业务时区解释，再转换成 UTC 查询数据库。
    # 直接使用宿主机的 date.today() 会在服务器时区与 STATS_TIMEZONE
    # 不一致时跨错自然日（尤其是上海零点附近）。
    stats_tz = get_stats_timezone()
    current_time = now or datetime.now(stats_tz)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=stats_tz)
    else:
        current_time = current_time.astimezone(stats_tz)
    today = current_time.date()
    start_of_day, end_of_day = get_window_bounds(today, today, now=current_time)
    normalized_start = normalize_datetime(start_of_day)
    normalized_end = normalize_datetime(end_of_day)

    today_reports = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.reported_at >= normalized_start,
            DeviceStatusHistory.reported_at <= normalized_end,
        )
        .count()
    )

    return {
        "total_devices": total_devices,
        "online_devices": online_devices,
        "idle_devices": idle_devices,
        "busy_devices": busy_devices,
        "maintenance_devices": maintenance_devices,
        "error_devices": error_devices,
        "offline_devices": total_devices - online_devices,
        "today_reports": today_reports,
        "today_completed_tasks": today_reports,
    }


def _build_state_snapshots(events: list) -> list[StateEventSnapshot]:
    return [
        StateEventSnapshot(
            occurred_at=event.occurred_at,
            status=event.status,
            event_type=event.event_type,
        )
        for event in events
    ]


def _build_stats_payload(
    db: Session,
    *,
    device_id: int,
    start_at: datetime,
    end_at: datetime,
) -> dict:
    normalized_start_at = normalize_datetime(start_at)
    normalized_end_at = normalize_datetime(end_at)
    initial_event = tracking_crud.get_latest_state_event_before(
        db,
        device_id=device_id,
        before=normalized_start_at,
    )
    events = tracking_crud.get_state_events_in_range(
        db,
        device_id=device_id,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
    )
    initial_status = initial_event.status if initial_event else OFFLINE_STATUS
    utilization = calculate_utilization(
        initial_status,
        _build_state_snapshots(events),
        start_at=start_at,
        end_at=end_at,
    )

    total_tasks = sum(1 for event in events if event.event_type == EVENT_TASK_START)
    completed_tasks = sum(
        1 for event in events if event.event_type == EVENT_TASK_COMPLETE
    )
    cohort_started_tasks, cohort_completed_tasks = calculate_completion_cohort(events)
    completion_rate = (
        (cohort_completed_tasks / cohort_started_tasks) * 100
        if cohort_started_tasks > 0
        else 0.0
    )

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "cohort_started_tasks": cohort_started_tasks,
        "cohort_completed_tasks": cohort_completed_tasks,
        "completion_rate": round(completion_rate, 2),
        "busy_seconds": int(utilization.busy_seconds),
        "total_seconds": int(utilization.total_seconds),
        "utilization_rate": round(utilization.utilization_rate, 2),
        "event_count": len(events),
        "busy_event_count": len(
            [event for event in events if event.status == BUSY_STATUS]
        ),
    }


def get_device_realtime_stats(db: Session, device_id: int) -> Dict:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        return {}

    today = datetime.now(get_stats_timezone()).date()
    start_at, end_at = get_window_bounds(today, today)
    stats_payload = _build_stats_payload(
        db,
        device_id=device_id,
        start_at=start_at,
        end_at=end_at,
    )

    actual_queue_count = get_queue_count(db, device_id)

    return {
        "device_id": device.id,
        "device_name": device.name,
        "status": device.status,
        "last_heartbeat": device.last_heartbeat,
        "total_reports_today": stats_payload["event_count"],
        "busy_reports_today": stats_payload["busy_event_count"],
        "utilization_today": stats_payload["utilization_rate"],
        "queue_count": actual_queue_count,
    }


def calculate_device_stats(
    db: Session, device_id: int, start_date: date, end_date: date
) -> dict:
    start_at, end_at = get_window_bounds(start_date, end_date)
    normalized_start_at = normalize_datetime(start_at)
    normalized_end_at = normalize_datetime(end_at)
    stats_payload = _build_stats_payload(
        db,
        device_id=device_id,
        start_at=start_at,
        end_at=end_at,
    )

    history = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= normalized_start_at,
            DeviceStatusHistory.reported_at <= normalized_end_at,
            DeviceStatusHistory.task_duration_seconds.isnot(None),
        )
        .all()
    )
    durations = [
        int(item.task_duration_seconds)
        for item in history
        if item.task_duration_seconds is not None
    ]

    avg_duration = sum(durations) / len(durations) if durations else 0
    max_duration = max(durations) if durations else 0
    min_duration = min(durations) if durations else 0

    return {
        "total_tasks": stats_payload["total_tasks"],
        "completed_tasks": stats_payload["completed_tasks"],
        "cohort_started_tasks": stats_payload["cohort_started_tasks"],
        "cohort_completed_tasks": stats_payload["cohort_completed_tasks"],
        "completion_rate": stats_payload["completion_rate"],
        "avg_duration": int(avg_duration),
        "max_duration": int(max_duration),
        "min_duration": int(min_duration),
        "utilization_rate": stats_payload["utilization_rate"],
    }


def get_summary_stats(
    db: Session, stat_type: str, start_date: date, end_date: date
) -> List[dict]:
    devices = db.query(Device).all()
    summary = []

    for device in devices:
        stats = calculate_device_stats(db, device.id, start_date, end_date)
        summary.append({"device_id": device.id, "device_name": device.name, **stats})

    return summary


def _normalize_trend_now(now: Optional[datetime], stats_tz) -> datetime:
    if now is None:
        return datetime.now(stats_tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=stats_tz)
    return now.astimezone(stats_tz)


def _floor_period_start(value: datetime, stat_type: str) -> datetime:
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if stat_type == "weekly":
        return start - timedelta(days=start.weekday())
    if stat_type == "monthly":
        return start.replace(day=1)
    return start


def _next_period_start(value: datetime, stat_type: str) -> datetime:
    if stat_type == "daily":
        return value + timedelta(days=1)
    if stat_type == "weekly":
        return value + timedelta(days=7)
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def _build_trend_periods(
    stat_type: str,
    start_date: date,
    end_date: date,
    *,
    now: datetime,
    stats_tz,
) -> list[tuple[datetime, datetime]]:
    query_start = datetime.combine(start_date, time.min, tzinfo=stats_tz)
    requested_end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=stats_tz,
    )
    query_end = min(requested_end, now)
    if query_end <= query_start:
        return []

    periods: list[tuple[datetime, datetime]] = []
    natural_start = _floor_period_start(query_start, stat_type)
    while natural_start < query_end:
        natural_end = _next_period_start(natural_start, stat_type)
        period_start = max(natural_start, query_start)
        period_end = min(natural_end, query_end)
        if period_end > period_start:
            periods.append((period_start, period_end))
        natural_start = natural_end
    return periods


def get_trend_stats(
    db: Session,
    *,
    stat_type: str,
    start_date: date,
    end_date: date,
    device_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """按业务时区的自然日、周或月实时聚合设备事件。"""
    stats_tz = get_stats_timezone()
    current_time = _normalize_trend_now(now, stats_tz)
    periods = _build_trend_periods(
        stat_type,
        start_date,
        end_date,
        now=current_time,
        stats_tz=stats_tz,
    )

    device_query = db.query(Device)
    if device_id is not None:
        device_query = device_query.filter(Device.id == device_id)
    devices = device_query.order_by(Device.id.asc()).all()

    response = {
        "stat_type": stat_type,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "device_id": device_id,
        "timezone": getattr(stats_tz, "key", str(stats_tz)),
        "items": [],
    }
    if not periods:
        return response

    first_start = periods[0][0]
    final_end = periods[-1][1]
    first_start_utc = normalize_datetime(first_start)
    final_end_utc = normalize_datetime(final_end)
    device_ids = [device.id for device in devices]

    events_by_device: dict[int, list[DeviceStateEvent]] = {
        current_device_id: [] for current_device_id in device_ids
    }
    duration_records: list[DeviceStatusHistory] = []
    if device_ids:
        events = (
            db.query(DeviceStateEvent)
            .filter(
                DeviceStateEvent.device_id.in_(device_ids),
                DeviceStateEvent.occurred_at >= first_start_utc,
                DeviceStateEvent.occurred_at < final_end_utc,
            )
            .order_by(
                DeviceStateEvent.device_id.asc(),
                DeviceStateEvent.occurred_at.asc(),
                DeviceStateEvent.id.asc(),
            )
            .all()
        )
        for event in events:
            events_by_device[event.device_id].append(event)

        duration_records = (
            db.query(DeviceStatusHistory)
            .filter(
                DeviceStatusHistory.device_id.in_(device_ids),
                DeviceStatusHistory.reported_at >= first_start_utc,
                DeviceStatusHistory.reported_at < final_end_utc,
                DeviceStatusHistory.task_duration_seconds.isnot(None),
            )
            .order_by(
                DeviceStatusHistory.reported_at.asc(),
                DeviceStatusHistory.id.asc(),
            )
            .all()
        )

    current_status_by_device: dict[int, str] = {}
    event_index_by_device: dict[int, int] = {}
    for current_device_id in device_ids:
        initial_event = tracking_crud.get_latest_state_event_before(
            db,
            device_id=current_device_id,
            before=first_start_utc,
        )
        current_status_by_device[current_device_id] = (
            initial_event.status if initial_event else OFFLINE_STATUS
        )
        event_index_by_device[current_device_id] = 0

    cohort_buckets = calculate_completion_cohort_buckets(
        events_by_device,
        periods,
    )
    items: list[dict] = []
    duration_index = 0
    for period_index, (period_start, period_end) in enumerate(periods):
        bucket_start = _floor_period_start(period_start, stat_type)
        bucket_end = _next_period_start(bucket_start, stat_type)
        busy_seconds = 0.0
        total_seconds = 0.0
        total_tasks = 0
        completed_tasks = 0
        cohort_started_tasks, cohort_completed_tasks = cohort_buckets[period_index]
        normalized_period_end = normalize_datetime(period_end)
        period_durations: list[int] = []

        while duration_index < len(duration_records):
            duration_record = duration_records[duration_index]
            if normalize_datetime(duration_record.reported_at) >= normalized_period_end:
                break
            period_durations.append(int(duration_record.task_duration_seconds))
            duration_index += 1

        for current_device_id in device_ids:
            device_events = events_by_device[current_device_id]
            event_index = event_index_by_device[current_device_id]
            period_events: list[DeviceStateEvent] = []
            while event_index < len(device_events):
                event = device_events[event_index]
                if normalize_datetime(event.occurred_at) >= normalized_period_end:
                    break
                period_events.append(event)
                event_index += 1

            utilization = calculate_utilization(
                current_status_by_device[current_device_id],
                _build_state_snapshots(period_events),
                start_at=period_start,
                end_at=period_end,
            )
            busy_seconds += utilization.busy_seconds
            total_seconds += utilization.total_seconds
            total_tasks += sum(
                1 for event in period_events if event.event_type == EVENT_TASK_START
            )
            completed_tasks += sum(
                1 for event in period_events if event.event_type == EVENT_TASK_COMPLETE
            )
            if period_events:
                current_status_by_device[current_device_id] = period_events[-1].status
            event_index_by_device[current_device_id] = event_index

        utilization_rate = (
            (busy_seconds / total_seconds) * 100 if total_seconds > 0 else 0.0
        )
        completion_rate = (
            (cohort_completed_tasks / cohort_started_tasks) * 100
            if cohort_started_tasks > 0
            else 0.0
        )
        items.append(
            {
                "bucket_start": bucket_start.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "cohort_started_tasks": cohort_started_tasks,
                "cohort_completed_tasks": cohort_completed_tasks,
                "completion_rate": round(completion_rate, 2),
                "avg_duration_seconds": int(
                    sum(period_durations) / len(period_durations)
                )
                if period_durations
                else 0,
                "busy_seconds": int(busy_seconds),
                "total_seconds": int(total_seconds),
                "utilization_rate": round(utilization_rate, 2),
            }
        )

    response["items"] = items
    return response
