from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timezone
from app.database import get_db
from app.schemas import (
    Device,
    DeviceCreate,
    DeviceUpdate,
    MessageResponse,
    StatusReport,
)

# ORM fields are runtime values; type checkers may warn on annotations.
# noqa: E501
from app.crud import devices as device_crud
from app.websocket.manager import websocket_manager
from app.models import DeviceStatus as ModelDeviceStatus

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=List[Device])
def get_devices(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """获取所有设备列表"""
    return device_crud.get_devices(db, skip=skip, limit=limit)


@router.get("/{device_id}", response_model=Device)
def get_device(device_id: int, db: Session = Depends(get_db)):
    """获取单个设备详情"""
    device = device_crud.get_device(db, device_id)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )
    return device


@router.post("", response_model=Device, status_code=status.HTTP_201_CREATED)
async def create_device(device: DeviceCreate, db: Session = Depends(get_db)):
    """创建新设备"""
    existing = device_crud.get_device_by_code(db, device.device_code)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Device code already exists"
        )
    db_device = device_crud.create_device(db, device)

    await websocket_manager.broadcast(
        {
            "type": "device_list_update",
            "data": {
                "action": "create",
                "device": db_device,
            },
        }
    )

    return db_device


@router.put("/{device_id}", response_model=Device)
async def update_device(
    device_id: int, device_update: DeviceUpdate, db: Session = Depends(get_db)
):
    """更新设备信息"""
    device = device_crud.update_device(db, device_id, device_update)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )

    await websocket_manager.broadcast(
        {
            "type": "device_list_update",
            "data": {
                "action": "update",
                "device": device,
            },
        }
    )

    return device


@router.delete("/{device_id}")
async def delete_device(device_id: int, db: Session = Depends(get_db)):
    """删除设备"""
    device = device_crud.get_device(db, device_id)
    success = device_crud.delete_device(db, device_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )
    if device:
        await websocket_manager.broadcast(
            {
                "type": "device_list_update",
                "data": {
                    "action": "delete",
                    "device_id": device.id,
                },
            }
        )
    return {"message": "Device deleted successfully"}


@router.post("/{device_code}/status", response_model=MessageResponse)
async def report_device_status(
    device_code: str, status_report: StatusReport, db: Session = Depends(get_db)
):
    """设备状态上报接口（供外部设备程序调用）"""
    device = device_crud.get_device_by_code(db, device_code)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not found"
        )

    from app.crud import history as history_crud
    from app.crud import device_tracking as tracking_crud
    from app.crud import queue as queue_crud
    from app.services.device_tracking import (
        EVENT_STATUS,
        EVENT_TASK_COMPLETE,
        EVENT_TASK_START,
        advance_task_state,
        normalize_task_key,
    )

    previous_status = (
        device.status.value if hasattr(device.status, "value") else str(device.status)
    )
    observed_at = datetime.now(timezone.utc)
    device_id = int(device.id)  # type: ignore[arg-type]

    device_crud.update_device_heartbeat(db, device)
    device_crud.update_device_status(
        db,
        device,
        ModelDeviceStatus(status_report.status),
        status_report.task_id,
        status_report.task_name,
        status_report.task_progress,
        status_report.metrics,
        status_report.client_base_url,
    )

    current_status = (
        device.status.value if hasattr(device.status, "value") else str(device.status)
    )
    normalized_task_key = normalize_task_key(status_report.task_key)
    task_state = tracking_crud.get_or_create_task_state(db, device_id)
    state_snapshot = tracking_crud.snapshot_task_state(task_state)
    decision = advance_task_state(
        state_snapshot,
        status=current_status,
        task_key=normalized_task_key,
        task_name=status_report.task_name,
        task_progress=status_report.task_progress,
    )

    if previous_status != current_status:
        tracking_crud.create_state_event(
            db,
            device_id=device_id,
            event_type=EVENT_STATUS,
            status=current_status,
            task_key=decision.next_state.task_key,
            task_name=status_report.task_name,
            task_progress=status_report.task_progress,
            occurred_at=observed_at,
        )

    if decision.emit_task_start:
        tracking_crud.create_state_event(
            db,
            device_id=device_id,
            event_type=EVENT_TASK_START,
            status=current_status,
            task_key=decision.next_state.task_key,
            task_name=decision.next_state.task_name,
            task_progress=status_report.task_progress,
            occurred_at=observed_at,
        )

    completed_record = None
    if decision.allow_completion:
        tracking_crud.create_state_event(
            db,
            device_id=device_id,
            event_type=EVENT_TASK_COMPLETE,
            status=current_status,
            task_key=decision.next_state.task_key,
            task_name=decision.next_state.task_name,
            task_progress=status_report.task_progress,
            occurred_at=observed_at,
        )
        task_duration_seconds = (
            int(device.task_elapsed_seconds)  # type: ignore[arg-type]
            if device.task_elapsed_seconds is not None
            else 0
        )
        history_crud.create_status_history(
            db,
            device_id,
            current_status,
            status_report.task_id,
            status_report.task_name,
            status_report.task_progress,
            status_report.metrics,
            task_duration_seconds,
        )
        completed_record = queue_crud.complete_first_in_queue(db, device_id)

    tracking_crud.save_task_state(db, task_state, decision.next_state)

    queue_count = queue_crud.get_queue_count(db, device_id)

    if completed_record:
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": device.id,
                    "action": "complete",
                    "queue_count": queue_count,
                    "queue_id": completed_record.id,
                    "completed_by": completed_record.inspector_name,
                    "completed_by_id": completed_record.created_by_id,
                    "device_name": device.name,
                },
            }
        )

    await websocket_manager.broadcast(
        {
            "type": "device_status_update",
            "data": {
                "device_id": device.id,
                "device_code": device.device_code,
                "device_name": device.name,
                "status": current_status,
                "task_id": status_report.task_id,
                "task_name": status_report.task_name,
                "task_progress": status_report.task_progress,
                "task_started_at": device.task_started_at,
                "task_elapsed_seconds": device.task_elapsed_seconds,
                "metrics": status_report.metrics,
                "last_heartbeat": device.last_heartbeat,
                "queue_count": queue_count,
            },
        }
    )

    return MessageResponse(
        success=True,
        message="Status updated successfully",
        data={"device_id": device.id, "queue_count": queue_count},
    )


@router.get("/online/list", response_model=List[Device])
def get_online_devices(db: Session = Depends(get_db)):
    """获取所有在线设备"""
    return device_crud.get_online_devices(db)
