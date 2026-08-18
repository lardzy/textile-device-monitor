from pathlib import Path
from tempfile import TemporaryFile

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
import uvicorn

from app.database import SessionLocal
from app.config import settings
from app.api import area, devices, execution, history, ocr, queue, results, stats
from app.execution.catalog import bootstrap_execution_system
from app.execution.persistence import ensure_storage_roots
from app.execution.worker_state import (
    database_schema_is_current,
    worker_heartbeat_is_fresh,
)
from app.websocket.manager import websocket_manager
from app.tasks.device_monitor import start_heartbeat_monitor
from app.tasks.queue_timeout import start_queue_timeout_monitor
from app.tasks.data_cleanup import start_cleanup_scheduler
from app.tasks.area_archive import start_area_archive_scheduler
from app.services.ocr_jobs import ocr_job_manager
from app.services.area_jobs import area_job_manager
import asyncio

app = FastAPI(
    title="纺织品检测设备监控系统",
    description="用于监控纺织品检测设备状态、排队管理和数据统计",
    version="1.0.0",
)

cors_origins = settings.cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "X-CSRF-Token",
            "X-Device-Token",
            "X-Request-ID",
        ],
    )

app.include_router(devices.router, prefix="/api")
app.include_router(history.router, prefix="/api")
app.include_router(queue.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(results.router, prefix="/api")
app.include_router(ocr.router, prefix="/api")
app.include_router(area.router, prefix="/api")
if settings.EXECUTION_ENABLED:
    app.include_router(execution.router, prefix="/api")


@app.get("/")
def read_root():
    return {
        "message": "纺织品检测设备监控系统 API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health/live")
def liveness_check():
    return {"status": "alive"}


def _probe_writable_directory(path_value: str) -> bool:
    path = Path(path_value)
    if not path.exists() or not path.is_dir():
        return False
    try:
        with TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def _readiness_payload() -> tuple[dict, int]:
    components: dict[str, dict] = {}
    ready = True
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1")).scalar_one()
            components["database"] = {"ready": True}
            migration_ready, current_revision, expected_revision = (
                database_schema_is_current(db)
            )
            components["migration"] = {
                "ready": migration_ready,
                "current": current_revision,
                "expected": expected_revision,
            }
            ready = ready and migration_ready
            if settings.EXECUTION_ENABLED:
                worker_ready, heartbeat = worker_heartbeat_is_fresh(db)
                components["execution_worker"] = {
                    "ready": worker_ready,
                    "last_seen_at": (
                        heartbeat.last_seen_at.isoformat()
                        if heartbeat is not None
                        else None
                    ),
                }
                ready = ready and worker_ready
    except Exception as exc:
        components["database"] = {
            "ready": False,
            "error": type(exc).__name__,
        }
        ready = False

    if settings.EXECUTION_ENABLED:
        staging_ready = _probe_writable_directory(
            settings.EXECUTION_RUNTIME_ROOT
        )
        publish_ready = _probe_writable_directory(
            settings.EXECUTION_PUBLISH_ROOT
        )
        report_images_ready = _probe_writable_directory(
            settings.EXECUTION_REPORT_IMAGE_ROOT
        )
        components["execution_storage"] = {
            "ready": staging_ready and publish_ready and report_images_ready,
            "staging_writable": staging_ready,
            "publish_writable": publish_ready,
            "report_images_writable": report_images_ready,
        }
        ready = ready and staging_ready and publish_ready and report_images_ready

    return (
        {
            "status": "ready" if ready else "not_ready",
            "components": components,
        },
        200 if ready else 503,
    )


@app.get("/health/ready")
def readiness_check():
    payload, status_code = _readiness_payload()
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/health")
def health_check():
    payload, status_code = _readiness_payload()
    return JSONResponse(status_code=status_code, content=payload)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        websocket_manager.disconnect(websocket)


@app.on_event("startup")
async def startup_event():
    settings.validate_execution_security()
    if settings.EXECUTION_ENABLED:
        with SessionLocal() as db:
            bootstrap_execution_system(db)
            ensure_storage_roots(db)
            db.commit()

    print("Starting heartbeat monitor...")
    asyncio.create_task(start_heartbeat_monitor())

    print("Starting cleanup scheduler...")
    asyncio.create_task(start_cleanup_scheduler())

    print("Starting area archive scheduler...")
    asyncio.create_task(start_area_archive_scheduler())

    print("Starting queue timeout monitor...")
    asyncio.create_task(start_queue_timeout_monitor())

    if settings.OCR_ENABLED:
        print("Starting OCR job manager...")
        ocr_job_manager.start()
    else:
        print("OCR job manager disabled by config")

    if settings.AREA_ENABLED:
        print("Starting area job manager...")
        area_job_manager.start()
    else:
        print("Area job manager disabled by config")

    print("Application started successfully!")


@app.on_event("shutdown")
async def shutdown_event():
    ocr_job_manager.stop()
    area_job_manager.stop()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
