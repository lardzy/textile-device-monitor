from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.devices import report_device_status
from app.crud import device_tracking as tracking_crud
from app.models import (
    Base,
    Device,
    DeviceStateEvent,
    DeviceStatus,
    DeviceStatusHistory,
    DeviceStatusReport,
    DeviceTaskState,
    QueueChangeLog,
    QueueRecord,
    TaskStatus,
)
from app.schemas import DeviceStatus as SchemaDeviceStatus, StatusReport
from app.services.device_tracking import EVENT_TASK_COMPLETE, EVENT_TASK_START


class StatusReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine("sqlite:///:memory:")
        cls.SessionLocal = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        Base.metadata.create_all(bind=self.engine)
        self.db = self.SessionLocal()
        self.device = Device(
            device_code="status-device",
            name="状态设备",
            status=DeviceStatus.IDLE,
        )
        self.db.add(self.device)
        self.db.commit()
        self.db.refresh(self.device)

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _add_waiting_user(self) -> QueueRecord:
        record = QueueRecord(
            inspector_name="Alice",
            device_id=self.device.id,
            position=1,
            created_by_id="alice-browser",
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def _report(
        self,
        *,
        report_id,
        status: SchemaDeviceStatus,
        progress: int,
    ):
        return asyncio.run(
            report_device_status(
                self.device.device_code,
                StatusReport(
                    report_id=report_id,
                    reported_at=datetime.now(timezone.utc),
                    status=status,
                    task_id="task-1",
                    task_key="task-1",
                    task_name="task-1",
                    task_progress=progress,
                ),
                db=self.db,
            )
        )

    def test_duplicate_completion_report_is_settled_exactly_once(self):
        queue_record = self._add_waiting_user()
        self._report(
            report_id=uuid4(),
            status=SchemaDeviceStatus.BUSY,
            progress=20,
        )
        completion_report_id = uuid4()

        messages = []
        with patch(
            "app.api.devices.schedule_websocket_broadcast",
            side_effect=messages.append,
        ):
            first = self._report(
                report_id=completion_report_id,
                status=SchemaDeviceStatus.IDLE,
                progress=100,
            )
            duplicate = self._report(
                report_id=completion_report_id,
                status=SchemaDeviceStatus.IDLE,
                progress=100,
            )
        distinct_repeat = self._report(
            report_id=uuid4(),
            status=SchemaDeviceStatus.IDLE,
            progress=100,
        )

        self.assertFalse(first.data["duplicate"])
        self.assertTrue(duplicate.data["duplicate"])
        self.assertFalse(distinct_repeat.data["duplicate"])
        completion_message = next(
            message
            for message in messages
            if message["type"] == "queue_update"
            and message["data"]["action"] == "complete"
        )
        status_message = next(
            message for message in messages if message["type"] == "device_status_update"
        )
        self.assertEqual(
            completion_message["data"]["report_id"],
            str(completion_report_id),
        )
        self.assertEqual(
            completion_message["data"]["report_id"],
            status_message["data"]["report_id"],
        )
        # The duplicate request itself must not emit another WebSocket event.
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            self.db.query(DeviceStatusReport).filter_by(device_id=self.device.id).count(),
            3,
        )
        self.assertEqual(
            self.db.query(DeviceStateEvent)
            .filter_by(device_id=self.device.id, event_type=EVENT_TASK_START)
            .count(),
            1,
        )
        self.assertEqual(
            self.db.query(DeviceStateEvent)
            .filter_by(device_id=self.device.id, event_type=EVENT_TASK_COMPLETE)
            .count(),
            1,
        )
        self.assertEqual(
            self.db.query(DeviceStatusHistory).filter_by(device_id=self.device.id).count(),
            1,
        )
        self.db.refresh(queue_record)
        self.assertEqual(queue_record.status, TaskStatus.COMPLETED)
        self.assertEqual(
            self.db.query(QueueChangeLog).filter_by(queue_id=queue_record.id).count(),
            1,
        )

    def test_failure_rolls_back_receipt_state_event_history_and_queue(self):
        queue_record = self._add_waiting_user()
        self._report(
            report_id=uuid4(),
            status=SchemaDeviceStatus.BUSY,
            progress=20,
        )
        completion_report_id = uuid4()

        with patch(
            "app.crud.queue.complete_first_in_queue",
            side_effect=RuntimeError("simulated queue failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated queue failure"):
                self._report(
                    report_id=completion_report_id,
                    status=SchemaDeviceStatus.IDLE,
                    progress=100,
                )

        self.db.expire_all()
        device = self.db.get(Device, self.device.id)
        task_state = self.db.get(DeviceTaskState, self.device.id)
        queue_record = self.db.get(QueueRecord, queue_record.id)
        self.assertEqual(device.status, DeviceStatus.BUSY)
        self.assertEqual(device.task_progress, 20)
        self.assertTrue(task_state.observed_in_progress)
        self.assertEqual(task_state.last_progress, 20)
        self.assertEqual(queue_record.status, TaskStatus.WAITING)
        self.assertEqual(
            self.db.query(DeviceStatusReport)
            .filter_by(
                device_id=self.device.id,
                report_id=str(completion_report_id),
            )
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(DeviceStateEvent)
            .filter_by(device_id=self.device.id, event_type=EVENT_TASK_COMPLETE)
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(DeviceStatusHistory).filter_by(device_id=self.device.id).count(),
            0,
        )

        retried = self._report(
            report_id=completion_report_id,
            status=SchemaDeviceStatus.IDLE,
            progress=100,
        )
        self.assertFalse(retried.data["duplicate"])
        self.assertEqual(
            self.db.query(DeviceStateEvent)
            .filter_by(device_id=self.device.id, event_type=EVENT_TASK_COMPLETE)
            .count(),
            1,
        )

    def test_websocket_uses_corrected_committed_snapshot(self):
        self._add_waiting_user()
        self._report(
            report_id=uuid4(),
            status=SchemaDeviceStatus.BUSY,
            progress=60,
        )

        messages = []
        with patch(
            "app.api.devices.schedule_websocket_broadcast",
            side_effect=messages.append,
        ):
            self._report(
                report_id=uuid4(),
                status=SchemaDeviceStatus.IDLE,
                progress=40,
            )

        status_message = next(
            message for message in messages if message["type"] == "device_status_update"
        )
        self.assertEqual(status_message["data"]["status"], "busy")
        self.assertEqual(status_message["data"]["task_progress"], 60)
        self.db.expire_all()
        device = self.db.get(Device, self.device.id)
        self.assertEqual(device.status, DeviceStatus.BUSY)
        self.assertEqual(device.task_progress, 60)

    def test_legacy_report_without_idempotency_fields_remains_valid(self):
        report = StatusReport(status=SchemaDeviceStatus.IDLE)
        self.assertIsNone(report.report_id)
        self.assertIsNone(report.reported_at)

    def test_reported_at_requires_timezone(self):
        with self.assertRaises(ValidationError):
            StatusReport(
                status=SchemaDeviceStatus.IDLE,
                reported_at=datetime(2026, 7, 17, 8, 0),
            )

    def test_expired_idempotency_receipts_are_bounded_by_cleanup(self):
        now = datetime.now(timezone.utc)
        self.db.add_all(
            [
                DeviceStatusReport(
                    device_id=self.device.id,
                    report_id=str(uuid4()),
                    reported_at=now - timedelta(hours=72),
                    processed_at=now - timedelta(hours=72),
                ),
                DeviceStatusReport(
                    device_id=self.device.id,
                    report_id=str(uuid4()),
                    reported_at=now,
                    processed_at=now,
                ),
            ]
        )
        self.db.commit()

        deleted = tracking_crud.delete_status_report_receipts_before(
            self.db,
            now - timedelta(hours=48),
        )
        self.db.commit()

        self.assertEqual(deleted, 1)
        self.assertEqual(self.db.query(DeviceStatusReport).count(), 1)


if __name__ == "__main__":
    unittest.main()
