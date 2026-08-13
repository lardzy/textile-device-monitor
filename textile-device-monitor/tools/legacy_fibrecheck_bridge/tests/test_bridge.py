from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402


ACCOUNT = "legacy-user"
FINAL_SAMPLE = "260111037"
FINAL_SHA256 = "a" * 64
FINAL_TARGET_FILENAME = "12345678-1234-1234-1234-123456789abc.xls"
SPECIAL_WOOL_TARGET_FILENAME = (
    "260187115-2-39-8B-纤维形状截面定量试验-2026.xls"
)
SPECIAL_WOOL_MAIN_ID = "sha256:" + "3" * 16
SPECIAL_WOOL_SOURCE_OPERATION_ID = "upload-operation-1"
SPECIAL_WOOL_SOURCE_PAYLOAD_CHECKSUM = "d" * 64
SPECIAL_WOOL_SOURCE_RECEIPT_CHECKSUM = "e" * 64


def final_entry_task_project():
    project = {
        "task_check_item_id": "sha256:" + "2" * 16,
        "check_item_id": "sha256:" + "3" * 16,
        "check_item_no": "5103.5",
        "check_item_name": "纤维微观形貌",
        "check_method": "GB/T 36422-2018",
        "seq_num": 7,
        "check_count": 1,
    }
    identity = "\0".join(
        str(project[key])
        for key in (
            "task_check_item_id",
            "check_item_id",
            "check_item_no",
            "check_item_name",
            "check_method",
            "seq_num",
        )
    )
    project["project_key"] = "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    return project


def paper_task_project():
    project = {
        "task_check_item_id": "sha256:" + "4" * 16,
        "check_item_id": "sha256:" + "5" * 16,
        "check_item_no": "PAPER-QUAL",
        "check_item_name": "纸、纸板和纸浆纤维鉴别分析",
        "check_method": "GB/T 4688-2020",
        "seq_num": 3,
        "check_count": 1,
    }
    identity = "\0".join(str(project[key]) for key in (
        "task_check_item_id",
        "check_item_id",
        "check_item_no",
        "check_item_name",
        "check_method",
        "seq_num",
    ))
    project["project_key"] = "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    return project


def generic_final_entry_machine_payload(value="100"):
    return {
        "schema_version": 2,
        "operation_type": "generic_item_record",
        "sample_number": "26W006687",
        "check_item_no": "PAPER-QUAL",
        "check_item_name": "纸、纸板和纸浆纤维鉴别分析",
        "task_project": paper_task_project(),
        "expected_existing_register_count": 0,
        "generic_record": {
            "header": {
                "grade": "",
                "unit": "%" if value == "100" else "",
                "judge_basis": "",
                "test_method": "GB/T 4688-2020",
                "sample_description": "",
                "standard_type": "",
                "report_check_item_name": "",
                "attach_info": "",
                "remark": "",
                "total_judge": "",
            },
            "details": [{
                "standard_location": "",
                "standard_value": "",
                "real_location": "",
                "real_value": value,
            }],
        },
    }


def generic_final_entry_raw_receipt(payload):
    now = "2026-08-05T08:30:00Z"
    details = {
        "package_validated": {
            "schema_version": 2,
            "operation_type": "generic_item_record",
            "expected_existing_register_count": 0,
            "controlled_test_override_active": False,
        },
        "generic_details_validated": {"row_count": 1},
        "remote_preflight_verified": {
            "expected_result_count": 1,
            "existing_register_count": 0,
            "controlled_test_override_applied": False,
        },
        "remote_write_ready": {
            "operation_type": "generic_item_record",
            "expected_existing_register_count": 0,
            "target_filename": None,
            "controlled_test_override_applied": False,
        },
        "generic_rows_saved": {
            "detail_count": 1,
            "record_fingerprint": "sha256:" + "6" * 16,
        },
        "generic_readback_verified": {
            "detail_count": 1,
            "key_result_count": 1,
            "record_fingerprint": "sha256:" + "6" * 16,
        },
    }
    return {
        "schema_version": 1,
        "mode": "generic_item_record",
        "exit_code": 0,
        "reconciliation_required": False,
        "package_schema_version": 2,
        "sample_number": payload["sample_number"],
        "check_item_no": payload["check_item_no"],
        "check_item_name": payload["check_item_name"],
        "task_project": dict(payload["task_project"]),
        "stages": [
            {
                "stage": stage,
                "at": now,
                **({"detail": details[stage]} if stage in details else {}),
            }
            for stage in bridge.GENERIC_FINAL_ENTRY_RAW_SUCCESS_STAGES
        ],
    }


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


class PrettyJsonFakeProcess(FakeProcess):
    def __init__(self, events, exit_code=0):
        super().__init__([], exit_code=exit_code)
        self.stdout = iter(
            [
                json.dumps(item, ensure_ascii=False, indent=2) + "\n"
                for item in events
            ]
        )


def claim_document(
    account=ACCOUNT,
    *,
    operation_type=bridge.LEGACY_REGENERATED_COUNT_OPERATION,
    capability_available=None,
):
    summary = {
        "operation_type": operation_type,
        "target_sample_number": "260187115-2",
        "files": [
            {
                "root_id": "records",
                "relative_path": "260187115.xls",
            }
        ],
    }
    if capability_available is not None:
        summary["execution_capability"] = {
            "available": capability_available,
        }
    if operation_type == bridge.LEGACY_SPECIAL_WOOL_IMAGE_OPERATION:
        summary["target_filename"] = SPECIAL_WOOL_TARGET_FILENAME
    operation = {
        "id": "operation-1",
        "payload_checksum": "c" * 64,
        "credential": {"account_name": account},
        "request_summary": summary,
    }
    if operation_type == bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION:
        source = {
            "operation_id": SPECIAL_WOOL_SOURCE_OPERATION_ID,
            "payload_checksum": SPECIAL_WOOL_SOURCE_PAYLOAD_CHECKSUM,
            "receipt_checksum": SPECIAL_WOOL_SOURCE_RECEIPT_CHECKSUM,
            "main_id": SPECIAL_WOOL_MAIN_ID,
        }
        summary["source_operation"] = source
        operation["machine_payload"] = {
            "schema_version": 1,
            "operation_type": bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
            "target_sample_number": summary["target_sample_number"],
            "source_upload": dict(source),
        }
    return {
        "claimed": True,
        "attempt": {"id": "attempt-1"},
        "operation": operation,
    }


