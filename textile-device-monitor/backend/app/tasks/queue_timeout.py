from datetime import datetime, timezone, timedelta
import asyncio

from app.config import settings
from app.crud import devices as device_crud
from app.crud import queue as queue_crud
from app.database import SessionLocal
from app.models import DeviceStatus
from app.websocket.manager import websocket_manager


def normalize_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def broadcast_timeout_update(device):
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


def reset_timeout_state(device):
    device.queue_timeout_active_id = None
    device.queue_timeout_started_at = None
    device.queue_timeout_deadline_at = None
    device.queue_timeout_reminded_at = None
    device.queue_timeout_extended_count = 0


async def check_queue_timeouts():
    db = SessionLocal()
    try:
        devices = device_crud.get_devices(db)
        now = datetime.now(timezone.utc)

        for device in devices:
            queue = queue_crud.get_queue_by_device(db, device.id)

            if device.status != DeviceStatus.IDLE or len(queue) < 2:
                if (
                    device.queue_timeout_active_id is not None
                    or device.queue_timeout_deadline_at is not None
                ):
                    reset_timeout_state(device)
                    db.commit()
                    await broadcast_timeout_update(device)
                continue

            active_record = queue[0]

            if (
                device.queue_timeout_active_id != active_record.id
                or device.queue_timeout_started_at is None
                or device.queue_timeout_deadline_at is None
            ):
                device.queue_timeout_active_id = active_record.id
                device.queue_timeout_started_at = now
                device.queue_timeout_deadline_at = now + timedelta(
                    seconds=settings.QUEUE_IDLE_TIMEOUT_SECONDS
                )
                device.queue_timeout_reminded_at = None
                device.queue_timeout_extended_count = 0
                db.commit()
                await broadcast_timeout_update(device)
                continue

            started_at = normalize_datetime(device.queue_timeout_started_at)
            deadline_at = normalize_datetime(device.queue_timeout_deadline_at)
            reminded_at = normalize_datetime(device.queue_timeout_reminded_at)

            if started_at is None or deadline_at is None:
                device.queue_timeout_started_at = now
                device.queue_timeout_deadline_at = now + timedelta(
                    seconds=settings.QUEUE_IDLE_TIMEOUT_SECONDS
                )
                device.queue_timeout_reminded_at = None
                device.queue_timeout_extended_count = 0
                db.commit()
                await broadcast_timeout_update(device)
                continue

            if (
                reminded_at is None
                and now < deadline_at
                and (now - started_at).total_seconds()
                >= settings.QUEUE_IDLE_REMIND_SECONDS
            ):
                device.queue_timeout_reminded_at = now
                db.commit()
                next_record = queue[1]
                await websocket_manager.broadcast(
                    {
                        "type": "queue_timeout_reminder",
                        "data": {
                            "device_id": device.id,
                            "device_name": device.name,
                            "active_queue_id": active_record.id,
                            "active_name": active_record.inspector_name,
                            "active_created_by_id": active_record.created_by_id,
                            "next_queue_id": next_record.id,
                            "next_name": next_record.inspector_name,
                            "next_created_by_id": next_record.created_by_id,
                        },
                    }
                )

            if now >= deadline_at:
                timed_out_record = queue[0]
                next_record = queue[1]
                queue_crud.swap_first_two_in_queue(
                    db, device.id, changed_by="系统", changed_by_id=None
                )
                queue_count = queue_crud.get_queue_count(db, device.id)

                await websocket_manager.broadcast(
                    {
                        "type": "queue_update",
                        "data": {
                            "device_id": device.id,
                            "action": "timeout_shift",
                            "queue_count": queue_count,
                        },
                    }
                )

                await websocket_manager.broadcast(
                    {
                        "type": "queue_timeout_shift",
                        "data": {
                            "device_id": device.id,
                            "device_name": device.name,
                            "timed_out_queue_id": timed_out_record.id,
                            "timed_out_name": timed_out_record.inspector_name,
                            "timed_out_created_by_id": timed_out_record.created_by_id,
                            "new_active_queue_id": next_record.id,
                            "new_active_name": next_record.inspector_name,
                            "new_active_created_by_id": next_record.created_by_id,
                        },
                    }
                )

                device.queue_timeout_active_id = next_record.id
                device.queue_timeout_started_at = now
                device.queue_timeout_deadline_at = now + timedelta(
                    seconds=settings.QUEUE_IDLE_TIMEOUT_SECONDS
                )
                device.queue_timeout_reminded_at = None
                device.queue_timeout_extended_count = 0
                db.commit()
                await broadcast_timeout_update(device)
    except Exception as e:
        print(f"Error checking queue timeouts: {e}")
    finally:
        db.close()


async def start_queue_timeout_monitor():
    while True:
        try:
            await check_queue_timeouts()
        except Exception as e:
            print(f"Error in queue timeout monitor: {e}")

        await asyncio.sleep(settings.QUEUE_IDLE_CHECK_INTERVAL)
