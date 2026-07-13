from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

db_fd, db_path = tempfile.mkstemp(prefix="textile-monitor-queue-", suffix=".sqlite")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["OCR_ENABLED"] = "false"
os.environ["AREA_ENABLED"] = "false"

from app.api.devices import report_device_status
from app.crud import queue as queue_crud
from app.database import SessionLocal, engine, ensure_queue_record_schema
from app.models import Base, Device, DeviceStatus, QueueChangeLog, QueueRecord, TaskStatus
from app.schemas import (
    DeviceStatus as SchemaDeviceStatus,
    PositionChange,
    QueueClaimRequest,
    QueueCreate,
    StatusReport,
)


class QueuePlaceholderTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        ensure_queue_record_schema()
        self.db = SessionLocal()
        self.device_counter = 0

    def tearDown(self):
        self.db.close()

    def _create_device(self, *, status: DeviceStatus = DeviceStatus.IDLE) -> Device:
        self.device_counter += 1
        device = Device(
            device_code=f"dev-{self.device_counter}",
            name=f"设备-{self.device_counter}",
            model="model",
            location="lab",
            status=status,
        )
        self.db.add(device)
        self.db.commit()
        self.db.refresh(device)
        return device

    def _get_logs(self, device_id: int) -> list[QueueChangeLog]:
        return (
            self.db.query(QueueChangeLog)
            .join(QueueRecord, QueueChangeLog.queue_id == QueueRecord.id)
            .filter(QueueRecord.device_id == device_id)
            .order_by(QueueChangeLog.id.asc())
            .all()
        )

    def test_idle_to_busy_creates_single_placeholder_once(self):
        device = self._create_device(status=DeviceStatus.IDLE)

        asyncio.run(
            report_device_status(
                device.device_code,
                StatusReport(
                    status=SchemaDeviceStatus.BUSY,
                    task_id="task-1",
                    task_key="task-1",
                    task_name="task-1",
                    task_progress=20,
                    metrics={"temperature": 25},
                ),
                db=self.db,
            )
        )

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        self.assertTrue(waiting[0].is_placeholder)
        self.assertTrue(waiting[0].auto_remove_when_inactive)
        self.assertEqual(waiting[0].inspector_name, queue_crud.PLACEHOLDER_NAME)

        asyncio.run(
            report_device_status(
                device.device_code,
                StatusReport(
                    status=SchemaDeviceStatus.BUSY,
                    task_id="task-1",
                    task_key="task-1",
                    task_name="task-1",
                    task_progress=40,
                    metrics={"temperature": 25},
                ),
                db=self.db,
            )
        )

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        logs = self._get_logs(device.id)
        self.assertEqual(
            sum(1 for log in logs if log.change_type == "placeholder_create"),
            1,
        )

    def test_existing_queue_blocks_placeholder_creation(self):
        device = self._create_device(status=DeviceStatus.IDLE)
        queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )

        asyncio.run(
            report_device_status(
                device.device_code,
                StatusReport(
                    status=SchemaDeviceStatus.BUSY,
                    task_id="task-2",
                    task_key="task-2",
                    task_name="task-2",
                    task_progress=10,
                ),
                db=self.db,
            )
        )

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        self.assertFalse(waiting[0].is_placeholder)
        self.assertEqual(
            sum(1 for log in self._get_logs(device.id) if log.change_type == "placeholder_create"),
            0,
        )

    def test_claimed_placeholder_is_not_auto_removed_after_manual_reorder(self):
        device = self._create_device()
        placeholder = queue_crud.create_placeholder_if_missing(self.db, device.id)
        self.assertIsNotNone(placeholder)

        claimed = queue_crud.claim_placeholder(
            self.db,
            placeholder.id,
            QueueClaimRequest(
                inspector_name="Alice",
                claimed_by_id="user-alice",
            ),
        )
        self.assertIsNotNone(claimed)

        queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )

        claimed = queue_crud.get_queue_record(self.db, placeholder.id)
        self.assertIsNotNone(claimed)
        queue_crud.update_queue_position(
            self.db,
            claimed.id,
            PositionChange(
                new_position=2,
                changed_by="Bob",
                version=claimed.version,
                changed_by_id="user-bob",
            ),
        )

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        waiting_ids = {record.id for record in waiting}
        self.assertEqual(len(waiting), 2)
        self.assertIn(placeholder.id, waiting_ids)
        self.assertEqual(
            next(record.position for record in waiting if record.id == placeholder.id),
            2,
        )
        self.assertEqual(
            sum(1 for log in self._get_logs(device.id) if log.change_type == "placeholder_auto_remove"),
            0,
        )

    def test_unclaimed_placeholder_auto_removed_after_manual_reorder(self):
        device = self._create_device()
        placeholder = queue_crud.create_placeholder_if_missing(self.db, device.id)
        self.assertIsNotNone(placeholder)
        queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )

        queue_crud.update_queue_position(
            self.db,
            placeholder.id,
            PositionChange(
                new_position=2,
                changed_by="Bob",
                version=placeholder.version,
                changed_by_id="user-bob",
            ),
        )

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].inspector_name, "Bob")
        self.assertEqual(waiting[0].position, 1)
        placeholder_record = queue_crud.get_queue_record(self.db, placeholder.id)
        self.assertIsNotNone(placeholder_record)
        self.assertEqual(placeholder_record.status, TaskStatus.COMPLETED)
        self.assertEqual(
            sum(1 for log in self._get_logs(device.id) if log.change_type == "placeholder_auto_remove"),
            1,
        )

    def test_unclaimed_placeholder_auto_removed_after_timeout_shift(self):
        device = self._create_device()
        placeholder = queue_crud.create_placeholder_if_missing(self.db, device.id)
        self.assertIsNotNone(placeholder)
        queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )

        result = queue_crud.swap_first_two_in_queue(
            self.db,
            device.id,
            changed_by="系统",
            changed_by_id=None,
        )

        self.assertIsNotNone(result)
        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].inspector_name, "Bob")
        logs = self._get_logs(device.id)
        self.assertEqual(
            sum(1 for log in logs if log.change_type == "timeout_shift"),
            1,
        )
        self.assertEqual(
            sum(1 for log in logs if log.change_type == "placeholder_auto_remove"),
            1,
        )

    def test_delete_unclaimed_placeholder_writes_dedicated_log_and_reorders(self):
        device = self._create_device()
        placeholder = queue_crud.create_placeholder_if_missing(self.db, device.id)
        self.assertIsNotNone(placeholder)
        queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )

        deleted = queue_crud.delete_queue(
            self.db,
            placeholder.id,
            changed_by_id="operator-1",
        )

        self.assertIsNotNone(deleted)
        self.assertEqual(deleted.status, TaskStatus.COMPLETED)
        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].inspector_name, "Bob")
        self.assertEqual(waiting[0].position, 1)
        self.assertEqual(
            sum(1 for log in self._get_logs(device.id) if log.change_type == "placeholder_delete"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
