"""Shared on-wire Bridge receipt fixtures; no business node or rule installation."""
import importlib.util
from pathlib import Path
from app.execution.external_operations import (LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION, LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION, SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES, SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES)

_BRIDGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "legacy_fibrecheck_bridge"
    / "bridge.py"
)
_BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "paper_external_contract_bridge",
    _BRIDGE_PATH,
)
assert _BRIDGE_SPEC is not None and _BRIDGE_SPEC.loader is not None
_BRIDGE_MODULE = importlib.util.module_from_spec(_BRIDGE_SPEC)
_BRIDGE_SPEC.loader.exec_module(_BRIDGE_MODULE)


def upload_receipt(operation):
    summary = operation.request_summary
    source = summary["files"][0]
    return {
        "schema_version": 1,
        "receipt_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        "operation_id": operation.id,
        "payload_checksum": operation.payload_checksum,
        "target_sample_number": summary["target_sample_number"],
        "target_filename": summary["target_filename"],
        "source_artifact": {
            key: source[key]
            for key in (
                "artifact_id",
                "filename",
                "size_bytes",
                "content_sha256",
            )
        },
        "task_project": dict(summary["task_project"]),
        "server_file": {
            "filename": summary["target_filename"],
            "size_bytes": source["size_bytes"],
            "content_sha256": source["content_sha256"],
            "verification": {
                "mode": "exact_sha256",
                "source_size_bytes": source["size_bytes"],
                "source_content_sha256": source["content_sha256"],
                "remote_size_bytes": source["size_bytes"],
                "remote_content_sha256": source["content_sha256"],
                "stream_paths_equal": True,
                "stream_sizes_equal": True,
                "non_workbook_streams_equal": True,
                "biff_record_boundaries_equal": True,
                "changed_record_ids": [],
                "changed_record_count": 0,
            },
        },
        "main_record": {
            "id": "sha256:" + "3" * 16,
            "field_fingerprint": "4" * 64,
            "create_user": "sha256:" + "5" * 16,
            "create_time": "2026-08-05T08:30:00Z",
            "file_path": summary["target_filename"],
        },
        "picture_count": 0,
        "readback": {
            "main_count": 1,
            "picture_count": 0,
            "mismatches": [],
            "verified_at": "2026-08-05T08:30:01Z",
            "target_filename": summary["target_filename"],
        },
        "stages": [
            {"stage": stage}
            for stage in SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES
        ],
        "reconciliation_required": False,
    }

def review_receipt(operation):
    summary = operation.request_summary
    source = summary["source_operation"]
    return {
        "schema_version": 1,
        "receipt_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
        "operation_id": operation.id,
        "payload_checksum": operation.payload_checksum,
        "target_sample_number": summary["target_sample_number"],
        "source_upload": {
            "operation_id": source["operation_id"],
            "receipt_checksum": source["receipt_checksum"],
            "main_id": source["main_id"],
        },
        "main_record": {
            "id": source["main_id"],
            "review_user": "sha256:" + "6" * 16,
            "review_time": "2026-08-05T08:31:00Z",
            "pre_fingerprint": "7" * 64,
            "post_fingerprint": "8" * 64,
        },
        "children": {
            "picture_count": 0,
            "before_fingerprint": "9" * 64,
            "after_fingerprint": "9" * 64,
            "unchanged": True,
        },
        "readback": {
            "main_count": 1,
            "mismatches": [],
            "verified_at": "2026-08-05T08:31:01Z",
        },
        "stages": [
            {"stage": stage}
            for stage in SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES
        ],
        "reconciliation_required": False,
    }
