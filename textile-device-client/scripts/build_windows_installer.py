from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_support import (
    DEFAULT_TIMESTAMP_URL,
    EXECUTABLE_NAME,
    BuildValidationError,
    read_app_version,
    sign_windows_file,
    validate_release_build,
    verify_windows_signature,
    write_installer_version_include,
    write_sha256_file,
)


def find_inno_setup_compiler() -> str | None:
    env_override = os.environ.get("ISCC_EXE", "").strip()
    if env_override:
        candidate = Path(env_override).expanduser()
        if candidate.exists():
            return str(candidate)

    for executable_name in ("ISCC.exe", "ISCC", "iscc.exe", "iscc"):
        resolved = shutil.which(executable_name)
        if resolved:
            return resolved

    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name, "").strip()
        if not base:
            continue
        candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
        if candidate.exists():
            return str(candidate)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        for relative_path in (
            Path("Programs") / "Inno Setup 6" / "ISCC.exe",
            Path("Inno Setup 6") / "ISCC.exe",
        ):
            candidate = Path(local_app_data) / relative_path
            if candidate.exists():
                return str(candidate)

    try:
        import winreg

        uninstall_key = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for registry_view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(
                        hive,
                        uninstall_key,
                        access=winreg.KEY_READ | registry_view,
                    ) as key:
                        install_location, _ = winreg.QueryValueEx(key, "InstallLocation")
                except OSError:
                    continue
                candidate = Path(install_location) / "ISCC.exe"
                if candidate.is_file():
                    return str(candidate)
    except ImportError:
        pass

    return None


def build_installer(
    *,
    root: Path,
    sync_only: bool = False,
    compiler_path: str | None = None,
    sign: bool = False,
    signtool_path: str | None = None,
    certificate_thumbprint: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
) -> int:
    iss_path = root / "packaging" / "inno-setup" / "textile_device_client.iss"
    app_dir = root / "dist" / "windows" / "TextileDeviceClient"

    if not iss_path.exists():
        print(f"Inno Setup script not found: {iss_path}", file=sys.stderr)
        return 1

    version_include = write_installer_version_include(root)
    version = read_app_version(root)
    print(f"Synchronized installer version include: {version_include} -> {version}")

    if sync_only:
        print(
            "Sync only mode: version.auto.iss has been refreshed from "
            "modules/version.py"
        )
        return 0

    if not app_dir.exists():
        print(
            "PyInstaller output not found. Build the onedir package first:\n"
            "  python scripts/build_windows_onedir.py",
            file=sys.stderr,
        )
        return 1

    try:
        manifest = validate_release_build(root, app_dir, require_signed=sign)
        if sign:
            verify_windows_signature(
                app_dir / EXECUTABLE_NAME,
                signtool_path=signtool_path,
            )
    except (BuildValidationError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if compiler_path:
        compiler_candidate = Path(compiler_path).expanduser()
        resolved_compiler = str(compiler_candidate) if compiler_candidate.is_file() else None
    else:
        resolved_compiler = find_inno_setup_compiler()
    if not resolved_compiler:
        print(
            "Inno Setup compiler not found. Install Inno Setup 6, pass --compiler, "
            "or set ISCC_EXE.",
            file=sys.stderr,
        )
        return 1

    output_dir = root / "dist" / "installer"
    expected_output = output_dir / f"textile-device-client-setup-{version}.exe"
    checksum_output = expected_output.with_name(f"{expected_output.name}.sha256")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        expected_output.unlink(missing_ok=True)
        checksum_output.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Unable to remove the previous installer artifact: {exc}", file=sys.stderr)
        return 1

    command = [resolved_compiler, str(iss_path)]
    print("Running Inno Setup:")
    print(" ".join(command))
    try:
        subprocess.run(command, cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Inno Setup build failed: {exc}", file=sys.stderr)
        return 1

    if not expected_output.is_file():
        print(
            f"Inno Setup completed without creating the expected output: {expected_output}",
            file=sys.stderr,
        )
        return 1

    try:
        if sign:
            sign_windows_file(
                expected_output,
                signtool_path=signtool_path,
                certificate_thumbprint=certificate_thumbprint,
                timestamp_url=timestamp_url,
            )
        checksum_path = write_sha256_file(expected_output, checksum_output)
    except (BuildValidationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Unable to finalize the installer artifact: {exc}", file=sys.stderr)
        return 1

    print("\nInstaller build completed.")
    print(f"Output directory: {output_dir}")
    print(f"Installer: {expected_output}")
    print(f"Signed: {'yes' if sign else 'no'}")
    print(f"Checksum: {checksum_path}")
    print(f"Source fingerprint: {manifest['source_sha256']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync installer version metadata and optionally build the Inno Setup "
            "installer."
        ),
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Only refresh packaging/inno-setup/version.auto.iss from modules/version.py.",
    )
    parser.add_argument(
        "--compiler",
        default="",
        help=(
            "Optional full path to ISCC.exe. If omitted, the script will try "
            "PATH / common install locations / ISCC_EXE."
        ),
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help=(
            "Require a signed release onedir build and Authenticode-sign the "
            "generated installer."
        ),
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

    root = Path(__file__).resolve().parents[1]
    compiler = args.compiler.strip() or None
    return build_installer(
        root=root,
        sync_only=args.sync_only,
        compiler_path=compiler,
        sign=args.sign,
        signtool_path=args.signtool.strip() or None,
        certificate_thumbprint=args.certificate_thumbprint.strip() or None,
        timestamp_url=args.timestamp_url.strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
