import os
from typing import List
from pydantic_settings import BaseSettings


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
    CORS_ORIGINS: str = "http://localhost,http://localhost:80,http://backend:8000"

    class Config:
        env_file = ".env"


settings = Settings()
