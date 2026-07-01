from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_support import write_installer_version_include


def build(clean: bool, *, console: bool, bootloader_debug: bool) -> int:
    root = Path(__file__).resolve().parents[1]
    spec_path = root / "packaging" / "pyinstaller" / "textile_device_client.spec"
    dist_path = root / "dist" / "windows"
    work_path = root / "build" / "pyinstaller"

    if not spec_path.exists():
        print(f"Spec file not found: {spec_path}", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed in the current environment.\n"
            "Please install it before building: pip install pyinstaller",
            file=sys.stderr,
        )
        return 1

    if clean:
        shutil.rmtree(dist_path, ignore_errors=True)
        shutil.rmtree(work_path, ignore_errors=True)

    dist_path.mkdir(parents=True, exist_ok=True)
    work_path.mkdir(parents=True, exist_ok=True)
    installer_version_file = write_installer_version_include(root)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
        str(spec_path),
    ]

    env = os.environ.copy()
    env["TDC_PYINSTALLER_CONSOLE"] = "1" if console else "0"
    env["TDC_PYINSTALLER_BOOTLOADER_DEBUG"] = "1" if bootloader_debug else "0"

    print("Running PyInstaller:")
    print(" ".join(command))
    subprocess.run(command, cwd=root, check=True, env=env)

    app_dir = dist_path / "TextileDeviceClient"
    print("\nBuild completed.")
    print(f"Output directory: {app_dir}")
    print(f"Console mode: {'on' if console else 'off'}")
    print(f"Bootloader debug: {'on' if bootloader_debug else 'off'}")
    print(f"Main executable: {app_dir / 'textile-device-client.exe'}")
    print(f"Installer version include: {installer_version_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the Windows onedir package with PyInstaller."
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Keep the existing dist/windows and build/pyinstaller contents before building.",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Build a console-enabled executable for troubleshooting startup issues.",
    )
    parser.add_argument(
        "--bootloader-debug",
        action="store_true",
        help="Enable PyInstaller bootloader debug output in the built executable.",
    )
    args = parser.parse_args()
    return build(
        clean=not args.no_clean,
        console=args.console,
        bootloader_debug=args.bootloader_debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
