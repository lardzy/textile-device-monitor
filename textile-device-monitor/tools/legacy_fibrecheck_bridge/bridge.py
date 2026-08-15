#!/usr/bin/env python3
"""旧检务系统集中式 Bridge（单任务版）。

职责：向执行系统领取 approved 的外部操作 -> 启动本地 Runner 写入 -> 按阶段
回报心跳/检查点 -> 完成或失败收尾。除 Runner 自身动作外不产生任何远端副作用。

用法（在能同时访问执行系统 API、旧 Oracle 与共享目录的 Windows 主机上）：

    python bridge.py \
        --api-base http://127.0.0.1:8000/api/execution/v1 \
        --bridge-id paralllels-win11-01 \
        --token-env EXECUTION_BRIDGE_TOKEN \
        --writer C:/path/to/FibreCheckWriter.exe \
        --fibrecheck-dir C:/path/to/FibreCheck \
        --source-root regenerated_fiber_records=//192.168.105.82/材料检测中心/10特纤/02-检验/2026-再生纤 \
        --account-env FIBRECHECK_RUNNER_ACCOUNT \
        --password-env FIBRECHECK_RUNNER_PASSWORD \
        --once
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request

SIDE_EFFECT_STAGE = "file_copy_started"
SIDE_EFFECT_READY_STAGE = "file_copy_ready"
SIDE_EFFECT_PERMIT = "PERMIT_REMOTE_WRITE"
LEGACY_REGENERATED_COUNT_OPERATION = "legacy_regenerated_fiber_count_upload"
LEGACY_SPECIAL_WOOL_IMAGE_OPERATION = "legacy_special_wool_image_upload"
LEGACY_SPECIAL_WOOL_REVIEW_OPERATION = "legacy_special_wool_review"
LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION = "legacy_microscopy_check_record_entry"
LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION = (
    "legacy_special_wool_qualitative_upload"
)
LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION = (
    "legacy_special_wool_qualitative_review"
)
LEGACY_GENERIC_FINAL_ENTRY_OPERATION = "legacy_generic_check_record_entry"
# 电镜 Excel 登记路线已证明的任务项目（编号, 名称, 测试方法）三元组：
# 5103.5 / 纤维微观形貌（26A045793）与 5103.426 / 纤维横截面（260191285）。
SUPPORTED_EXCEL_PROJECTS = frozenset(
    {
        ("5103.5", "纤维微观形貌", "GB/T 36422-2018"),
        ("5103.426", "纤维横截面", "GB/T 36422-2018"),
    }
)
SUPPORTED_OPERATION_TYPES = (
    LEGACY_REGENERATED_COUNT_OPERATION,
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
)
PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_absence_verified",
    SIDE_EFFECT_READY_STAGE,
    SIDE_EFFECT_STAGE,
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "completed",
)
REVIEW_PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "review_save_ready",
    "review_save_started",
    "review_main_verified",
    "review_children_verified",
    "completed",
)
FINAL_ENTRY_PROGRESS_STAGES = (
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
IMAGE_PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "task_project_verified",
    SIDE_EFFECT_READY_STAGE,
    SIDE_EFFECT_STAGE,
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "picture_child_verified",
    "completed",
)
QUALITATIVE_UPLOAD_PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "task_project_verified",
    SIDE_EFFECT_READY_STAGE,
    SIDE_EFFECT_STAGE,
    "file_copy_verified",
    "main_record_save_started",
    "main_record_verified",
    "completed",
)
QUALITATIVE_REVIEW_PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "review_save_ready",
    "review_save_started",
    "review_main_verified",
    "completed",
)
GENERIC_FINAL_ENTRY_PROGRESS_STAGES = (
    "authenticated",
    "permission_verified",
    "remote_state_verified",
    "generic_write_ready",
    "generic_save_started",
    "generic_rows_verified",
    "generic_projection_verified",
    "completed",
)
OPERATION_STAGE_PROFILES = {
    LEGACY_REGENERATED_COUNT_OPERATION: (
        PROGRESS_STAGES,
        "file_copy_ready",
        "file_copy_started",
    ),
    LEGACY_SPECIAL_WOOL_IMAGE_OPERATION: (
        IMAGE_PROGRESS_STAGES,
        "file_copy_ready",
        "file_copy_started",
    ),
    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION: (
        REVIEW_PROGRESS_STAGES,
        "review_save_ready",
        "review_save_started",
    ),
    LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION: (
        FINAL_ENTRY_PROGRESS_STAGES,
        "excel_write_ready",
        "excel_collection_started",
    ),
    LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION: (
        QUALITATIVE_UPLOAD_PROGRESS_STAGES,
        "file_copy_ready",
        "file_copy_started",
    ),
    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION: (
        QUALITATIVE_REVIEW_PROGRESS_STAGES,
        "review_save_ready",
        "review_save_started",
    ),
    LEGACY_GENERIC_FINAL_ENTRY_OPERATION: (
        GENERIC_FINAL_ENTRY_PROGRESS_STAGES,
        "generic_write_ready",
        "generic_save_started",
    ),
}

FINAL_ENTRY_RAW_STAGE_MAP = {
    "authenticated": "authenticated",
    "function_permission_verified": "permission_verified",
    "remote_preflight_verified": "remote_state_verified",
    "remote_write_ready": "excel_write_ready",
    "excel_collection_started": "excel_collection_started",
    "remote_file_verified": "remote_file_verified",
    "excel_register_saved_and_verified": "excel_register_verified",
    "excel_proof_verified": "excel_proof_verified",
    "completed": "completed",
}
FINAL_ENTRY_IGNORED_RAW_STAGES = {
    "package_validated",
    "workbook_verified",
    "side_effect_permit_accepted",
    "project_write_lock_acquired",
    "remote_state_revalidated",
    "staging_file_verified",
    "remote_file_copy_started",
    "excel_register_save_started",
    "excel_proof_save_started",
}
FINAL_ENTRY_RAW_SUCCESS_STAGES = (
    "package_validated",
    "workbook_verified",
    "authenticated",
    "function_permission_verified",
    "remote_preflight_verified",
    "remote_write_ready",
    "side_effect_permit_accepted",
    "project_write_lock_acquired",
    "remote_state_revalidated",
    "staging_file_verified",
    "excel_collection_started",
    "remote_file_copy_started",
    "remote_file_verified",
    "excel_register_save_started",
    "excel_register_saved_and_verified",
    "excel_proof_save_started",
    "excel_proof_verified",
    "completed",
)
GENERIC_FINAL_ENTRY_RAW_STAGE_MAP = {
    "authenticated": "authenticated",
    "function_permission_verified": "permission_verified",
    "remote_preflight_verified": "remote_state_verified",
    "remote_write_ready": "generic_write_ready",
    "generic_save_started": "generic_save_started",
    "generic_rows_saved": "generic_rows_verified",
    "generic_readback_verified": "generic_projection_verified",
    "completed": "completed",
}
GENERIC_FINAL_ENTRY_IGNORED_RAW_STAGES = {
    "package_validated",
    "generic_header_validated",
    "generic_details_validated",
    "side_effect_permit_accepted",
    "project_write_lock_acquired",
    "remote_state_revalidated",
    "generic_projection_started",
}
GENERIC_FINAL_ENTRY_RAW_SUCCESS_STAGES = (
    "package_validated",
    "generic_header_validated",
    "generic_details_validated",
    "authenticated",
    "function_permission_verified",
    "remote_preflight_verified",
    "remote_write_ready",
    "side_effect_permit_accepted",
    "project_write_lock_acquired",
    "remote_state_revalidated",
    "generic_save_started",
    "generic_rows_saved",
    "generic_projection_started",
    "generic_readback_verified",
    "completed",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REDACTED_ID_RE = re.compile(r"^sha256:[0-9a-f]{16}$")
_TASK_PROJECT_KEY_RE = re.compile(r"^task-project:[0-9a-f]{24}$")
_FINAL_ENTRY_FILENAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.xls$"
)


class BridgeError(Exception):
    pass


class WriterEventDecoder:
    """Decode the Writer's sequence of top-level JSON objects.

    The frozen .NET ``MiniJson`` formatter emits one logical event as a
    pretty-printed, multi-line JSON object.  Plain line-by-line ``json.loads``
    therefore misses the side-effect-ready event and leaves the Writer blocked
    on stdin.  This decoder keeps an incomplete object across stdout lines,
    while ignoring ordinary log lines only when no JSON object is in flight.
    """

    MAX_BUFFER_CHARS = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, text: str) -> list[dict]:
        if not isinstance(text, str):
            raise BridgeError("Writer stdout 必须是文本")
        if not self._buffer:
            candidate = text.lstrip()
            if not candidate or not candidate.startswith("{"):
                return []
            self._buffer = candidate
        else:
            self._buffer += text

        if len(self._buffer) > self.MAX_BUFFER_CHARS:
            raise BridgeError("Writer JSON 事件超过允许大小")

        events: list[dict] = []
        while True:
            candidate = self._buffer.lstrip()
            if not candidate:
                self._buffer = ""
                return events
            if not candidate.startswith("{"):
                raise BridgeError("Writer JSON 事件后包含非法输出")
            try:
                event, end = self._decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                self._buffer = candidate
                return events
            if not isinstance(event, dict):
                raise BridgeError("Writer JSON 顶层事件必须是对象")
            events.append(event)
            self._buffer = candidate[end:]

    def finish(self) -> None:
        if self._buffer.strip():
            raise BridgeError("Writer stdout 包含未完成的 JSON 事件")


def api_request(api_base: str, token: str, method: str, path: str, payload: dict) -> dict:
    url = api_base.rstrip("/") + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Execution-Bridge-Key": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise BridgeError(f"API {path} -> {exc.code}: {body}") from exc


def parse_root_map(pairs: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise BridgeError(f"--source-root 需要 root_id=路径 形式: {pair}")
        key, value = pair.split("=", 1)
        mapping[key.strip()] = value.strip()
    return mapping


def normalized_identity(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def configured_operation_types(args) -> tuple[str, ...]:
    configured = list(SUPPORTED_OPERATION_TYPES)
    if getattr(args, "final_entry_writer", None) and getattr(
        args, "final_entry_work_root", None
    ):
        configured.append(LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION)
        configured.append(LEGACY_GENERIC_FINAL_ENTRY_OPERATION)
    return tuple(configured)


def _strict_map(
    value,
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise BridgeError(f"{path} 必须是对象")
    allowed = required | (optional or set())
    if set(value) != allowed and not (
        optional is not None
        and required <= set(value)
        and set(value) <= allowed
    ):
        raise BridgeError(f"{path} 字段集合不符合已发布契约")
    return value


def _required_text(value, *, path: str, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{path} 缺少必要文本")
    normalized = value.strip()
    if pattern is not None and not pattern.fullmatch(normalized):
        raise BridgeError(f"{path} 文本格式不正确")
    return normalized


def _required_int(value, *, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BridgeError(f"{path} 必须是非负整数")
    return value


def validate_special_wool_review_machine_payload(
    operation: dict,
    summary: dict,
) -> dict:
    """Bind review to the exact main row proved by the upload receipt."""

    source = _strict_map(
        summary.get("source_operation"),
        path="request_summary.source_operation",
        required={
            "operation_id",
            "payload_checksum",
            "receipt_checksum",
            "main_id",
        },
    )
    _required_text(
        source.get("operation_id"),
        path="request_summary.source_operation.operation_id",
    )
    for key in ("payload_checksum", "receipt_checksum"):
        _required_text(
            source.get(key),
            path=f"request_summary.source_operation.{key}",
            pattern=_SHA256_RE,
        )
    _required_text(
        source.get("main_id"),
        path="request_summary.source_operation.main_id",
        pattern=_REDACTED_ID_RE,
    )
    payload = _strict_map(
        operation.get("machine_payload"),
        path="machine_payload",
        required={
            "schema_version",
            "operation_type",
            "target_sample_number",
            "source_upload",
        },
    )
    operation_type = summary.get("operation_type")
    if operation_type not in {
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    }:
        raise BridgeError("SpecialWool review operation_type 不受支持")
    if payload.get("schema_version") != 1 or payload.get(
        "operation_type"
    ) != operation_type:
        raise BridgeError("SpecialWool review machine_payload 类型或版本不正确")
    if payload.get("target_sample_number") != summary.get(
        "target_sample_number"
    ):
        raise BridgeError("SpecialWool review machine_payload 目标编号不一致")
    machine_source = _strict_map(
        payload.get("source_upload"),
        path="machine_payload.source_upload",
        required=set(source),
    )
    if machine_source != source:
        raise BridgeError("SpecialWool review machine_payload 主记录绑定不一致")
    return source


def validate_special_wool_review_receipt(
    operation: dict,
    summary: dict,
    receipt: dict,
) -> dict:
    source = validate_special_wool_review_machine_payload(operation, summary)
    document = _strict_map(
        receipt,
        path="receipt",
        required={
            "schema_version",
            "receipt_type",
            "operation_id",
            "payload_checksum",
            "target_sample_number",
            "source_upload",
            "main_record",
            "children",
            "readback",
            "stages",
            "reconciliation_required",
        },
    )
    operation_type = summary.get("operation_type")
    if document.get("schema_version") != 1 or document.get(
        "receipt_type"
    ) != operation_type:
        raise BridgeError("SpecialWool review raw receipt 类型或版本不正确")
    for key, expected in {
        "operation_id": operation.get("id"),
        "payload_checksum": operation.get("payload_checksum"),
        "target_sample_number": summary.get("target_sample_number"),
    }.items():
        if document.get(key) != expected:
            raise BridgeError(f"SpecialWool review raw receipt 字段 {key} 不一致")
    if document.get("reconciliation_required") is not False:
        raise BridgeError("SpecialWool review raw receipt 仍需人工对账")
    receipt_source = _strict_map(
        document.get("source_upload"),
        path="receipt.source_upload",
        required={"operation_id", "receipt_checksum", "main_id"},
    )
    for key in ("operation_id", "receipt_checksum", "main_id"):
        if receipt_source.get(key) != source.get(key):
            raise BridgeError(
                f"SpecialWool review raw receipt 来源字段 {key} 不一致"
            )
    main = _strict_map(
        document.get("main_record"),
        path="receipt.main_record",
        required={
            "id",
            "review_user",
            "review_time",
            "pre_fingerprint",
            "post_fingerprint",
        },
    )
    if main.get("id") != source.get("main_id"):
        raise BridgeError("SpecialWool review raw receipt 主记录 ID 不一致")
    _required_text(
        main.get("id"), path="receipt.main_record.id", pattern=_REDACTED_ID_RE
    )
    stages = document.get("stages")
    if not isinstance(stages, list):
        raise BridgeError("SpecialWool review raw receipt 缺少阶段列表")
    stage_names = [
        item.get("stage") if isinstance(item, dict) else item for item in stages
    ]
    expected_stages = (
        QUALITATIVE_REVIEW_PROGRESS_STAGES
        if operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION
        else REVIEW_PROGRESS_STAGES
    )
    if stage_names != list(expected_stages):
        raise BridgeError("SpecialWool review raw receipt 阶段不完整或顺序错误")
    if operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION:
        children = document.get("children")
        if not isinstance(children, dict) or children.get("picture_count") != 0:
            raise BridgeError("文档型 SpecialWool 复核必须确认图片子记录为 0")
    return document


def validate_special_wool_qualitative_upload_receipt(
    operation: dict,
    summary: dict,
    receipt: dict,
) -> dict:
    document = _strict_map(
        receipt,
        path="receipt",
        required={
            "schema_version",
            "receipt_type",
            "operation_id",
            "payload_checksum",
            "target_sample_number",
            "target_filename",
            "source_artifact",
            "task_project",
            "server_file",
            "main_record",
            "picture_count",
            "readback",
            "stages",
            "reconciliation_required",
        },
        # 顺号改写：Writer 按旧系统锁内实况取第一空闲号写入时，
        # 回执用 requested_sample_number 绑定预检单。
        optional={"requested_sample_number", "renumbered"},
    )
    if (
        document.get("schema_version") != 1
        or document.get("receipt_type")
        != LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        or document.get("operation_id") != operation.get("id")
        or document.get("payload_checksum")
        != operation.get("payload_checksum")
        or document.get("reconciliation_required") is not False
        or document.get("picture_count") != 0
    ):
        raise BridgeError("文档型 SpecialWool 上传回执身份或状态不匹配")
    expected_target = str(summary.get("target_sample_number") or "")
    requested = document.get("requested_sample_number")
    actual = document.get("target_sample_number")
    if requested is None:
        # 旧版 Writer 回执：目标编号必须与预检单完全一致。
        if actual != expected_target:
            raise BridgeError("文档型 SpecialWool 上传回执目标编号与预检单不一致")
    else:
        if requested != expected_target:
            raise BridgeError("文档型 SpecialWool 上传回执请求编号与预检单不一致")
        target_base = str(
            (summary.get("target_allocation") or {}).get("base_number")
            or expected_target
        ).strip().upper()
        if not isinstance(actual, str) or not (
            actual == requested or actual.startswith(target_base + "-")
        ):
            raise BridgeError("文档型 SpecialWool 上传回执实际编号不属于预检单编号族")
        renumbered = document.get("renumbered")
        if not isinstance(renumbered, bool) or renumbered != (actual != requested):
            raise BridgeError("文档型 SpecialWool 上传回执顺号标记与实际编号不一致")
    expected_filename = str(summary.get("target_filename") or "")
    if (
        isinstance(actual, str)
        and actual != expected_target
        and expected_filename.startswith(expected_target)
    ):
        # 顺号改写后最终文件名跟随实际编号（编号前缀替换，其余部分不变）。
        expected_filename = actual + expected_filename[len(expected_target):]
    if document.get("target_filename") != expected_filename:
        raise BridgeError("文档型 SpecialWool 上传回执目标文件名与预检单不一致")
    files = summary.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise BridgeError("文档型 SpecialWool 上传摘要缺少唯一源文件")
    source = _strict_map(
        document.get("source_artifact"),
        path="receipt.source_artifact",
        required={"artifact_id", "filename", "size_bytes", "content_sha256"},
    )
    expected = files[0]
    for key in ("artifact_id", "filename", "size_bytes", "content_sha256"):
        if source.get(key) != expected.get(key):
            raise BridgeError(f"文档型 SpecialWool source_artifact.{key} 不匹配")
    project = _validated_paper_task_project(
        document.get("task_project"), path="receipt.task_project"
    )
    if project != _validated_paper_task_project(
        summary.get("task_project"), path="request_summary.task_project"
    ):
        raise BridgeError("文档型 SpecialWool 上传任务项目绑定不一致")
    main = document.get("main_record")
    if not isinstance(main, dict):
        raise BridgeError("文档型 SpecialWool 上传缺少主记录读回")
    _required_text(main.get("id"), path="receipt.main_record.id", pattern=_REDACTED_ID_RE)
    if main.get("file_path") != expected_filename:
        raise BridgeError("文档型 SpecialWool 主记录文件名不匹配")
    readback = document.get("readback")
    if (
        not isinstance(readback, dict)
        or readback.get("main_count") != 1
        or readback.get("picture_count") != 0
        or readback.get("mismatches") != []
        or readback.get("target_filename") != expected_filename
    ):
        raise BridgeError("文档型 SpecialWool 上传读回未确认无图片子记录")
    stage_names = [
        item.get("stage") if isinstance(item, dict) else item
        for item in document.get("stages") or []
    ]
    if stage_names != list(QUALITATIVE_UPLOAD_PROGRESS_STAGES):
        raise BridgeError("文档型 SpecialWool 上传回执阶段不完整或顺序错误")
    return document


def _validated_final_entry_task_project(value, *, path: str) -> dict:
    project = _strict_map(
        value,
        path=path,
        required={
            "project_key",
            "task_check_item_id",
            "check_item_id",
            "check_item_no",
            "check_item_name",
            "check_method",
            "seq_num",
            "check_count",
        },
    )
    _required_text(
        project.get("project_key"),
        path=f"{path}.project_key",
        pattern=_TASK_PROJECT_KEY_RE,
    )
    for key in ("task_check_item_id", "check_item_id"):
        _required_text(
            project.get(key), path=f"{path}.{key}", pattern=_REDACTED_ID_RE
        )
    if (
        project.get("check_item_no"),
        project.get("check_item_name"),
        project.get("check_method"),
    ) not in SUPPORTED_EXCEL_PROJECTS:
        raise BridgeError(f"{path} 不是受支持的 GB/T 36422-2018 电镜项目")
    _required_int(project.get("seq_num"), path=f"{path}.seq_num")
    if _required_int(
        project.get("check_count"), path=f"{path}.check_count"
    ) < 1:
        raise BridgeError(f"{path}.check_count 必须至少为 1")
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
    expected_key = "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    if project.get("project_key") != expected_key:
        raise BridgeError(f"{path}.project_key 与项目字段不一致")
    return project


def validate_final_entry_machine_payload(
    operation: dict,
    summary: dict,
) -> tuple[dict, dict]:
    payload = operation.get("machine_payload")
    if not isinstance(payload, dict):
        raise BridgeError("FinalEntry claim 缺少 machine_payload")
    payload = _strict_map(
        payload,
        path="machine_payload",
        required={
            "schema_version",
            "operation_type",
            "sample_number",
            "check_item_no",
            "check_item_name",
            "task_project",
            "expected_existing_register_count",
            "excel_record",
        },
        optional={"controlled_test_override", "existing_record_decision"},
    )
    if payload.get("schema_version") != 2 or payload.get(
        "operation_type"
    ) != "excel_check_record":
        raise BridgeError("FinalEntry machine_payload 必须是 Excel schema v2")
    target = _required_text(
        summary.get("target_sample_number"), path="request_summary.target_sample_number"
    )
    if payload.get("sample_number") != target:
        raise BridgeError("FinalEntry machine_payload 样品号与签发目标不一致")
    if (
        payload.get("check_item_no"),
        payload.get("check_item_name"),
    ) not in {(no, name) for no, name, _method in SUPPORTED_EXCEL_PROJECTS}:
        raise BridgeError("FinalEntry machine_payload 任务项目不受支持")
    expected_existing = _required_int(
        payload.get("expected_existing_register_count"),
        path="machine_payload.expected_existing_register_count",
    )
    files = summary.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise BridgeError("FinalEntry request_summary 必须绑定一份源制品")
    source = _strict_map(
        files[0],
        path="request_summary.files[0]",
        required={
            "artifact_id",
            "root_id",
            "relative_path",
            "filename",
            "size_bytes",
            "content_sha256",
        },
    )
    for key in ("artifact_id", "root_id", "relative_path", "filename"):
        _required_text(source.get(key), path=f"request_summary.files[0].{key}")
    _required_int(source.get("size_bytes"), path="request_summary.files[0].size_bytes")
    _required_text(
        source.get("content_sha256"),
        path="request_summary.files[0].content_sha256",
        pattern=_SHA256_RE,
    )
    excel = payload.get("excel_record")
    workbook = excel.get("workbook") if isinstance(excel, dict) else None
    if not isinstance(workbook, dict):
        raise BridgeError("FinalEntry machine_payload 缺少工作簿绑定")
    for key in ("relative_path", "filename", "size_bytes", "content_sha256"):
        if workbook.get(key) != source.get(key):
            raise BridgeError(f"machine_payload 工作簿字段 {key} 与源制品不一致")
    package_project = _validated_final_entry_task_project(
        payload.get("task_project"), path="machine_payload.task_project"
    )
    summary_project = _validated_final_entry_task_project(
        summary.get("task_project"), path="request_summary.task_project"
    )
    if package_project != summary_project:
        raise BridgeError("FinalEntry 私有任务项目与签发摘要不一致")
    if not isinstance(summary.get("template_binding"), dict):
        raise BridgeError("FinalEntry request_summary 缺少项目或模板绑定")
    check_count = package_project["check_count"]
    override = payload.get("controlled_test_override")
    existing_decision_value = payload.get("existing_record_decision")
    if override is not None and existing_decision_value is not None:
        raise BridgeError("FinalEntry 不得同时声明测试覆盖和已有登记确认")
    if expected_existing == 0 and (
        override is not None or existing_decision_value is not None
    ):
        raise BridgeError("普通 FinalEntry 包不得声明受控既有登记")
    existing_decision = None
    if existing_decision_value is not None:
        existing_decision = _strict_map(
            existing_decision_value,
            path="machine_payload.existing_record_decision",
            required={
                "kind",
                "action",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
            },
        )
        if (
            existing_decision.get("kind") != "append_when_check_count_one"
            or existing_decision.get("action") != "append"
            or existing_decision.get("expected_task_check_count") != 1
            or check_count != 1
            or expected_existing < 1
            or existing_decision.get("expected_existing_register_count")
            != expected_existing
            or existing_decision.get("resulting_register_count")
            != expected_existing + 1
        ):
            raise BridgeError("FinalEntry 已有登记确认契约不正确")
    elif override is None and check_count == 1 and expected_existing > 0:
        raise BridgeError("FinalEntry 单份项目已有登记时必须明确确认继续新增")
    if existing_decision != summary.get("existing_record_decision"):
        raise BridgeError("FinalEntry 已有登记确认与签发摘要不一致")

    identity_contract = _strict_map(
        summary.get("sample_identity_contract"),
        path="request_summary.sample_identity_contract",
        required={
            "selected",
            "options",
            "option_count",
            "check_count",
            "count_mismatch",
        },
    )
    selected_identity = identity_contract.get("selected")
    offered_identities = identity_contract.get("options")
    expected_identities = excel.get("expected_key_identities")
    register = excel.get("register")
    register_identity = (
        register.get("sample_identity") if isinstance(register, dict) else None
    )
    if (
        not isinstance(selected_identity, str)
        or not isinstance(offered_identities, list)
        or any(not isinstance(value, str) for value in offered_identities)
        or identity_contract.get("option_count") != len(offered_identities)
        or identity_contract.get("check_count") != check_count
        or identity_contract.get("count_mismatch")
        is not bool(offered_identities and len(offered_identities) != check_count)
        or expected_identities != [selected_identity]
        or register_identity != selected_identity
        or (
            offered_identities
            and selected_identity not in offered_identities
        )
        or (not offered_identities and selected_identity)
    ):
        raise BridgeError("FinalEntry 样品识别与任务单或工作簿绑定不一致")
    return payload, source


def _validated_paper_task_project(value, *, path: str) -> dict:
    project = _strict_map(
        value,
        path=path,
        required={
            "project_key",
            "task_check_item_id",
            "check_item_id",
            "check_item_no",
            "check_item_name",
            "check_method",
            "seq_num",
            "check_count",
        },
    )
    _required_text(
        project.get("project_key"),
        path=f"{path}.project_key",
        pattern=_TASK_PROJECT_KEY_RE,
    )
    for key in ("task_check_item_id", "check_item_id"):
        _required_text(
            project.get(key), path=f"{path}.{key}", pattern=_REDACTED_ID_RE
        )
    for key in ("check_item_no", "check_item_name", "check_method"):
        if not isinstance(project.get(key), str):
            raise BridgeError(f"{path}.{key} 必须是文本")
    if (
        project.get("check_item_name") != "纸、纸板和纸浆纤维鉴别分析"
        or project.get("check_method") != "GB/T 4688-2020"
        or _required_int(project.get("check_count"), path=f"{path}.check_count")
        < 1
    ):
        raise BridgeError(f"{path} 不是受支持的 GB/T 4688-2020 纸纤维项目")
    _required_int(project.get("seq_num"), path=f"{path}.seq_num")
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
    expected_key = "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    if project.get("project_key") != expected_key:
        raise BridgeError(f"{path}.project_key 与项目字段不一致")
    return project


def validate_generic_final_entry_machine_payload(
    operation: dict,
    summary: dict,
) -> dict:
    payload = _strict_map(
        operation.get("machine_payload"),
        path="machine_payload",
        required={
            "schema_version",
            "operation_type",
            "sample_number",
            "check_item_no",
            "check_item_name",
            "task_project",
            "expected_existing_register_count",
            "generic_record",
        },
        optional={"controlled_test_override", "existing_record_decision"},
    )
    expected_existing = payload.get("expected_existing_register_count")
    if (
        payload.get("schema_version") != 2
        or payload.get("operation_type") != "generic_item_record"
        or payload.get("sample_number") != summary.get("target_sample_number")
        or not isinstance(expected_existing, int)
        or isinstance(expected_existing, bool)
        or expected_existing < 0
    ):
        raise BridgeError("Generic FinalEntry machine_payload 身份或计数不正确")
    override = payload.get("controlled_test_override")
    existing_decision_value = payload.get("existing_record_decision")
    if override is not None and existing_decision_value is not None:
        raise BridgeError("Generic FinalEntry 不得同时声明测试覆盖和已有登记确认")
    if expected_existing == 0 and override is not None:
        raise BridgeError("普通 Generic FinalEntry 包不得声明受控既有登记")
    package_project = _validated_paper_task_project(
        payload.get("task_project"), path="machine_payload.task_project"
    )
    summary_project = _validated_paper_task_project(
        summary.get("task_project"), path="request_summary.task_project"
    )
    if package_project != summary_project:
        raise BridgeError("Generic FinalEntry 私有任务项目与签发摘要不一致")
    check_count = package_project["check_count"]
    existing_decision = None
    if existing_decision_value is not None:
        existing_decision = _strict_map(
            existing_decision_value,
            path="machine_payload.existing_record_decision",
            required={
                "kind",
                "action",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
            },
        )
        if (
            existing_decision.get("kind") != "append_when_check_count_one"
            or existing_decision.get("action") != "append"
            or existing_decision.get("expected_task_check_count") != 1
            or check_count != 1
            or expected_existing < 1
            or existing_decision.get("expected_existing_register_count")
            != expected_existing
            or existing_decision.get("resulting_register_count")
            != expected_existing + 1
        ):
            raise BridgeError("Generic FinalEntry 已有登记确认契约不正确")
    elif override is None and check_count == 1 and expected_existing > 0:
        raise BridgeError("Generic FinalEntry 单份项目已有登记时必须明确确认继续新增")
    summary_decision = summary.get("existing_record_decision")
    if existing_decision != summary_decision:
        raise BridgeError("Generic FinalEntry 已有登记确认与签发摘要不一致")
    if (
        payload.get("check_item_no") != package_project.get("check_item_no")
        or payload.get("check_item_name")
        != package_project.get("check_item_name")
    ):
        raise BridgeError("Generic FinalEntry 检测项目与任务绑定不一致")
    result_contract = _strict_map(
        summary.get("result_contract"),
        path="request_summary.result_contract",
        required={"worksheet", "cell", "value", "unit"},
    )
    if (
        result_contract.get("worksheet") != "Sheet1"
        or result_contract.get("cell") != "W32"
        or not isinstance(result_contract.get("value"), str)
        or not result_contract["value"].strip()
        or result_contract.get("unit") not in {"", "%"}
    ):
        raise BridgeError("Generic FinalEntry W32 结果绑定不正确")
    judgement_contract_value = summary.get("judgement_contract")
    legacy_judgement_contract = judgement_contract_value is None
    if legacy_judgement_contract:
        final_entry_summary = summary.get("final_entry_summary")
        judgement_required = bool(
            isinstance(final_entry_summary, dict)
            and final_entry_summary.get("judgement_required") is True
        )
        expected_judge_basis = None if judgement_required else ""
        expected_judgement = None if judgement_required else ""
        expected_standard_value = (
            result_contract.get("value") if judgement_required else ""
        )
    else:
        judgement_contract = _strict_map(
            judgement_contract_value,
            path="request_summary.judgement_contract",
            required={
                "required",
                "judge_basis",
                "judgement",
                "standard_value",
            },
        )
        judgement_required = judgement_contract.get("required")
        if not isinstance(judgement_required, bool):
            raise BridgeError(
                "request_summary.judgement_contract.required 必须是布尔值"
            )
        if judgement_required:
            expected_judge_basis = _required_text(
                judgement_contract.get("judge_basis"),
                path="request_summary.judgement_contract.judge_basis",
            )
            expected_judgement = _required_text(
                judgement_contract.get("judgement"),
                path="request_summary.judgement_contract.judgement",
            )
            expected_standard_value = _required_text(
                judgement_contract.get("standard_value"),
                path="request_summary.judgement_contract.standard_value",
            )
            if expected_judgement not in {"符合", "不符合"}:
                raise BridgeError(
                    "request_summary.judgement_contract.judgement 不受支持"
                )
        else:
            if any(
                judgement_contract.get(key) not in {"", None}
                for key in ("judge_basis", "judgement", "standard_value")
            ):
                raise BridgeError("无需判定的 Generic FinalEntry 判定摘要必须为空")
            expected_judge_basis = ""
            expected_judgement = ""
            expected_standard_value = ""
    generic = _strict_map(
        payload.get("generic_record"),
        path="machine_payload.generic_record",
        required={"header", "details"},
    )
    header = _strict_map(
        generic.get("header"),
        path="machine_payload.generic_record.header",
        required={
            "grade",
            "unit",
            "judge_basis",
            "test_method",
            "sample_description",
            "standard_type",
            "report_check_item_name",
            "attach_info",
            "remark",
            "total_judge",
        },
    )
    if (
        header.get("unit") != result_contract.get("unit")
        or header.get("test_method") != "GB/T 4688-2020"
        or any(
            header.get(key) not in {"", None}
            for key in (
                "grade",
                "standard_type",
                "attach_info",
                "remark",
            )
        )
    ):
        raise BridgeError("Generic FinalEntry 表头与纸纤维结果绑定不一致")
    identity_contract_value = summary.get("sample_identity_contract")
    # Already-approved operations from the preceding release did not carry a
    # sample-identity summary and were only allowed to submit an empty value.
    if identity_contract_value is None:
        if header.get("sample_description") not in {"", None}:
            raise BridgeError("旧版 Generic FinalEntry 不得补写样品识别")
        identity_contract = None
    else:
        identity_contract = _strict_map(
            identity_contract_value,
            path="request_summary.sample_identity_contract",
            required={
                "selected",
                "options",
                "option_count",
                "check_count",
                "count_mismatch",
            },
        )
    if identity_contract is not None:
        options = identity_contract.get("options")
        selected_identity = identity_contract.get("selected")
        if (
            not isinstance(options, list)
            or not all(isinstance(value, str) and value for value in options)
            or len(set(options)) != len(options)
            or identity_contract.get("option_count") != len(options)
            or identity_contract.get("check_count") != check_count
            or identity_contract.get("count_mismatch")
            is not bool(options and len(options) != check_count)
            or not isinstance(selected_identity, str)
            or header.get("sample_description") != selected_identity
            or (options and selected_identity not in options)
            or (not options and selected_identity != "")
        ):
            raise BridgeError("Generic FinalEntry 样品识别契约不正确")
    details = generic.get("details")
    if not isinstance(details, list) or len(details) != 1:
        raise BridgeError("Generic FinalEntry 必须包含一条实测结果")
    detail = _strict_map(
        details[0],
        path="machine_payload.generic_record.details[0]",
        required={
            "standard_location",
            "standard_value",
            "real_location",
            "real_value",
        },
    )
    if (
        detail.get("real_value") != result_contract.get("value")
        or any(
            detail.get(key) not in {"", None}
            for key in (
                "standard_location",
                "real_location",
            )
        )
    ):
        raise BridgeError("Generic FinalEntry 实测值与 W32 结果不一致")
    if legacy_judgement_contract and judgement_required:
        # Prepared operations from the previous release carried no signed
        # judgement contract and only allowed StandardValue to mirror W32.
        # Keep those already-approved packages executable without granting
        # the new free-form override capability.
        expected_judge_basis = _required_text(
            header.get("judge_basis"),
            path="machine_payload.generic_record.header.judge_basis",
        )
        expected_judgement = _required_text(
            header.get("total_judge"),
            path="machine_payload.generic_record.header.total_judge",
        )
        if expected_judgement not in {"符合", "不符合"}:
            raise BridgeError("旧版 Generic FinalEntry 判定结果不受支持")
    expected_report_name = (
        package_project.get("check_item_name") if judgement_required else ""
    )
    if (
        header.get("judge_basis") != expected_judge_basis
        or header.get("report_check_item_name") != expected_report_name
        or header.get("total_judge") != expected_judgement
        or detail.get("standard_value") != expected_standard_value
    ):
        raise BridgeError("Generic FinalEntry 判定字段与签发摘要不一致")
    return payload


def controlled_final_entry_override_enabled(args, payload: dict) -> bool:
    override = payload.get("controlled_test_override")
    cli_allowed = bool(
        getattr(args, "allow_controlled_final_entry_test_override", False)
    )
    if override is None:
        # Writer 在普通包上收到 CLI flag 会主动失败；Bridge 因此绝不传递该 flag。
        return False
    if not isinstance(override, dict):
        raise BridgeError("controlled_test_override 必须是对象")
    target = _required_text(
        payload.get("sample_number"), path="machine_payload.sample_number"
    )
    if override.get("target_sample_number") != target:
        raise BridgeError("受控覆盖目标与 machine_payload 样品号不一致")
    if not cli_allowed:
        raise BridgeError("Bridge 未显式允许本次受控 FinalEntry 测试覆盖")
    environment_target = os.environ.get("FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO", "")
    if environment_target.strip() != target:
        raise BridgeError("受控 FinalEntry 环境变量未与任务包目标精确绑定")
    return True


def _raw_stage_index(
    raw_stages: list[dict],
    *,
    expected_stages: tuple[str, ...] = FINAL_ENTRY_RAW_SUCCESS_STAGES,
) -> dict[str, dict]:
    names: list[str] = []
    result: dict[str, dict] = {}
    for index, entry in enumerate(raw_stages):
        item = _strict_map(
            entry,
            path=f"raw_receipt.stages[{index}]",
            required={"stage", "at"},
            optional={"detail"},
        )
        name = _required_text(
            item.get("stage"), path=f"raw_receipt.stages[{index}].stage"
        )
        _required_text(item.get("at"), path=f"raw_receipt.stages[{index}].at")
        names.append(name)
        result[name] = item
    if tuple(names) != expected_stages or len(result) != len(names):
        raise BridgeError("FinalEntry raw receipt 阶段缺失、重复或顺序错误")
    return result


def _strict_stage_detail(
    stages: dict[str, dict],
    stage: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict:
    return _strict_map(
        stages[stage].get("detail"),
        path=f"raw_receipt.stages.{stage}.detail",
        required=required,
        optional=optional,
    )


def convert_final_entry_receipt(
    operation: dict,
    summary: dict,
    machine_payload: dict,
    source: dict,
    raw_receipt: dict,
) -> dict:
    override_payload = machine_payload.get("controlled_test_override")
    raw = _strict_map(
        raw_receipt,
        path="raw_receipt",
        required={
            "schema_version",
            "mode",
            "exit_code",
            "reconciliation_required",
            "stages",
            "package_schema_version",
            "sample_number",
            "check_item_no",
            "check_item_name",
            "task_project",
        },
        optional={"controlled_test_override"} if override_payload is not None else set(),
    )
    if (
        raw.get("schema_version") != 1
        or raw.get("mode") != "excel_check_record"
        or raw.get("exit_code") != 0
        or raw.get("reconciliation_required") is not False
        or raw.get("package_schema_version") != 2
        or raw.get("sample_number") != machine_payload.get("sample_number")
        or raw.get("check_item_no") != machine_payload.get("check_item_no")
        or raw.get("check_item_name") != machine_payload.get("check_item_name")
    ):
        raise BridgeError("FinalEntry raw receipt 身份或成功状态不匹配")
    if not isinstance(raw.get("stages"), list):
        raise BridgeError("FinalEntry raw receipt 缺少阶段列表")
    stages = _raw_stage_index(raw["stages"])
    measured_project = _validated_final_entry_task_project(
        raw.get("task_project"), path="raw_receipt.task_project"
    )
    package_project = _validated_final_entry_task_project(
        machine_payload.get("task_project"),
        path="machine_payload.task_project",
    )
    if measured_project != package_project:
        raise BridgeError("FinalEntry Writer 实测任务项目与私有任务包不一致")

    expected_existing = _required_int(
        machine_payload.get("expected_existing_register_count"),
        path="machine_payload.expected_existing_register_count",
    )
    excel = machine_payload["excel_record"]
    workbook = excel["workbook"]
    key_result_count = _required_int(
        excel.get("key_result_count"), path="machine_payload.excel_record.key_result_count"
    )
    package_detail = _strict_stage_detail(
        stages,
        "package_validated",
        {
            "schema_version",
            "operation_type",
            "expected_existing_register_count",
            "controlled_test_override_active",
        },
        optional={"existing_record_decision_present"},
    )
    existing_decision = machine_payload.get("existing_record_decision")
    expected_existing_decision = existing_decision is not None
    if (
        package_detail.get("schema_version") != 2
        or package_detail.get("operation_type") != "excel_check_record"
        or package_detail.get("expected_existing_register_count") != expected_existing
        or package_detail.get(
            "existing_record_decision_present", False
        ) is not expected_existing_decision
    ):
        raise BridgeError("FinalEntry package_validated 详情不匹配")
    workbook_detail = _strict_stage_detail(
        stages,
        "workbook_verified",
        {"filename", "size_bytes", "content_sha256", "key_result_count"},
    )
    for key in ("filename", "size_bytes", "content_sha256", "key_result_count"):
        expected = workbook.get(key) if key != "key_result_count" else key_result_count
        if workbook_detail.get(key) != expected:
            raise BridgeError(f"FinalEntry workbook_verified.{key} 不匹配")
    preflight = _strict_stage_detail(
        stages,
        "remote_preflight_verified",
        {
            "expected_result_count",
            "existing_register_count",
            "controlled_test_override_applied",
            "mapping_config_sha256",
            "mapping_config_count",
        },
        optional={"existing_record_decision_applied"},
    )
    expected_result_count = _required_int(
        preflight.get("expected_result_count"),
        path="raw_receipt.remote_preflight_verified.expected_result_count",
    )
    if expected_result_count != package_project["check_count"]:
        raise BridgeError("FinalEntry 远端任务份数与实测任务项目不一致")
    if preflight.get("existing_register_count") != expected_existing:
        raise BridgeError("FinalEntry 远端既有登记数与机器载荷不一致")
    if (
        preflight.get("existing_record_decision_applied", False)
        is not expected_existing_decision
    ):
        raise BridgeError("Excel FinalEntry 未按任务包应用已有登记确认")
    if preflight.get("mapping_config_sha256") != excel.get(
        "expected_mapping_config_sha256"
    ) or _required_int(
        preflight.get("mapping_config_count"),
        path="raw_receipt.remote_preflight_verified.mapping_config_count",
    ) < 1:
        raise BridgeError("FinalEntry 远端映射配置与机器载荷不一致")
    ready = _strict_stage_detail(
        stages,
        "remote_write_ready",
        {
            "operation_type",
            "expected_existing_register_count",
            "target_filename",
            "controlled_test_override_applied",
        },
        optional={"existing_record_decision_applied"},
    )
    target_filename = _required_text(
        ready.get("target_filename"),
        path="raw_receipt.remote_write_ready.target_filename",
        pattern=_FINAL_ENTRY_FILENAME_RE,
    )
    if ready.get("operation_type") != "excel_check_record" or ready.get(
        "expected_existing_register_count"
    ) != expected_existing:
        raise BridgeError("FinalEntry remote_write_ready 详情不匹配")
    if (
        ready.get("existing_record_decision_applied", False)
        is not expected_existing_decision
    ):
        raise BridgeError("Excel FinalEntry 写入阶段未按任务包应用已有登记确认")
    staging_file = _strict_stage_detail(
        stages,
        "staging_file_verified",
        {"filename", "size_bytes", "content_sha256"},
    )
    if (
        staging_file.get("filename") != target_filename
        or staging_file.get("size_bytes") != workbook.get("size_bytes")
        or staging_file.get("content_sha256") != workbook.get("content_sha256")
    ):
        raise BridgeError("FinalEntry staging 文件回读与源工作簿不一致")
    remote_file = _strict_stage_detail(
        stages,
        "remote_file_verified",
        {"filename", "content_sha256"},
    )
    if remote_file.get("filename") != target_filename or remote_file.get(
        "content_sha256"
    ) != workbook.get("content_sha256"):
        raise BridgeError("FinalEntry 远端文件回读与源工作簿不一致")
    proof = _strict_stage_detail(
        stages,
        "excel_proof_verified",
        {"key_result_count", "record_fingerprint"},
    )
    record_id = _required_text(
        proof.get("record_fingerprint"),
        path="raw_receipt.excel_proof_verified.record_fingerprint",
        pattern=_REDACTED_ID_RE,
    )
    if proof.get("key_result_count") != key_result_count:
        raise BridgeError("FinalEntry 校对后的关键结果数量不一致")

    override_receipt = None
    expected_override_active = override_payload is not None
    if package_detail.get("controlled_test_override_active") is not expected_override_active:
        raise BridgeError("FinalEntry package 阶段的受控覆盖状态不一致")
    if preflight.get("controlled_test_override_applied") is not expected_override_active or ready.get(
        "controlled_test_override_applied"
    ) is not expected_override_active:
        raise BridgeError("FinalEntry 远端预检未按任务包应用受控覆盖")
    if override_payload is not None:
        raw_override = _strict_map(
            raw.get("controlled_test_override"),
            path="raw_receipt.controlled_test_override",
            required={
                "active",
                "applied",
                "kind",
                "target_sample_number",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
                "reason",
            },
        )
        for key in (
            "kind",
            "target_sample_number",
            "expected_task_check_count",
            "expected_existing_register_count",
            "resulting_register_count",
            "reason",
        ):
            if raw_override.get(key) != override_payload.get(key):
                raise BridgeError(f"FinalEntry controlled_test_override.{key} 不匹配")
        if raw_override.get("active") is not True or raw_override.get("applied") is not True:
            raise BridgeError("FinalEntry 受控覆盖成功回执必须同时 active/applied")
        if expected_result_count != override_payload.get("expected_task_check_count"):
            raise BridgeError("FinalEntry 受控覆盖的任务份数与远端预检不一致")
        override_receipt = {
            key: raw_override[key]
            for key in (
                "active",
                "applied",
                "kind",
                "target_sample_number",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
            )
        }
        resulting_count = raw_override["resulting_register_count"]
    elif existing_decision is not None:
        if "controlled_test_override" in raw:
            raise BridgeError("已有登记确认回执不得包含受控测试覆盖对象")
        resulting_count = existing_decision["resulting_register_count"]
        if expected_result_count != existing_decision["expected_task_check_count"]:
            raise BridgeError("FinalEntry 已有登记确认的任务份数与远端预检不一致")
    else:
        if "controlled_test_override" in raw:
            raise BridgeError("普通 FinalEntry 回执不得包含受控覆盖对象")
        resulting_count = expected_existing + 1
        if expected_result_count == 1 and expected_existing > 0:
            raise BridgeError("FinalEntry 单份项目已有登记但缺少继续新增确认")

    inverse_stage = {
        canonical: raw_name
        for raw_name, canonical in FINAL_ENTRY_RAW_STAGE_MAP.items()
    }
    canonical_stages = [
        {
            "stage": canonical,
            "at": stages[inverse_stage[canonical]]["at"],
        }
        for canonical in FINAL_ENTRY_PROGRESS_STAGES
    ]
    return {
        "schema_version": 1,
        "receipt_type": LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
        "operation_id": operation.get("id"),
        "payload_checksum": operation.get("payload_checksum"),
        "target_sample_number": summary.get("target_sample_number"),
        "source_artifact": {
            key: source[key]
            for key in ("artifact_id", "filename", "size_bytes", "content_sha256")
        },
        # Never echo request_summary here.  This binding was measured by the
        # Writer from the current read-only Oracle row and then checked against
        # the private package above.
        "task_project": dict(measured_project),
        "template_binding": dict(summary["template_binding"]),
        "final_entry": {
            "package_schema_version": 2,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": resulting_count,
            "key_result_count": key_result_count,
            "record_id": record_id,
            "original_data_filename": target_filename,
            "content_sha256": workbook["content_sha256"],
            "proofed": True,
        },
        "controlled_test_override": override_receipt,
        "existing_record_decision": (
            dict(existing_decision)
            if isinstance(existing_decision, dict)
            else None
        ),
        "stages": canonical_stages,
        "reconciliation_required": False,
    }


def convert_generic_final_entry_receipt(
    operation: dict,
    summary: dict,
    machine_payload: dict,
    raw_receipt: dict,
) -> dict:
    override_payload = machine_payload.get("controlled_test_override")
    raw = _strict_map(
        raw_receipt,
        path="raw_receipt",
        required={
            "schema_version",
            "mode",
            "exit_code",
            "reconciliation_required",
            "stages",
            "package_schema_version",
            "sample_number",
            "check_item_no",
            "check_item_name",
            "task_project",
        },
        optional={"controlled_test_override"} if override_payload is not None else set(),
    )
    if (
        raw.get("schema_version") != 1
        or raw.get("mode") != "generic_item_record"
        or raw.get("exit_code") != 0
        or raw.get("reconciliation_required") is not False
        or raw.get("package_schema_version") != 2
        or raw.get("sample_number") != machine_payload.get("sample_number")
        or raw.get("check_item_no") != machine_payload.get("check_item_no")
        or raw.get("check_item_name")
        != machine_payload.get("check_item_name")
        or not isinstance(raw.get("stages"), list)
    ):
        raise BridgeError("Generic FinalEntry raw receipt 身份或成功状态不匹配")
    stages = _raw_stage_index(
        raw["stages"], expected_stages=GENERIC_FINAL_ENTRY_RAW_SUCCESS_STAGES
    )
    measured_project = _validated_paper_task_project(
        raw.get("task_project"), path="raw_receipt.task_project"
    )
    package_project = _validated_paper_task_project(
        machine_payload.get("task_project"),
        path="machine_payload.task_project",
    )
    if measured_project != package_project:
        raise BridgeError("Generic FinalEntry Writer 实测任务项目与私有载荷不一致")
    expected_existing = _required_int(
        machine_payload.get("expected_existing_register_count"),
        path="machine_payload.expected_existing_register_count",
    )
    package_detail = _strict_stage_detail(
        stages,
        "package_validated",
        {
            "schema_version",
            "operation_type",
            "expected_existing_register_count",
            "controlled_test_override_active",
        },
        optional={"existing_record_decision_present"},
    )
    if (
        package_detail.get("schema_version") != 2
        or package_detail.get("operation_type") != "generic_item_record"
        or package_detail.get("expected_existing_register_count") != expected_existing
    ):
        raise BridgeError("Generic FinalEntry package_validated 详情不匹配")
    detail_validation = _strict_stage_detail(
        stages, "generic_details_validated", {"row_count"}
    )
    if detail_validation.get("row_count") != 1:
        raise BridgeError("Generic FinalEntry 明细预检数量不正确")
    preflight = _strict_stage_detail(
        stages,
        "remote_preflight_verified",
        {
            "expected_result_count",
            "existing_register_count",
            "controlled_test_override_applied",
        },
        optional={"existing_record_decision_applied"},
    )
    expected_result_count = _required_int(
        preflight.get("expected_result_count"),
        path="raw_receipt.remote_preflight_verified.expected_result_count",
    )
    if expected_result_count != package_project["check_count"]:
        raise BridgeError("Generic FinalEntry 远端任务份数与实测任务项目不一致")
    if preflight.get("existing_register_count") != expected_existing:
        raise BridgeError("Generic FinalEntry 远端既有登记数与机器载荷不一致")
    ready = _strict_stage_detail(
        stages,
        "remote_write_ready",
        {
            "operation_type",
            "expected_existing_register_count",
            "target_filename",
            "controlled_test_override_applied",
        },
        optional={"existing_record_decision_applied"},
    )
    if (
        ready.get("operation_type") != "generic_item_record"
        or ready.get("expected_existing_register_count") != expected_existing
        or ready.get("target_filename") is not None
    ):
        raise BridgeError("Generic FinalEntry 写入许可详情不正确")
    saved = _strict_stage_detail(
        stages,
        "generic_rows_saved",
        {"detail_count", "record_fingerprint"},
    )
    verified = _strict_stage_detail(
        stages,
        "generic_readback_verified",
        {"detail_count", "key_result_count", "record_fingerprint"},
    )
    record_id = _required_text(
        verified.get("record_fingerprint"),
        path="raw_receipt.generic_readback_verified.record_fingerprint",
        pattern=_REDACTED_ID_RE,
    )
    if (
        saved.get("detail_count") != 1
        or verified.get("detail_count") != 1
        or verified.get("key_result_count") != 1
        or saved.get("record_fingerprint") != record_id
    ):
        raise BridgeError("Generic FinalEntry 明细或结果投影读回不一致")

    override_receipt = None
    expected_override_active = override_payload is not None
    existing_decision = machine_payload.get("existing_record_decision")
    expected_existing_decision = existing_decision is not None
    if package_detail.get("controlled_test_override_active") is not expected_override_active:
        raise BridgeError("Generic FinalEntry package 阶段的受控覆盖状态不一致")
    if (
        preflight.get("controlled_test_override_applied")
        is not expected_override_active
        or ready.get("controlled_test_override_applied")
        is not expected_override_active
    ):
        raise BridgeError("Generic FinalEntry 远端预检未按任务包应用受控覆盖")
    if (
        package_detail.get("existing_record_decision_present", False)
        is not expected_existing_decision
        or preflight.get("existing_record_decision_applied", False)
        is not expected_existing_decision
        or ready.get("existing_record_decision_applied", False)
        is not expected_existing_decision
    ):
        raise BridgeError("Generic FinalEntry 未按任务包应用已有登记确认")
    if override_payload is not None:
        raw_override = _strict_map(
            raw.get("controlled_test_override"),
            path="raw_receipt.controlled_test_override",
            required={
                "active",
                "applied",
                "kind",
                "target_sample_number",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
                "reason",
            },
        )
        for key in (
            "kind",
            "target_sample_number",
            "expected_task_check_count",
            "expected_existing_register_count",
            "resulting_register_count",
            "reason",
        ):
            if raw_override.get(key) != override_payload.get(key):
                raise BridgeError(f"Generic FinalEntry controlled_test_override.{key} 不匹配")
        if (
            raw_override.get("active") is not True
            or raw_override.get("applied") is not True
        ):
            raise BridgeError("Generic FinalEntry 受控覆盖成功回执必须同时 active/applied")
        if expected_result_count != override_payload.get("expected_task_check_count"):
            raise BridgeError("Generic FinalEntry 受控覆盖的任务份数与远端预检不一致")
        override_receipt = {
            key: raw_override[key]
            for key in (
                "active",
                "applied",
                "kind",
                "target_sample_number",
                "expected_task_check_count",
                "expected_existing_register_count",
                "resulting_register_count",
            )
        }
        resulting_count = raw_override["resulting_register_count"]
    elif existing_decision is not None:
        if "controlled_test_override" in raw:
            raise BridgeError("已有登记确认回执不得包含受控测试覆盖对象")
        resulting_count = existing_decision["resulting_register_count"]
        if expected_result_count != existing_decision["expected_task_check_count"]:
            raise BridgeError("已有登记确认的任务份数与远端预检不一致")
    else:
        if "controlled_test_override" in raw:
            raise BridgeError("普通 Generic FinalEntry 回执不得包含受控覆盖对象")
        resulting_count = expected_existing + 1
        if expected_result_count == 1 and expected_existing > 0:
            raise BridgeError("Generic FinalEntry 单份项目已有登记但缺少继续新增确认")

    inverse_stage = {
        canonical: raw_name
        for raw_name, canonical in GENERIC_FINAL_ENTRY_RAW_STAGE_MAP.items()
    }
    canonical_stages = [
        {
            "stage": canonical,
            "at": stages[inverse_stage[canonical]]["at"],
        }
        for canonical in GENERIC_FINAL_ENTRY_PROGRESS_STAGES
    ]
    return {
        "schema_version": 1,
        "receipt_type": LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
        "operation_id": operation.get("id"),
        "payload_checksum": operation.get("payload_checksum"),
        "target_sample_number": summary.get("target_sample_number"),
        "task_project": dict(measured_project),
        "final_entry": {
            "package_schema_version": 2,
            "expected_existing_register_count": expected_existing,
            "resulting_register_count": resulting_count,
            "detail_count": 1,
            "key_result_count": 1,
            "record_id": record_id,
            "proofed": False,
        },
        "controlled_test_override": override_receipt,
        "existing_record_decision": (
            dict(existing_decision)
            if isinstance(existing_decision, dict)
            else None
        ),
        "stages": canonical_stages,
        "reconciliation_required": False,
    }


def furthest_stage(
    *values: str | None,
    progress_stages: tuple[str, ...] = PROGRESS_STAGES,
) -> str | None:
    known = [stage for stage in values if stage in progress_stages]
    if not known:
        return None
    return max(known, key=progress_stages.index)


def stage_before_side_effect(
    stage: str | None,
    *,
    progress_stages: tuple[str, ...] = PROGRESS_STAGES,
    side_effect_stage: str = SIDE_EFFECT_STAGE,
) -> bool:
    return stage not in progress_stages or (
        progress_stages.index(stage) < progress_stages.index(side_effect_stage)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧检务系统集中式 Bridge（单任务版）")
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--token-env", default="EXECUTION_BRIDGE_TOKEN")
    parser.add_argument("--writer", required=True, help="FibreCheckWriter.exe 路径")
    parser.add_argument(
        "--final-entry-writer",
        help="FibreCheckFinalEntryWriter.exe 路径；未配置时不广告最终录入能力",
    )
    parser.add_argument(
        "--final-entry-work-root",
        help="FinalEntry Writer 的本机受控 staging 根；未配置时不广告最终录入能力",
    )
    parser.add_argument(
        "--allow-controlled-final-entry-test-override",
        action="store_true",
        help="仅允许三重绑定完整的 FinalEntry 受控追加测试",
    )
    parser.add_argument("--fibrecheck-dir", required=True)
    parser.add_argument("--source-root", action="append", default=[], help="root_id=本地/UNC 前缀")
    parser.add_argument("--account-env", default="FIBRECHECK_RUNNER_ACCOUNT")
    parser.add_argument("--password-env", default="FIBRECHECK_RUNNER_PASSWORD")
    parser.add_argument("--once", action="store_true", help="只领取并执行一个任务（无任务则立即返回）")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"缺少 Bridge 令牌环境变量 {args.token_env}", file=sys.stderr)
        return 2
    account = os.environ.get(args.account_env, "")
    password = os.environ.get(args.password_env, "")
    if not account or not password:
        print("缺少旧系统账号/口令环境变量", file=sys.stderr)
        return 2
    root_map = parse_root_map(args.source_root)

    while True:
        outcome = run_one_cycle(args, token, account, password, root_map)
        if args.once or outcome == "claimed":
            return 0 if outcome == "claimed" else 1
        time.sleep(args.poll_seconds)


def run_one_cycle(args, token: str, account: str, password: str, root_map: dict[str, str]) -> str:
    supported_operation_types = configured_operation_types(args)
    claim = api_request(
        args.api_base,
        token,
        "POST",
        "/external-bridge/claim",
        {
            "bridge_id": args.bridge_id,
            "account_name": account,
            "supported_operation_types": list(supported_operation_types),
        },
    )
    if not claim.get("claimed"):
        print("没有待领取的旧系统操作")
        return "idle"

    attempt = claim["attempt"]
    operation = claim["operation"]
    attempt_id = attempt["id"]
    summary = operation.get("request_summary") or {}
    operation_type = (
        summary.get("operation_type") or LEGACY_REGENERATED_COUNT_OPERATION
    )
    expected_account = (
        (operation.get("credential") or {}).get("account_name") or ""
    )
    if normalized_identity(expected_account) != normalized_identity(account):
        api_request(
            args.api_base,
            token,
            "POST",
            f"/external-bridge/attempts/{attempt_id}/fail",
            {
                "bridge_id": args.bridge_id,
                "stage": "authenticated",
                "error_code": "credential_account_mismatch",
                "message": "Bridge 本地账号与任务绑定账号不一致，已拒绝启动 Writer",
            },
        )
        print("任务绑定账号与 Bridge 本地账号不一致，已安全拒绝", file=sys.stderr)
        return "claimed"
    capability = summary.get("execution_capability") or {}
    if (
        operation_type not in supported_operation_types
        or capability.get("available") is False
    ):
        api_request(
            args.api_base,
            token,
            "POST",
            f"/external-bridge/attempts/{attempt_id}/fail",
            {
                "bridge_id": args.bridge_id,
                "stage": "authenticated",
                "error_code": "writer_capability_unavailable",
                "message": (
                    "该外部操作尚未完成官方 DAL 行为证明，Bridge 已在副作用前拒绝"
                ),
            },
        )
        print(
            f"Writer 能力未开放: {operation_type}",
            file=sys.stderr,
        )
        return "claimed"
    progress_stages, side_effect_ready_stage, side_effect_stage = (
        OPERATION_STAGE_PROFILES[operation_type]
    )
    source_root = None
    final_entry_payload = None
    final_entry_source = None
    final_entry_override_enabled = False
    special_wool_review_source = None
    if operation_type in {
        LEGACY_REGENERATED_COUNT_OPERATION,
        LEGACY_SPECIAL_WOOL_IMAGE_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION,
    }:
        files = summary.get("files") or []
        if len(files) != 1:
            api_request(
                args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/fail",
                {"bridge_id": args.bridge_id, "stage": "authenticated",
                 "error_code": "unsupported_file_count", "message": "文件上传操作仅支持单文件"},
            )
            return "claimed"

        root_id = files[0].get("root_id")
        source_root = root_map.get(root_id)
        if not source_root:
            api_request(
                args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/fail",
                {"bridge_id": args.bridge_id, "stage": "authenticated",
                 "error_code": "unknown_root", "message": f"未配置源根 {root_id}"},
            )
            return "claimed"
    elif operation_type == LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION:
        try:
            final_entry_payload, final_entry_source = (
                validate_final_entry_machine_payload(operation, summary)
            )
            source_root = root_map.get(final_entry_source["root_id"])
            if not source_root:
                raise BridgeError(
                    f"未配置源根 {final_entry_source['root_id']}"
                )
            final_entry_override_enabled = (
                controlled_final_entry_override_enabled(
                    args, final_entry_payload
                )
            )
        except BridgeError as exc:
            api_request(
                args.api_base,
                token,
                "POST",
                f"/external-bridge/attempts/{attempt_id}/fail",
                {
                    "bridge_id": args.bridge_id,
                    "stage": "authenticated",
                    "error_code": "final_entry_machine_payload_rejected",
                    "message": str(exc),
                },
            )
            print(f"FinalEntry 任务包已安全拒绝: {exc}", file=sys.stderr)
            return "claimed"
    elif operation_type == LEGACY_GENERIC_FINAL_ENTRY_OPERATION:
        try:
            final_entry_payload = validate_generic_final_entry_machine_payload(
                operation, summary
            )
            final_entry_override_enabled = (
                controlled_final_entry_override_enabled(
                    args, final_entry_payload
                )
            )
        except BridgeError as exc:
            api_request(
                args.api_base,
                token,
                "POST",
                f"/external-bridge/attempts/{attempt_id}/fail",
                {
                    "bridge_id": args.bridge_id,
                    "stage": "authenticated",
                    "error_code": "generic_final_entry_machine_payload_rejected",
                    "message": str(exc),
                },
            )
            print(f"Generic FinalEntry 任务包已安全拒绝: {exc}", file=sys.stderr)
            return "claimed"
    elif operation_type in {
        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
    }:
        try:
            special_wool_review_source = (
                validate_special_wool_review_machine_payload(
                    operation, summary
                )
            )
        except BridgeError as exc:
            api_request(
                args.api_base,
                token,
                "POST",
                f"/external-bridge/attempts/{attempt_id}/fail",
                {
                    "bridge_id": args.bridge_id,
                    "stage": "authenticated",
                    "error_code": "special_wool_review_identity_rejected",
                    "message": str(exc),
                },
            )
            print(f"SpecialWool 复核主记录绑定已安全拒绝: {exc}", file=sys.stderr)
            return "claimed"

    print(f"领取成功: target={summary.get('target_sample_number')} attempt={attempt_id}")
    package_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False, prefix="bridge-package-")
    json.dump(
        final_entry_payload
        if operation_type in {
            LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
            LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
        }
        else claim,
        package_file,
        ensure_ascii=False,
        indent=2,
    )
    package_file.close()

    env = dict(os.environ)
    env["FIBRECHECK_RUNNER_PASSWORD"] = password

    if operation_type in {
        LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
        LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
    }:
        writer_command = [
            args.final_entry_writer,
            "--fibrecheck-dir", args.fibrecheck_dir,
            "--account", account,
            "--package", package_file.name,
            "--execute",
            "--side-effect-permit-stdin",
        ]
        if operation_type == LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION:
            writer_command.extend(
                [
                    "--source-root", source_root,
                    "--work-root", args.final_entry_work_root,
                ]
            )
        if final_entry_override_enabled:
            writer_command.append("--allow-controlled-test-override")
    else:
        writer_command = [
            args.writer,
            "--fibrecheck-dir", args.fibrecheck_dir,
            "--account", account,
            "--execute-upload",
            "--side-effect-permit-stdin",
            "--package", package_file.name,
        ]
        if source_root is not None:
            writer_command.extend(["--source-root", source_root])

    process = subprocess.Popen(
        writer_command,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stop_heartbeat = threading.Event()
    abort_requested = threading.Event()
    state_lock = threading.Lock()
    stage_holder = {
        "local": None,
        "confirmed": None,
        "permit_state": "not_sent",
        "termination_requested": False,
    }

    def force_kill_if_still_running() -> None:
        poll = getattr(process, "poll", None)
        kill = getattr(process, "kill", None)
        if not callable(poll) or not callable(kill):
            return
        try:
            if poll() is None:
                kill()
        except OSError:
            pass

    def terminate_before_side_effect() -> None:
        with state_lock:
            # The persisted server checkpoint is deliberately advanced before
            # the permit is delivered.  It therefore cannot tell us whether
            # the local Writer is still safely blocked on stdin.
            if stage_holder["permit_state"] == "sent":
                return
            if stage_holder["termination_requested"]:
                return
            stage_holder["termination_requested"] = True
        try:
            process.terminate()
        except OSError:
            pass
        watchdog = threading.Timer(5.0, force_kill_if_still_running)
        watchdog.daemon = True
        watchdog.start()

    def observe_abort(response: dict | None) -> bool:
        if response and response.get("abort_requested"):
            abort_requested.set()
            terminate_before_side_effect()
            return True
        return False

    def heartbeat_loop():
        while not stop_heartbeat.wait(30.0):
            try:
                response = api_request(
                    args.api_base,
                    token,
                    "POST",
                    f"/external-bridge/attempts/{attempt_id}/heartbeat",
                    {"bridge_id": args.bridge_id},
                )
                observe_abort(response)
            except Exception as exc:  # noqa: BLE001
                # 服务端尚未确认副作用边界时，失去心跳即停止 Writer；
                # 确认边界后保留进程，租约最终会安全转入人工对账。
                print(f"心跳失败: {exc}", file=sys.stderr)
                terminate_before_side_effect()

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()

    receipt: dict | None = None
    converted_receipt: dict | None = None
    final_entry_completed_seen = False
    special_wool_review_completed_seen = False
    qualitative_upload_completed_seen = False
    generic_final_entry_completed_seen = False
    receipt_validation_error: Exception | None = None
    last_stage = None
    stage_report_error: Exception | None = None
    stdout_tail: list[str] = []

    def persist_stage(stage: str, detail: str | None = None) -> dict:
        payload = {"bridge_id": args.bridge_id, "stage": stage}
        if detail:
            payload["detail"] = detail
        return api_request(
            args.api_base,
            token,
            "POST",
            f"/external-bridge/attempts/{attempt_id}/stage",
            payload,
        )

    def report_writer_stage(local_stage: str) -> None:
        nonlocal last_stage, stage_report_error
        print(f"阶段: {local_stage}")
        # reconciliation_required 是 Writer outcome，不是进度阶段。失败收尾
        # 必须使用服务端已确认的最远合法阶段。
        if local_stage == "reconciliation_required":
            return
        if local_stage not in progress_stages:
            stage_report_error = BridgeError(f"Writer 返回未知阶段: {local_stage}")
            terminate_before_side_effect()
            return
        with state_lock:
            stage_holder["local"] = local_stage
        try:
            if local_stage == side_effect_ready_stage:
                ready_response = persist_stage(side_effect_ready_stage)
                with state_lock:
                    stage_holder["confirmed"] = furthest_stage(
                        stage_holder["confirmed"],
                        side_effect_ready_stage,
                        progress_stages=progress_stages,
                    )
                    last_stage = stage_holder["confirmed"]
                if observe_abort(ready_response):
                    return

                # 服务端先持久化副作用边界；只有成功响应后才允许 Writer 继续。
                boundary_response = persist_stage(
                    side_effect_stage,
                    "server_persisted_before_writer_permit",
                )
                with state_lock:
                    stage_holder["confirmed"] = side_effect_stage
                    last_stage = side_effect_stage
                if observe_abort(boundary_response):
                    return
                with state_lock:
                    if abort_requested.is_set():
                        should_abort = True
                    else:
                        should_abort = False
                        if process.stdin is None:
                            raise BridgeError("无法向 Writer 发放副作用许可")
                        stage_holder["permit_state"] = "sending"
                        try:
                            process.stdin.write(SIDE_EFFECT_PERMIT + "\n")
                            process.stdin.flush()
                        except Exception:
                            # A partial pipe write is not proof that the Writer
                            # consumed the permit.  Stop it, while retaining the
                            # persisted boundary for conservative reconciliation.
                            stage_holder["permit_state"] = "delivery_uncertain"
                            raise
                        stage_holder["permit_state"] = "sent"
                if should_abort:
                    terminate_before_side_effect()
                return

            with state_lock:
                already_confirmed = stage_holder["confirmed"]
            if (
                local_stage == side_effect_stage
                and already_confirmed == side_effect_stage
            ):
                # Writer 获许可后会回显实际开始；边界已先行持久化。
                return
            response = persist_stage(local_stage)
            with state_lock:
                stage_holder["confirmed"] = furthest_stage(
                    stage_holder["confirmed"],
                    local_stage,
                    progress_stages=progress_stages,
                )
                last_stage = stage_holder["confirmed"]
            observe_abort(response)
        except Exception as exc:  # noqa: BLE001
            stage_report_error = exc
            print(
                f"阶段回报失败，已按副作用边界停止/收口: {exc}",
                file=sys.stderr,
            )
            terminate_before_side_effect()

    event_decoder = WriterEventDecoder()
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        stdout_tail.append(line)
        stdout_tail = stdout_tail[-200:]
        try:
            events = event_decoder.feed(raw_line)
        except BridgeError as exc:
            if stage_report_error is None:
                stage_report_error = exc
            print(f"Writer 输出协议错误: {exc}", file=sys.stderr)
            terminate_before_side_effect()
            continue
        for event in events:
            if "stage" in event:
                raw_stage = event["stage"]
                if operation_type in {
                    LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
                    LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
                }:
                    if not isinstance(raw_stage, str):
                        stage_report_error = BridgeError(
                            "FinalEntry Writer 阶段名称必须是字符串"
                        )
                        terminate_before_side_effect()
                        continue
                    if raw_stage == "reconciliation_required":
                        # outcome only; never persist it as a progress checkpoint.
                        continue
                    ignored_stages = (
                        GENERIC_FINAL_ENTRY_IGNORED_RAW_STAGES
                        if operation_type == LEGACY_GENERIC_FINAL_ENTRY_OPERATION
                        else FINAL_ENTRY_IGNORED_RAW_STAGES
                    )
                    raw_stage_map = (
                        GENERIC_FINAL_ENTRY_RAW_STAGE_MAP
                        if operation_type == LEGACY_GENERIC_FINAL_ENTRY_OPERATION
                        else FINAL_ENTRY_RAW_STAGE_MAP
                    )
                    if raw_stage in ignored_stages:
                        print(f"FinalEntry 本地阶段: {raw_stage}")
                        continue
                    canonical_stage = raw_stage_map.get(raw_stage)
                    if canonical_stage is None:
                        stage_report_error = BridgeError(
                            f"FinalEntry Writer 返回未知阶段: {raw_stage}"
                        )
                        terminate_before_side_effect()
                        continue
                    if canonical_stage == "completed":
                        # Writer stdout 不是可信的成功回执。只有 raw receipt 完整通过
                        # 严格校验后，才向服务端提交 completed 检查点。
                        if operation_type == LEGACY_GENERIC_FINAL_ENTRY_OPERATION:
                            generic_final_entry_completed_seen = True
                        else:
                            final_entry_completed_seen = True
                        continue
                    report_writer_stage(canonical_stage)
                elif (
                    operation_type in {
                        LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
                        LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
                    }
                    and raw_stage == "completed"
                ):
                    # Like FinalEntry, do not persist completed until the raw
                    # receipt proves the exact upload main-row identity.
                    special_wool_review_completed_seen = True
                elif (
                    operation_type
                    == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
                    and raw_stage == "completed"
                ):
                    qualitative_upload_completed_seen = True
                else:
                    report_writer_stage(raw_stage)
            if "receipt" in event:
                receipt = event["receipt"]
    try:
        event_decoder.finish()
    except BridgeError as exc:
        if stage_report_error is None:
            stage_report_error = exc
        print(f"Writer 输出协议错误: {exc}", file=sys.stderr)
        terminate_before_side_effect()
    exit_code = process.wait()
    stop_heartbeat.set()
    heartbeat.join(timeout=5)

    summary_text = "\n".join(stdout_tail)[-4000:]
    if (
        operation_type == LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION
        and exit_code == 0
        and stage_report_error is None
    ):
        try:
            if receipt is None:
                raise BridgeError("FinalEntry Writer 未输出 raw receipt")
            if not final_entry_completed_seen:
                raise BridgeError("FinalEntry Writer 未输出 completed 阶段")
            converted_receipt = convert_final_entry_receipt(
                operation,
                summary,
                final_entry_payload,
                final_entry_source,
                receipt,
            )
            # completed 必须晚于 raw receipt 校验，并早于 complete API。
            report_writer_stage("completed")
        except BridgeError as exc:
            receipt_validation_error = exc
            print(f"FinalEntry 成功回执已拒绝: {exc}", file=sys.stderr)

    if (
        operation_type == LEGACY_GENERIC_FINAL_ENTRY_OPERATION
        and exit_code == 0
        and stage_report_error is None
    ):
        try:
            if receipt is None:
                raise BridgeError("Generic FinalEntry Writer 未输出 raw receipt")
            if not generic_final_entry_completed_seen:
                raise BridgeError("Generic FinalEntry Writer 未输出 completed 阶段")
            converted_receipt = convert_generic_final_entry_receipt(
                operation,
                summary,
                final_entry_payload,
                receipt,
            )
            report_writer_stage("completed")
        except BridgeError as exc:
            receipt_validation_error = exc
            print(f"Generic FinalEntry 成功回执已拒绝: {exc}", file=sys.stderr)

    if (
        operation_type == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
        and exit_code == 0
        and stage_report_error is None
    ):
        try:
            if receipt is None:
                raise BridgeError("文档型 SpecialWool Writer 未输出 raw receipt")
            if not qualitative_upload_completed_seen:
                raise BridgeError("文档型 SpecialWool Writer 未输出 completed 阶段")
            validate_special_wool_qualitative_upload_receipt(
                operation, summary, receipt
            )
            report_writer_stage("completed")
        except BridgeError as exc:
            receipt_validation_error = exc
            print(f"文档型 SpecialWool 上传回执已拒绝: {exc}", file=sys.stderr)

    if (
        operation_type in {
            LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
            LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
        }
        and exit_code == 0
        and stage_report_error is None
    ):
        try:
            if receipt is None:
                raise BridgeError("SpecialWool review Writer 未输出 raw receipt")
            if not special_wool_review_completed_seen:
                raise BridgeError("SpecialWool review Writer 未输出 completed 阶段")
            validate_special_wool_review_receipt(operation, summary, receipt)
            report_writer_stage("completed")
        except BridgeError as exc:
            receipt_validation_error = exc
            print(f"SpecialWool 复核成功回执已拒绝: {exc}", file=sys.stderr)

    completion_receipt = (
        converted_receipt
        if operation_type in {
            LEGACY_MICROSCOPY_FINAL_ENTRY_OPERATION,
            LEGACY_GENERIC_FINAL_ENTRY_OPERATION,
        }
        else receipt
    )
    if (
        completion_receipt
        and not completion_receipt.get("reconciliation_required")
        and not completion_receipt.get("error")
        and exit_code == 0
        and stage_report_error is None
        and receipt_validation_error is None
    ):
        api_request(
            args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/complete",
            {
                "bridge_id": args.bridge_id,
                "receipt": completion_receipt,
                "stdout_summary": summary_text,
            },
        )
        print("写入完成并已回报")
    else:
        fail_stage = last_stage or "authenticated"
        with state_lock:
            permit_sent = stage_holder["permit_state"] == "sent"
        if abort_requested.is_set() and not permit_sent:
            error_code = "abort_acknowledged"
            error_message = "收到取消请求，Writer 在副作用许可前停止"
        elif receipt_validation_error is not None:
            error_code = (
                "special_wool_review_receipt_rejected"
                if operation_type in {
                    LEGACY_SPECIAL_WOOL_REVIEW_OPERATION,
                    LEGACY_SPECIAL_WOOL_QUALITATIVE_REVIEW_OPERATION,
                }
                else (
                    "special_wool_qualitative_upload_receipt_rejected"
                    if operation_type
                    == LEGACY_SPECIAL_WOOL_QUALITATIVE_UPLOAD_OPERATION
                    else "final_entry_receipt_rejected"
                )
            )
            error_message = str(receipt_validation_error)
        elif stage_report_error is not None:
            error_code = "bridge_stage_persistence_failed"
            error_message = str(stage_report_error)
        else:
            error_code = (receipt or {}).get("error", f"exit_{exit_code}")
            error_message = summary_text[-1800:]
        api_request(
            args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/fail",
            {
                "bridge_id": args.bridge_id,
                "stage": fail_stage,
                "error_code": error_code,
                "message": error_message,
            },
        )
        print(f"写入失败/待对账: stage={fail_stage} exit={exit_code}")
    try:
        os.unlink(package_file.name)
    except OSError:
        pass
    return "claimed"


if __name__ == "__main__":
    sys.exit(main())
