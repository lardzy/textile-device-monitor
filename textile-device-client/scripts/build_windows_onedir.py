from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_support import (
    BUILD_MANIFEST_NAME,
    DEFAULT_TIMESTAMP_URL,
    EXECUTABLE_NAME,
    BuildValidationError,
    create_isolated_build_environment,
    ensure_build_environment,
    sign_windows_file,
    write_build_manifest,
    write_installer_version_include,
    write_pyinstaller_version_file,
    write_sha256_file,
)


def build(
    clean: bool,
    *,
    console: bool,
    bootloader_debug: bool,
    sign: bool = False,
    signtool_path: str | None = None,
    certificate_thumbprint: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
) -> int:
    root = Path(__file__).resolve().parents[1]
    spec_path = root / "packaging" / "pyinstaller" / "textile_device_client.spec"
    dist_path = root / "dist" / "windows"
    work_path = root / "build" / "pyinstaller"
    generated_path = root / "build" / "generated"
    app_dir = dist_path / "TextileDeviceClient"
    executable = app_dir / EXECUTABLE_NAME

    if not spec_path.exists():
        print(f"Spec file not found: {spec_path}", file=sys.stderr)
        return 1

    try:
        ensure_build_environment(root)
    except BuildValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if clean:
            if dist_path.exists():
                shutil.rmtree(dist_path)
            if work_path.exists():
                shutil.rmtree(work_path)
        else:
            stale_manifest = app_dir / BUILD_MANIFEST_NAME
            stale_manifest.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Unable to clean previous build output: {exc}", file=sys.stderr)
        return 1

    dist_path.mkdir(parents=True, exist_ok=True)
    work_path.mkdir(parents=True, exist_ok=True)
    generated_path.mkdir(parents=True, exist_ok=True)
    installer_version_file = write_installer_version_include(root)
    pyinstaller_version_file = write_pyinstaller_version_file(
        root,
        generated_path / "textile_device_client_version.txt",
    )

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
    ]
    if clean:
        command.append("--clean")
    command.extend(
        [
            "--distpath",
            str(dist_path),
            "--workpath",
            str(work_path),
            str(spec_path),
        ]
    )

    env = create_isolated_build_environment()
    env["TDC_PYINSTALLER_CONSOLE"] = "1" if console else "0"
    env["TDC_PYINSTALLER_BOOTLOADER_DEBUG"] = "1" if bootloader_debug else "0"
    env["TDC_PYINSTALLER_VERSION_FILE"] = str(pyinstaller_version_file)

    print("Running PyInstaller:")
    print(" ".join(command))
    try:
        subprocess.run(command, cwd=root, check=True, env=env)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"PyInstaller build failed: {exc}", file=sys.stderr)
        return 1

    if not executable.is_file():
        print(f"PyInstaller did not create the expected executable: {executable}", file=sys.stderr)
        return 1

    try:
        if sign:
            sign_windows_file(
                executable,
                signtool_path=signtool_path,
                certificate_thumbprint=certificate_thumbprint,
                timestamp_url=timestamp_url,
            )
        manifest_path = write_build_manifest(
            root,
            app_dir,
            console=console,
            bootloader_debug=bootloader_debug,
            signed=sign,
        )
        checksum_path = write_sha256_file(executable)
    except (BuildValidationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Unable to finalize the build output: {exc}", file=sys.stderr)
        return 1

    print("\nBuild completed.")
    print(f"Output directory: {app_dir}")
    print(f"Console mode: {'on' if console else 'off'}")
    print(f"Bootloader debug: {'on' if bootloader_debug else 'off'}")
    print(f"Signed: {'yes' if sign else 'no'}")
    print(f"Main executable: {executable}")
    print(f"Build manifest: {manifest_path}")
    print(f"Executable checksum: {checksum_path}")
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
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Sign the executable with Authenticode after PyInstaller completes.",
    )
    parser.add_argument(
        "--signtool",
        default="",
        help="Optional full path to signtool.exe (or set SIGNTOOL_EXE).",
    )
    parser.add_argument(
        "--certificate-thumbprint",
        default="",
        help=(
            "SHA-1 thumbprint of the code-signing certificate in the Windows "
            "certificate store (or set TDC_SIGN_CERT_THUMBPRINT)."
        ),
    )
    parser.add_argument(
        "--timestamp-url",
        default=DEFAULT_TIMESTAMP_URL,
        help="RFC 3161 timestamp server used when --sign is enabled.",
    )
    args = parser.parse_args()
    return build(
        clean=not args.no_clean,
        console=args.console,
        bootloader_debug=args.bootloader_debug,
        sign=args.sign,
        signtool_path=args.signtool.strip() or None,
        certificate_thumbprint=args.certificate_thumbprint.strip() or None,
        timestamp_url=args.timestamp_url.strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
