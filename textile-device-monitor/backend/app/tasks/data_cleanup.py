from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import DeviceStatusHistory, QueueChangeLog
from app.config import settings
from datetime import datetime, date, timedelta
import asyncio


async def cleanup_old_data():
    """清理30天前的历史数据"""
    db = SessionLocal()
    try:
        cutoff_date = datetime.now() - timedelta(days=settings.DATA_RETENTION_DAYS)

        # 删除30天前的状态历史记录
        deleted_history = (
            db.query(DeviceStatusHistory)
            .filter(DeviceStatusHistory.reported_at < cutoff_date)
            .delete()
        )

        # 删除昨天的排队修改日志
        yesterday = date.today() - timedelta(days=1)
        start_of_yesterday = datetime.combine(yesterday, datetime.min.time())
        end_of_yesterday = datetime.combine(yesterday, datetime.max.time())

        deleted_logs = (
            db.query(QueueChangeLog)
            .filter(QueueChangeLog.change_time < start_of_yesterday)
            .delete()
        )

        db.commit()

        print(
            f"Cleaned up {deleted_history} history records and {deleted_logs} log records"
        )

    except Exception as e:
        print(f"Error cleaning up old data: {e}")
        db.rollback()
    finally:
        db.close()


async def start_cleanup_scheduler():
    """启动数据清理调度器"""
    while True:
        try:
            now = datetime.now()

            # 计算距离凌晨2点的时间
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)

            wait_seconds = (next_run - now).total_seconds()
            print(
                f"Next cleanup scheduled at {next_run}, waiting {wait_seconds} seconds"
            )

            await asyncio.sleep(wait_seconds)

            # 执行清理
            await cleanup_old_data()

        except Exception as e:
            print(f"Error in cleanup scheduler: {e}")
            # 如果出错，等待1小时后重试
            await asyncio.sleep(3600)
