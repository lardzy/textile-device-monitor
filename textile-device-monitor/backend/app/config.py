import os
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://admin:password123@postgres:5432/textile_monitor"
    SECRET_KEY: str = "your-secret-key-change-in-production"
    HEARTBEAT_TIMEOUT: int = 30
    DATA_RETENTION_DAYS: int = 30
    CORS_ORIGINS: str = "http://localhost,http://localhost:80,http://backend:8000,https://localhost,https://localhost:443,https://127.0.0.1,https://127.0.0.1:443"

    class Config:
        env_file = ".env"


settings = Settings()
