from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date, datetime, timedelta
from app.database import get_db
from app.schemas import Statistic
from app.crud import stats as stats_crud

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/realtime")
def get_realtime_statistics(db: Session = Depends(get_db)):
    """获取实时统计数据"""
    return stats_crud.get_realtime_stats(db)


@router.get("/device/{device_id}")
def get_device_realtime_stats(device_id: int, db: Session = Depends(get_db)):
    """获取指定设备的实时统计"""
    return stats_crud.get_device_realtime_stats(db, device_id)


@router.get("/devices/{device_id}")
def get_device_statistics(
    device_id: int,
    stat_type: str = Query("daily", description="统计类型: daily/weekly/monthly"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    db: Session = Depends(get_db),
):
    """获取指定设备的统计数据"""
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()

    return stats_crud.get_statistics(db, device_id, stat_type, start_date, end_date)


@router.get("/summary")
def get_summary_statistics(
    stat_type: str = Query("daily", description="统计类型: daily/weekly/monthly"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    db: Session = Depends(get_db),
):
    """获取汇总统计数据"""
    if not start_date:
        start_date = date.today() - timedelta(days=7)
    if not end_date:
        end_date = date.today()

    return stats_crud.get_summary_stats(db, stat_type, start_date, end_date)
