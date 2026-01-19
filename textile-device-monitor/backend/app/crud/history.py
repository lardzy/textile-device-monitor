from sqlalchemy.orm import Session
from sqlalchemy import desc, and_
from typing import List, Optional, Tuple
from app.models import DeviceStatusHistory
from app.schemas import HistoryQuery
from datetime import datetime, timedelta, date
from sqlalchemy.sql import func


def create_status_history(
    db: Session,
    device_id: int,
    status: str,
    task_id: Optional[str] = None,
    task_name: Optional[str] = None,
    task_progress: Optional[int] = None,
    device_metrics: Optional[dict] = None,
    task_duration_seconds: Optional[int] = None,
) -> DeviceStatusHistory:
    history = DeviceStatusHistory(
        device_id=device_id,
        status=status,
        task_id=task_id,
        task_name=task_name,
        task_progress=task_progress,
        task_duration_seconds=task_duration_seconds,
        device_metrics=device_metrics,
    )
    db.add(history)
    db.commit()
    db.refresh(history)
    return history


def get_device_history(
    db: Session,
    device_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    status: Optional[str] = None,
    task_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
) -> Tuple[List[DeviceStatusHistory], int]:
    query = db.query(DeviceStatusHistory)

    if device_id is not None:
        query = query.filter(DeviceStatusHistory.device_id == device_id)

    if start_date:
        query = query.filter(DeviceStatusHistory.reported_at >= start_date)

    if end_date:
        query = query.filter(DeviceStatusHistory.reported_at <= end_date)

    if status:
        query = query.filter(DeviceStatusHistory.status == status)

    if task_id:
        query = query.filter(DeviceStatusHistory.task_id == task_id)

    total = query.count()
    history = (
        query.order_by(desc(DeviceStatusHistory.reported_at))
        .offset(skip)
        .limit(limit)
        .all()
    )

    return history, total


def get_latest_status(db: Session, device_id: int) -> Optional[DeviceStatusHistory]:
    return (
        db.query(DeviceStatusHistory)
        .filter(DeviceStatusHistory.device_id == device_id)
        .order_by(desc(DeviceStatusHistory.reported_at))
        .first()
    )


def get_daily_stats(db: Session, device_id: int, stat_date: date) -> dict:
    start_datetime = datetime.combine(stat_date, datetime.min.time())
    end_datetime = datetime.combine(stat_date, datetime.max.time())

    history = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= start_datetime,
            DeviceStatusHistory.reported_at <= end_datetime,
            DeviceStatusHistory.status == "busy",
        )
        .all()
    )

    total_busy_duration = len(history) * 5  # Assuming 5 seconds per report

    return {
        "date": stat_date,
        "busy_reports": len(history),
        "total_busy_duration": total_busy_duration,
    }


def get_history_by_date_range(
    db: Session, device_id: int, start_date: date, end_date: date
) -> List[DeviceStatusHistory]:
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())

    return (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= start_datetime,
            DeviceStatusHistory.reported_at <= end_datetime,
        )
        .order_by(DeviceStatusHistory.reported_at)
        .all()
    )