def special_wool_review_receipt(claim: dict) -> dict:
    operation = claim["operation"]
    summary = operation["request_summary"]
    source = summary["source_operation"]
    return {
        "schema_version": 1,
        "receipt_type": bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        "operation_id": operation["id"],
        "payload_checksum": operation["payload_checksum"],
        "target_sample_number": summary["target_sample_number"],
        "source_upload": {
            "operation_id": source["operation_id"],
            "receipt_checksum": source["receipt_checksum"],
            "main_id": source["main_id"],
        },
        "main_record": {
            "id": source["main_id"],
            "review_user": "sha256:" + "9" * 16,
            "review_time": "2026-08-05T08:30:00Z",
            "pre_fingerprint": "a" * 64,
            "post_fingerprint": "b" * 64,
        },
        "children": {
            "picture_count": 1,
            "before_fingerprint": "8" * 64,
            "after_fingerprint": "8" * 64,
            "unchanged": True,
        },
        "readback": {
            "main_count": 1,
            "mismatches": [],
            "verified_at": "2026-08-05T08:30:01Z",
        },
        "stages": [
            {"stage": stage} for stage in bridge.REVIEW_PROGRESS_STAGES
        ],
        "reconciliation_required": False,
    }


def qualitative_upload_claim() -> dict:
    project = {
        "task_check_item_id": "sha256:" + "1" * 16,
        "check_item_id": "sha256:" + "2" * 16,
        "check_item_no": "51.113K",
        "check_item_name": "纸、纸板和纸浆纤维鉴别分析",
        "check_method": "GB/T 4688-2020",
        "seq_num": 18,
        "check_count": 1,
    }
    identity = "\0".join(
        " ".join(str(project[key]).strip().split())
        for key in (
            "task_check_item_id",
            "check_item_id",
            "check_item_no",
            "check_item_name",
            "check_method",
            "seq_num",
        )
    )
    project["project_key"] = "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    summary = {
        "operation_type": bridge.LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        "source_inspection_number": "26W006740",
        "target_sample_number": "26W006740-1",
        "target_filename": "26W006740-1-26W006740-record.xls",
        "target_allocation": {"base_number": "26W006740"},
        "task_project": project,
        "files": [
            {
                "root_id": "paper_fiber_records",
                "relative_path": "26W006740/record.xls",
                "artifact_id": "artifact-1",
                "filename": "26W006740-record.xls",
                "size_bytes": 84480,
                "content_sha256": "d" * 64,
            }
        ],
    }
    return {
        "claimed": True,
        "attempt": {"id": "attempt-1"},
        "operation": {
            "id": "operation-1",
            "payload_checksum": "c" * 64,
            "credential": {"account_name": ACCOUNT},
            "request_summary": summary,
        },
    }


def qualitative_upload_receipt(
    claim: dict, *, target: str | None = None, filename: str | None = None
) -> dict:
    operation = claim["operation"]
    summary = operation["request_summary"]
    source = summary["files"][0]
    target = target or summary["target_sample_number"]
    filename = filename or summary["target_filename"]
    return {
        "schema_version": 1,
        "receipt_type": bridge.LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        "operation_id": operation["id"],
        "payload_checksum": operation["payload_checksum"],
        "target_sample_number": target,
        "target_filename": filename,
        "source_artifact": {
            "artifact_id": source["artifact_id"],
            "filename": source["filename"],
            "size_bytes": source["size_bytes"],
            "content_sha256": source["content_sha256"],
        },
        "task_project": dict(summary["task_project"]),
        "server_file": {
            "filename": filename,
            "size_bytes": source["size_bytes"],
            "content_sha256": source["content_sha256"],
        },
        "main_record": {"id": "sha256:" + "3" * 16, "file_path": filename},
        "picture_count": 0,
        "readback": {
            "main_count": 1,
            "picture_count": 0,
            "mismatches": [],
            "target_filename": filename,
        },
        "stages": [
            {"stage": stage}
            for stage in bridge.QUALITATIVE_UPLOAD_PROGRESS_STAGES
        ],
        "reconciliation_required": False,
    }


def final_entry_machine_payload(*, controlled=False):
    payload = {
        "schema_version": 2,
        "operation_type": "excel_check_record",
        "sample_number": FINAL_SAMPLE,
        "check_item_no": "5103.5",
        "check_item_name": "纤维微观形貌",
        "task_project": final_entry_task_project(),
        "expected_existing_register_count": 1 if controlled else 0,
        "excel_record": {
            "template_name": "纤维形貌7张图",
            "collection_mode": "standard",
            "expected_mapping_config_sha256": "b" * 64,
            "key_result_count": 1,
            "expected_key_identities": ["纵向"],
            "register": {
                "level": "",
                "sample_identity": "纵向",
                "equipment_no": "",
                "check_basis": "",
            },
            "workbook": {
                "relative_path": "runs/run-1/record.xls",
                "filename": "record.xls",
                "size_bytes": 4096,
                "content_sha256": FINAL_SHA256,
            },
        },
    }
    if controlled:
        payload["controlled_test_override"] = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": FINAL_SAMPLE,
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "仅用于已批准的首次链路验证",
        }
    return payload


def final_entry_claim(*, controlled=False):
    payload = final_entry_machine_payload(controlled=controlled)
    source = {
        "artifact_id": "artifact-final-1",
        "root_id": "execution_staging",
        "relative_path": payload["excel_record"]["workbook"]["relative_path"],
        "filename": payload["excel_record"]["workbook"]["filename"],
        "size_bytes": payload["excel_record"]["workbook"]["size_bytes"],
        "content_sha256": payload["excel_record"]["workbook"]["content_sha256"],
    }
    return {
        "claimed": True,
        "attempt": {"id": "attempt-final-1"},
        "operation": {
            "id": "operation-final-1",
            "payload_checksum": "c" * 64,
            "credential": {"account_name": ACCOUNT},
            "machine_payload": payload,
            "request_summary": {
                "operation_type": bridge.LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
                "target_sample_number": FINAL_SAMPLE,
                "files": [source],
                "task_project": final_entry_task_project(),
                "template_binding": {
                    "binding_version": 1,
                    "image_count": 7,
                    "legacy_template_name": "纤维形貌7张图",
                    "local_asset_name": "microscopy-7.xls",
                    "local_asset_sha256": "d" * 64,
                    "mapping_config_sha256": "b" * 64,
                },
                "sample_identity_contract": {
                    "selected": "纵向",
                    "options": ["纵向"],
                    "option_count": 1,
                    "check_count": 1,
                    "count_mismatch": False,
                },
                "execution_capability": {"available": True},
            },
        },
    }


