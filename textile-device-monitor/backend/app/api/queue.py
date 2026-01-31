from fastapi import APIRouter, Depends, HTTPException, status, Query
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas import (
    QueueRecord,
    QueueCreate,
    PositionChange,
    MessageResponse,
    QueueWithLogs,
    QueueTimeoutExtend,
)
from app.crud import queue as queue_crud
from app.crud import devices as device_crud
from app.models import QueueChangeLog, DeviceStatus as ModelDeviceStatus
from app.config import settings
from app.websocket.manager import websocket_manager

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/{device_id}", response_model=QueueWithLogs)
def get_queue(device_id: int, db: Session = Depends(get_db)):
    """获取指定设备的排队列表和修改日志"""
    queue, logs = queue_crud.get_queue_by_device_with_logs(db, device_id)
    return QueueWithLogs(queue=queue, logs=logs)


@router.post("", status_code=status.HTTP_201_CREATED)
async def join_queue(queue: QueueCreate, db: Session = Depends(get_db)):
    """加入排队"""
    queue_records = queue_crud.join_queue(db, queue)

    if queue_records:
        queue_count = queue_crud.get_queue_count(db, queue.device_id)

        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": queue.device_id,
                    "action": "join",
                    "inspector_name": queue.inspector_name,
                    "position": queue_records[0].position,
                    "queue_count": queue_count,
                    "queue_records": queue_records,
                },
            }
        )

    return queue_records


@router.put("/{queue_id}/position", response_model=QueueRecord)
async def change_queue_position(
    queue_id: int, position_change: PositionChange, db: Session = Depends(get_db)
):
    """修改排队位置"""
    existing_record = queue_crud.get_queue_record(db, queue_id)
    if not existing_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
        )

    old_position = existing_record.position

    try:
        queue_record = queue_crud.update_queue_position(db, queue_id, position_change)
        if not queue_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
            )
    except ValueError as e:
        if "Concurrency conflict" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该记录已被其他用户修改，请刷新后重试",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    queue_count = queue_crud.get_queue_count(db, queue_record.device_id)
    await websocket_manager.broadcast(
        {
            "type": "queue_update",
            "data": {
                "device_id": queue_record.device_id,
                "action": "position_change",
                "queue_id": queue_id,
                "old_position": old_position,
                "new_position": position_change.new_position,
                "changed_by": queue_record.inspector_name,
                "queue_count": queue_count,
            },
        }
    )

    return queue_record


@router.delete("/{queue_id}")
async def leave_queue(
    queue_id: int,
    changed_by_id: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
):
    """离开排队"""
    queue_record = queue_crud.get_queue_record(db, queue_id)

    if not queue_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
        )

    device_id = queue_record.device_id
    success = queue_crud.delete_queue(db, queue_id, changed_by_id)

    if success:
        queue_count = queue_crud.get_queue_count(db, device_id)
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": device_id,
                    "action": "leave",
                    "queue_id": queue_id,
                    "queue_count": queue_count,
                },
            }
        )

    return {"message": "Queue record deleted successfully"}


@router.post("/{device_id}/complete", response_model=MessageResponse)
async def complete_task(device_id: int, db: Session = Depends(get_db)):
    """设备完成一单（减少排队数量）"""
    completed_record = queue_crud.complete_first_in_queue(db, device_id)

    queue_count = queue_crud.get_queue_count(db, device_id)

    if completed_record:
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": device_id,
                    "action": "complete",
                    "queue_id": completed_record.id,
                    "completed_by": completed_record.inspector_name,
                    "queue_count": queue_count,
                },
            }
        )

    return MessageResponse(
        success=True,
        message="Task completed",
        data={
            "device_id": device_id,
            "queue_count": queue_count,
        },
    )


@router.post("/{device_id}/timeout/extend", response_model=MessageResponse)
async def extend_queue_timeout(
    device_id: int, payload: QueueTimeoutExtend, db: Session = Depends(get_db)
):
    """延长排队超时计时（+5分钟）"""
    device = device_crud.get_device(db, device_id)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )

    if device.status != ModelDeviceStatus.IDLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="设备未处于空闲状态"
        )

    queue = queue_crud.get_queue_by_device(db, device_id)
    if len(queue) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="排队人数不足"
        )

    active_record = queue[0]
    now = datetime.now(timezone.utc)

    if device.queue_timeout_active_id != active_record.id:
        device.queue_timeout_active_id = active_record.id
        if device.queue_timeout_started_at is None:
            device.queue_timeout_started_at = now

    if device.queue_timeout_deadline_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有可延长的倒计时"
        )

    deadline = device.queue_timeout_deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    if now >= deadline:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="倒计时已到期，无法延长"
        )

    device.queue_timeout_deadline_at = deadline + timedelta(
        seconds=settings.QUEUE_IDLE_EXTEND_SECONDS
    )
    device.queue_timeout_extended_count = (device.queue_timeout_extended_count or 0) + 1

    changed_by = payload.changed_by.strip() if payload.changed_by else "系统"
    remark = f"设备超时被延长5分钟（操作人ID: {payload.changed_by_id or '-'}）"
    timeout_log = QueueChangeLog(
        queue_id=active_record.id,
        old_position=active_record.position,
        new_position=active_record.position,
        changed_by=changed_by,
        changed_by_id=payload.changed_by_id,
        change_type="timeout_extend",
        remark=remark,
    )
    db.add(timeout_log)
    db.commit()
    db.refresh(device)

    await websocket_manager.broadcast(
        {
            "type": "queue_timeout_update",
            "data": {
                "device_id": device.id,
                "queue_timeout_active_id": device.queue_timeout_active_id,
                "queue_timeout_started_at": device.queue_timeout_started_at,
                "queue_timeout_deadline_at": device.queue_timeout_deadline_at,
                "queue_timeout_reminded_at": device.queue_timeout_reminded_at,
                "queue_timeout_extended_count": device.queue_timeout_extended_count,
            },
        }
    )

    return MessageResponse(
        success=True,
        message="Queue timeout extended",
        data={
            "device_id": device_id,
            "queue_timeout_deadline_at": device.queue_timeout_deadline_at,
            "queue_timeout_extended_count": device.queue_timeout_extended_count,
            "queue_timeout_active_id": device.queue_timeout_active_id,
        },
    )


@router.get("/count/{device_id}")
def get_queue_count(device_id: int, db: Session = Depends(get_db)):
    """获取当前排队数量"""
    count = queue_crud.get_queue_count(db, device_id)
    return {"device_id": device_id, "queue_count": count}
