import hashlib
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from app.models import QueueRecord, QueueChangeLog, TaskStatus, Device
from app.schemas import QueueCreate, PositionChange, QueueClaimRequest

PLACEHOLDER_NAME = "占位人员"
PLACEHOLDER_CREATE_REMARK = "系统因设备从空闲进入检测中，自动创建占位人员"
PLACEHOLDER_AUTO_REMOVE_REMARK = "占位人员已不在正在使用位置，系统自动移除"
PLACEHOLDER_IDLE_REMOVE_REMARK = "设备已空闲且无人排队，系统自动移除占位人员"
PLACEHOLDER_DELETE_REMARK = "占位人员已被手动删除"


class QueueVersionConflict(ValueError):
    """A stale queue mutation was rejected after the device lock was acquired."""

    def __init__(
        self,
        *,
        current_version: int,
        queue: list[dict],
        message: str = "队列已被其他用户修改，请刷新后重试",
    ) -> None:
        super().__init__(message)
        self.current_version = current_version
        self.queue = queue


def _status_value(record: QueueRecord) -> str:
    status = record.status
    return status.value if hasattr(status, "value") else str(status)


def serialize_queue_record(record: QueueRecord) -> dict:
    return {
        "id": record.id,
        "inspector_name": record.inspector_name,
        "device_id": record.device_id,
        "position": record.position,
        "submitted_at": (
            record.submitted_at.isoformat() if record.submitted_at else None
        ),
        "completed_at": (
            record.completed_at.isoformat() if record.completed_at else None
        ),
        "status": _status_value(record),
        "version": record.version,
        "created_by_id": record.created_by_id,
        "is_placeholder": record.is_placeholder,
        "auto_remove_when_inactive": record.auto_remove_when_inactive,
    }


def serialize_queue(db: Session, device_id: int) -> list[dict]:
    """Return a detached, JSON-safe final queue snapshot."""
    return [
        serialize_queue_record(record)
        for record in get_queue_by_device(db, device_id)
    ]


def lock_device_queue(db: Session, device_id: int) -> Optional[Device]:
    """Serialize every queue mutation for a device inside the caller transaction.

    PostgreSQL acquires a row-level ``SELECT FOR UPDATE`` lock on ``devices``.
    SQLite ignores row locking; the partial unique index remains the final safety
    net used by local tests.
    """
    query = db.query(Device).filter(Device.id == device_id).populate_existing()
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return query.first()


def lock_user_quota(db: Session, created_by_id: str, confocal: bool) -> None:
    """Serialize the cross-device quota check for one browser in PostgreSQL."""
    if not created_by_id or db.get_bind().dialect.name != "postgresql":
        return
    category = "confocal" if confocal else "standard"
    digest = hashlib.sha256(
        f"queue-quota:{category}:{created_by_id}".encode("utf-8")
    ).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _device_id_for_record(db: Session, queue_id: int) -> Optional[int]:
    row = (
        db.query(QueueRecord.device_id)
        .filter(QueueRecord.id == queue_id)
        .first()
    )
    return int(row[0]) if row else None


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
    if lock_device_queue(db, queue.device_id) is None:
        raise ValueError("Device not found")

    normalize_queue_positions(db, queue.device_id)
    copies = queue.copies if queue.copies else 1
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

    # CRUD helpers deliberately never commit. The device lock and all related
    # quota/queue changes must remain in the API caller's single transaction.
    db.flush()
    return records


def get_queue_record(db: Session, queue_id: int) -> Optional[QueueRecord]:
    return db.query(QueueRecord).filter(QueueRecord.id == queue_id).first()


def create_placeholder_if_missing(db: Session, device_id: int) -> Optional[QueueRecord]:
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_by_device(db, device_id)
    if queue:
        return None

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
    db.flush()
    return placeholder


def cleanup_inactive_placeholders(db: Session, device_id: int) -> List[QueueRecord]:
    queue = get_queue_by_device(db, device_id)
    removed_records: List[QueueRecord] = []
    removed_at = datetime.now(timezone.utc)

    for record in queue:
        if record.auto_remove_when_inactive and record.position != 1:
            old_position = record.position
            record.status = TaskStatus.COMPLETED
            record.completed_at = removed_at
            record.version = (record.version or 0) + 1
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


def cleanup_idle_orphan_placeholders(
    db: Session, device_id: int
) -> List[QueueRecord]:
    """Remove unclaimed placeholders when they are the device's only waiters.

    A laser-confocal device can briefly report ``busy`` while its last sample is
    being removed. That transition creates an unclaimed placeholder, but the
    following ``idle`` report proves nobody is using the device. Claimed
    placeholders and queues containing a real person are deliberately left
    untouched.
    """
    if lock_device_queue(db, device_id) is None:
        return []

    queue = get_queue_by_device(db, device_id)
    orphan_placeholders = [
        record
        for record in queue
        if record.is_placeholder and record.auto_remove_when_inactive
    ]
    if not orphan_placeholders or len(orphan_placeholders) != len(queue):
        return []

    removed_at = datetime.now(timezone.utc)
    for record in orphan_placeholders:
        old_position = record.position
        record.status = TaskStatus.COMPLETED
        record.completed_at = removed_at
        record.version = (record.version or 0) + 1
        _add_queue_log(
            db,
            queue_id=record.id,
            old_position=old_position,
            new_position=-1,
            changed_by="系统",
            changed_by_id=None,
            change_type="placeholder_auto_remove",
            remark=PLACEHOLDER_IDLE_REMOVE_REMARK,
        )

    db.flush()
    normalize_queue_positions(db, device_id)
    return orphan_placeholders


