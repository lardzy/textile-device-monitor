"""
系统指标采集模块
"""

import psutil
import time
from typing import Dict, Any
from .logger import Logger


class MetricsCollector:
    def __init__(self, logger: Logger):
        self.logger = logger
        self.start_time = time.time()

    def collect_metrics(self) -> Dict[str, float]:
        """采集系统指标

        Returns:
            Dict: 包含 CPU、内存、磁盘使用率和运行时间
        """
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)

            memory = psutil.virtual_memory()
            memory_percent = memory.percent

            disk = psutil.disk_usage("/")
            disk_percent = disk.percent

            runtime = int(time.time() - self.start_time)

            metrics = {
                "cpu": round(cpu_percent, 1),
                "memory": round(memory_percent, 1),
                "disk": round(disk_percent, 1),
                "runtime": runtime,
            }

            self.logger.debug(
                f"采集到指标: CPU={metrics['cpu']}%, Memory={metrics['memory']}%, Disk={metrics['disk']}%, Runtime={runtime}s"
            )

            return metrics

        except Exception as e:
            self.logger.error(f"采集系统指标失败: {e}")
            return {
                "cpu": 0.0,
                "memory": 0.0,
                "disk": 0.0,
                "runtime": int(time.time() - self.start_time),
            }

    def get_cpu_usage(self) -> float:
        """获取 CPU 使用率"""
        try:
            return round(psutil.cpu_percent(interval=0.1), 1)
        except Exception as e:
            self.logger.error(f"获取 CPU 使用率失败: {e}")
            return 0.0

    def get_memory_usage(self) -> float:
        """获取内存使用率"""
        try:
            memory = psutil.virtual_memory()
            return round(memory.percent, 1)
        except Exception as e:
            self.logger.error(f"获取内存使用率失败: {e}")
            return 0.0

    def get_disk_usage(self) -> float:
        """获取磁盘使用率"""
        try:
            disk = psutil.disk_usage("/")
            return round(disk.percent, 1)
        except Exception as e:
            self.logger.error(f"获取磁盘使用率失败: {e}")
            return 0.0

    def get_runtime(self) -> int:
        """获取运行时间（秒）"""
        return int(time.time() - self.start_time)
