from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Float,
    Date,
    Boolean,
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum
import uuid


class DeviceStatus(str, enum.Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class TaskStatus(str, enum.Enum):
    WAITING = "waiting"
    COMPLETED = "completed"


class ConversionTaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    model = Column(String(100))
    location = Column(String(100))
    description = Column(Text)
    status = Column(SQLEnum(DeviceStatus), default=DeviceStatus.OFFLINE)
    last_heartbeat = Column(DateTime(timezone=True))
    task_id = Column(String(100))
    task_name = Column(String(200))
    task_progress = Column(Integer)
    task_started_at = Column(DateTime(timezone=True))
    task_elapsed_seconds = Column(Integer)
    metrics = Column(JSONB)
    client_base_url = Column(String(200))
    queue_timeout_active_id = Column(Integer)
    queue_timeout_started_at = Column(DateTime(timezone=True))
    queue_timeout_deadline_at = Column(DateTime(timezone=True))
    queue_timeout_reminded_at = Column(DateTime(timezone=True))
    queue_timeout_extended_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    status_history = relationship("DeviceStatusHistory", back_populates="device")
    queue_records = relationship("QueueRecord", back_populates="device")
    statistics = relationship("Statistic", back_populates="device")


class DeviceStatusHistory(Base):
    __tablename__ = "device_status_history"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    status = Column(String(20), nullable=False)
    task_id = Column(String(100))
    task_name = Column(String(200))
    task_progress = Column(Integer)
    task_duration_seconds = Column(Integer)
    device_metrics = Column(JSONB)
    reported_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    device = relationship("Device", back_populates="status_history")


class QueueRecord(Base):
    __tablename__ = "queue_records"

    id = Column(Integer, primary_key=True, index=True)
    inspector_name = Column(String(50), nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    position = Column(Integer, nullable=False)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    status = Column(SQLEnum(TaskStatus), default=TaskStatus.WAITING)
    version = Column(Integer, default=1, nullable=False)
    created_by_id = Column(String(64))

    device = relationship("Device", back_populates="queue_records")
    change_logs = relationship(
        "QueueChangeLog",
        back_populates="queue_record",
        cascade="all, delete-orphan",
    )


class QueueChangeLog(Base):
    __tablename__ = "queue_change_logs"

    id = Column(Integer, primary_key=True, index=True)
    queue_id = Column(
        Integer, ForeignKey("queue_records.id", ondelete="CASCADE"), nullable=False
    )
    old_position = Column(Integer)
    new_position = Column(Integer)
    changed_by = Column(String(50))
    changed_by_id = Column(String(64))
    change_type = Column(String(50))
    remark = Column(Text)
    change_time = Column(DateTime(timezone=True), server_default=func.now())

    queue_record = relationship("QueueRecord", back_populates="change_logs")


class Statistic(Base):
    __tablename__ = "statistics"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    stat_date = Column(Date, nullable=False)
    stat_type = Column(String(20), nullable=False)  # daily/weekly/monthly
    total_tasks = Column(Integer, default=0)
    completed_tasks = Column(Integer, default=0)
    failed_tasks = Column(Integer, default=0)
    avg_duration = Column(Integer)
    max_duration = Column(Integer)
    min_duration = Column(Integer)
    utilization_rate = Column(Float)

    device = relationship("Device", back_populates="statistics")


class ConversionTask(Base):
    __tablename__ = "conversion_tasks"

    id = Column(String(36), primary_key=True)
    original_filename = Column(String(255), nullable=False)
    output_format = Column(String(20), nullable=False)  # markdown, docx
    language = Column(String(10), default="auto")
    status = Column(SQLEnum(ConversionTaskStatus), default=ConversionTaskStatus.PENDING)
    file_path = Column(String(500))  # 上传文件的路径
    result_path = Column(String(500))  # 结果文件的路径
    error_message = Column(Text)
    processing_time = Column(Float)  # 处理时间（秒）
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))  # 结果文件过期时间
