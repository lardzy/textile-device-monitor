from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import os
import json
import importlib.util
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


CLIENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = CLIENT_ROOT / "scripts"
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from build_support import (
    BUILD_MANIFEST_NAME,
    EXECUTABLE_NAME,
    BuildValidationError,
    calculate_source_fingerprint,
    collect_hidden_imports,
    create_isolated_build_environment,
    prepare_tls_build_assets,
    read_app_version,
    read_build_lock,
    validate_release_build,
    write_build_manifest,
    write_installer_version_include,
    write_pyinstaller_version_file,
)
from build_windows_installer import build_installer, find_inno_setup_compiler


@lru_cache(maxsize=None)
def build_test_root_ca(common_name: str = "Inspection Test Root CA") -> bytes:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=1),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def create_minimal_project(
    root: Path,
    version: str = "9.8.7",
    *,
    use_https: bool = False,
) -> Path:
    files = {
        "main.py": "print('client')\n",
        "requirements.txt": "requests>=2.31.0\n",
        "requirements-build.lock.txt": "requests==2.34.2\n",
        "modules/version.py": f'__version__ = "{version}"\n',
        "resources/icon.ico": "icon",
        "packaging/pyinstaller/textile_device_client.spec": "# spec\n",
        "packaging/inno-setup/textile_device_client.iss": "# installer\n",
        "scripts/build_support.py": "# build support\n",
        "scripts/build_windows_installer.py": "# installer\n",
        "scripts/build_windows_onedir.py": "# onedir\n",
        "scripts/build_windows_release.py": "# release\n",
        "scripts/migrate_to_internal_https.ps1": "# migration\n",
        "scripts/verify_client_https_reporting.ps1": "# verification\n",
    }
    for relative_path, payload in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    app_dir = root / "dist" / "windows" / "TextileDeviceClient"
    app_dir.mkdir(parents=True)
    (app_dir / EXECUTABLE_NAME).write_bytes(b"fake executable")
    if use_https:
        (app_dir / "certs").mkdir()
        (app_dir / "certs" / "inspection-root-ca.pem").write_bytes(
            build_test_root_ca()
        )
    (app_dir / "client-build-defaults.json").write_text(
        json.dumps(
            {
                "config_schema_version": 2,
                "server_url": (
                    "https://textile-monitor.internal"
                    if use_https
                    else "http://127.0.0.1"
                ),
                "transport_security": (
                    "required" if use_https else "compatible"
                ),
                "tls_ca_bundle": (
                    "certs/inspection-root-ca.pem" if use_https else ""
                ),
            }
        ),
        encoding="utf-8",
    )
    (app_dir / "admin-tools").mkdir()
    for name in (
        "migrate_to_internal_https.ps1",
        "verify_client_https_reporting.ps1",
        "Install-InspectionTlsTrust.ps1",
        "Restore-InspectionTlsTrust.ps1",
        "InspectionTls.Common.ps1",
    ):
        (app_dir / "admin-tools" / name).write_text(
            f"# {name}\n",
            encoding="utf-8",
        )
    return app_dir


