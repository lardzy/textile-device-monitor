"""Compatibility wrapper for the Windows onedir build script."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    scripts_path = script_dir / "scripts"
    if str(scripts_path) not in sys.path:
        sys.path.insert(0, str(scripts_path))

    from build_windows_onedir import main as build_onedir_main

    print("build.py is a compatibility wrapper.")
    print("Preferred command: python scripts/build_windows_onedir.py")
    return build_onedir_main()


if __name__ == "__main__":
    raise SystemExit(main())
