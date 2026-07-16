from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timezone
import asyncio
from sqlalchemy.exc import IntegrityError
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


def schedule_websocket_broadcast(message: dict) -> None:
    """Schedule fire-and-forget WebSocket work without delaying API responses."""
    if not websocket_manager.active_connections:
        return
    asyncio.create_task(websocket_manager.broadcast(message))


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _status_snapshot(device, queue_count: int) -> dict:
    """Build the committed device snapshot used by responses and WebSocket events."""
    return {
        "device_id": device.id,
        "device_code": device.device_code,
        "device_name": device.name,
        "status": _enum_value(device.status),
        "task_id": device.task_id,
        "task_name": device.task_name,
        "task_progress": device.task_progress,
        "task_started_at": device.task_started_at,
        "task_elapsed_seconds": device.task_elapsed_seconds,
        "metrics": device.metrics,
        "last_heartbeat": device.last_heartbeat,
        "queue_count": queue_count,
    }


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
    """Settle one device report atomically and broadcast only committed state."""
    from app.crud import history as history_crud
    from app.crud import device_tracking as tracking_crud
    from app.crud import queue as queue_crud
    from app.services.device_tracking import (
        EVENT_STATUS,
        EVENT_TASK_COMPLETE,
        EVENT_TASK_START,
        advance_task_state,
        resolve_tracking_task_key,
    )

    observed_at = status_report.reported_at or datetime.now(timezone.utc)
    report_id = str(status_report.report_id) if status_report.report_id else None
    completed_message = None
    placeholder_message = None

    try:
        # All reports for one device are serialized in PostgreSQL. The lock is
        # deliberately acquired before checking the receipt, so a retry waits
        # for the original transaction and then observes its committed receipt.
        device = device_crud.get_device_by_code_for_update(db, device_code)
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Device not found",
            )

        device_id = int(device.id)  # type: ignore[arg-type]
        if report_id and tracking_crud.get_status_report_receipt(
            db,
            device_id=device_id,
            report_id=report_id,
        ):
            queue_count = queue_crud.get_queue_count(db, device_id)
            response_snapshot = _status_snapshot(device, queue_count)
            db.commit()
            return MessageResponse(
                success=True,
                message="Duplicate status report ignored",
                data={**response_snapshot, "duplicate": True},
            )

        if report_id:
            tracking_crud.create_status_report_receipt(
                db,
                device_id=device_id,
                report_id=report_id,
                reported_at=observed_at,
            )

        previous_status = _enum_value(device.status)
        device_crud.update_device_status(
            db,
            device,
            ModelDeviceStatus(status_report.status),
            status_report.task_id,
            status_report.task_name,
            status_report.task_progress,
            status_report.metrics,
            status_report.client_base_url,
            commit=False,
        )

        current_status = _enum_value(device.status)
        effective_task_id = device.task_id
        effective_task_name = device.task_name
        effective_task_progress = device.task_progress
        effective_metrics = device.metrics
        is_laser_confocal = (
            isinstance(effective_metrics, dict)
            and effective_metrics.get("device_type") == "laser_confocal"
        )
        normalized_task_key = resolve_tracking_task_key(
            status_report.task_key,
            effective_task_name,
        )
        task_state = tracking_crud.get_or_create_task_state(
            db,
            device_id,
            commit=False,
            for_update=True,
        )
        state_snapshot = tracking_crud.snapshot_task_state(task_state)
        decision = advance_task_state(
            state_snapshot,
            status=current_status,
            task_key=normalized_task_key,
            task_name=effective_task_name,
            task_progress=effective_task_progress,
            is_laser_confocal=is_laser_confocal,
        )

        if previous_status != current_status:
            tracking_crud.create_state_event(
                db,
                device_id=device_id,
                event_type=EVENT_STATUS,
                status=current_status,
                task_key=decision.next_state.task_key,
                task_name=effective_task_name,
                task_progress=effective_task_progress,
                occurred_at=observed_at,
                commit=False,
            )

        if decision.emit_task_start:
            tracking_crud.create_state_event(
                db,
                device_id=device_id,
                event_type=EVENT_TASK_START,
                status=current_status,
                task_key=decision.next_state.task_key,
                task_name=decision.next_state.task_name,
                task_progress=effective_task_progress,
                occurred_at=observed_at,
                commit=False,
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
                task_progress=effective_task_progress,
                occurred_at=observed_at,
                commit=False,
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
                effective_task_id,
                effective_task_name,
                effective_task_progress,
                effective_metrics,
                task_duration_seconds,
                reported_at=observed_at,
                commit=False,
            )
            completed_record = queue_crud.complete_first_in_queue(db, device_id)

        tracking_crud.save_task_state(
            db,
            task_state,
            decision.next_state,
            commit=False,
        )

        placeholder_record = None
        if (
            previous_status == ModelDeviceStatus.IDLE.value
            and current_status == ModelDeviceStatus.BUSY.value
        ):
            placeholder_record = queue_crud.create_placeholder_if_missing(db, device_id)

        queue_count = queue_crud.get_queue_count(db, device_id)
        if completed_record:
            completed_message = {
                "type": "queue_update",
                "data": {
                    "device_id": device.id,
                    "action": "complete",
                    "queue_count": queue_count,
                    "queue_id": completed_record.id,
                    "completed_by": completed_record.inspector_name,
                    "completed_by_id": completed_record.created_by_id,
                    "device_name": device.name,
                    "report_id": report_id,
                    "reported_at": observed_at,
                    "task_key": decision.next_state.task_key,
                },
            }
        if placeholder_record:
            placeholder_message = {
                "type": "queue_update",
                "data": {
                    "device_id": device.id,
                    "action": "placeholder_create",
                    "queue_id": placeholder_record.id,
                    "queue_count": queue_count,
                    "inspector_name": placeholder_record.inspector_name,
                    "device_name": device.name,
                },
            }

        # Flush every dependent row before the single commit. No WebSocket work
        # is scheduled until this succeeds.
        db.flush()
        db.commit()
        db.refresh(device)
        response_snapshot = _status_snapshot(device, queue_count)
        response_snapshot.update(
            {
                "report_id": report_id,
                "reported_at": observed_at,
                "task_key": decision.next_state.task_key,
            }
        )
    except IntegrityError:
        db.rollback()
        # SQLite cannot serialize with SELECT FOR UPDATE. Its unique constraint
        # may therefore be the first duplicate detector; translate that race to
        # the same successful idempotent response used by PostgreSQL.
        if report_id:
            duplicate_device = device_crud.get_device_by_code(db, device_code)
            if duplicate_device and tracking_crud.get_status_report_receipt(
                db,
                device_id=int(duplicate_device.id),
                report_id=report_id,
            ):
                duplicate_count = queue_crud.get_queue_count(
                    db,
                    int(duplicate_device.id),
                )
                return MessageResponse(
                    success=True,
                    message="Duplicate status report ignored",
                    data={
                        **_status_snapshot(duplicate_device, duplicate_count),
                        "duplicate": True,
                    },
                )
        raise
    except Exception:
        db.rollback()
        raise

    if completed_message:
        schedule_websocket_broadcast(completed_message)
    if placeholder_message:
        schedule_websocket_broadcast(placeholder_message)
    schedule_websocket_broadcast(
        {"type": "device_status_update", "data": response_snapshot}
    )

    return MessageResponse(
        success=True,
        message="Status updated successfully",
        data={**response_snapshot, "duplicate": False},
    )


@router.get("/online/list", response_model=List[Device])
def get_online_devices(db: Session = Depends(get_db)):
    """获取所有在线设备"""
    return device_crud.get_online_devices(db)
