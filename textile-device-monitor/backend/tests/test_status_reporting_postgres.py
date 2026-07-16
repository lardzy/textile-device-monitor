from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import threading
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.devices import report_device_status
from app.models import (
    Base,
    Device,
    DeviceStateEvent,
    DeviceStatus,
    DeviceStatusHistory,
    DeviceStatusReport,
    QueueRecord,
    TaskStatus,
)
from app.schemas import DeviceStatus as SchemaDeviceStatus, StatusReport
from app.services.device_tracking import EVENT_TASK_COMPLETE


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if TEST_DATABASE_URL:
    test_database_url = make_url(TEST_DATABASE_URL)
    if (
        test_database_url.get_backend_name() != "postgresql"
        or not (test_database_url.database or "").endswith("_test")
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL must use PostgreSQL and a database ending in _test"
        )


@unittest.skipUnless(TEST_DATABASE_URL, "PostgreSQL TEST_DATABASE_URL not configured")
class PostgreSQLStatusReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine)
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self) -> None:
        db = self.SessionLocal()
        try:
            self.device = Device(
                device_code="postgres-status-device",
                name="PostgreSQL 状态设备",
                status=DeviceStatus.IDLE,
            )
            db.add(self.device)
            db.flush()
            db.add(
                QueueRecord(
                    inspector_name="Alice",
                    device_id=self.device.id,
                    position=1,
                    created_by_id="alice-browser",
                )
            )
            db.commit()
            db.refresh(self.device)
            self.device_id = self.device.id
            self.device_code = self.device.device_code
        finally:
            db.close()

    def tearDown(self) -> None:
        db = self.SessionLocal()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()
        finally:
            db.close()

    def _report(self, *, report_id, status, progress, db):
        return asyncio.run(
            report_device_status(
                self.device_code,
                StatusReport(
                    report_id=report_id,
                    reported_at=datetime.now(timezone.utc),
                    status=status,
                    task_id="task-1",
                    task_key="task-1",
                    task_name="task-1",
                    task_progress=progress,
                ),
                db=db,
            )
        )

    def test_parallel_duplicate_completion_is_processed_once(self):
        db = self.SessionLocal()
        try:
            self._report(
                report_id=uuid4(),
                status=SchemaDeviceStatus.BUSY,
                progress=20,
                db=db,
            )
        finally:
            db.close()

        completion_report_id = uuid4()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker() -> None:
            worker_db = self.SessionLocal()
            try:
                barrier.wait(timeout=5)
                results.append(
                    self._report(
                        report_id=completion_report_id,
                        status=SchemaDeviceStatus.IDLE,
                        progress=100,
                        db=worker_db,
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted by parent thread
                errors.append(exc)
            finally:
                worker_db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sorted(result.data["duplicate"] for result in results),
            [False, True],
        )

        db = self.SessionLocal()
        try:
            self.assertEqual(
                db.query(DeviceStatusReport)
                .filter_by(device_id=self.device_id)
                .count(),
                2,
            )
            self.assertEqual(
                db.query(DeviceStateEvent)
                .filter_by(
                    device_id=self.device_id,
                    event_type=EVENT_TASK_COMPLETE,
                )
                .count(),
                1,
            )
            self.assertEqual(
                db.query(DeviceStatusHistory)
                .filter_by(device_id=self.device_id)
                .count(),
                1,
            )
            self.assertEqual(
                db.query(QueueRecord)
                .filter_by(device_id=self.device_id, status=TaskStatus.COMPLETED)
                .count(),
                1,
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
