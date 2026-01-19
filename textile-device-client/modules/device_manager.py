"""
设备管理模块 - 负责设备注册和管理
"""

from typing import Optional
from .api_client import ApiClient, Device
from .logger import Logger


class DeviceManager:
    PRESET_DEVICES = ["1号", "2号", "3号", "4号", "5号", "6号", "7号", "8号"]

    def __init__(self, api_client: ApiClient, logger: Logger):
        self.api_client = api_client
        self.logger = logger
        self.device: Optional[Device] = None

    def register_device(
        self,
        device_code: str,
        device_name: str,
        model: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        client_base_url: Optional[str] = None,
    ) -> bool:
        """注册设备（如果不存在）

        Args:
            device_code: 设备编码
            device_name: 设备名称
            model: 设备型号（可选）
            location: 设备位置（可选）
            description: 设备描述（可选）

        Returns:
            bool: 注册是否成功
        """
        self.logger.info(f"检查设备是否存在: {device_code}")

        device = self.api_client.get_device_by_code(device_code)

        if device:
            self.logger.info(f"设备已存在: {device.name} (ID: {device.id})")
            self.device = device
            return True

        self.logger.info(f"设备不存在，开始注册: {device_name}")

        new_device = self.api_client.create_device(
            device_code=device_code,
            name=device_name,
            model=model,
            location=location,
            description=description,
            client_base_url=client_base_url,
        )

        if new_device:
            self.logger.info(f"设备注册成功: {new_device.name} (ID: {new_device.id})")
            self.device = new_device
            return True
        else:
            self.logger.error(f"设备注册失败: {device_code}")
            return False

    def check_device_exists(self, device_code: str) -> bool:
        """检查设备是否存在

        Args:
            device_code: 设备编码

        Returns:
            bool: 设备是否存在
        """
        device = self.api_client.get_device_by_code(device_code)
        if device:
            self.device = device
            return True
        return False

    def get_device(self) -> Optional[Device]:
        """获取当前设备

        Returns:
            Device or None
        """
        return self.device

    def is_preset_device(self, device_name: str) -> bool:
        """判断是否为预设设备

        Args:
            device_name: 设备名称

        Returns:
            bool: 是否为预设设备
        """
        return device_name in self.PRESET_DEVICES

    def get_preset_devices(self) -> list:
        """获取预设设备列表

        Returns:
            list: 预设设备名称列表
        """
        return self.PRESET_DEVICES.copy()
