"""
PyInstaller 打包脚本
"""

import PyInstaller.__main__
import os
import shutil

APP_NAME = "textile-device-client"
VERSION = "1.0.0"


def clean_build():
    """清理旧的构建文件"""
    import datetime

    # 备份旧的 dist 目录（避免删除占用中的文件）
    if os.path.exists("dist"):
        backup_name = f"dist_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            os.rename("dist", backup_name)
            print(f"已备份旧版本: {backup_name}")
        except:
            print("无法备份旧版本，继续打包")

    # 清理 build 目录
    if os.path.exists("build"):
        try:
            shutil.rmtree("build")
            print(f"已删除: build")
        except:
            print("无法删除 build 目录，继续打包")


def build():
    """执行打包 - 生成两个版本"""
    clean_build()

    print("开始打包...")
    print("\n[1/2] 打包带控制台版本（用于首次配置）...")

    # 版本1: 带控制台（首次配置用）
    PyInstaller.__main__.run(
        [
            "main.py",
            "--name=" + APP_NAME,
            "--onedir",
            "--clean",
            "--noconfirm",
            "--console",  # 显示控制台
            "--hidden-import=PyQt6",
            "--hidden-import=PyQt6.QtWidgets",
            "--hidden-import=PyQt6.QtCore",
            "--hidden-import=pystray",
            "--hidden-import=PIL",
            "--hidden-import=psutil",
            "--hidden-import=requests",
            "--paths=.",
        ]
    )

    # 将带控制台版本复制到 dist 作为默认版本
    if os.path.exists("dist/textile-device-client"):
        if not os.path.exists("dist/textile-device-client-console"):
            shutil.copytree(
                "dist/textile-device-client", "dist/textile-device-client-console"
            )
        print("✓ 带控制台版本: dist/textile-device-client-console/")

    print("\n[2/2] 打包无控制台版本（用于日常运行）...")

    # 版本2: 无控制台（日常运行用）
    PyInstaller.__main__.run(
        [
            "main.py",
            "--name=" + APP_NAME + "-silent",
            "--onedir",
            "--noconfirm",
            "--windowed",  # 隐藏控制台
            "--hidden-import=PyQt6",
            "--hidden-import=PyQt6.QtWidgets",
            "--hidden-import=PyQt6.QtCore",
            "--hidden-import=pystray",
            "--hidden-import=PIL",
            "--hidden-import=psutil",
            "--hidden-import=requests",
            "--paths=.",
        ]
    )

    # 将无控制台版本复制到 dist 作为默认版本
    if os.path.exists("dist/textile-device-client-silent"):
        if os.path.exists("dist/textile-device-client"):
            shutil.rmtree("dist/textile-device-client", ignore_errors=True)
        shutil.copytree(
            "dist/textile-device-client-silent", "dist/textile-device-client"
        )
        print("✓ 无控制台版本: dist/textile-device-client/")

    print(f"\n打包完成！")
    print("=" * 60)
    print("生成的版本:")
    print("  1. dist/textile-device-client-console/ - 带控制台（首次配置用）")
    print("  2. dist/textile-device-client/        - 无控制台（日常运行用）")
    print("=" * 60)


if __name__ == "__main__":
    build()
