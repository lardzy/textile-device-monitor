from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4


class ExecutionApiError(Exception):
    """Domain error rendered by the execution router without FastAPI nesting."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}
        self.request_id = request_id or str(uuid4())

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "request_id": self.request_id,
        }


def not_found(resource_name: str, resource_id: str) -> ExecutionApiError:
    return ExecutionApiError(
        404,
        "resource_not_found",
        f"{resource_name}不存在",
        details={"resource_id": resource_id},
    )


def conflict(
    code: str,
    message: str,
    **details: Any,
) -> ExecutionApiError:
    return ExecutionApiError(409, code, message, details=details)