def update_queue_position(
    db: Session, queue_id: int, change: PositionChange
) -> Optional[tuple[QueueRecord, List[QueueRecord], int]]:
    device_id = _device_id_for_record(db, queue_id)
    if device_id is None:
        return None
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    if queue.version != change.version:
        raise QueueVersionConflict(
            current_version=queue.version,
            queue=serialize_queue(db, queue.device_id),
        )

    queue_list = get_queue_by_device(db, queue.device_id)
    if not queue_list:
        return None
    if queue not in queue_list:
        return None

    if change.target_queue_id is not None or change.target_version is not None:
        target = next(
            (
                record
                for record in queue_list
                if record.id == change.target_queue_id
            ),
            None,
        )
        target_is_current = (
            target is not None
            and change.target_queue_id is not None
            and change.target_version is not None
            and target.id != queue.id
            and target.position == change.new_position
            and target.version == change.target_version
        )
        if not target_is_current:
            raise QueueVersionConflict(
                current_version=max(
                    (int(record.version or 0) for record in queue_list),
                    default=int(queue.version or 0),
                ),
                queue=[serialize_queue_record(record) for record in queue_list],
                message="拖动目标已发生变化，请按最新队列重新拖动",
            )

    total = len(queue_list)
    new_position = max(1, min(change.new_position, total))
    old_position = queue.position
    if new_position == old_position:
        return queue, [], old_position

    current_index = queue_list.index(queue)
    target_index = new_position - 1

    if current_index == target_index:
        return queue, [], old_position

    queue_list.pop(current_index)
    queue_list.insert(target_index, queue)
    _apply_ordered_positions(db, queue_list)

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
    db.flush()
    return queue, auto_removed, old_position


def swap_first_two_in_queue(
    db: Session, device_id: int, changed_by: str, changed_by_id: Optional[str]
) -> Optional[tuple[QueueRecord, QueueRecord, List[QueueRecord]]]:
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_by_device(db, device_id)
    if len(queue) < 2:
        return None

    first_record = queue[0]
    second_record = queue[1]

    old_first_position = first_record.position
    old_second_position = second_record.position

    queue[0], queue[1] = second_record, first_record
    _apply_ordered_positions(db, queue)

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
    db.flush()
    return first_record, second_record, auto_removed


def _apply_ordered_positions(db: Session, queue: List[QueueRecord]) -> None:
    """Apply an ordering without transiently violating the partial unique index."""
    old_positions = {record.id: record.position for record in queue}
    changed = [
        record
        for position, record in enumerate(queue, start=1)
        if record.position != position
    ]
    if not changed:
        return

    # PostgreSQL unique indexes are checked per statement. Move all changed rows
    # to stable, per-row negative positions first, then write the final sequence.
    for record in changed:
        record.position = -(1_000_000_000 + int(record.id))
    db.flush()

    for position, record in enumerate(queue, start=1):
        record.position = position
        if old_positions[record.id] != position:
            record.version = (record.version or 0) + 1
    db.flush()


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
    _apply_ordered_positions(db, queue)


def complete_first_in_queue(db: Session, device_id: int) -> Optional[QueueRecord]:
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_by_device(db, device_id)
    if not queue:
        return None

    first_record = queue[0]
    old_position = first_record.position
    first_record.status = TaskStatus.COMPLETED
    first_record.completed_at = datetime.now(timezone.utc)
    first_record.version = (first_record.version or 0) + 1
    db.flush()

    completion_log = QueueChangeLog(
        queue_id=first_record.id,
        old_position=old_position,
        new_position=0,
        changed_by=first_record.inspector_name,
        changed_by_id=None,
        change_type="complete",
    )
    db.add(completion_log)

    normalize_queue_positions(db, device_id)
    db.flush()
    return first_record


def claim_placeholder(
    db: Session, queue_id: int, claim: QueueClaimRequest
) -> Optional[QueueRecord]:
    device_id = _device_id_for_record(db, queue_id)
    if device_id is None:
        return None
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_record(db, queue_id)
    if not queue:
        return None

    if queue.status != TaskStatus.WAITING:
        raise QueueVersionConflict(
            current_version=queue.version,
            queue=serialize_queue(db, queue.device_id),
            message="该占位记录已失效，请刷新后重试",
        )

    if not queue.is_placeholder or not queue.auto_remove_when_inactive:
        raise QueueVersionConflict(
            current_version=queue.version,
            queue=serialize_queue(db, queue.device_id),
            message="占位人员已被认领，请刷新后重试",
        )

    queue.inspector_name = claim.inspector_name
    queue.created_by_id = claim.claimed_by_id
    queue.is_placeholder = False
    queue.auto_remove_when_inactive = False
    queue.version = (queue.version or 0) + 1

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
    db.flush()
    return queue


def delete_queue(
    db: Session, queue_id: int, changed_by_id: Optional[str] = None
) -> Optional[QueueRecord]:
    device_id = _device_id_for_record(db, queue_id)
    if device_id is None:
        return None
    if lock_device_queue(db, device_id) is None:
        return None

    queue = get_queue_record(db, queue_id)
    if not queue:
        return None
    if queue.status != TaskStatus.WAITING:
        raise QueueVersionConflict(
            current_version=queue.version,
            queue=serialize_queue(db, queue.device_id),
            message="该排队记录已发生变化，请刷新后重试",
        )

    old_position = queue.position
    is_placeholder_delete = queue.is_placeholder and queue.auto_remove_when_inactive
    queue.status = TaskStatus.COMPLETED
    queue.completed_at = datetime.now(timezone.utc)
    queue.version = (queue.version or 0) + 1
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
    db.flush()
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
