from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from app.models import QueueRecord, QueueChangeLog, TaskStatus, Device
from app.schemas import QueueCreate, PositionChange, QueueClaimRequest
from datetime import datetime, date
from sqlalchemy.exc import IntegrityError

PLACEHOLDER_NAME = "占位人员"
PLACEHOLDER_CREATE_REMARK = "系统因设备从空闲进入检测中，自动创建占位人员"
PLACEHOLDER_AUTO_REMOVE_REMARK = "占位人员已不在正在使用位置，系统自动移除"
PLACEHOLDER_DELETE_REMARK = "占位人员已被手动删除"


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


def _add_queue_log(
    db: Session,
    *,
    queue_id: int,
    old_position: Optional[int],
    new_position: int,
    changed_by: str,
    changed_by_id: Optional[str],
    change_type: Optional[str] = None,
    remark: Optional[str] = None,
) -> None:
    db.add(
        QueueChangeLog(
            queue_id=queue_id,
            old_position=old_position,
            new_position=new_position,
            changed_by=changed_by,
            changed_by_id=changed_by_id,
            change_type=change_type,
            remark=remark,
        )
    )


def join_queue(db: Session, queue: QueueCreate) -> List[QueueRecord]:
    copies = queue.copies if queue.copies else 1

    try:
        max_position = (
            db.query(func.max(QueueRecord.position))
            .filter(
                QueueRecord.device_id == queue.device_id,
                QueueRecord.status == TaskStatus.WAITING,
            )
            .scalar()
            or 0
        )

        records = []
        for i in range(copies):
            position = max_position + i + 1
            db_queue = QueueRecord(
                inspector_name=queue.inspector_name,
                device_id=queue.device_id,
                position=position,
                created_by_id=queue.created_by_id,
            )
            db.add(db_queue)
            records.append(db_queue)

        db.commit()

        for record in records:
            db.refresh(record)

        return records
    except Exception as e:
        db.rollback()
        raise e


def get_queue_record(db: Session, queue_id: int) -> Optional[QueueRecord]:
    return db.query(QueueRecord).filter(QueueRecord.id == queue_id).first()


def create_placeholder_if_missing(db: Session, device_id: int) -> Optional[QueueRecord]:
    queue = get_queue_by_device(db, device_id)
    if queue:
        return None

    try:
        placeholder = QueueRecord(
            inspector_name=PLACEHOLDER_NAME,
            device_id=device_id,
            position=1,
            created_by_id=None,
            is_placeholder=True,
            auto_remove_when_inactive=True,
        )
        db.add(placeholder)
        db.flush()

        _add_queue_log(
            db,
            queue_id=placeholder.id,
            old_position=None,
            new_position=placeholder.position,
            changed_by="系统",
            changed_by_id=None,
            change_type="placeholder_create",
            remark=PLACEHOLDER_CREATE_REMARK,
        )

        db.commit()
        db.refresh(placeholder)
        return placeholder
    except Exception as exc:
        db.rollback()
        raise exc


def cleanup_inactive_placeholders(db: Session, device_id: int) -> List[QueueRecord]:
    queue = get_queue_by_device(db, device_id)
    removed_records: List[QueueRecord] = []
    removed_at = datetime.now()

    for record in queue:
        if record.auto_remove_when_inactive and record.position != 1:
            old_position = record.position
            record.status = TaskStatus.COMPLETED
            record.completed_at = removed_at
            removed_records.append(record)
            _add_queue_log(
                db,
                queue_id=record.id,
                old_position=old_position,
                new_position=-1,
                changed_by="系统",
                changed_by_id=None,
                change_type="placeholder_auto_remove",
                remark=PLACEHOLDER_AUTO_REMOVE_REMARK,
            )

    if removed_records:
        db.flush()
        normalize_queue_positions(db, device_id)

    return removed_records


def update_queue_position(
    db: Session, queue_id: int, change: PositionChange
) -> Optional[tuple[QueueRecord, List[QueueRecord]]]:
    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    if queue.version != change.version:
        raise ValueError(
            "Concurrency conflict: record has been modified by another user"
        )

    try:
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
            return queue, []

        total = len(queue_list)
        new_position = max(1, min(change.new_position, total))
        old_position = queue.position
        if new_position == old_position:
            return queue, []

        current_index = queue_list.index(queue)
        target_index = new_position - 1

        if current_index == target_index:
            return queue, []

        queue_list.pop(current_index)
        queue_list.insert(target_index, queue)

        for i, record in enumerate(queue_list, start=1):
            record.position = i

        queue.version += 1

        _add_queue_log(
            db,
            queue_id=queue_id,
            old_position=old_position,
            new_position=new_position,
            changed_by=queue.inspector_name,
            changed_by_id=change.changed_by_id,
            change_type="position_change",
        )

        auto_removed = cleanup_inactive_placeholders(db, queue.device_id)

        db.commit()
        db.refresh(queue)
        for removed in auto_removed:
            db.refresh(removed)
        return queue, auto_removed
    except IntegrityError as e:
        db.rollback()
        raise ValueError("Database integrity error: " + str(e))
    except ValueError:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise e


