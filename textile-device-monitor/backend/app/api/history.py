from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.database import get_db
from app.schemas import DeviceStatusHistory, MessageResponse
from app.crud import history as history_crud
from app.utils.exporters import export_history_to_excel

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=dict)
def get_history(
    device_id: Optional[int] = Query(None, description="设备ID"),
    start_date: Optional[datetime] = Query(None, description="开始日期"),
    end_date: Optional[datetime] = Query(None, description="结束日期"),
    status: Optional[str] = Query(None, description="状态筛选"),
    task_id: Optional[str] = Query(None, description="任务ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    db: Session = Depends(get_db),
):
    """查询历史记录"""
    skip = (page - 1) * page_size

    history, total = history_crud.get_device_history(
        db,
        device_id=device_id,
        start_date=start_date,
        end_date=end_date,
        status=status,
        task_id=task_id,
        skip=skip,
        limit=page_size,
    )

    return {
        "data": [
            DeviceStatusHistory.model_validate(item).model_dump() for item in history
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/export")
def export_history(
    device_id: Optional[int] = Query(None, description="设备ID"),
    start_date: Optional[datetime] = Query(None, description="开始日期"),
    end_date: Optional[datetime] = Query(None, description="结束日期"),
    status: Optional[str] = Query(None, description="状态筛选"),
    task_id: Optional[str] = Query(None, description="任务ID"),
    db: Session = Depends(get_db),
):
    """导出历史记录为Excel"""
    history, total = history_crud.get_device_history(
        db,
        device_id=device_id,
        start_date=start_date,
        end_date=end_date,
        status=status,
        task_id=task_id,
        skip=0,
        limit=10000,
    )

    if total == 0:
        raise HTTPException(status_code=404, detail="No data to export")

    return export_history_to_excel(history)


@router.get("/device/{device_id}", response_model=List[DeviceStatusHistory])
def get_device_history_by_id(device_id: int, db: Session = Depends(get_db)):
    """获取指定设备的历史记录"""
    history = history_crud.get_device_history(db, device_id=device_id)[0]
    return history


@router.get("/latest/{device_id}", response_model=DeviceStatusHistory)
def get_latest_device_status(device_id: int, db: Session = Depends(get_db)):
    """获取设备最新状态"""
    latest = history_crud.get_latest_status(db, device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No history found")
    return latest
