from __future__ import annotations

from pathlib import Path
import importlib.util
import sys
import types
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

if importlib.util.find_spec("requests") is None:
    requests_stub = types.ModuleType("requests")
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=TimeoutError,
        ConnectionError=ConnectionError,
    )
    requests_stub.Session = object
    sys.modules["requests"] = requests_stub

if importlib.util.find_spec("psutil") is None:
    sys.modules["psutil"] = types.ModuleType("psutil")

from modules.status_reporter import StatusReporter


class _Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []
        self.infos = []

    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


class _MetricsCollector:
    def collect_metrics(self):
        return {"cpu": 1, "memory": 2}


class _Response:
    data = {"queue_count": 0}


class _ApiClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_request_info = {}

    def report_status(self, **payload):
        self.calls.append(payload)
        self.last_request_info = {
            "attempts": 1,
            "elapsed_seconds": 0.01,
            "success": bool(self.responses[0]),
        }
        return self.responses.pop(0)


class _SnapshotProgressReader:
    is_laser_confocal = False

    def __init__(self):
        self.snapshot_calls = 0

    def get_status_snapshot(self):
        self.snapshot_calls += 1
        return {
            "task_progress": 100,
            "latest_folder_name": "task-a",
            "task_key": "/data/task-a",
            "client_base_url": "http://127.0.0.1:9100",
            "extra_metrics": {},
            "device_state": None,
            "task_active": False,
        }


class StatusReporterTests(unittest.TestCase):
    def test_report_uses_single_progress_snapshot(self):
        logger = _Logger()
        reader = _SnapshotProgressReader()
        api_client = _ApiClient([_Response()])
        reporter = StatusReporter(
            api_client=api_client,
            progress_reader=reader,
            metrics_collector=_MetricsCollector(),
            device_code="dev-1",
            logger=logger,
        )

        reporter.report_once()

        self.assertEqual(reader.snapshot_calls, 1)
        self.assertEqual(api_client.calls[0]["status"], "idle")
        self.assertEqual(api_client.calls[0]["task_progress"], 100)
        self.assertEqual(api_client.calls[0]["task_key"], "/data/task-a")

    def test_report_logs_recovery_after_consecutive_failures(self):
        logger = _Logger()
        api_client = _ApiClient([None, None, _Response()])
        reporter = StatusReporter(
            api_client=api_client,
            progress_reader=_SnapshotProgressReader(),
            metrics_collector=_MetricsCollector(),
            device_code="dev-1",
            logger=logger,
        )

        reporter.report_once()
        reporter.report_once()
        reporter.report_once()

        self.assertTrue(any("状态上报已恢复" in item for item in logger.warnings))


if __name__ == "__main__":
    unittest.main()
