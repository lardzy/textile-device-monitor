from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.progress_reader import OlympusProgressReader


class _Logger:
    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


def _prepare_start_line() -> str:
    return (
        "01/19/2026 16:59:36.9888430000,"
        "MATLManagerImpl(487),start@CameraImageSourceImpl2:63,[Step],"
        "start() was called."
    )


def _prepared_protocol_line() -> str:
    return (
        "01/19/2026 16:59:36.9943791000,"
        "MATLManagerImpl(2288),getProtocol@CameraImageSourceImpl2:63,[Step],"
        "getProtocol() was called (protocolId=protocol1)."
    )


def _prepare_complete_line() -> str:
    return (
        "01/19/2026 16:59:37.0287915000,"
        "MATLManagerImpl(514),start@CameraImageSourceImpl2:63,[Step],"
        "prepare() was called."
    )


def _matl_started_line() -> str:
    return (
        "01/19/2026 16:59:37.1071377000,"
        "MATLManagerImpl(3768),notifyMATLStarted@RiJMainThread:21,[AppInfo],"
        "sequence:(type=matl; name=New Protocol 01; action=start; id=session)"
    )


def _group_line(index: int, action: str) -> str:
    method = (
        "notifyProtocolGroupStarted"
        if action == "start"
        else "notifyProtocolGroupCompleted"
    )
    return (
        f"01/19/2026 17:00:00.0000000000,{method}@RiJMainThread:21,"
        f"[AppInfo],sequence:(type=group; name=G{index:03d}; "
        f"action={action}; id=group-{index})"
    )


class OlympusGroupTotalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = OlympusProgressReader("/nonexistent.log", _Logger())
        self.reader._initialized = True

    def _start_session(self, group_total: int) -> None:
        self.reader._process_line(_prepare_start_line())
        for _ in range(group_total):
            self.reader._process_line(_prepared_protocol_line())
        self.reader._process_line(_matl_started_line())

    def test_prepared_protocol_count_sets_total_before_first_group(self):
        self._start_session(5)

        metrics = self.reader._build_extra_metrics()["olympus"]

        self.assertEqual(metrics["group_total"], 5)
        self.assertEqual(metrics["group_completed"], 0)
        self.assertEqual(metrics["group_total_source"], "prepared_protocols")
        self.assertEqual(self.reader._calculate_overall_progress(), 0)

    def test_total_stays_fixed_as_groups_complete(self):
        self._start_session(5)
        self.reader._process_line(_group_line(1, "start"))
        self.reader._process_line(_group_line(1, "end"))

        metrics = self.reader._build_extra_metrics()["olympus"]

        self.assertEqual(metrics["group_total"], 5)
        self.assertEqual(metrics["group_completed"], 1)
        self.assertEqual(self.reader._calculate_overall_progress(), 20)

    def test_runtime_protocol_lookups_do_not_change_total(self):
        self._start_session(3)
        self.reader._process_line(
            "01/19/2026 17:00:00.0000000000,MATLManagerImpl(2288),"
            "getProtocol@RiJMainThread:21,[Step],getProtocol() was called."
        )

        metrics = self.reader._build_extra_metrics()["olympus"]

        self.assertEqual(metrics["group_total"], 3)

    def test_overall_progress_does_not_move_backwards_within_a_group(self):
        self._start_session(5)
        self.reader._groups_completed.add("G001")
        self.reader._current_group = "G002"
        self.reader._last_frame_total = 100
        self.reader._current_frame = 100
        high_progress = self.reader._calculate_overall_progress()

        self.reader._current_frame = 5
        lower_frame_progress = self.reader._calculate_overall_progress()

        self.assertEqual(high_progress, 40)
        self.assertEqual(lower_frame_progress, 40)

    def test_current_group_is_not_counted_twice_after_export(self):
        self._start_session(5)
        self.reader._current_group = "G001"
        self.reader._groups_completed.add("G001")
        self.reader._last_frame_total = 100
        self.reader._current_frame = 50

        self.assertEqual(self.reader._calculate_overall_progress(), 10)

    def test_next_session_replaces_previous_total(self):
        self._start_session(5)
        self.reader._finish_acquisition(None)
        self._start_session(2)

        metrics = self.reader._build_extra_metrics()["olympus"]

        self.assertEqual(metrics["group_total"], 2)
        self.assertEqual(metrics["group_completed"], 0)

    def test_initialization_recovers_active_session_before_tail_window(self):
        lines = [_prepare_start_line()]
        lines.extend(_prepared_protocol_line() for _ in range(5))
        lines.append(_prepare_complete_line())
        lines.append(_matl_started_line())
        lines.extend(
            f"01/19/2026 17:00:00.0000000000,filler,{index:06d},"
            + ("x" * 120)
            for index in range(2000)
        )
        lines.append(_group_line(1, "start"))

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "Olympus.log"
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            reader = OlympusProgressReader(str(log_path), _Logger())

            metrics = reader.get_status_snapshot()["extra_metrics"]["olympus"]

        self.assertEqual(metrics["group_total"], 5)
        self.assertEqual(metrics["group_current"], 1)
        self.assertEqual(metrics["group_total_source"], "prepared_protocols")


if __name__ == "__main__":
    unittest.main()
