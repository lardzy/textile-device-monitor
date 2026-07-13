"""
状态上报器模块
"""

import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, Callable, cast
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
        on_task_completed: Optional[Callable[[], None]] = None,
    ):
        self.api_client = api_client
        self.progress_reader = progress_reader
        self.metrics_collector = metrics_collector
        self.device_code = device_code
        self.logger = logger
        self.report_interval = report_interval
        self.on_task_completed = on_task_completed

        self.manual_status: Optional[str] = None
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self._last_progress: Optional[int] = None
        self._consecutive_failures = 0

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
            if self.thread.is_alive():
                self.logger.warning("状态上报器停止超时，线程仍在运行")
            self.thread = None
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
        report_started_at = time.monotonic()
        progress_snapshot, progress_elapsed = self._collect_progress_snapshot()
        device_status = self._determine_status(progress_snapshot)
        latest_folder_name = progress_snapshot.get("latest_folder_name")
        task_key = progress_snapshot.get("task_key")
        task_id = self._generate_task_id(latest_folder_name)
        task_name = latest_folder_name or "AI显微镜检测"
        task_progress = progress_snapshot.get("task_progress")
        metrics = self.metrics_collector.collect_metrics()
        extra_metrics = progress_snapshot.get("extra_metrics") or {}
        if extra_metrics:
            metrics = {**metrics, **extra_metrics}
        client_base_url = progress_snapshot.get("client_base_url")

        if (
            task_progress is not None
            and self._last_progress is not None
            and self._last_progress < 100
            and task_progress == 100
            and self.on_task_completed
        ):
            threading.Thread(target=self.on_task_completed, daemon=True).start()
        if task_progress is not None:
            self._last_progress = task_progress

        response = self.api_client.report_status(
            device_code=self.device_code,
            status=device_status,
            task_id=task_id,
            task_key=task_key,
            task_name=task_name,
            task_progress=task_progress,
            metrics=metrics,
            client_base_url=client_base_url,
        )
        total_elapsed = time.monotonic() - report_started_at
        request_info = getattr(self.api_client, "last_request_info", {}) or {}
        http_elapsed = float(request_info.get("elapsed_seconds") or 0)
        attempts = int(request_info.get("attempts") or 0)

        if response:
            if self._consecutive_failures:
                self.logger.warning(
                    f"状态上报已恢复 - 连续失败: {self._consecutive_failures}, "
                    f"本次耗时: {total_elapsed:.2f}s"
                )
            self._consecutive_failures = 0
            queue_count = response.data.get("queue_count", 0) if response.data else 0
            self.logger.info(
                f"上报成功 - 状态: {device_status}, "
                f"进度: {task_progress}%, 排队: {queue_count}, "
                f"CPU: {metrics['cpu']}%, Memory: {metrics['memory']}%"
            )
        else:
            self._consecutive_failures += 1
            self.logger.error("上报失败")

        if total_elapsed >= 10 or not response:
            self.logger.warning(
                f"状态上报诊断 - 成功: {bool(response)}, "
                f"总耗时: {total_elapsed:.2f}s, "
                f"进度耗时: {progress_elapsed:.2f}s, "
                f"HTTP耗时: {http_elapsed:.2f}s, "
                f"HTTP尝试: {attempts}, "
                f"连续失败: {self._consecutive_failures}"
            )

    def _collect_progress_snapshot(self) -> tuple[Dict[str, Any], float]:
        started_at = time.monotonic()
        default_snapshot: Dict[str, Any] = {
            "task_progress": None,
            "latest_folder_name": None,
            "task_key": None,
            "client_base_url": None,
            "extra_metrics": {},
            "device_state": None,
            "task_active": False,
        }
        if not self.progress_reader:
            return default_snapshot, time.monotonic() - started_at

        progress_reader = cast(Any, self.progress_reader)
        snapshot_getter = getattr(progress_reader, "get_status_snapshot", None)
        if callable(snapshot_getter):
            try:
                snapshot = snapshot_getter() or {}
                if isinstance(snapshot, dict):
                    merged = {**default_snapshot, **snapshot}
                    return merged, time.monotonic() - started_at
            except Exception as exc:
                self.logger.error(f"获取状态快照失败: {exc}")

        try:
            default_snapshot.update(
                {
                    "task_progress": self.progress_reader.read_progress(),
                    "latest_folder_name": self.progress_reader.get_latest_folder_name(),
                    "task_key": self._get_task_key(),
                    "client_base_url": self.progress_reader.get_client_base_url(),
                }
            )
        except Exception as exc:
            self.logger.error(f"获取状态快照失败: {exc}")
        return default_snapshot, time.monotonic() - started_at

    def _determine_status(
        self, progress_snapshot: Optional[Dict[str, Any]] = None
    ) -> str:
        """确定设备状态

        Returns:
            str: 设备状态
        """
        if self.manual_status:
            return self.manual_status

        progress_snapshot = progress_snapshot or {}
        if self.progress_reader and getattr(
            self.progress_reader, "is_laser_confocal", False
        ):
            state = progress_snapshot.get("device_state")
            is_active = bool(progress_snapshot.get("task_active"))
            progress = progress_snapshot.get("task_progress")
            progress = int(progress) if progress is not None else 0
            if state in (
                "StateRepeatRunning",
                "StateRepeatStarting",
                "StateRepeatStopping",
            ):
                return "busy"
            if is_active:
                return "busy"
            if state == "StateIdle":
                if progress < 100:
                    return "busy"
                return "idle"
        progress = progress_snapshot.get("task_progress")
        progress = int(progress) if progress is not None else 0

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

    def _get_task_key(self) -> Optional[str]:
        if not self.progress_reader:
            return None
        task_key_getter = getattr(self.progress_reader, "get_task_key", None)
        if callable(task_key_getter):
            try:
                return task_key_getter()
            except Exception as exc:
                self.logger.error(f"获取 task_key 失败: {exc}")
                return None
        return self.progress_reader.get_latest_folder_name()

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
