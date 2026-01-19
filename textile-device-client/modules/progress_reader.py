"""
进度文件读取模块
"""

import os
from typing import Optional
from .logger import Logger


class ProgressReader:
    def __init__(self, working_path: str, logger: Logger, results_port: int = 9100):
        self.working_path = working_path
        self.logger = logger
        self.results_port = results_port

    def read_progress(self) -> int:
        """读取设备进度

        Returns:
            int: 进度值 (0-100)
        """
        if not self.working_path:
            self.logger.warning("工作路径未配置，进度设为 0")
            return 0

        self.logger.debug(f"尝试读取工作路径: {self.working_path}")

        try:
            if not os.path.exists(self.working_path):
                self.logger.warning(f"工作路径不存在: {self.working_path}，进度设为 0")
                return 0

            latest_folder = self._get_latest_modified_folder(self.working_path)
            if not latest_folder:
                self.logger.warning("未找到可用的子文件夹，进度设为 0")
                return 0

            progress = self._check_progress(latest_folder)
            self.logger.debug(f"当前最新文件夹: {latest_folder}，进度: {progress}%")
            return progress

        except PermissionError:
            self.logger.error(f"无权限访问工作路径: {self.working_path}")
            return 0
        except Exception as e:
            self.logger.error(f"读取工作路径失败: {e}，进度设为 0")
            return 0

    def check_path_accessible(self) -> bool:
        """检查工作路径是否可访问

        Returns:
            bool: 是否可访问
        """
        if not self.working_path:
            self.logger.warning("工作路径未配置")
            return False

        try:
            if not os.path.exists(self.working_path):
                self.logger.warning(f"工作路径不存在: {self.working_path}")
                return False

            if not os.path.isdir(self.working_path):
                self.logger.error(f"工作路径不是文件夹: {self.working_path}")
                return False

            self.logger.debug(f"工作路径可访问: {self.working_path}")
            return True

        except Exception as e:
            self.logger.error(f"检查工作路径失败: {e}")
            return False

    def _get_latest_modified_folder(self, base_path: str) -> Optional[str]:
        """获取指定路径下最近修改的子文件夹"""
        try:
            entries = [
                os.path.join(base_path, name)
                for name in os.listdir(base_path)
                if os.path.isdir(os.path.join(base_path, name))
            ]
            if not entries:
                return None
            entries.sort(key=lambda p: os.path.getmtime(p))
            return entries[-1]
        except Exception as e:
            self.logger.error(f"获取最新文件夹失败: {e}")
            return None

    def get_latest_folder_name(self) -> Optional[str]:
        """获取最新文件夹名称"""
        latest_folder = self._get_latest_modified_folder(self.working_path)
        if not latest_folder:
            return None
        return os.path.basename(latest_folder)

    def _check_progress(self, folder_path: str) -> int:
        """根据文件夹结构判断进度"""
        result_folder = os.path.join(folder_path, "result")
        original_image = os.path.join(folder_path, "original_image")
        mask = os.path.join(folder_path, "mask")
        cut_pic = os.path.join(folder_path, "cut_pic")

        if os.path.exists(result_folder) and os.listdir(result_folder):
            return 100

        if (
            os.path.exists(original_image)
            and os.path.exists(mask)
            and os.path.exists(cut_pic)
            and os.path.exists(result_folder)
            and not os.listdir(result_folder)
        ):
            return 80

        if os.path.exists(original_image) and os.path.exists(cut_pic):
            return 20

        return 0

    def get_client_base_url(self) -> Optional[str]:
        """构建客户端结果服务地址"""
        try:
            import socket

            host = None
            hostname = socket.gethostname()
            candidates = socket.gethostbyname_ex(hostname)[2]
            for candidate in candidates:
                if not candidate.startswith("127."):
                    host = candidate
                    break
            if not host:
                host = socket.gethostbyname(hostname)

            port = getattr(self, "results_port", None)
            if not port:
                port = 9100
            return f"http://{host}:{port}"
        except Exception:
            return None