def final_entry_raw_receipt(*, controlled=False):
    override = (
        final_entry_machine_payload(controlled=True)["controlled_test_override"]
        if controlled
        else None
    )
    expected_existing = 1 if controlled else 0
    details = {
        "package_validated": {
            "schema_version": 2,
            "operation_type": "excel_check_record",
            "expected_existing_register_count": expected_existing,
            "controlled_test_override_active": controlled,
        },
        "workbook_verified": {
            "filename": "record.xls",
            "size_bytes": 4096,
            "content_sha256": FINAL_SHA256,
            "key_result_count": 1,
        },
        "remote_preflight_verified": {
            "expected_result_count": 1,
            "existing_register_count": expected_existing,
            "controlled_test_override_applied": controlled,
            "mapping_config_sha256": "b" * 64,
            "mapping_config_count": 1,
        },
        "remote_write_ready": {
            "operation_type": "excel_check_record",
            "expected_existing_register_count": expected_existing,
            "target_filename": FINAL_TARGET_FILENAME,
            "controlled_test_override_applied": controlled,
        },
        "staging_file_verified": {
            "filename": FINAL_TARGET_FILENAME,
            "size_bytes": 4096,
            "content_sha256": FINAL_SHA256,
        },
        "remote_file_verified": {
            "filename": FINAL_TARGET_FILENAME,
            "content_sha256": FINAL_SHA256,
        },
        "excel_proof_verified": {
            "key_result_count": 1,
            "record_fingerprint": "sha256:0123456789abcdef",
        },
    }
    stages = []
    for index, name in enumerate(bridge.FINAL_ENTRY_RAW_SUCCESS_STAGES):
        stage = {
            "stage": name,
            "at": f"2026-08-05T08:{index:02d}:00Z",
        }
        if name in details:
            stage["detail"] = details[name]
        stages.append(stage)
    receipt = {
        "schema_version": 1,
        "mode": "excel_check_record",
        "exit_code": 0,
        "reconciliation_required": False,
        "stages": stages,
        "package_schema_version": 2,
        "sample_number": FINAL_SAMPLE,
        "check_item_no": "5103.5",
        "check_item_name": "纤维微观形貌",
        # This is the Writer's read-only Oracle measurement, not a Bridge copy
        # of request_summary.
        "task_project": final_entry_task_project(),
    }
    if controlled:
        receipt["controlled_test_override"] = {
            "active": True,
            "applied": True,
            **override,
        }
    return receipt


