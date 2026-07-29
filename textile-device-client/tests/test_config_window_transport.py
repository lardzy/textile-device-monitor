from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from PyQt6.QtWidgets import QApplication

from modules.config_window import ConfigWindow


class ConfigWindowTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_http_is_preferred_and_required_https_remains_selectable(self):
        window = ConfigWindow(
            {
                "server_url": "http://127.0.0.1",
                "transport_security": "compatible",
                "tls_ca_bundle": "",
            },
            [],
        )
        self.addCleanup(window.close)

        modes = [
            window.transport_security_combo.itemData(index)
            for index in range(window.transport_security_combo.count())
        ]
        self.assertEqual(modes, ["compatible", "required"])
        self.assertEqual(
            window.transport_security_combo.currentData(),
            "compatible",
        )
        self.assertFalse(window.tls_ca_bundle_edit.isEnabled())

        window.server_url_edit.setText("https://textile-monitor.internal")

        self.assertTrue(window.tls_ca_bundle_edit.isEnabled())


if __name__ == "__main__":
    unittest.main()
