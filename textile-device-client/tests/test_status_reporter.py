from __future__ import annotations

from pathlib import Path
from datetime import datetime
import importlib.util
import sys
import types
import unittest
from uuid import UUID


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

from modules import api_client as api_client_module
from modules.api_client import ApiClient
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


class _SuccessHttpResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"success": True, "message": "ok", "data": {"queue_count": 0}}


class _RetrySession:
    def __init__(self):
        self.payloads = []

    def request(self, method, url, json, timeout):
        self.payloads.append(dict(json))
        if len(self.payloads) == 1:
            raise api_client_module.requests.exceptions.Timeout(
                "first request timed out"
            )
        return _SuccessHttpResponse()


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
        UUID(api_client.calls[0]["report_id"])
        reported_at = datetime.fromisoformat(api_client.calls[0]["reported_at"])
        self.assertIsNotNone(reported_at.tzinfo)

    def test_each_sampling_cycle_gets_a_distinct_report_id(self):
        logger = _Logger()
        api_client = _ApiClient([_Response(), _Response()])
        reporter = StatusReporter(
            api_client=api_client,
            progress_reader=_SnapshotProgressReader(),
            metrics_collector=_MetricsCollector(),
            device_code="dev-1",
            logger=logger,
        )

        reporter.report_once()
        reporter.report_once()

        self.assertNotEqual(
            api_client.calls[0]["report_id"],
            api_client.calls[1]["report_id"],
        )

    def test_http_retry_reuses_the_same_report_id_and_timestamp(self):
        logger = _Logger()
        api_client = ApiClient("http://server", logger)
        retry_session = _RetrySession()
        api_client.session = retry_session
        api_client.max_retries = 2

        response = api_client.report_status(
            device_code="dev-1",
            status="idle",
            report_id="760d7727-b8fa-45f8-9492-c91e93a7363b",
            reported_at="2026-07-17T00:00:00+00:00",
        )

        self.assertIsNotNone(response)
        self.assertEqual(len(retry_session.payloads), 2)
        self.assertEqual(
            retry_session.payloads[0]["report_id"],
            retry_session.payloads[1]["report_id"],
        )
        self.assertEqual(
            retry_session.payloads[0]["reported_at"],
            retry_session.payloads[1]["reported_at"],
        )

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
