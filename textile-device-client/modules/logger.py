"""
日志管理模块
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path


class Logger:
    def __init__(self, log_dir: str = "logs", log_level: str = "INFO"):
        self.log_dir = log_dir
        self.log_level = log_level
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """设置日志记录器"""
        logger = logging.getLogger("TextileDeviceClient")
        logger.setLevel(getattr(logging, self.log_level, logging.INFO))

        if logger.handlers:
            return logger

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        os.makedirs(self.log_dir, exist_ok=True)

        today = datetime.now().strftime("%Y%m%d")
        log_file = os.path.join(self.log_dir, f"client.{today}.log")

        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger

    def debug(self, message: str):
        """调试日志"""
        self.logger.debug(message)

    def info(self, message: str):
        """信息日志"""
        self.logger.info(message)

    def warning(self, message: str):
        """警告日志"""
        self.logger.warning(message)

    def error(self, message: str):
        """错误日志"""
        self.logger.error(message)

    def exception(self, message: str):
        """异常日志"""
        self.logger.exception(message)

    def get_recent_logs(self, lines: int = 100) -> list:
        """获取最近的日志"""
        try:
            today = datetime.now().strftime("%Y%m%d")
            log_file = os.path.join(self.log_dir, f"client.{today}.log")
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                    return all_lines[-lines:]
            return []
        except Exception as e:
            self.error(f"读取日志文件失败: {e}")
            return []

    def cleanup_old_logs(self, days: int = 7):
        """清理旧日志文件"""
        try:
            log_files = list(Path(self.log_dir).glob("client.*.log"))
            now = datetime.now()

            for log_file in log_files:
                try:
                    file_date = datetime.strptime(
                        log_file.stem.split(".")[-1], "%Y%m%d"
                    )
                    if (now - file_date).days > days:
                        log_file.unlink()
                        self.info(f"已删除旧日志文件: {log_file}")
                except ValueError:
                    continue
        except Exception as e:
            self.error(f"清理日志文件失败: {e}")
