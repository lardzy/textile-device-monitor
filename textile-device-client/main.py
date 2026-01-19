"""
纺织品检测设备客户端 - 主程序
"""

import sys
import time
import os
import ctypes
from typing import Optional
from modules.config import Config
from modules.logger import Logger
from modules.api_client import ApiClient
from modules.device_manager import DeviceManager
from modules.progress_reader import ProgressReader
from modules.metrics_collector import MetricsCollector
from modules.config_window import ConfigWindow

# Windows 控制台控制
try:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    HAS_WINDOWS_CONSOLE = True
except:
    HAS_WINDOWS_CONSOLE = False


def hide_console():
    """隐藏控制台窗口（仅 Windows）"""
    if HAS_WINDOWS_CONSOLE:
        try:
            # 获取控制台窗口句柄
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                # 隐藏窗口
                user32.ShowWindow(hwnd, 0)
        except:
            pass


def show_console():
    """显示控制台窗口（仅 Windows）"""
    if HAS_WINDOWS_CONSOLE:
        try:
            # 获取控制台窗口句柄
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                # 显示窗口
                user32.ShowWindow(hwnd, 1)
        except:
            pass


try:
    from modules.status_reporter import StatusReporter
except:
    StatusReporter = None

try:
    from modules.tray_icon import TrayIcon
except:
    TrayIcon = None


