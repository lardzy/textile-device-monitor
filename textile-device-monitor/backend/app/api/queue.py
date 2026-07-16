from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import JSONResponse
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas import (
    QueueRecord,
    QueueCreate,
    PositionChange,
    QueueClaimRequest,
    MessageResponse,
    QueueWithLogs,
    QueueTimeoutExtend,
)
from app.crud import queue as queue_crud
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
    if not queue.created_by_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少浏览器ID，无法加入排队",
        )

    # The device row is the queue-wide mutex. Quota checks, position selection,
    # insertion and the final snapshot all belong to this one transaction.
    device = queue_crud.lock_device_queue(db, queue.device_id)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )

    device_metrics = device.metrics or {}
    is_confocal = (
        isinstance(device_metrics, dict)
        and device_metrics.get("device_type") == "laser_confocal"
    )
    limit = 2 if is_confocal else 3
    queue_crud.lock_user_quota(db, queue.created_by_id, is_confocal)
    used = queue_crud.count_user_quota(db, queue.created_by_id, is_confocal)
    remaining = limit - used

    if remaining <= 0:
        detail = (
            "已达排队上限（共聚焦设备最多2个）"
            if is_confocal
            else "已达排队上限（非共聚焦设备最多3个）"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    copies = queue.copies or 1
    effective_copies = min(copies, remaining)
    queue.copies = effective_copies

    try:
        queue_records = queue_crud.join_queue(db, queue)
        queue_count = queue_crud.get_queue_count(db, queue.device_id)
        created_records = [
            queue_crud.serialize_queue_record(record) for record in queue_records
        ]
        db.commit()
    except Exception:
        db.rollback()
        raise

    if queue_records:
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": queue.device_id,
                    "action": "join",
                    "inspector_name": queue.inspector_name,
                    "position": queue_records[0].position,
                    "queue_count": queue_count,
                    "queue_records": created_records,
                },
            }
        )

    return created_records


@router.put("/{queue_id}/position", response_model=QueueRecord)
async def change_queue_position(
    queue_id: int, position_change: PositionChange, db: Session = Depends(get_db)
):
    """修改排队位置"""
    try:
        queue_result = queue_crud.update_queue_position(db, queue_id, position_change)
        if not queue_result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
            )
        queue_record, auto_removed, old_position = queue_result
        queue_count = queue_crud.get_queue_count(db, queue_record.device_id)
        db.commit()
    except queue_crud.QueueVersionConflict as exc:
        payload = {
            "code": "queue_version_conflict",
            "message": str(exc),
            "current_version": exc.current_version,
            "queue": exc.queue,
        }
        db.rollback()
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=payload)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except Exception:
        db.rollback()
        raise

    action = "placeholder_auto_remove" if auto_removed else "position_change"
    await websocket_manager.broadcast(
        {
            "type": "queue_update",
            "data": {
                "device_id": queue_record.device_id,
                "action": action,
                "queue_id": queue_id,
                "old_position": old_position,
                "new_position": position_change.new_position,
                "changed_by": queue_record.inspector_name,
                "queue_count": queue_count,
                "auto_removed_queue_ids": [record.id for record in auto_removed],
            },
        }
    )

    return queue_record


@router.post("/{queue_id}/claim", response_model=QueueRecord)
async def claim_placeholder(
    queue_id: int, payload: QueueClaimRequest, db: Session = Depends(get_db)
):
    """认领占位人员"""
    try:
        queue_record = queue_crud.claim_placeholder(db, queue_id, payload)
        if queue_record:
            queue_count = queue_crud.get_queue_count(db, queue_record.device_id)
        db.commit()
    except queue_crud.QueueVersionConflict as exc:
        payload = {
            "code": "queue_version_conflict",
            "message": str(exc),
            "current_version": exc.current_version,
            "queue": exc.queue,
        }
        db.rollback()
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=payload)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception:
        db.rollback()
        raise

    if not queue_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
        )

    await websocket_manager.broadcast(
        {
            "type": "queue_update",
            "data": {
                "device_id": queue_record.device_id,
                "action": "placeholder_claim",
                "queue_id": queue_record.id,
                "inspector_name": queue_record.inspector_name,
                "claimed_by_id": queue_record.created_by_id,
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
    try:
        deleted_record = queue_crud.delete_queue(db, queue_id, changed_by_id)
        if not deleted_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Queue record not found",
            )
        device_id = deleted_record.device_id
        queue_count = queue_crud.get_queue_count(db, device_id)
        db.commit()
    except queue_crud.QueueVersionConflict as exc:
        payload = {
            "code": "queue_version_conflict",
            "message": str(exc),
            "current_version": exc.current_version,
            "queue": exc.queue,
        }
        db.rollback()
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=payload)
    except Exception:
        db.rollback()
        raise

    action = (
        "placeholder_delete"
        if deleted_record.is_placeholder and deleted_record.auto_remove_when_inactive
        else "leave"
    )
    await websocket_manager.broadcast(
        {
            "type": "queue_update",
            "data": {
                "device_id": device_id,
                "action": action,
                "queue_id": queue_id,
                "queue_count": queue_count,
            },
        }
    )

    return {"message": "Queue record deleted successfully"}


@router.post("/{device_id}/complete", response_model=MessageResponse)
async def complete_task(device_id: int, db: Session = Depends(get_db)):
    """Retired: queue settlement must be driven by an idempotent status report."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "queue_completion_endpoint_retired",
            "message": (
                "该接口已停用；请通过设备状态上报并携带 report_id 完成队首结算"
            ),
            "device_id": device_id,
        },
    )


@router.post("/{device_id}/timeout/extend", response_model=MessageResponse)
async def extend_queue_timeout(
    device_id: int, payload: QueueTimeoutExtend, db: Session = Depends(get_db)
):
    """延长排队超时计时（+5分钟）"""
    device = queue_crud.lock_device_queue(db, device_id)
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

    current_extended_count = device.queue_timeout_extended_count or 0
    if current_extended_count >= 3:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="延长次数已达上限（最多3次）"
        )

    device.queue_timeout_deadline_at = deadline + timedelta(
        seconds=settings.QUEUE_IDLE_EXTEND_SECONDS
    )
    device.queue_timeout_extended_count = current_extended_count + 1

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
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

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
