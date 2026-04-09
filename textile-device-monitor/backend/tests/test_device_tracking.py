from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.services.device_tracking import (
    EVENT_DEVICE_OFFLINE,
    EVENT_STATUS,
    StateEventSnapshot,
    TaskStateSnapshot,
    advance_task_state,
    calculate_utilization,
    get_window_bounds,
)


class DeviceTrackingTests(unittest.TestCase):
    def test_reconnect_without_observed_progress_does_not_complete(self):
        current = TaskStateSnapshot(
            task_key="task-a",
            task_name="task-a",
            observed_in_progress=False,
            last_status="offline",
            last_progress=0,
        )

        decision = advance_task_state(
            current,
            status="idle",
            task_key="task-a",
            task_name="task-a",
            task_progress=100,
        )

        self.assertFalse(decision.emit_task_start)
        self.assertFalse(decision.allow_completion)

    def test_reconnect_after_observed_progress_completes_once(self):
        current = TaskStateSnapshot(
            task_key="task-a",
            task_name="task-a",
            observed_in_progress=True,
            last_status="offline",
            last_progress=20,
        )

        decision = advance_task_state(
            current,
            status="idle",
            task_key="task-a",
            task_name="task-a",
            task_progress=100,
        )
        self.assertTrue(decision.allow_completion)
        self.assertFalse(decision.emit_task_start)

        repeat = advance_task_state(
            decision.next_state,
            status="idle",
            task_key="task-a",
            task_name="task-a",
            task_progress=100,
        )
        self.assertFalse(repeat.allow_completion)

    def test_task_key_change_blocks_completion(self):
        current = TaskStateSnapshot(
            task_key="task-a",
            task_name="task-a",
            observed_in_progress=True,
            last_status="offline",
            last_progress=80,
        )

        decision = advance_task_state(
            current,
            status="idle",
            task_key="task-b",
            task_name="task-b",
            task_progress=100,
        )

        self.assertEqual(decision.next_state.task_key, "task-b")
        self.assertFalse(decision.allow_completion)
        self.assertFalse(decision.emit_task_start)

    def test_in_progress_report_emits_task_start(self):
        decision = advance_task_state(
            TaskStateSnapshot(),
            status="busy",
            task_key="task-a",
            task_name="task-a",
            task_progress=20,
        )

        self.assertTrue(decision.emit_task_start)
        self.assertFalse(decision.allow_completion)
        self.assertTrue(decision.next_state.observed_in_progress)

    def test_utilization_uses_busy_duration(self):
        start_at = datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc)
        end_at = datetime(2026, 4, 9, 9, 40, tzinfo=timezone.utc)
        events = [
            StateEventSnapshot(
                occurred_at=datetime(2026, 4, 9, 8, 10, tzinfo=timezone.utc),
                status="busy",
                event_type=EVENT_STATUS,
            ),
            StateEventSnapshot(
                occurred_at=datetime(2026, 4, 9, 8, 40, tzinfo=timezone.utc),
                status="idle",
                event_type=EVENT_STATUS,
            ),
            StateEventSnapshot(
                occurred_at=datetime(2026, 4, 9, 9, 0, tzinfo=timezone.utc),
                status="offline",
                event_type=EVENT_DEVICE_OFFLINE,
            ),
        ]

        summary = calculate_utilization(
            "offline",
            events,
            start_at=start_at,
            end_at=end_at,
        )

        self.assertEqual(int(summary.busy_seconds), 1800)
        self.assertEqual(int(summary.total_seconds), 6000)
        self.assertAlmostEqual(summary.utilization_rate, 30.0)

    def test_today_window_ends_at_now(self):
        now = datetime(2026, 4, 9, 10, 15, tzinfo=timezone.utc)
        start_at, end_at = get_window_bounds(
            date(2026, 4, 9),
            date(2026, 4, 9),
            now=now,
        )

        self.assertEqual(start_at, datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end_at, now)


if __name__ == "__main__":
    unittest.main()
