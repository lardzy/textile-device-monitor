from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime, date
from enum import Enum


class DeviceStatus(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class TaskStatus(str, Enum):
    WAITING = "waiting"
    COMPLETED = "completed"


class DeviceBase(BaseModel):
    device_code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    model: Optional[str] = Field(None, max_length=100)
    location: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    model: Optional[str] = Field(None, max_length=100)
    location: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    status: Optional[DeviceStatus] = None


class Device(DeviceBase):
    id: int
    status: DeviceStatus
    task_id: Optional[str]
    task_name: Optional[str]
    task_progress: Optional[int]
    task_started_at: Optional[datetime]
    task_elapsed_seconds: Optional[int]
    metrics: Optional[Dict[str, Any]]
    last_heartbeat: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class StatusReport(BaseModel):
    status: DeviceStatus
    task_id: Optional[str] = Field(None, max_length=100)
    task_name: Optional[str] = Field(None, max_length=200)
    task_progress: Optional[int] = Field(None, ge=0, le=100)
    metrics: Optional[Dict[str, Any]] = None


class DeviceStatusHistory(BaseModel):
    id: int
    device_id: int
    status: str
    task_id: Optional[str]
    task_name: Optional[str]
    task_progress: Optional[int]
    task_duration_seconds: Optional[int]
    device_metrics: Optional[Dict[str, Any]]
    reported_at: datetime

    class Config:
        from_attributes = True


class QueueCreate(BaseModel):
    inspector_name: str = Field(..., min_length=1, max_length=50)
    device_id: int


class PositionChange(BaseModel):
    new_position: int = Field(..., gt=0)
    changed_by: str = Field(..., min_length=1, max_length=50)


class QueueRecord(BaseModel):
    id: int
    inspector_name: str
    device_id: int
    position: int
    submitted_at: datetime
    completed_at: Optional[datetime]
    status: TaskStatus

    class Config:
        from_attributes = True


class QueueChangeLog(BaseModel):
    id: int
    queue_id: int
    old_position: Optional[int]
    new_position: int
    changed_by: str
    change_time: datetime

    class Config:
        from_attributes = True


class QueueWithLogs(BaseModel):
    queue: list[QueueRecord]
    logs: list[QueueChangeLog]


class Statistic(BaseModel):
    id: int
    device_id: int
    stat_date: date
    stat_type: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    avg_duration: Optional[int]
    max_duration: Optional[int]
    min_duration: Optional[int]
    utilization_rate: Optional[float]

    class Config:
        from_attributes = True


class HistoryQuery(BaseModel):
    device_id: Optional[int] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[str] = None
    task_id: Optional[str] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class StatQuery(BaseModel):
    device_id: Optional[int] = None
    stat_type: str = Field("daily", pattern="^(daily|weekly|monthly)$")
    start_date: date
    end_date: date


class MessageResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
