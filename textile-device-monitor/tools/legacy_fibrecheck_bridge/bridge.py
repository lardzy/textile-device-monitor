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
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

SIDE_EFFECT_STAGE = "file_copy_started"
FINAL_STAGES = {"completed"}


class BridgeError(Exception):
    pass


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧检务系统集中式 Bridge（单任务版）")
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--token-env", default="EXECUTION_BRIDGE_TOKEN")
    parser.add_argument("--writer", required=True, help="FibreCheckWriter.exe 路径")
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
    claim = api_request(
        args.api_base,
        token,
        "POST",
        "/external-bridge/claim",
        {"bridge_id": args.bridge_id},
    )
    if not claim.get("claimed"):
        print("没有待领取的旧系统操作")
        return "idle"

    attempt = claim["attempt"]
    operation = claim["operation"]
    attempt_id = attempt["id"]
    summary = operation.get("request_summary") or {}
    files = summary.get("files") or []
    if len(files) != 1:
        api_request(
            args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/fail",
            {"bridge_id": args.bridge_id, "stage": "authenticated",
             "error_code": "unsupported_file_count", "message": "首版仅支持单文件操作"},
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

    print(f"领取成功: target={summary.get('target_sample_number')} attempt={attempt_id}")
    package_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False, prefix="bridge-package-")
    json.dump(claim, package_file, ensure_ascii=False, indent=2)
    package_file.close()

    env = dict(os.environ)
    env["FIBRECHECK_RUNNER_PASSWORD"] = password

    stop_heartbeat = threading.Event()
    stage_holder = {"stage": None}

    def heartbeat_loop():
        while not stop_heartbeat.wait(30.0):
            try:
                response = api_request(
                    args.api_base, token, "POST",
                    f"/external-bridge/attempts/{attempt_id}/heartbeat",
                    {"bridge_id": args.bridge_id, "stage": stage_holder["stage"]},
                )
                if response.get("abort_requested"):
                    stop_heartbeat.set()
            except Exception as exc:  # noqa: BLE001
                print(f"心跳失败: {exc}", file=sys.stderr)

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()

    process = subprocess.Popen(
        [
            args.writer,
            "--fibrecheck-dir", args.fibrecheck_dir,
            "--account", account,
            "--execute-upload",
            "--package", package_file.name,
            "--source-root", source_root,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    receipt: dict | None = None
    last_stage = None
    stdout_tail: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        stdout_tail.append(line)
        stdout_tail = stdout_tail[-200:]
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "stage" in event:
            last_stage = event["stage"]
            stage_holder["stage"] = last_stage
            print(f"阶段: {last_stage}")
            try:
                api_request(
                    args.api_base, token, "POST",
                    f"/external-bridge/attempts/{attempt_id}/stage",
                    {"bridge_id": args.bridge_id, "stage": last_stage},
                )
            except Exception as exc:  # noqa: BLE001
                print(f"阶段回报失败: {exc}", file=sys.stderr)
        if "receipt" in event:
            receipt = event["receipt"]
    exit_code = process.wait()
    stop_heartbeat.set()
    heartbeat.join(timeout=5)

    summary_text = "\n".join(stdout_tail)[-4000:]
    if receipt and not receipt.get("reconciliation_required") and not receipt.get("error") and exit_code == 0:
        api_request(
            args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/complete",
            {"bridge_id": args.bridge_id, "receipt": receipt, "stdout_summary": summary_text},
        )
        print("写入完成并已回报")
    else:
        fail_stage = last_stage or "authenticated"
        api_request(
            args.api_base, token, "POST", f"/external-bridge/attempts/{attempt_id}/fail",
            {
                "bridge_id": args.bridge_id,
                "stage": fail_stage,
                "error_code": (receipt or {}).get("error", f"exit_{exit_code}"),
                "message": summary_text[-1800:],
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