class BuildSupportTests(unittest.TestCase):
    def test_isolated_build_environment_drops_foreign_dll_paths(self):
        foreign_path = r"E:\Software\anaconda3\Library\bin"
        with patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join((foreign_path, r"C:\Windows\System32")),
                "PYTHONPATH": r"E:\foreign-python-modules",
            },
            clear=False,
        ):
            environment = create_isolated_build_environment()

        self.assertNotIn(foreign_path.lower(), environment["PATH"].lower())
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertIn(str(Path(sys.executable).parent), environment["PATH"])

    def test_read_app_version_matches_runtime_version(self):
        version_payload = runpy.run_path(str(CLIENT_ROOT / "modules" / "version.py"))
        __version__ = version_payload["__version__"]

        self.assertEqual(read_app_version(CLIENT_ROOT), __version__)

    def test_write_installer_version_include(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "modules").mkdir()
            (root / "modules" / "version.py").write_text(
                '__version__ = "9.8.7"\n', encoding="utf-8"
            )

            output = write_installer_version_include(root)

            self.assertEqual(
                output, root / "packaging" / "inno-setup" / "version.auto.iss"
            )
            self.assertIn(
                '#define MyAppVersion "9.8.7"', output.read_text(encoding="utf-8")
            )

    def test_write_pyinstaller_version_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "modules").mkdir()
            (root / "modules" / "version.py").write_text(
                '__version__ = "1.2.3"\n', encoding="utf-8"
            )

            output = write_pyinstaller_version_file(root, root / "version.txt")
            payload = output.read_text(encoding="utf-8")

            self.assertIn("filevers=(1, 2, 3, 0)", payload)
            self.assertIn("StringStruct('ProductVersion', '1.2.3')", payload)

    @unittest.skipUnless(
        importlib.util.find_spec("PyInstaller") is not None,
        "PyInstaller is only installed in the locked Windows build environment",
    )
    def test_collect_hidden_imports_excludes_optional_formulas_apps(self):
        imports = collect_hidden_imports()

        self.assertIn("formulas", imports)
        self.assertNotIn("formulas.app", imports)
        self.assertNotIn("formulas.cli", imports)
        self.assertNotIn("formulas.excel.ods_reader", imports)
        self.assertEqual(len(imports), len(set(imports)))

    def test_build_lock_rejects_unpinned_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "requirements-build.lock.txt").write_text(
                "requests>=2.31.0\n", encoding="utf-8"
            )

            with self.assertRaises(BuildValidationError):
                read_build_lock(root)

    def test_http_build_assets_do_not_require_a_ca_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            defaults_path, ca_path = prepare_tls_build_assets(
                root / "generated",
                default_server_url="http://192.168.106.50:8080/",
                tls_ca_bundle=None,
            )

            defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
            self.assertEqual(defaults["server_url"], "http://192.168.106.50:8080")
            self.assertEqual(defaults["transport_security"], "compatible")
            self.assertEqual(defaults["tls_ca_bundle"], "")
            self.assertIsNone(ca_path)

    def test_https_build_assets_require_fixed_production_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ca_path = root / "root.pem"
            ca_path.write_bytes(build_test_root_ca())

            for invalid_origin in (
                "https://other.internal",
                "https://textile-monitor.internal:8443",
                "https://textile-monitor.internal?",
                r"https://textile-monitor.internal\api",
            ):
                with self.subTest(invalid_origin=invalid_origin):
                    with self.assertRaises(BuildValidationError):
                        prepare_tls_build_assets(
                            root / "generated",
                            default_server_url=invalid_origin,
                            tls_ca_bundle=ca_path,
                        )

    def test_https_build_assets_require_a_ca_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(BuildValidationError, "required"):
                prepare_tls_build_assets(
                    Path(tmpdir) / "generated",
                    default_server_url="https://textile-monitor.internal",
                    tls_ca_bundle=None,
                )

    def test_tls_build_assets_require_one_rsa_3072_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ca_path = root / "root.pem"
            ca_path.write_bytes(
                build_test_root_ca("Root One")
                + build_test_root_ca("Root Two")
            )

            with self.assertRaisesRegex(
                BuildValidationError,
                "exactly one root certificate",
            ):
                prepare_tls_build_assets(
                    root / "generated",
                    default_server_url="https://textile-monitor.internal",
                    tls_ca_bundle=ca_path,
                )

    def test_source_fingerprint_changes_with_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            create_minimal_project(root)
            first = calculate_source_fingerprint(root)

            (root / "main.py").write_text("print('changed')\n", encoding="utf-8")

            self.assertNotEqual(first, calculate_source_fingerprint(root))

    def test_release_manifest_validates_clean_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            manifest_path = write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )

            manifest = validate_release_build(root, app_dir)

            self.assertEqual(manifest_path.name, BUILD_MANIFEST_NAME)
            self.assertEqual(manifest["app_version"], "9.8.7")
            self.assertEqual(manifest["build_mode"], "release")
            self.assertEqual(
                manifest["default_server_url"],
                "http://127.0.0.1",
            )
            self.assertEqual(manifest["transport_security"], "compatible")
            self.assertEqual(manifest["tls_ca_bundle"], "")
            self.assertIsNone(manifest["tls_ca_sha256"])
            self.assertIn("requests_version", manifest)
            self.assertIn("certifi_version", manifest)
            self.assertIn("cryptography_version", manifest)
            self.assertEqual(len(manifest["admin_tools_sha256"]), 5)

    def test_release_manifest_keeps_strict_https_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root, use_https=True)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )

            manifest = validate_release_build(root, app_dir)

            self.assertEqual(
                manifest["default_server_url"],
                "https://textile-monitor.internal",
            )
            self.assertEqual(manifest["transport_security"], "required")
            self.assertEqual(
                manifest["tls_ca_bundle"],
                "certs/inspection-root-ca.pem",
            )
            self.assertRegex(manifest["tls_ca_sha256"], r"^[0-9a-f]{64}$")

    def test_release_manifest_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )
            (root / "main.py").write_text("print('changed')\n", encoding="utf-8")

            with self.assertRaisesRegex(BuildValidationError, "source files changed"):
                validate_release_build(root, app_dir)

    def test_release_manifest_rejects_console_build(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            write_build_manifest(
                root,
                app_dir,
                console=True,
                bootloader_debug=False,
                signed=False,
            )

            with self.assertRaisesRegex(BuildValidationError, "build mode"):
                validate_release_build(root, app_dir)

    def test_release_manifest_rejects_modified_executable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )
            (app_dir / EXECUTABLE_NAME).write_bytes(b"modified executable")

            with self.assertRaisesRegex(BuildValidationError, "hash"):
                validate_release_build(root, app_dir)

    def test_release_manifest_rejects_modified_tls_ca(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root, use_https=True)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )
            (app_dir / "certs" / "inspection-root-ca.pem").write_bytes(
                build_test_root_ca("Changed Inspection Root")
            )

            with self.assertRaisesRegex(BuildValidationError, "TLS CA hash"):
                validate_release_build(root, app_dir)

    def test_release_manifest_rejects_modified_admin_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )
            (
                app_dir / "admin-tools" / "InspectionTls.Common.ps1"
            ).write_text("# changed\n", encoding="utf-8")

            with self.assertRaisesRegex(BuildValidationError, "admin tool"):
                validate_release_build(root, app_dir)

    def test_find_inno_setup_compiler_prefers_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = Path(tmpdir) / "ISCC.exe"
            compiler.write_text("", encoding="utf-8")

            with patch.dict(os.environ, {"ISCC_EXE": str(compiler)}, clear=False):
                self.assertEqual(find_inno_setup_compiler(), str(compiler))

    def test_installer_allows_http_package_without_certs_directory(self):
        installer = (
            CLIENT_ROOT
            / "packaging"
            / "inno-setup"
            / "textile_device_client.iss"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "onlyifdoesntexist skipifsourcedoesntexist",
            installer,
        )

    def test_installer_build_fails_when_compiler_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )

            with patch(
                "build_windows_installer.find_inno_setup_compiler",
                return_value=None,
            ):
                result = build_installer(root=root)

            self.assertEqual(result, 1)

    def test_installer_build_requires_expected_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = create_minimal_project(root)
            compiler = root / "ISCC.exe"
            compiler.write_text("", encoding="utf-8")
            write_build_manifest(
                root,
                app_dir,
                console=False,
                bootloader_debug=False,
                signed=False,
            )

            with patch("build_windows_installer.subprocess.run"):
                result = build_installer(root=root, compiler_path=str(compiler))

            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
