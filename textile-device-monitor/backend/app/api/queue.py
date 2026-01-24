from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas import (
    QueueRecord,
    QueueCreate,
    PositionChange,
    MessageResponse,
    QueueWithLogs,
)
from app.crud import queue as queue_crud
from app.websocket.manager import websocket_manager

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/{device_id}", response_model=QueueWithLogs)
def get_queue(device_id: int, db: Session = Depends(get_db)):
    """获取指定设备的排队列表和修改日志"""
    queue, logs = queue_crud.get_queue_by_device_with_logs(db, device_id)
    return QueueWithLogs(queue=queue, logs=logs)


@router.post("", response_model=QueueRecord, status_code=status.HTTP_201_CREATED)
async def join_queue(queue: QueueCreate, db: Session = Depends(get_db)):
    """加入排队"""
    queue_record = queue_crud.join_queue(db, queue)

    await websocket_manager.broadcast(
        {
            "type": "queue_update",
            "data": {
                "device_id": queue.device_id,
                "action": "join",
                "inspector_name": queue.inspector_name,
                "position": queue_record.position,
            },
        }
    )

    return queue_record


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
            },
        }
    )

    return queue_record


@router.delete("/{queue_id}")
async def leave_queue(queue_id: int, db: Session = Depends(get_db)):
    """离开排队"""
    queue_record = queue_crud.get_queue_record(db, queue_id)

    if not queue_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Queue record not found"
        )

    device_id = queue_record.device_id
    success = queue_crud.delete_queue(db, queue_id)

    if success:
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": device_id,
                    "action": "leave",
                    "queue_id": queue_id,
                },
            }
        )

    return {"message": "Queue record deleted successfully"}


@router.post("/{device_id}/complete", response_model=MessageResponse)
async def complete_task(device_id: int, db: Session = Depends(get_db)):
    """设备完成一单（减少排队数量）"""
    completed_record = queue_crud.complete_first_in_queue(db, device_id)

    if completed_record:
        await websocket_manager.broadcast(
            {
                "type": "queue_update",
                "data": {
                    "device_id": device_id,
                    "action": "complete",
                    "queue_id": completed_record.id,
                    "completed_by": completed_record.inspector_name,
                },
            }
        )

    return MessageResponse(
        success=True,
        message="Task completed",
        data={
            "device_id": device_id,
            "queue_count": queue_crud.get_queue_count(db, device_id),
        },
    )


@router.get("/count/{device_id}")
def get_queue_count(device_id: int, db: Session = Depends(get_db)):
    """获取当前排队数量"""
    count = queue_crud.get_queue_count(db, device_id)
    return {"device_id": device_id, "queue_count": count}
