from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.progress_reader import ProgressReader
from modules.results_server import ResultsHandler, ResultsServer


class _Logger:
    def __init__(self) -> None:
        self.debug_messages = []

    def debug(self, message: str) -> None:
        self.debug_messages.append(message)

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class RecentResultsTests(unittest.TestCase):
    def test_stop_closes_the_http_server_socket(self):
        httpd = Mock()
        server = object.__new__(ResultsServer)
        server.httpd = httpd

        server.stop()

        self.assertIsNone(server.httpd)
        httpd.shutdown.assert_called_once_with()
        httpd.server_close.assert_called_once_with()

    def test_http_access_logging_does_not_require_stderr(self):
        handler = object.__new__(ResultsHandler)
        logger = _Logger()
        handler.logger = logger
        handler.client_address = ("127.0.0.1", 12345)
        original_stderr = sys.stderr
        try:
            sys.stderr = None
            handler.log_message('"%s" %s', "GET /client/results/recent", 200)
        finally:
            sys.stderr = original_stderr

        self.assertEqual(
            logger.debug_messages,
            ['results_http 127.0.0.1 "GET /client/results/recent" 200'],
        )

    def test_recent_results_include_completed_folders_from_previous_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_folder = self._create_result_folder(root, "old-result")
            latest_folder = self._create_result_folder(root, "latest-result")
            now = time.time()
            os.utime(old_folder, (now - 2 * 86400, now - 2 * 86400))
            os.utime(latest_folder, (now, now))

            reader = ProgressReader(str(root), _Logger())
            handler = object.__new__(ResultsHandler)
            handler.reader = reader
            handler.logger = _Logger()
            ResultsHandler.invalidate_recent_cache()

            items = handler._get_recent_results(5)

            self.assertEqual([item["folder"] for item in items], ["old-result"])

    @staticmethod
    def _create_result_folder(root: Path, name: str) -> Path:
        folder = root / name
        result_dir = folder / "result"
        result_dir.mkdir(parents=True)
        (result_dir / f"{name}.xlsx").write_bytes(b"test")
        return folder


if __name__ == "__main__":
    unittest.main()
