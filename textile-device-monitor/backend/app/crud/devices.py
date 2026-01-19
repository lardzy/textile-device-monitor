from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from app.models import (
    Device,
    DeviceStatus,
    DeviceStatusHistory,
    QueueRecord,
    QueueChangeLog,
    Statistic,
)

# SQLAlchemy assignments are runtime-correct; static type checkers may warn.
from app.schemas import DeviceCreate, DeviceUpdate
from datetime import datetime, timezone


def get_device(db: Session, device_id: int) -> Optional[Device]:
    return db.query(Device).filter(Device.id == device_id).first()


def get_device_by_code(db: Session, device_code: str) -> Optional[Device]:
    return db.query(Device).filter(Device.device_code == device_code).first()


def get_devices(db: Session, skip: int = 0, limit: int = 100) -> List[Device]:
    return db.query(Device).offset(skip).limit(limit).all()


def create_device(db: Session, device: DeviceCreate) -> Device:
    db_device = Device(**device.dict())
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    return db_device


def update_device(
    db: Session, device_id: int, device_update: DeviceUpdate
) -> Optional[Device]:
    db_device = get_device(db, device_id)
    if db_device:
        update_data = device_update.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_device, field, value)
        db_device.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(db_device)
    return db_device


def delete_device(db: Session, device_id: int) -> bool:
    db_device = get_device(db, device_id)
    if not db_device:
        return False

    db.query(QueueChangeLog).filter(
        QueueChangeLog.queue_id.in_(
            db.query(QueueRecord.id).filter(QueueRecord.device_id == device_id)
        )
    ).delete(synchronize_session=False)
    db.query(QueueRecord).filter(QueueRecord.device_id == device_id).delete(
        synchronize_session=False
    )
    db.query(DeviceStatusHistory).filter(
        DeviceStatusHistory.device_id == device_id
    ).delete(synchronize_session=False)
    db.query(Statistic).filter(Statistic.device_id == device_id).delete(
        synchronize_session=False
    )

    db.delete(db_device)
    db.commit()
    return True


def update_device_status(
    db: Session,
    device: Device,
    status: DeviceStatus,
    task_id: Optional[str] = None,
    task_name: Optional[str] = None,
    task_progress: Optional[int] = None,
    metrics: Optional[dict] = None,
) -> Device:
    device.status = status
    device.task_id = task_id
    device.task_name = task_name
    device.task_progress = task_progress
    device.metrics = metrics
    device.last_heartbeat = datetime.now(timezone.utc)
    db.commit()
    db.refresh(device)
    return device


def update_device_heartbeat(db: Session, device: Device) -> Device:
    device.last_heartbeat = datetime.now(timezone.utc)
    db.commit()
    db.refresh(device)
    return device


def get_online_devices(db: Session) -> List[Device]:
    return db.query(Device).filter(Device.status != DeviceStatus.OFFLINE).all()


def get_device_stats(
    db: Session, device_id: int, start_date: datetime, end_date: datetime
):
    from app.models import DeviceStatusHistory

    history = (
        db.query(DeviceStatusHistory)
        .filter(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.reported_at >= start_date,
            DeviceStatusHistory.reported_at <= end_date,
        )
        .all()
    )

    total = len(history)
    busy_count = len([h for h in history if h.status == "busy"])

    return {
        "total_reports": total,
        "busy_reports": busy_count,
        "utilization": (busy_count / total * 100) if total > 0 else 0,
    }
