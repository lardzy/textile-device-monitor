#!/usr/bin/env python3
"""Windows 旧检务系统任务快照 Bridge。

这个进程只负责领取待刷新的样品编号，调用现有 FibreCheck 严格只读探针，
并把最小任务快照提交给执行系统。它不直接连接执行系统数据库，也不执行任
何旧系统写入。
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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_TOKEN_ENV = "EXECUTION_BRIDGE_TOKEN"
DEFAULT_POLL_SECONDS = 15.0
# 后端默认领取租约为 180 秒；探针必须更早超时，才能给完成回传和
# 短暂网络抖动保留足够余量。
DEFAULT_PROBE_TIMEOUT_SECONDS = 90.0
TASK_SNAPSHOT_SCHEMA_VERSION = 5
MICROSCOPY_PROJECT_NAMES = frozenset({"纤维微观形貌", "膜平面形貌"})
PUBLIC_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{16}$")


def _special_wool_occupied_numbers(
    rows: list[dict[str, Any]], inspection_number: str
) -> list[str]:
    base_number = inspection_number.split("-", 1)[0]
    family_pattern = re.compile(
        re.escape(base_number) + r"(?:-([1-9][0-9]*))?$"
    )
    occupied: list[str] = []
    seen: set[str] = set()
    for row in rows:
        sample_number = str(row.get("SampleNo") or "").strip().upper()
        raw_count = row.get("RecordCount")
        try:
            record_count = int(raw_count)
        except (TypeError, ValueError):
            record_count = -1
        if (
            isinstance(raw_count, bool)
            or record_count < 0
            or str(raw_count).strip() != str(record_count)
        ):
            raise SnapshotBridgeError(
                "probe_special_wool_family_invalid",
                "特种毛编号族查询返回了无效记录数",
            )
        if not family_pattern.fullmatch(sample_number):
            raise SnapshotBridgeError(
                "probe_special_wool_family_invalid",
                "特种毛编号族查询返回了当前任务以外的编号",
            )
        if record_count > 0 and sample_number not in seen:
            occupied.append(sample_number)
            seen.add(sample_number)
    return occupied


class SnapshotBridgeError(Exception):
    """可安全回传给服务端的 Bridge 错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def api_request(
    api_base: str,
    token: str,
    method: str,
    path: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """调用 Bridge 专用 API，不把响应正文带入异常或日志。"""

    request = urllib.request.Request(
        api_base.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Execution-Bridge-Key": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # 响应正文可能含任务信息，禁止拼入异常。
        raise SnapshotBridgeError(
            "bridge_api_http_error",
            f"Bridge API 返回 HTTP {exc.code}",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SnapshotBridgeError(
            "bridge_api_unavailable",
            "Bridge API 暂时不可用",
        ) from exc

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotBridgeError(
            "bridge_api_invalid_response",
            "Bridge API 返回了无效响应",
        ) from exc
    if not isinstance(result, dict):
        raise SnapshotBridgeError(
            "bridge_api_invalid_response",
            "Bridge API 返回了无效响应",
        )
    return result


def _query_rows(
    results: Mapping[str, Any],
    key: str,
) -> list[dict[str, Any]]:
    state = results.get(key)
    if not isinstance(state, Mapping) or state.get("status") != "ok":
        raise SnapshotBridgeError(
            "probe_query_incomplete",
            "只读探针的必要查询未成功",
        )
    rows = state.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SnapshotBridgeError(
            "probe_query_invalid",
            "只读探针返回了无效查询结果",
        )
    row_count = state.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count != len(rows):
        raise SnapshotBridgeError(
            "probe_query_invalid",
            "只读探针返回了不一致的查询结果",
        )
    return rows


def _same_identifier(left: Any, right: Any) -> bool:
    """兼容 Oracle 驱动可能返回的数字/字符串 ID。"""

    if left is None or right is None:
        return False
    return str(left).strip() == str(right).strip()


def _compact_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _nonnegative_int(value: Any, *, error_code: str, message: str) -> int:
    if isinstance(value, bool):
        raise SnapshotBridgeError(error_code, message)
    try:
        text = str(value).strip()
        result = int(text)
    except (TypeError, ValueError):
        raise SnapshotBridgeError(error_code, message) from None
    if result < 0 or text != str(result):
        raise SnapshotBridgeError(error_code, message)
    return result


def _project_key(item: Mapping[str, Any]) -> str:
    """Build a stable public key without exposing FibreCheck record IDs."""

    identity = "\0".join(
        _compact_text(item.get(key))
        for key in (
            "ID",
            "CheckItemID",
            "CheckItemNo",
            "CheckItemName",
            "CheckMethod",
            "SeqNum",
        )
    )
    return "task-project:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _is_microscopy_project(item: Mapping[str, Any]) -> bool:
    return _compact_text(item.get("CheckItemName")) in MICROSCOPY_PROJECT_NAMES


def _public_identifier(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized if PUBLIC_ID_PATTERN.fullmatch(normalized) else None


def build_snapshot(
    document: Mapping[str, Any],
    inspection_number: str,
) -> dict[str, Any]:
    """校验探针文档并生成后端约定的最小快照。"""

    if document.get("mode") != "probe":
        raise SnapshotBridgeError("probe_mode_invalid", "探针未以只读查询模式运行")
    if document.get("sample_no") != inspection_number:
        raise SnapshotBridgeError(
            "probe_sample_mismatch",
            "探针结果与领取的样品编号不一致",
        )
    if document.get("connection_attempted") is not True:
        raise SnapshotBridgeError(
            "probe_connection_not_attempted",
            "探针没有建立只读查询连接",
        )
    if document.get("read_only_transaction_started") is not True:
        raise SnapshotBridgeError(
            "probe_read_only_not_confirmed",
            "探针没有确认只读事务",
        )

    results = document.get("results")
    if not isinstance(results, Mapping):
        raise SnapshotBridgeError("probe_results_invalid", "探针结果结构无效")
    tasks = _query_rows(results, "tasks")
    task_samples = _query_rows(results, "task_samples")
    task_items = _query_rows(results, "task_check_items")
    register_counts = _query_rows(results, "task_project_register_counts")
    occupied_numbers = _special_wool_occupied_numbers(
        _query_rows(results, "task_special_wool_family"),
        inspection_number,
    )

    # 旧系统中不存在对应 Task 是可缓存的正常事实，避免后端不断重复查询。
    if not tasks:
        if task_samples or task_items or register_counts:
            raise SnapshotBridgeError(
                "probe_task_link_invalid",
                "任务不存在但返回了样品或任务项目",
            )
        return {
            "schema_version": TASK_SNAPSHOT_SCHEMA_VERSION,
            "sample_name": None,
            "sample_names": [],
            "check_basis": None,
            "projects": [],
            "special_wool_occupied_numbers": occupied_numbers,
        }
    if len(tasks) != 1:
        raise SnapshotBridgeError(
            "probe_task_not_unique",
            "同一样品编号返回了多个任务主记录",
        )

    task = tasks[0]
    if task.get("ReportNo") != inspection_number:
        raise SnapshotBridgeError(
            "probe_task_sample_mismatch",
            "任务主记录编号与领取编号不一致",
        )
    task_id = task.get("ID")
    if task_id is None:
        raise SnapshotBridgeError("probe_task_id_missing", "任务主记录缺少标识")

    sample_names: list[str] = []
    seen_sample_names: set[str] = set()
    for sample in task_samples:
        if not _same_identifier(sample.get("TaskID"), task_id):
            continue
        sample_name = _compact_text(sample.get("SampleName"))
        folded = sample_name.casefold()
        if sample_name and folded not in seen_sample_names:
            sample_names.append(sample_name)
            seen_sample_names.add(folded)

    count_by_project: dict[tuple[str, str], int] = {}
    for row in register_counts:
        key = (
            str(row.get("TaskCheckItemID") or "").strip(),
            str(row.get("CheckItemID") or "").strip(),
        )
        if not key[1]:
            # 套餐/分组标题行没有 CheckItemID 绑定。登记记录按 CheckItemID
            # 关联，NULL 永远关联不上（计数恒为 0），此类行不是可登记项目，
            # 直接跳过，不参与绑定唯一性校验。
            continue
        if not key[0] or key in count_by_project:
            raise SnapshotBridgeError(
                "probe_project_register_count_invalid",
                "任务项目登记数量查询返回了重复或无效的项目绑定",
            )
        count_by_project[key] = _nonnegative_int(
            row.get("RegisterCount"),
            error_code="probe_project_register_count_invalid",
            message="任务项目登记数量查询返回了无效计数",
        )

    projects = []
    consumed_count_keys: set[tuple[str, str]] = set()
    for item in task_items:
        # 探针查询已由 Task join 限定；仍再次按 TaskID 过滤，避免把其它任务的
        # 项目误写进当前编号的缓存。ID 可能已由探针做脱敏，故只做可比时过滤。
        item_task_id = item.get("TaskID")
        if not _same_identifier(item_task_id, task_id):
            continue
        task_check_item_id = _public_identifier(item.get("ID"))
        check_item_id = _public_identifier(item.get("CheckItemID"))
        if _is_microscopy_project(item) and not (
            task_check_item_id and check_item_id
        ):
            raise SnapshotBridgeError(
                "probe_project_identity_missing",
                "纤维微观形貌任务项目缺少完整的脱敏项目标识",
            )
        if not str(item.get("CheckItemID") or "").strip():
            # 与登记计数循环同一规则：套餐/分组标题行没有 CheckItemID 绑定，
            # 不是可登记项目，不进入快照项目列表。
            continue
        count_key = (
            str(item.get("ID") or "").strip(),
            str(item.get("CheckItemID") or "").strip(),
        )
        if count_key not in count_by_project:
            raise SnapshotBridgeError(
                "probe_project_register_count_missing",
                "任务项目缺少当前检验记录登记数量",
            )
        consumed_count_keys.add(count_key)
        projects.append(
            {
                "project_key": _project_key(item),
                # 探针已对数据库内部 ID 做 sha256 脱敏。保留这两个稳定引用，
                # 让后续图片上传预检能证明“用户选择的项目”和 Windows 端
                # 再次只读解析到的 Task_CheckItem 是同一行，而不暴露原始 ID。
                "task_check_item_id": task_check_item_id,
                "check_item_id": check_item_id,
                "check_item_no": item.get("CheckItemNo"),
                "check_item_name": item.get("CheckItemName"),
                "check_method": item.get("CheckMethod"),
                "check_count": item.get("CheckCount"),
                "register_count": count_by_project[count_key],
                "seq_num": item.get("SeqNum"),
                "sample_identify": item.get("SampleIdentify"),
                "remark": item.get("Remark"),
                "give_judgement": item.get("GiveJudgement"),
            }
        )

    if consumed_count_keys != set(count_by_project):
        raise SnapshotBridgeError(
            "probe_project_register_count_orphaned",
            "任务项目登记数量查询返回了无法绑定的项目",
        )

    return {
        "schema_version": TASK_SNAPSHOT_SCHEMA_VERSION,
        # 只有唯一非空名称时给出无歧义快捷值；全量选项保留在
        # sample_names，供后续人工确认节点处理一任务多样品情况。
        "sample_name": sample_names[0] if len(sample_names) == 1 else None,
        "sample_names": sample_names,
        "check_basis": task.get("CheckBasis"),
        "projects": projects,
        "special_wool_occupied_numbers": occupied_numbers,
    }


def run_probe(args: argparse.Namespace, inspection_number: str) -> dict[str, Any]:
    """运行严格只读探针，并保证临时 JSON 在所有路径下都被删除。"""

    handle, output_name = tempfile.mkstemp(
        prefix="fibrecheck-task-snapshot-",
        suffix=".json",
    )
    os.close(handle)
    output_path = Path(output_name)
    try:
        command = [
            str(args.probe_python),
            str(args.probe_script),
            "--fibrecheck-dir",
            str(args.fibrecheck_dir),
            "--sample-no",
            inspection_number,
            "--output",
            str(output_path),
            "--task-snapshot-only",
        ]
        if args.oracle_client_dir:
            command.extend(["--oracle-client-dir", str(args.oracle_client_dir)])
        if args.credential_profile:
            command.extend(["--credential-profile", args.credential_profile])
        if args.data_source:
            command.extend(["--data-source", args.data_source])

        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.probe_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SnapshotBridgeError("probe_timeout", "只读探针执行超时") from exc
        except OSError as exc:
            raise SnapshotBridgeError("probe_start_failed", "无法启动只读探针") from exc
        if completed.returncode != 0:
            # stdout/stderr 可能含数据源或业务上下文，故不写日志也不回传。
            raise SnapshotBridgeError("probe_failed", "只读探针执行失败")

        try:
            with output_path.open("r", encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotBridgeError("probe_output_invalid", "无法读取只读探针结果") from exc
        if not isinstance(document, dict):
            raise SnapshotBridgeError("probe_output_invalid", "只读探针结果结构无效")
        return document
    finally:
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                output_path.unlink()
                last_error = None
                break
            except FileNotFoundError:
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05)
        if last_error is not None:
            # 不输出路径；清理失败时不得把含业务数据的临时文件当作成功处理。
            raise SnapshotBridgeError(
                "probe_temp_cleanup_failed",
                "无法清理只读探针临时结果",
            ) from last_error


def _fail_claim(
    args: argparse.Namespace,
    token: str,
    inspection_number: str,
    claim_token: str,
    error: SnapshotBridgeError,
    *,
    request: Callable[..., dict[str, Any]],
) -> None:
    try:
        request(
            args.api_base,
            token,
            "POST",
            f"/task-snapshot-bridge/{inspection_number}/fail",
            {
                "bridge_id": args.bridge_id,
                "claim_token": claim_token,
                "error_code": error.code,
                "message": error.message,
            },
        )
    except SnapshotBridgeError:
        # 原始错误已经决定本次失败；避免用回报失败覆盖它，也不泄漏响应内容。
        pass


def run_one_cycle(
    args: argparse.Namespace,
    token: str,
    *,
    request: Callable[..., dict[str, Any]] = api_request,
    probe: Callable[[argparse.Namespace, str], dict[str, Any]] = run_probe,
) -> str:
    claim = request(
        args.api_base,
        token,
        "POST",
        "/task-snapshot-bridge/claim",
        {"bridge_id": args.bridge_id},
    )
    if claim.get("claimed") is not True:
        print("没有待刷新的旧系统任务快照")
        return "idle"

    inspection_number = claim.get("inspection_number")
    claim_token = claim.get("claim_token")
    if not isinstance(inspection_number, str) or not inspection_number:
        raise SnapshotBridgeError("claim_invalid", "领取响应缺少样品编号")
    if not isinstance(claim_token, str) or not claim_token:
        raise SnapshotBridgeError("claim_invalid", "领取响应缺少领取令牌")

    print("已领取一个旧系统任务快照刷新请求")
    try:
        document = probe(args, inspection_number)
        snapshot = build_snapshot(document, inspection_number)
        request(
            args.api_base,
            token,
            "POST",
            f"/task-snapshot-bridge/{inspection_number}/complete",
            {
                "bridge_id": args.bridge_id,
                "claim_token": claim_token,
                "snapshot": snapshot,
            },
        )
    except SnapshotBridgeError as exc:
        _fail_claim(
            args,
            token,
            inspection_number,
            claim_token,
            exc,
            request=request,
        )
        print(f"旧系统任务快照刷新失败（{exc.code}）", file=sys.stderr)
        return "failed"

    print("旧系统任务快照已刷新")
    return "completed"


def build_parser() -> argparse.ArgumentParser:
    default_probe = Path(__file__).resolve().parents[1] / "legacy_fibrecheck_probe" / "probe.py"
    parser = argparse.ArgumentParser(description="旧检务系统任务快照只读 Bridge")
    parser.add_argument("--api-base", required=True, help="执行系统 API v1 地址")
    parser.add_argument("--bridge-id", required=True, help="当前 Bridge 的稳定实例编号")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--probe-python", default=sys.executable)
    parser.add_argument("--probe-script", default=str(default_probe))
    parser.add_argument("--fibrecheck-dir", required=True)
    parser.add_argument("--oracle-client-dir")
    parser.add_argument("--credential-profile")
    parser.add_argument("--data-source")
    parser.add_argument(
        "--probe-timeout-seconds",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT_SECONDS,
    )
    parser.add_argument("--once", action="store_true", help="只执行一次领取轮询")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"缺少 Bridge 令牌环境变量 {args.token_env}", file=sys.stderr)
        return 2
    if args.poll_seconds <= 0 or args.probe_timeout_seconds <= 0:
        print("轮询间隔和探针超时必须大于 0", file=sys.stderr)
        return 2

    while True:
        try:
            outcome = run_one_cycle(args, token)
        except SnapshotBridgeError as exc:
            print(f"任务快照 Bridge 请求失败（{exc.code}）", file=sys.stderr)
            outcome = "failed"
        if args.once:
            return 0 if outcome in {"idle", "completed"} else 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
