from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from typing import List, Optional, Dict
from app.models import Statistic, Device, DeviceStatusHistory
from app.schemas import Statistic
from datetime import datetime, date, timedelta
from sqlalchemy.sql import func as sql_func


def create_daily_statistic(
    db: Session, device_id: int, stat_date: date, data: dict
) -> Statistic:
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
) -> List[Statistic]:
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


def get_device_realtime_stats(db: Session, device_id: int) -> Dict:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        return {}

    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())

    history = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= start_of_day,
        )
        .all()
    )

    total_busy = len([h for h in history if h.status == "busy"])
    total_reports = len(history)

    from app.crud.queue import get_queue_count

    actual_queue_count = get_queue_count(db, device_id)

    return {
        "device_id": device.id,
        "device_name": device.name,
        "status": device.status,
        "last_heartbeat": device.last_heartbeat,
        "total_reports_today": total_reports,
        "busy_reports_today": total_busy,
        "utilization_today": (total_busy / total_reports * 100)
        if total_reports > 0
        else 0,
        "queue_count": actual_queue_count,
    }


def calculate_device_stats(
    db: Session, device_id: int, start_date: date, end_date: date
) -> dict:
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())

    history = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= start_datetime,
            DeviceStatusHistory.reported_at <= end_datetime,
        )
        .all()
    )

    total_tasks = len(set([h.task_id for h in history if h.task_id]))
    completed_tasks = len([h for h in history if h.status == "idle" and h.task_id])

    durations = []
    task_start = None
    for h in history:
        if h.status == "busy" and h.task_id:
            if task_start is None or task_start != h.task_id:
                task_start = h.task_id
                task_start_time = h.reported_at
        elif h.status == "idle" and task_start:
            if h.task_id == task_start:
                duration = (h.reported_at - task_start_time).total_seconds()
                durations.append(duration)
                task_start = None

    avg_duration = sum(durations) / len(durations) if durations else 0
    max_duration = max(durations) if durations else 0
    min_duration = min(durations) if durations else 0

    total_reports = len(history)
    busy_reports = len([h for h in history if h.status == "busy"])
    utilization_rate = (busy_reports / total_reports * 100) if total_reports > 0 else 0

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "avg_duration": int(avg_duration),
        "max_duration": int(max_duration),
        "min_duration": int(min_duration),
        "utilization_rate": round(utilization_rate, 2),
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
