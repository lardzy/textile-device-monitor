from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.stats import get_statistics_trend
from app.config import settings
from app.crud import stats as stats_crud
from app.models import (
    Base,
    Device,
    DeviceStateEvent,
    DeviceStatus,
    DeviceStatusHistory,
)
from app.services.device_tracking import EVENT_TASK_COMPLETE, EVENT_TASK_START


class StatsTrendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:")
        cls.SessionLocal = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.create_all(bind=self.engine)
        self.db = self.SessionLocal()
        self.original_timezone = settings.STATS_TIMEZONE
        settings.STATS_TIMEZONE = "Asia/Shanghai"

    def tearDown(self):
        settings.STATS_TIMEZONE = self.original_timezone
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _create_device(
        self,
        code: str,
        *,
        status: DeviceStatus = DeviceStatus.IDLE,
    ) -> Device:
        device = Device(device_code=code, name=code, status=status)
        self.db.add(device)
        self.db.commit()
        self.db.refresh(device)
        return device

    def _add_event(
        self,
        device: Device,
        occurred_at: datetime,
        *,
        event_type: str,
        status: str,
    ) -> None:
        self.db.add(
            DeviceStateEvent(
                device_id=device.id,
                event_type=event_type,
                status=status,
                occurred_at=occurred_at,
            )
        )
        self.db.commit()

    def _add_duration(
        self,
        device: Device,
        reported_at: datetime,
        duration_seconds: int,
    ) -> None:
        self.db.add(
            DeviceStatusHistory(
                device_id=device.id,
                status="idle",
                task_progress=100,
                task_duration_seconds=duration_seconds,
                reported_at=reported_at,
            )
        )
        self.db.commit()

    def test_daily_trend_uses_events_timezone_and_current_time_cutoff(self):
        device = self._create_device("daily-device")
        self._add_event(
            device,
            datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc),
            event_type="status",
            status="idle",
        )
        self._add_event(
            device,
            datetime(2026, 6, 30, 17, 0, tzinfo=timezone.utc),
            event_type=EVENT_TASK_START,
            status="busy",
        )
        self._add_event(
            device,
            datetime(2026, 6, 30, 19, 0, tzinfo=timezone.utc),
            event_type=EVENT_TASK_COMPLETE,
            status="idle",
        )
        self._add_duration(
            device,
            datetime(2026, 6, 30, 19, 0, tzinfo=timezone.utc),
            120,
        )
        self._add_duration(
            device,
            datetime(2026, 6, 30, 20, 0, tzinfo=timezone.utc),
            180,
        )

        result = stats_crud.get_trend_stats(
            self.db,
            stat_type="daily",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            device_id=device.id,
            now=datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["period_start"], "2026-07-01T00:00:00+08:00")
        self.assertEqual(item["period_end"], "2026-07-01T12:00:00+08:00")
        self.assertEqual(item["total_tasks"], 1)
        self.assertEqual(item["completed_tasks"], 1)
        self.assertEqual(item["avg_duration_seconds"], 150)
        self.assertEqual(item["busy_seconds"], 7200)
        self.assertEqual(item["total_seconds"], 43200)
        self.assertEqual(item["utilization_rate"], 16.67)

    def test_all_devices_aggregate_device_time_denominator(self):
        first = self._create_device("aggregate-1")
        second = self._create_device("aggregate-2")
        for device in (first, second):
            self._add_event(
                device,
                datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc),
                event_type="status",
                status="idle",
            )
        self._add_event(
            first,
            datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc),
            event_type=EVENT_TASK_START,
            status="busy",
        )
        self._add_event(
            first,
            datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
            event_type=EVENT_TASK_COMPLETE,
            status="idle",
        )
        self._add_event(
            second,
            datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc),
            event_type="status",
            status="busy",
        )
        self._add_event(
            second,
            datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc),
            event_type="status",
            status="idle",
        )

        result = stats_crud.get_trend_stats(
            self.db,
            stat_type="daily",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            now=datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc),
        )
        item = result["items"][0]

        self.assertEqual(item["total_seconds"], 86400)
        self.assertEqual(item["busy_seconds"], 28800)
        self.assertEqual(item["utilization_rate"], 33.33)

    def test_weekly_and_monthly_periods_use_natural_boundaries(self):
        self._create_device("period-device")
        weekly = stats_crud.get_trend_stats(
            self.db,
            stat_type="weekly",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 19),
            now=datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
        )
        partial_week = stats_crud.get_trend_stats(
            self.db,
            stat_type="weekly",
            start_date=date(2026, 7, 8),
            end_date=date(2026, 7, 12),
            now=datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
        )
        monthly = stats_crud.get_trend_stats(
            self.db,
            stat_type="monthly",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 7, 31),
            now=datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [item["period_start"] for item in weekly["items"]],
            ["2026-07-06T00:00:00+08:00", "2026-07-13T00:00:00+08:00"],
        )
        self.assertEqual(
            [item["bucket_start"] for item in weekly["items"]],
            ["2026-07-06T00:00:00+08:00", "2026-07-13T00:00:00+08:00"],
        )
        self.assertEqual(
            partial_week["items"][0]["period_start"],
            "2026-07-08T00:00:00+08:00",
        )
        self.assertEqual(
            partial_week["items"][0]["bucket_start"],
            "2026-07-06T00:00:00+08:00",
        )
        self.assertEqual(
            [item["period_start"] for item in monthly["items"]],
            ["2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00"],
        )
        self.assertEqual(
            monthly["items"][-1]["period_end"],
            "2026-07-15T12:00:00+08:00",
        )

    def test_trend_endpoint_rejects_invalid_input_and_missing_device(self):
        with self.assertRaises(HTTPException) as invalid_type:
            get_statistics_trend(
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                stat_type="yearly",
                device_id=None,
                db=self.db,
            )
        self.assertEqual(invalid_type.exception.status_code, 422)

        with self.assertRaises(HTTPException) as invalid_range:
            get_statistics_trend(
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 1),
                stat_type="daily",
                device_id=None,
                db=self.db,
            )
        self.assertEqual(invalid_range.exception.status_code, 422)

        with self.assertRaises(HTTPException) as missing_device:
            get_statistics_trend(
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                stat_type="daily",
                device_id=999,
                db=self.db,
            )
        self.assertEqual(missing_device.exception.status_code, 404)

    def test_realtime_stats_keep_statuses_distinct_and_alias_completed_count(self):
        for status in (
            DeviceStatus.IDLE,
            DeviceStatus.BUSY,
            DeviceStatus.MAINTENANCE,
            DeviceStatus.ERROR,
            DeviceStatus.OFFLINE,
        ):
            self._create_device(f"status-{status.value}", status=status)

        today = datetime.now(timezone.utc)
        device = self.db.query(Device).first()
        self._add_duration(device, today - timedelta(minutes=2), 60)
        self._add_duration(device, today - timedelta(minutes=1), 90)

        result = stats_crud.get_realtime_stats(self.db)

        self.assertEqual(result["total_devices"], 5)
        self.assertEqual(result["online_devices"], 4)
        self.assertEqual(result["idle_devices"], 1)
        self.assertEqual(result["busy_devices"], 1)
        self.assertEqual(result["maintenance_devices"], 1)
        self.assertEqual(result["error_devices"], 1)
        self.assertEqual(result["offline_devices"], 1)
        self.assertEqual(result["today_reports"], 2)
        self.assertEqual(result["today_completed_tasks"], 2)


if __name__ == "__main__":
    unittest.main()