def final_entry_process(*, controlled=False, receipt=None):
    raw_receipt = receipt or final_entry_raw_receipt(controlled=controlled)
    return FakeProcess(
        [{"stage": stage["stage"]} for stage in raw_receipt["stages"]]
        + [{"receipt": raw_receipt}]
    )


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
            final_entry_writer="final-entry-writer.exe",
            final_entry_work_root="C:/final-entry-work",
            allow_controlled_final_entry_test_override=False,
            fibrecheck_dir="C:/FibreCheck",
        )
        self.root_map = {
            "records": "C:/records",
            "execution_staging": "C:/execution-staging",
        }

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

    def test_claim_advertises_all_writer_dispatch_profiles(self):
        api = ApiStub(claim={"claimed": False})
        with patch.object(bridge, "api_request", side_effect=api):
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(outcome, "idle")
        self.assertEqual(
            api.calls[0][1]["supported_operation_types"],
            [
                bridge.LEGACY_REGENERATED_COUNT_OPERATION,
                bridge.LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
                bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
                bridge.LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
                bridge.LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
                bridge.LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
                bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            ],
        )

    def test_final_entry_is_not_advertised_without_both_runtime_paths(self):
        self.args.final_entry_work_root = None
        api = ApiStub(claim={"claimed": False})
        with patch.object(bridge, "api_request", side_effect=api):
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(outcome, "idle")
        self.assertNotIn(
            bridge.LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
            api.calls[0][1]["supported_operation_types"],
        )

    def test_unproven_special_wool_operation_never_starts_writer(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            capability_available=False,
        )
        api = ApiStub(claim=claim)
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess,
            "Popen",
        ) as popen:
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(outcome, "claimed")
        popen.assert_not_called()
        failure = api.calls[-1][1]
        self.assertEqual(failure["stage"], "authenticated")
        self.assertEqual(
            failure["error_code"],
            "writer_capability_unavailable",
        )

    def test_image_operation_uses_its_full_stage_contract(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            capability_available=True,
        )
        writer_receipt = {
            "target_sample_number": "260187115-2",
            "target_filename": SPECIAL_WOOL_TARGET_FILENAME,
        }
        process = FakeProcess(
            [
                {"stage": "authenticated"},
                {"stage": "permission_verified"},
                {"stage": "remote_state_verified"},
                {"stage": "task_project_verified"},
                {"stage": "file_copy_ready"},
                {"stage": "file_copy_started"},
                {"stage": "file_copy_verified"},
                {"stage": "main_record_save_started"},
                {"stage": "main_record_verified"},
                {"stage": "picture_child_verified"},
                {"stage": "completed"},
                {"receipt": writer_receipt},
            ]
        )
        api = ApiStub(claim=claim)
        self.run_cycle(process, api)
        stages = [
            payload["stage"]
            for path, payload in api.calls
            if path.endswith("/stage")
        ]
        self.assertIn("task_project_verified", stages)
        self.assertIn("picture_child_verified", stages)
        self.assertEqual(process.stdin.getvalue(), bridge.SIDE_EFFECT_PERMIT + "\n")
        complete = next(
            payload for path, payload in api.calls if path.endswith("/complete")
        )
        self.assertEqual(complete["receipt"], writer_receipt)
        self.assertEqual(
            complete["receipt"]["target_filename"],
            SPECIAL_WOOL_TARGET_FILENAME,
        )

    def test_image_operation_accepts_writer_pretty_printed_json_events(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            capability_available=True,
        )
        process = PrettyJsonFakeProcess(
            [
                {"stage": "authenticated"},
                {
                    "stage": "permission_verified",
                    "detail": {"note": "包含括号 } 和转义 \\\" 仍是字符串"},
                },
                {"stage": "remote_state_verified"},
                {"stage": "task_project_verified"},
                {"stage": "file_copy_ready"},
                {"stage": "file_copy_started"},
                {"stage": "file_copy_verified"},
                {"stage": "main_record_save_started"},
                {"stage": "main_record_verified"},
                {"stage": "picture_child_verified"},
                {"stage": "completed"},
                {"receipt": {"target_sample_number": "260187115-2"}},
            ]
        )
        api = ApiStub(claim=claim)

        self.run_cycle(process, api)

        stages = [
            payload["stage"]
            for path, payload in api.calls
            if path.endswith("/stage")
        ]
        self.assertEqual(stages, list(bridge.IMAGE_PROGRESS_STAGES))
        self.assertEqual(
            process.stdin.getvalue(), bridge.SIDE_EFFECT_PERMIT + "\n"
        )
        self.assertTrue(any(path.endswith("/complete") for path, _ in api.calls))

    def test_incomplete_writer_json_never_releases_side_effect_permit(self):
        process = FakeProcess([], exit_code=1)
        process.stdout = iter([
            '{\n',
            '  "stage": "file_copy_ready"\n',
        ])
        api = ApiStub()

        self.run_cycle(process, api)

        self.assertEqual(process.stdin.getvalue(), "")
        self.assertTrue(process.terminated)
        failure = next(
            payload for path, payload in api.calls if path.endswith("/fail")
        )
        self.assertEqual(failure["error_code"], "bridge_stage_persistence_failed")
        self.assertIn("未完成", failure["message"])

    def test_review_uses_review_boundary_and_needs_no_source_root(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
            capability_available=True,
        )
        claim["operation"]["request_summary"].pop("files")
        receipt = special_wool_review_receipt(claim)
        process = FakeProcess(
            [
                {"stage": "authenticated"},
                {"stage": "permission_verified"},
                {"stage": "remote_state_verified"},
                {"stage": "review_save_ready"},
                {"stage": "review_save_started"},
                {"stage": "review_main_verified"},
                {"stage": "review_children_verified"},
                {"stage": "completed"},
                {"receipt": receipt},
            ]
        )
        api = ApiStub(claim=claim)
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess,
            "Popen",
            return_value=process,
        ) as popen:
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(outcome, "claimed")
        command = popen.call_args.args[0]
        self.assertNotIn("--source-root", command)
        stages = [
            payload["stage"]
            for path, payload in api.calls
            if path.endswith("/stage")
        ]
        self.assertEqual(stages[:5], [
            "authenticated",
            "permission_verified",
            "remote_state_verified",
            "review_save_ready",
            "review_save_started",
        ])
        self.assertEqual(process.stdin.getvalue(), bridge.SIDE_EFFECT_PERMIT + "\n")
        self.assertTrue(any(path.endswith("/complete") for path, _ in api.calls))

    def test_review_rejects_replaced_main_id_before_starting_writer(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
            capability_available=True,
        )
        claim["operation"]["machine_payload"]["source_upload"][
            "main_id"
        ] = "sha256:" + "f" * 16
        api = ApiStub(claim=claim)
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen"
        ) as popen:
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertEqual(outcome, "claimed")
        popen.assert_not_called()
        failure = api.calls[-1][1]
        self.assertEqual(
            failure["error_code"],
            "special_wool_review_identity_rejected",
        )

    def test_review_rejects_raw_receipt_for_replaced_main_id(self):
        claim = claim_document(
            operation_type=bridge.LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
            capability_available=True,
        )
        receipt = special_wool_review_receipt(claim)
        replacement = "sha256:" + "f" * 16
        receipt["source_upload"]["main_id"] = replacement
        receipt["main_record"]["id"] = replacement
        process = FakeProcess(
            [{"stage": stage} for stage in bridge.REVIEW_PROGRESS_STAGES]
            + [{"receipt": receipt}]
        )
        api = ApiStub(claim=claim)
        with patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen", return_value=process
        ):
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertFalse(any(path.endswith("/complete") for path, _ in api.calls))
        failure = next(
            payload for path, payload in api.calls if path.endswith("/fail")
        )
        self.assertEqual(
            failure["error_code"],
            "special_wool_review_receipt_rejected",
        )

    def test_qualitative_upload_receipt_accepts_exact_match(self):
        claim = qualitative_upload_claim()
        receipt = qualitative_upload_receipt(claim)
        self.assertIs(
            bridge.validate_special_wool_qualitative_upload_receipt(
                claim["operation"],
                claim["operation"]["request_summary"],
                receipt,
            ),
            receipt,
        )

    def test_qualitative_upload_receipt_accepts_renumbered_actual(self):
        # Writer 按旧系统锁内实况顺号写入 -2；requested 仍绑定预检的 -1。
        claim = qualitative_upload_claim()
        receipt = qualitative_upload_receipt(
            claim,
            target="26W006740-2",
            filename="26W006740-2-26W006740-record.xls",
        )
        receipt["requested_sample_number"] = "26W006740-1"
        receipt["renumbered"] = True
        self.assertIs(
            bridge.validate_special_wool_qualitative_upload_receipt(
                claim["operation"],
                claim["operation"]["request_summary"],
                receipt,
            ),
            receipt,
        )

    def test_qualitative_upload_receipt_rejects_renumber_tampering(self):
        claim = qualitative_upload_claim()
        operation = claim["operation"]
        summary = operation["request_summary"]
        base = qualitative_upload_receipt(
            claim,
            target="26W006740-2",
            filename="26W006740-2-26W006740-record.xls",
        )
        base["requested_sample_number"] = "26W006740-1"
        base["renumbered"] = True

        def rejected(receipt):
            with self.assertRaises(bridge.BridgeError):
                bridge.validate_special_wool_qualitative_upload_receipt(
                    operation, summary, receipt
                )

        # 顺号标记与实际编号不一致
        rejected({**base, "renumbered": False})
        # requested 不绑定预检单
        rejected({**base, "requested_sample_number": "26W006740-3"})
        # 实际号不属于同一编号族
        rejected({**base, "target_sample_number": "26X999999-1"})
        # 旧版回执（无顺号字段）编号漂移必须拒绝
        legacy = qualitative_upload_receipt(claim, target="26W006740-2")
        rejected(legacy)
        # 未知字段仍按契约拒绝
        rejected({**qualitative_upload_receipt(claim), "unexpected": True})

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

    def test_abort_returned_with_boundary_never_releases_writer(self):
        process = FakeProcess([{"stage": "file_copy_ready"}], exit_code=1)
        api = ApiStub(
            stage_responses={"file_copy_started": {"abort_requested": True}},
        )
        self.run_cycle(process, api)

        self.assertTrue(process.terminated)
        self.assertEqual(process.stdin.getvalue(), "")
        self.assertFalse(any(path.endswith("/complete") for path, _ in api.calls))
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_started")
        self.assertEqual(fail["error_code"], "abort_acknowledged")

    def test_permit_pipe_write_failure_terminates_blocked_writer(self):
        class BrokenPermitPipe:
            def write(self, _value):
                raise BrokenPipeError("simulated closed stdin")

            def flush(self):
                raise AssertionError("flush must not run after write failure")

        process = FakeProcess([{"stage": "file_copy_ready"}], exit_code=1)
        process.stdin = BrokenPermitPipe()
        api = ApiStub()
        self.run_cycle(process, api)

        self.assertTrue(process.terminated)
        self.assertFalse(any(path.endswith("/complete") for path, _ in api.calls))
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_started")
        self.assertEqual(fail["error_code"], "bridge_stage_persistence_failed")

    def test_permit_pipe_flush_failure_terminates_uncertain_writer(self):
        class UncertainPermitPipe:
            def __init__(self):
                self.value = ""

            def write(self, value):
                self.value += value

            def flush(self):
                raise BrokenPipeError("simulated flush failure")

        process = FakeProcess([{"stage": "file_copy_ready"}], exit_code=1)
        process.stdin = UncertainPermitPipe()
        api = ApiStub()
        self.run_cycle(process, api)

        self.assertTrue(process.terminated)
        self.assertEqual(process.stdin.value, bridge.SIDE_EFFECT_PERMIT + "\n")
        self.assertFalse(any(path.endswith("/complete") for path, _ in api.calls))
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_started")
        self.assertEqual(fail["error_code"], "bridge_stage_persistence_failed")

    def test_abort_after_successful_permit_does_not_kill_writer_as_preflight(self):
        process = FakeProcess(
            [
                {"stage": "file_copy_ready"},
                {"stage": "file_copy_started"},
                {"stage": "file_copy_verified"},
            ],
            exit_code=1,
        )
        api = ApiStub(
            stage_responses={"file_copy_verified": {"abort_requested": True}},
        )
        self.run_cycle(process, api)

        self.assertFalse(process.terminated)
        self.assertEqual(
            process.stdin.getvalue(),
            bridge.SIDE_EFFECT_PERMIT + "\n",
        )
        fail = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(fail["stage"], "file_copy_verified")
        self.assertNotEqual(fail["error_code"], "abort_acknowledged")

    def test_final_entry_dispatches_only_machine_payload_and_converts_receipt(self):
        claim = final_entry_claim()
        raw_receipt = final_entry_raw_receipt()
        process = final_entry_process(receipt=raw_receipt)
        api = ApiStub(claim=claim)
        captured = {}
        permit_state_at_boundary = []

        def api_call(api_base, token, method, path, payload):
            if path.endswith("/stage") and payload.get("stage") == (
                "excel_collection_started"
            ):
                permit_state_at_boundary.append(process.stdin.getvalue())
            return api(api_base, token, method, path, payload)

        def launch(command, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs["env"])
            package_path = command[command.index("--package") + 1]
            captured["package"] = json.loads(
                Path(package_path).read_text(encoding="utf-8")
            )
            return process

        with patch.object(bridge, "api_request", side_effect=api_call), patch.object(
            bridge.subprocess, "Popen", side_effect=launch
        ):
            outcome = bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )

        self.assertEqual(outcome, "claimed")
        self.assertEqual(
            captured["package"],
            claim["operation"]["machine_payload"],
        )
        self.assertEqual(captured["command"][0], self.args.final_entry_writer)
        self.assertEqual(
            captured["command"][captured["command"].index("--source-root") + 1],
            self.root_map["execution_staging"],
        )
        self.assertEqual(
            captured["command"][captured["command"].index("--work-root") + 1],
            self.args.final_entry_work_root,
        )
        self.assertNotIn("--allow-controlled-test-override", captured["command"])
        self.assertEqual(captured["env"]["FIBRECHECK_RUNNER_PASSWORD"], "secret")
        self.assertEqual(permit_state_at_boundary, [""])
        self.assertEqual(process.stdin.getvalue(), bridge.SIDE_EFFECT_PERMIT + "\n")

        stages = [
            payload["stage"]
            for path, payload in api.calls
            if path.endswith("/stage")
        ]
        self.assertEqual(stages, list(bridge.FINAL_ENTRY_PROGRESS_STAGES))
        complete = next(
            payload for path, payload in api.calls if path.endswith("/complete")
        )
        receipt = complete["receipt"]
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "receipt_type",
                "operation_id",
                "payload_checksum",
                "target_sample_number",
                "source_artifact",
                "task_project",
                "template_binding",
                "final_entry",
                "controlled_test_override",
                "existing_record_decision",
                "stages",
                "reconciliation_required",
            },
        )
        self.assertEqual(
            receipt["receipt_type"],
            bridge.LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
        )
        self.assertIsNone(receipt["controlled_test_override"])
        self.assertEqual(receipt["final_entry"]["resulting_register_count"], 1)
        self.assertEqual(
            receipt["task_project"], raw_receipt["task_project"]
        )
        self.assertIsNot(
            receipt["task_project"],
            claim["operation"]["request_summary"]["task_project"],
        )
        self.assertEqual(
            [entry["stage"] for entry in receipt["stages"]],
            list(bridge.FINAL_ENTRY_PROGRESS_STAGES),
        )

    def test_final_entry_rejects_invalid_raw_receipt_without_completing(self):
        claim = final_entry_claim()
        raw_receipt = final_entry_raw_receipt()
        raw_receipt = copy.deepcopy(raw_receipt)
        remote_file = next(
            stage
            for stage in raw_receipt["stages"]
            if stage["stage"] == "remote_file_verified"
        )
        remote_file["detail"]["content_sha256"] = "f" * 64
        process = final_entry_process(receipt=raw_receipt)
        api = ApiStub(claim=claim)

        self.run_cycle(process, api)

        self.assertFalse(any(path.endswith("/complete") for path, _ in api.calls))
        failure = next(payload for path, payload in api.calls if path.endswith("/fail"))
        self.assertEqual(failure["error_code"], "final_entry_receipt_rejected")
        self.assertEqual(failure["stage"], "excel_proof_verified")
        self.assertNotIn(
            "completed",
            [
                payload["stage"]
                for path, payload in api.calls
                if path.endswith("/stage")
            ],
        )

    def test_final_entry_rejects_writer_project_drift_without_completing(self):
        for field, changed in (
            ("task_check_item_id", "sha256:" + "9" * 16),
            ("check_item_id", "sha256:" + "8" * 16),
            ("check_method", "GB/T 36422-2018/XG1-2024"),
            ("seq_num", 8),
            ("check_count", 2),
        ):
            with self.subTest(field=field):
                claim = final_entry_claim()
                raw_receipt = copy.deepcopy(final_entry_raw_receipt())
                raw_receipt["task_project"][field] = changed
                api = ApiStub(
                    claim=claim,
                )
                self.run_cycle(
                    final_entry_process(receipt=raw_receipt), api
                )
                self.assertFalse(
                    any(path.endswith("/complete") for path, _ in api.calls)
                )
                failure = next(
                    payload
                    for path, payload in api.calls
                    if path.endswith("/fail")
                )
                self.assertEqual(
                    failure["error_code"], "final_entry_receipt_rejected"
                )

    def test_final_entry_rejects_private_and_summary_project_mismatch_before_writer(self):
        for field, changed in (
            ("task_check_item_id", "sha256:" + "9" * 16),
            ("check_item_id", "sha256:" + "8" * 16),
            ("check_method", "GB/T 36422-2018/XG1-2024"),
            ("seq_num", 8),
            ("check_count", 2),
        ):
            with self.subTest(field=field):
                claim = final_entry_claim()
                claim["operation"]["machine_payload"]["task_project"][
                    field
                ] = changed
                api = ApiStub(claim=claim)
                with patch.object(
                    bridge, "api_request", side_effect=api
                ), patch.object(bridge.subprocess, "Popen") as popen:
                    bridge.run_one_cycle(
                        self.args,
                        "bridge-token",
                        ACCOUNT,
                        "secret",
                        self.root_map,
                    )
                popen.assert_not_called()
                self.assertEqual(
                    api.calls[-1][1]["error_code"],
                    "final_entry_machine_payload_rejected",
                )

    def test_final_entry_rejects_register_identity_drift_before_writer(self):
        claim = final_entry_claim()
        claim["operation"]["machine_payload"]["excel_record"]["register"][
            "sample_identity"
        ] = "横向"
        api = ApiStub(claim=claim)
        with patch.object(
            bridge, "api_request", side_effect=api
        ), patch.object(bridge.subprocess, "Popen") as popen:
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        popen.assert_not_called()
        self.assertEqual(
            api.calls[-1][1]["error_code"],
            "final_entry_machine_payload_rejected",
        )

    def test_final_entry_multi_copy_allows_append_at_or_beyond_task_count(self):
        for existing_count in (4, 5):
            with self.subTest(existing_count=existing_count):
                claim = final_entry_claim()
                operation = claim["operation"]
                payload = operation["machine_payload"]
                summary = operation["request_summary"]
                payload["task_project"]["check_count"] = 4
                summary["task_project"]["check_count"] = 4
                payload["expected_existing_register_count"] = existing_count
                payload["excel_record"]["expected_key_identities"] = ["浴巾"]
                payload["excel_record"]["register"]["sample_identity"] = "浴巾"
                summary["sample_identity_contract"] = {
                    "selected": "浴巾",
                    "options": ["浴巾", "枕套", "床单", "被套"],
                    "option_count": 4,
                    "check_count": 4,
                    "count_mismatch": False,
                }
                validated, _ = bridge.validate_final_entry_machine_payload(
                    operation, summary
                )
                self.assertIs(validated, payload)

    def test_final_entry_single_copy_existing_record_requires_signed_decision(self):
        claim = final_entry_claim()
        operation = claim["operation"]
        payload = operation["machine_payload"]
        summary = operation["request_summary"]
        payload["expected_existing_register_count"] = 1
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_final_entry_machine_payload(operation, summary)

        decision = {
            "kind": "append_when_check_count_one",
            "action": "append",
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
        }
        payload["existing_record_decision"] = dict(decision)
        summary["existing_record_decision"] = dict(decision)
        validated, _ = bridge.validate_final_entry_machine_payload(
            operation, summary
        )
        self.assertIs(validated, payload)

    def test_final_entry_multi_copy_receipt_allows_count_overrun(self):
        claim = final_entry_claim()
        operation = claim["operation"]
        payload = operation["machine_payload"]
        summary = operation["request_summary"]
        payload["task_project"]["check_count"] = 4
        summary["task_project"]["check_count"] = 4
        payload["expected_existing_register_count"] = 4
        summary["sample_identity_contract"]["check_count"] = 4
        summary["sample_identity_contract"]["count_mismatch"] = True

        raw = final_entry_raw_receipt()
        raw["task_project"]["check_count"] = 4
        for stage in raw["stages"]:
            detail = stage.get("detail", {})
            if "expected_existing_register_count" in detail:
                detail["expected_existing_register_count"] = 4
            if "existing_register_count" in detail:
                detail["existing_register_count"] = 4
            if "expected_result_count" in detail:
                detail["expected_result_count"] = 4
        receipt = bridge.convert_final_entry_receipt(
            operation, summary, payload, summary["files"][0], raw
        )
        self.assertEqual(receipt["final_entry"]["resulting_register_count"], 5)
        self.assertIsNone(receipt["existing_record_decision"])

    def test_final_entry_controlled_override_requires_cli_and_existing_env(self):
        claim = final_entry_claim(controlled=True)
        api = ApiStub(claim=claim)
        with patch.dict(
            os.environ,
            {"FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO": FINAL_SAMPLE},
            clear=False,
        ), patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen"
        ) as popen:
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        popen.assert_not_called()
        self.assertEqual(
            api.calls[-1][1]["error_code"],
            "final_entry_machine_payload_rejected",
        )

        self.args.allow_controlled_final_entry_test_override = True
        api = ApiStub(claim=claim)
        with patch.dict(
            os.environ,
            {"FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO": "260111038"},
            clear=False,
        ), patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen"
        ) as popen:
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        popen.assert_not_called()
        self.assertEqual(
            api.calls[-1][1]["error_code"],
            "final_entry_machine_payload_rejected",
        )

    def test_final_entry_controlled_override_is_forwarded_only_when_triply_bound(self):
        self.args.allow_controlled_final_entry_test_override = True
        claim = final_entry_claim(controlled=True)
        process = final_entry_process(controlled=True)
        api = ApiStub(claim=claim)
        captured = {}

        def launch(command, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs["env"])
            package_path = command[command.index("--package") + 1]
            captured["package"] = json.loads(
                Path(package_path).read_text(encoding="utf-8")
            )
            return process

        with patch.dict(
            os.environ,
            {"FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO": FINAL_SAMPLE},
            clear=False,
        ), patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen", side_effect=launch
        ):
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )

        self.assertIn("--allow-controlled-test-override", captured["command"])
        self.assertEqual(
            captured["env"]["FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO"],
            FINAL_SAMPLE,
        )
        self.assertEqual(
            captured["package"],
            claim["operation"]["machine_payload"],
        )
        complete = next(
            payload for path, payload in api.calls if path.endswith("/complete")
        )
        override = complete["receipt"]["controlled_test_override"]
        self.assertTrue(override["active"])
        self.assertTrue(override["applied"])
        self.assertNotIn("reason", override)
        self.assertEqual(
            complete["receipt"]["final_entry"]["resulting_register_count"],
            2,
        )

    def test_normal_final_entry_never_receives_controlled_override_flag(self):
        self.args.allow_controlled_final_entry_test_override = True
        claim = final_entry_claim(controlled=False)
        process = final_entry_process(controlled=False)
        api = ApiStub(claim=claim)
        with patch.dict(
            os.environ,
            {"FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO": FINAL_SAMPLE},
            clear=False,
        ), patch.object(bridge, "api_request", side_effect=api), patch.object(
            bridge.subprocess, "Popen", return_value=process
        ) as popen:
            bridge.run_one_cycle(
                self.args,
                "bridge-token",
                ACCOUNT,
                "secret",
                self.root_map,
            )
        self.assertNotIn(
            "--allow-controlled-test-override",
            popen.call_args.args[0],
        )

    def test_generic_final_entry_binds_paper_project_value_and_no_proof(self):
        payload = generic_final_entry_machine_payload("100")
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
            "judgement_contract": {
                "required": False,
                "judge_basis": "",
                "judgement": "",
                "standard_value": "",
            },
        }
        operation = {
            "id": "generic-operation-1",
            "payload_checksum": "7" * 64,
            "machine_payload": payload,
        }
        self.assertEqual(
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            ),
            payload,
        )
        self.assertEqual(
            payload["generic_record"]["header"]["report_check_item_name"],
            "",
        )
        receipt = bridge.convert_generic_final_entry_receipt(
            operation,
            summary,
            payload,
            generic_final_entry_raw_receipt(payload),
        )
        self.assertFalse(receipt["final_entry"]["proofed"])
        self.assertEqual(receipt["final_entry"]["detail_count"], 1)
        self.assertEqual(
            [item["stage"] for item in receipt["stages"]],
            list(bridge.GENERIC_FINAL_ENTRY_PROGRESS_STAGES),
        )

    def test_generic_final_entry_accepts_human_confirmed_standard_value(self):
        payload = generic_final_entry_machine_payload("100")
        header = payload["generic_record"]["header"]
        header["judge_basis"] = "按客户要求"
        header["report_check_item_name"] = payload["check_item_name"]
        header["total_judge"] = "符合"
        detail = payload["generic_record"]["details"][0]
        detail["standard_value"] = "定性，100%木浆"
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
            "judgement_contract": {
                "required": True,
                "judge_basis": "按客户要求",
                "judgement": "符合",
                "standard_value": "定性，100%木浆",
            },
        }
        operation = {"machine_payload": payload}
        self.assertEqual(
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            ),
            payload,
        )

        detail["standard_value"] = "被篡改的标准值"
        with self.assertRaisesRegex(
            bridge.BridgeError,
            "判定字段与签发摘要不一致",
        ):
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            )

    def test_generic_final_entry_legacy_judgement_only_allows_w32_mirror(self):
        payload = generic_final_entry_machine_payload("100")
        header = payload["generic_record"]["header"]
        header["judge_basis"] = "按客户要求"
        header["report_check_item_name"] = payload["check_item_name"]
        header["total_judge"] = "符合"
        detail = payload["generic_record"]["details"][0]
        detail["standard_value"] = "100"
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
            "final_entry_summary": {"judgement_required": True},
        }
        operation = {"machine_payload": payload}
        self.assertEqual(
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            ),
            payload,
        )

        detail["standard_value"] = "定性，100%木浆"
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            )

    def test_generic_final_entry_rejects_unit_not_bound_to_w32(self):
        payload = generic_final_entry_machine_payload("混合纤维")
        payload["generic_record"]["header"]["unit"] = "%"
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "混合纤维",
                "unit": "",
            },
        }
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_generic_final_entry_machine_payload(
                {"machine_payload": payload}, summary
            )

    def test_generic_final_entry_override_field_set_and_presence_rules(self):
        override = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": "26W006687",
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "既有 1 条登记，受控追加 1 条",
        }
        payload = generic_final_entry_machine_payload("100")
        payload["expected_existing_register_count"] = 1
        payload["controlled_test_override"] = dict(override)
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
        }
        operation = {"id": "generic-operation-override", "machine_payload": payload}
        self.assertEqual(
            bridge.validate_generic_final_entry_machine_payload(
                operation, summary
            ),
            payload,
        )
        plain = generic_final_entry_machine_payload("100")
        plain["controlled_test_override"] = dict(override)
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_generic_final_entry_machine_payload(
                {"machine_payload": plain}, summary
            )
        no_override = generic_final_entry_machine_payload("100")
        no_override["expected_existing_register_count"] = 1
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_generic_final_entry_machine_payload(
                {"machine_payload": no_override}, summary
            )

    def test_generic_final_entry_multi_copy_ignores_capacity_without_decision(self):
        for existing_count in (4, 5):
            with self.subTest(existing_count=existing_count):
                payload = generic_final_entry_machine_payload("100")
                payload["task_project"]["check_count"] = 4
                payload["expected_existing_register_count"] = existing_count
                summary = {
                    "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
                    "target_sample_number": payload["sample_number"],
                    "task_project": dict(payload["task_project"]),
                    "result_contract": {
                        "worksheet": "Sheet1",
                        "cell": "W32",
                        "value": "100",
                        "unit": "%",
                    },
                }
                validated = bridge.validate_generic_final_entry_machine_payload(
                    {"machine_payload": payload}, summary
                )
                self.assertIs(validated, payload)

    def test_generic_single_copy_existing_record_requires_signed_decision(self):
        payload = generic_final_entry_machine_payload("100")
        payload["expected_existing_register_count"] = 1
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
        }
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_generic_final_entry_machine_payload(
                {"machine_payload": payload}, summary
            )

    def test_generic_multi_copy_receipt_allows_count_overrun(self):
        payload = generic_final_entry_machine_payload("100")
        payload["task_project"]["check_count"] = 4
        payload["expected_existing_register_count"] = 4
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
        }
        raw = generic_final_entry_raw_receipt(payload)
        for stage in raw["stages"]:
            detail = stage.get("detail", {})
            if "expected_existing_register_count" in detail:
                detail["expected_existing_register_count"] = 4
            if "existing_register_count" in detail:
                detail["existing_register_count"] = 4
            if "expected_result_count" in detail:
                detail["expected_result_count"] = 4
        operation = {
            "id": "generic-operation-multi",
            "payload_checksum": "8" * 64,
            "machine_payload": payload,
        }
        receipt = bridge.convert_generic_final_entry_receipt(
            operation, summary, payload, raw
        )
        self.assertEqual(receipt["final_entry"]["resulting_register_count"], 5)
        self.assertIsNone(receipt["existing_record_decision"])

    @staticmethod
    def _override_generic_raw_receipt(payload, override):
        receipt = generic_final_entry_raw_receipt(payload)
        receipt["controlled_test_override"] = {
            "active": True,
            "applied": True,
            **override,
        }
        for stage in receipt["stages"]:
            detail = stage.get("detail")
            if not detail:
                continue
            if "expected_existing_register_count" in detail:
                detail["expected_existing_register_count"] = 1
            if "existing_register_count" in detail:
                detail["existing_register_count"] = 1
            if "controlled_test_override_active" in detail:
                detail["controlled_test_override_active"] = True
            if "controlled_test_override_applied" in detail:
                detail["controlled_test_override_applied"] = True
        return receipt

    def test_generic_final_entry_override_receipt_conversion(self):
        override = {
            "kind": "append_one_when_check_count_one",
            "target_sample_number": "26W006687",
            "expected_task_check_count": 1,
            "expected_existing_register_count": 1,
            "resulting_register_count": 2,
            "reason": "既有 1 条登记，受控追加 1 条",
        }
        payload = generic_final_entry_machine_payload("100")
        payload["expected_existing_register_count"] = 1
        payload["controlled_test_override"] = dict(override)
        summary = {
            "operation_type": bridge.LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
            "target_sample_number": payload["sample_number"],
            "task_project": dict(payload["task_project"]),
            "result_contract": {
                "worksheet": "Sheet1",
                "cell": "W32",
                "value": "100",
                "unit": "%",
            },
        }
        operation = {
            "id": "generic-operation-override",
            "payload_checksum": "9" * 64,
            "machine_payload": payload,
        }
        receipt = bridge.convert_generic_final_entry_receipt(
            operation,
            summary,
            payload,
            self._override_generic_raw_receipt(payload, override),
        )
        self.assertEqual(receipt["final_entry"]["expected_existing_register_count"], 1)
        self.assertEqual(receipt["final_entry"]["resulting_register_count"], 2)
        self.assertFalse(receipt["final_entry"]["proofed"])
        echoed = receipt["controlled_test_override"]
        self.assertTrue(echoed["active"])
        self.assertTrue(echoed["applied"])
        self.assertEqual(echoed["resulting_register_count"], 2)
        self.assertNotIn("reason", echoed)

        missing_echo = self._override_generic_raw_receipt(payload, override)
        del missing_echo["controlled_test_override"]
        with self.assertRaises(bridge.BridgeError):
            bridge.convert_generic_final_entry_receipt(
                operation, summary, payload, missing_echo
            )

        not_applied = self._override_generic_raw_receipt(payload, override)
        not_applied["controlled_test_override"]["applied"] = False
        with self.assertRaises(bridge.BridgeError):
            bridge.convert_generic_final_entry_receipt(
                operation, summary, payload, not_applied
            )

        plain_payload = generic_final_entry_machine_payload("100")
        stray_override = generic_final_entry_raw_receipt(plain_payload)
        stray_override["controlled_test_override"] = {
            "active": True,
            "applied": True,
            **override,
        }
        with self.assertRaises(bridge.BridgeError):
            bridge.convert_generic_final_entry_receipt(
                {"id": "plain", "machine_payload": plain_payload},
                summary,
                plain_payload,
                stray_override,
            )


if __name__ == "__main__":
    unittest.main()
