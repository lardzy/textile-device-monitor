"""
系统托盘模块
"""

import pystray
from PIL import Image, ImageDraw, ImageFont
import threading
from typing import Optional, Callable
from .logger import Logger


class TrayIcon:
    def __init__(
        self,
        logger: Logger,
        on_open_config: Callable = None,
        on_toggle_maintenance: Callable = None,
        on_view_logs: Callable = None,
        on_reconnect: Callable = None,
        on_exit: Callable = None,
    ):
        self.logger = logger
        self.on_open_config = on_open_config
        self.on_toggle_maintenance = on_toggle_maintenance
        self.on_view_logs = on_view_logs
        self.on_reconnect = on_reconnect
        self.on_exit = on_exit

        self.is_maintenance = False
        self.icon = self._create_icon()
        self.tray: Optional[pystray.Icon] = None
        self.thread: Optional[threading.Thread] = None

    def _create_icon(self, status: str = "idle") -> Image.Image:
        """创建托盘图标

        Args:
            status: 状态

        Returns:
            Image: 图像对象
        """
        width = 64
        height = 64
        image = Image.new("RGB", (width, height), color="white")
        draw = ImageDraw.Draw(image)

        if status == "idle":
            color = "green"
            text = "空闲"
        elif status == "busy":
            color = "blue"
            text = "运行"
        elif status == "maintenance":
            color = "orange"
            text = "维护"
        elif status == "error":
            color = "red"
            text = "错误"
        else:
            color = "gray"
            text = "离线"

        draw.ellipse([4, 4, 60, 60], fill=color, outline="black")

        try:
            font = ImageFont.truetype("arial.ttf", 12)
        except:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        text_x = (width - text_width) // 2
        text_y = (height - text_height) // 2

        draw.text((text_x, text_y), text, fill="white", font=font)

        return image

    def _update_icon(self):
        """更新托盘图标"""
        if self.is_maintenance:
            self.icon = self._create_icon("maintenance")
        else:
            self.icon = self._create_icon("idle")

        if self.tray:
            self.tray.icon = self.icon

    def _open_config(self):
        """打开配置窗口"""
        self.logger.info("打开配置窗口")
        if self.on_open_config:
            self.on_open_config()

    def _toggle_maintenance(self):
        """切换维护模式"""
        self.is_maintenance = not self.is_maintenance
        status = "maintenance" if self.is_maintenance else "idle"
        self.logger.info(f"切换到 {status} 模式")
        self._update_icon()

        if self.on_toggle_maintenance:
            self.on_toggle_maintenance(status if self.is_maintenance else None)

    def _view_logs(self):
        """查看日志"""
        self.logger.info("查看日志")
        if self.on_view_logs:
            self.on_view_logs()

    def _reconnect(self):
        """重新连接服务器"""
        self.logger.info("重新连接服务器")
        if self.on_reconnect:
            self.on_reconnect()

    def _exit(self):
        """退出程序"""
        self.logger.info("退出程序")
        if self.on_exit:
            self.on_exit()
        if self.tray:
            self.tray.stop()

    def _on_clicked(self, icon, button, time):
        """托盘图标点击事件"""
        if button == pystray.MouseButton.LEFT:
            self._view_logs()

    def start(self):
        """启动系统托盘"""
        if self.tray:
            return

        menu = pystray.Menu(
            pystray.MenuItem("打开配置", self._open_config),
            pystray.MenuItem(
                "维护模式" if not self.is_maintenance else "正常模式",
                self._toggle_maintenance,
            ),
            pystray.MenuItem("查看日志", self._view_logs),
            pystray.MenuItem("重新连接", self._reconnect),
            pystray.MenuItem("退出", self._exit),
        )

        self.tray = pystray.Icon(
            "纺织品检测设备客户端", self.icon, "纺织品检测设备客户端", menu
        )

        self.tray.on_click = self._on_clicked

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.logger.info("系统托盘已启动")

    def _run(self):
        """运行托盘"""
        try:
            self.tray.run()
        except Exception as e:
            self.logger.error(f"系统托盘运行失败: {e}")

    def stop(self):
        """停止系统托盘"""
        if self.tray:
            self.tray.stop()
            self.tray = None
            self.logger.info("系统托盘已停止")

    def update_status(self, status: str):
        """更新状态显示

        Args:
            status: 设备状态
        """
        if status == "maintenance":
            self.is_maintenance = True
        elif status == "idle" and not self.is_maintenance:
            self.is_maintenance = False
        elif status == "busy" and not self.is_maintenance:
            self.is_maintenance = False

        self._update_icon()

    def show_notification(self, title: str, message: str):
        """显示通知

        Args:
            title: 标题
            message: 消息
        """
        if self.tray:
            self.tray.notify(message, title)