class TextileDeviceClient:
    def __init__(self):
        self.config = Config()
        self.logger = Logger(
            log_dir="logs", log_level=self.config.get("log_level", "INFO")
        )
        self.logger.info("=" * 60)
        self.logger.info("纺织品检测设备客户端启动")
        self.logger.info("=" * 60)

        self.api_client = None
        self.device_manager = None
        self.progress_reader = None
        self.metrics_collector = None
        self.status_reporter = None
        self.tray_icon = None

    def initialize(self):
        """初始化客户端"""
        self.logger.info("正在初始化客户端...")

        config = self.config.get_all()

        self.api_client = ApiClient(base_url=config["server_url"], logger=self.logger)

        self.device_manager = DeviceManager(
            api_client=self.api_client, logger=self.logger
        )

        self.progress_reader = ProgressReader(
            working_path=config["working_path"], logger=self.logger
        )

        self.metrics_collector = MetricsCollector(logger=self.logger)

        if StatusReporter:
            self.status_reporter = StatusReporter(
                api_client=self.api_client,
                progress_reader=self.progress_reader,
                metrics_collector=self.metrics_collector,
                device_code=config["device_code"],
                logger=self.logger,
                report_interval=config["report_interval"],
            )

        if TrayIcon:
            self.tray_icon = TrayIcon(
                logger=self.logger,
                on_open_config=self._open_config,
                on_toggle_maintenance=self._toggle_maintenance,
                on_view_logs=self._view_logs,
                on_reconnect=self._reconnect,
                on_exit=self._exit,
            )

        self.logger.info("客户端初始化完成")

    def run(self):
        """运行客户端"""
        try:
            if self.config.is_first_run():
                self.logger.info("首次运行，进入配置流程")
                # 首次运行时显示控制台
                show_console()
                if not self._show_config_dialog():
                    self.logger.warning("用户取消配置，程序退出")
                    return
                # 配置完成后，隐藏控制台
                hide_console()
            else:
                self.logger.info("加载已保存的配置")
                # 后续运行时隐藏控制台
                hide_console()
                self.initialize()

            if not self._register_device():
                self.logger.error("设备注册失败，程序退出")
                self._show_error_and_exit("设备注册失败，请检查网络连接后重试")
                return

            if self.status_reporter:
                self.status_reporter.set_manual_status(self.config.get_manual_status())
                self.status_reporter.start()

            if self.tray_icon:
                self.tray_icon.start()

            print("\n" + "=" * 60)
            print("客户端已启动")
            print("=" * 60)
            print(
                f"设备: {self.config.get_device_name()} ({self.config.get_device_code()})"
            )
            print(f"服务器: {self.config.get_server_url()}")
            print(f"上报间隔: {self.config.get_report_interval()} 秒")
            print("=" * 60)
            print("\n命令列表:")
            print("  c - 修改配置")
            print("  l - 查看日志")
            print("  r - 重新连接服务器")
            print("  m - 切换维护模式")
            print("  q - 退出程序")
            print("=" * 60)

            if self.tray_icon:
                self.tray_icon.show_notification(
                    "纺织品检测设备客户端",
                    f"设备 {self.config.get_device_name()} 已启动",
                )

            self.logger.info("客户端已启动，进入交互模式")

            while True:
                try:
                    cmd = input("\n> ").strip().lower()

                    if cmd == "q":
                        break
                    elif cmd == "c":
                        self._open_config()
                    elif cmd == "l":
                        self._view_logs()
                    elif cmd == "r":
                        self._reconnect()
                    elif cmd == "m":
                        current_status = self.config.get_manual_status()
                        new_status = None if current_status else "maintenance"
                        self._toggle_maintenance(new_status)
                        print(f"维护模式: {'开启' if new_status else '关闭'}")
                    elif cmd == "h" or cmd == "help" or cmd == "?":
                        print("\n命令列表:")
                        print("  c - 修改配置")
                        print("  l - 查看日志")
                        print("  r - 重新连接服务器")
                        print("  m - 切换维护模式")
                        print("  h - 显示帮助")
                        print("  q - 退出程序")
                    elif cmd:
                        print(f"未知命令: {cmd} (输入 h 查看帮助)")

                except KeyboardInterrupt:
                    print("\n")
                    break
                except Exception as e:
                    print(f"命令执行失败: {e}")

            print("\n正在退出...")
            self._exit()

        except KeyboardInterrupt:
            self.logger.info("用户中断，程序退出")
        except Exception as e:
            self.logger.exception(f"程序异常: {e}")
            self._show_error_and_exit(f"程序发生错误: {e}")

    def _show_config_dialog(self) -> bool:
        """显示配置对话框（命令行）

        Returns:
            bool: 用户是否确认配置
        """
        print("\n" + "=" * 60)
        print("纺织品检测设备客户端 - 首次配置")
        print("=" * 60)

        try:
            config = self.config.get_all()

            print(f"\n预设设备列表: {', '.join(DeviceManager.PRESET_DEVICES)}")

            device_code = input("\n请输入设备编码（默认: 1号）: ").strip()
            if not device_code:
                device_code = "1号"

            device_name = input("请输入设备名称: ").strip()
            if not device_name:
                device_name = device_code

            print(f"\n默认服务器地址: {config['server_url']}")
            server_url = input("请输入服务器地址（直接回车使用默认）: ").strip()
            if not server_url:
                server_url = config["server_url"]

            print(f"\n默认工作路径: {config['working_path']}")
            working_path = input("请输入工作路径（直接回车使用默认）: ").strip()
            if not working_path:
                working_path = config["working_path"]

            print(f"\n默认上报间隔: {config['report_interval']} 秒")
            interval_input = input("请输入上报间隔（直接回车使用默认）: ").strip()
            try:
                interval = (
                    int(interval_input) if interval_input else config["report_interval"]
                )
            except ValueError:
                print("输入无效，使用默认值")
                interval = config["report_interval"]

            new_config = {
                "device_code": device_code,
                "device_name": device_name,
                "server_url": server_url,
                "working_path": working_path,
                "report_interval": interval,
                "manual_status": None,
                "is_first_run": False,
                "device_registered": False,
            }

            print("\n" + "=" * 60)
            print("配置预览:")
            print("=" * 60)
            print(f"设备编码: {new_config['device_code']}")
            print(f"设备名称: {new_config['device_name']}")
            print(f"服务器地址: {new_config['server_url']}")
            print(f"工作路径: {new_config['working_path']}")
            print(f"上报间隔: {new_config['report_interval']} 秒")
            print("=" * 60)

            confirm = input("\n确认配置并启动? (Y/n): ").strip().lower()
            if confirm == "n":
                print("配置已取消")
                return False

            self.config.update(new_config)
            self.initialize()
            return True

        except KeyboardInterrupt:
            print("\n\n配置已取消")
            return False
        except Exception as e:
            self.logger.error(f"配置错误: {e}")
            print(f"\n配置错误: {e}")
            return False

    def _register_device(self) -> bool:
        """注册设备

        Returns:
            bool: 是否成功
        """
        if self.config.is_device_registered():
            self.logger.info("设备已注册，跳过注册流程")
            return True

        self.logger.info("开始注册设备...")

        success = False
        if self.device_manager:
            success = self.device_manager.register_device(
                device_code=self.config.get_device_code(),
                device_name=self.config.get_device_name(),
            )

        if success:
            self.config.mark_device_registered()
            self.config.mark_configured()
            return True
        else:
            return False

    def _open_config(self):
        """打开配置窗口"""
        self.logger.info("打开配置窗口")
        from PyQt6.QtWidgets import QApplication

        if not QApplication.instance():
            app = QApplication(sys.argv)

        current_config = self.config.get_all()

        try:
            new_config = ConfigWindow.show_config_dialog(current_config, [])
            if new_config:
                old_device_code = self.config.get_device_code()
                old_server_url = self.config.get_server_url()
                old_working_path = self.config.get_working_path()

                self.config.update(new_config)

                if (
                    old_device_code != new_config["device_code"]
                    or old_server_url != new_config["server_url"]
                    or old_working_path != new_config["working_path"]
                ):
                    self.logger.info("配置已更改，需要重新初始化")
                    if self.status_reporter:
                        self.status_reporter.stop()
                    self.initialize()
                    self._register_device()
                    if self.status_reporter:
                        self.status_reporter.set_manual_status(
                            self.config.get_manual_status()
                        )
                        self.status_reporter.start()
                else:
                    self.logger.info("配置已更新")

            else:
                self.logger.info("配置未更改")

                if self.tray_icon:
                    self.tray_icon.show_notification(
                        "配置已更新", f"设备: {self.config.get_device_name()}"
                    )

        except Exception as e:
            self.logger.error(f"配置更新失败: {e}")
            if self.tray_icon:
                self.tray_icon.show_notification("配置更新失败", str(e))

    def _toggle_maintenance(self, status: Optional[str]):
        """切换维护模式

        Args:
            status: 'maintenance', 'error' 或 None
        """
        self.logger.info(f"切换到 {status or '正常'} 模式")
        self.config.set_manual_status(status)
        if self.status_reporter:
            self.status_reporter.set_manual_status(status)

    def _view_logs(self):
        """查看日志（命令行）"""
        self.logger.info("查看日志")
        print("\n" + "=" * 60)
        print("最近日志:")
        print("=" * 60)

        logs = self.logger.get_recent_logs(50)
        for log in logs:
            print(log.strip())

        print("=" * 60)
        print("日志文件位于: logs/")
        print("=" * 60)

    def _reconnect(self):
        """重新连接服务器"""
        self.logger.info("重新连接服务器...")

        if self.api_client and self.api_client.health_check():
            self.logger.info("服务器连接正常")
            if self.status_reporter:
                self.status_reporter.report_once()
            print("\n✓ 服务器连接正常")
            if self.tray_icon:
                self.tray_icon.show_notification("连接成功", "服务器连接正常")
        else:
            self.logger.error("服务器连接失败")
            print("\n✗ 服务器连接失败")
            if self.tray_icon:
                self.tray_icon.show_notification("连接失败", "无法连接到服务器")

    def _exit(self):
        """退出程序"""
        self.logger.info("正在退出程序...")

        if self.status_reporter:
            self.status_reporter.stop()

        if self.tray_icon:
            self.tray_icon.stop()

        self.logger.info("程序已退出")
        os._exit(0)

    def _show_error_and_exit(self, message: str):
        """显示错误并退出

        Args:
            message: 错误消息
        """
        self.logger.error(message)

        print("\n" + "=" * 60)
        print("错误")
        print("=" * 60)
        print(message)
        print("=" * 60)

        input("按回车键退出...")

        sys.exit(1)


def main():
    """主函数"""
    client = TextileDeviceClient()
    client.run()


if __name__ == "__main__":
    main()
