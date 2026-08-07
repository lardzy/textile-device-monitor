#!/usr/bin/env python3
"""Local, ignored helper for the authorized 260111037 integration run.

The helper reads the existing ignored environment file and never prints
credentials.  It intentionally exposes only the narrow preparation actions
needed before the real legacy-system run.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import os
from pathlib import Path

import requests


_FILE_PARENTS = Path(__file__).resolve().parents
ROOT = Path(
    os.environ.get(
        "EXECUTION_HELPER_ROOT",
        str(_FILE_PARENTS[2] if len(_FILE_PARENTS) > 2 else Path("/")),
    )
)
ENV_PATH = ROOT / ".tmp" / "execution-system-local-runtime" / "local.env"
API_BASE = os.environ.get(
    "EXECUTION_API_BASE",
    "http://127.0.0.1/api/execution/v1",
).rstrip("/")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def checked(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API {response.request.method} {response.url} returned "
            f"HTTP {response.status_code} without JSON"
        ) from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"API {response.request.method} {response.url} -> "
            f"{response.status_code}: {json.dumps(payload, ensure_ascii=False)}"
        )
    return payload


def login() -> tuple[requests.Session, str]:
    env = load_env(ENV_PATH) if ENV_PATH.exists() else {}
    username = os.environ.get(
        "EXECUTION_BOOTSTRAP_ADMIN_USERNAME",
        env.get("EXECUTION_BOOTSTRAP_ADMIN_USERNAME", ""),
    )
    password = os.environ.get(
        "EXECUTION_BOOTSTRAP_ADMIN_PASSWORD",
        env.get("EXECUTION_BOOTSTRAP_ADMIN_PASSWORD", ""),
    )
    if not username or not password:
        raise RuntimeError("execution bootstrap administrator is not configured")
    session = requests.Session()
    session.trust_env = False
    payload = checked(
        session.post(
            f"{API_BASE}/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
    )
    return session, str(payload["csrf_token"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "set-display-name",
            "snapshot-refresh",
            "snapshot-status",
            "workflow",
            "create-run",
            "run-status",
            "tasks",
            "task-detail",
            "submit-task",
            "operations",
            "approve-operation",
            "reconcile-no-side-effect",
            "reconcile-final-entry-no-side-effect",
            "reconciliation-context",
            "reconcile-confirm-completed",
            "retry-node",
        ),
    )
    parser.add_argument("--run-id")
    parser.add_argument("--task-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--node-id")
    parser.add_argument("--expected-operation-type")
    parser.add_argument("--data-file", type=Path)
    parser.add_argument(
        "--idempotency-key",
        default="authorized-microscopy-260111037-20260805-01",
    )
    args = parser.parse_args()
    session, csrf = login()

    if args.command == "set-display-name":
        users = checked(session.get(f"{API_BASE}/users", timeout=15))["items"]
        matches = [item for item in users if item.get("username") == "localadmin"]
        if len(matches) != 1:
            raise RuntimeError("localadmin account is not unique")
        payload = checked(
            session.patch(
                f"{API_BASE}/users/{matches[0]['id']}",
                json={"display_name": "李舒洋"},
                headers={"X-CSRF-Token": csrf},
                timeout=15,
            )
        )
        print(
            json.dumps(
                {
                    "username": payload.get("username"),
                    "display_name": payload.get("display_name"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "workflow":
        items = checked(session.get(f"{API_BASE}/workflows", timeout=15))["items"]
        matches = [
            item
            for item in items
            if item.get("slug") == "electron-microscopy-gbt36422"
        ]
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return 0

    if args.command == "create-run":
        items = checked(session.get(f"{API_BASE}/workflows", timeout=15))["items"]
        matches = [
            item
            for item in items
            if item.get("slug") == "electron-microscopy-gbt36422"
        ]
        if len(matches) != 1:
            raise RuntimeError("electron microscopy workflow is not unique")
        payload = checked(
            session.post(
                f"{API_BASE}/runs",
                json={
                    "workflow_id": matches[0]["id"],
                    "inspection_number": "260111037",
                    "input_data": {
                        "inspection_number": "260111037",
                        "controlled_test_override": {
                            "kind": "append_one_when_check_count_one",
                            "target_sample_number": "260111037",
                            "expected_task_check_count": 1,
                            "expected_existing_register_count": 1,
                            "resulting_register_count": 2,
                            "reason": (
                                "260111037 首次全链路受控验证；"
                                "保留既有记录并追加一条"
                            ),
                        },
                    },
                    "global_data": {},
                    "idempotency_key": args.idempotency_key,
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0


    if args.command in {"run-status", "tasks", "operations"}:
        if not args.run_id:
            raise RuntimeError("--run-id is required")
        if args.command == "run-status":
            payload = checked(
                session.get(f"{API_BASE}/runs/{args.run_id}", timeout=20)
            )
        elif args.command == "operations":
            payload = checked(
                session.get(
                    f"{API_BASE}/runs/{args.run_id}/external-operations",
                    timeout=20,
                )
            )
        else:
            items = []
            for status in ("open", "claimed"):
                items.extend(
                    checked(
                        session.get(
                            f"{API_BASE}/human-tasks",
                            params={"status": status},
                            timeout=20,
                        )
                    )["items"]
                )
            payload = {
                "items": [
                    item for item in items if item.get("run_id") == args.run_id
                ]
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "task-detail":
        if not args.task_id:
            raise RuntimeError("--task-id is required")
        payload = checked(
            session.get(f"{API_BASE}/human-tasks/{args.task_id}", timeout=20)
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "submit-task":
        if not args.task_id or not args.data_file:
            raise RuntimeError("--task-id and --data-file are required")
        values = json.loads(args.data_file.read_text(encoding="utf-8"))
        detail = checked(
            session.get(f"{API_BASE}/human-tasks/{args.task_id}", timeout=20)
        )
        task = detail["task"]
        if task["status"] == "open":
            task = checked(
                session.post(
                    f"{API_BASE}/human-tasks/{args.task_id}/claim",
                    json={"revision": task["revision"]},
                    headers={"X-CSRF-Token": csrf},
                    timeout=20,
                )
            )
        if task["status"] != "claimed":
            raise RuntimeError(f"task is not claimable: {task['status']}")
        payload = checked(
            session.post(
                f"{API_BASE}/human-tasks/{args.task_id}/submit",
                json={"revision": task["revision"], "data": values},
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "approve-operation":
        if not args.operation_id or not args.expected_operation_type:
            raise RuntimeError(
                "--operation-id and --expected-operation-type are required"
            )
        operation = checked(
            session.get(
                f"{API_BASE}/external-operations/{args.operation_id}",
                timeout=20,
            )
        )
        actual_type = (operation.get("request_summary") or {}).get(
            "operation_type"
        )
        if operation.get("status") != "prepared":
            raise RuntimeError(
                f"operation is not prepared: {operation.get('status')}"
            )
        if actual_type != args.expected_operation_type:
            raise RuntimeError(
                f"operation type mismatch: {actual_type!r}"
            )
        target = (operation.get("request_summary") or {}).get(
            "target_sample_number"
        )
        payload = checked(
            session.post(
                f"{API_BASE}/external-operations/{args.operation_id}/approve",
                json={
                    "approved": True,
                    "payload_checksum": operation["payload_checksum"],
                    "confirmed_sample_number": target,
                    "note": (
                        "260111037 首次受控全链路验证；"
                        "已核对项目、目标编号、目标文件名与只读快照。"
                    ),
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reconcile-no-side-effect":
        if not args.operation_id:
            raise RuntimeError("--operation-id is required")
        context = checked(
            session.get(
                f"{API_BASE}/external-operations/{args.operation_id}/reconciliation",
                timeout=20,
            )
        )
        operation = context["operation"]
        attempt = context["attempt"]
        summary = operation.get("request_summary") or {}
        if operation.get("status") != "reconciliation_required":
            raise RuntimeError("operation is not awaiting reconciliation")
        if summary.get("operation_type") != "legacy_special_wool_image_upload":
            raise RuntimeError("unexpected operation type")
        if summary.get("target_sample_number") != "260111037-1":
            raise RuntimeError("unexpected target sample number")
        if attempt.get("current_stage") != "file_copy_started":
            raise RuntimeError("unexpected reconciliation boundary")
        payload = checked(
            session.post(
                f"{API_BASE}/external-operations/{args.operation_id}/reconcile",
                json={
                    "action": "confirm_no_side_effect",
                    "attempt_id": attempt["id"],
                    "payload_checksum": operation["payload_checksum"],
                    "confirmed_sample_number": "260111037-1",
                    "note": (
                        "Oracle 只读探针确认目标主记录和图片子记录均为 0；"
                        "Windows WNet 只读核对确认目标文件计数为 0。"
                    ),
                    "evidence": {
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "exact_record_count": 0,
                        "contains_record_count": 0,
                        "target_file_count": 0,
                    },
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reconciliation-context":
        if not args.operation_id:
            raise RuntimeError("--operation-id is required")
        payload = checked(
            session.get(
                f"{API_BASE}/external-operations/"
                f"{args.operation_id}/reconciliation",
                timeout=20,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reconcile-final-entry-no-side-effect":
        if not args.operation_id:
            raise RuntimeError("--operation-id is required")
        context = checked(
            session.get(
                f"{API_BASE}/external-operations/"
                f"{args.operation_id}/reconciliation",
                timeout=20,
            )
        )
        operation = context["operation"]
        attempt = context["attempt"]
        summary = operation.get("request_summary") or {}
        expected = context.get("expected_evidence") or {}
        no_effect = expected.get("confirm_no_side_effect") or {}
        required = {
            "operation_id": "076052e4-76a7-4934-9512-cfacf3a4f488",
            "attempt_id": "b7e6d363-195d-44cf-9ef4-e6f425ebbeb9",
            "payload_checksum": (
                "be121c6649c3261ccc2459b4262d9c67b"
                "87371d827d1ac20deb96e9d480ab860"
            ),
            "target_sample_number": "260111037",
            "writer_stage": "excel_collection_started",
            "summary_checksum": (
                "c0135acc98030d485ff9117a1449bc2a"
                "20d88493ec6058bd07ad37787431a8b7"
            ),
        }
        if operation.get("id") != required["operation_id"]:
            raise RuntimeError("operation id mismatch")
        if operation.get("status") != "reconciliation_required":
            raise RuntimeError("operation is not awaiting reconciliation")
        if summary.get("operation_type") != (
            "legacy_microscopy_check_record_entry"
        ):
            raise RuntimeError("unexpected operation type")
        if summary.get("target_sample_number") != required[
            "target_sample_number"
        ]:
            raise RuntimeError("unexpected target sample number")
        if operation.get("payload_checksum") != required[
            "payload_checksum"
        ]:
            raise RuntimeError("payload checksum mismatch")
        if attempt.get("id") != required["attempt_id"]:
            raise RuntimeError("attempt id mismatch")
        if attempt.get("status") != "failed":
            raise RuntimeError("attempt is not failed")
        if attempt.get("current_stage") != required["writer_stage"]:
            raise RuntimeError("unexpected writer stage")
        if expected.get("evidence_contract") != (
            "microscopy_final_entry_v1"
        ):
            raise RuntimeError("unexpected evidence contract")
        if expected.get("final_entry_summary_checksum") != required[
            "summary_checksum"
        ]:
            raise RuntimeError("final-entry summary checksum mismatch")
        expected_counts = {
            "expected_existing_register_count": 1,
            "actual_register_count": 1,
            "actual_file_reference_count": 1,
            "actual_key_result_count": 1,
            "actual_proofed_count": 1,
            "target_file_count": 0,
            "writer_stage": required["writer_stage"],
        }
        for key, value in expected_counts.items():
            if no_effect.get(key) != value:
                raise RuntimeError(f"unexpected expected evidence: {key}")
        payload = checked(
            session.post(
                f"{API_BASE}/external-operations/"
                f"{args.operation_id}/reconcile",
                json={
                    "action": "confirm_no_side_effect",
                    "attempt_id": attempt["id"],
                    "payload_checksum": operation["payload_checksum"],
                    "confirmed_sample_number": "260111037",
                    "note": (
                        "失败后 Oracle 只读探针确认登记、文件引用、"
                        "关键结果和已校对计数均保持 1；只读 SMB 精确搜索"
                        "确认目标 GUID 文件不存在；Writer 停在"
                        " excel_collection_started，未进入远端复制或保存。"
                    ),
                    "evidence": {
                        "evidence_contract": (
                            "microscopy_final_entry_v1"
                        ),
                        "checked_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "final_entry_summary_checksum": required[
                            "summary_checksum"
                        ],
                        **expected_counts,
                    },
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reconcile-confirm-completed":
        if not args.operation_id or not args.data_file:
            raise RuntimeError("--operation-id and --data-file are required")
        receipt = json.loads(args.data_file.read_text(encoding="utf-8"))
        context = checked(
            session.get(
                f"{API_BASE}/external-operations/"
                f"{args.operation_id}/reconciliation",
                timeout=20,
            )
        )
        operation = context["operation"]
        attempt = context["attempt"]
        summary = operation.get("request_summary") or {}
        expected_filename = (
            "260111037-1-39-8B-"
            "纤维形状截面定量试验-2026.xls"
        )
        if operation.get("status") != "reconciliation_required":
            raise RuntimeError("operation is not awaiting reconciliation")
        if operation.get("id") != args.operation_id:
            raise RuntimeError("operation id mismatch")
        if summary.get("operation_type") != "legacy_special_wool_image_upload":
            raise RuntimeError("unexpected operation type")
        if summary.get("target_sample_number") != "260111037-1":
            raise RuntimeError("unexpected target sample number")
        if summary.get("target_filename") != expected_filename:
            raise RuntimeError("unexpected target filename")
        if attempt.get("id") != "b3759be9-f541-4213-a13b-c58042d2199b":
            raise RuntimeError("unexpected attempt id")
        if attempt.get("status") != "failed":
            raise RuntimeError("unexpected attempt status")
        if receipt.get("operation_id") != operation.get("id"):
            raise RuntimeError("receipt operation mismatch")
        if receipt.get("payload_checksum") != operation.get(
            "payload_checksum"
        ):
            raise RuntimeError("receipt payload mismatch")
        if receipt.get("target_sample_number") != "260111037-1":
            raise RuntimeError("receipt sample mismatch")
        if receipt.get("target_filename") != expected_filename:
            raise RuntimeError("receipt filename mismatch")
        server_file = receipt.get("server_file") or {}
        verification = server_file.get("verification") or {}
        if verification.get("mode") != "cfb_biff_writeaccess_only":
            raise RuntimeError("unexpected server file verification mode")
        if verification.get("changed_record_ids") != ["0x005C"]:
            raise RuntimeError("unexpected BIFF normalization")
        main_record = receipt.get("main_record") or {}
        payload = checked(
            session.post(
                f"{API_BASE}/external-operations/"
                f"{args.operation_id}/reconcile",
                json={
                    "action": "confirm_completed",
                    "attempt_id": attempt["id"],
                    "payload_checksum": operation["payload_checksum"],
                    "confirmed_sample_number": "260111037-1",
                    "note": (
                        "只读 Writer 已核对唯一主记录、唯一图片子记录、"
                        "最终文件名、项目和检验员；服务器工作簿仅发生 "
                        "BIFF WRITEACCESS(0x005C) 元数据规范化。"
                    ),
                    "evidence": {
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "exact_record_count": 1,
                        "remote_record_id": main_record.get("id"),
                        "business_fields_match": True,
                        "inspector_match": True,
                        "target_file_count": 1,
                        "remote_file_sha256": server_file.get(
                            "content_sha256"
                        ),
                        "receipt": receipt,
                    },
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "retry-node":
        if not args.run_id or not args.node_id:
            raise RuntimeError("--run-id and --node-id are required")
        payload = checked(
            session.post(
                f"{API_BASE}/runs/{args.run_id}/nodes/"
                f"{args.node_id}/retry",
                json={
                    "reason": (
                        "修复旧客户端固定附件模板分支兼容差异；"
                        "历史尝试均在远端写入边界前失败，"
                        "仅重新准备最终登记，不重复上传或复核"
                    )
                },
                headers={"X-CSRF-Token": csrf},
                timeout=30,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "snapshot-refresh":
        payload = checked(
            session.post(
                f"{API_BASE}/task-snapshots/260111037/refresh?force=true",
                headers={"X-CSRF-Token": csrf},
                timeout=15,
            )
        )
    else:
        payload = checked(
            session.get(
                f"{API_BASE}/task-snapshots/260111037/status",
                timeout=15,
            )
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
