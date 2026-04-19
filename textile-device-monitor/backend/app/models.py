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
    UniqueConstraint,
    Enum as SQLEnum,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum


JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


class DeviceStatus(str, enum.Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class TaskStatus(str, enum.Enum):
    WAITING = "waiting"
    COMPLETED = "completed"


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
    metrics = Column(JSON_VARIANT)
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
    task_state = relationship(
        "DeviceTaskState",
        back_populates="device",
        uselist=False,
    )
    state_events = relationship("DeviceStateEvent", back_populates="device")
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
    device_metrics = Column(JSON_VARIANT)
    reported_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    device = relationship("Device", back_populates="status_history")


class DeviceTaskState(Base):
    __tablename__ = "device_task_states"

    device_id = Column(Integer, ForeignKey("devices.id"), primary_key=True)
    task_key = Column(String(500), index=True)
    task_name = Column(String(200))
    observed_in_progress = Column(Boolean, nullable=False, default=False)
    last_status = Column(String(20))
    last_progress = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    device = relationship("Device", back_populates="task_state")


class DeviceStateEvent(Base):
    __tablename__ = "device_state_events"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    task_key = Column(String(500))
    task_name = Column(String(200))
    task_progress = Column(Integer)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    device = relationship("Device", back_populates="state_events")


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
    is_placeholder = Column(Boolean, nullable=False, default=False)
    auto_remove_when_inactive = Column(Boolean, nullable=False, default=False)

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


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String(100), unique=True, nullable=False, index=True)
    value_text = Column(Text)
    value_json = Column(JSON_VARIANT)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AreaJob(Base):
    __tablename__ = "area_jobs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    folder_name = Column(String(255), nullable=False, index=True)
    model_name = Column(String(200), nullable=False)
    model_file = Column(String(255), nullable=False)
    root_path = Column(Text, nullable=False)
    output_dir = Column(Text, nullable=False)
    overlay_dir = Column(Text, nullable=False)
    result_json_path = Column(Text, nullable=False)
    excel_path = Column(Text, nullable=False)
    infer_url = Column(String(500), nullable=False)
    infer_timeout_sec = Column(Integer, nullable=False, default=60)
    inference_options = Column(JSON_VARIANT)
    status = Column(String(32), nullable=False, default="queued", index=True)
    error_code = Column(String(128))
    error_message = Column(Text)
    total_images = Column(Integer, nullable=False, default=0)
    processed_images = Column(Integer, nullable=False, default=0)
    succeeded_images = Column(Integer, nullable=False, default=0)
    failed_images = Column(Integer, nullable=False, default=0)
    engine_meta = Column(JSON_VARIANT)
    summary_json = Column(JSON_VARIANT)
    detail_json = Column(JSON_VARIANT)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    images = relationship(
        "AreaJobImage",
        back_populates="job",
        cascade="all, delete-orphan",
    )


class AreaJobImage(Base):
    __tablename__ = "area_job_images"
    __table_args__ = (UniqueConstraint("job_id", "image_name", name="uq_area_job_image"),)

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("area_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    image_name = Column(String(255), nullable=False)
    overlay_filename = Column(String(255), nullable=False, default="")
    source_image_path = Column(Text, nullable=False)
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    total_area_px = Column(Integer, nullable=False, default=0)
    per_class_area_px = Column(JSON_VARIANT)
    error_message = Column(Text)
    edited_by_id = Column(String(64))
    edited_at = Column(DateTime(timezone=True))
    edit_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job = relationship("AreaJob", back_populates="images")
    instances = relationship(
        "AreaJobInstance",
        back_populates="image",
        cascade="all, delete-orphan",
    )


class AreaJobInstance(Base):
    __tablename__ = "area_job_instances"

    id = Column(Integer, primary_key=True, index=True)
    image_id = Column(
        Integer,
        ForeignKey("area_job_images.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    class_name = Column(String(100), nullable=False)
    score = Column(Float)
    bbox = Column(JSON_VARIANT)
    polygon = Column(JSON_VARIANT)
    area_px = Column(Integer, nullable=False, default=0)
    is_deleted = Column(Boolean, nullable=False, default=False)
    sort_index = Column(Integer, nullable=False, default=0)
    initial_bbox = Column(JSON_VARIANT)
    initial_polygon = Column(JSON_VARIANT)
    initial_area_px = Column(Integer, nullable=False, default=0)
    initial_is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    image = relationship("AreaJobImage", back_populates="instances")
