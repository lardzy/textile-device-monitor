"""
纺织品检测设备客户端 - 主程序
"""

import sys
import time
import os
import ctypes
import subprocess
import threading
import queue
from typing import Optional
from modules.config import Config, ConfigValidationError
from modules.logger import Logger
from modules.api_client import ApiClient
from modules.device_manager import DeviceManager
from modules.progress_reader import ProgressReader, OlympusProgressReader
from modules.metrics_collector import MetricsCollector
from modules.results_server import ResultsServer
from modules.transport_security import (
    CONFIG_SCHEMA_VERSION,
    TRANSPORT_COMPATIBLE,
    TransportSecurityError,
)

# Windows 控制台控制
try:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    HAS_WINDOWS_CONSOLE = True
except:
    HAS_WINDOWS_CONSOLE = False


def _is_windowed_runtime() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        and (sys.stdin is None or sys.stdout is None)
    )


def _set_runtime_working_directory() -> None:
    """Keep packaged config, certificates and logs relative to the EXE."""

    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(os.path.abspath(sys.executable)))


def _self_command(mode: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, mode]
    return [sys.executable, os.path.abspath(__file__), mode]


def _show_native_error(message: str) -> None:
    if not HAS_WINDOWS_CONSOLE:
        return
    try:
        user32.MessageBoxW(None, message, "纺织品检测设备客户端", 0x10)
    except Exception:
        pass


def _run_config_tool() -> int:
    from modules.config_window import ConfigWindow

    config = Config()
    old_config = config.get_all()
    new_config = ConfigWindow.show_config_dialog(
        old_config,
        DeviceManager.PRESET_DEVICES,
        config_base_directory=config.application_directory,
    )
    if new_config is None:
        return 2
    candidate = {**old_config, **new_config}
    transport_changed = config.is_first_run() or any(
        old_config.get(key) != candidate.get(key)
        for key in ("server_url", "transport_security", "tls_ca_bundle")
    )
    if transport_changed:
        logger = Logger(
            log_dir="logs",
            log_level=old_config.get("log_level", "INFO"),
        )
        try:
            ca_bundle = None
            if str(candidate.get("server_url", "")).startswith("https://"):
                ca_bundle = str(config.resolve_tls_ca_bundle(candidate))
            probe = ApiClient(
                candidate["server_url"],
                logger,
                transport_security=candidate.get(
                    "transport_security",
                    TRANSPORT_COMPATIBLE,
                ),
                tls_ca_bundle=ca_bundle,
            )
            if not probe.health_check():
                details = (
                    probe.last_tls_error_message
                    or probe.last_request_info.get("error_message")
                    or "服务器就绪检查失败"
                )
                raise RuntimeError(details)
        except (KeyError, RuntimeError, TransportSecurityError) as exc:
            message = f"新配置连接测试失败，原配置未被修改：{exc}"
            logger.error(message)
            _show_native_error(message)
            return 1
    return 0 if config.update(new_config) else 1


def _run_log_viewer() -> int:
    from modules.log_window import LogWindow

    logger = Logger(log_dir="logs", log_level="INFO")
    LogWindow.show_log_dialog(logger.get_recent_logs)
    return 0


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


