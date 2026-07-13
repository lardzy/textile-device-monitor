from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.crud import device_tracking as tracking_crud
from app.crud.queue import get_queue_count
from app.models import Device, DeviceStatusHistory, Statistic
from app.schemas import Statistic as StatisticSchema
from app.services.device_tracking import (
    BUSY_STATUS,
    EVENT_TASK_COMPLETE,
    EVENT_TASK_START,
    OFFLINE_STATUS,
    StateEventSnapshot,
    calculate_utilization,
    get_window_bounds,
)


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


def get_realtime_stats(db: Session) -> Dict:
    total_devices = db.query(Device).count()
    online_devices = db.query(Device).filter(Device.status != "offline").count()
    busy_devices = db.query(Device).filter(Device.status == "busy").count()

    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())
    end_of_day = datetime.combine(today, datetime.max.time())

    today_reports = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.reported_at >= start_of_day,
            DeviceStatusHistory.reported_at <= end_of_day,
        )
        .count()
    )

    return {
        "total_devices": total_devices,
        "online_devices": online_devices,
        "idle_devices": online_devices - busy_devices,
        "busy_devices": busy_devices,
        "offline_devices": total_devices - online_devices,
        "today_reports": today_reports,
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
    initial_event = tracking_crud.get_latest_state_event_before(
        db,
        device_id=device_id,
        before=start_at,
    )
    events = tracking_crud.get_state_events_in_range(
        db,
        device_id=device_id,
        start_at=start_at,
        end_at=end_at,
    )
    initial_status = initial_event.status if initial_event else OFFLINE_STATUS
    utilization = calculate_utilization(
        initial_status,
        _build_state_snapshots(events),
        start_at=start_at,
        end_at=end_at,
    )

    completed_tasks = tracking_crud.count_state_events(
        db,
        device_id=device_id,
        event_type=EVENT_TASK_COMPLETE,
        start_at=start_at,
        end_at=end_at,
    )
    total_tasks = tracking_crud.count_state_events(
        db,
        device_id=device_id,
        event_type=EVENT_TASK_START,
        start_at=start_at,
        end_at=end_at,
    )

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "busy_seconds": int(utilization.busy_seconds),
        "total_seconds": int(utilization.total_seconds),
        "utilization_rate": round(utilization.utilization_rate, 2),
        "event_count": len(events),
        "busy_event_count": len([event for event in events if event.status == BUSY_STATUS]),
    }


def get_device_realtime_stats(db: Session, device_id: int) -> Dict:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        return {}

    today = date.today()
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
            DeviceStatusHistory.reported_at >= start_at,
            DeviceStatusHistory.reported_at <= end_at,
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
