from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from scripts.replay_olympus_log import parse_log_timestamp, replay_logs


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ReplayOlympusLogTests(unittest.TestCase):
    def test_parse_log_timestamp_truncates_subseconds_to_microseconds(self):
        timestamp = parse_log_timestamp(
            b"01/19/2026 16:59:10.7250902000,source,message\n"
        )

        self.assertEqual(timestamp, datetime(2026, 1, 19, 16, 59, 10, 725090))

    def test_replay_scales_intervals_without_accumulating_delay(self):
        lines = (
            b"01/19/2026 16:59:10.0000000000,first\n"
            b"01/19/2026 16:59:12.0000000000,second\n"
            b"01/19/2026 16:59:15.0000000000,third\n"
        )
        clock = _Clock()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "archive.log"
            output = root / "live" / "Olympus.log"
            source.write_bytes(lines)

            stats = replay_logs(
                [source],
                output,
                speed=2.0,
                truncate=True,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

            self.assertEqual(output.read_bytes(), lines)

        self.assertEqual(stats.lines_written, 3)
        self.assertEqual(stats.bytes_written, len(lines))
        self.assertEqual(clock.sleeps, [1.0, 1.5])

    def test_replay_can_filter_a_source_time_range(self):
        lines = (
            b"01/19/2026 16:59:10.0000000000,first\n"
            b"01/19/2026 16:59:12.0000000000,second\n"
            b"01/19/2026 16:59:15.0000000000,third\n"
        )
        clock = _Clock()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "archive.log"
            output = root / "Olympus.log"
            source.write_bytes(lines)

            stats = replay_logs(
                [source],
                output,
                speed=100.0,
                truncate=True,
                start_at=datetime(2026, 1, 19, 16, 59, 12),
                end_at=datetime(2026, 1, 19, 16, 59, 12),
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

            self.assertEqual(
                output.read_bytes(),
                b"01/19/2026 16:59:12.0000000000,second\n",
            )
            self.assertEqual(stats.lines_written, 1)


if __name__ == "__main__":
    unittest.main()
