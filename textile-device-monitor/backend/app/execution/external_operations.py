from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.events import append_audit_log, append_run_event
from app.execution.electron_microscopy import (
    ELECTRON_TEST_METHOD,
    cached_task_snapshot,
)
from app.execution.microscopy_families import (
    ALL_PROJECT_NAME_ALIASES,
    microscopy_family_for_project,
)
from app.execution.microscopy_check_record import (
    MICROSCOPY_CHECK_RECORD_COMPATIBLE_GENERATOR_VERSIONS,
)
from app.execution.microscopy_original_record import (
    MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME,
)
from app.execution.project_rules import (
    PAPER_FIBER_RULE_KEY,
    normalized_fact,
    resolve_rule,
)
from app.execution.models import (
    ExecutionCredential,
    ExecutionArtifact,
    ExecutionExternalAttempt,
    ExecutionExternalOperation,
    ExecutionFileIndexEntry,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    utcnow,
)
from app.execution.persistence import build_file_gateway
from app.execution.storage import ArtifactRef, StorageError
from app.execution.workbook_format import (
    WorkbookFormat,
    detect_workbook_format,
)


LEGACY_REGENERATED_COUNT_NODE = (
    "external.legacy_regenerated_fiber_count_upload"
)
LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE = (
    "external.legacy_special_wool_image_upload"
)
LEGACY_SPECIAL_WOOL_REVIEW_NODE = "external.legacy_special_wool_review"
LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE = (
    "external.legacy_microscopy_check_record_entry"
)
LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE = (
    "external.legacy_special_wool_qualitative_upload"
)
LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE = (
    "external.legacy_special_wool_qualitative_review"
)
LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE = (
    "external.legacy_generic_check_record_entry"
)
LEGACY_CONNECTOR_KEY = "legacy_fibrecheck"
LEGACY_CREDENTIAL_SYSTEM = "legacy_inspection"
LEGACY_REMOTE_MODULE = (
    "inspection_record_registration:special_fiber:inspection"
)
OPERATION_KEY_PREFIX = "legacy-regenerated-count-upload:v1"
SPECIAL_WOOL_IMAGE_OPERATION_KEY_PREFIX = (
    "legacy-special-wool-image-upload:v1"
)
SPECIAL_WOOL_REVIEW_OPERATION_KEY_PREFIX = "legacy-special-wool-review:v1"
MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION_KEY_PREFIX = (
    "legacy-microscopy-check-record-entry:v1"
)
SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION_KEY_PREFIX = (
    "legacy-special-wool-qualitative-upload:v1"
)
SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION_KEY_PREFIX = (
    "legacy-special-wool-qualitative-review:v1"
)
GENERIC_CHECK_RECORD_ENTRY_OPERATION_KEY_PREFIX = (
    "legacy-generic-check-record-entry:v1"
)
LEGACY_REGENERATED_COUNT_OPERATION = (
    "legacy_regenerated_fiber_count_upload"
)
LEGACY_SPECIAL_WOOL_IMAGE_OPERATION = "legacy_special_wool_image_upload"
LEGACY_SPECIAL_WOOL_REVIEW_OPERATION = "legacy_special_wool_review"
LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION = (
    "legacy_microscopy_check_record_entry"
)
LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION = (
    "legacy_special_wool_qualitative_upload"
)
LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION = (
    "legacy_special_wool_qualitative_review"
)
LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION = (
    "legacy_generic_check_record_entry"
)
ACTIVE_REMOTE_OPERATION_STATUSES = (
    "prepared",
    "approved",
    "in_progress",
    "cancel_pending",
    "reconciliation_required",
)
EXTERNAL_ATTEMPT_ACTIVE_STATUSES = frozenset({"claimed", "in_progress"})
EXTERNAL_ATTEMPT_TERMINAL_STATUSES = frozenset({"failed", "completed"})
EXTERNAL_OPERATION_EXPIRY_ERROR_CODES = frozenset(
    {
        "external_operation_preflight_expired",
        "external_operation_approval_expired",
    }
)
BRIDGE_LEASE_SECONDS = 120
BRIDGE_STDOUT_LIMIT = 4000
BRIDGE_GLOBAL_WRITE_CAPACITY = 1
BRIDGE_ACTIVE_WRITE_STATUSES = ("in_progress", "cancel_pending")
# Stable, process-independent key for the PostgreSQL transaction advisory lock
# that serializes the global Bridge capacity check and claim transition.
BRIDGE_GLOBAL_CLAIM_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"external-bridge-global-claim-capacity:v1").digest()[:8],
    byteorder="big",
    signed=True,
)
# 到达 file_copy_started 即视为可能已经接触旧系统：之前的阶段失败可以安全
# 重领，之后（含）的失败必须转入人工对账，不能自动重试或放行同一样品。
EXTERNAL_REMOTE_WRITE_STAGE = "file_copy_started"
EXTERNAL_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_absence_verified",
    "file_copy_ready",
    "file_copy_started",
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "completed",
)
SPECIAL_WOOL_IMAGE_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "task_project_verified",
    "file_copy_ready",
    "file_copy_started",
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "completed",
)
SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "review_save_ready",
    "review_save_started",
    "review_main_verified",
    "review_children_verified",
    "completed",
)
MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "excel_write_ready",
    "excel_collection_started",
    "remote_file_verified",
    "excel_register_verified",
    "excel_proof_verified",
    "completed",
)
SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "task_project_verified",
    "file_copy_ready",
    "file_copy_started",
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "completed",
)
SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "review_save_ready",
    "review_save_started",
    "review_main_verified",
    "completed",
)
GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "generic_write_ready",
    "generic_save_started",
    "generic_rows_verified",
    "generic_projection_verified",
    "completed",
)
EXTERNAL_OPERATION_STAGE_PROFILES = {
    LEGACY_REGENERATED_COUNT_OPERATION: (
        EXTERNAL_ATTEMPT_STAGES,
        EXTERNAL_REMOTE_WRITE_STAGE,
        "main_record_verified",
    ),
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION: (
        SPECIAL_WOOL_IMAGE_ATTEMPT_STAGES,
        EXTERNAL_REMOTE_WRITE_STAGE,
        "main_record_verified",
    ),
    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION: (
        SPECIAL_WOOL_REVIEW_ATTEMPT_STAGES,
        "review_save_started",
        "review_children_verified",
    ),
    LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION: (
        MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES,
        "excel_collection_started",
        "excel_proof_verified",
    ),
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION: (
        SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES,
        "file_copy_started",
        "main_record_verified",
    ),
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION: (
        SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES,
        "review_save_started",
        "review_main_verified",
    ),
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION: (
        GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES,
        "generic_save_started",
        "generic_projection_verified",
    ),
}
SPECIAL_WOOL_EXECUTION_CAPABILITY = {
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION: {
        "available": False,
        "code": "legacy_special_wool_image_write_disabled",
        "message": "图片类特种毛记录写入当前未在部署环境启用",
    },
    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION: {
        "available": False,
        "code": "legacy_special_wool_review_write_disabled",
        "message": "特纤复核写入当前未在部署环境启用",
    },
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION: {
        "available": False,
        "code": "legacy_special_wool_qualitative_write_disabled",
        "message": "定性原始记录写入当前未在部署环境启用",
    },
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION: {
        "available": False,
        "code": "legacy_special_wool_qualitative_review_disabled",
        "message": "定性原始记录复核当前未在部署环境启用",
    },
}
_LEGACY_SAMPLE_NUMBER_RE = re.compile(
    r"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$"
)
_TASK_PROJECT_KEY_RE = re.compile(r"^task-project:[0-9a-f]{24}$")
_REDACTED_LEGACY_ID_RE = re.compile(r"^sha256:[0-9a-f]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SPECIAL_WOOL_IMAGE_OBSERVATION_TYPE = (
    "legacy_special_wool_image_upload_dry_run"
)
SPECIAL_WOOL_REVIEW_OBSERVATION_TYPE = (
    "legacy_special_wool_review_dry_run"
)
SPECIAL_WOOL_IMAGE_RECEIPT_TYPE = "legacy_special_wool_image_upload"
SPECIAL_WOOL_REVIEW_RECEIPT_TYPE = "legacy_special_wool_review"
MICROSCOPY_CHECK_RECORD_ENTRY_RECEIPT_TYPE = (
    "legacy_microscopy_check_record_entry"
)
SPECIAL_WOOL_QUALITATIVE_UPLOAD_RECEIPT_TYPE = (
    "legacy_special_wool_qualitative_upload"
)
SPECIAL_WOOL_QUALITATIVE_REVIEW_RECEIPT_TYPE = (
    "legacy_special_wool_qualitative_review"
)
GENERIC_CHECK_RECORD_ENTRY_RECEIPT_TYPE = (
    "legacy_generic_check_record_entry"
)
FINAL_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT = (
    "microscopy_final_entry_v1"
)
GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT = (
    "generic_check_record_entry_v1"
)
PAPER_FIBER_ROOT_ID = "paper_fiber_records"
PAPER_FIBER_PROJECT_NAME = "纸、纸板和纸浆纤维鉴别分析"
PAPER_FIBER_TEST_METHOD = "GB/T 4688-2020"
PAPER_FIBER_SPECIAL_WOOL_ITEM = "棉再生纤定性"
_PAPER_STANDALONE_100_RE = re.compile(
    r"(?<![\w.])100(?:\.0+)?(?![\w.])"
)
_LEGACY_ORIGINAL_DATA_FILENAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.xls$"
)


