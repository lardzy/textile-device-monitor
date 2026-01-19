"""
配置管理模块
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any

DEFAULT_CONFIG = {
    "device_code": "1号",
    "device_name": "1号",
    "server_url": "http://127.0.0.1:8000",
    "working_path": "",
    "report_interval": 5,
    "results_port": 9100,
    "log_level": "INFO",
    "manual_status": None,
    "is_first_run": True,
    "device_registered": False,
}


class Config:
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.config = self.load()

    def load(self) -> Dict[str, Any]:
        """加载配置文件"""
        config_path = os.path.abspath(self.config_file)

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                return {**DEFAULT_CONFIG, **config}
            except Exception as e:
                print(f"加载配置文件失败: {e}，使用默认配置")
                return DEFAULT_CONFIG.copy()
        else:
            return DEFAULT_CONFIG.copy()

    def save(self) -> bool:
        """保存配置文件"""
        config_path = os.path.abspath(self.config_file)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"保存配置文件失败: {e}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        """设置配置项"""
        self.config[key] = value
        return self.save()

    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self.config.copy()

    def update(self, updates: Dict[str, Any]) -> bool:
        """批量更新配置"""
        self.config.update(updates)
        return self.save()

    def is_first_run(self) -> bool:
        """是否首次运行"""
        return self.config.get("is_first_run", True)

    def mark_configured(self) -> bool:
        """标记已配置"""
        return self.set("is_first_run", False)

    def is_device_registered(self) -> bool:
        """设备是否已注册"""
        return self.config.get("device_registered", False)

    def mark_device_registered(self) -> bool:
        """标记设备已注册"""
        return self.set("device_registered", True)

    def get_device_code(self) -> str:
        """获取设备编码"""
        return self.config.get("device_code", "1号")

    def get_device_name(self) -> str:
        """获取设备名称"""
        return self.config.get("device_name", "1号")

    def get_server_url(self) -> str:
        """获取服务器地址"""
        return self.config.get("server_url", "http://192.168.1.100:8000")

    def get_working_path(self) -> str:
        """获取工作路径"""
        return self.config.get("working_path", "")

    def get_report_interval(self) -> int:
        """获取上报间隔（秒）"""
        return self.config.get("report_interval", 5)

    def get_results_port(self) -> int:
        """获取结果服务端口"""
        return int(self.config.get("results_port", 9100))

    def get_manual_status(self) -> Optional[str]:
        """获取手动设置的状态"""
        return self.config.get("manual_status")

    def set_manual_status(self, status: Optional[str]) -> bool:
        """设置手动状态（maintenance/error/None）"""
        return self.set("manual_status", status)