def swap_first_two_in_queue(
    db: Session, device_id: int, changed_by: str, changed_by_id: Optional[str]
) -> Optional[tuple[QueueRecord, QueueRecord, List[QueueRecord]]]:
    queue = get_queue_by_device(db, device_id)
    if len(queue) < 2:
        return None

    first_record = queue[0]
    second_record = queue[1]

    old_first_position = first_record.position
    old_second_position = second_record.position

    first_record.position = old_second_position
    second_record.position = old_first_position

    first_record.version += 1
    second_record.version += 1

    remark = f"{first_record.inspector_name} 超时未使用设备，已顺延"
    _add_queue_log(
        db,
        queue_id=first_record.id,
        old_position=old_first_position,
        new_position=old_second_position,
        changed_by=changed_by,
        changed_by_id=changed_by_id,
        change_type="timeout_shift",
        remark=remark,
    )

    auto_removed = cleanup_inactive_placeholders(db, device_id)

    db.commit()
    db.refresh(first_record)
    db.refresh(second_record)
    for removed in auto_removed:
        db.refresh(removed)
    return first_record, second_record, auto_removed


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
        record.position = i

    db.flush()


def complete_first_in_queue(db: Session, device_id: int) -> Optional[QueueRecord]:
    queue = get_queue_by_device(db, device_id)
    if not queue:
        return None

    first_record = queue[0]
    old_position = first_record.position
    first_record.status = TaskStatus.COMPLETED
    first_record.completed_at = datetime.now()
    db.flush()

    completion_log = QueueChangeLog(
        queue_id=first_record.id,
        old_position=old_position,
        new_position=0,
        changed_by=first_record.inspector_name,
        changed_by_id=None,
    )
    db.add(completion_log)

    normalize_queue_positions(db, device_id)

    db.commit()
    db.refresh(first_record)
    return first_record


def claim_placeholder(
    db: Session, queue_id: int, claim: QueueClaimRequest
) -> Optional[QueueRecord]:
    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    if queue.status != TaskStatus.WAITING:
        raise ValueError("该占位记录已失效，请刷新后重试")

    if not queue.is_placeholder or not queue.auto_remove_when_inactive:
        raise ValueError("当前记录不是可认领的占位人员")

    try:
        queue.inspector_name = claim.inspector_name
        queue.created_by_id = claim.claimed_by_id
        queue.is_placeholder = False
        queue.auto_remove_when_inactive = False
        queue.version += 1

        _add_queue_log(
            db,
            queue_id=queue.id,
            old_position=queue.position,
            new_position=queue.position,
            changed_by=claim.inspector_name,
            changed_by_id=claim.claimed_by_id,
            change_type="placeholder_claim",
            remark=f"{claim.inspector_name} 认领了占位人员",
        )

        db.commit()
        db.refresh(queue)
        return queue
    except Exception as exc:
        db.rollback()
        raise exc


def delete_queue(
    db: Session, queue_id: int, changed_by_id: Optional[str] = None
) -> Optional[QueueRecord]:
    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    device_id = queue.device_id
    old_position = queue.position
    is_placeholder_delete = queue.is_placeholder and queue.auto_remove_when_inactive
    queue.status = TaskStatus.COMPLETED
    queue.completed_at = datetime.now()
    db.flush()

    _add_queue_log(
        db,
        queue_id=queue_id,
        old_position=old_position,
        new_position=-1,
        changed_by="手动删除" if is_placeholder_delete else queue.inspector_name,
        changed_by_id=changed_by_id,
        change_type="placeholder_delete" if is_placeholder_delete else "leave",
        remark=PLACEHOLDER_DELETE_REMARK if is_placeholder_delete else None,
    )

    normalize_queue_positions(db, device_id)
    db.commit()
    db.refresh(queue)

    return queue


def get_queue_count(db: Session, device_id: int) -> int:
    return (
        db.query(QueueRecord)
        .filter(
            QueueRecord.device_id == device_id, QueueRecord.status == TaskStatus.WAITING
        )
        .count()
    )


def count_user_quota(db: Session, created_by_id: str, confocal: bool) -> int:
    if not created_by_id:
        return 0

    query = (
        db.query(QueueRecord)
        .join(Device, QueueRecord.device_id == Device.id)
        .filter(
            QueueRecord.status == TaskStatus.WAITING,
            QueueRecord.created_by_id == created_by_id,
        )
    )

    device_type_expr = Device.metrics["device_type"].as_string()
    confocal_expr = device_type_expr == "laser_confocal"

    if confocal:
        query = query.filter(confocal_expr)
    else:
        query = query.filter(
            or_(
                Device.metrics.is_(None),
                device_type_expr.is_(None),
                device_type_expr != "laser_confocal",
            )
        )

    return query.count()
