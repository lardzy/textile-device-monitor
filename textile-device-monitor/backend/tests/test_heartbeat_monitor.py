from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest


db_fd, db_path = tempfile.mkstemp(prefix="textile-monitor-heartbeat-", suffix=".sqlite")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["OCR_ENABLED"] = "false"
os.environ["AREA_ENABLED"] = "false"

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Base, Device, DeviceStatus
from app.tasks.device_monitor import check_device_heartbeat


class HeartbeatMonitorTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self._original_timeout = settings.HEARTBEAT_TIMEOUT

    def tearDown(self):
        settings.HEARTBEAT_TIMEOUT = self._original_timeout
        self.db.close()

    def _create_device(self, *, code: str, heartbeat_age_seconds: int) -> Device:
        device = Device(
            device_code=code,
            name=code,
            status=DeviceStatus.IDLE,
            last_heartbeat=datetime.now(timezone.utc)
            - timedelta(seconds=heartbeat_age_seconds),
        )
        self.db.add(device)
        self.db.commit()
        self.db.refresh(device)
        return device

    def test_heartbeat_timeout_uses_configured_threshold(self):
        settings.HEARTBEAT_TIMEOUT = 90
        recent = self._create_device(code="recent", heartbeat_age_seconds=31)
        borderline = self._create_device(code="borderline", heartbeat_age_seconds=60)
        expired = self._create_device(code="expired", heartbeat_age_seconds=91)

        asyncio.run(check_device_heartbeat())

        self.db.refresh(recent)
        self.db.refresh(borderline)
        self.db.refresh(expired)
        self.assertEqual(recent.status, DeviceStatus.IDLE)
        self.assertEqual(borderline.status, DeviceStatus.IDLE)
        self.assertEqual(expired.status, DeviceStatus.OFFLINE)

    def test_shorter_configured_timeout_marks_device_offline(self):
        settings.HEARTBEAT_TIMEOUT = 5
        device = self._create_device(code="short-timeout", heartbeat_age_seconds=6)
        original_heartbeat = device.last_heartbeat

        asyncio.run(check_device_heartbeat())

        self.db.refresh(device)
        self.assertEqual(device.status, DeviceStatus.OFFLINE)
        self.assertEqual(
            device.last_heartbeat.replace(tzinfo=None),
            original_heartbeat.replace(tzinfo=None),
        )


if __name__ == "__main__":
    unittest.main()
