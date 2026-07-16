from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

db_fd, db_path = tempfile.mkstemp(prefix="textile-monitor-queue-", suffix=".sqlite")
os.close(db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["OCR_ENABLED"] = "false"
os.environ["AREA_ENABLED"] = "false"

from app.api.devices import report_device_status
from app.api.queue import change_queue_position, complete_task, leave_queue
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
from app.tasks.queue_timeout import check_queue_timeouts
from app.websocket.manager import websocket_manager


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

    def test_queue_crud_does_not_commit_caller_transaction(self):
        device = self._create_device()
        records = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )
        queue_id = records[0].id
        self.assertIsNotNone(queue_id)

        self.db.rollback()

        self.assertIsNone(queue_crud.get_queue_record(self.db, queue_id))

    def test_waiting_position_unique_index_rejects_duplicate_position(self):
        from sqlalchemy.exc import IntegrityError

        device = self._create_device()
        self.db.add_all(
            [
                QueueRecord(
                    inspector_name="Alice",
                    device_id=device.id,
                    position=1,
                    status=TaskStatus.WAITING,
                ),
                QueueRecord(
                    inspector_name="Bob",
                    device_id=device.id,
                    position=1,
                    status=TaskStatus.WAITING,
                ),
            ]
        )

        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_stale_reorder_returns_uniform_conflict_snapshot(self):
        device = self._create_device()
        records = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )
        records += queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )
        self.db.commit()
        stale_version = records[0].version

        queue_crud.update_queue_position(
            self.db,
            records[0].id,
            PositionChange(
                new_position=2,
                changed_by="Alice",
                changed_by_id="user-alice",
                version=stale_version,
            ),
        )
        self.db.commit()

        response = asyncio.run(
            change_queue_position(
                records[0].id,
                PositionChange(
                    new_position=1,
                    changed_by="Alice",
                    changed_by_id="user-alice",
                    version=stale_version,
                ),
                db=self.db,
            )
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["code"], "queue_version_conflict")
        self.assertGreater(payload["current_version"], stale_version)
        self.assertEqual(
            [record["position"] for record in payload["queue"]],
            [1, 2],
        )

    def test_reorder_rejects_target_deleted_after_latest_snapshot(self):
        device = self._create_device()
        records = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                copies=3,
                created_by_id="user-alice",
            ),
        )
        self.db.commit()
        moving, stale_target, replacement = records
        moving_version = moving.version
        target_version = stale_target.version

        queue_crud.delete_queue(self.db, stale_target.id, "operator")
        self.db.commit()

        response = asyncio.run(
            change_queue_position(
                moving.id,
                PositionChange(
                    new_position=2,
                    changed_by="Alice",
                    changed_by_id="user-alice",
                    version=moving_version,
                    target_queue_id=stale_target.id,
                    target_version=target_version,
                ),
                db=self.db,
            )
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["code"], "queue_version_conflict")
        self.assertIn("拖动目标", payload["message"])
        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual([record.id for record in waiting], [moving.id, replacement.id])

    def test_legacy_complete_endpoint_is_retired_without_popping_queue(self):
        device = self._create_device()
        record = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )[0]
        self.db.commit()

        with self.assertRaises(HTTPException) as context:
            asyncio.run(complete_task(device.id, db=self.db))

        self.assertEqual(context.exception.status_code, 410)
        self.assertEqual(
            context.exception.detail["code"],
            "queue_completion_endpoint_retired",
        )
        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual([item.id for item in waiting], [record.id])

    def test_leave_after_completion_returns_conflict_without_second_log(self):
        device = self._create_device()
        record = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )[0]
        self.db.commit()
        queue_crud.complete_first_in_queue(self.db, device.id)
        self.db.commit()

        response = asyncio.run(
            leave_queue(record.id, changed_by_id="user-alice", db=self.db)
        )
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["code"], "queue_version_conflict")
        logs = self._get_logs(device.id)
        self.assertEqual(sum(log.change_type == "complete" for log in logs), 1)
        self.assertEqual(sum(log.change_type == "leave" for log in logs), 0)

    def test_reorder_and_delete_keep_positions_contiguous(self):
        device = self._create_device()
        records = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                copies=3,
                created_by_id="user-alice",
            ),
        )
        self.db.commit()

        queue_crud.update_queue_position(
            self.db,
            records[2].id,
            PositionChange(
                new_position=1,
                changed_by="Alice",
                changed_by_id="user-alice",
                version=records[2].version,
            ),
        )
        self.db.commit()
        queue_crud.delete_queue(self.db, records[0].id, "user-alice")
        self.db.commit()

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual([record.position for record in waiting], [1, 2])
        self.assertEqual(len({record.position for record in waiting}), 2)

    def test_compatibility_schema_repairs_duplicates_before_adding_index(self):
        from sqlalchemy import inspect, text

        device = self._create_device()
        self.db.execute(
            text("DROP INDEX uq_queue_records_waiting_device_position")
        )
        self.db.commit()
        self.db.add_all(
            [
                QueueRecord(
                    inspector_name="Alice",
                    device_id=device.id,
                    position=1,
                    status=TaskStatus.WAITING,
                ),
                QueueRecord(
                    inspector_name="Bob",
                    device_id=device.id,
                    position=1,
                    status=TaskStatus.WAITING,
                ),
            ]
        )
        self.db.commit()

        ensure_queue_record_schema()
        self.db.expire_all()

        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual([record.position for record in waiting], [1, 2])
        self.assertIn(
            "uq_queue_records_waiting_device_position",
            {index["name"] for index in inspect(engine).get_indexes("queue_records")},
        )

    def test_timeout_shift_is_committed_before_websocket_broadcast(self):
        device = self._create_device()
        records = queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Alice",
                device_id=device.id,
                created_by_id="user-alice",
            ),
        )
        records += queue_crud.join_queue(
            self.db,
            QueueCreate(
                inspector_name="Bob",
                device_id=device.id,
                created_by_id="user-bob",
            ),
        )
        now = datetime.now(timezone.utc)
        device.queue_timeout_active_id = records[0].id
        device.queue_timeout_started_at = now - timedelta(minutes=10)
        device.queue_timeout_deadline_at = now - timedelta(seconds=1)
        self.db.commit()

        committed_orders = []
        original_broadcast = websocket_manager.broadcast

        async def observe_committed_queue(message):
            if message.get("type") == "queue_update":
                observer = SessionLocal()
                try:
                    committed_orders.append(
                        [
                            record.inspector_name
                            for record in queue_crud.get_queue_by_device(
                                observer, device.id
                            )
                        ]
                    )
                finally:
                    observer.close()

        websocket_manager.broadcast = observe_committed_queue
        try:
            asyncio.run(check_queue_timeouts())
        finally:
            websocket_manager.broadcast = original_broadcast

        self.assertEqual(committed_orders, [["Bob", "Alice"]])
        waiting = queue_crud.get_queue_by_device(self.db, device.id)
        self.assertEqual([record.id for record in waiting], [records[1].id, records[0].id])


if __name__ == "__main__":
    unittest.main()