def _canonical_checksum(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _normalized_business_text(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )


def _validated_microscopy_project_binding(
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """Bind preflight to the exact task project selected by the human task.

    IDs in the task snapshot are already one-way hashes emitted by the
    read-only Windows probe.  The future Writer must re-query raw IDs and
    recompute the same project key; the backend never accepts or exposes a raw
    FibreCheck identifier here.
    """

    selected_key = str(input_data.get("selected_project_key") or "").strip()
    selected = input_data.get("selected_project")
    if not _TASK_PROJECT_KEY_RE.fullmatch(selected_key) or not isinstance(
        selected, dict
    ):
        raise ExecutionApiError(
            422,
            "legacy_special_wool_task_project_required",
            "图片上传预检缺少人工确认的任务项目，请刷新任务信息后重试",
        )
    if str(selected.get("project_key") or "").strip() != selected_key:
        raise conflict(
            "legacy_special_wool_task_project_changed",
            "人工确认的任务项目键与项目快照不一致，请重新选择",
        )
    task_check_item_id = str(
        selected.get("task_check_item_id") or ""
    ).strip()
    check_item_id = str(selected.get("check_item_id") or "").strip()
    if not _REDACTED_LEGACY_ID_RE.fullmatch(
        task_check_item_id
    ) or not _REDACTED_LEGACY_ID_RE.fullmatch(check_item_id):
        raise ExecutionApiError(
            422,
            "legacy_special_wool_task_project_id_missing",
            "任务项目快照缺少只读探针签发的项目标识，请刷新任务信息后重试",
        )
    check_item_name = _normalized_business_text(
        selected.get("check_item_name")
    )
    check_item_no = _normalized_business_text(selected.get("check_item_no"))
    check_method = _normalized_business_text(selected.get("check_method"))
    if check_item_name not in ALL_PROJECT_NAME_ALIASES:
        raise ExecutionApiError(
            422,
            "legacy_special_wool_task_project_name_mismatch",
            "图片上传仅支持“纤维微观形貌”“膜平面形貌”或“纤维横截面”任务项目",
            details={"expected": sorted(ALL_PROJECT_NAME_ALIASES)},
        )
    if check_method != ELECTRON_TEST_METHOD:
        raise ExecutionApiError(
            422,
            "legacy_special_wool_task_project_method_mismatch",
            "图片上传仅支持测试方法 GB/T 36422-2018",
            details={"expected": ELECTRON_TEST_METHOD},
        )
    seq_num = selected.get("seq_num")
    check_count = selected.get("check_count")
    if (
        not isinstance(seq_num, int)
        or isinstance(seq_num, bool)
        or seq_num < 0
        or not isinstance(check_count, int)
        or isinstance(check_count, bool)
        or check_count < 1
    ):
        raise ExecutionApiError(
            422,
            "legacy_special_wool_task_project_count_invalid",
            "微观形貌任务项目的顺序或检验份数已变化；检验份数必须至少为 1",
        )
    expected_project_key = "task-project:" + hashlib.sha256(
        "\0".join(
            (
                task_check_item_id,
                check_item_id,
                check_item_no,
                check_item_name,
                check_method,
                str(seq_num),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    if selected_key != expected_project_key:
        raise conflict(
            "legacy_special_wool_task_project_changed",
            "任务项目键与当前项目字段不一致，请刷新任务信息后重新选择",
        )
    return {
        "project_key": selected_key,
        "task_check_item_id": task_check_item_id,
        "check_item_id": check_item_id,
        "check_item_no": check_item_no,
        "check_item_name": check_item_name,
        "check_method": check_method,
        "seq_num": seq_num,
        "check_count": check_count,
    }


def _validated_paper_project_binding(
    input_data: dict[str, Any],
    rule=None,
) -> dict[str, Any]:
    """Validate the exact cached task project selected by the paper reader.

    The accepted item name / test method come from the admin-editable
    project rule when provided; the code constants remain the fallback.
    """

    if rule is not None:
        allowed_names = {
            normalized_fact(value)
            for value in rule.fact_values("task_item_name")
        } or {normalized_fact(PAPER_FIBER_PROJECT_NAME)}
        method_values = rule.fact_values("test_method")
        expected_method = (
            normalized_fact(method_values[0])
            if method_values
            else normalized_fact(PAPER_FIBER_TEST_METHOD)
        )
    else:
        allowed_names = {normalized_fact(PAPER_FIBER_PROJECT_NAME)}
        expected_method = normalized_fact(PAPER_FIBER_TEST_METHOD)
    selected_key = str(input_data.get("selected_project_key") or "").strip()
    selected = input_data.get("selected_project")
    if not _TASK_PROJECT_KEY_RE.fullmatch(selected_key) or not isinstance(
        selected, dict
    ):
        raise ExecutionApiError(
            422,
            "paper_fiber_task_project_required",
            "纸纤维流程缺少已匹配的旧系统任务项目，请刷新任务信息后重试",
        )
    task_check_item_id = str(
        selected.get("task_check_item_id") or ""
    ).strip()
    check_item_id = str(selected.get("check_item_id") or "").strip()
    check_item_no = _normalized_business_text(selected.get("check_item_no"))
    check_item_name = _normalized_business_text(
        selected.get("check_item_name")
    )
    check_method = _normalized_business_text(selected.get("check_method"))
    seq_num = selected.get("seq_num")
    check_count = selected.get("check_count")
    if (
        str(selected.get("project_key") or "").strip() != selected_key
        or not _REDACTED_LEGACY_ID_RE.fullmatch(task_check_item_id)
        or not _REDACTED_LEGACY_ID_RE.fullmatch(check_item_id)
        or check_item_name not in allowed_names
        or check_method != expected_method
        or not isinstance(seq_num, int)
        or isinstance(seq_num, bool)
        or seq_num < 0
        or not isinstance(check_count, int)
        or isinstance(check_count, bool)
        or check_count < 1
    ):
        raise ExecutionApiError(
            422,
            "paper_fiber_task_project_mismatch",
            "任务项目必须是 GB/T 4688-2020 纸、纸板和纸浆纤维鉴别分析且份数大于 0",
        )
    expected_project_key = "task-project:" + hashlib.sha256(
        "\0".join(
            (
                task_check_item_id,
                check_item_id,
                check_item_no,
                check_item_name,
                check_method,
                str(seq_num),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    if selected_key != expected_project_key:
        raise conflict(
            "paper_fiber_task_project_changed",
            "纸纤维任务项目键与当前项目字段不一致，请刷新后重试",
        )
    return {
        "project_key": selected_key,
        "task_check_item_id": task_check_item_id,
        "check_item_id": check_item_id,
        "check_item_no": check_item_no,
        "check_item_name": check_item_name,
        "check_method": check_method,
        "seq_num": seq_num,
        "check_count": check_count,
    }


def _machine_document_error(path: str, message: str) -> ExecutionApiError:
    return ExecutionApiError(
        422,
        "legacy_special_wool_machine_document_invalid",
        message,
        details={"path": path},
    )


def _strict_object(
    value: Any,
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _machine_document_error(path, "旧系统机器文档字段必须是对象")
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise _machine_document_error(
            path,
            "旧系统机器文档字段集合不符合已发布契约",
        )
    return value


def _required_text(
    value: Any,
    *,
    path: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _machine_document_error(path, "旧系统机器文档缺少必要文本")
    normalized = value.strip()
    if pattern is not None and not pattern.fullmatch(normalized):
        raise _machine_document_error(path, "旧系统机器文档文本格式无效")
    return normalized


def _required_count(value: Any, *, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _machine_document_error(path, "旧系统机器文档计数必须是非负整数")
    return value


def _validate_existing_record_decision_receipt(
    actual: Any,
    *,
    expected: Any,
    path: str = "$.existing_record_decision",
) -> None:
    if expected is None:
        if actual is not None:
            raise _machine_document_error(
                path, "普通业务回执不得声明已有登记追加确认"
            )
        return
    if not isinstance(expected, dict):
        raise _machine_document_error(
            "$.request_summary.existing_record_decision",
            "预检单中的已有登记追加确认无效",
        )
    decision = _strict_object(
        actual,
        path=path,
        required={
            "kind",
            "action",
            "expected_task_check_count",
            "expected_existing_register_count",
            "resulting_register_count",
        },
    )
    if decision != expected:
        raise _machine_document_error(
            path, "已有登记追加确认回执与已批准的任务包不一致"
        )


def _validate_bound_project_document(
    value: Any,
    *,
    expected: dict[str, Any],
    path: str,
    require_match_count: bool,
    require_check_count: bool = False,
) -> dict[str, Any]:
    required = {
        "project_key",
        "task_check_item_id",
        "check_item_id",
        "check_item_no",
        "check_item_name",
        "check_method",
        "seq_num",
    }
    if require_match_count:
        required.add("match_count")
    if require_check_count:
        required.add("check_count")
    project = _strict_object(
        value,
        path=path,
        required=required,
        optional=(None if require_check_count else {"check_count"}),
    )
    _required_text(
        project.get("project_key"),
        path=f"{path}.project_key",
        pattern=_TASK_PROJECT_KEY_RE,
    )
    _required_text(
        project.get("task_check_item_id"),
        path=f"{path}.task_check_item_id",
        pattern=_REDACTED_LEGACY_ID_RE,
    )
    _required_text(
        project.get("check_item_id"),
        path=f"{path}.check_item_id",
        pattern=_REDACTED_LEGACY_ID_RE,
    )
    if require_match_count and _required_count(
        project.get("match_count"), path=f"{path}.match_count"
    ) != 1:
        raise _machine_document_error(
            f"{path}.match_count", "任务项目必须且只能匹配一条旧系统记录"
        )
    if require_check_count:
        check_count = _required_count(
            project.get("check_count"), path=f"{path}.check_count"
        )
        if check_count < 1:
            raise _machine_document_error(
                f"{path}.check_count", "任务项目检验份数必须大于 0"
            )
    for key in (
        "project_key",
        "task_check_item_id",
        "check_item_id",
        "check_item_no",
        "check_item_name",
        "check_method",
        "seq_num",
    ):
        if project.get(key) != expected.get(key):
            raise _machine_document_error(
                f"{path}.{key}", "旧系统只读结果与流程预检绑定的任务项目不一致"
            )
    if "check_count" in project:
        _required_count(project.get("check_count"), path=f"{path}.check_count")
        if project.get("check_count") != expected.get("check_count"):
            raise _machine_document_error(
                f"{path}.check_count",
                "旧系统只读结果与流程预检绑定的任务项目不一致",
            )
    return project


def validate_special_wool_machine_observation(
    operation: ExecutionExternalOperation,
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Strictly validate a future Windows read-only dry-run observation.

    The function deliberately does not change ``operation`` or its capability;
    callers may persist the returned document only after an authenticated
    read-only transport is introduced.
    """

    operation_type = _operation_type(operation)
    summary = operation.request_summary or {}
    common_required = {
        "schema_version",
        "observation_type",
        "mode",
        "operation_id",
        "payload_checksum",
        "generated_at",
        "observation_checksum",
        "target_sample_number",
        "write_performed",
        "ready_for_write",
    }
    if operation_type == LEGACY_SPECIAL_WOOL_IMAGE_OPERATION:
        document = _strict_object(
            observation,
            path="$",
            required=common_required
            | {
                "source_inspection_number",
                "target_family",
                "task_project",
                "picture_readback",
            },
        )
        expected_type = SPECIAL_WOOL_IMAGE_OBSERVATION_TYPE
    elif operation_type == LEGACY_SPECIAL_WOOL_REVIEW_OPERATION:
        document = _strict_object(
            observation,
            path="$",
            required=common_required
            | {"source_upload", "permission", "remote_state", "would_update"},
        )
        expected_type = SPECIAL_WOOL_REVIEW_OBSERVATION_TYPE
    else:
        raise _machine_document_error("$", "当前操作不接受特种毛机器观察文档")
    if document.get("schema_version") != 1:
        raise _machine_document_error("$.schema_version", "机器文档版本不受支持")
    if document.get("observation_type") != expected_type:
        raise _machine_document_error("$.observation_type", "机器观察类型与操作不一致")
    if document.get("mode") != "read_only":
        raise _machine_document_error("$.mode", "机器观察必须来自只读模式")
    if document.get("write_performed") is not False or document.get(
        "ready_for_write"
    ) is not False:
        raise _machine_document_error(
            "$.write_performed", "禁写阶段的机器观察不得声明已执行或可执行写入"
        )
    expected_pairs = {
        "operation_id": operation.id,
        "payload_checksum": operation.payload_checksum,
        "target_sample_number": summary.get("target_sample_number"),
    }
    for key, expected in expected_pairs.items():
        if document.get(key) != expected:
            raise _machine_document_error(f"$.{key}", "机器观察与预检单不一致")
    _required_text(document.get("generated_at"), path="$.generated_at")
    _required_text(
        document.get("observation_checksum"),
        path="$.observation_checksum",
        pattern=_SHA256_RE,
    )

    if operation_type == LEGACY_SPECIAL_WOOL_IMAGE_OPERATION:
        if document.get("source_inspection_number") != summary.get(
            "source_inspection_number"
        ):
            raise _machine_document_error(
                "$.source_inspection_number", "机器观察的源编号与预检单不一致"
            )
        family = _strict_object(
            document.get("target_family"),
            path="$.target_family",
            required={
                "base_number",
                "occupied_numbers",
                "ignored_numbers",
                "candidate_number",
                "candidate_exact_count",
                "unique_sample_number_constraint",
            },
        )
        if family.get("base_number") != (
            (summary.get("target_allocation") or {}).get("base_number")
        ) or family.get("candidate_number") != summary.get(
            "target_sample_number"
        ):
            raise _machine_document_error(
                "$.target_family", "远端编号族观察与预检分配结果不一致"
            )
        for key in ("occupied_numbers", "ignored_numbers"):
            if not isinstance(family.get(key), list) or not all(
                isinstance(item, str) for item in family.get(key)
            ):
                raise _machine_document_error(
                    f"$.target_family.{key}", "编号族字段必须是文本列表"
                )
        _required_count(
            family.get("candidate_exact_count"),
            path="$.target_family.candidate_exact_count",
        )
        if family.get("unique_sample_number_constraint") not in {
            True,
            False,
            None,
        }:
            raise _machine_document_error(
                "$.target_family.unique_sample_number_constraint",
                "编号唯一约束状态必须是 true、false 或 null",
            )
        _validate_bound_project_document(
            document.get("task_project"),
            expected=dict(summary.get("task_project") or {}),
            path="$.task_project",
            require_match_count=True,
            require_check_count=True,
        )
        readback = _strict_object(
            document.get("picture_readback"),
            path="$.picture_readback",
            required={"main_count", "picture_count", "records"},
        )
        main_count = _required_count(
            readback.get("main_count"), path="$.picture_readback.main_count"
        )
        picture_count = _required_count(
            readback.get("picture_count"),
            path="$.picture_readback.picture_count",
        )
        records = readback.get("records")
        if not isinstance(records, list) or len(records) != max(
            main_count, picture_count
        ):
            raise _machine_document_error(
                "$.picture_readback.records", "图片记录明细数量与只读计数不一致"
            )
    else:
        source_ref = _strict_object(
            document.get("source_upload"),
            path="$.source_upload",
            required={"operation_id", "receipt_checksum", "main_id"},
        )
        expected_source = summary.get("source_operation") or {}
        if source_ref.get("operation_id") != expected_source.get(
            "operation_id"
        ) or source_ref.get("receipt_checksum") != expected_source.get(
            "receipt_checksum"
        ) or source_ref.get("main_id") != expected_source.get(
            "main_id"
        ):
            raise _machine_document_error(
                "$.source_upload", "复核观察引用的上传回执与预检单不一致"
            )
        _required_text(
            source_ref.get("main_id"),
            path="$.source_upload.main_id",
            pattern=_REDACTED_LEGACY_ID_RE,
        )
        permission = _strict_object(
            document.get("permission"),
            path="$.permission",
            required={"function_type", "granted", "btn_check"},
        )
        if not str(permission.get("function_type") or "").endswith(
            ".SpecialWoolCheckUI"
        ) or permission.get("granted") is not True:
            raise _machine_document_error(
                "$.permission", "复核 dry-run 未证明 SpecialWoolCheckUI 权限"
            )
        btn_check = _strict_object(
            permission.get("btn_check"),
            path="$.permission.btn_check",
            required={"configured", "enabled"},
        )
        if not isinstance(btn_check.get("configured"), bool) or (
            btn_check.get("configured") is True
            and btn_check.get("enabled") is not True
        ) or (
            btn_check.get("configured") is False
            and btn_check.get("enabled") is not None
        ):
            raise _machine_document_error(
                "$.permission.btn_check", "btnCheck 控件权限事实不完整或未授权"
            )
        remote = _strict_object(
            document.get("remote_state"),
            path="$.remote_state",
            required={
                "main_count",
                "review_user",
                "review_time",
                "picture_count",
                "children_fingerprint",
            },
        )
        if _required_count(remote.get("main_count"), path="$.remote_state.main_count") != 1:
            raise _machine_document_error(
                "$.remote_state.main_count", "复核目标必须恰好存在一条主记录"
            )
        if remote.get("review_user") is not None or remote.get(
            "review_time"
        ) is not None:
            raise _machine_document_error(
                "$.remote_state.review_user", "目标记录已经复核，不能再次处理"
            )
        _required_count(
            remote.get("picture_count"), path="$.remote_state.picture_count"
        )
        _required_text(
            remote.get("children_fingerprint"),
            path="$.remote_state.children_fingerprint",
            pattern=_SHA256_RE,
        )
        would_update = _strict_object(
            document.get("would_update"),
            path="$.would_update",
            required={
                "main",
                "conditional_check_user5",
                "picture_children",
                "wool_children",
                "quantification_children",
            },
        )
        if would_update.get("main") != ["ReviewUser", "ReviewTime"] or (
            would_update.get("picture_children") != []
        ):
            raise _machine_document_error(
                "$.would_update", "复核 dry-run 与已取证的旧客户端保存语义不一致"
            )
    return document


def _receipt_stage_names(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _machine_document_error(path, "写入回执缺少阶段列表")
    result: list[str] = []
    for index, item in enumerate(value):
        stage = item.get("stage") if isinstance(item, dict) else item
        result.append(_required_text(stage, path=f"{path}[{index}].stage"))
    return result


def _validate_special_wool_server_file_verification(
    value: Any,
    *,
    source_artifact: dict[str, Any],
    server_file: dict[str, Any],
) -> dict[str, Any]:
    path = "$.server_file.verification"
    verification = _strict_object(
        value,
        path=path,
        required={
            "mode",
            "source_size_bytes",
            "source_content_sha256",
            "remote_size_bytes",
            "remote_content_sha256",
            "stream_paths_equal",
            "stream_sizes_equal",
            "non_workbook_streams_equal",
            "biff_record_boundaries_equal",
            "changed_record_ids",
            "changed_record_count",
        },
    )
    mode = _required_text(
        verification.get("mode"),
        path=f"{path}.mode",
    )
    source_size = _required_count(
        verification.get("source_size_bytes"),
        path=f"{path}.source_size_bytes",
    )
    source_sha256 = _required_text(
        verification.get("source_content_sha256"),
        path=f"{path}.source_content_sha256",
        pattern=_SHA256_RE,
    )
    remote_size = _required_count(
        verification.get("remote_size_bytes"),
        path=f"{path}.remote_size_bytes",
    )
    remote_sha256 = _required_text(
        verification.get("remote_content_sha256"),
        path=f"{path}.remote_content_sha256",
        pattern=_SHA256_RE,
    )
    if (
        source_size != source_artifact.get("size_bytes")
        or source_sha256 != source_artifact.get("content_sha256")
    ):
        raise _machine_document_error(
            path,
            "服务器文件核验中的源文件标识与预检制品不一致",
        )
    if (
        remote_size != server_file.get("size_bytes")
        or remote_sha256 != server_file.get("content_sha256")
    ):
        raise _machine_document_error(
            path,
            "服务器文件核验中的远端文件标识与实际回执不一致",
        )
    for key in (
        "stream_paths_equal",
        "stream_sizes_equal",
        "non_workbook_streams_equal",
        "biff_record_boundaries_equal",
    ):
        if verification.get(key) is not True:
            raise _machine_document_error(
                f"{path}.{key}",
                "服务器工作簿逻辑结构核验未通过",
            )
    changed_record_ids = verification.get("changed_record_ids")
    changed_record_count = _required_count(
        verification.get("changed_record_count"),
        path=f"{path}.changed_record_count",
    )
    if mode == "exact_sha256":
        if (
            source_size != remote_size
            or source_sha256 != remote_sha256
            or changed_record_ids != []
            or changed_record_count != 0
        ):
            raise _machine_document_error(
                path,
                "精确哈希核验回执与源文件、远端文件或"
                "变更记录不一致",
            )
    elif mode == "cfb_biff_writeaccess_only":
        if (
            source_sha256 == remote_sha256
            or changed_record_ids != ["0x005C"]
            or changed_record_count < 1
        ):
            raise _machine_document_error(
                path,
                "CFB/BIFF 规范化只能包含 WRITEACCESS(0x005C) 记录变化",
            )
    else:
        raise _machine_document_error(
            f"{path}.mode",
            "服务器文件核验模式不受支持",
        )
    return verification


def validate_external_receipt(
    operation: ExecutionExternalOperation,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Validate operation-specific machine receipts before state completion."""

    operation_type = _operation_type(operation)
    if operation_type not in {
        LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
        LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
    }:
        if not isinstance(receipt, dict) or not receipt:
            raise ExecutionApiError(
                422, "external_receipt_invalid", "旧系统上传回执不能为空"
            )
        return receipt
    summary = operation.request_summary or {}
    common = {
        "schema_version",
        "receipt_type",
        "operation_id",
        "payload_checksum",
        "target_sample_number",
        "stages",
        "reconciliation_required",
    }
    if operation_type == LEGACY_SPECIAL_WOOL_IMAGE_OPERATION:
        document = _strict_object(
            receipt,
            path="$",
            required=common
            | {
                "target_filename",
                "source_artifact",
                "task_project",
                "server_file",
                "main_record",
                "picture_records",
                "readback",
            },
            # 顺号改写：Writer 按旧系统锁内实况取第一空闲号，
            # 回执用 requested_sample_number 绑定预检单。
            optional={"requested_sample_number", "renumbered"},
        )
        expected_type = SPECIAL_WOOL_IMAGE_RECEIPT_TYPE
    elif operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION:
        document = _strict_object(
            receipt,
            path="$",
            required=common
            | {
                "target_filename",
                "source_artifact",
                "task_project",
                "server_file",
                "main_record",
                "picture_count",
                "readback",
            },
            optional={"requested_sample_number", "renumbered"},
        )
        expected_type = SPECIAL_WOOL_QUALITATIVE_UPLOAD_RECEIPT_TYPE
    elif operation_type in {
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    }:
        document = _strict_object(
            receipt,
            path="$",
            required=common
            | {"source_upload", "main_record", "children", "readback"},
        )
        expected_type = (
            SPECIAL_WOOL_QUALITATIVE_REVIEW_RECEIPT_TYPE
            if operation_type
            == LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
            else SPECIAL_WOOL_REVIEW_RECEIPT_TYPE
        )
    elif operation_type == LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION:
        document = _strict_object(
            receipt,
            path="$",
            required=common
            | {
                "source_artifact",
                "task_project",
                "template_binding",
                "final_entry",
            },
            # 受控测试覆盖通道已于 2026-08 移除；当前部署的 Writer 仍会在
            # 回执中携带 null，保持容忍但拒绝任何非空声明。
            optional={"existing_record_decision", "controlled_test_override"},
        )
        expected_type = MICROSCOPY_CHECK_RECORD_ENTRY_RECEIPT_TYPE
    else:
        document = _strict_object(
            receipt,
            path="$",
            required=common | {"task_project", "final_entry"},
            optional={"existing_record_decision", "controlled_test_override"},
        )
        expected_type = GENERIC_CHECK_RECORD_ENTRY_RECEIPT_TYPE
    if document.get("schema_version") != 1 or document.get(
        "receipt_type"
    ) != expected_type:
        raise _machine_document_error("$.receipt_type", "写入回执类型或版本不正确")
    for key, expected in {
        "operation_id": operation.id,
        "payload_checksum": operation.payload_checksum,
    }.items():
        if document.get(key) != expected:
            raise _machine_document_error(f"$.{key}", "写入回执与外部操作预检单不一致")
    expected_target = summary.get("target_sample_number")
    requested_target = document.get("requested_sample_number")
    if requested_target is None:
        # 旧版 Writer 回执：目标编号必须与预检单完全一致。
        if document.get("target_sample_number") != expected_target:
            raise _machine_document_error(
                "$.target_sample_number", "写入回执与外部操作预检单不一致"
            )
    else:
        # 顺号改写回执：requested 绑定预检单，target 是旧系统实况决定的
        # 实际写入编号，必须仍属于同一编号族。
        if requested_target != expected_target:
            raise _machine_document_error(
                "$.requested_sample_number", "写入回执与外部操作预检单不一致"
            )
        actual_target = document.get("target_sample_number")
        target_base = str(
            (summary.get("target_allocation") or {}).get("base_number")
            or expected_target
            or ""
        ).strip().upper()
        if (
            not isinstance(actual_target, str)
            or not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(actual_target)
            or not (
                actual_target == requested_target
                or actual_target.startswith(target_base + "-")
            )
        ):
            raise _machine_document_error(
                "$.target_sample_number",
                "写入回执的实际目标编号不属于预检单编号族",
            )
        renumbered = document.get("renumbered")
        if not isinstance(renumbered, bool) or renumbered != (
            actual_target != requested_target
        ):
            raise _machine_document_error(
                "$.renumbered", "写入回执的顺号标记与实际编号不一致"
            )
    if document.get("reconciliation_required") is not False:
        raise _machine_document_error(
            "$.reconciliation_required", "需要人工对账的结果不能作为成功回执"
        )
    stage_names = _receipt_stage_names(document.get("stages"), path="$.stages")
    allowed_stages, _boundary, verified_stage = _operation_stage_profile(operation)
    indexes: list[int] = []
    for stage in stage_names:
        if stage not in allowed_stages:
            raise _machine_document_error("$.stages", "写入回执包含未知阶段")
        indexes.append(allowed_stages.index(stage))
    if indexes != sorted(set(indexes)) or verified_stage not in stage_names:
        raise _machine_document_error(
            "$.stages", "写入回执阶段必须有序、无重复且包含最终核验阶段"
        )

    if (
        operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        and stage_names
        != list(SPECIAL_WOOL_QUALITATIVE_UPLOAD_ATTEMPT_STAGES)
    ):
        raise _machine_document_error(
            "$.stages", "文档型特纤上传回执阶段不完整或顺序错误"
        )
    if (
        operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
        and stage_names
        != list(SPECIAL_WOOL_QUALITATIVE_REVIEW_ATTEMPT_STAGES)
    ):
        raise _machine_document_error(
            "$.stages", "文档型特纤复核回执阶段不完整或顺序错误"
        )

    if operation_type == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION:
        if stage_names != list(GENERIC_CHECK_RECORD_ENTRY_ATTEMPT_STAGES):
            raise _machine_document_error(
                "$.stages",
                "通用检验记录登记回执必须包含完整保存与读回阶段",
            )
        _validate_bound_project_document(
            document.get("task_project"),
            expected=dict(summary.get("task_project") or {}),
            path="$.task_project",
            require_match_count=False,
            require_check_count=True,
        )
        package = summary.get("final_entry_package") or {}
        expected_existing = _required_count(
            package.get("expected_existing_register_count"),
            path="$.request_summary.final_entry_package."
            "expected_existing_register_count",
        )
        final_entry = _strict_object(
            document.get("final_entry"),
            path="$.final_entry",
            required={
                "package_schema_version",
                "expected_existing_register_count",
                "resulting_register_count",
                "detail_count",
                "key_result_count",
                "record_id",
                "proofed",
            },
        )
        if (
            final_entry.get("package_schema_version") != 2
            or final_entry.get("expected_existing_register_count")
            != expected_existing
            or final_entry.get("resulting_register_count")
            != expected_existing + 1
            or final_entry.get("detail_count") != 1
            or final_entry.get("key_result_count") != 1
            or final_entry.get("proofed") is not False
        ):
            raise _machine_document_error(
                "$.final_entry",
                "通用登记、明细、结果投影或未校对状态读回不一致",
            )
        _required_text(
            final_entry.get("record_id"),
            path="$.final_entry.record_id",
            pattern=_REDACTED_LEGACY_ID_RE,
        )
        # 受控测试覆盖通道已移除：后端不再签发覆盖对象，任何非空声明
        # 一律拒绝；当前部署的 Writer 会在回执中携带 null，保持容忍。
        if document.get("controlled_test_override") is not None:
            raise _machine_document_error(
                "$.controlled_test_override", "普通业务回执不得声明受控测试覆盖"
            )
        _validate_existing_record_decision_receipt(
            document.get("existing_record_decision"),
            expected=package.get("existing_record_decision"),
        )
        return document

    if operation_type == LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION:
        if stage_names != list(MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES):
            raise _machine_document_error(
                "$.stages",
                "检验记录登记回执必须包含从登录到校对核验的完整阶段",
            )
        expected_file = list(summary.get("files") or [{}])[0]
        artifact = _strict_object(
            document.get("source_artifact"),
            path="$.source_artifact",
            required={"artifact_id", "filename", "size_bytes", "content_sha256"},
        )
        for key in ("artifact_id", "filename", "size_bytes", "content_sha256"):
            if artifact.get(key) != expected_file.get(key):
                raise _machine_document_error(
                    f"$.source_artifact.{key}",
                    "校对回执的工作簿与预检单不一致",
                )
        _validate_bound_project_document(
            document.get("task_project"),
            expected=dict(summary.get("task_project") or {}),
            path="$.task_project",
            require_match_count=False,
            require_check_count=True,
        )
        binding = _strict_object(
            document.get("template_binding"),
            path="$.template_binding",
            required={
                "binding_version",
                "image_count",
                "legacy_template_name",
                "local_asset_name",
                "local_asset_sha256",
                "mapping_config_sha256",
            },
        )
        _required_count(
            binding.get("image_count"),
            path="$.template_binding.image_count",
        )
        if binding != summary.get("template_binding"):
            raise _machine_document_error(
                "$.template_binding", "校对回执的模板绑定与预检单不一致"
            )
        expected_package = summary.get("final_entry_package") or {}
        expected_existing = _required_count(
            expected_package.get("expected_existing_register_count"),
            path="$.request_summary.final_entry_package."
            "expected_existing_register_count",
        )
        final_entry = _strict_object(
            document.get("final_entry"),
            path="$.final_entry",
            required={
                "package_schema_version",
                "expected_existing_register_count",
                "resulting_register_count",
                "key_result_count",
                "record_id",
                "original_data_filename",
                "content_sha256",
                "proofed",
            },
        )
        actual_expected_existing = _required_count(
            final_entry.get("expected_existing_register_count"),
            path="$.final_entry.expected_existing_register_count",
        )
        actual_resulting = _required_count(
            final_entry.get("resulting_register_count"),
            path="$.final_entry.resulting_register_count",
        )
        actual_key_count = _required_count(
            final_entry.get("key_result_count"),
            path="$.final_entry.key_result_count",
        )
        if (
            final_entry.get("package_schema_version") != 2
            or actual_expected_existing != expected_existing
            or actual_resulting != expected_existing + 1
            or actual_key_count != 1
            or final_entry.get("content_sha256")
            != artifact.get("content_sha256")
            or final_entry.get("proofed") is not True
        ):
            raise _machine_document_error(
                "$.final_entry", "新增、关键结果或校对读回计数未通过核对"
            )
        _required_text(
            final_entry.get("record_id"),
            path="$.final_entry.record_id",
            pattern=_REDACTED_LEGACY_ID_RE,
        )
        _required_text(
            final_entry.get("original_data_filename"),
            path="$.final_entry.original_data_filename",
            pattern=_LEGACY_ORIGINAL_DATA_FILENAME_RE,
        )
        # 受控测试覆盖通道已移除：后端不再签发覆盖对象，任何非空声明
        # 一律拒绝；当前部署的 Writer 会在回执中携带 null，保持容忍。
        if document.get("controlled_test_override") is not None:
            raise _machine_document_error(
                "$.controlled_test_override", "普通业务回执不得声明受控测试覆盖"
            )
        _validate_existing_record_decision_receipt(
            document.get("existing_record_decision"),
            expected=expected_package.get("existing_record_decision"),
        )
        return document

    if operation_type in {
        LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
    }:
        qualitative_document = (
            operation_type
            == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        )
        if qualitative_document:
            source_file = list(summary.get("files") or [{}])[0]
            preflight_target_filename = _paper_special_wool_target_filename(
                str(summary.get("target_sample_number") or ""),
                str(source_file.get("filename") or ""),
            )
            expected_target_filename = _paper_special_wool_target_filename(
                str(document.get("target_sample_number") or ""),
                str(source_file.get("filename") or ""),
            )
        else:
            preflight_target_filename = _special_wool_target_filename(
                str(summary.get("target_sample_number") or "")
            )
            expected_target_filename = _special_wool_target_filename(
                str(document.get("target_sample_number") or "")
            )
        if summary.get("target_filename") != preflight_target_filename:
            raise _machine_document_error(
                "$.request_summary.target_filename",
                "预检目标文件名与预检样品编号不一致",
            )
        if document.get("target_filename") != expected_target_filename:
            raise _machine_document_error(
                "$.target_filename",
                "写入回执中的目标文件名与最终样品编号不一致",
            )
        artifact = _strict_object(
            document.get("source_artifact"),
            path="$.source_artifact",
            required={"artifact_id", "filename", "size_bytes", "content_sha256"},
        )
        expected_file = list(summary.get("files") or [{}])[0]
        for key in ("artifact_id", "filename", "size_bytes", "content_sha256"):
            if artifact.get(key) != expected_file.get(key):
                raise _machine_document_error(
                    f"$.source_artifact.{key}", "写入回执中的源制品与预检单不一致"
                )
        _validate_bound_project_document(
            document.get("task_project"),
            expected=dict(summary.get("task_project") or {}),
            path="$.task_project",
            require_match_count=False,
            require_check_count=True,
        )
        server_file = _strict_object(
            document.get("server_file"),
            path="$.server_file",
            required={
                "filename",
                "size_bytes",
                "content_sha256",
                "verification",
            },
        )
        _required_count(
            server_file.get("size_bytes"), path="$.server_file.size_bytes"
        )
        _required_text(
            server_file.get("content_sha256"),
            path="$.server_file.content_sha256",
            pattern=_SHA256_RE,
        )
        if server_file.get("filename") != expected_target_filename:
            raise _machine_document_error(
                "$.server_file.filename", "服务器文件名与实际写入目标不一致"
            )
        _validate_special_wool_server_file_verification(
            server_file.get("verification"),
            source_artifact=artifact,
            server_file=server_file,
        )
        main = _strict_object(
            document.get("main_record"),
            path="$.main_record",
            required={
                "id",
                "field_fingerprint",
                "create_user",
                "create_time",
                "file_path",
            },
        )
        if main.get("file_path") != expected_target_filename:
            raise _machine_document_error(
                "$.main_record.file_path",
                "主记录文件名与实际写入目标不一致",
            )
        for key in ("id", "create_user"):
            _required_text(
                main.get(key), path=f"$.main_record.{key}", pattern=_REDACTED_LEGACY_ID_RE
            )
        _required_text(
            main.get("field_fingerprint"),
            path="$.main_record.field_fingerprint",
            pattern=_SHA256_RE,
        )
        if qualitative_document:
            if document.get("picture_count") != 0:
                raise _machine_document_error(
                    "$.picture_count", "文档型特纤上传不得生成图片子记录"
                )
            readback = _strict_object(
                document.get("readback"),
                path="$.readback",
                required={
                    "main_count",
                    "picture_count",
                    "mismatches",
                    "verified_at",
                    "target_filename",
                },
            )
            if (
                readback.get("main_count") != 1
                or readback.get("picture_count") != 0
                or readback.get("mismatches") != []
                or readback.get("target_filename")
                != expected_target_filename
            ):
                raise _machine_document_error(
                    "$.readback", "文档型特纤上传读回核对未通过"
                )
            return document
        pictures = document.get("picture_records")
        if not isinstance(pictures, list) or pictures:
            raise _machine_document_error(
                "$.picture_records", "图片类特纤上传不得生成图片子记录"
            )
        readback = _strict_object(
            document.get("readback"),
            path="$.readback",
            required={
                "main_count",
                "picture_count",
                "mismatches",
                "verified_at",
                "target_filename",
            },
            optional={"original_data_filename"},
        )
        if _required_count(readback.get("main_count"), path="$.readback.main_count") != 1 or _required_count(
            readback.get("picture_count"), path="$.readback.picture_count"
        ) != 0 or readback.get("mismatches") != [] or readback.get(
            "target_filename"
        ) != expected_target_filename or (
            "original_data_filename" in readback
            and readback.get("original_data_filename")
            != expected_target_filename
        ):
            raise _machine_document_error("$.readback", "图片上传读回核对未通过")
    else:
        source_upload = _strict_object(
            document.get("source_upload"),
            path="$.source_upload",
            required={"operation_id", "receipt_checksum", "main_id"},
        )
        expected_source = summary.get("source_operation") or {}
        if source_upload.get("operation_id") != expected_source.get(
            "operation_id"
        ) or source_upload.get("receipt_checksum") != expected_source.get(
            "receipt_checksum"
        ) or source_upload.get("main_id") != expected_source.get(
            "main_id"
        ):
            raise _machine_document_error("$.source_upload", "复核回执引用了错误的上传回执")
        main = _strict_object(
            document.get("main_record"),
            path="$.main_record",
            required={
                "id",
                "review_user",
                "review_time",
                "pre_fingerprint",
                "post_fingerprint",
            },
        )
        if main.get("id") != source_upload.get("main_id"):
            raise _machine_document_error("$.main_record.id", "复核主记录与上传主记录不一致")
        for key in ("id", "review_user"):
            _required_text(
                main.get(key), path=f"$.main_record.{key}", pattern=_REDACTED_LEGACY_ID_RE
            )
        _required_text(main.get("review_time"), path="$.main_record.review_time")
        for key in ("pre_fingerprint", "post_fingerprint"):
            _required_text(
                main.get(key),
                path=f"$.main_record.{key}",
                pattern=_SHA256_RE,
            )
        children = _strict_object(
            document.get("children"),
            path="$.children",
            required={
                "picture_count",
                "before_fingerprint",
                "after_fingerprint",
                "unchanged",
            },
        )
        if children.get("unchanged") is not True or children.get(
            "before_fingerprint"
        ) != children.get("after_fingerprint"):
            raise _machine_document_error("$.children", "复核后图片子记录发生了意外变化")
        _required_count(
            children.get("picture_count"), path="$.children.picture_count"
        )
        if children.get("picture_count") != 0:
            raise _machine_document_error(
                "$.children.picture_count",
                "特纤复核必须确认不存在图片子记录",
            )
        for key in ("before_fingerprint", "after_fingerprint"):
            _required_text(
                children.get(key),
                path=f"$.children.{key}",
                pattern=_SHA256_RE,
            )
        readback = _strict_object(
            document.get("readback"),
            path="$.readback",
            required={"main_count", "mismatches", "verified_at"},
        )
        if _required_count(readback.get("main_count"), path="$.readback.main_count") != 1 or readback.get(
            "mismatches"
        ) != []:
            raise _machine_document_error("$.readback", "复核主记录读回核对未通过")
    return document


def _scoped_hash(scope: str, *parts: str) -> str:
    payload = json.dumps(
        [scope, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _account_scope_key(account_name: str) -> str:
    normalized = _normalized_identity(account_name)
    if not normalized:
        raise ExecutionApiError(
            422,
            "legacy_credential_account_missing",
            "旧检务系统凭据必须配置账号名称",
        )
    return _scoped_hash(
        "external-account-scope:v1",
        LEGACY_CONNECTOR_KEY,
        normalized,
    )


def _remote_business_key(sample_number: str) -> str:
    normalized = _normalized_identity(sample_number)
    if not normalized:
        raise ExecutionApiError(
            422,
            "external_target_sample_number_missing",
            "旧系统上传预检缺少目标样品编号",
        )
    return _scoped_hash(
        "external-remote-business:v1",
        LEGACY_CONNECTOR_KEY,
        LEGACY_REMOTE_MODULE,
        normalized,
    )


def _operation_type(operation: ExecutionExternalOperation) -> str:
    return str(
        (operation.request_summary or {}).get("operation_type") or ""
    ).strip()


def _operation_stage_profile(
    operation: ExecutionExternalOperation,
) -> tuple[tuple[str, ...], str, str]:
    return EXTERNAL_OPERATION_STAGE_PROFILES.get(
        _operation_type(operation),
        EXTERNAL_OPERATION_STAGE_PROFILES[
            LEGACY_REGENERATED_COUNT_OPERATION
        ],
    )


def _operation_execution_capability(
    operation: ExecutionExternalOperation,
) -> dict[str, Any]:
    declared = (operation.request_summary or {}).get(
        "execution_capability"
    )
    capability = dict(declared) if isinstance(declared, dict) else {
        "available": True
    }
    if (
        _operation_type(operation) in {
            LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION,
            LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
        }
        and not settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
    ):
        generic_entry = (
            _operation_type(operation)
            == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION
        )
        return {
            "available": False,
            "code": (
                "legacy_generic_check_record_entry_disabled"
                if generic_entry
                else "legacy_microscopy_final_entry_disabled"
            ),
            "message": (
                "通用检验记录登记写入当前未在部署环境启用"
                if generic_entry
                else "检验记录登记与校对写入当前未在部署环境启用"
            ),
        }
    if _operation_type(operation) in {
        LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    }:
        if not settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED:
            return dict(
                SPECIAL_WOOL_EXECUTION_CAPABILITY[
                    _operation_type(operation)
                ]
            )
        return {"available": True}
    if isinstance(declared, dict):
        return capability
    return {"available": True}


def _ensure_operation_execution_available(
    operation: ExecutionExternalOperation,
) -> None:
    capability = _operation_execution_capability(operation)
    if capability.get("available") is not False:
        return
    raise conflict(
        str(capability.get("code") or "external_capability_unavailable"),
        str(
            capability.get("message")
            or "当前旧系统外部操作仅支持预检，尚未开放执行"
        ),
        operation_id=operation.id,
        operation_type=_operation_type(operation),
        execution_available=False,
    )


def allocate_legacy_sample_number(
    base_number: str,
    occupied_numbers: set[str] | list[str] | tuple[str, ...],
) -> str:
    """Allocate base, base-1, base-2... against a trusted occupancy set.

    This helper is deliberately deterministic.  Backend preflight uses only
    durable execution-operation fences as its occupancy set; the returned
    value remains provisional until a Windows read-only FibreCheck probe has
    verified the legacy database immediately before final approval.
    """

    normalized_base = unicodedata.normalize("NFKC", base_number).strip().upper()
    if not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(normalized_base):
        raise ExecutionApiError(
            422,
            "external_target_sample_number_invalid",
            "旧系统目标样品编号格式无效",
        )
    occupied = {
        unicodedata.normalize("NFKC", str(value)).strip().casefold()
        for value in occupied_numbers
        if str(value).strip()
    }
    candidate = normalized_base
    suffix = 0
    while candidate.casefold() in occupied:
        suffix += 1
        candidate = f"{normalized_base}-{suffix}"
        if suffix > 99999999:
            raise ExecutionApiError(
                409,
                "external_target_sample_number_exhausted",
                "旧系统目标样品编号后缀已耗尽",
            )
    return candidate


def _locally_occupied_target_numbers(db: Session) -> set[str]:
    """Return numbers allocated by SpecialWool upload operations.

    Other legacy modules use the same source inspection number for different
    business records.  Treating a completed CheckRecord registration or review
    as a SpecialWool allocation would incorrectly force the next image upload
    to skip a suffix.
    """

    occupied: set[str] = set()
    rows = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.connector_key == LEGACY_CONNECTOR_KEY,
            ExecutionExternalOperation.status.in_(
                (*ACTIVE_REMOTE_OPERATION_STATUSES, "completed")
            ),
        )
        .all()
    )
    for row in rows:
        if _operation_type(row) not in {
            LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        }:
            continue
        target = str(
            (row.request_summary or {}).get("target_sample_number") or ""
        ).strip()
        if target:
            occupied.add(target)
        # Writer 可能按旧系统实况顺号改写；实际写入号同样视为本地占用。
        receipt = row.receipt if isinstance(row.receipt, dict) else {}
        actual = str(receipt.get("target_sample_number") or "").strip()
        if actual:
            occupied.add(actual)
    return occupied


def _legacy_special_wool_occupied_target_numbers(
    db: Session,
    *,
    inspection_number: str,
) -> set[str]:
    """Combine read-only legacy occupancy with local durable reservations.

    The task snapshot Bridge queries the existing SpecialWool number family in
    the legacy database.  The local operation table closes the interval between
    that snapshot and a completed remote write, so concurrent runs cannot pick
    the same suffix.
    """

    cached = cached_task_snapshot(
        db,
        inspection_number=inspection_number,
    )
    snapshot = cached.get("snapshot")
    if not isinstance(snapshot, dict):
        raise conflict(
            "special_wool_occupancy_snapshot_required",
            "尚未取得旧系统特纤编号占用信息，请等待任务信息刷新后重试",
            inspection_number=inspection_number,
            cache_state=cached.get("cache_state"),
            refresh_status=cached.get("refresh_status"),
        )
    remote = snapshot.get("special_wool_occupied_numbers")
    if not isinstance(remote, list) or not all(
        isinstance(value, str) and value.strip() for value in remote
    ):
        raise conflict(
            "special_wool_occupancy_snapshot_invalid",
            "旧系统任务快照缺少有效的特纤编号占用信息，请重新刷新后重试",
            inspection_number=inspection_number,
        )
    return {
        str(value).strip().upper()
        for value in remote
    } | _locally_occupied_target_numbers(db)


def resolve_legacy_target_sample_number(run: "ExecutionRun") -> str:
    """目标样品编号：运行输入 `target_sample_number`，缺省回退源检验编号。

    预检创建与批准路径都必须经这里解析，保证业务围栏绑定的是同一个编号。
    """

    return (
        str((run.input_data or {}).get("target_sample_number") or "").strip()
        or run.inspection_number.strip()
    )


def lock_legacy_remote_business_scope(
    db: Session,
    *,
    sample_number: str,
) -> str:
    """Serialize one target sample before either operation or file row locks.

    PostgreSQL's transaction-scoped advisory lock also covers the moment before
    an operation row exists.  SQLite unit tests remain single-process and rely
    on the partial unique index as their final conflict fence.
    """

    remote_business_key = _remote_business_key(sample_number)
    if db.get_bind().dialect.name == "postgresql":
        lock_key = int.from_bytes(
            bytes.fromhex(remote_business_key)[:8],
            byteorder="big",
            signed=True,
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    return remote_business_key


def lock_external_bridge_claim_capacity(db: Session) -> None:
    """Serialize the global Bridge capacity check and claim on PostgreSQL."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": BRIDGE_GLOBAL_CLAIM_LOCK_KEY},
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return _aware_utc(value).isoformat() if value is not None else None


def _source_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _read_count_inspector(snapshot: Path) -> str:
    try:
        workbook_format = detect_workbook_format(snapshot)
        if workbook_format is WorkbookFormat.OLE:
            try:
                import xlrd
            except ImportError as exc:
                raise ExecutionApiError(
                    503,
                    "legacy_workbook_reader_unavailable",
                    "服务端缺少旧版 Excel 读取组件",
                ) from exc
            workbook = xlrd.open_workbook(
                filename=str(snapshot),
                on_demand=True,
            )
            try:
                if "根数法报告1" not in workbook.sheet_names():
                    raise ExecutionApiError(
                        422,
                        "external_count_worksheet_missing",
                        "所选文件缺少根数法报告1工作表",
                    )
                value = workbook.sheet_by_name("根数法报告1").cell_value(
                    7,
                    8,
                )
            finally:
                workbook.release_resources()
        elif workbook_format is WorkbookFormat.OOXML:
            with snapshot.open("rb") as stream:
                workbook = load_workbook(
                    stream,
                    read_only=True,
                    data_only=True,
                    keep_links=False,
                )
                try:
                    if "根数法报告1" not in workbook.sheetnames:
                        raise ExecutionApiError(
                            422,
                            "external_count_worksheet_missing",
                            "所选文件缺少根数法报告1工作表",
                        )
                    value = workbook["根数法报告1"]["I8"].value
                finally:
                    workbook.close()
        else:
            raise ExecutionApiError(
                415,
                "external_workbook_format_unsupported",
                "无法识别所选工作簿的实际文件格式",
            )
    except ExecutionApiError:
        raise
    except Exception as exc:
        raise ExecutionApiError(
            422,
            "external_workbook_read_failed",
            "读取旧系统上传预检字段失败",
            details={"error_type": type(exc).__name__},
        ) from exc
    return value.strip() if isinstance(value, str) else ""


def _stable_snapshot_summary(
    path: Path,
    *,
    expected_fingerprint: str,
    candidate_id: str,
) -> tuple[int, str, str]:
    if _source_fingerprint(path) != expected_fingerprint:
        raise conflict(
            "external_source_file_changed",
            "所选原始记录已发生变化，请重新读取并选择",
            candidate_id=candidate_id,
        )
    with tempfile.TemporaryDirectory(
        prefix="execution-external-preflight-"
    ) as directory:
        snapshot = Path(directory) / f"source{path.suffix.casefold()}"
        try:
            shutil.copyfile(path, snapshot)
        except OSError as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "无法创建原始记录的稳定预检副本",
                candidate_id=candidate_id,
            ) from exc
        if _source_fingerprint(path) != expected_fingerprint:
            raise conflict(
                "external_source_file_changed",
                "复制预检副本期间原始记录发生变化，请重试",
                candidate_id=candidate_id,
            )
        expected_size = int(expected_fingerprint.split(":", 1)[0])
        if snapshot.stat().st_size != expected_size:
            raise conflict(
                "external_source_file_changed",
                "预检副本大小与索引不一致，请重新查询",
                candidate_id=candidate_id,
            )
        digest = hashlib.sha256()
        with snapshot.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        inspector_name = _read_count_inspector(snapshot)
        if _source_fingerprint(path) != expected_fingerprint:
            raise conflict(
                "external_source_file_changed",
                "读取预检副本期间原始记录发生变化，请重试",
                candidate_id=candidate_id,
            )
        return expected_size, digest.hexdigest(), inspector_name


def _generated_microscopy_artifact_rows(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Bind the upload preflight to a server-created immutable artifact row."""

    declared = input_data.get("original_record")
    if not isinstance(declared, dict):
        raise ExecutionApiError(
            422,
            "special_wool_original_record_required",
            "旧系统上传前必须先生成纤维微观形貌原始记录",
        )
    artifact_id = str(declared.get("artifact_id") or "").strip()
    if not artifact_id:
        raise ExecutionApiError(
            422,
            "special_wool_original_record_invalid",
            "生成制品缺少服务器签发的制品标识",
        )
    row = (
        db.query(ExecutionArtifact, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionArtifact.storage_root_id,
        )
        .filter(ExecutionArtifact.id == artifact_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise conflict(
            "special_wool_original_record_stale",
            "生成的微观形貌原始记录已不存在，请重新生成",
            artifact_id=artifact_id,
        )
    artifact, root = row
    expected_media_type = "application/vnd.ms-excel"
    expected_filename = (
        f"{run.inspection_number.strip().upper()}-"
        f"{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}"
    )
    artifact_metadata = artifact.metadata_json or {}
    if (
        artifact.run_id != run.id
        or artifact.role != "working"
        or artifact.media_type != expected_media_type
        or root.root_id != "execution_staging"
        or root.access_mode != "write"
        or str(declared.get("root_id") or "") != root.root_id
        or str(declared.get("relative_path") or "")
        != artifact.relative_path
        or str(declared.get("filename") or "") != artifact.filename
        or str(declared.get("content_sha256") or "")
        != artifact.content_sha256
        or artifact.filename != expected_filename
        or artifact_metadata.get("template_original_filename")
        != MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME
    ):
        raise conflict(
            "special_wool_original_record_stale",
            "生成制品与服务器记录不一致，请重新生成后再上传",
            artifact_id=artifact_id,
        )

    gateway = build_file_gateway(db)
    ref = ArtifactRef(root.root_id, artifact.relative_path)
    try:
        path = gateway.resolve(ref, expected_type="file")
        fingerprint = gateway.fingerprint(ref)
    except (StorageError, OSError, ValueError) as exc:
        raise conflict(
            "special_wool_original_record_unavailable",
            "生成的微观形貌原始记录当前不可读取",
            artifact_id=artifact_id,
        ) from exc
    if (
        fingerprint.size != artifact.size_bytes
        or fingerprint.sha256 != artifact.content_sha256
        or detect_workbook_format(path) is not WorkbookFormat.OLE
    ):
        raise conflict(
            "special_wool_original_record_changed",
            "生成的微观形貌原始记录内容已变化，请重新生成",
            artifact_id=artifact_id,
        )

    operator = db.get(ExecutionUser, run.created_by_id)
    inspector = str(operator.display_name if operator is not None else "").strip()
    if not inspector:
        raise ExecutionApiError(
            422,
            "special_wool_operator_display_name_missing",
            "当前执行系统账号未配置姓名，不能生成旧系统上传预检单",
        )
    return [
        {
            "id": artifact.id,
            "artifact_id": artifact.id,
            "root_id": root.root_id,
            "relative_path": artifact.relative_path,
            "filename": artifact.filename,
            "content_sha256": artifact.content_sha256,
            "size_bytes": artifact.size_bytes,
            "is_primary": True,
            "media_type": artifact.media_type,
            "role": artifact.role,
        }
    ], inspector


def _special_wool_target_filename(target_sample_number: str) -> str:
    target = str(target_sample_number or "").strip().upper()
    if not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(target):
        raise ExecutionApiError(
            422,
            "external_target_sample_number_invalid",
            "旧系统目标样品编号格式不正确",
        )
    filename = f"{target}-{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}"
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or Path(filename).name != filename
    ):
        raise ExecutionApiError(
            422,
            "special_wool_target_filename_invalid",
            "旧系统上传目标文件名不安全",
        )
    return filename


def _generated_microscopy_check_record_artifact(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:
    """Bind final-entry preflight to one immutable generated Sheet1 workbook."""

    declared = input_data.get("registration_workbook")
    declared_binding = input_data.get("template_binding")
    if not isinstance(declared, dict) or not isinstance(
        declared_binding, dict
    ):
        raise ExecutionApiError(
            422,
            "microscopy_check_record_artifact_required",
            "检验记录登记前必须先生成已绑定模板的工作簿",
        )
    artifact_id = str(declared.get("artifact_id") or "").strip()
    row = (
        db.query(ExecutionArtifact, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionArtifact.storage_root_id,
        )
        .filter(ExecutionArtifact.id == artifact_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise conflict(
            "microscopy_check_record_artifact_stale",
            "检验记录登记工作簿已不存在，请重新生成",
            artifact_id=artifact_id,
        )
    artifact, root = row
    metadata = artifact.metadata_json or {}
    stored_binding = metadata.get("template_binding")
    verification = metadata.get("verification")
    cells = (
        verification.get("cells")
        if isinstance(verification, dict)
        else None
    )
    source_number = run.inspection_number.strip().upper()
    if (
        artifact.run_id != run.id
        or root.root_id != "execution_staging"
        or root.access_mode != "write"
        or artifact.role != "working"
        or artifact.immutable is not True
        or artifact.media_type != "application/vnd.ms-excel"
        or not artifact.filename.casefold().endswith(".xls")
        or metadata.get("generator_version")
        not in MICROSCOPY_CHECK_RECORD_COMPATIBLE_GENERATOR_VERSIONS
        or not isinstance(stored_binding, dict)
        or stored_binding != declared_binding
        or not isinstance(cells, dict)
        or _normalized_business_text(cells.get("AS4")).upper()
        != source_number
        or _normalized_business_text(cells.get("I8"))
        != ELECTRON_TEST_METHOD
        or str(declared.get("root_id") or "") != root.root_id
        or str(declared.get("relative_path") or "")
        != artifact.relative_path
        or str(declared.get("filename") or "") != artifact.filename
        or str(declared.get("content_sha256") or "")
        != artifact.content_sha256
        or declared.get("size_bytes") != artifact.size_bytes
    ):
        raise conflict(
            "microscopy_check_record_artifact_stale",
            "检验记录登记工作簿、模板绑定或生成核对结果已变化",
            artifact_id=artifact_id,
        )
    binding_required = {
        "binding_version",
        "image_count",
        "legacy_template_name",
        "local_asset_name",
        "local_asset_sha256",
        "mapping_config_sha256",
    }
    if set(stored_binding) != binding_required or (
        not isinstance(stored_binding.get("image_count"), int)
        or isinstance(stored_binding.get("image_count"), bool)
        or not _SHA256_RE.fullmatch(
            str(stored_binding.get("local_asset_sha256") or "")
        )
        or not _SHA256_RE.fullmatch(
            str(stored_binding.get("mapping_config_sha256") or "")
        )
    ):
        raise conflict(
            "microscopy_check_record_template_binding_invalid",
            "检验记录登记工作簿的模板绑定不完整",
            artifact_id=artifact_id,
        )

    gateway = build_file_gateway(db)
    ref = ArtifactRef(root.root_id, artifact.relative_path)
    try:
        path = gateway.resolve(ref, expected_type="file")
        fingerprint = gateway.fingerprint(ref)
    except (StorageError, OSError, ValueError) as exc:
        raise conflict(
            "microscopy_check_record_artifact_unavailable",
            "检验记录登记工作簿当前无法读取",
            artifact_id=artifact_id,
        ) from exc
    if (
        fingerprint.size != artifact.size_bytes
        or fingerprint.sha256 != artifact.content_sha256
        or detect_workbook_format(path) is not WorkbookFormat.OLE
    ):
        raise conflict(
            "microscopy_check_record_artifact_changed",
            "检验记录登记工作簿内容已变化，请重新生成",
            artifact_id=artifact_id,
        )
    file_row = {
        "artifact_id": artifact.id,
        "root_id": root.root_id,
        "relative_path": artifact.relative_path,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "content_sha256": artifact.content_sha256,
    }
    return (
        file_row,
        dict(stored_binding),
        _normalized_business_text(cells.get("Z7")),
        dict(cells),
    )


def _completed_special_wool_review_source(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> ExecutionExternalOperation:
    review_result = input_data.get("review_result")
    operation_id = (
        str(review_result.get("operation_id") or "").strip()
        if isinstance(review_result, dict)
        else ""
    )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == operation_id,
            ExecutionExternalOperation.run_id == run.id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source) != LEGACY_SPECIAL_WOOL_REVIEW_OPERATION
        or not isinstance(source.receipt, dict)
        or not source.receipt
    ):
        raise conflict(
            "special_wool_review_not_completed",
            "检验记录登记只能衔接本流程已完成且已核对的特纤复核",
            operation_id=operation_id,
        )
    validate_external_receipt(source, source.receipt)
    return source


def _reverify_generated_artifact_source(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    summary = operation.request_summary or {}
    files = summary.get("files")
    if (
        not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
    ):
        raise conflict(
            "external_operation_preflight_invalid",
            "预检单缺少生成制品核对信息，请重新运行流程",
            operation_id=operation.id,
        )
    expected = files[0]
    row = (
        db.query(ExecutionArtifact, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionArtifact.storage_root_id,
        )
        .filter(ExecutionArtifact.id == str(expected.get("artifact_id") or ""))
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise conflict(
            "special_wool_original_record_changed",
            "批准前生成制品已不存在，请重新运行流程",
        )
    artifact, root = row
    source_number = str(summary.get("source_inspection_number") or "").strip().upper()
    expected_filename = (
        f"{source_number}-{MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME}"
    )
    artifact_metadata = artifact.metadata_json or {}
    if (
        artifact.run_id != operation.run_id
        or root.root_id != expected.get("root_id")
        or artifact.relative_path != expected.get("relative_path")
        or artifact.filename != expected.get("filename")
        or artifact.content_sha256 != expected.get("content_sha256")
        or artifact.size_bytes != expected.get("size_bytes")
        or artifact.role != "working"
        or artifact.media_type != "application/vnd.ms-excel"
        or artifact.filename != expected_filename
        or artifact_metadata.get("template_original_filename")
        != MICROSCOPY_ORIGINAL_TEMPLATE_FILENAME
    ):
        raise conflict(
            "special_wool_original_record_changed",
            "批准前生成制品记录已变化，请重新运行流程",
        )
    gateway = build_file_gateway(db)
    ref = ArtifactRef(root.root_id, artifact.relative_path)
    try:
        fingerprint = gateway.fingerprint(ref)
    except (StorageError, OSError, ValueError) as exc:
        raise conflict(
            "special_wool_original_record_unavailable",
            "批准前无法重新读取生成制品",
        ) from exc
    if (
        fingerprint.size != artifact.size_bytes
        or fingerprint.sha256 != artifact.content_sha256
    ):
        raise conflict(
            "special_wool_original_record_changed",
            "批准前生成制品内容已变化，请重新运行流程",
        )


def _reverify_microscopy_final_entry_sources(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    summary = operation.request_summary or {}
    run = db.get(ExecutionRun, operation.run_id)
    files = summary.get("files")
    if (
        run is None
        or not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
        or not isinstance(summary.get("template_binding"), dict)
    ):
        raise conflict(
            "external_operation_preflight_invalid",
            "检验记录登记预检单缺少制品或模板绑定",
            operation_id=operation.id,
        )
    _file, _binding, _identity, _cells = (
        _generated_microscopy_check_record_artifact(
            db,
            run=run,
            input_data={
                "registration_workbook": dict(files[0]),
                "template_binding": dict(summary["template_binding"]),
            },
        )
    )

    source_ref = summary.get("source_review_operation")
    if not isinstance(source_ref, dict):
        raise conflict(
            "external_operation_preflight_invalid",
            "检验记录登记预检单缺少特纤复核来源",
            operation_id=operation.id,
        )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id
            == str(source_ref.get("operation_id") or ""),
            ExecutionExternalOperation.run_id == operation.run_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source) != LEGACY_SPECIAL_WOOL_REVIEW_OPERATION
        or source.payload_checksum != source_ref.get("payload_checksum")
        or not isinstance(source.receipt, dict)
        or _canonical_checksum(source.receipt)
        != source_ref.get("receipt_checksum")
        or str(
            (source.request_summary or {}).get("target_sample_number") or ""
        )
        != source_ref.get("special_wool_target_sample_number")
    ):
        raise conflict(
            "special_wool_review_result_changed",
            "检验记录登记所引用的特纤复核结果已变化",
            operation_id=operation.id,
        )
    validate_external_receipt(source, source.receipt)
    package = summary.get("final_entry_package")
    source_project = (source.request_summary or {}).get("task_project")
    if (
        not isinstance(package, dict)
        or package.get("task_project") != summary.get("task_project")
        or source_project != summary.get("task_project")
    ):
        raise conflict(
            "microscopy_final_entry_binding_changed",
            "检验记录登记的任务项目或检验份数绑定已变化",
            operation_id=operation.id,
        )


def _reverify_paper_file_source(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    summary = operation.request_summary or {}
    files = summary.get("files")
    if (
        not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
    ):
        raise conflict(
            "external_operation_preflight_invalid",
            "纸纤维预检单缺少唯一源文件",
            operation_id=operation.id,
        )
    expected = files[0]
    row = (
        db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionFileIndexEntry.storage_root_id,
        )
        .filter(
            ExecutionFileIndexEntry.id == str(expected.get("id") or "")
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise conflict(
            "paper_fiber_source_file_changed",
            "批准前纸纤维原始记录已不在索引中",
        )
    entry, root = row
    if (
        entry.missing_since is not None
        or root.root_id != PAPER_FIBER_ROOT_ID
        or root.root_id != expected.get("root_id")
        or entry.relative_path != expected.get("relative_path")
        or entry.filename != expected.get("filename")
        or entry.fingerprint != expected.get("fingerprint")
    ):
        raise conflict(
            "paper_fiber_source_file_changed",
            "批准前纸纤维原始记录索引已变化",
        )
    gateway = build_file_gateway(db)
    try:
        fingerprint = gateway.fingerprint(
            ArtifactRef(root.root_id, entry.relative_path)
        )
    except (StorageError, OSError, ValueError) as exc:
        raise conflict(
            "paper_fiber_source_file_unavailable",
            "批准前无法重新读取纸纤维原始记录",
        ) from exc
    if (
        fingerprint.size != expected.get("size_bytes")
        or fingerprint.sha256 != expected.get("content_sha256")
    ):
        raise conflict(
            "paper_fiber_source_file_changed",
            "批准前纸纤维原始记录内容已变化",
        )
    run = db.get(ExecutionRun, operation.run_id)
    operator = db.get(ExecutionUser, run.created_by_id) if run else None
    if (
        operator is None
        or str(operator.display_name or "").strip()
        != str(summary.get("inspector") or "").strip()
    ):
        raise conflict(
            "paper_fiber_inspector_changed",
            "批准前执行账号姓名已变化，请重新运行流程",
        )


def _reverify_paper_upload_source(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    source_ref = (operation.request_summary or {}).get("source_operation")
    if not isinstance(source_ref, dict):
        raise conflict(
            "external_operation_preflight_invalid",
            "纸纤维复核预检单缺少上传来源",
        )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id
            == str(source_ref.get("operation_id") or ""),
            ExecutionExternalOperation.run_id == operation.run_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source)
        != LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        or source.payload_checksum != source_ref.get("payload_checksum")
        or not isinstance(source.receipt, dict)
        or _canonical_checksum(source.receipt)
        != source_ref.get("receipt_checksum")
        or _special_wool_upload_main_id(source)
        != source_ref.get("main_id")
    ):
        raise conflict(
            "paper_special_wool_upload_result_changed",
            "纸纤维复核所引用的上传结果已变化",
        )
    validate_external_receipt(source, source.receipt)


def _reverify_generic_entry_sources(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    summary = operation.request_summary or {}
    source_ref = summary.get("source_review_operation")
    package = summary.get("final_entry_package")
    if not isinstance(source_ref, dict) or not isinstance(package, dict):
        raise conflict(
            "external_operation_preflight_invalid",
            "通用检验记录登记缺少纸纤维复核或机器载荷绑定",
        )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id
            == str(source_ref.get("operation_id") or ""),
            ExecutionExternalOperation.run_id == operation.run_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source)
        != LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
        or source.payload_checksum != source_ref.get("payload_checksum")
        or not isinstance(source.receipt, dict)
        or _canonical_checksum(source.receipt)
        != source_ref.get("receipt_checksum")
        or str((source.request_summary or {}).get("target_sample_number") or "")
        != source_ref.get("special_wool_target_sample_number")
    ):
        raise conflict(
            "paper_special_wool_review_result_changed",
            "通用检验记录登记所引用的纸纤维复核结果已变化",
        )
    validate_external_receipt(source, source.receipt)
    if (
        package.get("task_project") != summary.get("task_project")
        or package.get("sample_number")
        != summary.get("target_sample_number")
    ):
        raise conflict(
            "paper_fiber_final_entry_binding_changed",
            "通用检验记录登记的任务项目或样品编号绑定已变化",
        )


def _credential_for_node(
    db: Session,
    *,
    run: ExecutionRun,
    node: dict[str, Any],
) -> ExecutionCredential:
    slot_name = str((node.get("config") or {}).get("credential_slot") or "")
    slot = next(
        (
            item
            for item in (run.definition_snapshot or {}).get(
                "credential_slots",
                [],
            )
            if isinstance(item, dict) and item.get("name") == slot_name
        ),
        None,
    )
    if (
        slot is None
        or slot.get("system_key") != LEGACY_CREDENTIAL_SYSTEM
    ):
        raise ExecutionApiError(
            422,
            "legacy_credential_slot_invalid",
            "旧检务系统节点未绑定有效的旧系统凭据槽位",
        )
    credential = (
        db.query(ExecutionCredential)
        .filter(
            ExecutionCredential.user_id == run.created_by_id,
            ExecutionCredential.system_key == LEGACY_CREDENTIAL_SYSTEM,
            ExecutionCredential.is_active.is_(True),
        )
        .one_or_none()
    )
    if credential is None:
        raise ExecutionApiError(
            422,
            "legacy_credential_missing",
            "请先在执行系统账号中配置旧检务系统凭据",
        )
    return credential


def _selected_file_rows(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    selected = input_data.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise ExecutionApiError(
            422,
            "external_selected_files_required",
            "旧系统上传前必须先完成人工文件选择",
        )
    if len(selected) != 1:
        raise ExecutionApiError(
            422,
            "external_exactly_one_file_required",
            "旧系统每次上传必须且只能选择一份原始记录",
            details={"selected_file_count": len(selected)},
        )
    primary_file_id = str(input_data.get("primary_file_id") or "")
    if not primary_file_id:
        raise ExecutionApiError(
            422,
            "external_primary_file_required",
            "旧系统上传前必须指定主单",
        )

    declared_roots = {
        str(item.get("root_id"))
        for item in (run.definition_snapshot or {}).get("root_slots", [])
        if isinstance(item, dict) and item.get("access", "read") == "read"
    }
    gateway = build_file_gateway(db)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    inspector_names: dict[str, str] = {}
    primary_inspector: str | None = None

    for raw in selected:
        if not isinstance(raw, dict):
            raise ExecutionApiError(
                409,
                "external_selected_file_invalid",
                "所选文件不是服务器签发的文件对象",
            )
        candidate_id = str(raw.get("id") or "")
        if not candidate_id or candidate_id in seen:
            raise ExecutionApiError(
                409,
                "external_selected_file_invalid",
                "所选文件标识缺失或重复",
            )
        row = (
            db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
            .join(
                ExecutionStorageRoot,
                ExecutionStorageRoot.id
                == ExecutionFileIndexEntry.storage_root_id,
            )
            .filter(ExecutionFileIndexEntry.id == candidate_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise conflict(
                "external_selected_file_stale",
                "所选文件已不在索引中，请重新查询",
                candidate_id=candidate_id,
            )
        entry, root = row
        if (
            entry.missing_since is not None
            or root.root_id != "regenerated_fiber_records"
            or root.root_id not in declared_roots
            or raw.get("root_id") != root.root_id
            or raw.get("relative_path") != entry.relative_path
            or raw.get("fingerprint") != entry.fingerprint
            or raw.get("read_status") != "succeeded"
        ):
            raise conflict(
                "external_selected_file_stale",
                "所选文件已变化或不属于再生纤根数法流程",
                candidate_id=candidate_id,
            )
        result = raw.get("result")
        if (
            not isinstance(result, dict)
            or result.get("method") != "count"
            or result.get("worksheet") != "根数法报告1"
        ):
            raise ExecutionApiError(
                422,
                "external_count_result_invalid",
                "所选文件不是根数法报告1的读取结果",
                details={"candidate_id": candidate_id},
            )
        try:
            path = gateway.resolve(
                ArtifactRef(root.root_id, entry.relative_path),
                expected_type="file",
            )
            size_bytes, content_sha256, inspector_name = (
                _stable_snapshot_summary(
                    path,
                    expected_fingerprint=entry.fingerprint,
                    candidate_id=candidate_id,
                )
            )
        except (StorageError, OSError) as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "所选文件当前不可读取，请重新查询",
                candidate_id=candidate_id,
            ) from exc
        if not inspector_name:
            raise ExecutionApiError(
                422,
                "external_inspector_missing",
                "所选文件的根数法报告1!I8未读取到检验员",
                details={"candidate_id": candidate_id},
            )
        inspector_names.setdefault(inspector_name.casefold(), inspector_name)
        if entry.id == primary_file_id:
            primary_inspector = inspector_name
        rows.append(
            {
                "id": entry.id,
                "root_id": root.root_id,
                "relative_path": entry.relative_path,
                "filename": entry.filename,
                "fingerprint": entry.fingerprint,
                "content_sha256": content_sha256,
                "size_bytes": size_bytes,
                "is_primary": entry.id == primary_file_id,
            }
        )
        seen.add(candidate_id)

    if primary_file_id not in seen:
        raise ExecutionApiError(
            422,
            "external_primary_file_not_selected",
            "主单必须属于本次已选择的文件",
        )
    if len(inspector_names) != 1:
        raise ExecutionApiError(
            422,
            "external_inspector_conflict",
            "多个原始记录中的检验员不一致，不能自动生成上传预检单",
            details={"inspector_count": len(inspector_names)},
        )
    rows.sort(
        key=lambda item: (
            not item["is_primary"],
            item["relative_path"].casefold(),
            item["id"],
        )
    )
    if primary_inspector is None:
        raise ExecutionApiError(
            422,
            "external_primary_inspector_missing",
            "未能从主单稳定快照读取检验员",
        )
    return rows, primary_inspector


def _paper_result_value(raw: dict[str, Any]) -> tuple[str, str]:
    result = raw.get("result")
    if not isinstance(result, dict):
        raise ExecutionApiError(
            422,
            "paper_fiber_result_required",
            "所选纸纤维原始记录缺少 Sheet1!W32 读取结果",
        )
    if result.get("worksheet") != "Sheet1" or result.get("cell") != "W32":
        raise ExecutionApiError(
            422,
            "paper_fiber_result_contract_invalid",
            "所选原始记录不是已核验的 Sheet1!W32 结果",
        )
    value = str(
        result.get("w32_value")
        if result.get("w32_value") is not None
        else result.get("qualitative_result") or ""
    ).strip()
    if not value or len(value) > 2000:
        raise ExecutionApiError(
            422,
            "paper_fiber_result_value_invalid",
            "Sheet1!W32 结果为空或过长，不能登记",
        )
    normalized_value = unicodedata.normalize("NFKC", value)
    standalone_100 = _PAPER_STANDALONE_100_RE.search(
        normalized_value
    ) is not None
    expected_unit = "%" if standalone_100 else ""
    if bool(result.get("contains_standalone_100")) != standalone_100 or str(
        result.get("unit") or ""
    ) != expected_unit:
        raise ExecutionApiError(
            422,
            "paper_fiber_result_unit_invalid",
            "Sheet1!W32 的 100 与百分号单位判断不一致，请重新读取",
        )
    return value, expected_unit


def _selected_paper_file_row(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    selected = input_data.get("selected_files")
    if not isinstance(selected, list) or len(selected) != 1 or not isinstance(
        selected[0], dict
    ):
        raise ExecutionApiError(
            422,
            "paper_fiber_exactly_one_file_required",
            "纸纤维原始记录必须且只能选择一份工作簿",
        )
    raw = selected[0]
    candidate_id = str(raw.get("id") or "").strip()
    primary_file_id = str(input_data.get("primary_file_id") or "").strip()
    if not candidate_id or primary_file_id != candidate_id:
        raise ExecutionApiError(
            422,
            "paper_fiber_primary_file_required",
            "所选纸纤维工作簿必须同时标记为主单",
        )
    row = (
        db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
        .join(
            ExecutionStorageRoot,
            ExecutionStorageRoot.id == ExecutionFileIndexEntry.storage_root_id,
        )
        .filter(ExecutionFileIndexEntry.id == candidate_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise conflict(
            "paper_fiber_selected_file_stale",
            "所选纸纤维工作簿已不在索引中，请重新查询",
            candidate_id=candidate_id,
        )
    entry, root = row
    declared_roots = {
        str(item.get("root_id"))
        for item in (run.definition_snapshot or {}).get("root_slots", [])
        if isinstance(item, dict) and item.get("access", "read") == "read"
    }
    if (
        entry.missing_since is not None
        or root.root_id != PAPER_FIBER_ROOT_ID
        or root.root_id not in declared_roots
        or raw.get("root_id") != root.root_id
        or raw.get("relative_path") != entry.relative_path
        or raw.get("fingerprint") != entry.fingerprint
        or raw.get("read_status") != "succeeded"
        or not entry.filename.casefold().endswith(".xls")
    ):
        raise conflict(
            "paper_fiber_selected_file_stale",
            "所选文件已变化或不属于纸纤维定性流程",
            candidate_id=candidate_id,
        )
    result_value, unit = _paper_result_value(raw)
    gateway = build_file_gateway(db)
    ref = ArtifactRef(root.root_id, entry.relative_path)
    try:
        fingerprint = gateway.fingerprint(ref)
        path = gateway.resolve(ref, expected_type="file")
    except (StorageError, OSError, ValueError) as exc:
        raise conflict(
            "paper_fiber_selected_file_unavailable",
            "所选纸纤维工作簿当前不可读取",
            candidate_id=candidate_id,
        ) from exc
    if (
        fingerprint.size <= 0
        or not _SHA256_RE.fullmatch(str(fingerprint.sha256 or ""))
        or path.name != entry.filename
    ):
        raise conflict(
            "paper_fiber_selected_file_changed",
            "所选纸纤维工作簿内容已变化，请重新查询",
            candidate_id=candidate_id,
        )
    operator = db.get(ExecutionUser, run.created_by_id)
    inspector = str(
        operator.display_name if operator is not None else ""
    ).strip()
    if not inspector:
        raise ExecutionApiError(
            422,
            "paper_fiber_operator_display_name_missing",
            "当前执行系统账号未配置姓名，不能生成旧系统预检单",
        )
    return (
        {
            "id": entry.id,
            "artifact_id": entry.id,
            "root_id": root.root_id,
            "relative_path": entry.relative_path,
            "filename": entry.filename,
            "fingerprint": entry.fingerprint,
            "content_sha256": fingerprint.sha256,
            "size_bytes": fingerprint.size,
            "is_primary": True,
        },
        inspector,
        result_value,
        unit,
    )


def _paper_special_wool_target_filename(
    target_sample_number: str,
    source_filename: str,
) -> str:
    target = str(target_sample_number or "").strip().upper()
    source_name = str(source_filename or "").strip()
    if (
        not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(target)
        or not source_name
        or Path(source_name).name != source_name
        or source_name in {".", ".."}
        or "/" in source_name
        or "\\" in source_name
        or "\x00" in source_name
        or not source_name.casefold().endswith(".xls")
    ):
        raise ExecutionApiError(
            422,
            "paper_fiber_target_filename_invalid",
            "纸纤维旧系统上传文件名无效",
        )
    return f"{target}-{source_name}"


def _bound_credential_for_approval(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
) -> ExecutionCredential:
    credential = (
        db.query(ExecutionCredential)
        .filter(ExecutionCredential.id == operation.credential_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        credential is None
        or not credential.is_active
        or credential.user_id != run.created_by_id
        or credential.system_key != LEGACY_CREDENTIAL_SYSTEM
        or credential.revision != operation.credential_revision
    ):
        raise conflict(
            "external_operation_credential_changed",
            "旧检务系统凭据已变化，请重新生成并核对预检单",
            operation_id=operation.id,
        )
    if _account_scope_key(credential.account_name or "") != (
        operation.account_scope_key
    ):
        raise conflict(
            "external_operation_credential_changed",
            "旧检务系统账号已变化，请重新生成并核对预检单",
            operation_id=operation.id,
        )
    return credential


def _ensure_preflight_not_expired(
    operation: ExecutionExternalOperation,
    *,
    now: datetime,
) -> None:
    expires_at = operation.preflight_expires_at
    if expires_at is None or _aware_utc(expires_at) <= _aware_utc(now):
        raise conflict(
            "external_operation_preflight_expired",
            "预检单已过期，系统将自动结束本次运行，请重新执行",
            operation_id=operation.id,
        )


def _ensure_approval_not_expired(
    operation: ExecutionExternalOperation,
    *,
    now: datetime,
) -> None:
    expires_at = operation.approval_expires_at
    # 服务端自动批准不依赖浏览器停留，也不设倒计时。NULL 表示该自动
    # 交付可一直等待 Bridge；人工批准仍写入明确 TTL 并按原规则校验。
    if expires_at is None:
        return
    if _aware_utc(expires_at) <= _aware_utc(now):
        raise conflict(
            "external_operation_approval_expired",
            "本次批准已过期，系统将自动结束本次运行，请重新执行并再次确认",
            operation_id=operation.id,
        )


def _reverify_operation_sources(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    operation_type = _operation_type(operation)
    if operation_type == LEGACY_SPECIAL_WOOL_IMAGE_OPERATION:
        _reverify_generated_artifact_source(db, operation=operation)
        return
    if operation_type == LEGACY_SPECIAL_WOOL_REVIEW_OPERATION:
        _reverify_special_wool_review_source(db, operation=operation)
        return
    if operation_type == LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION:
        _reverify_microscopy_final_entry_sources(db, operation=operation)
        return
    if operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION:
        _reverify_paper_file_source(db, operation=operation)
        return
    if operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION:
        _reverify_paper_upload_source(db, operation=operation)
        return
    if operation_type == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION:
        _reverify_generic_entry_sources(db, operation=operation)
        return
    summary = operation.request_summary or {}
    files = summary.get("files")
    expected_inspector = summary.get("inspector")
    if (
        not isinstance(files, list)
        or len(files) != 1
        or not isinstance(expected_inspector, str)
        or not expected_inspector
    ):
        raise conflict(
            "external_operation_preflight_invalid",
            "预检单缺少完整的源文件核对信息，请重新运行流程",
            operation_id=operation.id,
        )

    gateway = build_file_gateway(db)
    for expected in files:
        if not isinstance(expected, dict):
            raise conflict(
                "external_operation_preflight_invalid",
                "预检单中的源文件信息无效，请重新运行流程",
                operation_id=operation.id,
            )
        candidate_id = str(expected.get("id") or "")
        row = (
            db.query(ExecutionFileIndexEntry, ExecutionStorageRoot)
            .join(
                ExecutionStorageRoot,
                ExecutionStorageRoot.id
                == ExecutionFileIndexEntry.storage_root_id,
            )
            .filter(ExecutionFileIndexEntry.id == candidate_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise conflict(
                "external_source_file_changed",
                "预检单中的原始记录已不在索引中，请重新运行流程",
                candidate_id=candidate_id,
            )
        entry, root = row
        expected_fingerprint = str(expected.get("fingerprint") or "")
        if (
            not candidate_id
            or entry.missing_since is not None
            or root.root_id != expected.get("root_id")
            or entry.relative_path != expected.get("relative_path")
            or entry.fingerprint != expected_fingerprint
        ):
            raise conflict(
                "external_source_file_changed",
                "预检后原始记录索引已变化，请重新运行流程",
                candidate_id=candidate_id,
            )
        try:
            path = gateway.resolve(
                ArtifactRef(root.root_id, entry.relative_path),
                expected_type="file",
            )
            size_bytes, content_sha256, inspector_name = (
                _stable_snapshot_summary(
                    path,
                    expected_fingerprint=expected_fingerprint,
                    candidate_id=candidate_id,
                )
            )
        except ExecutionApiError:
            raise
        except (StorageError, OSError, ValueError) as exc:
            raise conflict(
                "external_selected_file_unavailable",
                "批准前无法重新读取原始记录，请稍后重试",
                candidate_id=candidate_id,
            ) from exc

        changed_fields: list[str] = []
        if size_bytes != expected.get("size_bytes"):
            changed_fields.append("size_bytes")
        if content_sha256 != expected.get("content_sha256"):
            changed_fields.append("content_sha256")
        if inspector_name != expected_inspector:
            changed_fields.append("inspector")
        if changed_fields:
            raise conflict(
                "external_source_file_changed",
                "预检后原始记录内容已变化，请重新运行流程",
                candidate_id=candidate_id,
                changed_fields=changed_fields,
            )


def _rearm_expired_external_operation(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    prepared_at,
    preflight_expires_at,
) -> bool:
    """Reuse a pre-side-effect fence after an explicit node retry.

    The operation row is unique per node, so a retry must reuse the same fence.
    Claims made before approval expiry are not by themselves remote writes:
    rearming remains safe when *every* durable attempt ended before the
    operation-specific write boundary.  Unknown/inconsistent history and any
    completion or reconciliation evidence fail closed.

    Besides ``expired`` operations, a ``failed`` operation settled by the
    attempts-exhausted path is accepted: settlement only marks an operation
    ``failed`` without reconciliation evidence when every attempt provably
    stopped before the write boundary, which is the same pre-side-effect
    situation.  Reconciled operations keep their non-empty verification and
    therefore still fall through to the dedicated reconciled rearm path.
    """

    if operation.status == "expired":
        pass
    elif (
        operation.status == "failed"
        and operation.error_code != "external_reconciliation_no_side_effect"
        and not operation.verification
        and not operation.receipt
        and operation.remote_record_id is None
    ):
        pass
    else:
        return False

    def reject(reason: str, **details: Any) -> None:
        raise conflict(
            "external_operation_not_rearmable",
            (
                "该外部操作存在执行痕迹，不能自动重新准备，"
                "请先人工核对"
            ),
            operation_id=operation.id,
            status=operation.status,
            reason=reason,
            **details,
        )

    # ``_prepare_external_operation_wait`` holds run -> node before this
    # helper locks operation -> attempts.  Requiring a second running node
    # attempt keeps this path exclusive to the explicit node-retry action.
    if node_run.status != "running" or int(node_run.attempt_count or 0) < 2:
        reject(
            "explicit_node_retry_required",
            node_status=node_run.status,
            node_attempt_count=int(node_run.attempt_count or 0),
        )

    operation_type = _operation_type(operation)
    stage_profile = EXTERNAL_OPERATION_STAGE_PROFILES.get(operation_type)
    if stage_profile is None:
        reject("unknown_operation_type", operation_type=operation_type)
    attempt_stages, write_boundary, _verified_stage = stage_profile
    boundary_index = attempt_stages.index(write_boundary)

    if (
        operation.status == "expired"
        and operation.error_code not in EXTERNAL_OPERATION_EXPIRY_ERROR_CODES
    ):
        reject(
            "not_a_system_expiry",
            operation_error_code=operation.error_code,
        )
    if operation.completed_at is None:
        reject("expiry_completion_missing")
    if operation.remote_record_id is not None:
        reject("remote_record_present")
    if operation.receipt not in (None, {}):
        reject("receipt_present")
    if operation.verification not in (None, {}):
        reject("verification_present")
    if (
        operation.fence_token is not None
        or operation.lease_owner is not None
        or operation.lease_expires_at is not None
    ):
        reject("active_operation_claim_present")

    output = (
        node_run.output_data
        if isinstance(node_run.output_data, dict)
        else {}
    )
    if output.get("status") == "completed":
        reject("node_completion_present")
    if output.get("remote_write_performed") is True:
        reject("node_remote_write_present")
    if output.get("remote_record_id") is not None:
        reject("node_remote_record_present")
    if output.get("receipt") not in (None, {}):
        reject("node_receipt_present")
    if output.get("verification") not in (None, {}):
        reject("node_verification_present")

    attempts = (
        db.query(ExecutionExternalAttempt)
        .filter(ExecutionExternalAttempt.operation_id == operation.id)
        .order_by(
            ExecutionExternalAttempt.attempt_no.asc(),
            ExecutionExternalAttempt.id.asc(),
        )
        .populate_existing()
        .with_for_update()
        .all()
    )
    if int(operation.attempt_count or 0) != len(attempts):
        reject(
            "attempt_history_incomplete",
            operation_attempt_count=int(operation.attempt_count or 0),
            durable_attempt_count=len(attempts),
        )
    if operation.started_at is not None and not attempts:
        reject("started_without_attempt_history")

    prior_attempts: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.status in EXTERNAL_ATTEMPT_ACTIVE_STATUSES:
            reject(
                "active_attempt_present",
                attempt_id=attempt.id,
                attempt_status=attempt.status,
            )
        if attempt.status not in EXTERNAL_ATTEMPT_TERMINAL_STATUSES:
            reject(
                "unknown_attempt_status",
                attempt_id=attempt.id,
                attempt_status=attempt.status,
            )
        # A completed attempt or a zero exit code is itself durable evidence
        # of a successful Writer path, even if a corrupted row claims an
        # earlier stage.
        if attempt.status == "completed" or attempt.exit_code == 0:
            reject(
                "attempt_completion_present",
                attempt_id=attempt.id,
                attempt_status=attempt.status,
                exit_code=attempt.exit_code,
            )
        if attempt.finished_at is None or attempt.lease_expires_at is not None:
            reject(
                "attempt_not_fully_settled",
                attempt_id=attempt.id,
                attempt_status=attempt.status,
            )
        stage = attempt.current_stage
        if stage not in attempt_stages:
            reject(
                "unknown_attempt_stage",
                attempt_id=attempt.id,
                current_stage=stage,
            )
        if attempt_stages.index(stage) >= boundary_index:
            reject(
                "write_boundary_reached",
                attempt_id=attempt.id,
                current_stage=stage,
                write_boundary=write_boundary,
            )

        checkpoint_stages: list[str] = []
        for checkpoint in attempt.checkpoints or []:
            checkpoint_stage = (
                checkpoint.get("stage")
                if isinstance(checkpoint, dict)
                else None
            )
            if checkpoint_stage not in attempt_stages:
                reject(
                    "unknown_checkpoint_stage",
                    attempt_id=attempt.id,
                    checkpoint_stage=checkpoint_stage,
                )
            if attempt_stages.index(checkpoint_stage) >= boundary_index:
                reject(
                    "write_boundary_checkpoint_present",
                    attempt_id=attempt.id,
                    checkpoint_stage=checkpoint_stage,
                    write_boundary=write_boundary,
                )
            checkpoint_stages.append(checkpoint_stage)
        prior_attempts.append(
            {
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "current_stage": stage,
                "checkpoint_stages": checkpoint_stages,
            }
        )

    blocker = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id != operation.id,
            ExecutionExternalOperation.remote_business_key
            == operation.remote_business_key,
            ExecutionExternalOperation.status.in_(
                ACTIVE_REMOTE_OPERATION_STATUSES
            ),
        )
        .order_by(ExecutionExternalOperation.created_at.asc())
        .with_for_update()
        .first()
    )
    if blocker is not None:
        raise conflict(
            "external_remote_business_conflict",
            "同一样品已有待处理的旧系统操作，请先处理或取消原流程",
            operation_id=blocker.id,
        )

    previous_error_code = operation.error_code
    previous_started_at = operation.started_at
    previous_completed_at = operation.completed_at
    operation.status = "prepared"
    operation.preflight_expires_at = preflight_expires_at
    operation.approved_by_id = None
    operation.approved_at = None
    operation.approval_expires_at = None
    operation.approval_note = None
    operation.fence_token = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.remote_record_id = None
    operation.receipt = {}
    operation.verification = {}
    operation.error_code = None
    operation.error_message = None
    operation.started_at = None
    operation.completed_at = None
    append_run_event(
        db,
        run_id=run.id,
        event_type="external_operation.rearmed",
        payload={
            "operation_id": operation.id,
            "node_id": node_run.node_id,
            "previous_error_code": previous_error_code,
            "prepared_at": prepared_at.isoformat(),
            "preflight_expires_at": preflight_expires_at.isoformat(),
            "operation_type": operation_type,
            "write_boundary": write_boundary,
            "prior_attempt_count": len(prior_attempts),
            "prior_attempts": prior_attempts,
            "remote_write_performed": False,
        },
    )
    append_audit_log(
        db,
        action="external_operation.rearm",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        details={
            "run_id": run.id,
            "previous_error_code": previous_error_code,
            "previous_started_at": (
                previous_started_at.isoformat()
                if previous_started_at is not None
                else None
            ),
            "previous_completed_at": (
                previous_completed_at.isoformat()
                if previous_completed_at is not None
                else None
            ),
            "operation_type": operation_type,
            "write_boundary": write_boundary,
            "prior_attempt_count": len(prior_attempts),
            "prior_attempts": prior_attempts,
            "remote_write_performed": False,
        },
    )
    return True


def _rearm_reconciled_no_side_effect_operation(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    prepared_at,
    preflight_expires_at,
) -> bool:
    """Reopen the same fence after an admin proved that no write occurred.

    Reconciliation intentionally leaves the operation failed so the run does
    not continue by itself.  A later explicit node retry may reuse that exact
    idempotency fence, but only when the durable reconciliation conclusion is
    ``confirm_no_side_effect`` and the referenced failed attempt is still the
    latest attempt.  The full attestation remains in the append-only audit log;
    the active operation verification is cleared for the next attempt.
    """

    if operation.status != "failed":
        return False

    def reject(reason: str, **details: Any) -> None:
        raise conflict(
            "external_operation_not_rearmable",
            "该外部操作尚未确认无副作用，不能重新执行",
            operation_id=operation.id,
            status=operation.status,
            reason=reason,
            **details,
        )

    if node_run.status != "running" or int(node_run.attempt_count or 0) < 2:
        reject(
            "explicit_node_retry_required",
            node_status=node_run.status,
            node_attempt_count=int(node_run.attempt_count or 0),
        )
    if operation.error_code != "external_reconciliation_no_side_effect":
        reject("reconciliation_error_code_missing")
    if operation.remote_record_id is not None or operation.receipt not in (
        None,
        {},
    ):
        reject("remote_result_present")
    if (
        operation.fence_token is not None
        or operation.lease_owner is not None
        or operation.lease_expires_at is not None
    ):
        reject("active_operation_claim_present")

    reconciliation = dict(operation.verification or {}).get("reconciliation")
    if (
        not isinstance(reconciliation, dict)
        or reconciliation.get("action") != "confirm_no_side_effect"
        or reconciliation.get("remote_write_performed") is not False
        or not str(reconciliation.get("evidence_checksum") or "").strip()
    ):
        reject("no_side_effect_attestation_missing")

    attempts = (
        db.query(ExecutionExternalAttempt)
        .filter(ExecutionExternalAttempt.operation_id == operation.id)
        .order_by(
            ExecutionExternalAttempt.attempt_no.asc(),
            ExecutionExternalAttempt.id.asc(),
        )
        .populate_existing()
        .with_for_update()
        .all()
    )
    if not attempts or int(operation.attempt_count or 0) != len(attempts):
        reject(
            "attempt_history_incomplete",
            operation_attempt_count=int(operation.attempt_count or 0),
            durable_attempt_count=len(attempts),
        )
    latest = attempts[-1]
    if (
        latest.id != reconciliation.get("attempt_id")
        or latest.status != "failed"
        or latest.finished_at is None
        or latest.lease_expires_at is not None
    ):
        reject(
            "reconciled_attempt_changed",
            latest_attempt_id=latest.id,
            reconciled_attempt_id=reconciliation.get("attempt_id"),
            latest_attempt_status=latest.status,
        )

    prior_reconciliation = dict(reconciliation)
    previous_completed_at = operation.completed_at
    operation.status = "prepared"
    operation.preflight_expires_at = preflight_expires_at
    operation.approved_by_id = None
    operation.approved_at = None
    operation.approval_expires_at = None
    operation.approval_note = None
    operation.fence_token = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.remote_record_id = None
    operation.receipt = {}
    operation.verification = {}
    operation.error_code = None
    operation.error_message = None
    operation.started_at = None
    operation.completed_at = None

    event_details = {
        "operation_id": operation.id,
        "node_id": node_run.node_id,
        "prepared_at": prepared_at.isoformat(),
        "preflight_expires_at": preflight_expires_at.isoformat(),
        "prior_attempt_count": len(attempts),
        "reconciled_attempt_id": latest.id,
        "reconciliation_evidence_checksum": prior_reconciliation.get(
            "evidence_checksum"
        ),
        "remote_write_performed": False,
    }
    append_run_event(
        db,
        run_id=run.id,
        event_type="external_operation.rearmed_after_no_side_effect",
        payload=event_details,
    )
    append_audit_log(
        db,
        action="external_operation.rearm_after_no_side_effect",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        details={
            **event_details,
            "run_id": run.id,
            "previous_completed_at": (
                previous_completed_at.isoformat()
                if previous_completed_at is not None
                else None
            ),
        },
    )
    return True


def _create_prepared_external_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    credential: ExecutionCredential,
    account_scope_key: str,
    remote_business_key: str,
    request_summary: dict[str, Any],
    operation_key_prefix: str,
) -> tuple[ExecutionExternalOperation, bool]:
    credential_revision = int(credential.revision)
    payload_checksum = _canonical_checksum(
        {
            "request_summary": request_summary,
            "credential_binding": {
                "credential_id": credential.id,
                "credential_revision": credential_revision,
                "account_scope_key": account_scope_key,
                "remote_business_key": remote_business_key,
            },
        }
    )
    operation_key = hashlib.sha256(
        (
            f"{operation_key_prefix}:{run.id}:{node_run.node_id}"
        ).encode("utf-8")
    ).hexdigest()
    prepared_at = utcnow()
    preflight_expires_at = prepared_at + timedelta(
        minutes=settings.EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES
    )
    existing = (
        db.query(ExecutionExternalOperation)
        .filter(ExecutionExternalOperation.node_run_id == node_run.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if existing is None:
        existing = (
            db.query(ExecutionExternalOperation)
            .filter(ExecutionExternalOperation.operation_key == operation_key)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
    if existing is not None:
        if (
            existing.run_id != run.id
            or existing.node_run_id != node_run.id
            or existing.connector_key != LEGACY_CONNECTOR_KEY
            or existing.credential_id != credential.id
            or existing.credential_revision != credential_revision
            or existing.account_scope_key != account_scope_key
            or existing.remote_business_key != remote_business_key
            or existing.payload_checksum != payload_checksum
        ):
            raise conflict(
                "external_operation_idempotency_conflict",
                "该外部操作幂等键已绑定另一份预检内容",
                operation_id=existing.id,
            )
        rearmed = _rearm_expired_external_operation(
            db,
            operation=existing,
            run=run,
            node_run=node_run,
            prepared_at=prepared_at,
            preflight_expires_at=preflight_expires_at,
        )
        if not rearmed:
            _rearm_reconciled_no_side_effect_operation(
                db,
                operation=existing,
                run=run,
                node_run=node_run,
                prepared_at=prepared_at,
                preflight_expires_at=preflight_expires_at,
            )
        return existing, True

    blocking_operation = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.remote_business_key
            == remote_business_key,
            ExecutionExternalOperation.status.in_(
                ACTIVE_REMOTE_OPERATION_STATUSES
            ),
        )
        .order_by(ExecutionExternalOperation.created_at.asc())
        .with_for_update()
        .first()
    )
    if blocking_operation is not None:
        raise conflict(
            "external_remote_business_conflict",
            "同一样品已有待处理的旧系统操作，请先处理或取消原流程",
            operation_id=blocking_operation.id,
        )

    operation = ExecutionExternalOperation(
        operation_key=operation_key,
        run_id=run.id,
        node_run_id=node_run.id,
        connector_key=LEGACY_CONNECTOR_KEY,
        credential_id=credential.id,
        credential_revision=credential_revision,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        status="prepared",
        payload_checksum=payload_checksum,
        request_summary=request_summary,
        preflight_expires_at=preflight_expires_at,
    )
    try:
        with db.begin_nested():
            db.add(operation)
            db.flush()
    except IntegrityError as exc:
        blocker = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.remote_business_key
                == remote_business_key,
                ExecutionExternalOperation.status.in_(
                    ACTIVE_REMOTE_OPERATION_STATUSES
                ),
            )
            .order_by(ExecutionExternalOperation.created_at.asc())
            .first()
        )
        if blocker is not None:
            raise conflict(
                "external_remote_business_conflict",
                "同一样品已有待处理的旧系统操作，请先处理或取消原流程",
                operation_id=blocker.id,
            ) from exc
        raise conflict(
            "external_operation_idempotency_conflict",
            "该外部操作已被并发创建，请刷新后重试",
        ) from exc
    return operation, False


def prepare_legacy_regenerated_count_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Create a durable preflight record without contacting FibreCheck."""

    if node_run.node_type != LEGACY_REGENERATED_COUNT_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source_number = run.inspection_number.strip()
    target_number = resolve_legacy_target_sample_number(run)
    remote_business_key = lock_legacy_remote_business_scope(
        db,
        sample_number=target_number,
    )
    files, inspector_name = _selected_file_rows(
        db,
        run=run,
        input_data=input_data,
    )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_REGENERATED_COUNT_OPERATION,
        "source_inspection_number": source_number,
        "target_sample_number": target_number,
        "business_fields": {
            "fiber_category": "棉再生纤",
            "inspection_method": "定量",
            "inspection_item": "棉再生纤定量-根数法",
            "inspection_copies": 1,
        },
        "inspector": inspector_name,
        "files": files,
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=OPERATION_KEY_PREFIX,
    )


def prepare_legacy_special_wool_image_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Prepare an image-category SpecialWool upload without remote writes.

    The backend can allocate only against its durable operation fences.  The
    summary therefore labels the candidate provisional and the capability as
    unavailable until a Windows read-only probe supplies legacy occupancy and
    the official image-child save/readback behavior has been proved.
    """

    if node_run.node_type != LEGACY_SPECIAL_WOOL_IMAGE_UPLOAD_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source_number = run.inspection_number.strip().upper()
    requested_base = resolve_legacy_target_sample_number(run).upper()
    # Serialize the base family before consulting locally durable fences.
    lock_legacy_remote_business_scope(db, sample_number=requested_base)
    existing = (
        db.query(ExecutionExternalOperation)
        .filter(ExecutionExternalOperation.node_run_id == node_run.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        target_number = str(
            (existing.request_summary or {}).get("target_sample_number") or ""
        ).strip()
        if (
            _operation_type(existing) != LEGACY_SPECIAL_WOOL_IMAGE_OPERATION
            or not target_number
        ):
            raise conflict(
                "external_operation_idempotency_conflict",
                "该节点已绑定另一份外部操作预检单",
                operation_id=existing.id,
            )
    else:
        target_number = allocate_legacy_sample_number(
            requested_base,
            _legacy_special_wool_occupied_target_numbers(
                db,
                inspection_number=source_number,
            ),
        )
    remote_business_key = lock_legacy_remote_business_scope(
        db,
        sample_number=target_number,
    )
    task_project = _validated_microscopy_project_binding(input_data)
    files, inspector_name = _generated_microscopy_artifact_rows(
        db,
        run=run,
        input_data=input_data,
    )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        "profile": "special_wool_image_v1",
        "source_inspection_number": source_number,
        "target_sample_number": target_number,
        "target_filename": _special_wool_target_filename(target_number),
        "target_allocation": {
            "base_number": requested_base,
            "candidate_number": target_number,
            "suffix_policy": "base_then_numeric_suffix",
            "occupancy_scope": (
                "legacy_task_snapshot_and_execution_operation_fences"
            ),
            "legacy_readonly_verification_required": True,
        },
        "business_fields": {
            "fiber_category": "图片",
            "inspection_method": "",
            "inspection_item": "图片",
            "inspection_copies": 1,
            "review_item": "",
            "review_copies": 1,
        },
        "task_project": task_project,
        "inspector": inspector_name,
        "files": files,
        "execution_capability": (
            {"available": True}
            if settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED
            else dict(
                SPECIAL_WOOL_EXECUTION_CAPABILITY[
                    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION
                ]
            )
        ),
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
            "remote_target_allocation_verified": False,
        },
        "machine_contract": {
            "observation_type": SPECIAL_WOOL_IMAGE_OBSERVATION_TYPE,
            "receipt_type": SPECIAL_WOOL_IMAGE_RECEIPT_TYPE,
            "schema_version": 1,
            "read_only_probe_required": True,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=SPECIAL_WOOL_IMAGE_OPERATION_KEY_PREFIX,
    )


def _special_wool_upload_source_operation(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> ExecutionExternalOperation:
    upload_result = input_data.get("upload_result")
    operation_id = (
        str(upload_result.get("operation_id") or "").strip()
        if isinstance(upload_result, dict)
        else ""
    )
    if not operation_id and isinstance(upload_result, dict):
        # 早期人工对账完成路径没有把 operation_id 从节点预检输出带回
        # 顶层，但完整机器回执本身已与操作、运行和 payload checksum
        # 严格绑定。兼容读取该回执中的 ID，后续仍会查询同一 run 并再次
        # 执行 validate_external_receipt，不能按样品号猜测记录。
        nested_receipt = upload_result.get("receipt")
        if isinstance(nested_receipt, dict):
            operation_id = str(
                nested_receipt.get("operation_id") or ""
            ).strip()
    if not operation_id:
        raise ExecutionApiError(
            422,
            "special_wool_upload_result_required",
            "特纤复核必须引用本流程已完成的图片上传结果",
        )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == operation_id,
            ExecutionExternalOperation.run_id == run.id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or _operation_type(source) != LEGACY_SPECIAL_WOOL_IMAGE_OPERATION
        or source.status != "completed"
        or not isinstance(source.receipt, dict)
        or not source.receipt
    ):
        raise conflict(
            "special_wool_upload_not_completed",
            "特纤复核只能衔接本流程已完成且已回读核对的图片上传",
            operation_id=operation_id,
        )
    validate_external_receipt(source, source.receipt)
    return source


def _special_wool_upload_main_id(
    source: ExecutionExternalOperation,
) -> str:
    """Return the immutable, redacted main-row identity proved by upload.

    The target sample number is not an identity: a legacy row can be replaced
    under the same number.  Review therefore carries the upload readback's
    main-row id all the way to the Windows writer.
    """

    receipt = source.receipt if isinstance(source.receipt, dict) else {}
    main_record = receipt.get("main_record")
    if not isinstance(main_record, dict):
        raise conflict(
            "special_wool_upload_receipt_invalid",
            "图片上传回执缺少已核验的主记录标识，不能进入复核",
            operation_id=source.id,
        )
    try:
        return _required_text(
            main_record.get("id"),
            path="$.source_upload.main_record.id",
            pattern=_REDACTED_LEGACY_ID_RE,
        )
    except ExecutionApiError as exc:
        raise conflict(
            "special_wool_upload_receipt_invalid",
            "图片上传回执中的主记录标识无效，不能进入复核",
            operation_id=source.id,
        ) from exc


def prepare_legacy_special_wool_review_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Prepare an independent SpecialWool review fence."""

    if node_run.node_type != LEGACY_SPECIAL_WOOL_REVIEW_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source = _special_wool_upload_source_operation(
        db,
        run=run,
        input_data=input_data,
    )
    source_summary = source.request_summary or {}
    source_main_id = _special_wool_upload_main_id(source)
    # 上传可能按旧系统实况顺号改写；复核必须跟随回执中的实际写入编号。
    target_number = str(
        (source.receipt or {}).get("target_sample_number") or ""
    ).strip() or str(
        source_summary.get("target_sample_number") or ""
    ).strip()
    if not target_number:
        raise conflict(
            "special_wool_upload_receipt_invalid",
            "图片上传回执缺少目标样品编号，不能进入复核",
        )
    remote_business_key = lock_legacy_remote_business_scope(
        db,
        sample_number=target_number,
    )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        "profile": "special_wool_review_v1",
        "source_inspection_number": run.inspection_number.strip().upper(),
        "target_sample_number": target_number,
        "source_operation": {
            "operation_id": source.id,
            "payload_checksum": source.payload_checksum,
            "receipt_checksum": _canonical_checksum(source.receipt),
            "main_id": source_main_id,
        },
        "business_fields": {
            "fiber_category": "图片",
            "review_action": "特纤复核",
            "review_item": "",
            "review_copies": 1,
        },
        "task_project": dict(source_summary.get("task_project") or {}),
        "files": list(source_summary.get("files") or []),
        "execution_capability": (
            {"available": True}
            if settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED
            else dict(
                SPECIAL_WOOL_EXECUTION_CAPABILITY[
                    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION
                ]
            )
        ),
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
        },
        "machine_contract": {
            "observation_type": SPECIAL_WOOL_REVIEW_OBSERVATION_TYPE,
            "receipt_type": SPECIAL_WOOL_REVIEW_RECEIPT_TYPE,
            "schema_version": 1,
            "read_only_probe_required": True,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=SPECIAL_WOOL_REVIEW_OPERATION_KEY_PREFIX,
    )


def prepare_legacy_microscopy_check_record_entry_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Prepare one FibreCheck CheckRecord save-and-proof operation.

    CheckRecord is keyed by ``Task.ReportNo`` and must therefore always use the
    source inspection number.  The independently allocated SpecialWool
    ``base-N`` number is retained only as source-review audit evidence.
    """

    if node_run.node_type != LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source_number = run.inspection_number.strip().upper()
    if not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(source_number):
        raise ExecutionApiError(
            422,
            "external_target_sample_number_invalid",
            "检验记录登记的源检验编号格式无效",
        )
    declared_target = str(
        (run.input_data or {}).get("target_sample_number") or ""
    ).strip().upper()
    if declared_target and declared_target != source_number:
        raise ExecutionApiError(
            422,
            "microscopy_final_entry_target_override_forbidden",
            "检验记录登记必须使用任务单原编号，不能使用特纤后缀号",
        )
    remote_business_key = lock_legacy_remote_business_scope(
        db, sample_number=source_number
    )
    project = _validated_microscopy_project_binding(input_data)
    project_family = microscopy_family_for_project(
        project.get("check_item_no"),
        project.get("check_item_name"),
        db=db,
    )
    if project_family is None:
        raise ExecutionApiError(
            422,
            "microscopy_final_entry_project_mismatch",
            "检验记录登记节点仅支持 5103.5 / 纤维微观形貌 或"
            " 5103.426 / 纤维横截面",
        )
    source_review = _completed_special_wool_review_source(
        db, run=run, input_data=input_data
    )
    source_review_summary = source_review.request_summary or {}
    source_task_project = source_review_summary.get("task_project")
    if (
        not isinstance(source_task_project, dict)
        or source_task_project != project
    ):
        raise conflict(
            "microscopy_task_project_changed_after_upload",
            "检验记录登记前任务项目或检验份数已与图片上传时不一致，请重新运行流程",
        )
    file_row, template_binding, key_identity, generated_cells = (
        _generated_microscopy_check_record_artifact(
            db, run=run, input_data=input_data
        )
    )
    family_binding = project_family.template_bindings.get(
        template_binding.get("image_count")
    )
    if family_binding is None or any(
        template_binding.get(key) != expected
        for key, expected in family_binding.items()
    ):
        raise ExecutionApiError(
            422,
            "microscopy_final_entry_template_family_mismatch",
            f"检验记录登记模板不属于{project_family.check_item_name}家族，请重新生成",
        )
    selected_project = input_data.get("selected_project")
    if not isinstance(selected_project, dict):
        raise conflict(
            "microscopy_registration_context_changed",
            "检验记录登记项目上下文已变化，请重新运行流程",
        )
    record_input = input_data.get("record_input")
    record_input = record_input if isinstance(record_input, dict) else {}

    identity_options: list[str] = []
    seen_identities: set[str] = set()
    for part in re.split(
        r"[，,、]", str(selected_project.get("sample_identify") or "")
    ):
        identity = _normalized_business_text(part)
        if identity and identity.casefold() not in seen_identities:
            identity_options.append(identity)
            seen_identities.add(identity.casefold())
    sample_identity = _normalized_business_text(
        record_input.get("sample_identity") or key_identity
    )
    if key_identity != sample_identity:
        raise conflict(
            "microscopy_sample_identity_workbook_mismatch",
            "检验记录登记工作簿中的样品识别与人工确认结果不一致",
        )
    if identity_options and sample_identity not in identity_options:
        raise conflict(
            "microscopy_sample_identity_not_offered",
            "样品识别与录入前刷新到的任务单选项不一致，请重新确认",
        )
    if not identity_options and sample_identity:
        raise conflict(
            "microscopy_sample_identity_not_offered",
            "任务单未提供样品识别，不能写入旧系统下拉框",
        )

    raw_judgement_flag = selected_project.get("give_judgement")
    judgement_required = not (
        raw_judgement_flag is None
        or raw_judgement_flag is False
        or raw_judgement_flag == 0
        or str(raw_judgement_flag).strip().casefold()
        in {"", "0", "false", "no", "否", "否定"}
    )
    judgement_fields = {
        "judge_basis": _normalized_business_text(
            record_input.get("judge_basis") or generated_cells.get("I9")
        ),
        "indicator_requirement": _normalized_business_text(
            record_input.get("indicator_requirement")
            or generated_cells.get("I10")
        ),
        "test_result": _normalized_business_text(
            record_input.get("test_result") or generated_cells.get("I11")
        ),
        "judgement": _normalized_business_text(
            record_input.get("judgement") or generated_cells.get("G13")
        ),
        "remark": _normalized_business_text(
            record_input.get("remark") or generated_cells.get("G12")
        ),
    }
    generated_field_map = {
        "judge_basis": "I9",
        "indicator_requirement": "I10",
        "test_result": "I11",
        "judgement": "G13",
        "remark": "G12",
    }
    if any(
        _normalized_business_text(generated_cells.get(cell))
        != judgement_fields[field]
        for field, cell in generated_field_map.items()
    ):
        raise conflict(
            "microscopy_judgement_workbook_mismatch",
            "检验记录登记工作簿中的判定字段与人工确认结果不一致",
        )
    if judgement_required and any(
        not judgement_fields[field]
        for field in (
            "judge_basis",
            "indicator_requirement",
            "test_result",
            "judgement",
        )
    ):
        raise conflict(
            "microscopy_judgement_fields_required",
            "任务单要求判定，请先填写判定依据、指标要求、测试结果和判定",
        )
    if not judgement_required and any(
        judgement_fields[field]
        for field in (
            "judge_basis",
            "indicator_requirement",
            "test_result",
            "judgement",
        )
    ):
        raise conflict(
            "microscopy_judgement_fields_unexpected",
            "任务单未要求判定，检验记录登记工作簿不应包含判定字段",
        )

    registration_decision = input_data.get("registration_decision")
    register_count = selected_project.get("register_count")
    check_count = project["check_count"]
    append_existing = False
    if isinstance(registration_decision, dict):
        expected_existing = registration_decision.get(
            "expected_existing_register_count"
        )
        action = _normalized_business_text(
            registration_decision.get("existing_record_action")
        )
        if (
            not isinstance(expected_existing, int)
            or isinstance(expected_existing, bool)
            or expected_existing < 0
            or expected_existing != register_count
            or registration_decision.get("registration_cancelled") is True
        ):
            raise conflict(
                "microscopy_registration_decision_changed",
                "录入前已有登记核对结果已变化，请重新运行流程",
            )
        append_existing = check_count == 1 and expected_existing > 0
        if append_existing and action != "append":
            raise conflict(
                "microscopy_existing_record_confirmation_required",
                "当前单份项目已有登记，必须由用户确认直接新增",
            )
        if not append_existing and action != "continue":
            raise conflict(
                "microscopy_registration_decision_changed",
                "已有登记处理选择与当前任务份数不一致，请重新运行流程",
            )
    elif register_count == 0:
        # Compatibility for an already-running workflow published before the
        # shared registration-decision node was introduced.
        expected_existing = 0
    else:
        raise conflict(
            "microscopy_registration_decision_missing",
            "缺少录入前已有登记核对结果，请重新运行流程",
        )

    existing_record_decision = None
    if append_existing:
        existing_record_decision = {
            "kind": "append_when_check_count_one",
            "action": "append",
            "expected_task_check_count": 1,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": expected_existing + 1,
        }
    final_entry_package: dict[str, Any] = {
        "schema_version": 2,
        "operation_type": "excel_check_record",
        "sample_number": source_number,
        "check_item_no": project["check_item_no"],
        "check_item_name": project["check_item_name"],
        # The Writer re-queries the current Task_CheckItem row and recomputes
        # these one-way identifiers plus project_key before any side effect.
        "task_project": dict(project),
        "expected_existing_register_count": expected_existing,
        "excel_record": {
            "template_name": template_binding["legacy_template_name"],
            "collection_mode": "standard",
            "expected_mapping_config_sha256": template_binding[
                "mapping_config_sha256"
            ],
            "key_result_count": 1,
            "expected_key_identities": [key_identity],
            "register": {
                "level": "",
                "sample_identity": sample_identity,
                "equipment_no": "",
                "check_basis": "",
            },
            "workbook": {
                key: file_row[key]
                for key in (
                    "relative_path",
                    "filename",
                    "size_bytes",
                    "content_sha256",
                )
            },
        },
    }
    if existing_record_decision is not None:
        final_entry_package["existing_record_decision"] = (
            existing_record_decision
        )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION,
        "profile": "microscopy_check_record_entry_v1",
        "source_inspection_number": source_number,
        "target_sample_number": source_number,
        "task_project": project,
        "source_review_operation": {
            "operation_id": source_review.id,
            "payload_checksum": source_review.payload_checksum,
            "receipt_checksum": _canonical_checksum(source_review.receipt),
            "special_wool_target_sample_number": str(
                source_review_summary.get("target_sample_number") or ""
            ),
        },
        "files": [file_row],
        "template_binding": template_binding,
        "sample_identity_contract": {
            "selected": sample_identity,
            "options": identity_options,
            "option_count": len(identity_options),
            "check_count": check_count,
            "count_mismatch": bool(
                identity_options and len(identity_options) != check_count
            ),
        },
        "judgement_contract": {
            "required": judgement_required,
            **judgement_fields,
        },
        "existing_record_decision": existing_record_decision,
        "final_entry_package": final_entry_package,
        "final_entry_summary": {
            "source_review_target_sample_number": str(
                source_review_summary.get("target_sample_number") or ""
            ),
            "image_count": template_binding["image_count"],
            "expected_task_check_count": check_count,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": expected_existing + 1,
            "registration_capacity_mode": (
                "informational_for_multi_copy"
                if check_count > 1
                else "single_copy_confirmation_required"
            ),
            "registration_capacity_exceeded": (
                expected_existing + 1 > check_count
            ),
            "existing_record_append_confirmed": (
                existing_record_decision is not None
            ),
        },
        "business_fields": {
            "inspection_item": project["check_item_name"],
            "inspection_method": ELECTRON_TEST_METHOD,
            "inspection_copies": check_count,
            "sample_identity": sample_identity,
        },
        "execution_capability": {
            "available": bool(
                settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
            ),
            "code": (
                None
                if settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
                else "legacy_microscopy_final_entry_disabled"
            ),
            "message": (
                None
                if settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
                else "检验记录登记与校对写入当前未在部署环境启用"
            ),
        },
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
        },
        "machine_contract": {
            "receipt_type": MICROSCOPY_CHECK_RECORD_ENTRY_RECEIPT_TYPE,
            "schema_version": 1,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=(
            MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION_KEY_PREFIX
        ),
    )


def prepare_legacy_special_wool_qualitative_upload_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    """Prepare one document-only SpecialWool qualitative upload."""

    if node_run.node_type != LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source_number = run.inspection_number.strip().upper()
    requested_base = resolve_legacy_target_sample_number(run).upper()
    lock_legacy_remote_business_scope(db, sample_number=requested_base)
    existing = (
        db.query(ExecutionExternalOperation)
        .filter(ExecutionExternalOperation.node_run_id == node_run.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        target_number = str(
            (existing.request_summary or {}).get("target_sample_number") or ""
        ).strip()
        if (
            _operation_type(existing)
            != LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
            or not target_number
        ):
            raise conflict(
                "external_operation_idempotency_conflict",
                "该节点已绑定另一份外部操作预检单",
                operation_id=existing.id,
            )
    else:
        target_number = allocate_legacy_sample_number(
            requested_base,
            _legacy_special_wool_occupied_target_numbers(
                db, inspection_number=source_number
            ),
        )
    remote_business_key = lock_legacy_remote_business_scope(
        db, sample_number=target_number
    )
    project = _validated_paper_project_binding(
        input_data,
        rule=resolve_rule(db, PAPER_FIBER_RULE_KEY),
    )
    file_row, inspector, result_value, unit = _selected_paper_file_row(
        db, run=run, input_data=input_data
    )
    target_filename = _paper_special_wool_target_filename(
        target_number, file_row["filename"]
    )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        "profile": "special_wool_qualitative_upload_v1",
        "source_inspection_number": source_number,
        "target_sample_number": target_number,
        "target_filename": target_filename,
        "target_allocation": {
            "base_number": requested_base,
            "candidate_number": target_number,
            "suffix_policy": "base_then_numeric_suffix",
            "occupancy_scope": (
                "legacy_task_snapshot_and_execution_operation_fences"
            ),
            "legacy_readonly_verification_required": True,
        },
        "business_fields": {
            "fiber_category": "棉再生纤",
            "inspection_method": "定量",
            "inspection_item": PAPER_FIBER_SPECIAL_WOOL_ITEM,
            "inspection_copies": 1,
            "review_item": PAPER_FIBER_SPECIAL_WOOL_ITEM,
            "review_copies": 1,
        },
        "task_project": project,
        "inspector": inspector,
        "files": [file_row],
        "result_contract": {
            "worksheet": "Sheet1",
            "cell": "W32",
            "value": result_value,
            "unit": unit,
        },
        "execution_capability": (
            {"available": True}
            if settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED
            else dict(
                SPECIAL_WOOL_EXECUTION_CAPABILITY[
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
                ]
            )
        ),
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
            "expected_picture_count": 0,
        },
        "machine_contract": {
            "observation_type": (
                "legacy_special_wool_qualitative_upload_dry_run"
            ),
            "receipt_type": SPECIAL_WOOL_QUALITATIVE_UPLOAD_RECEIPT_TYPE,
            "schema_version": 1,
            "picture_count": 0,
            "read_only_probe_required": True,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=(
            SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION_KEY_PREFIX
        ),
    )


def _paper_upload_source_operation(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> ExecutionExternalOperation:
    upload_result = input_data.get("upload_result")
    operation_id = (
        str(upload_result.get("operation_id") or "").strip()
        if isinstance(upload_result, dict)
        else ""
    )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == operation_id,
            ExecutionExternalOperation.run_id == run.id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source)
        != LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        or not isinstance(source.receipt, dict)
        or not source.receipt
    ):
        raise conflict(
            "paper_special_wool_upload_not_completed",
            "纸纤维复核只能衔接本流程已完成且已核对的原始记录上传",
            operation_id=operation_id,
        )
    validate_external_receipt(source, source.receipt)
    return source


def prepare_legacy_special_wool_qualitative_review_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    if node_run.node_type != LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source = _paper_upload_source_operation(db, run=run, input_data=input_data)
    source_summary = source.request_summary or {}
    # 与图片复核一致：优先跟随上传回执中的实际顺号写入编号。
    target_number = str(
        (source.receipt or {}).get("target_sample_number") or ""
    ).strip() or str(source_summary.get("target_sample_number") or "").strip()
    source_main_id = _special_wool_upload_main_id(source)
    remote_business_key = lock_legacy_remote_business_scope(
        db, sample_number=target_number
    )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
        "profile": "special_wool_qualitative_review_v1",
        "source_inspection_number": run.inspection_number.strip().upper(),
        "target_sample_number": target_number,
        "source_operation": {
            "operation_id": source.id,
            "payload_checksum": source.payload_checksum,
            "receipt_checksum": _canonical_checksum(source.receipt),
            "main_id": source_main_id,
        },
        "business_fields": {
            "fiber_category": "棉再生纤",
            "review_action": "特纤复核",
            "review_item": PAPER_FIBER_SPECIAL_WOOL_ITEM,
            "review_copies": 1,
        },
        "task_project": dict(source_summary.get("task_project") or {}),
        "files": list(source_summary.get("files") or []),
        "result_contract": dict(source_summary.get("result_contract") or {}),
        "execution_capability": (
            {"available": True}
            if settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED
            else dict(
                SPECIAL_WOOL_EXECUTION_CAPABILITY[
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
                ]
            )
        ),
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
            "expected_picture_count": 0,
        },
        "machine_contract": {
            "observation_type": (
                "legacy_special_wool_qualitative_review_dry_run"
            ),
            "receipt_type": SPECIAL_WOOL_QUALITATIVE_REVIEW_RECEIPT_TYPE,
            "schema_version": 1,
            "picture_count": 0,
            "read_only_probe_required": True,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=(
            SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION_KEY_PREFIX
        ),
    )


def _completed_paper_review_source(
    db: Session,
    *,
    run: ExecutionRun,
    input_data: dict[str, Any],
) -> ExecutionExternalOperation:
    review_result = input_data.get("review_result")
    operation_id = (
        str(review_result.get("operation_id") or "").strip()
        if isinstance(review_result, dict)
        else ""
    )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == operation_id,
            ExecutionExternalOperation.run_id == run.id,
        )
        .with_for_update()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source)
        != LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
        or not isinstance(source.receipt, dict)
        or not source.receipt
    ):
        raise conflict(
            "paper_special_wool_review_not_completed",
            "检验记录登记只能衔接本流程已完成且已核对的纸纤维复核",
            operation_id=operation_id,
        )
    validate_external_receipt(source, source.receipt)
    return source


def prepare_legacy_generic_check_record_entry_operation(
    db: Session,
    *,
    run: ExecutionRun,
    node_run: ExecutionNodeRun,
    node: dict[str, Any],
    input_data: dict[str, Any],
) -> tuple[ExecutionExternalOperation, bool]:
    if node_run.node_type != LEGACY_GENERIC_CHECK_RECORD_ENTRY_NODE:
        raise ValueError("unsupported_external_node")
    if (run.capabilities_snapshot or {}).get("external_write") is not True:
        raise ExecutionApiError(
            403,
            "workflow_external_write_capability_required",
            "当前流程未声明外部系统写入能力",
        )
    credential = _credential_for_node(db, run=run, node=node)
    account_scope_key = _account_scope_key(credential.account_name or "")
    source_number = run.inspection_number.strip().upper()
    if not _LEGACY_SAMPLE_NUMBER_RE.fullmatch(source_number):
        raise ExecutionApiError(
            422,
            "external_target_sample_number_invalid",
            "检验记录登记的源检验编号格式无效",
        )
    remote_business_key = lock_legacy_remote_business_scope(
        db, sample_number=source_number
    )
    project = _validated_paper_project_binding(
        input_data,
        rule=resolve_rule(db, PAPER_FIBER_RULE_KEY),
    )
    source_review = _completed_paper_review_source(
        db, run=run, input_data=input_data
    )
    source_review_summary = source_review.request_summary or {}
    result_contract = source_review_summary.get("result_contract")
    if not isinstance(result_contract, dict):
        raise conflict(
            "paper_fiber_result_contract_changed",
            "纸纤维复核来源缺少已绑定的 W32 结果",
        )
    result_value = str(result_contract.get("value") or "").strip()
    unit = str(result_contract.get("unit") or "")
    if not result_value or unit not in {"", "%"}:
        raise conflict(
            "paper_fiber_result_contract_changed",
            "纸纤维 W32 结果或单位绑定无效",
        )
    # 判定变体：任务单要求判定时必须已由判定确认节点收集依据与结论；
    # 未要求判定时一律留空（Writer 会按任务单 GiveJudgement 再核验一次）。
    # give_judgement 是任务项目的辅助信息而非标识字段，不进 task_project
    # 契约（Writer ParseTaskProject 与回执均为严格键集）。
    selected_project = input_data.get("selected_project")
    give_judgement = (
        selected_project.get("give_judgement")
        if isinstance(selected_project, dict)
        else None
    )
    judgement_required = not (
        give_judgement is None
        or give_judgement is False
        or give_judgement == 0
        or str(give_judgement).strip().casefold()
        in {"", "0", "false", "no", "否", "否定"}
    )
    judgement_input = input_data.get("judgement_input")
    if not isinstance(judgement_input, dict):
        judgement_input = {}
    judge_basis = " ".join(
        str(judgement_input.get("judge_basis") or "").strip().split()
    )
    judgement = " ".join(
        str(judgement_input.get("judgement") or "").strip().split()
    )
    # 标准值与允差由人工在判定确认步骤核对填写（默认 W32 同文，可改）。
    standard_value = " ".join(
        str(judgement_input.get("standard_value") or "").strip().split()
    )
    if judgement_required and (not judge_basis or not judgement):
        raise conflict(
            "paper_fiber_judgement_required",
            "任务单要求对本项目判定，请先完成判定信息确认后再登记",
        )
    if judgement_required and not standard_value:
        raise conflict(
            "paper_fiber_standard_value_required",
            "请先在确认判定信息步骤填写标准值与允差后再登记",
        )
    if not judgement_required:
        judge_basis = ""
        judgement = ""
        standard_value = ""

    identity_options: list[str] = []
    seen_identities: set[str] = set()
    for part in re.split(
        r"[，,、]", str(selected_project.get("sample_identify") or "")
    ):
        identity = " ".join(part.strip().split())
        if identity and identity.casefold() not in seen_identities:
            identity_options.append(identity)
            seen_identities.add(identity.casefold())
    sample_identity = " ".join(
        str(judgement_input.get("sample_identity") or "").strip().split()
    )
    if identity_options and sample_identity not in identity_options:
        raise conflict(
            "paper_sample_identity_not_offered",
            "样品识别与录入前刷新到的任务单选项不一致，请重新确认",
        )
    if not identity_options and sample_identity:
        raise conflict(
            "paper_sample_identity_not_offered",
            "任务单未提供样品识别，不能写入旧系统下拉框",
        )

    registration_decision = input_data.get("registration_decision")
    if not isinstance(registration_decision, dict):
        raise conflict(
            "paper_registration_decision_missing",
            "缺少录入前已有登记核对结果，请重新运行流程",
        )
    check_count = project["check_count"]
    expected_existing = registration_decision.get(
        "expected_existing_register_count"
    )
    action = str(
        registration_decision.get("existing_record_action") or ""
    ).strip()
    if (
        not isinstance(expected_existing, int)
        or isinstance(expected_existing, bool)
        or expected_existing < 0
        or expected_existing != selected_project.get("register_count")
        or registration_decision.get("registration_cancelled") is True
    ):
        raise conflict(
            "paper_registration_decision_changed",
            "录入前已有登记核对结果已变化，请重新运行流程",
        )
    append_existing = check_count == 1 and expected_existing > 0
    if append_existing and action != "append":
        raise conflict(
            "paper_existing_record_confirmation_required",
            "当前单份项目已有登记，必须由用户确认直接新增",
        )
    if not append_existing and action != "continue":
        raise conflict(
            "paper_registration_decision_changed",
            "已有登记处理选择与当前任务份数不一致，请重新运行流程",
        )
    final_entry_package = {
        "schema_version": 2,
        "operation_type": "generic_item_record",
        "sample_number": source_number,
        "check_item_no": project["check_item_no"],
        "check_item_name": project["check_item_name"],
        "task_project": dict(project),
        "expected_existing_register_count": expected_existing,
        "generic_record": {
            "header": {
                "grade": "",
                "unit": unit,
                "judge_basis": judge_basis,
                "test_method": PAPER_FIBER_TEST_METHOD,
                "sample_description": sample_identity,
                "standard_type": "",
                "report_check_item_name": (
                    project["check_item_name"] if judgement_required else ""
                ),
                "attach_info": "",
                "remark": "",
                "total_judge": judgement,
            },
            "details": [
                {
                    "standard_location": "",
                    "standard_value": (
                        standard_value if judgement_required else ""
                    ),
                    "real_location": "",
                    "real_value": result_value,
                }
            ],
        },
    }
    existing_record_decision = None
    if append_existing:
        existing_record_decision = {
            "kind": "append_when_check_count_one",
            "action": "append",
            "expected_task_check_count": 1,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": expected_existing + 1,
        }
        final_entry_package["existing_record_decision"] = (
            existing_record_decision
        )
    request_summary = {
        "schema_version": 1,
        "operation_type": LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
        "profile": "generic_check_record_entry_v1",
        "source_inspection_number": source_number,
        "target_sample_number": source_number,
        "task_project": project,
        "source_review_operation": {
            "operation_id": source_review.id,
            "payload_checksum": source_review.payload_checksum,
            "receipt_checksum": _canonical_checksum(source_review.receipt),
            "special_wool_target_sample_number": str(
                source_review_summary.get("target_sample_number") or ""
            ),
        },
        "result_contract": dict(result_contract),
        # Public, checksum-bound contract used by both the approval UI and the
        # Windows Bridge to verify the exact human-confirmed judgement fields.
        "judgement_contract": {
            "required": judgement_required,
            "judge_basis": judge_basis,
            "judgement": judgement,
            "standard_value": standard_value,
        },
        "sample_identity_contract": {
            "selected": sample_identity,
            "options": identity_options,
            "option_count": len(identity_options),
            "check_count": check_count,
            "count_mismatch": bool(
                identity_options and len(identity_options) != check_count
            ),
        },
        "existing_record_decision": existing_record_decision,
        "final_entry_package": final_entry_package,
        "final_entry_summary": {
            "expected_task_check_count": check_count,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": expected_existing + 1,
            "detail_count": 1,
            "judgement_required": judgement_required,
            "expected_proofed_count": 0,
        },
        "business_fields": {
            "inspection_item": project["check_item_name"],
            "inspection_method": project["check_method"],
            "inspection_copies": check_count,
            "sample_identity": sample_identity,
        },
        "execution_capability": {
            "available": bool(
                settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
            ),
            "code": (
                None
                if settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
                else "legacy_generic_check_record_entry_disabled"
            ),
            "message": (
                None
                if settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED
                else "通用检验记录登记写入当前未在部署环境启用"
            ),
        },
        "safety": {
            "remote_write_performed": False,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
            "proof_required": False,
        },
        "machine_contract": {
            "receipt_type": GENERIC_CHECK_RECORD_ENTRY_RECEIPT_TYPE,
            "schema_version": 1,
            "proof_required": False,
        },
    }
    return _create_prepared_external_operation(
        db,
        run=run,
        node_run=node_run,
        credential=credential,
        account_scope_key=account_scope_key,
        remote_business_key=remote_business_key,
        request_summary=request_summary,
        operation_key_prefix=GENERIC_CHECK_RECORD_ENTRY_OPERATION_KEY_PREFIX,
    )


def _reverify_special_wool_review_source(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
) -> None:
    source_ref = (operation.request_summary or {}).get("source_operation")
    if not isinstance(source_ref, dict):
        raise conflict(
            "external_operation_preflight_invalid",
            "复核预检单缺少来源上传操作",
            operation_id=operation.id,
        )
    source = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id
            == str(source_ref.get("operation_id") or ""),
            ExecutionExternalOperation.run_id == operation.run_id,
        )
        .with_for_update()
        .one_or_none()
    )
    target = str(
        (operation.request_summary or {}).get("target_sample_number") or ""
    )
    expected_main_id = str(source_ref.get("main_id") or "")
    # 上传可能按旧系统实况顺号改写（如人工删除后顺号回退）；
    # 与 prepare 对称，以来源回执中的实际写入编号为准。
    source_target = ""
    if source is not None:
        source_target = str(
            (source.receipt or {}).get("target_sample_number") or ""
        ).strip() or str(
            (source.request_summary or {}).get("target_sample_number") or ""
        ).strip()
    if (
        source is None
        or source.status != "completed"
        or _operation_type(source) != LEGACY_SPECIAL_WOOL_IMAGE_OPERATION
        or source.payload_checksum != source_ref.get("payload_checksum")
        or not isinstance(source.receipt, dict)
        or _canonical_checksum(source.receipt)
        != source_ref.get("receipt_checksum")
        or source_target != target
    ):
        raise conflict(
            "special_wool_upload_result_changed",
            "复核所引用的图片上传结果已变化，请重新运行流程",
            operation_id=operation.id,
        )
    validate_external_receipt(source, source.receipt)
    if _special_wool_upload_main_id(source) != expected_main_id:
        raise conflict(
            "special_wool_upload_result_changed",
            "复核所引用的上传主记录已变化，请重新运行流程",
            operation_id=operation.id,
        )


def approve_prepared_external_operation(
    db: Session,
    *,
    operation: ExecutionExternalOperation,
    run: ExecutionRun,
    actor: ExecutionUser,
    payload_checksum: str,
    confirmed_sample_number: str,
    note: str | None = None,
    automatic: bool = False,
) -> tuple[ExecutionExternalOperation, bool]:
    if payload_checksum != operation.payload_checksum:
        raise conflict(
            "external_operation_payload_changed",
            "预检内容已变化，请刷新并重新核对后再确认",
            operation_id=operation.id,
        )
    target_sample_number = str(
        (operation.request_summary or {}).get("target_sample_number") or ""
    )
    if confirmed_sample_number != target_sample_number:
        raise conflict(
            "external_operation_sample_confirmation_mismatch",
            "确认的样品编号与预检单不一致，请重新核对",
            operation_id=operation.id,
        )
    if operation.run_id != run.id:
        raise conflict(
            "external_operation_run_changed",
            "预检单与当前流程运行不一致，请刷新后重试",
            operation_id=operation.id,
        )
    if operation.status not in {"prepared", "approved"}:
        raise conflict(
            "external_operation_not_approvable",
            "当前外部操作状态不能批准",
            operation_id=operation.id,
            status=operation.status,
        )

    now = utcnow()
    if operation.status == "prepared":
        _ensure_preflight_not_expired(operation, now=now)
    else:
        _ensure_approval_not_expired(operation, now=now)
    _ensure_operation_execution_available(operation)
    _bound_credential_for_approval(
        db,
        operation=operation,
        run=run,
    )
    _reverify_operation_sources(db, operation=operation)
    if operation.status == "approved":
        return operation, True

    operation.status = "approved"
    operation.approved_by_id = actor.id
    operation.approved_at = now
    operation.approval_expires_at = (
        None
        if automatic
        else now
        + timedelta(
            minutes=settings.EXECUTION_EXTERNAL_APPROVAL_TTL_MINUTES
        )
    )
    operation.approval_note = note.strip() if note and note.strip() else None
    node_run = db.get(ExecutionNodeRun, operation.node_run_id)
    if node_run is not None and node_run.status == "waiting_external":
        output = dict(node_run.output_data or {})
        output["status"] = "approved"
        output["approved_at"] = now.isoformat()
        output["approval_expires_at"] = _isoformat(
            operation.approval_expires_at
        )
        node_run.output_data = output
    append_run_event(
        db,
        run_id=operation.run_id,
        event_type="external_operation.approved",
        actor_type="system" if automatic else "user",
        actor_id=None if automatic else actor.id,
        payload={
            "operation_id": operation.id,
            "node_id": node_run.node_id if node_run is not None else None,
            "status": operation.status,
            "payload_checksum": operation.payload_checksum,
            "approval_expires_at": _isoformat(
                operation.approval_expires_at
            ),
            "remote_write_performed": False,
            "automatic": automatic,
        },
    )
    append_audit_log(
        db,
        action="external_operation.approve",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        actor_user_id=actor.id,
        details={
            "run_id": operation.run_id,
            "payload_checksum": operation.payload_checksum,
            "approval_expires_at": _isoformat(
                operation.approval_expires_at
            ),
            "remote_write_performed": False,
            "automatic": automatic,
        },
    )
    return operation, False


def _public_remote_write_performed(
    operation: ExecutionExternalOperation,
) -> bool | None:
    """Return true, false, or unknown without claiming more than we know."""

    reconciliation = dict(operation.verification or {}).get(
        "reconciliation"
    )
    if isinstance(reconciliation, dict):
        if reconciliation.get("action") == "confirm_completed":
            return True
        if reconciliation.get("action") == "confirm_no_side_effect":
            return False
    if operation.status == "completed":
        return True
    if operation.status == "reconciliation_required":
        return None
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    boundary_index = attempt_stages.index(write_boundary)
    for attempt in operation.attempts or []:
        stage = attempt.current_stage
        if (
            stage in attempt_stages
            and attempt_stages.index(stage) >= boundary_index
        ):
            return None
    return False


def public_external_operation(
    operation: ExecutionExternalOperation,
) -> dict[str, Any]:
    summary = operation.request_summary or {}
    remote_write_performed = _public_remote_write_performed(operation)
    business_fields = summary.get("business_fields")
    result_contract = summary.get("result_contract")
    judgement_contract = summary.get("judgement_contract")
    files = summary.get("files")
    public_files = [
        {
            key: item[key]
            for key in (
                "id",
                "artifact_id",
                "root_id",
                "relative_path",
                "filename",
                "fingerprint",
                "content_sha256",
                "size_bytes",
                "is_primary",
            )
            if key in item
        }
        for item in files
        if isinstance(item, dict)
    ] if isinstance(files, list) else []
    public_summary = {
        "schema_version": summary.get("schema_version"),
        "operation_type": summary.get("operation_type"),
        "profile": summary.get("profile"),
        "source_inspection_number": (
            summary.get("source_inspection_number")
            or summary.get("target_sample_number")
        ),
        "target_sample_number": summary.get("target_sample_number"),
        "target_filename": summary.get("target_filename"),
        "business_fields": {
            key: business_fields.get(key)
            for key in (
                "fiber_category",
                "inspection_method",
                "inspection_item",
                "inspection_copies",
                "sample_identity",
                "review_action",
                "review_item",
                "review_copies",
            )
        } if isinstance(business_fields, dict) else {},
        "inspector": summary.get("inspector"),
        "files": public_files,
        "target_allocation": (
            dict(summary.get("target_allocation"))
            if isinstance(summary.get("target_allocation"), dict)
            else None
        ),
        "source_operation": (
            dict(summary.get("source_operation"))
            if isinstance(summary.get("source_operation"), dict)
            else None
        ),
        "task_project": (
            dict(summary.get("task_project"))
            if isinstance(summary.get("task_project"), dict)
            else None
        ),
        "result_contract": (
            {
                key: result_contract.get(key)
                for key in ("worksheet", "cell", "value", "unit")
            }
            if isinstance(result_contract, dict)
            else None
        ),
        "judgement_contract": (
            {
                key: judgement_contract[key]
                for key in (
                    "required",
                    "judge_basis",
                    "indicator_requirement",
                    "test_result",
                    "judgement",
                    "standard_value",
                    "remark",
                )
                if key in judgement_contract
            }
            if isinstance(judgement_contract, dict)
            else None
        ),
        "sample_identity_contract": (
            dict(summary.get("sample_identity_contract"))
            if isinstance(summary.get("sample_identity_contract"), dict)
            else None
        ),
        "existing_record_decision": (
            dict(summary.get("existing_record_decision"))
            if isinstance(summary.get("existing_record_decision"), dict)
            else None
        ),
        "final_entry_summary": (
            dict(summary.get("final_entry_summary"))
            if isinstance(summary.get("final_entry_summary"), dict)
            else None
        ),
        "machine_contract": (
            dict(summary.get("machine_contract"))
            if isinstance(summary.get("machine_contract"), dict)
            else None
        ),
        "execution_capability": (
            dict(summary.get("execution_capability"))
            if isinstance(summary.get("execution_capability"), dict)
            else {"available": True}
        ),
        "safety": {
            "remote_write_performed": remote_write_performed,
            "requires_final_approval": False,
            "requires_source_reverification": True,
            "overwrite_allowed": False,
            "execution_available": _operation_execution_capability(
                operation
            ).get("available") is not False,
        },
    }
    reconciliation_record = dict(operation.verification or {}).get(
        "reconciliation"
    )
    public_reconciliation = None
    if operation.status == "reconciliation_required" or isinstance(
        reconciliation_record,
        dict,
    ):
        public_reconciliation = {
            "required": operation.status == "reconciliation_required",
            "action": (
                reconciliation_record.get("action")
                if isinstance(reconciliation_record, dict)
                else None
            ),
            "attempt_id": (
                reconciliation_record.get("attempt_id")
                if isinstance(reconciliation_record, dict)
                else None
            ),
            "resolved_at": (
                reconciliation_record.get("reconciled_at")
                if isinstance(reconciliation_record, dict)
                else None
            ),
        }
    return {
        "id": operation.id,
        "operation_key": operation.operation_key,
        "run_id": operation.run_id,
        "node_run_id": operation.node_run_id,
        "connector_key": operation.connector_key,
        "status": operation.status,
        "payload_checksum": operation.payload_checksum,
        "request_summary": public_summary,
        "preflight_expires_at": _isoformat(
            operation.preflight_expires_at
        ),
        "approval": {
            "approved_by_id": operation.approved_by_id,
            "approved_at": _isoformat(operation.approved_at),
            "expires_at": _isoformat(operation.approval_expires_at),
            "note": operation.approval_note,
        },
        "remote_write_performed": remote_write_performed,
        "reconciliation": public_reconciliation,
        "error": (
            {
                "code": operation.error_code,
                "message": operation.error_message,
            }
            if operation.error_code
            else None
        ),
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
    }


def _latest_external_attempt(
    operation: ExecutionExternalOperation,
) -> ExecutionExternalAttempt | None:
    attempts = list(operation.attempts or [])
    if not attempts:
        return None
    return max(attempts, key=lambda item: int(item.attempt_no or 0))


def _final_entry_reconciliation_expectations(
    operation: ExecutionExternalOperation,
) -> dict[str, Any]:
    summary = operation.request_summary or {}
    final_entry_summary = summary.get("final_entry_summary")
    if not isinstance(final_entry_summary, dict):
        raise ExecutionApiError(
            422,
            "external_reconciliation_final_entry_summary_invalid",
            "检验记录登记预检摘要缺失，不能执行人工对账",
        )
    expected_existing = final_entry_summary.get(
        "expected_existing_register_count"
    )
    resulting = final_entry_summary.get("resulting_register_count")
    expected_task_count = final_entry_summary.get(
        "expected_task_check_count"
    )
    exceptional_append = bool(
        isinstance(summary.get("existing_record_decision"), dict)
        or (
            isinstance(expected_task_count, int)
            and not isinstance(expected_task_count, bool)
            and expected_task_count > 1
        )
    )
    if (
        not isinstance(expected_existing, int)
        or isinstance(expected_existing, bool)
        or expected_existing < 0
        or not isinstance(resulting, int)
        or isinstance(resulting, bool)
        or resulting != expected_existing + 1
        or not isinstance(expected_task_count, int)
        or isinstance(expected_task_count, bool)
        or expected_task_count < 1
        or (resulting > expected_task_count and not exceptional_append)
    ):
        raise ExecutionApiError(
            422,
            "external_reconciliation_final_entry_summary_invalid",
            "检验记录登记预检计数摘要无效，不能执行人工对账",
        )
    return {
        "summary": dict(final_entry_summary),
        "summary_checksum": _canonical_checksum(final_entry_summary),
        "expected_existing_register_count": expected_existing,
        "resulting_register_count": resulting,
    }


def _final_entry_expected_reconciliation_evidence(
    operation: ExecutionExternalOperation,
    *,
    attempt: ExecutionExternalAttempt,
) -> dict[str, Any]:
    expected = _final_entry_reconciliation_expectations(operation)
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    boundary_index = attempt_stages.index(write_boundary)
    existing = expected["expected_existing_register_count"]
    resulting = expected["resulting_register_count"]
    common = {
        "evidence_contract": FINAL_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT,
        "final_entry_summary": expected["summary"],
        "final_entry_summary_checksum": expected["summary_checksum"],
        "expected_existing_register_count": existing,
        "writer_stage": attempt.current_stage,
    }
    return {
        **common,
        "confirm_completed": {
            "expected_existing_register_count": existing,
            "resulting_register_count": resulting,
            "actual_register_count": resulting,
            "actual_file_reference_count": resulting,
            "actual_key_result_count": resulting,
            "actual_proofed_count": resulting,
            "target_file_count": 1,
            "writer_stage": attempt.current_stage,
            "allowed_writer_stages": list(
                attempt_stages[boundary_index:]
            ),
        },
        "confirm_no_side_effect": {
            "expected_existing_register_count": existing,
            "actual_register_count": existing,
            "actual_file_reference_count": existing,
            "actual_key_result_count": existing,
            "actual_proofed_count": existing,
            "target_file_count": 0,
            "writer_stage": attempt.current_stage,
            "latest_allowed_writer_stage": write_boundary,
            "allowed_writer_stages": list(
                attempt_stages[: boundary_index + 1]
            ),
        },
    }


def _generic_entry_expected_reconciliation_evidence(
    operation: ExecutionExternalOperation,
    *,
    attempt: ExecutionExternalAttempt,
) -> dict[str, Any]:
    expected = _final_entry_reconciliation_expectations(operation)
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    boundary_index = attempt_stages.index(write_boundary)
    existing = expected["expected_existing_register_count"]
    resulting = expected["resulting_register_count"]
    common = {
        "evidence_contract": GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT,
        "final_entry_summary": expected["summary"],
        "final_entry_summary_checksum": expected["summary_checksum"],
        "expected_existing_register_count": existing,
        "writer_stage": attempt.current_stage,
    }
    return {
        **common,
        "confirm_completed": {
            "expected_existing_register_count": existing,
            "resulting_register_count": resulting,
            "actual_register_count": resulting,
            "actual_detail_count": resulting,
            "actual_key_result_count": resulting,
            "actual_proofed_count": 0,
            "writer_stage": attempt.current_stage,
            "allowed_writer_stages": list(attempt_stages[boundary_index:]),
        },
        "confirm_no_side_effect": {
            "expected_existing_register_count": existing,
            "actual_register_count": existing,
            "actual_detail_count": existing,
            "actual_key_result_count": existing,
            "actual_proofed_count": 0,
            "writer_stage": attempt.current_stage,
            "latest_allowed_writer_stage": write_boundary,
            "allowed_writer_stages": list(
                attempt_stages[: boundary_index + 1]
            ),
        },
    }


def public_external_reconciliation_context(
    operation: ExecutionExternalOperation,
) -> dict[str, Any]:
    """Return context for an admin attestation, not a machine probe result."""

    if operation.status != "reconciliation_required":
        raise conflict(
            "external_reconciliation_not_required",
            "当前外部操作不处于待对账状态",
            operation_id=operation.id,
            status=operation.status,
        )
    attempt = _latest_external_attempt(operation)
    if attempt is None:
        raise conflict(
            "external_reconciliation_attempt_missing",
            "待对账操作缺少执行尝试，不能人工处置",
            operation_id=operation.id,
        )
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    if (
        attempt.status != "failed"
        or attempt.current_stage not in attempt_stages
        or attempt_stages.index(attempt.current_stage)
        < attempt_stages.index(write_boundary)
    ):
        raise conflict(
            "external_reconciliation_attempt_invalid",
            "最新执行尝试不满足人工对账条件",
            attempt_id=attempt.id,
            status=attempt.status,
            current_stage=attempt.current_stage,
        )
    summary = operation.request_summary or {}
    files = summary.get("files")
    source_file = (
        files[0]
        if isinstance(files, list)
        and len(files) == 1
        and isinstance(files[0], dict)
        else {}
    )
    expected_evidence = {
        "target_sample_number": summary.get("target_sample_number"),
        "payload_checksum": operation.payload_checksum,
        "source_file_sha256": source_file.get("content_sha256"),
        "confirm_completed": {
            "exact_record_count": 1,
            "target_file_count": 1,
            "business_fields_match": True,
            "inspector_match": True,
        },
        "confirm_no_side_effect": {
            "exact_record_count": 0,
            "contains_record_count": 0,
            "target_file_count": 0,
        },
    }
    if (
        _operation_type(operation)
        == LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION
    ):
        expected_evidence = {
            "target_sample_number": summary.get("target_sample_number"),
            "payload_checksum": operation.payload_checksum,
            "source_file_sha256": source_file.get("content_sha256"),
            **_final_entry_expected_reconciliation_evidence(
                operation,
                attempt=attempt,
            ),
        }
    elif _operation_type(operation) == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION:
        expected_evidence = {
            "target_sample_number": summary.get("target_sample_number"),
            "payload_checksum": operation.payload_checksum,
            **_generic_entry_expected_reconciliation_evidence(
                operation,
                attempt=attempt,
            ),
        }
    return {
        "evidence_kind": "admin_attestation_v1",
        "attestation_notice": (
            "以下证据由管理员根据只读核对结果人工声明；"
            "当前系统不会自动执行或签名远端探针。"
        ),
        "operation": public_external_operation(operation),
        "attempt": {
            "id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "bridge_id": attempt.bridge_id,
            "status": attempt.status,
            "current_stage": attempt.current_stage,
            "checkpoints": [
                {
                    "stage": item.get("stage"),
                    "at": item.get("at"),
                }
                for item in (attempt.checkpoints or [])
                if isinstance(item, dict)
            ],
            "error": (
                {
                    "code": attempt.error_code,
                    "message": attempt.error_message,
                }
                if attempt.error_code
                else None
            ),
            "started_at": _isoformat(attempt.started_at),
            "finished_at": _isoformat(attempt.finished_at),
        },
        "expected_evidence": expected_evidence,
    }


def _normalized_reconciliation_evidence(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    value = dict(evidence or {})
    checked_at = value.get("checked_at")
    if isinstance(checked_at, datetime):
        value["checked_at"] = checked_at.isoformat()
    return value


def _reconciliation_checked_at(
    evidence: dict[str, Any],
) -> datetime:
    raw_value = evidence.get("checked_at")
    if isinstance(raw_value, datetime):
        value = raw_value
    elif isinstance(raw_value, str):
        try:
            value = datetime.fromisoformat(
                raw_value.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ExecutionApiError(
                422,
                "external_reconciliation_checked_at_invalid",
                "对账证据时间格式无效",
            ) from exc
    else:
        raise ExecutionApiError(
            422,
            "external_reconciliation_checked_at_invalid",
            "对账证据必须包含核验时间",
        )
    if value.tzinfo is None:
        raise ExecutionApiError(
            422,
            "external_reconciliation_checked_at_timezone_required",
            "对账证据时间必须包含时区",
        )
    return _aware_utc(value)


def _validate_final_entry_reconciliation_evidence(
    operation: ExecutionExternalOperation,
    *,
    attempt: ExecutionExternalAttempt,
    action: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    expected = _final_entry_reconciliation_expectations(operation)
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    boundary_index = attempt_stages.index(write_boundary)

    def reject(reason: str, **details: Any) -> None:
        raise ExecutionApiError(
            422,
            "external_reconciliation_evidence_incomplete",
            (
                "检验记录登记对账证据不能证明所选结论，"
                "操作仍保持锁定"
            ),
            details={"reason": reason, **details},
        )

    common_keys = {
        "evidence_contract",
        "checked_at",
        "final_entry_summary_checksum",
        "expected_existing_register_count",
        "actual_register_count",
        "actual_file_reference_count",
        "actual_key_result_count",
        "actual_proofed_count",
        "target_file_count",
        "writer_stage",
    }
    required_keys = set(common_keys)
    if action == "confirm_completed":
        required_keys.update({"resulting_register_count", "remote_record_id"})
    elif action != "confirm_no_side_effect":
        reject("unsupported_action", action=action)
    if set(evidence) != required_keys:
        reject(
            "evidence_shape_mismatch",
            missing=sorted(required_keys - set(evidence)),
            unexpected=sorted(set(evidence) - required_keys),
        )
    if (
        evidence.get("evidence_contract")
        != FINAL_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT
    ):
        reject("evidence_contract_mismatch")
    if (
        evidence.get("final_entry_summary_checksum")
        != expected["summary_checksum"]
    ):
        reject("final_entry_summary_checksum_mismatch")
    existing = expected["expected_existing_register_count"]
    resulting = expected["resulting_register_count"]
    if evidence.get("expected_existing_register_count") != existing:
        reject(
            "expected_existing_register_count_mismatch",
            expected=existing,
            actual=evidence.get("expected_existing_register_count"),
        )

    writer_stage = str(evidence.get("writer_stage") or "").strip()
    if writer_stage not in attempt_stages:
        reject("writer_stage_unknown", writer_stage=writer_stage)
    if writer_stage != attempt.current_stage:
        reject(
            "writer_stage_attempt_mismatch",
            writer_stage=writer_stage,
            attempt_stage=attempt.current_stage,
        )
    writer_stage_index = attempt_stages.index(writer_stage)

    if action == "confirm_no_side_effect":
        valid = (
            writer_stage_index <= boundary_index
            and evidence.get("actual_register_count") == existing
            and evidence.get("actual_file_reference_count") == existing
            and evidence.get("actual_key_result_count") == existing
            and evidence.get("actual_proofed_count") == existing
            and evidence.get("target_file_count") == 0
        )
        if not valid:
            reject(
                "no_side_effect_counts_or_stage_mismatch",
                latest_allowed_writer_stage=write_boundary,
                expected_existing_register_count=existing,
            )
        return None

    remote_record_id = str(
        evidence.get("remote_record_id") or ""
    ).strip()
    valid = (
        writer_stage_index >= boundary_index
        and evidence.get("resulting_register_count") == resulting
        and evidence.get("actual_register_count") == resulting
        and evidence.get("actual_file_reference_count") == resulting
        and evidence.get("actual_key_result_count") == resulting
        and evidence.get("actual_proofed_count") == resulting
        and evidence.get("target_file_count") == 1
        and bool(remote_record_id)
    )
    if not valid:
        reject(
            "completed_counts_file_or_proof_mismatch",
            resulting_register_count=resulting,
            earliest_writer_stage=write_boundary,
        )
    summary = operation.request_summary or {}
    return {
        "schema_version": 1,
        "receipt_type": (
            "legacy_microscopy_check_record_entry_manual_reconciliation"
        ),
        "source": "manual_reconciliation",
        "operation_id": operation.id,
        "target_sample_number": summary.get("target_sample_number"),
        "remote_record_id": remote_record_id,
        "final_entry_summary_checksum": expected["summary_checksum"],
        "final_entry": {
            "expected_existing_register_count": existing,
            "resulting_register_count": resulting,
            "actual_register_count": evidence["actual_register_count"],
            "actual_file_reference_count": evidence[
                "actual_file_reference_count"
            ],
            "actual_key_result_count": evidence[
                "actual_key_result_count"
            ],
            "actual_proofed_count": evidence["actual_proofed_count"],
            "target_file_count": evidence["target_file_count"],
            "writer_stage": writer_stage,
            "proofed": True,
        },
    }


def _validate_generic_entry_reconciliation_evidence(
    operation: ExecutionExternalOperation,
    *,
    attempt: ExecutionExternalAttempt,
    action: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    expected = _final_entry_reconciliation_expectations(operation)
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    boundary_index = attempt_stages.index(write_boundary)

    def reject(reason: str, **details: Any) -> None:
        raise ExecutionApiError(
            422,
            "external_reconciliation_evidence_incomplete",
            "通用检验记录登记对账证据不能证明所选结论，操作仍保持锁定",
            details={"reason": reason, **details},
        )

    common_keys = {
        "evidence_contract",
        "checked_at",
        "final_entry_summary_checksum",
        "expected_existing_register_count",
        "actual_register_count",
        "actual_detail_count",
        "actual_key_result_count",
        "actual_proofed_count",
        "writer_stage",
    }
    required_keys = set(common_keys)
    if action == "confirm_completed":
        required_keys.update({"resulting_register_count", "remote_record_id"})
    elif action != "confirm_no_side_effect":
        reject("unsupported_action", action=action)
    if set(evidence) != required_keys:
        reject(
            "evidence_shape_mismatch",
            missing=sorted(required_keys - set(evidence)),
            unexpected=sorted(set(evidence) - required_keys),
        )
    if (
        evidence.get("evidence_contract")
        != GENERIC_ENTRY_RECONCILIATION_EVIDENCE_CONTRACT
        or evidence.get("final_entry_summary_checksum")
        != expected["summary_checksum"]
    ):
        reject("evidence_contract_or_checksum_mismatch")
    existing = expected["expected_existing_register_count"]
    resulting = expected["resulting_register_count"]
    if evidence.get("expected_existing_register_count") != existing:
        reject("expected_existing_register_count_mismatch")
    writer_stage = str(evidence.get("writer_stage") or "").strip()
    if writer_stage not in attempt_stages or writer_stage != attempt.current_stage:
        reject("writer_stage_mismatch", writer_stage=writer_stage)
    stage_index = attempt_stages.index(writer_stage)
    if action == "confirm_no_side_effect":
        if not (
            stage_index <= boundary_index
            and evidence.get("actual_register_count") == existing
            and evidence.get("actual_detail_count") == existing
            and evidence.get("actual_key_result_count") == existing
            and evidence.get("actual_proofed_count") == 0
        ):
            reject("no_side_effect_counts_or_stage_mismatch")
        return None
    remote_record_id = str(evidence.get("remote_record_id") or "").strip()
    if not (
        stage_index >= boundary_index
        and evidence.get("resulting_register_count") == resulting
        and evidence.get("actual_register_count") == resulting
        and evidence.get("actual_detail_count") == resulting
        and evidence.get("actual_key_result_count") == resulting
        and evidence.get("actual_proofed_count") == 0
        and remote_record_id
    ):
        reject("completed_counts_or_stage_mismatch")
    summary = operation.request_summary or {}
    return {
        "schema_version": 1,
        "receipt_type": "legacy_generic_check_record_entry_manual_reconciliation",
        "source": "manual_reconciliation",
        "operation_id": operation.id,
        "target_sample_number": summary.get("target_sample_number"),
        "remote_record_id": remote_record_id,
        "final_entry_summary_checksum": expected["summary_checksum"],
        "final_entry": {
            "expected_existing_register_count": existing,
            "resulting_register_count": resulting,
            "actual_register_count": evidence["actual_register_count"],
            "actual_detail_count": evidence["actual_detail_count"],
            "actual_key_result_count": evidence["actual_key_result_count"],
            "actual_proofed_count": 0,
            "writer_stage": writer_stage,
            "proofed": False,
        },
    }


def _validate_manual_reconciliation_evidence(
    operation: ExecutionExternalOperation,
    *,
    attempt: ExecutionExternalAttempt,
    action: str,
    evidence: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    """Validate an admin attestation's shape; no remote probe runs here."""
    checked_at = _reconciliation_checked_at(evidence)
    oldest_allowed = _aware_utc(now) - timedelta(
        minutes=settings.EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES
    )
    newest_allowed = _aware_utc(now) + timedelta(minutes=5)
    if checked_at < oldest_allowed or checked_at > newest_allowed:
        raise conflict(
            "external_reconciliation_evidence_stale",
            "只读对账证据已过期或时间异常，请重新核验",
            checked_at=checked_at.isoformat(),
        )

    if (
        _operation_type(operation)
        == LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION
    ):
        return _validate_final_entry_reconciliation_evidence(
            operation,
            attempt=attempt,
            action=action,
            evidence=evidence,
        )
    if _operation_type(operation) == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION:
        return _validate_generic_entry_reconciliation_evidence(
            operation,
            attempt=attempt,
            action=action,
            evidence=evidence,
        )

    if action == "confirm_completed":
        summary = operation.request_summary or {}
        files = summary.get("files")
        special_wool_image = _operation_type(operation) in {
            LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        }
        expected_sha256 = (
            files[0].get("content_sha256")
            if isinstance(files, list)
            and len(files) == 1
            and isinstance(files[0], dict)
            else None
        )
        valid = (
            evidence.get("exact_record_count") == 1
            and bool(str(evidence.get("remote_record_id") or "").strip())
            and evidence.get("business_fields_match") is True
            and evidence.get("inspector_match") is True
            and evidence.get("target_file_count") == 1
            and isinstance(expected_sha256, str)
            and (
                (
                    special_wool_image
                    and isinstance(
                        evidence.get("remote_file_sha256"), str
                    )
                    and _SHA256_RE.fullmatch(
                        evidence["remote_file_sha256"]
                    )
                    is not None
                )
                or (
                    not special_wool_image
                    and evidence.get("remote_file_sha256")
                    == expected_sha256
                )
            )
        )
    elif action == "confirm_no_side_effect":
        valid = (
            evidence.get("exact_record_count") == 0
            and evidence.get("contains_record_count") == 0
            and evidence.get("target_file_count") == 0
        )
    else:
        valid = False
    if not valid:
        raise ExecutionApiError(
            422,
            "external_reconciliation_evidence_incomplete",
            "对账证据不能证明所选结论，操作仍保持锁定",
        )
    if (
        action == "confirm_completed"
        and _operation_type(operation) in {
            LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
            LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
        }
    ):
        raw_receipt = evidence.get("receipt")
        if not isinstance(raw_receipt, dict) or not raw_receipt:
            raise ExecutionApiError(
                422,
                "external_reconciliation_receipt_required",
                "特纤图片上传确认完成时必须提交完整机器回执",
            )
        receipt = validate_external_receipt(operation, raw_receipt)
        main_record = receipt.get("main_record")
        server_file = receipt.get("server_file")
        receipt_record_id = (
            str(main_record.get("id") or "").strip()
            if isinstance(main_record, dict)
            else ""
        )
        receipt_file_sha256 = (
            server_file.get("content_sha256")
            if isinstance(server_file, dict)
            else None
        )
        if (
            evidence.get("remote_record_id") != receipt_record_id
            or evidence.get("remote_file_sha256")
            != receipt_file_sha256
        ):
            raise ExecutionApiError(
                422,
                "external_reconciliation_receipt_mismatch",
                "人工对账声明与特纤图片上传机器回执不一致",
            )
        return receipt
    return None


def reconcile_external_operation(
    db: Session,
    *,
    operation_id: str,
    actor: ExecutionUser,
    action: str,
    attempt_id: str,
    payload_checksum: str,
    confirmed_sample_number: str,
    note: str,
    evidence: dict[str, Any],
    now: datetime | None = None,
) -> tuple[ExecutionExternalOperation, bool]:
    """Resolve an unknown remote outcome without performing remote I/O."""

    if actor.role != "admin" or not actor.is_active:
        raise ExecutionApiError(
            403,
            "external_reconciliation_admin_required",
            "只有管理员可以处置旧系统待对账操作",
        )
    normalized_note = str(note or "").strip()
    if not normalized_note:
        raise ExecutionApiError(
            422,
            "external_reconciliation_note_required",
            "人工对账必须填写核验依据",
        )
    locator = (
        db.query(
            ExecutionExternalOperation.run_id,
            ExecutionExternalOperation.node_run_id,
            ExecutionExternalOperation.request_summary,
        )
        .filter(ExecutionExternalOperation.id == operation_id)
        .one_or_none()
    )
    if locator is None:
        raise not_found("外部操作预检单", operation_id)

    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.id == locator.run_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise not_found("外部操作预检单", operation_id)
    node_run = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == locator.node_run_id,
            ExecutionNodeRun.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if node_run is None:
        raise not_found("外部操作预检单", operation_id)

    target_sample_number = str(
        (locator.request_summary or {}).get("target_sample_number") or ""
    ).strip() or resolve_legacy_target_sample_number(run)
    remote_business_key = lock_legacy_remote_business_scope(
        db,
        sample_number=target_sample_number,
    )
    operation = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == operation_id,
            ExecutionExternalOperation.run_id == run.id,
            ExecutionExternalOperation.node_run_id == node_run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if operation is None:
        raise not_found("外部操作预检单", operation_id)
    latest_attempt = (
        db.query(ExecutionExternalAttempt)
        .filter(ExecutionExternalAttempt.operation_id == operation.id)
        .order_by(ExecutionExternalAttempt.attempt_no.desc())
        .populate_existing()
        .with_for_update()
        .first()
    )
    if latest_attempt is None:
        raise conflict(
            "external_reconciliation_attempt_missing",
            "待对账操作缺少执行尝试，不能人工处置",
            operation_id=operation.id,
        )

    if operation.connector_key != LEGACY_CONNECTOR_KEY:
        raise conflict(
            "external_reconciliation_connector_mismatch",
            "当前连接器不支持该人工对账契约",
        )
    if operation.remote_business_key != remote_business_key:
        raise conflict(
            "external_operation_run_changed",
            "预检单与当前流程运行不一致，请刷新后重试",
            operation_id=operation.id,
        )
    if payload_checksum != operation.payload_checksum:
        raise conflict(
            "external_operation_payload_changed",
            "预检内容已变化，请刷新并重新核对",
            operation_id=operation.id,
        )
    summary_target = str(
        (operation.request_summary or {}).get("target_sample_number") or ""
    )
    if (
        confirmed_sample_number != summary_target
        or summary_target != target_sample_number
    ):
        raise conflict(
            "external_operation_sample_confirmation_mismatch",
            "确认的样品编号与待对账预检单不一致",
            operation_id=operation.id,
        )
    if attempt_id != latest_attempt.id:
        raise conflict(
            "external_reconciliation_attempt_changed",
            "待对账执行尝试已变化，请刷新后重新核验",
            expected_attempt_id=latest_attempt.id,
        )

    current_time = now or utcnow()
    normalized_evidence = _normalized_reconciliation_evidence(evidence)
    evidence_checksum = _canonical_checksum(normalized_evidence)
    request_checksum = _canonical_checksum(
        {
            "action": action,
            "attempt_id": attempt_id,
            "payload_checksum": payload_checksum,
            "confirmed_sample_number": confirmed_sample_number,
            "note": normalized_note,
            "evidence": normalized_evidence,
        }
    )
    verification = dict(operation.verification or {})
    previous_reconciliation = verification.get("reconciliation")
    if isinstance(previous_reconciliation, dict):
        if (
            previous_reconciliation.get("request_checksum")
            == request_checksum
            and previous_reconciliation.get("attempt_id") == attempt_id
            and previous_reconciliation.get("action") == action
        ):
            return operation, True
        raise conflict(
            "external_reconciliation_already_resolved",
            "该外部操作已由另一份对账结论处置",
            operation_id=operation.id,
        )
    reconciled_receipt = _validate_manual_reconciliation_evidence(
        operation,
        attempt=latest_attempt,
        action=action,
        evidence=normalized_evidence,
        now=current_time,
    )

    if operation.status != "reconciliation_required":
        raise conflict(
            "external_reconciliation_not_required",
            "当前外部操作不处于待对账状态",
            operation_id=operation.id,
            status=operation.status,
        )
    if node_run.status != "waiting_external":
        raise conflict(
            "external_operation_node_not_waiting",
            "外部操作对应节点已不再等待人工对账",
            operation_id=operation.id,
            node_status=node_run.status,
        )
    if latest_attempt.status != "failed":
        raise conflict(
            "external_reconciliation_attempt_not_failed",
            "只有已失败且结果未知的执行尝试可以人工对账",
            attempt_id=latest_attempt.id,
            status=latest_attempt.status,
        )
    attempt_stages, write_boundary, _verified_stage = (
        _operation_stage_profile(operation)
    )
    if (
        latest_attempt.current_stage not in attempt_stages
        or attempt_stages.index(latest_attempt.current_stage)
        < attempt_stages.index(write_boundary)
    ):
        raise conflict(
            "external_reconciliation_prewrite_attempt",
            "该执行尝试尚未越过远端写入边界，不应人工释放围栏",
            attempt_id=latest_attempt.id,
            current_stage=latest_attempt.current_stage,
        )

    previous_status = operation.status
    remote_write_performed = action == "confirm_completed"
    reconciliation_record = {
        "schema_version": 1,
        "evidence_kind": "admin_attestation_v1",
        "action": action,
        "attempt_id": latest_attempt.id,
        "attempt_no": latest_attempt.attempt_no,
        "reconciled_by_id": actor.id,
        "reconciled_at": current_time.isoformat(),
        "note": normalized_note,
        "evidence": normalized_evidence,
        "evidence_checksum": evidence_checksum,
        "request_checksum": request_checksum,
        "remote_write_performed": remote_write_performed,
    }
    verification["reconciliation"] = reconciliation_record
    operation.verification = verification
    operation.fence_token = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.completed_at = current_time

    if action == "confirm_completed":
        from app.execution.engine import complete_external_node

        remote_record_id = str(
            normalized_evidence.get("remote_record_id") or ""
        ).strip()
        receipt = (
            reconciled_receipt
            if reconciled_receipt is not None
            else {
                "schema_version": 1,
                "source": "manual_reconciliation",
                "remote_record_id": remote_record_id,
                "target_sample_number": target_sample_number,
                "remote_file_sha256": normalized_evidence.get(
                    "remote_file_sha256"
                ),
                "evidence_checksum": evidence_checksum,
            }
        )
        operation.status = "completed"
        operation.remote_record_id = remote_record_id
        operation.receipt = receipt
        operation.error_code = None
        operation.error_message = None
        node_output = dict(node_run.output_data or {})
        node_output.update(
            {
                "operation_id": operation.id,
                "status": "completed",
                "receipt": receipt,
                "attempt_id": latest_attempt.id,
                "attempt_no": latest_attempt.attempt_no,
                "reconciliation": {
                    "action": action,
                    "evidence_checksum": evidence_checksum,
                },
            }
        )
        complete_external_node(
            db,
            node_run_id=node_run.id,
            output_data=node_output,
        )
    else:
        operation.remote_record_id = None
        operation.receipt = {}
        operation.error_code = "external_reconciliation_no_side_effect"
        operation.error_message = (
            "人工只读对账确认旧系统未产生记录或文件，本次运行已停止"
        )
        if run.status == "cancel_pending":
            from app.execution.engine import cancel_external_waiting_node

            operation.status = "cancelled"
            cancel_external_waiting_node(
                db,
                node_run_id=node_run.id,
                error_code="run_cancelled",
                error_message="流程已取消，人工对账确认旧系统未写入",
            )
        else:
            from app.execution.engine import fail_external_waiting_node

            operation.status = "failed"
            fail_external_waiting_node(
                db,
                node_run_id=node_run.id,
                error_code=operation.error_code,
                error_message=operation.error_message,
                actor_user_id=actor.id,
            )

    append_run_event(
        db,
        run_id=operation.run_id,
        event_type="external_operation.reconciled",
        actor_type="user",
        actor_id=actor.id,
        payload={
            "operation_id": operation.id,
            "attempt_id": latest_attempt.id,
            "attempt_no": latest_attempt.attempt_no,
            "action": action,
            "previous_status": previous_status,
            "status": operation.status,
            "remote_write_performed": remote_write_performed,
            "evidence_checksum": evidence_checksum,
            "remote_record_id": operation.remote_record_id,
        },
    )
    append_audit_log(
        db,
        action="external_operation.reconcile",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        actor_user_id=actor.id,
        details={
            "run_id": operation.run_id,
            "attempt_id": latest_attempt.id,
            "attempt_no": latest_attempt.attempt_no,
            "action": action,
            "previous_status": previous_status,
            "status": operation.status,
            "remote_write_performed": remote_write_performed,
            "evidence_checksum": evidence_checksum,
            "remote_record_id": operation.remote_record_id,
            "note": normalized_note,
            "evidence": normalized_evidence,
        },
    )
    return operation, False


def _stage_index(
    stage: str,
    operation: ExecutionExternalOperation | None = None,
) -> int:
    stages = (
        _operation_stage_profile(operation)[0]
        if operation is not None
        else EXTERNAL_ATTEMPT_STAGES
    )
    return stages.index(stage)


def _stage_before_remote_write(
    stage: str | None,
    operation: ExecutionExternalOperation | None = None,
) -> bool:
    stages, boundary, _verified = (
        _operation_stage_profile(operation)
        if operation is not None
        else (
            EXTERNAL_ATTEMPT_STAGES,
            EXTERNAL_REMOTE_WRITE_STAGE,
            "main_record_verified",
        )
    )
    if stage is None or stage not in stages:
        return True
    return stages.index(stage) < stages.index(boundary)


def _furthest_stage(
    *values: str | None,
    operation: ExecutionExternalOperation | None = None,
) -> str | None:
    stages = (
        _operation_stage_profile(operation)[0]
        if operation is not None
        else EXTERNAL_ATTEMPT_STAGES
    )
    known = [stage for stage in values if stage in stages]
    if not known:
        return None
    return max(known, key=stages.index)


def _validate_attempt_stage(
    stage: str,
    operation: ExecutionExternalOperation | None = None,
) -> str:
    normalized = stage.strip()
    stages = (
        _operation_stage_profile(operation)[0]
        if operation is not None
        else EXTERNAL_ATTEMPT_STAGES
    )
    if normalized not in stages:
        raise ExecutionApiError(
            422,
            "external_attempt_stage_invalid",
            "外部操作执行阶段不合法",
            details={"stage": stage},
        )
    return normalized


def _append_checkpoint(
    attempt: ExecutionExternalAttempt,
    *,
    operation: ExecutionExternalOperation | None = None,
    stage: str,
    at: datetime,
    detail: str | None = None,
) -> None:
    checkpoints = [
        dict(item)
        for item in (attempt.checkpoints or [])
        if isinstance(item, dict)
    ]
    entry: dict[str, Any] = {
        "stage": stage,
        "at": _aware_utc(at).isoformat(),
    }
    if detail:
        entry["detail"] = detail
    checkpoints.append(entry)
    attempt.checkpoints = checkpoints
    attempt.current_stage = _furthest_stage(
        attempt.current_stage,
        stage,
        operation=operation,
    )


def _append_stdout(existing: str | None, appended: str | None) -> str:
    if not appended:
        return existing or ""
    combined = (existing or "") + appended
    if len(combined) > BRIDGE_STDOUT_LIMIT:
        return combined[-BRIDGE_STDOUT_LIMIT:]
    return combined


def public_external_attempt(
    attempt: ExecutionExternalAttempt,
) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "operation_id": attempt.operation_id,
        "attempt_no": attempt.attempt_no,
        "bridge_id": attempt.bridge_id,
        "status": attempt.status,
        "current_stage": attempt.current_stage,
        "lease_expires_at": _isoformat(attempt.lease_expires_at),
        "checkpoints": [
            dict(item)
            for item in (attempt.checkpoints or [])
            if isinstance(item, dict)
        ],
        "stdout_summary": attempt.stdout_summary or "",
        "exit_code": attempt.exit_code,
        "error": (
            {
                "code": attempt.error_code,
                "message": attempt.error_message,
            }
            if attempt.error_code
            else None
        ),
        "started_at": _isoformat(attempt.started_at),
        "finished_at": _isoformat(attempt.finished_at),
        "created_at": attempt.created_at.isoformat(),
        "updated_at": attempt.updated_at.isoformat(),
    }


def bridge_external_operation(
    operation: ExecutionExternalOperation,
    *,
    credential: ExecutionCredential | None,
) -> dict[str, Any]:
    """Public view plus the Bridge-only credential account name.

    The Bridge channel never receives the credential id, revision or the
    encrypted secret; the account name is the only credential field a client
    needs to perform the legacy login.
    """

    view = public_external_operation(operation)
    view["credential"] = {
        "account_name": (
            credential.account_name if credential is not None else None
        ),
    }
    operation_type = _operation_type(operation)
    if operation_type in {
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    }:
        summary = operation.request_summary or {}
        # This duplicate is intentional: the authenticated Bridge channel and
        # Windows Writer both verify it against request_summary before starting
        # any remote side effect. Public operation views never receive it.
        view["machine_payload"] = {
            "schema_version": 1,
            "operation_type": operation_type,
            "target_sample_number": summary.get("target_sample_number"),
            "source_upload": dict(summary.get("source_operation") or {}),
        }
    if operation_type == (
        LEGACY_MICROSCOPY_CHECK_RECORD_ENTRY_OPERATION
    ):
        summary = operation.request_summary or {}
        bridge_summary = view.get("request_summary")
        if isinstance(bridge_summary, dict):
            bridge_summary["template_binding"] = dict(
                summary.get("template_binding") or {}
            )
            bridge_summary["source_review_operation"] = dict(
                summary.get("source_review_operation") or {}
            )
        # The flat writer package is returned only through the authenticated
        # Bridge claim endpoint; public operation views deliberately omit it.
        view["machine_payload"] = dict(
            summary.get("final_entry_package") or {}
        )
    if operation_type == LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION:
        summary = operation.request_summary or {}
        bridge_summary = view.get("request_summary")
        if isinstance(bridge_summary, dict):
            bridge_summary["source_review_operation"] = dict(
                summary.get("source_review_operation") or {}
            )
            bridge_summary["result_contract"] = dict(
                summary.get("result_contract") or {}
            )
        view["machine_payload"] = dict(
            summary.get("final_entry_package") or {}
        )
    return view


def ensure_bridge_credential_account(
    credential: ExecutionCredential,
    *,
    account_name: str,
) -> None:
    """Fail closed unless a returned credential belongs to the claimant."""

    if _account_scope_key(credential.account_name or "") != (
        _account_scope_key(account_name)
    ):
        raise conflict(
            "external_bridge_account_mismatch",
            "Bridge 账号与待领取操作绑定的凭据账号不匹配",
        )


def claim_approved_external_operation(
    db: Session,
    *,
    bridge_id: str,
    account_name: str,
    supported_operation_types: set[str] | None = None,
    now: datetime | None = None,
) -> (
    tuple[
        ExecutionExternalOperation,
        ExecutionExternalAttempt,
        ExecutionCredential,
    ]
    | None
):
    """Claim the oldest approved operation for a Bridge client.

    Returns None when nothing is claimable; expired manual approvals are
    skipped and left to regular expiry maintenance.  Automatic approvals have
    no countdown (``approval_expires_at IS NULL``), while every selected
    operation still passes credential-revision and source-content
    re-verification before any state flips.
    """

    current_time = now or utcnow()
    supported_types = supported_operation_types or {
        LEGACY_REGENERATED_COUNT_OPERATION
    }
    account_scope_key = _account_scope_key(account_name)
    lock_external_bridge_claim_capacity(db)
    active_write_count = (
        db.query(ExecutionExternalOperation.id)
        .filter(
            ExecutionExternalOperation.connector_key
            == LEGACY_CONNECTOR_KEY,
            ExecutionExternalOperation.status.in_(
                BRIDGE_ACTIVE_WRITE_STATUSES
            )
        )
        .count()
    )
    if active_write_count >= BRIDGE_GLOBAL_WRITE_CAPACITY:
        return None
    candidates = (
        db.query(ExecutionExternalOperation.id)
        .filter(
            ExecutionExternalOperation.connector_key
            == LEGACY_CONNECTOR_KEY,
            ExecutionExternalOperation.status == "approved",
            ExecutionExternalOperation.account_scope_key
            == account_scope_key,
            or_(
                ExecutionExternalOperation.approval_expires_at.is_(None),
                ExecutionExternalOperation.approval_expires_at
                > current_time,
            ),
        )
        .order_by(
            ExecutionExternalOperation.created_at.asc(),
            ExecutionExternalOperation.id.asc(),
        )
        .limit(20)
        .all()
    )
    for (operation_id,) in candidates:
        locator = (
            db.query(
                ExecutionExternalOperation.run_id,
                ExecutionExternalOperation.node_run_id,
            )
            .filter(ExecutionExternalOperation.id == operation_id)
            .one_or_none()
        )
        if locator is None:
            continue
        run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.id == locator.run_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if run is None:
            continue
        node_run = (
            db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.id == locator.node_run_id,
                ExecutionNodeRun.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        operation = (
            db.query(ExecutionExternalOperation)
            .filter(
                ExecutionExternalOperation.id == operation_id,
                ExecutionExternalOperation.run_id == run.id,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if operation is None or operation.status != "approved":
            continue
        if (
            _operation_type(operation) not in supported_types
            or _operation_execution_capability(operation).get("available")
            is False
        ):
            continue
        if node_run is None or node_run.status != "waiting_external":
            raise conflict(
                "external_operation_node_not_waiting",
                "外部操作对应节点已不再等待连接器处理",
                operation_id=operation.id,
            )
        _ensure_approval_not_expired(operation, now=current_time)
        credential = _bound_credential_for_approval(
            db,
            operation=operation,
            run=run,
        )
        ensure_bridge_credential_account(
            credential,
            account_name=account_name,
        )
        _reverify_operation_sources(db, operation=operation)

        lease_expires_at = current_time + timedelta(
            seconds=BRIDGE_LEASE_SECONDS
        )
        attempt_no = int(operation.attempt_count or 0) + 1
        operation.status = "in_progress"
        operation.attempt_count = attempt_no
        operation.lease_owner = bridge_id
        operation.lease_expires_at = lease_expires_at
        if operation.started_at is None:
            operation.started_at = current_time
        attempt = ExecutionExternalAttempt(
            operation_id=operation.id,
            attempt_no=attempt_no,
            bridge_id=bridge_id,
            status="in_progress",
            lease_expires_at=lease_expires_at,
            started_at=current_time,
        )
        db.add(attempt)
        db.flush()
        output = dict(node_run.output_data or {})
        output["status"] = "in_progress"
        output["attempt_id"] = attempt.id
        output["attempt_no"] = attempt_no
        node_run.output_data = output
        append_run_event(
            db,
            run_id=operation.run_id,
            event_type="external_operation.claimed",
            actor_type="bridge",
            actor_id=bridge_id,
            payload={
                "operation_id": operation.id,
                "node_id": node_run.node_id,
                "attempt_id": attempt.id,
                "attempt_no": attempt_no,
                "bridge_id": bridge_id,
                "status": operation.status,
                "lease_expires_at": lease_expires_at.isoformat(),
                "remote_write_performed": False,
            },
        )
        append_audit_log(
            db,
            action="external_operation.claim",
            resource_type="execution_external_operation",
            resource_id=operation.id,
            details={
                "run_id": operation.run_id,
                "attempt_id": attempt.id,
                "attempt_no": attempt_no,
                "bridge_id": bridge_id,
                "remote_write_performed": False,
            },
        )
        # Keep the account assertion adjacent to the returned credential as a
        # final defense if claim construction changes in the future.
        ensure_bridge_credential_account(
            credential,
            account_name=account_name,
        )
        return operation, attempt, credential
    return None


def _attempt_for_bridge(
    db: Session,
    *,
    attempt_id: str,
    bridge_id: str,
) -> tuple[
    ExecutionRun,
    ExecutionNodeRun,
    ExecutionExternalOperation,
    ExecutionExternalAttempt,
]:
    """Lock an attempt in the engine-wide run -> node -> operation order."""

    locator = (
        db.query(
            ExecutionExternalOperation.id,
            ExecutionExternalOperation.run_id,
            ExecutionExternalOperation.node_run_id,
        )
        .join(
            ExecutionExternalAttempt,
            ExecutionExternalAttempt.operation_id
            == ExecutionExternalOperation.id,
        )
        .filter(ExecutionExternalAttempt.id == attempt_id)
        .one_or_none()
    )
    if locator is None:
        raise not_found("外部操作执行尝试", attempt_id)
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.id == locator.run_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise not_found("外部操作执行尝试", attempt_id)
    node_run = (
        db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.id == locator.node_run_id,
            ExecutionNodeRun.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    operation = (
        db.query(ExecutionExternalOperation)
        .filter(
            ExecutionExternalOperation.id == locator.id,
            ExecutionExternalOperation.run_id == run.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    attempt = (
        db.query(ExecutionExternalAttempt)
        .filter(
            ExecutionExternalAttempt.id == attempt_id,
            ExecutionExternalAttempt.operation_id == locator.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if node_run is None or operation is None or attempt is None:
        raise not_found("外部操作执行尝试", attempt_id)
    if attempt.bridge_id != bridge_id:
        # 不透露其它 Bridge 的尝试是否存在。
        raise not_found("外部操作执行尝试", attempt_id)
    return run, node_run, operation, attempt


def _ensure_attempt_active(
    operation: ExecutionExternalOperation,
    attempt: ExecutionExternalAttempt,
    *,
    now: datetime,
) -> None:
    if attempt.status not in {"claimed", "in_progress"}:
        raise conflict(
            "external_attempt_not_active",
            "该外部操作执行尝试已结束，不能继续上报",
            attempt_id=attempt.id,
            status=attempt.status,
        )
    if operation.status not in {"in_progress", "cancel_pending"}:
        raise conflict(
            "external_operation_not_in_progress",
            "外部操作当前状态不允许继续执行",
            operation_id=operation.id,
            status=operation.status,
        )
    if attempt.lease_expires_at is None or _aware_utc(
        attempt.lease_expires_at
    ) <= _aware_utc(now):
        raise conflict(
            "external_attempt_lease_expired",
            "Bridge 租约已过期，该尝试将由系统回收，请重新领取",
            attempt_id=attempt.id,
        )


def _ensure_stage_transition_allowed(
    operation: ExecutionExternalOperation,
    attempt: ExecutionExternalAttempt,
    *,
    stage: str,
) -> None:
    if (
        operation.status == "cancel_pending"
        and _stage_before_remote_write(attempt.current_stage, operation)
        and not _stage_before_remote_write(stage, operation)
    ):
        raise conflict(
            "external_attempt_cancelled_before_remote_write",
            "流程已请求取消，Bridge 不得开始写入旧系统",
            operation_id=operation.id,
            attempt_id=attempt.id,
            current_stage=attempt.current_stage,
            requested_stage=stage,
        )


def heartbeat_external_attempt(
    db: Session,
    *,
    attempt_id: str,
    bridge_id: str,
    stage: str | None = None,
    stdout_append: str | None = None,
    now: datetime | None = None,
) -> tuple[ExecutionExternalAttempt, ExecutionExternalOperation, bool]:
    current_time = now or utcnow()
    _run, _node_run, operation, attempt = _attempt_for_bridge(
        db,
        attempt_id=attempt_id,
        bridge_id=bridge_id,
    )
    _ensure_attempt_active(operation, attempt, now=current_time)
    validated_stage = None
    if stage is not None:
        validated_stage = _validate_attempt_stage(stage, operation)
        _ensure_stage_transition_allowed(
            operation,
            attempt,
            stage=validated_stage,
        )
    lease_expires_at = current_time + timedelta(seconds=BRIDGE_LEASE_SECONDS)
    attempt.lease_expires_at = lease_expires_at
    operation.lease_expires_at = lease_expires_at
    if validated_stage is not None:
        _append_checkpoint(
            attempt,
            operation=operation,
            stage=validated_stage,
            at=current_time,
        )
    attempt.stdout_summary = _append_stdout(
        attempt.stdout_summary,
        stdout_append,
    )
    return attempt, operation, operation.status == "cancel_pending"


def record_external_attempt_stage(
    db: Session,
    *,
    attempt_id: str,
    bridge_id: str,
    stage: str,
    detail: str | None = None,
    now: datetime | None = None,
) -> tuple[ExecutionExternalAttempt, ExecutionExternalOperation]:
    current_time = now or utcnow()
    _run, _node_run, operation, attempt = _attempt_for_bridge(
        db,
        attempt_id=attempt_id,
        bridge_id=bridge_id,
    )
    _ensure_attempt_active(operation, attempt, now=current_time)
    validated_stage = _validate_attempt_stage(stage, operation)
    _ensure_stage_transition_allowed(
        operation,
        attempt,
        stage=validated_stage,
    )
    _append_checkpoint(
        attempt,
        operation=operation,
        stage=validated_stage,
        at=current_time,
        detail=detail,
    )
    lease_expires_at = current_time + timedelta(seconds=BRIDGE_LEASE_SECONDS)
    attempt.lease_expires_at = lease_expires_at
    operation.lease_expires_at = lease_expires_at
    return attempt, operation


def complete_external_attempt(
    db: Session,
    *,
    attempt_id: str,
    bridge_id: str,
    receipt: dict[str, Any],
    stdout_summary: str | None = None,
    now: datetime | None = None,
) -> tuple[ExecutionExternalOperation, ExecutionExternalAttempt]:
    from app.execution.engine import complete_external_node

    current_time = now or utcnow()
    _run, node_run, operation, attempt = _attempt_for_bridge(
        db,
        attempt_id=attempt_id,
        bridge_id=bridge_id,
    )
    _ensure_attempt_active(operation, attempt, now=current_time)
    receipt = validate_external_receipt(operation, receipt)
    attempt_stages, _write_boundary, verified_stage = (
        _operation_stage_profile(operation)
    )
    record_verified = (
        attempt.current_stage in attempt_stages
        and attempt_stages.index(attempt.current_stage)
        >= attempt_stages.index(verified_stage)
    )
    if not record_verified:
        raise conflict(
            "external_attempt_not_verified",
            "主单记录尚未核验完成，不能结束本次外部操作",
            attempt_id=attempt.id,
            current_stage=attempt.current_stage,
        )

    attempt.status = "completed"
    attempt.exit_code = 0
    attempt.finished_at = current_time
    attempt.lease_expires_at = None
    if stdout_summary is not None:
        attempt.stdout_summary = stdout_summary[-BRIDGE_STDOUT_LIMIT:]
    operation.status = "completed"
    operation.receipt = receipt
    operation.error_code = None
    operation.error_message = None
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.completed_at = current_time

    output = dict(node_run.output_data or {})
    output["operation_id"] = operation.id
    output["status"] = "completed"
    output["receipt"] = receipt
    output["attempt_id"] = attempt.id
    output["attempt_no"] = attempt.attempt_no
    complete_external_node(
        db,
        node_run_id=node_run.id,
        output_data=output,
    )
    append_run_event(
        db,
        run_id=operation.run_id,
        event_type="external_operation.completed",
        actor_type="bridge",
        actor_id=bridge_id,
        payload={
            "operation_id": operation.id,
            "node_id": node_run.node_id,
            "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "status": operation.status,
            "remote_write_performed": True,
        },
    )
    append_audit_log(
        db,
        action="external_operation.complete",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        details={
            "run_id": operation.run_id,
            "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "bridge_id": bridge_id,
            "remote_write_performed": True,
        },
    )
    return operation, attempt


def settle_external_attempt_failure(
    db: Session,
    *,
    node_run: ExecutionNodeRun | None,
    operation: ExecutionExternalOperation,
    attempt: ExecutionExternalAttempt,
    settling_run_status: str,
    stage: str | None,
    error_code: str | None,
    error_message: str | None,
    now: datetime,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> None:
    """Settle an operation whose attempt failed at the given stage.

    A failure before file_copy_started provably never touched the legacy
    system: the operation becomes claimable again, or finishes a pending
    cancellation.  Anything later may have left a remote side effect, so the
    operation keeps its business fence until manual reconciliation.
    """

    operation.lease_owner = None
    operation.lease_expires_at = None
    reclaimed = _stage_before_remote_write(stage, operation)
    remote_write_performed: bool | None = False if reclaimed else None
    settlement_reason: str | None = None
    if reclaimed and operation.status == "cancel_pending":
        from app.execution.engine import cancel_external_waiting_node

        operation.status = "cancelled"
        failed_while_settling = settling_run_status == "failure_pending"
        settlement_reason = (
            "run_failed" if failed_while_settling else "run_cancelled"
        )
        operation.error_code = settlement_reason
        operation.error_message = (
            "同一流程已有节点失败，外部操作确认未写入旧系统"
            if failed_while_settling
            else "流程已取消，外部操作未写入旧系统"
        )
        operation.completed_at = now
        if node_run is not None and node_run.status == "waiting_external":
            cancel_external_waiting_node(
                db,
                node_run_id=node_run.id,
                error_code=settlement_reason,
                error_message=operation.error_message,
            )
    elif reclaimed:
        db.flush()
        failed_attempts = (
            db.query(ExecutionExternalAttempt)
            .filter(
                ExecutionExternalAttempt.operation_id == operation.id,
                ExecutionExternalAttempt.status == "failed",
            )
            .count()
        )
        if failed_attempts >= settings.EXECUTION_EXTERNAL_MAX_ATTEMPTS:
            # 确定性预检失败（如 target_allocation_stale）无限重领只会
            # 反复冲击旧系统：达到上限后按“未写入”失败收尾，节点随 run
            # 进入可人工重试状态，重试会以最新快照重新预检。
            from app.execution.engine import fail_external_waiting_node

            operation.status = "failed"
            operation.error_code = error_code or "external_attempts_exhausted"
            operation.error_message = (
                "外部操作已连续 {count} 次在执行前失败（最近一次：{reason}），"
                "已停止自动重试；请确认旧系统与任务信息状态后重试节点".format(
                    count=failed_attempts,
                    reason=error_code or error_message or "未知错误",
                )
            )
            operation.completed_at = now
            settlement_reason = "attempts_exhausted"
            if node_run is not None and node_run.status == "waiting_external":
                fail_external_waiting_node(
                    db,
                    node_run_id=node_run.id,
                    error_code="external_attempts_exhausted",
                    error_message=operation.error_message,
                )
        else:
            # 批准 TTL 不延长；过期后由 expire_stale_external_operations 收尾。
            operation.status = "approved"
    else:
        operation.status = "reconciliation_required"
        operation.error_code = error_code
        operation.error_message = (
            error_message or "外部操作执行失败，可能已写入旧系统，需要人工对账"
        )
    append_run_event(
        db,
        run_id=operation.run_id,
        event_type="external_operation.attempt_failed",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={
            "operation_id": operation.id,
            "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "stage": stage,
            "code": error_code,
            "message": error_message,
            "settlement_reason": settlement_reason,
            "status": operation.status,
            "reconciliation_required": (
                operation.status == "reconciliation_required"
            ),
            "remote_write_performed": remote_write_performed,
        },
    )
    append_audit_log(
        db,
        action="external_operation.fail_attempt",
        resource_type="execution_external_operation",
        resource_id=operation.id,
        details={
            "run_id": operation.run_id,
            "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "bridge_id": attempt.bridge_id,
            "stage": stage,
            "code": error_code,
            "settlement_reason": settlement_reason,
            "status": operation.status,
            "remote_write_performed": remote_write_performed,
        },
    )


def fail_external_attempt(
    db: Session,
    *,
    attempt_id: str,
    bridge_id: str,
    stage: str,
    error_code: str | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> tuple[ExecutionExternalOperation, ExecutionExternalAttempt]:
    current_time = now or utcnow()
    run, node_run, operation, attempt = _attempt_for_bridge(
        db,
        attempt_id=attempt_id,
        bridge_id=bridge_id,
    )
    _ensure_attempt_active(operation, attempt, now=current_time)
    reported_stage = _validate_attempt_stage(stage, operation)
    # 失败定位以上报过的最远阶段为准，避免失败上报把操作倒退回可重领区间。
    effective_stage = _furthest_stage(
        attempt.current_stage,
        reported_stage,
        operation=operation,
    )
    _append_checkpoint(
        attempt,
        operation=operation,
        stage=reported_stage,
        at=current_time,
        detail=message or error_code,
    )
    attempt.current_stage = effective_stage
    attempt.status = "failed"
    attempt.error_code = error_code
    attempt.error_message = message
    attempt.finished_at = current_time
    attempt.lease_expires_at = None
    settle_external_attempt_failure(
        db,
        node_run=node_run,
        operation=operation,
        attempt=attempt,
        settling_run_status=run.status,
        stage=effective_stage,
        error_code=error_code,
        error_message=message,
        now=current_time,
        actor_type="bridge",
        actor_id=bridge_id,
    )
    return operation, attempt
