from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import uvicorn
from pathlib import Path

from app.database import engine, get_db
from app.models import Base
from app.config import settings
from app.api import devices, history, queue, stats
from app.websocket.manager import websocket_manager
from app.tasks.device_monitor import start_heartbeat_monitor
from app.tasks.data_cleanup import start_cleanup_scheduler
import asyncio

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="纺织品检测设备监控系统",
    description="用于监控纺织品检测设备状态、排队管理和数据统计",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(devices.router, prefix="/api")
app.include_router(history.router, prefix="/api")
app.include_router(queue.router, prefix="/api")
app.include_router(stats.router, prefix="/api")


@app.get("/")
def read_root():
    return {
        "message": "纺织品检测设备监控系统 API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


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
    print("Starting heartbeat monitor...")
    asyncio.create_task(start_heartbeat_monitor())

    print("Starting cleanup scheduler...")
    asyncio.create_task(start_cleanup_scheduler())

    print("Application started successfully!")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
