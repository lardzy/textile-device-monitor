from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402


ACCOUNT = "legacy-user"


class FakeProcess:
    def __init__(self, lines, exit_code=0):
        self.stdout = iter([json.dumps(item, ensure_ascii=False) + "\n" for item in lines])
        self.stdin = io.StringIO()
        self.exit_code = exit_code
        self.terminated = False

    def wait(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True


def claim_document(account=ACCOUNT):
    return {
        "claimed": True,
        "attempt": {"id": "attempt-1"},
        "operation": {
            "credential": {"account_name": account},
            "request_summary": {
                "target_sample_number": "260187115-2",
                "files": [
                    {
                        "root_id": "records",
                        "relative_path": "260187115.xls",
                    }
                ],
            },
        },
    }


class ApiStub:
    def __init__(self, *, claim=None, stage_responses=None, stage_error=None):
        self.claim = claim or claim_document()
        self.stage_responses = stage_responses or {}
        self.stage_error = stage_error
        self.calls = []

    def __call__(self, api_base, token, method, path, payload):
        self.calls.append((path, payload))
        if path == "/external-bridge/claim":
            return self.claim
        if path.endswith("/stage"):
            if payload["stage"] == self.stage_error:
                raise bridge.BridgeError("simulated stage outage")
            return self.stage_responses.get(payload["stage"], {})
        return {}


class BridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            api_base="http://execution.test/api/execution/v1",
            bridge_id="bridge-test",
            writer="writer.exe",
            fibrecheck_dir="C:/FibreCheck",
        )
        self.root_map = {"records": "C:/records"}

    def run_cycle(self, process, api):
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess,
            "Popen",
            return_value=process,
        ):
            result = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(result, "claimed")

    def test_claim_is_account_scoped_and_mismatch_never_starts_writer(self):
        api = ApiStub(claim=claim_document("another-user"))
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess,
            "Popen",
        ) as popen:
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        popen.assert_not_called()
        self.assertEqual(api.calls[0][1]["account_name"], ACCOUNT)
        self.assertEqual(api.calls[-1][1]["error_code"], "credential_account_mismatch")

    def test_writer_receives_permit_only_after_boundary_is_persisted(self):
        process = FakeProcess(
            [
                {"stage": "authenticated"},
                {"stage": "permission_verified"},
                {"stage": "remote_absence_verified"},
                {"stage": "file_copy_ready"},
                {"stage": "file_copy_started"},
                {"stage": "file_copy_verified"},
                {"stage": "main_record_save_started"},
                {"stage": "main_record_verified"},
                {"stage": "completed"},
                {"receipt": {"target_sample_number": "260187115-2"}},
            ]
        )
        api = ApiStub()
        self.run_cycle(process, api)

        stage_calls = [
            payload["stage"]
            for path, payload in api.calls
            if path.endswith("/stage")
        ]
        self.assertEqual(stage_calls[:5], [
            "authenticated",
            "permission_verified",
            "remote_absence_verified",
            "file_copy_ready",
            "file_copy_started",
        ])
        self.assertEqual(process.stdin.getvalue(), bridge.SIDE_EFFECT_PERMIT + "\n")
        self.assertTrue(any(path.endswith("/complete") for path, _ in api.calls))

    def test_boundary_persistence_failure_never_releases_writer(self):
        process = FakeProcess([{"stage": "file_copy_ready"}], exit_code=1)
        api = ApiStub(stage_error="file_copy_started")
        self.run_cycle(process, api)

        self.assertEqual(process.stdin.getvalue(), "")
        self.assertTrue(process.terminated)
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_ready")
        self.assertEqual(fail["error_code"], "bridge_stage_persistence_failed")

    def test_reconciliation_outcome_uses_last_legal_progress_stage(self):
        process = FakeProcess(
            [
                {"stage": "file_copy_ready"},
                {"stage": "file_copy_started"},
                {"stage": "reconciliation_required"},
                {
                    "receipt": {
                        "error": "main_record_save_failed",
                        "failure_stage": "file_copy_started",
                        "reconciliation_required": True,
                    }
                },
            ],
            exit_code=30,
        )
        api = ApiStub()
        self.run_cycle(process, api)
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_started")
        self.assertNotIn(
            "reconciliation_required",
            [
                payload.get("stage")
                for path, payload in api.calls
                if path.endswith("/stage")
            ],
        )

    def test_abort_before_boundary_terminates_without_permit(self):
        process = FakeProcess([{"stage": "file_copy_ready"}], exit_code=1)
        api = ApiStub(
            stage_responses={"file_copy_ready": {"abort_requested": True}},
        )
        self.run_cycle(process, api)
        self.assertTrue(process.terminated)
        self.assertEqual(process.stdin.getvalue(), "")
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_ready")
        self.assertEqual(fail["error_code"], "abort_acknowledged")


if __name__ == "__main__":
    unittest.main()
