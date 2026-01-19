from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.crud import devices as device_crud
from app.websocket.manager import websocket_manager
from app.models import DeviceStatus
from datetime import datetime, timezone
import asyncio


async def check_device_heartbeat():
    """检查设备心跳，标记离线设备"""
    db = SessionLocal()
    try:
        devices = device_crud.get_devices(db)
        for device in devices:
            if device.last_heartbeat is not None:
                now = datetime.now(timezone.utc)
                last_heartbeat = device.last_heartbeat
                if last_heartbeat.tzinfo is None:
                    last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
                time_diff = (now - last_heartbeat).total_seconds()
                if time_diff > 30 and device.status != DeviceStatus.OFFLINE:
                    device_crud.update_device_status(db, device, DeviceStatus.OFFLINE)

                    await websocket_manager.broadcast(
                        {
                            "type": "device_offline",
                            "data": {
                                "device_id": device.id,
                                "last_seen": last_heartbeat.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                            },
                        }
                    )

                    print(f"Device {device.device_code} marked as offline")
    except Exception as e:
        print(f"Error checking device heartbeat: {e}")
    finally:
        db.close()


async def start_heartbeat_monitor():
    """启动心跳监控任务"""
    while True:
        try:
            await check_device_heartbeat()
        except Exception as e:
            print(f"Error in heartbeat monitor: {e}")

        # 每10秒检查一次
        await asyncio.sleep(10)
