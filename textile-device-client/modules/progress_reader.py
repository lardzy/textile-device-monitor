"""
进度文件读取模块
"""

import os
from typing import Optional
from .logger import Logger


class ProgressReader:
    def __init__(self, progress_file_path: str, logger: Logger):
        self.progress_file_path = progress_file_path
        self.logger = logger

    def read_progress(self) -> int:
        """读取设备进度

        Returns:
            int: 进度值 (0-100)
        """
        if not self.progress_file_path:
            self.logger.warning("进度文件路径未配置，进度设为 0")
            return 0

        self.logger.debug(f"尝试读取进度文件: {self.progress_file_path}")

        try:
            if not os.path.exists(self.progress_file_path):
                self.logger.warning(
                    f"进度文件不存在: {self.progress_file_path}，进度设为 0"
                )
                return 0

            with open(self.progress_file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()

            if not content:
                self.logger.warning(
                    f"进度文件为空: {self.progress_file_path}，进度设为 0"
                )
                return 0

            try:
                progress = int(content)
                if progress < 0:
                    self.logger.warning(f"进度值小于0: {progress}，修正为 0")
                    return 0
                elif progress > 100:
                    self.logger.warning(f"进度值大于100: {progress}，修正为 100")
                    return 100
                else:
                    self.logger.debug(f"读取到进度: {progress}")
                    return progress

            except ValueError as e:
                self.logger.error(f"进度值格式错误: {content}，错误: {e}，进度设为 0")
                return 0

        except PermissionError:
            self.logger.error(f"无权限访问进度文件: {self.progress_file_path}")
            return 0
        except Exception as e:
            self.logger.error(f"读取进度文件失败: {e}，进度设为 0")
            return 0

    def check_file_accessible(self) -> bool:
        """检查进度文件是否可访问

        Returns:
            bool: 是否可访问
        """
        if not self.progress_file_path:
            self.logger.warning("进度文件路径未配置")
            return False

        try:
            if not os.path.exists(self.progress_file_path):
                self.logger.warning(f"进度文件不存在: {self.progress_file_path}")
                return False

            if not os.path.isfile(self.progress_file_path):
                self.logger.error(f"进度文件不是文件: {self.progress_file_path}")
                return False

            self.logger.debug(f"进度文件可访问: {self.progress_file_path}")
            return True

        except Exception as e:
            self.logger.error(f"检查进度文件失败: {e}")
            return False
