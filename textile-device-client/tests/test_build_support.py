from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch


CLIENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = CLIENT_ROOT / "scripts"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from build_support import (
    collect_hidden_imports,
    read_app_version,
    write_installer_version_include,
)
from build_windows_installer import find_inno_setup_compiler


class BuildSupportTests(unittest.TestCase):
    def test_read_app_version_matches_runtime_version(self):
        version_payload = runpy.run_path(str(CLIENT_ROOT / "modules" / "version.py"))
        __version__ = version_payload["__version__"]

        self.assertEqual(read_app_version(CLIENT_ROOT), __version__)

    def test_write_installer_version_include(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            modules_dir = root / "modules"
            modules_dir.mkdir()
            (modules_dir / "version.py").write_text(
                '__version__ = "9.8.7"\n', encoding="utf-8"
            )

            output = write_installer_version_include(root)

            self.assertEqual(
                output, root / "packaging" / "inno-setup" / "version.auto.iss"
            )
            self.assertIn(
                '#define MyAppVersion "9.8.7"', output.read_text(encoding="utf-8")
            )

    def test_collect_hidden_imports_deduplicates_base_and_formulas(self):
        imports = collect_hidden_imports()

        self.assertIn("formulas", imports)
        self.assertEqual(len(imports), len(set(imports)))

    def test_find_inno_setup_compiler_prefers_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = Path(tmpdir) / "ISCC.exe"
            compiler.write_text("", encoding="utf-8")

            with patch.dict(os.environ, {"ISCC_EXE": str(compiler)}, clear=False):
                self.assertEqual(find_inno_setup_compiler(), str(compiler))


if __name__ == "__main__":
    unittest.main()
