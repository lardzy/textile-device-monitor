from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_support import read_app_version, write_installer_version_include


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
        for folder_name in ("Inno Setup 6", "Inno Setup 5"):
            candidate = Path(base) / folder_name / "ISCC.exe"
            if candidate.exists():
                return str(candidate)

    return None


def build_installer(
    *,
    root: Path,
    sync_only: bool = False,
    compiler_path: str | None = None,
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

    resolved_compiler = compiler_path or find_inno_setup_compiler()
    if not resolved_compiler:
        print(
            "Inno Setup compiler not found. version.auto.iss has been refreshed, "
            "so you can now\n"
            "open packaging/inno-setup/textile_device_client.iss manually, or set "
            "ISCC_EXE to compile automatically.",
            file=sys.stderr,
        )
        return 0

    command = [resolved_compiler, str(iss_path)]
    print("Running Inno Setup:")
    print(" ".join(command))
    subprocess.run(command, cwd=root, check=True)

    output_dir = root / "dist" / "installer"
    print("\nInstaller build completed.")
    print(f"Output directory: {output_dir}")
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
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    compiler = args.compiler.strip() or None
    return build_installer(root=root, sync_only=args.sync_only, compiler_path=compiler)


if __name__ == "__main__":
    raise SystemExit(main())
