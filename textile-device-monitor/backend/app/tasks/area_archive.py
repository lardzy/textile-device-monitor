from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.crud import area as area_crud
from app.database import SessionLocal
from app.services.area_jobs import area_job_manager


ARCHIVE_INTERVAL_HOURS = 48
ARCHIVE_THRESHOLD_HOURS = 24
ARCHIVE_CHECK_INTERVAL_SECONDS = 600


async def run_area_archive_if_due(force: bool = False) -> dict[str, object]:
    db = SessionLocal()
    try:
        config = area_crud.get_area_config(db)
        last_run = area_crud.get_archive_last_run_at(db)
        now = datetime.now(timezone.utc)
        due = force or last_run is None or (now - last_run) >= timedelta(hours=ARCHIVE_INTERVAL_HOURS)
        if not due:
            return {
                "status": "skipped",
                "reason": "not_due",
                "last_run_at": last_run.isoformat() if last_run else None,
            }
        result = area_job_manager.run_archive(
            root_path=str(config.get("root_path") or ""),
            old_root_path=str(config.get("old_root_path") or ""),
            older_than_hours=ARCHIVE_THRESHOLD_HOURS,
        )
        area_crud.set_archive_last_run_at(db, now)
        return {
            "status": "ok",
            "ran_at": now.isoformat(),
            **result,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }
    finally:
        db.close()


async def start_area_archive_scheduler() -> None:
    while True:
        try:
            result = await run_area_archive_if_due(force=False)
            print(f"Area archive scheduler tick: {result}")
        except Exception as exc:
            print(f"Area archive scheduler error: {exc}")
        await asyncio.sleep(ARCHIVE_CHECK_INTERVAL_SECONDS)

