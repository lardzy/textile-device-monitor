import os
from typing import List
from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=".env",
        extra="ignore"  # 忽略额外的环境变量
    )

    # Database configuration
    DATABASE_URL: str = "postgresql://admin:password123@postgres:5432/textile_monitor"
    SECRET_KEY: str = "your-secret-key-change-in-production"

    # File upload configuration
    MAX_FILE_SIZE: int = 50 * 1024 * 1024  # 50MB
    UPLOAD_DIR: str = "uploads"
    RESULTS_DIR: str = "results"

    # Queue configuration
    HEARTBEAT_TIMEOUT: int = 30
    DATA_RETENTION_DAYS: int = 30
    QUEUE_IDLE_REMIND_SECONDS: int = 60
    QUEUE_IDLE_TIMEOUT_SECONDS: int = 300
    QUEUE_IDLE_EXTEND_SECONDS: int = 300
    QUEUE_IDLE_CHECK_INTERVAL: int = 10

    # CORS configuration
    CORS_ORIGINS: str = "http://localhost,http://localhost:80,http://backend:8000"


settings = Settings()