def input_with_timeout(timeout=None, exit_check=None):
    """带超时的输入函数

    Args:
        timeout: 超时时间（秒），None 表示无超时
        exit_check: 退出检查函数，返回 True 表示应该退出

    Returns:
        str: 用户输入的字符串，超时或退出时返回 None
    """
    try:
        import msvcrt

        result = []
        start_time = time.time()
        first_char_printed = False

        while True:
            if exit_check and exit_check():
                return None

            if timeout and (time.time() - start_time) > timeout:
                return None

            if msvcrt.kbhit():
                ch = msvcrt.getch()

                if ch == b"\r":  # Enter
                    print()
                    return "".join(result)
                elif ch == b"\x03":  # Ctrl+C
                    raise KeyboardInterrupt()
                elif ch == b"\x08":  # Backspace
                    if result:
                        result.pop()
                        print("\b \b", end="", flush=True)
                else:
                    try:
                        char = ch.decode("utf-8")
                        if char.isprintable():
                            result.append(char)
                            if not first_char_printed:
                                print("> ", end="", flush=True)
                                first_char_printed = True
                            print(char, end="", flush=True)
                    except:
                        pass

            time.sleep(0.01)

    except ImportError:
        if exit_check and exit_check():
            return None
        return input()


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
        self.results_server = None
        self.tray_icon = None
        self._config_lock = threading.Lock()
        self._config_process = None
        self._log_process = None
        self._config_watcher = None
        self._should_exit = False

    def initialize(self):
        """初始化客户端"""
        self.logger.info("正在初始化客户端...")

        config = self.config.get_all()

        self.api_client = self._create_api_client(config)

        self.device_manager = DeviceManager(
            api_client=self.api_client, logger=self.logger
        )

        if config.get("is_laser_confocal"):
            self.progress_reader = OlympusProgressReader(
                log_path=config.get(
                    "log_path",
                    "C:\\ProgramData\\OLYMPUS\\LEXT-OLS50-SW\\Log\\Olympus.log",
                ),
                logger=self.logger,
                results_port=self.config.get_results_port(),
                server_url=config["server_url"],
            )
        else:
            self.progress_reader = ProgressReader(
                working_path=config["working_path"],
                logger=self.logger,
                results_port=self.config.get_results_port(),
                server_url=config["server_url"],
            )

        self.metrics_collector = MetricsCollector(logger=self.logger)

        self.results_server = ResultsServer(
            reader=self.progress_reader,
            logger=self.logger,
            port=self.config.get_results_port(),
        )

        if StatusReporter:
            self.status_reporter = StatusReporter(
                api_client=self.api_client,
                progress_reader=self.progress_reader,
                metrics_collector=self.metrics_collector,
                device_code=config["device_code"],
                logger=self.logger,
                report_interval=config["report_interval"],
                on_task_completed=(
                    self.results_server.prewarm_latest_formulas
                    if self.results_server
                    else None
                ),
            )

        if TrayIcon and self.tray_icon is None:
            self.tray_icon = TrayIcon(
                logger=self.logger,
                on_open_config=self._open_config,
                on_toggle_maintenance=self._toggle_maintenance,
                on_view_logs=self._view_logs,
                on_reconnect=self._reconnect,
                on_exit=self._exit,
            )

        self.logger.info("客户端初始化完成")

    def _create_api_client(self, config: dict) -> ApiClient:
        tls_ca_bundle = None
        if str(config.get("server_url", "")).startswith("https://"):
            tls_ca_bundle = str(self.config.resolve_tls_ca_bundle(config))
        return ApiClient(
            base_url=config["server_url"],
            logger=self.logger,
            transport_security=config.get(
                "transport_security",
                TRANSPORT_COMPATIBLE,
            ),
            tls_ca_bundle=tls_ca_bundle,
        )

    def run(self):
        """运行客户端"""
        try:
            if self.config.is_first_run():
                self.logger.info("首次运行，进入配置流程")
                if _is_windowed_runtime():
                    completed = subprocess.run(
                        _self_command("--config-tool"),
                        cwd=os.getcwd(),
                        check=False,
                    )
                    self.config.load()
                    if completed.returncode != 0 or self.config.is_first_run():
                        self.logger.warning("用户取消配置，程序退出")
                        return
                    self.initialize()
                else:
                    show_console()
                    if not self._show_config_dialog():
                        self.logger.warning("用户取消配置，程序退出")
                        return
                    hide_console()
            else:
                self.logger.info("加载已保存的配置")
                # 后续运行时隐藏控制台
                hide_console()
                self.initialize()

            if not self._register_device():
                self.logger.error("设备注册失败，程序退出")
                details = None
                if self.api_client:
                    details = (
                        self.api_client.last_tls_error_message
                        or self.api_client.last_request_info.get("error_message")
                    )
                self._show_error_and_exit(
                    details or "设备注册失败，请检查网络连接后重试"
                )
                return

            if self.status_reporter:
                self.status_reporter.set_manual_status(self.config.get_manual_status())
                self.status_reporter.start()

            if self.results_server:
                server_thread = threading.Thread(
                    target=self.results_server.start, daemon=True
                )
                server_thread.start()

            if self.tray_icon:
                self.tray_icon.start()

            self._start_config_watcher()

            print("\n" + "=" * 60)
            print("客户端已启动")
            print("=" * 60)
            print(
                f"设备: {self.config.get_device_name()} ({self.config.get_device_code()})"
            )
            print(f"服务器: {self.config.get_server_url()}")
            print(f"上报间隔: {self.config.get_report_interval()} 秒")
            print(f"结果服务端口: {self.config.get_results_port()}")
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

            self.config.set_last_mtime()

            while True:
                try:
                    cmd = input_with_timeout(
                        timeout=0.5, exit_check=lambda: self._should_exit
                    )

                    if cmd is None:
                        if self._should_exit:
                            break
                        continue

                    cmd = cmd.strip().lower()

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
                except EOFError:
                    if self._should_exit:
                        print("\n检测到退出请求")
                        break
                    continue
                except Exception as e:
                    print(f"命令执行失败: {e}")

            print("\n正在退出...")
            if not self._should_exit:
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

            confocal_input = input("\n是否激光共聚焦显微镜? (y/N): ").strip().lower()
            is_confocal = confocal_input in ("y", "yes")

            log_path = config.get(
                "log_path",
                "C:\\ProgramData\\OLYMPUS\\LEXT-OLS50-SW\\Log\\Olympus.log",
            )
            working_path = config["working_path"]

            if is_confocal:
                print(f"\n默认日志路径: {log_path}")
                log_input = input("请输入日志文件路径（直接回车使用默认）: ").strip()
                if log_input:
                    log_path = log_input
                working_path = ""
            else:
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
                "config_schema_version": CONFIG_SCHEMA_VERSION,
                "device_code": device_code,
                "device_name": device_name,
                "server_url": server_url,
                "transport_security": config.get(
                    "transport_security",
                    TRANSPORT_COMPATIBLE,
                ),
                "tls_ca_bundle": config.get(
                    "tls_ca_bundle",
                    "",
                ),
                "is_laser_confocal": is_confocal,
                "log_path": log_path,
                "working_path": working_path,
                "report_interval": interval,
                "results_port": config.get("results_port", 9100),
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
            print(
                "传输安全: "
                f"{new_config['transport_security']} "
                f"({new_config['tls_ca_bundle']})"
            )
            print(f"激光共聚焦: {'是' if new_config['is_laser_confocal'] else '否'}")
            if new_config["is_laser_confocal"]:
                print(f"日志路径: {new_config['log_path']}")
            print(f"工作路径: {new_config['working_path']}")
            print(f"上报间隔: {new_config['report_interval']} 秒")
            print(f"结果服务端口: {new_config['results_port']}")
            print("=" * 60)

            confirm = input("\n确认配置并启动? (Y/n): ").strip().lower()
            if confirm == "n":
                print("配置已取消")
                return False

            if not self.config.update(new_config):
                print(f"\n配置未保存：{self.config.last_load_error or '配置无效'}")
                return False
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
                client_base_url=self.progress_reader.get_client_base_url()
                if self.progress_reader
                else None,
            )

        if success:
            self.config.mark_device_registered()
            self.config.mark_configured()
            return True
        else:
            return False

    def _start_config_watcher(self):
        """启动配置文件监控线程"""

        def watcher():
            self.config.set_last_mtime()
            while True:
                try:
                    time.sleep(2)
                    if self.config.is_config_changed():
                        self.logger.info("检测到配置文件变更，重新加载...")
                        self._reload_config()
                        self.config.set_last_mtime()
                except Exception as e:
                    self.logger.error(f"配置文件监控错误: {e}")

        self._config_watcher = threading.Thread(target=watcher, daemon=True)
        self._config_watcher.start()
        self.logger.info("配置文件监控已启动")

    def _reload_config(self):
        """重新加载配置"""
        old_config = self.config.get_all()
        try:
            candidate = self.config.load_candidate()
        except ConfigValidationError as exc:
            self.logger.error(f"新配置无效，继续使用旧配置：{exc}")
            if self.tray_icon:
                self.tray_icon.show_notification(
                    "配置未应用",
                    f"新配置无效，已继续使用旧配置：{exc}",
                )
            return

        old_device_code = old_config.get("device_code")
        old_server_url = old_config.get("server_url")
        old_working_path = old_config.get("working_path")
        old_log_path = old_config.get("log_path")
        old_is_confocal = bool(old_config.get("is_laser_confocal"))
        old_port = int(old_config.get("results_port", 9100))
        old_report_interval = int(old_config.get("report_interval", 5))
        old_manual_status = old_config.get("manual_status")

        new_device_code = candidate.get("device_code")
        new_server_url = candidate.get("server_url")
        new_working_path = candidate.get("working_path")
        new_log_path = candidate.get("log_path")
        new_is_confocal = bool(candidate.get("is_laser_confocal"))
        new_port = int(candidate.get("results_port", 9100))
        new_report_interval = int(candidate.get("report_interval", 5))
        new_manual_status = candidate.get("manual_status")

        transport_changed = any(
            old_config.get(key) != candidate.get(key)
            for key in (
                "server_url",
                "transport_security",
                "tls_ca_bundle",
            )
        )
        requires_reinitialize = (
            transport_changed
            or old_device_code != new_device_code
            or old_server_url != new_server_url
            or old_working_path != new_working_path
            or old_log_path != new_log_path
            or old_is_confocal != new_is_confocal
            or old_port != new_port
            or old_report_interval != new_report_interval
        )
        probe_client = None
        if requires_reinitialize:
            try:
                probe_client = self._create_api_client(candidate)
                if transport_changed and not probe_client.health_check():
                    details = (
                        probe_client.last_tls_error_message
                        or probe_client.last_request_info.get("error_message")
                        or "服务器就绪检查失败"
                    )
                    raise RuntimeError(details)
            except (TransportSecurityError, RuntimeError) as exc:
                self.logger.error(f"新配置预检失败，继续使用旧运行实例：{exc}")
                if self.tray_icon:
                    self.tray_icon.show_notification(
                        "配置未应用",
                        f"{exc}；客户端仍使用原配置",
                    )
                return

        if requires_reinitialize:
            self.logger.info("配置已更改，需要重新初始化")
            self.config.apply_candidate(candidate)
            if self.status_reporter:
                self.status_reporter.stop()
            if self.results_server:
                self.results_server.stop()
            self.initialize()
            self._register_device()
            if self.status_reporter:
                self.status_reporter.set_manual_status(self.config.get_manual_status())
                self.status_reporter.start()
            if self.results_server:
                server_thread = threading.Thread(
                    target=self.results_server.start, daemon=True
                )
                server_thread.start()
            if self.tray_icon:
                self.tray_icon.show_notification("配置已更新", "新配置已自动应用")
        else:
            self.config.apply_candidate(candidate)
            if not self.config.is_device_registered():
                self._register_device()
            if old_manual_status != new_manual_status and self.status_reporter:
                self.status_reporter.set_manual_status(new_manual_status)
            self.logger.info("配置已更新")

    def _open_config(self):
        """打开配置窗口"""
        self.logger.info("打开配置窗口")
        self._launch_window_tool("--config-tool", "配置窗口", "_config_process")

    def _launch_window_tool(self, mode: str, label: str, process_attribute: str):
        """Launch a UI helper as a separate instance of this executable."""
        with self._config_lock:
            current_process = getattr(self, process_attribute)
            if current_process is not None and current_process.poll() is None:
                self.logger.warning(f"{label}已打开，忽略重复请求")
                return

            try:
                process = subprocess.Popen(_self_command(mode), cwd=os.getcwd())
            except Exception as exc:
                self.logger.exception(f"启动{label}失败: {exc}")
                if self.tray_icon:
                    self.tray_icon.show_notification(f"{label}启动失败", str(exc))
                return
            setattr(self, process_attribute, process)

        def wait_for_tool() -> None:
            return_code = process.wait()
            if return_code not in (0, 2):
                self.logger.error(f"{label}异常退出，退出码: {return_code}")
                if self.tray_icon:
                    self.tray_icon.show_notification(
                        f"{label}异常退出", f"退出码: {return_code}"
                    )
            with self._config_lock:
                if getattr(self, process_attribute) is process:
                    setattr(self, process_attribute, None)

        threading.Thread(target=wait_for_tool, daemon=True).start()
        self.logger.info(f"{label}已启动")

    def _toggle_maintenance(self, status: Optional[str]):
        """切换维护模式

        Args:
            status: 'maintenance', 'error' 或 None
        """
        self.logger.info(f"切换到 {status or '正常'} 模式")
        self.config.set_manual_status(status)
        if self.status_reporter:
            self.status_reporter.set_manual_status(status)
        if self.tray_icon:
            self.tray_icon.update_status(status or "idle")

    def _view_logs(self):
        """打开日志查看窗口"""
        self.logger.info("查看日志")
        self._launch_window_tool("--log-viewer", "日志窗口", "_log_process")

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
            details = None
            if self.api_client:
                details = (
                    self.api_client.last_tls_error_message
                    or self.api_client.last_request_info.get("error_message")
                )
            print("\n✗ 服务器连接失败")
            if self.tray_icon:
                self.tray_icon.show_notification(
                    "连接失败",
                    details or "无法连接到服务器",
                )

    def _exit(self):
        """退出程序"""
        self.logger.info("正在退出程序...")
        self._should_exit = True

        if self.status_reporter:
            self.status_reporter.stop()

        if self.results_server:
            self.results_server.stop()

        self.logger.info("程序已退出")

        if self.tray_icon:
            self.tray_icon.stop()

    def _show_error_and_exit(self, message: str):
        """显示错误并退出

        Args:
            message: 错误消息
        """
        self.logger.error(message)

        if _is_windowed_runtime():
            _show_native_error(message)
            raise SystemExit(1)

        print("\n" + "=" * 60)
        print("错误")
        print("=" * 60)
        print(message)
        print("=" * 60)

        input("按回车键退出...")

        sys.exit(1)


def main() -> int:
    """主函数"""
    _set_runtime_working_directory()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "--config-tool":
            return _run_config_tool()
        if mode == "--log-viewer":
            return _run_log_viewer()
    except Exception as exc:
        _show_native_error(str(exc))
        return 1

    client = TextileDeviceClient()
    client.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
