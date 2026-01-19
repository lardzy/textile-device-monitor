"""
服务端 API 客户端模块
"""

import requests
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

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
    def __init__(self, base_url: str, logger: Logger):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = 5
        self.max_retries = 3
        self.logger = logger

    def _request(
        self, method: str, endpoint: str, data: Optional[Dict] = None
    ) -> Optional[Any]:
        """发送 HTTP 请求"""
        url = f"{self.base_url}/api{endpoint}"

        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method, url, json=data, timeout=self.timeout
                )

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 201:
                    return response.json()
                elif response.status_code == 404:
                    self.logger.warning(f"资源不存在: {endpoint}")
                    return None
                elif response.status_code == 400:
                    self.logger.error(f"请求错误: {response.text}")
                    return None
                else:
                    self.logger.error(f"HTTP {response.status_code}: {response.text}")
                    return None

            except requests.exceptions.Timeout:
                self.logger.warning(
                    f"请求超时 (尝试 {attempt + 1}/{self.max_retries}): {url}"
                )
            except requests.exceptions.ConnectionError:
                self.logger.warning(
                    f"连接失败 (尝试 {attempt + 1}/{self.max_retries}): {url}"
                )
            except Exception as e:
                self.logger.error(f"请求异常: {e}")
                return None

        self.logger.error(f"请求失败，已达最大重试次数: {url}")
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
        task_name: Optional[str] = None,
        task_progress: Optional[int] = None,
        metrics: Optional[Dict[str, Any]] = None,
        client_base_url: Optional[str] = None,
    ) -> Optional[MessageResponse]:
        """上报设备状态

        Args:
            device_code: 设备编码
            status: 设备状态
            task_id: 任务ID（可选）
            task_name: 任务名称（可选）
            task_progress: 任务进度 0-100（可选）
            metrics: 设备指标（可选）

        Returns:
            MessageResponse or None
        """
        data = {
            "status": status,
            "task_id": task_id,
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
        """健康检查

        Returns:
            bool: 服务器是否正常
        """
        try:
            response = requests.get(f"{self.base_url}/health", timeout=3)
            return response.status_code == 200
        except:
            return False
