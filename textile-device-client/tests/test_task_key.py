from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


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


class _RouteSocket:
    def __init__(self, local_ip: str):
        self.local_ip = local_ip
        self.connected_to = None
        self.closed = False

    def connect(self, endpoint) -> None:
        self.connected_to = endpoint

    def getsockname(self):
        return self.local_ip, 50000

    def close(self) -> None:
        self.closed = True


class ClientBaseUrlTests(unittest.TestCase):
    def test_loopback_server_uses_docker_host_gateway(self):
        reader = ProgressReader(
            working_path="",
            logger=_Logger(),
            results_port=9100,
            server_url="http://127.0.0.1:8000",
        )

        self.assertEqual(
            reader.get_client_base_url(),
            "http://host.docker.internal:9100",
        )

    def test_lan_server_uses_source_ip_for_server_route(self):
        route_socket = _RouteSocket("192.168.1.28")
        reader = ProgressReader(
            working_path="",
            logger=_Logger(),
            results_port=9100,
            server_url="http://192.168.1.100:8000",
        )

        with patch("modules.progress_reader.socket.socket", return_value=route_socket):
            base_url = reader.get_client_base_url()

        self.assertEqual(base_url, "http://192.168.1.28:9100")
        self.assertEqual(route_socket.connected_to, ("192.168.1.100", 8000))
        self.assertTrue(route_socket.closed)


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
        reader._task_started = True
        reader._current_output_path = (
            "C:/ProgramData/OLYMPUS/LEXT-OLS50-SW/MicroscopeApp/Temp/image/job-1"
        )

        self.assertEqual(reader.get_task_key(), "job-1")

    def test_olympus_stable_path_uses_normalized_output_path(self):
        reader = OlympusProgressReader(log_path="/nonexistent.log", logger=_Logger())
        reader._initialized = True
        reader._task_started = True
        reader._current_output_path = "/Volumes/data/job-2"
        expected = os.path.normcase(
            os.path.normpath(os.path.abspath("/Volumes/data/job-2"))
        )

        self.assertEqual(reader.get_task_key(), expected)

    def test_olympus_task_key_stays_stable_within_session(self):
        reader = OlympusProgressReader(log_path="/nonexistent.log", logger=_Logger())
        reader._initialized = True
        reader._task_started = True
        reader._current_output_path = (
            "C:/ProgramData/OLYMPUS/LEXT-OLS50-SW/MicroscopeApp/Temp/image/job-1"
        )

        first_key = reader.get_task_key()
        reader._current_output_path = "/Volumes/data/job-1"
        second_key = reader.get_task_key()

        self.assertEqual(first_key, "job-1")
        self.assertEqual(second_key, "job-1")

    def test_olympus_task_key_resets_for_next_session(self):
        reader = OlympusProgressReader(log_path="/nonexistent.log", logger=_Logger())
        reader._initialized = True
        reader._task_started = True
        reader._current_output_path = (
            "C:/ProgramData/OLYMPUS/LEXT-OLS50-SW/MicroscopeApp/Temp/image/job-1"
        )
        self.assertEqual(reader.get_task_key(), "job-1")

        reader._reset_acquisition_state()
        reader._task_started = True
        reader._current_output_path = "/Volumes/data/job-2"
        expected = os.path.normcase(
            os.path.normpath(os.path.abspath("/Volumes/data/job-2"))
        )

        self.assertEqual(reader.get_task_key(), expected)


if __name__ == "__main__":
    unittest.main()
