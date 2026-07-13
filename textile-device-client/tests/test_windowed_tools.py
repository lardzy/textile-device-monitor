from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

import main as client_main


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

    def test_config_tool_persists_an_accepted_config(self):
        config = Mock()
        config.get_all.return_value = {"device_code": "test"}
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


if __name__ == "__main__":
    unittest.main()
