"""
测试配置加载逻辑
"""

import os
import sys

# 确保在正确的目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.config import Config


def test_config_loading():
    """测试配置加载"""
    print("=" * 60)
    print("测试配置加载逻辑")
    print("=" * 60)

    # 测试1：加载现有配置
    print("\n【测试1】加载现有配置文件")
    config = Config("config.json")

    print(f"  - 设备编码: {config.get_device_code()}")
    print(f"  - 设备名称: {config.get_device_name()}")
    print(f"  - 服务器地址: {config.get_server_url()}")
    print(f"  - 是否首次运行: {config.is_first_run()}")
    print(f"  - 设备是否已注册: {config.is_device_registered()}")

    # 验证is_first_run应该是False
    if config.is_first_run():
        print("\n  [X] 错误：is_first_run 应该是 False")
    else:
        print("\n  [OK] 正确：is_first_run 是 False")

    # 验证device_registered应该是True
    if config.is_device_registered():
        print("  [OK] 正确：device_registered 是 True")
    else:
        print("  [X] 错误：device_registered 应该是 True")

    # 测试2：标记为已配置
    print("\n【测试2】标记为已配置")
    success = config.mark_configured()
    print(f"  - 保存成功: {success}")
    print(f"  - is_first_run: {config.is_first_run()}")

    if not config.is_first_run():
        print("  [OK] 正确：is_first_run 仍然是 False")
    else:
        print("  [X] 错误：is_first_run 应该是 False")

    # 测试3：创建新配置
    print("\n【测试3】创建新配置文件")
    test_config = Config("test_new_config.json")
    print(f"  - 新配置 is_first_run: {test_config.is_first_run()}")

    if test_config.is_first_run():
        print("  [OK] 正确：新配置 is_first_run 是 True")
    else:
        print("  [X] 错误：新配置 is_first_run 应该是 True")

    # 标记为已配置
    test_config.mark_configured()
    test_config.mark_device_registered()
    print(f"  - 标记后 is_first_run: {test_config.is_first_run()}")
    print(f"  - 标记后 device_registered: {test_config.is_device_registered()}")

    # 重新加载
    print("\n【测试4】重新加载配置文件")
    reloaded_config = Config("test_new_config.json")
    print(f"  - 重新加载 is_first_run: {reloaded_config.is_first_run()}")
    print(f"  - 重新加载 device_registered: {reloaded_config.is_device_registered()}")

    if not reloaded_config.is_first_run() and reloaded_config.is_device_registered():
        print("  [OK] 正确：配置持久化成功")
    else:
        print("  [X] 错误：配置持久化失败")

    # 清理测试文件
    if os.path.exists("test_new_config.json"):
        os.remove("test_new_config.json")
        print("\n清理测试文件")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_config_loading()
