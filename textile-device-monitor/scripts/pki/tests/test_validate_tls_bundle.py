from __future__ import annotations

import importlib.util
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
PROJECT_DIRECTORY = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = SCRIPT_DIRECTORY / "validate_tls_bundle.py"
SPEC = importlib.util.spec_from_file_location("validate_tls_bundle", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, capture_output=True)


class TlsBundleValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openssl = shutil.which("openssl")
        if cls.openssl is None:
            raise unittest.SkipTest("OpenSSL is not installed")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="textile-tls-validator-tests-"
        )
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self._build_valid_bundle()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_valid_bundle(self) -> None:
        root_key = self.root / "root.key"
        root_cert = self.root / "root.pem"
        intermediate_key = self.root / "intermediate.key"
        intermediate_csr = self.root / "intermediate.csr"
        intermediate_cert = self.root / "intermediate.pem"
        leaf_key = self.bundle / "privkey.pem"
        leaf_csr = self.root / "leaf.csr"
        leaf_cert = self.root / "leaf.pem"
        intermediate_ext = self.root / "intermediate.ext"
        leaf_ext = self.root / "leaf.ext"

        intermediate_ext.write_text(
            """
[intermediate]
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
""".strip()
            + "\n",
            encoding="ascii",
        )
        leaf_ext.write_text(
            """
[server]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:textile-monitor.internal
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
""".strip()
            + "\n",
            encoding="ascii",
        )
        run(
            self.openssl,
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-x509",
            "-sha256",
            "-days",
            "3650",
            "-subj",
            "/CN=Test Root",
            "-addext",
            "basicConstraints=critical,CA:true,pathlen:1",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(root_key),
            "-out",
            str(root_cert),
        )
        run(
            self.openssl,
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=Test Intermediate",
            "-keyout",
            str(intermediate_key),
            "-out",
            str(intermediate_csr),
        )
        run(
            self.openssl,
            "x509",
            "-req",
            "-sha256",
            "-days",
            "1825",
            "-in",
            str(intermediate_csr),
            "-CA",
            str(root_cert),
            "-CAkey",
            str(root_key),
            "-CAcreateserial",
            "-extfile",
            str(intermediate_ext),
            "-extensions",
            "intermediate",
            "-out",
            str(intermediate_cert),
        )
        run(
            self.openssl,
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=textile-monitor.internal",
            "-keyout",
            str(leaf_key),
            "-out",
            str(leaf_csr),
        )
        run(
            self.openssl,
            "x509",
            "-req",
            "-sha256",
            "-days",
            "365",
            "-in",
            str(leaf_csr),
            "-CA",
            str(intermediate_cert),
            "-CAkey",
            str(intermediate_key),
            "-CAcreateserial",
            "-extfile",
            str(leaf_ext),
            "-extensions",
            "server",
            "-out",
            str(leaf_cert),
        )
        (self.bundle / "fullchain.pem").write_bytes(
            leaf_cert.read_bytes() + intermediate_cert.read_bytes()
        )
        shutil.copy(root_cert, self.bundle / "root-ca.pem")
        run(
            self.openssl,
            "x509",
            "-in",
            str(root_cert),
            "-outform",
            "DER",
            "-out",
            str(self.bundle / "root-ca.cer"),
        )
        os.chmod(leaf_key, 0o600)

    def validate(self, *, minimum_days: int = 30) -> dict[str, str | int]:
        return VALIDATOR.validate_bundle(
            tls_dir=self.bundle,
            hostname="textile-monitor.internal",
            minimum_valid_days=minimum_days,
            openssl=self.openssl,
        )

    def test_valid_bundle_passes(self) -> None:
        result = self.validate()
        self.assertEqual(result["hostname"], "textile-monitor.internal")
        self.assertEqual(len(str(result["root_sha256"])), 64)

    def test_bundle_matches_application_deployment_validator(self) -> None:
        sys.path.insert(0, str(PROJECT_DIRECTORY / "backend"))
        try:
            from app import deployment_validation
        except ImportError as error:
            self.skipTest(f"application deployment dependencies unavailable: {error}")
        finally:
            sys.path.pop(0)

        deployment_validation.validate_tls_material(
            self.bundle,
            expected_hostname="textile-monitor.internal",
            minimum_valid_days=30,
        )

    def test_wrong_private_key_is_rejected(self) -> None:
        run(
            self.openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:3072",
            "-out",
            str(self.bundle / "privkey.pem"),
        )
        os.chmod(self.bundle / "privkey.pem", 0o600)
        with self.assertRaisesRegex(VALIDATOR.ValidationError, "不匹配"):
            self.validate()

    def test_short_remaining_validity_is_rejected(self) -> None:
        with self.assertRaisesRegex(VALIDATOR.ValidationError, "剩余有效期"):
            self.validate(minimum_days=400)

    def test_fullchain_with_root_is_rejected(self) -> None:
        with (self.bundle / "fullchain.pem").open("ab") as stream:
            stream.write((self.bundle / "root-ca.pem").read_bytes())
        with self.assertRaisesRegex(VALIDATOR.ValidationError, "严格包含"):
            self.validate()

    def test_openssl_english_month_is_locale_independent(self) -> None:
        previous = locale.setlocale(locale.LC_TIME)
        try:
            for candidate in ("zh_CN.UTF-8", "zh_CN", "Chinese_China.936"):
                try:
                    locale.setlocale(locale.LC_TIME, candidate)
                    break
                except locale.Error:
                    continue
            parsed = VALIDATOR.parse_openssl_time(
                "Jul 26 08:00:00 2026 GMT"
            )
        finally:
            locale.setlocale(locale.LC_TIME, previous)
        self.assertEqual(parsed.month, 7)
        self.assertEqual(parsed.tzinfo, VALIDATOR.dt.timezone.utc)


