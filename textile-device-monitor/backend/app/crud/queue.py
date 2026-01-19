from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from typing import List, Optional
from app.models import QueueRecord, QueueChangeLog, TaskStatus
from app.schemas import QueueCreate, PositionChange
from datetime import datetime, date


def get_queue_by_device(db: Session, device_id: int) -> List[QueueRecord]:
    return (
        db.query(QueueRecord)
        .filter(
            QueueRecord.device_id == device_id, QueueRecord.status == TaskStatus.WAITING
        )
        .order_by(QueueRecord.position, QueueRecord.submitted_at, QueueRecord.id)
        .all()
    )


def get_queue_by_device_with_logs(db: Session, device_id: int) -> tuple:
    queue = get_queue_by_device(db, device_id)

    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())

    logs = (
        db.query(QueueChangeLog)
        .join(QueueRecord, QueueChangeLog.queue_id == QueueRecord.id)
        .filter(
            QueueRecord.device_id == device_id,
            QueueChangeLog.change_time >= start_of_day,
        )
        .order_by(QueueChangeLog.change_time.desc())
        .limit(50)
        .all()
    )

    return queue, logs


def join_queue(db: Session, queue: QueueCreate) -> QueueRecord:
    max_position = (
        db.query(func.max(QueueRecord.position))
        .filter(
            QueueRecord.device_id == queue.device_id,
            QueueRecord.status == TaskStatus.WAITING,
        )
        .scalar()
        or 0
    )

    db_queue = QueueRecord(
        inspector_name=queue.inspector_name,
        device_id=queue.device_id,
        position=max_position + 1,
    )
    db.add(db_queue)
    db.commit()
    db.refresh(db_queue)
    return db_queue


def get_queue_record(db: Session, queue_id: int) -> Optional[QueueRecord]:
    return db.query(QueueRecord).filter(QueueRecord.id == queue_id).first()


def update_queue_position(
    db: Session, queue_id: int, change: PositionChange
) -> Optional[QueueRecord]:
    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    queue_list = (
        db.query(QueueRecord)
        .filter(
            QueueRecord.device_id == queue.device_id,
            QueueRecord.status == TaskStatus.WAITING,
        )
        .order_by(QueueRecord.position, QueueRecord.submitted_at, QueueRecord.id)
        .all()
    )
    if not queue_list:
        return queue

    total = len(queue_list)
    new_position = max(1, min(change.new_position, total))
    old_position = queue.position
    if new_position == old_position:
        return queue

    target = next((item for item in queue_list if item.position == new_position), None)
    if target and target.id != queue.id:
        (
            db.query(QueueRecord)
            .filter(QueueRecord.id == target.id)
            .update({QueueRecord.position: old_position}, synchronize_session="fetch")
        )

    (
        db.query(QueueRecord)
        .filter(QueueRecord.id == queue.id)
        .update({QueueRecord.position: new_position}, synchronize_session="fetch")
    )

    log = QueueChangeLog(
        queue_id=queue_id,
        old_position=old_position,
        new_position=new_position,
        changed_by=queue.inspector_name,
    )
    db.add(log)

    db.commit()
    db.refresh(queue)
    return queue


def normalize_queue_positions(db: Session, device_id: int) -> None:
    queue = (
        db.query(QueueRecord)
        .filter(
            QueueRecord.device_id == device_id,
            QueueRecord.status == TaskStatus.WAITING,
        )
        .order_by(QueueRecord.position, QueueRecord.submitted_at, QueueRecord.id)
        .all()
    )

    for i, record in enumerate(queue, start=1):
        if record.position != i:
            record.position = i

    db.flush()


def complete_first_in_queue(db: Session, device_id: int) -> Optional[QueueRecord]:
    queue = get_queue_by_device(db, device_id)
    if not queue:
        return None

    first_record = queue[0]
    first_record.status = TaskStatus.COMPLETED
    first_record.completed_at = datetime.now()
    db.flush()

    normalize_queue_positions(db, device_id)

    db.commit()
    db.refresh(first_record)
    return first_record


def delete_queue(db: Session, queue_id: int) -> bool:
    queue = get_queue_record(db, queue_id)
    if not queue:
        return False

    device_id = queue.device_id

    db.query(QueueChangeLog).filter(QueueChangeLog.queue_id == queue_id).delete()
    db.delete(queue)

    normalize_queue_positions(db, device_id)

    db.commit()
    return True


def get_queue_count(db: Session, device_id: int) -> int:
    return (
        db.query(QueueRecord)
        .filter(
            QueueRecord.device_id == device_id, QueueRecord.status == TaskStatus.WAITING
        )
        .count()
    )
