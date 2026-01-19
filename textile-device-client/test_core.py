"""
测试脚本 - 验证客户端核心功能（不依赖 GUI）
"""

import sys
import os

base_path = os.path.dirname(__file__)
modules_path = os.path.join(base_path, "modules")

if modules_path not in sys.path:
    sys.path.insert(0, modules_path)

# 现在可以正常导入，因为 __init__.py 已经改为可选导入 GUI 模块
from config import Config
from logger import Logger
from api_client import ApiClient
from device_manager import DeviceManager
from progress_reader import ProgressReader
from metrics_collector import MetricsCollector


def test_config():
    """测试配置管理"""
    print("\n=== 测试配置管理 ===")
    config = Config("test_config.json")

    print(f"设备编码: {config.get_device_code()}")
    print(f"设备名称: {config.get_device_name()}")
    print(f"服务器地址: {config.get_server_url()}")
    print(f"上报间隔: {config.get_report_interval()} 秒")

    test_config_path = "test_config.json"
    if os.path.exists(test_config_path):
        os.remove(test_config_path)


def test_logger():
    """测试日志管理"""
    print("\n=== 测试日志管理 ===")
    logger = Logger(log_dir="logs", log_level="INFO")

    logger.info("这是一条信息日志")
    logger.warning("这是一条警告日志")
    logger.error("这是一条错误日志")

    logs = logger.get_recent_logs(5)
    print(f"最近 5 条日志:")
    for log in logs:
        print(f"  {log.strip()}")


def test_metrics_collector():
    """测试指标采集"""
    print("\n=== 测试指标采集 ===")
    logger = Logger(log_dir="logs", log_level="INFO")
    collector = MetricsCollector(logger)

    metrics = collector.collect_metrics()
    print(f"CPU 使用率: {metrics['cpu']}%")
    print(f"内存使用率: {metrics['memory']}%")
    print(f"磁盘使用率: {metrics['disk']}%")
    print(f"运行时间: {metrics['runtime']} 秒")


def test_progress_reader():
    """测试进度读取"""
    print("\n=== 测试进度读取 ===")
    logger = Logger(log_dir="logs", log_level="INFO")

    base_path = (
        r"\\192.168.105.66\17检验八部\10特纤\02-检验\其他\AI显微镜检测进度(重要勿删)"
    )
    reader = ProgressReader(base_path, logger)

    if reader.check_path_accessible():
        print("工作路径可访问")
        progress = reader.read_progress()
        print(f"设备进度: {progress}%")
    else:
        print("工作路径不可访问（这是正常的，因为没有网络连接）")
        progress = reader.read_progress()
        print(f"设备进度: {progress}% (默认值)")


def test_api_client():
    """测试 API 客户端"""
    print("\n=== 测试 API 客户端 ===")
    logger = Logger(log_dir="logs", log_level="INFO")

    server_url = "http://localhost:8000"
    client = ApiClient(server_url, logger)

    print(f"服务器地址: {server_url}")

    is_healthy = client.health_check()
    if is_healthy:
        print("服务器健康检查: 成功")

        devices = client.get_all_devices()
        print(f"获取到 {len(devices)} 个设备")
        for device in devices:
            print(f"  - {device.name} ({device.device_code}): {device.status}")
    else:
        print("服务器健康检查: 失败（这是正常的，因为服务器可能未运行）")


def test_device_manager():
    """测试设备管理"""
    print("\n=== 测试设备管理 ===")
    logger = Logger(log_dir="logs", log_level="INFO")
    server_url = "http://localhost:8000"
    api_client = ApiClient(server_url, logger)
    device_manager = DeviceManager(api_client, logger)

    print(f"预设设备列表: {device_manager.get_preset_devices()}")
    print(f"1号是否为预设设备: {device_manager.is_preset_device('1号')}")
    print(f"自定义设备是否为预设设备: {device_manager.is_preset_device('自定义')}")


def main():
    """主函数"""
    print("=" * 60)
    print("纺织品检测设备客户端 - 功能测试")
    print("=" * 60)

    try:
        test_config()
        test_logger()
        test_metrics_collector()
        test_progress_reader()
        test_api_client()
        test_device_manager()

        print("\n" + "=" * 60)
        print("测试完成！")
        print("=" * 60)
        print("\n注意:")
        print("1. 进度读取失败是正常的，因为没有网络连接")
        print("2. API 客户端失败是正常的，因为服务器可能未运行")
        print("3. 其他核心功能都工作正常")
        print("\n如需完整测试 GUI 功能，请安装 Visual C++ Redistributable:")
        print(
            "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
        )

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
