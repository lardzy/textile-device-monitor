"""Local task boundaries for an installed Windows Bridge. No network access."""
from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from pathlib import Path


def atomic_record(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", delete=False, mode="w", encoding="utf-8"
    ) as f:
        temp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        # Concurrent read-only status views on Windows may deny delete-sharing briefly.
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.025)
    finally:
        temp.unlink(missing_ok=True)


class ManagedCycle:
    def __init__(self, root: str, revision: int, kind: str, *, recover=None,
                 context: dict | None = None, secrets=()):
        self.root = Path(root)
        self.revision = revision
        self.kind = kind
        self.authenticated = False
        self.unknown = False
        self.last_state = None
        self.status_path = self.root / "runtime" / (kind + ".json")
        self.pending = self.root / "runtime" / (kind + ".pending.json")
        self.recover = recover
        self.context = context or {}
        self.secrets = tuple(value for value in secrets if value)
        self.writer_process = None

    def status(self, state: str, error: str = "") -> None:
        record = dict(State=state, Revision=self.revision, Authenticated=self.authenticated,
                      LastError=error, Pid=os.getpid())
        if self.last_state != record:
            atomic_record(self.status_path, record)
            self.last_state = record

    def boundary(self) -> str:
        if self.pending.exists() and self.recover is None:
            return "receipt_unknown"
        if (self.root / "config" / "maintenance.lock").exists():
            return "draining"
        settings = json.loads((self.root / "config/settings.json").read_text(encoding="utf-8-sig"))
        if not settings["enabled"][self.kind]:
            return "stopped"
        if settings["revision"] != self.revision:
            return "applying"
        if self.pending.exists():
            return "recovering"
        return "ready"

    def read_pending(self) -> dict:
        return json.loads(self.pending.read_text(encoding="utf-8-sig"))

    def redact(self, value):
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
        return value

    def remember_failure(self, failure: dict) -> None:
        record = self.read_pending()
        record["Failure"] = self.redact(failure)
        atomic_record(self.pending, record)

    def remember_success(self, receipt: dict, stage: str | None, stdout_summary: str) -> None:
        record = self.read_pending()
        record["Success"] = self.redact({"receipt": receipt, "stage": stage,
                                         "stdout_summary": stdout_summary})
        atomic_record(self.pending, record)

    def remember_exception(self, exc: Exception) -> None:
        record = self.read_pending()
        detail = self.redact({"exception_type": type(exc).__name__, "message": str(exc),
                              "stack_trace": "".join(traceback.format_exception(exc))})
        failure = record.setdefault("Failure", {"error_code": "bridge_interrupted", "message": detail["message"]})
        if failure.get("diagnostics"):
            failure["diagnostics"]["bridge_error"] = detail
        else:
            failure["diagnostics"] = detail
        atomic_record(self.pending, record)

    def writer_started(self, process) -> None:
        self.writer_process = process
        record = self.read_pending()
        record["ProcessExited"] = False
        record["WriterPid"] = getattr(process, "pid", None)
        atomic_record(self.pending, record)

    def writer_exited(self) -> None:
        self.writer_process = None
        record = self.read_pending()
        record["ProcessExited"] = True
        atomic_record(self.pending, record)

    def finish_pending(self, acknowledgement: dict | None = None) -> None:
        if not self.pending.exists():
            return
        record = self.read_pending()
        if record.get("Failure") or record.get("Success") or acknowledgement is not None:
            history = self.root / "runtime" / "receipts"
            history.mkdir(exist_ok=True)
            identifier = "".join(c for c in str(record.get("AttemptId", "unknown")) if c.isalnum() or c in "-_")
            atomic_record(history / f"{self.kind}-{identifier}.json",
                          {"pending": record, "handoff": acknowledgement})
        self.pending.unlink(missing_ok=True)
        self.unknown = False

    def retry_wait(self, poll_seconds: float) -> None:
        deadline = time.monotonic() + max(poll_seconds, 0.1)
        while self.boundary() in ("ready", "recovering") and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def recover_pending(self) -> bool:
        # A failed HTTP call must not detach a Writer which is still executing.
        if self.writer_process is not None:
            if self.writer_process.poll() is None:
                self.status("recovering", "等待当前 Writer 退出后回报结果")
                return False
            self.writer_exited()
        self.status("recovering", "正在恢复上一项回执，恢复后继续接单")
        response = self.recover(self.read_pending())
        if not response.get("released"):
            return False
        self.authenticated = True
        self.finish_pending({"reason": response.get("reason"),
                             "operation_status": (response.get("operation") or {}).get("status")})
        self.status("ready")
        return True

    def claim(self, identifier: str = "", operation_id: str = "") -> None:
        self.authenticated = True
        if identifier:
            atomic_record(self.pending, dict(AttemptId=identifier, OperationId=operation_id,
                                             Revision=self.revision, ProcessExited=True, **self.context))
            self.status("working")
        else:
            self.status("ready")

    def run(self, cycle, poll_seconds: float, once: bool = False) -> int:
        while True:
            boundary = self.boundary()
            if boundary == "recovering":
                try:
                    recovered = self.recover_pending()
                except Exception as exc:
                    self.status("recovering", "回执回报暂未成功，正在自动重试（" + type(exc).__name__ + "）")
                    recovered = False
                if not recovered:
                    if once:
                        return 22
                    self.retry_wait(poll_seconds)
                    continue
                boundary = self.boundary()
            if boundary != "ready":
                self.status(boundary)
                return 22 if boundary == "receipt_unknown" else 20
            try:
                result = cycle()
            except Exception as exc:
                # The API exception body may contain private details. Only expose its class.
                self.unknown = self.pending.exists()
                if self.unknown and self.recover is not None:
                    self.remember_exception(exc)
                    if once:
                        try:
                            return 0 if self.recover_pending() else 22
                        except Exception:
                            self.status("recovering", "回执回报暂未成功，将自动重试")
                            return 22
                    continue
                cause = exc.__cause__ or exc
                code = getattr(cause, "code", None)
                detail = f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
                self.status("receipt_unknown" if self.unknown else "error",
                            "待核验回执" if self.unknown else f"连接或执行失败 ({detail})，请检查地址、令牌和网络")
                return 22 if self.unknown else 21
            if self.unknown:
                if self.recover is not None:
                    continue
                self.status("receipt_unknown", "回执未确认，请在执行系统对账")
                return 22
            self.finish_pending()
            if once:
                if self.kind == "write":
                    return 0 if result == "claimed" else 1
                return 0 if result in ("idle", "completed") else 1
            if result in ("claimed", "completed"):
                continue
            self.retry_wait(poll_seconds)
