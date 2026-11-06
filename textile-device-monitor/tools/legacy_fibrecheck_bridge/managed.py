"""Local task boundaries for an installed Windows Bridge. No network access."""
from __future__ import annotations

import json
import os
import tempfile
import time
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
    def __init__(self, root: str, revision: int, kind: str):
        self.root = Path(root)
        self.revision = revision
        self.kind = kind
        self.authenticated = False
        self.unknown = False
        self.last_state = None
        self.status_path = self.root / "runtime" / (kind + ".json")
        self.pending = self.root / "runtime" / (kind + ".pending.json")

    def status(self, state: str, error: str = "") -> None:
        record = dict(State=state, Revision=self.revision, Authenticated=self.authenticated,
                      LastError=error, Pid=os.getpid())
        if self.last_state != record:
            atomic_record(self.status_path, record)
            self.last_state = record

    def boundary(self) -> str:
        if self.pending.exists():
            return "receipt_unknown"
        if (self.root / "config" / "maintenance.lock").exists():
            return "draining"
        settings = json.loads((self.root / "config/settings.json").read_text(encoding="utf-8-sig"))
        if not settings["enabled"][self.kind]:
            return "stopped"
        if settings["revision"] != self.revision:
            return "applying"
        return "ready"

    def claim(self, identifier: str = "", operation_id: str = "") -> None:
        self.authenticated = True
        if identifier:
            atomic_record(self.pending, dict(AttemptId=identifier, OperationId=operation_id,
                                             Revision=self.revision))
            self.status("working")
        else:
            self.status("ready")

    def run(self, cycle, poll_seconds: float, once: bool = False) -> int:
        while True:
            boundary = self.boundary()
            if boundary != "ready":
                self.status(boundary)
                return 22 if boundary == "receipt_unknown" else 20
            try:
                result = cycle()
            except Exception as exc:
                # The API exception body may contain private details. Only expose its class.
                self.unknown = self.pending.exists()
                cause = exc.__cause__ or exc
                code = getattr(cause, "code", None)
                detail = f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
                self.status("receipt_unknown" if self.unknown else "error",
                            "待核验回执" if self.unknown else f"连接或执行失败 ({detail})，请检查地址、令牌和网络")
                return 22 if self.unknown else 21
            if self.unknown:
                self.status("receipt_unknown", "回执未确认，请在执行系统对账")
                return 22
            self.pending.unlink(missing_ok=True)
            if once:
                if self.kind == "write":
                    return 0 if result == "claimed" else 1
                return 0 if result in ("idle", "completed") else 1
            if result in ("claimed", "completed"):
                continue
            deadline = time.monotonic() + poll_seconds
            while self.boundary() == "ready" and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
