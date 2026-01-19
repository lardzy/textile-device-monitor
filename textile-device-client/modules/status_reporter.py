"""
状态上报器模块
"""

import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, Callable

# noqa: E501
from .api_client import ApiClient
from .progress_reader import ProgressReader
from .metrics_collector import MetricsCollector
from .logger import Logger


class StatusReporter:
    def __init__(
        self,
        api_client: ApiClient,
        progress_reader: ProgressReader,
        metrics_collector: MetricsCollector,
        device_code: str,
        logger: Logger,
        report_interval: int = 5,
    ):
        self.api_client = api_client
        self.progress_reader = progress_reader
        self.metrics_collector = metrics_collector
        self.device_code = device_code
        self.logger = logger
        self.report_interval = report_interval

        self.manual_status: Optional[str] = None
        self.is_running = False
        self.thread: Optional[threading.Thread] = None

    def set_manual_status(self, status: Optional[str]):
        """设置手动状态

        Args:
            status: 'maintenance', 'error' 或 None
        """
        self.manual_status = status
        self.logger.info(f"手动状态已设置: {status}")

    def start(self):
        """启动定时上报"""
        if self.is_running:
            self.logger.warning("状态上报器已在运行")
            return

        self.is_running = True
        self.thread = threading.Thread(target=self._report_loop, daemon=True)
        self.thread.start()
        self.logger.info("状态上报器已启动")

    def stop(self):
        """停止定时上报"""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
            self.logger.info("状态上报器已停止")

    def _report_loop(self):
        """上报循环"""
        while self.is_running:
            try:
                self._report()
            except Exception as e:
                self.logger.error(f"上报异常: {e}")

            time.sleep(self.report_interval)

    def _report(self):
        """执行单次上报"""
        device_status = self._determine_status()
        latest_folder_name = self.progress_reader.get_latest_folder_name()
        task_id = self._generate_task_id(latest_folder_name)
        task_name = latest_folder_name or "AI显微镜检测"
        task_progress = self._get_task_progress()
        metrics = self.metrics_collector.collect_metrics()

        response = self.api_client.report_status(
            device_code=self.device_code,
            status=device_status,
            task_id=task_id,
            task_name=task_name,
            task_progress=task_progress,
            metrics=metrics,
        )

        if response:
            queue_count = response.data.get("queue_count", 0) if response.data else 0
            self.logger.info(
                f"上报成功 - 状态: {device_status}, "
                f"进度: {task_progress}%, 排队: {queue_count}, "
                f"CPU: {metrics['cpu']}%, Memory: {metrics['memory']}%"
            )
        else:
            self.logger.error("上报失败")

    def _determine_status(self) -> str:
        """确定设备状态

        Returns:
            str: 设备状态
        """
        if self.manual_status:
            return self.manual_status

        progress = self.progress_reader.read_progress()

        if progress < 100:
            return "busy"
        else:
            return "idle"

    def _generate_task_id(self, folder_name: Optional[str]) -> str:
        """生成任务 ID

        Returns:
            str: 任务 ID
        """
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{folder_name}" if folder_name else ""
        return f"TASK_{now}{suffix}"

    def _get_task_progress(self) -> Optional[int]:
        """获取任务进度

        Returns:
            int or None: 任务进度
        """
        return self.progress_reader.read_progress()

    def report_once(self) -> bool:
        """执行单次上报（手动触发）

        Returns:
            bool: 是否成功
        """
        try:
            self._report()
            return True
        except Exception as e:
            self.logger.error(f"单次上报失败: {e}")
            return False
