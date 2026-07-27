"""
服务端 API 客户端模块
"""

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

import requests

from modules.transport_security import (
    TRANSPORT_COMPATIBLE,
    TransportSecurityError,
    diagnose_ssl_error,
    normalize_server_origin,
    validate_ca_bundle,
)

try:
    from modules.logger import Logger
except ImportError:
    from logger import Logger


@dataclass
class Device:
    id: int
    device_code: str
    name: str
    model: Optional[str]
    location: Optional[str]
    description: Optional[str]
    status: str
    last_heartbeat: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class MessageResponse:
    success: bool
    message: str
    data: Optional[Dict[str, Any]]


class ApiClient:
    def __init__(
        self,
        base_url: str,
        logger: Logger,
        *,
        transport_security: str = TRANSPORT_COMPATIBLE,
        tls_ca_bundle: Optional[str] = None,
    ):
        self.base_url = normalize_server_origin(base_url, transport_security)
        self.transport_security = transport_security
        self.session = requests.Session()
        # This is a closed LAN deployment. Environment proxies must never
        # intercept device status, health or registration traffic.
        self.session.trust_env = False
        self.session.headers.update({"Content-Type": "application/json"})
        if self.base_url.startswith("https://"):
            if not tls_ca_bundle:
                raise TransportSecurityError(
                    "tls_ca_bundle_empty",
                    "HTTPS 配置必须指定内部 CA 证书文件",
                )
            ca_path = validate_ca_bundle(Path(tls_ca_bundle))
            self.session.verify = str(ca_path)
        else:
            self.session.verify = True
        self.timeout = 5
        self.max_retries = 3
        self.logger = logger
        self.last_request_info: Dict[str, Any] = {}
        self.last_tls_error_message: Optional[str] = None

    def _record_request_info(
        self,
        *,
        method: str,
        endpoint: str,
        attempts: int,
        elapsed_seconds: float,
        success: bool,
        status_code: Optional[int] = None,
        error: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self.last_request_info = {
            "method": method,
            "endpoint": endpoint,
            "attempts": attempts,
            "elapsed_seconds": elapsed_seconds,
            "success": success,
            "status_code": status_code,
            "error": error,
            "error_message": error_message,
        }

    def _send_with_retries(
        self,
        method: str,
        url: str,
        *,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Optional[Any]:
        started_at = time.monotonic()
        attempts = 0
        last_error = None
        last_error_message = None
        self.last_tls_error_message = None

        for attempt in range(self.max_retries):
            attempts = attempt + 1
            try:
                response = self.session.request(
                    method,
                    url,
                    json=data,
                    timeout=self.timeout,
                    allow_redirects=False,
                )

                if 200 <= response.status_code < 300:
                    self._record_request_info(
                        method=method,
                        endpoint=endpoint,
                        attempts=attempts,
                        elapsed_seconds=time.monotonic() - started_at,
                        success=True,
                        status_code=response.status_code,
                    )
                    return response
                if 300 <= response.status_code < 400:
                    last_error = "redirect_refused"
                    last_error_message = (
                        "服务器返回了重定向，客户端为防止 HTTPS 降级已拒绝跟随"
                    )
                    self.logger.error(
                        f"{last_error_message}: {response.status_code} {endpoint}"
                    )
                    self._record_request_info(
                        method=method,
                        endpoint=endpoint,
                        attempts=attempts,
                        elapsed_seconds=time.monotonic() - started_at,
                        success=False,
                        status_code=response.status_code,
                        error=last_error,
                        error_message=last_error_message,
                    )
                    return None
                elif response.status_code == 404:
                    self.logger.warning(f"资源不存在: {endpoint}")
                    self._record_request_info(
                        method=method,
                        endpoint=endpoint,
                        attempts=attempts,
                        elapsed_seconds=time.monotonic() - started_at,
                        success=False,
                        status_code=response.status_code,
                        error="not_found",
                        error_message="服务器资源不存在",
                    )
                    return None
                elif response.status_code == 400:
                    self.logger.error(f"请求错误: {response.text}")
                    self._record_request_info(
                        method=method,
                        endpoint=endpoint,
                        attempts=attempts,
                        elapsed_seconds=time.monotonic() - started_at,
                        success=False,
                        status_code=response.status_code,
                        error=response.text,
                        error_message="服务器拒绝了请求",
                    )
                    return None
                else:
                    self.logger.error(f"HTTP {response.status_code}: {response.text}")
                    self._record_request_info(
                        method=method,
                        endpoint=endpoint,
                        attempts=attempts,
                        elapsed_seconds=time.monotonic() - started_at,
                        success=False,
                        status_code=response.status_code,
                        error=response.text,
                        error_message=f"服务器返回 HTTP {response.status_code}",
                    )
                    return None

            except requests.exceptions.SSLError as exc:
                last_error, last_error_message = diagnose_ssl_error(exc)
                self.last_tls_error_message = last_error_message
                self.logger.error(f"{last_error_message}: {url}")
                self._record_request_info(
                    method=method,
                    endpoint=endpoint,
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started_at,
                    success=False,
                    error=last_error,
                    error_message=last_error_message,
                )
                # Certificate failures are deterministic; retrying cannot heal
                # them and only delays the operator-facing diagnosis.
                return None
            except requests.exceptions.Timeout:
                last_error = "timeout"
                last_error_message = "连接服务器超时"
                self.logger.warning(
                    f"请求超时 (尝试 {attempt + 1}/{self.max_retries}): {url}"
                )
            except requests.exceptions.ConnectionError:
                last_error = "connection_error"
                last_error_message = "无法连接到服务器"
                self.logger.warning(
                    f"连接失败 (尝试 {attempt + 1}/{self.max_retries}): {url}"
                )
            except Exception as e:
                last_error = str(e)
                last_error_message = "客户端请求发生异常"
                self.logger.error(f"请求异常: {e}")
                self._record_request_info(
                    method=method,
                    endpoint=endpoint,
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started_at,
                    success=False,
                    error=last_error,
                    error_message=last_error_message,
                )
                return None

        self.logger.error(f"请求失败，已达最大重试次数: {url}")
        self._record_request_info(
            method=method,
            endpoint=endpoint,
            attempts=attempts,
            elapsed_seconds=time.monotonic() - started_at,
            success=False,
            error=last_error,
            error_message=last_error_message,
        )
        return None

    def _request(
        self, method: str, endpoint: str, data: Optional[Dict] = None
    ) -> Optional[Any]:
        """Send a same-origin API request and decode its JSON response."""

        url = f"{self.base_url}/api{endpoint}"
        response = self._send_with_retries(
            method,
            url,
            endpoint=endpoint,
            data=data,
        )
        if response is None:
            return None
        try:
            return response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as exc:
            self.logger.error(f"服务器响应不是有效 JSON: {endpoint}: {exc}")
            self._record_request_info(
                method=method,
                endpoint=endpoint,
                attempts=self.last_request_info.get("attempts", 1),
                elapsed_seconds=self.last_request_info.get("elapsed_seconds", 0),
                success=False,
                status_code=response.status_code,
                error="invalid_json",
                error_message="服务器响应格式无效",
            )
            return None

    def get_all_devices(self) -> List[Device]:
        """获取所有设备列表

        Returns:
            List[Device]: 设备列表
        """
        response = self._request("GET", "/devices")
        if response is None:
            return []

        try:
            devices = []
            for item in response:
                devices.append(
                    Device(
                        id=item["id"],
                        device_code=item["device_code"],
                        name=item["name"],
                        model=item.get("model"),
                        location=item.get("location"),
                        description=item.get("description"),
                        status=item["status"],
                        last_heartbeat=item.get("last_heartbeat"),
                        created_at=item["created_at"],
                        updated_at=item["updated_at"],
                    )
                )
            return devices
        except Exception as e:
            self.logger.error(f"解析设备列表失败: {e}")
            return []

    def get_device_by_code(self, device_code: str) -> Optional[Device]:
        """根据设备编码查找设备

        Args:
            device_code: 设备编码

        Returns:
            Device or None
        """
        devices = self.get_all_devices()
        for device in devices:
            if device.device_code == device_code:
                return device
        return None

    def create_device(
        self,
        device_code: str,
        name: str,
        model: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        client_base_url: Optional[str] = None,
    ) -> Optional[Device]:
        """创建新设备

        Args:
            device_code: 设备编码
            name: 设备名称
            model: 设备型号（可选）
            location: 设备位置（可选）
            description: 设备描述（可选）

        Returns:
            Device or None
        """
        data = {
            "device_code": device_code,
            "name": name,
            "model": model,
            "location": location,
            "description": description,
            "client_base_url": client_base_url,
        }

        response = self._request("POST", "/devices", data=data)
        if response is None:
            return None

        try:
            return Device(
                id=response["id"],
                device_code=response["device_code"],
                name=response["name"],
                model=response.get("model"),
                location=response.get("location"),
                description=response.get("description"),
                status=response["status"],
                last_heartbeat=response.get("last_heartbeat"),
                created_at=response["created_at"],
                updated_at=response["updated_at"],
            )
        except Exception as e:
            self.logger.error(f"解析设备创建响应失败: {e}")
            return None

    def report_status(
        self,
        device_code: str,
        status: str,
        task_id: Optional[str] = None,
        task_key: Optional[str] = None,
        task_name: Optional[str] = None,
        task_progress: Optional[int] = None,
        metrics: Optional[Dict[str, Any]] = None,
        client_base_url: Optional[str] = None,
        report_id: Optional[str] = None,
        reported_at: Optional[str] = None,
    ) -> Optional[MessageResponse]:
        """上报设备状态

        Args:
            device_code: 设备编码
            status: 设备状态
            report_id: 单次采样幂等 UUID；同一次传输重试必须复用
            reported_at: 带时区的 UTC 采样时间
            task_id: 任务ID（可选）
            task_key: 稳定任务标识（可选）
            task_name: 任务名称（可选）
            task_progress: 任务进度 0-100（可选）
            metrics: 设备指标（可选）

        Returns:
            MessageResponse or None
        """
        data = {
            "report_id": report_id,
            "reported_at": reported_at,
            "status": status,
            "task_id": task_id,
            "task_key": task_key,
            "task_name": task_name,
            "task_progress": task_progress,
            "metrics": metrics,
            "client_base_url": client_base_url,
        }

        endpoint = f"/devices/{device_code}/status"
        response = self._request("POST", endpoint, data=data)
        if response is None:
            return None

        try:
            return MessageResponse(
                success=response["success"],
                message=response["message"],
                data=response.get("data"),
            )
        except Exception as e:
            self.logger.error(f"解析状态上报响应失败: {e}")
            return None

    def health_check(self) -> bool:
        """Check backend readiness through the same pinned, proxy-free session."""

        response = self._send_with_retries(
            "GET",
            f"{self.base_url}/health/ready",
            endpoint="/health/ready",
        )
        return response is not None
