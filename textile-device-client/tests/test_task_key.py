from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.progress_reader import OlympusProgressReader, ProgressReader


class _Logger:
    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class TaskKeyTests(unittest.TestCase):
    def test_progress_reader_uses_latest_folder_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample-001"
            target.mkdir()

            reader = ProgressReader(str(root), _Logger())
            expected = os.path.normcase(os.path.normpath(os.path.abspath(str(target))))

            self.assertEqual(reader.get_task_key(), expected)

    def test_olympus_temp_path_falls_back_to_folder_name(self):
        reader = OlympusProgressReader(log_path="/nonexistent.log", logger=_Logger())
        reader._initialized = True
        reader._current_output_path = (
            "C:/ProgramData/OLYMPUS/LEXT-OLS50-SW/MicroscopeApp/Temp/image/job-1"
        )

        self.assertEqual(reader.get_task_key(), "job-1")

    def test_olympus_stable_path_uses_normalized_output_path(self):
        reader = OlympusProgressReader(log_path="/nonexistent.log", logger=_Logger())
        reader._initialized = True
        reader._current_output_path = "/Volumes/data/job-2"
        expected = os.path.normcase(
            os.path.normpath(os.path.abspath("/Volumes/data/job-2"))
        )

        self.assertEqual(reader.get_task_key(), expected)


if __name__ == "__main__":
    unittest.main()
