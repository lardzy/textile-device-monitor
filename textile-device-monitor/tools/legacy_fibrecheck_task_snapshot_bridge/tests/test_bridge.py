from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402


INSPECTION_NUMBER = "26A029794"


def probe_document(*, tasks=None, items=None):
    task_rows = (
        [
            {
                "ID": "task-1",
                "ReportNo": INSPECTION_NUMBER,
                "CheckBasis": "---",
            }
        ]
        if tasks is None
        else tasks
    )
    item_rows = (
        [
            {
                "ID": "item-1",
                "TaskID": "task-1",
                "CheckItemNo": "5103.5",
                "CheckItemName": "纤维微观形貌",
                "CheckMethod": "GB/T 36422-2018",
                "CheckCount": 1,
                "SampleIdentify": None,
                "Remark": "内部备注",
                "GiveJudgement": 0,
            }
        ]
        if items is None
        else items
    )
    return {
        "schema_version": 1,
        "mode": "probe",
        "sample_no": INSPECTION_NUMBER,
        "connection_attempted": True,
        "read_only_transaction_started": True,
        "results": {
            "tasks": {"status": "ok", "row_count": len(task_rows), "rows": task_rows},
            "task_check_items": {
                "status": "ok",
                "row_count": len(item_rows),
                "rows": item_rows,
            },
        },
    }


def args_fixture():
    return SimpleNamespace(
        api_base="http://execution.test/api/execution/v1",
        bridge_id="snapshot-bridge-test",
        probe_python="python.exe",
        probe_script="probe.py",
        fibrecheck_dir="C:/FibreCheck",
        oracle_client_dir="C:/oracle",
        credential_profile="WebService.dll.config:PanYuJianWu",
        data_source="192.168.105.106/orcl",
        probe_timeout_seconds=30.0,
    )


class ApiStub:
    def __init__(self, claim):
        self.claim = claim
        self.calls = []

    def __call__(self, api_base, token, method, path, payload):
        self.calls.append((path, payload))
        if path == "/task-snapshot-bridge/claim":
            return self.claim
        return {}


class SnapshotMappingTests(unittest.TestCase):
    def test_maps_only_backend_contract_fields_and_matching_task(self):
        document = probe_document(
            items=probe_document()["results"]["task_check_items"]["rows"]
            + [
                {
                    "TaskID": "another-task",
                    "CheckItemName": "不应进入快照",
                    "CheckMethod": "OTHER",
                }
            ]
        )
        snapshot = bridge.build_snapshot(document, INSPECTION_NUMBER)
        self.assertEqual(
            snapshot,
            {
                "sample_name": None,
                "check_basis": "---",
                "projects": [
                    {
                        "check_item_no": "5103.5",
                        "check_item_name": "纤维微观形貌",
                        "check_method": "GB/T 36422-2018",
                        "check_count": 1,
                        "sample_identify": None,
                        "remark": "内部备注",
                        "give_judgement": 0,
                    }
                ],
            },
        )

    def test_missing_task_is_a_valid_empty_snapshot(self):
        snapshot = bridge.build_snapshot(
            probe_document(tasks=[], items=[]),
            INSPECTION_NUMBER,
        )
        self.assertEqual(
            snapshot,
            {"sample_name": None, "check_basis": None, "projects": []},
        )

    def test_duplicate_task_is_rejected(self):
        task = probe_document()["results"]["tasks"]["rows"][0]
        with self.assertRaisesRegex(bridge.SnapshotBridgeError, "多个任务"):
            bridge.build_snapshot(probe_document(tasks=[task, dict(task)]), INSPECTION_NUMBER)

    def test_query_error_is_rejected(self):
        document = probe_document()
        document["results"]["task_check_items"]["status"] = "query_error"
        with self.assertRaisesRegex(bridge.SnapshotBridgeError, "必要查询"):
            bridge.build_snapshot(document, INSPECTION_NUMBER)


class CycleTests(unittest.TestCase):
    def test_idle_claim_does_not_run_probe(self):
        api = ApiStub({"claimed": False})
        with patch.object(bridge, "run_probe") as probe:
            outcome = bridge.run_one_cycle(
                args_fixture(),
                "secret-token",
                request=api,
                probe=probe,
            )
        self.assertEqual(outcome, "idle")
        probe.assert_not_called()

    def test_success_completes_claim(self):
        api = ApiStub(
            {
                "claimed": True,
                "inspection_number": INSPECTION_NUMBER,
                "claim_token": "claim-secret",
            }
        )
        outcome = bridge.run_one_cycle(
            args_fixture(),
            "secret-token",
            request=api,
            probe=lambda *_: probe_document(),
        )
        self.assertEqual(outcome, "completed")
        complete_path, complete_payload = api.calls[-1]
        self.assertEqual(
            complete_path,
            f"/task-snapshot-bridge/{INSPECTION_NUMBER}/complete",
        )
        self.assertEqual(complete_payload["bridge_id"], "snapshot-bridge-test")
        self.assertEqual(complete_payload["claim_token"], "claim-secret")
        self.assertEqual(
            complete_payload["snapshot"]["projects"][0]["check_item_name"],
            "纤维微观形貌",
        )

    def test_probe_failure_reports_fail(self):
        api = ApiStub(
            {
                "claimed": True,
                "inspection_number": INSPECTION_NUMBER,
                "claim_token": "claim-secret",
            }
        )

        def fail_probe(*_):
            raise bridge.SnapshotBridgeError("probe_failed", "只读探针执行失败")

        outcome = bridge.run_one_cycle(
            args_fixture(),
            "secret-token",
            request=api,
            probe=fail_probe,
        )
        self.assertEqual(outcome, "failed")
        fail_path, fail_payload = api.calls[-1]
        self.assertEqual(
            fail_path,
            f"/task-snapshot-bridge/{INSPECTION_NUMBER}/fail",
        )
        self.assertEqual(fail_payload["error_code"], "probe_failed")
        self.assertNotIn("snapshot", fail_payload)


class ProbeProcessTests(unittest.TestCase):
    def test_temporary_output_is_removed_after_success(self):
        output_paths = []

        def fake_run(command, **kwargs):
            self.assertIn("--task-snapshot-only", command)
            output_path = Path(command[command.index("--output") + 1])
            output_paths.append(output_path)
            output_path.write_text(json.dumps(probe_document()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(bridge.subprocess, "run", side_effect=fake_run):
            document = bridge.run_probe(args_fixture(), INSPECTION_NUMBER)
        self.assertEqual(document["sample_no"], INSPECTION_NUMBER)
        self.assertEqual(len(output_paths), 1)
        self.assertFalse(output_paths[0].exists())

    def test_temporary_output_is_removed_after_probe_failure(self):
        output_paths = []

        def fake_run(command, **kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_paths.append(output_path)
            output_path.write_text("sensitive", encoding="utf-8")
            return subprocess.CompletedProcess(command, 2, stdout="private", stderr="private")

        with patch.object(bridge.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bridge.SnapshotBridgeError):
                bridge.run_probe(args_fixture(), INSPECTION_NUMBER)
        self.assertEqual(len(output_paths), 1)
        self.assertFalse(output_paths[0].exists())


if __name__ == "__main__":
    unittest.main()
