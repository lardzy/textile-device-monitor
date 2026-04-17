import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://admin:password123@postgres:5432/textile_monitor"
    SECRET_KEY: str = "your-secret-key-change-in-production"
    HEARTBEAT_TIMEOUT: int = 30
    DATA_RETENTION_DAYS: int = 30
    QUEUE_IDLE_REMIND_SECONDS: int = 60
    QUEUE_IDLE_TIMEOUT_SECONDS: int = 300
    QUEUE_IDLE_EXTEND_SECONDS: int = 300
    QUEUE_IDLE_CHECK_INTERVAL: int = 10
    RESULTS_RECENT_CACHE_TTL: int = 5
    RESULTS_RECENT_CACHE_STALE_TTL: int = 30
    RESULTS_RECENT_INFLIGHT_WAIT_SECONDS: int = 12
    OCR_ENABLED: bool = True
    OCR_SERVICE_URL: str = "http://ocr-adapter:5002"
    OCR_UPLOAD_DIR: str = "/tmp/ocr_uploads"
    OCR_OUTPUT_DIR: str = "/tmp/ocr_outputs"
    OCR_MAX_UPLOAD_MB: int = 30
    OCR_MAX_BATCH_FILES: int = 10
    OCR_JOB_TIMEOUT_SECONDS: int = 600
    OCR_MAX_CONCURRENT_JOBS: int = 1
    OCR_RETENTION_HOURS: int = 24
    AREA_ENABLED: bool = True
    AREA_OUTPUT_DIR: str = "/data/area_outputs"
    AREA_MAX_CONCURRENT_JOBS: int = 1
    AREA_ROOT_PATH_DEFAULT: str = "/tmp/area_inputs"
    AREA_WEIGHTS_DIR: str = "runtime/area-models"
    AREA_EXCEL_TEMPLATE_PATH: str = "/opt/area_templates/-面积法-定量试验原始记录-新系统.xls"
    AREA_INFER_URL: str = "http://area-infer:9001"
    AREA_INFER_TIMEOUT_SEC: int = 60
    STATS_TIMEZONE: str = "Asia/Shanghai"
    CORS_ORIGINS: str = "http://localhost,http://localhost:80,http://backend:8000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
