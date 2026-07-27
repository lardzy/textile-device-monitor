from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import main as client_main
from modules.config import ConfigValidationError


class WindowedToolTests(unittest.TestCase):
    def test_frozen_self_command_reuses_the_packaged_executable(self):
        with patch.object(client_main.sys, "frozen", True, create=True):
            command = client_main._self_command("--config-tool")

        self.assertEqual(command, [sys.executable, "--config-tool"])

    def test_source_self_command_runs_main_script(self):
        with patch.object(client_main.sys, "frozen", False, create=True):
            command = client_main._self_command("--log-viewer")

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]), CLIENT_ROOT / "main.py")
        self.assertEqual(command[2], "--log-viewer")

    def test_frozen_runtime_uses_executable_directory(self):
        with (
            patch.object(client_main.sys, "frozen", True, create=True),
            patch.object(
                client_main.sys,
                "executable",
                "client.exe",
            ),
            patch.object(
                client_main.os.path,
                "abspath",
                return_value="/installed/client.exe",
            ),
            patch.object(
                client_main.os.path,
                "dirname",
                return_value="/installed",
            ),
            patch.object(client_main.os, "chdir") as chdir,
        ):
            client_main._set_runtime_working_directory()

        chdir.assert_called_once_with("/installed")

    def test_config_tool_persists_an_accepted_config(self):
        config = Mock()
        config.get_all.return_value = {"device_code": "test"}
        config.is_first_run.return_value = False
        config.update.return_value = True
        new_config = {"device_code": "updated"}

        with (
            patch.object(client_main, "Config", return_value=config),
            patch(
                "modules.config_window.ConfigWindow.show_config_dialog",
                return_value=new_config,
            ),
        ):
            result = client_main._run_config_tool()

        self.assertEqual(result, 0)
        config.update.assert_called_once_with(new_config)

    def test_windowed_error_uses_native_dialog_without_input(self):
        client = object.__new__(client_main.TextileDeviceClient)
        client.logger = Mock()

        with (
            patch.object(client_main, "_is_windowed_runtime", return_value=True),
            patch.object(client_main, "_show_native_error") as show_error,
            self.assertRaises(SystemExit) as raised,
        ):
            client._show_error_and_exit("registration failed")

        self.assertEqual(raised.exception.code, 1)
        show_error.assert_called_once_with("registration failed")

    def test_invalid_hot_reload_keeps_running_components(self):
        client = object.__new__(client_main.TextileDeviceClient)
        client.config = Mock()
        client.config.get_all.return_value = {
            "server_url": "http://old.local",
        }
        client.config.load_candidate.side_effect = ConfigValidationError("broken")
        client.logger = Mock()
        client.tray_icon = Mock()
        client.status_reporter = Mock()
        client.results_server = Mock()

        client._reload_config()

        client.status_reporter.stop.assert_not_called()
        client.results_server.stop.assert_not_called()
        client.config.apply_candidate.assert_not_called()
        client.tray_icon.show_notification.assert_called_once()

    def test_failed_tls_probe_keeps_running_components(self):
        client = object.__new__(client_main.TextileDeviceClient)
        old = {
            "device_code": "dev-1",
            "server_url": "http://old.local",
            "transport_security": "compatible",
            "tls_ca_bundle": "certs/old.pem",
            "working_path": "C:\\data",
            "log_path": "",
            "is_laser_confocal": False,
            "results_port": 9100,
            "report_interval": 5,
            "manual_status": None,
        }
        candidate = {
            **old,
            "server_url": "https://textile-monitor.internal",
            "transport_security": "required",
            "tls_ca_bundle": "certs/new.pem",
        }
        client.config = Mock()
        client.config.get_all.return_value = old
        client.config.load_candidate.return_value = candidate
        probe = Mock()
        probe.health_check.return_value = False
        probe.last_tls_error_message = "无法信任服务器证书"
        probe.last_request_info = {}
        client._create_api_client = Mock(return_value=probe)
        client.logger = Mock()
        client.tray_icon = Mock()
        client.status_reporter = Mock()
        client.results_server = Mock()

        client._reload_config()

        client.status_reporter.stop.assert_not_called()
        client.results_server.stop.assert_not_called()
        client.config.apply_candidate.assert_not_called()
        client.tray_icon.show_notification.assert_called_once()

    def test_transport_only_hot_reload_rebuilds_api_client(self):
        old = {
            "device_code": "dev-1",
            "server_url": "https://textile-monitor.internal",
            "transport_security": "compatible",
            "tls_ca_bundle": "certs/old.pem",
            "working_path": "C:\\data",
            "log_path": "",
            "is_laser_confocal": False,
            "results_port": 9100,
            "report_interval": 5,
            "manual_status": None,
        }
        candidate = {
            **old,
            "transport_security": "required",
            "tls_ca_bundle": "certs/new.pem",
        }
        client = object.__new__(client_main.TextileDeviceClient)
        client.config = Mock()
        client.config.get_all.return_value = old
        client.config.load_candidate.return_value = candidate
        client.config.get_manual_status.return_value = None
        client.logger = Mock()
        client.tray_icon = Mock()
        client.status_reporter = Mock()
        client.results_server = None
        probe = Mock()
        probe.health_check.return_value = True
        client._create_api_client = Mock(return_value=probe)
        client.initialize = Mock()
        client._register_device = Mock()

        client._reload_config()

        probe.health_check.assert_called_once_with()
        client.config.apply_candidate.assert_called_once_with(candidate)
        client.status_reporter.stop.assert_called_once_with()
        client.initialize.assert_called_once_with()
        client._register_device.assert_called_once_with()

    def test_transport_only_failed_probe_keeps_old_runtime(self):
        old = {
            "device_code": "dev-1",
            "server_url": "https://textile-monitor.internal",
            "transport_security": "compatible",
            "tls_ca_bundle": "certs/old.pem",
            "working_path": "C:\\data",
            "log_path": "",
            "is_laser_confocal": False,
            "results_port": 9100,
            "report_interval": 5,
            "manual_status": None,
        }
        candidate = {
            **old,
            "tls_ca_bundle": "certs/new.pem",
        }
        client = object.__new__(client_main.TextileDeviceClient)
        client.config = Mock()
        client.config.get_all.return_value = old
        client.config.load_candidate.return_value = candidate
        client.logger = Mock()
        client.tray_icon = Mock()
        client.status_reporter = Mock()
        client.results_server = Mock()
        probe = Mock()
        probe.health_check.return_value = False
        probe.last_tls_error_message = "新的 CA 文件不可用"
        probe.last_request_info = {}
        client._create_api_client = Mock(return_value=probe)

        client._reload_config()

        probe.health_check.assert_called_once_with()
        client.config.apply_candidate.assert_not_called()
        client.status_reporter.stop.assert_not_called()
        client.results_server.stop.assert_not_called()
        client.tray_icon.show_notification.assert_called_once()

    def test_https_migration_is_two_phase_and_never_restores_http_after_activation(
        self,
    ):
        script = (
            CLIENT_ROOT / "scripts" / "migrate_to_internal_https.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('[ValidateSet("Prepare", "Activate")]', script)
        self.assertIn('[string]$TransportSecurity = "compatible"', script)
        self.assertIn("-StageOnly", script)
        self.assertIn("AllowedAdditionalRootSha256", script)
        self.assertIn("ExpectedServerRootSha256", script)
        self.assertIn(
            'ClientCaBundleConfigValue = "certs/inspection-root-ca.pem"',
            script,
        )
        self.assertIn("Fail-InspectionTlsExternalVerification", script)
        self.assertIn("Write-Warning", script)
        self.assertIn("拒绝降级为 compatible", script)
        self.assertNotIn("& $restoreTrustScript", script)

    def test_https_reporting_probe_bypasses_proxy_and_redirects(self):
        script = (
            CLIENT_ROOT / "scripts" / "verify_client_https_reporting.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("$handler.UseProxy = $false", script)
        self.assertIn("$handler.AllowAutoRedirect = $false", script)
        self.assertIn("$baselineHeartbeat", script)


if __name__ == "__main__":
    unittest.main()