class OperationalScriptStaticTests(unittest.TestCase):
    def test_issuance_contract_is_locked(self) -> None:
        source = (SCRIPT_DIRECTORY / "Issue-ServerCertificate.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('if ($ValidDays -ne 365)', source)
        self.assertIn("rsa_keygen_bits:3072", source)
        self.assertIn("DNS.1 = $DnsName", source)
        self.assertIn("extendedKeyUsage = serverAuth", source)
        self.assertNotIn("extendedKeyUsage = critical", source)

    def test_no_private_key_material_is_committed(self) -> None:
        scripts_root = SCRIPT_DIRECTORY.parent
        for path in scripts_root.rglob("*"):
            if (
                path.is_file()
                and path != Path(__file__).resolve()
                and "__pycache__" not in path.parts
                and path.suffix in {".ps1", ".py"}
            ):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("-----BEGIN PRIVATE KEY-----", source, path)
                self.assertNotIn(
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----", source, path
                )

    def test_windows_prepare_activate_contract_is_explicit(self) -> None:
        windows_directory = SCRIPT_DIRECTORY.parent / "windows"
        install = (windows_directory / "Install-InspectionTlsTrust.ps1").read_text(
            encoding="utf-8"
        )
        common = (windows_directory / "InspectionTls.Common.ps1").read_text(
            encoding="utf-8"
        )
        restore = (
            windows_directory / "Restore-InspectionTlsTrust.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[switch]$StageOnly", install)
        self.assertIn('status = "trust_staged_pending_server"', install)
        self.assertIn(
            "function Fail-InspectionTlsExternalVerification", common
        )
        self.assertIn('"activation_failed"', common)
        self.assertIn("secure_https_state_retained", restore)
        self.assertIn("禁止自动降级 HTTP", restore)

    def test_server_deployment_uses_full_preflight_and_direct_probe(self) -> None:
        deploy = (SCRIPT_DIRECTORY / "Deploy-ServerCertificate.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"app.deployment_validation"', deploy)
        self.assertIn("--noproxy", deploy)
        self.assertIn("$candidateServerFingerprint", deploy)
        self.assertIn("$deployedServerFingerprint", deploy)
        self.assertIn("[string]$ExpectedRootSha256", deploy)
        self.assertIn("$oldBundleValid", deploy)
        self.assertIn("$initialDeployment", deploy)
        self.assertIn(
            "$initialDeployment -and $frontendWasRunningBefore", deploy
        )
        self.assertIn("$liveServerFingerprintBefore", deploy)
        self.assertIn("$activeServerFingerprint", deploy)
        self.assertIn('[string]$ExternalLivePath = "/health/live"', deploy)
        self.assertIn(
            '[string]$InternalReadinessUrl = '
            '"http://127.0.0.1:8080/backend-ready"',
            deploy,
        )
        self.assertIn('"wget",', deploy)
        self.assertIn('"-qO-",', deploy)
        self.assertIn("Invoke-ContainerReadinessProbe", deploy)
        self.assertIn(
            'internal_backend_readiness_probe = "passed"', deploy
        )
        self.assertNotIn('[string]$HealthPath = "/health/ready"', deploy)
        success_manifest = deploy.index("$manifest = [ordered]@{")
        self.assertLess(
            deploy.rindex("Invoke-TrustedHttpsProbe"),
            success_manifest,
        )
        self.assertLess(
            deploy.rindex("Invoke-ContainerReadinessProbe"),
            success_manifest,
        )
        self.assertLess(
            deploy.rindex("Invoke-ApplicationDeploymentPreflight -Probe"),
            success_manifest,
        )
        self.assertIn("Protect-PkiPrivateDirectory -Path $backupDirectory", deploy)
        self.assertNotIn("$activeExisted", deploy)

    def test_windows_firewall_tool_is_fail_closed_and_owns_only_its_rule(
        self,
    ) -> None:
        windows_directory = SCRIPT_DIRECTORY.parent / "windows"
        firewall = (
            windows_directory / "Manage-InspectionHttpsFirewall.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"Audit", "Apply", "Validate", "Restore"', firewall)
        self.assertIn("$ConfirmDockerSourceIpPreserved", firewall)
        self.assertIn("$ObservedRemoteAddress", firewall)
        self.assertIn("$ExpectedClientAddress", firewall)
        self.assertIn("-Protocol TCP", firewall)
        self.assertIn("-LocalPort 443", firewall)
        self.assertIn('"$item/32"', firewall)
        self.assertIn("changed_existing_rules = @()", firewall)
        self.assertNotIn("Disable-NetFirewallRule", firewall)
        self.assertNotIn("Set-NetFirewallRule", firewall)
        self.assertIn(
            "Remove-NetFirewallRule `\n                -Name $ruleName",
            firewall,
        )

    def test_browser_proxy_check_is_read_only(self) -> None:
        windows_directory = SCRIPT_DIRECTORY.parent / "windows"
        proxy = (
            windows_directory / "Test-InspectionBrowserProxy.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("manual_browser_confirmation_required", proxy)
        self.assertIn("netsh.exe winhttp show proxy", proxy)
        self.assertNotIn("Set-ItemProperty", proxy)
        self.assertNotIn("netsh.exe winhttp set", proxy)
        self.assertNotIn("netsh.exe winhttp reset", proxy)


if __name__ == "__main__":
    unittest.main()
