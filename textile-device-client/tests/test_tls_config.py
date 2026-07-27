from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.config import Config, ConfigValidationError, validate_config


class TLSConfigTests(unittest.TestCase):
    def test_new_install_defaults_to_required_internal_https(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(str(Path(tmpdir) / "config.json"))

            self.assertEqual(config.get("config_schema_version"), 2)
            self.assertEqual(
                config.get_server_url(),
                "https://textile-monitor.internal",
            )
            self.assertEqual(config.get_transport_security(), "required")
            self.assertEqual(
                config.get_tls_ca_bundle(),
                "certs/inspection-root-ca.pem",
            )

    def test_v1_config_migrates_to_compatible_and_keeps_http_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            legacy = {
                "server_url": "http://192.168.1.100:8000/",
                "device_code": "dev-1",
                "is_first_run": False,
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")

            config = Config(str(path))
            saved = json.loads(path.read_text(encoding="utf-8"))
            backup = json.loads(
                path.with_name("config.json.bak").read_text(encoding="utf-8")
            )

            self.assertEqual(config.get_transport_security(), "compatible")
            self.assertEqual(config.get_server_url(), "http://192.168.1.100:8000")
            self.assertEqual(saved["config_schema_version"], 2)
            self.assertEqual(saved["transport_security"], "compatible")
            self.assertEqual(backup, legacy)

    def test_required_mode_rejects_http_without_mutating_active_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(str(Path(tmpdir) / "config.json"))
            original = config.get_all()

            saved = config.update({"server_url": "http://server.local"})

            self.assertFalse(saved)
            self.assertEqual(config.get_all(), original)
            self.assertIn("HTTPS", config.last_load_error)

    def test_explicit_v1_schema_also_migrates_to_compatible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "config_schema_version": 1,
                        "server_url": "http://old-server.local",
                    }
                ),
                encoding="utf-8",
            )

            config = Config(str(path))

            self.assertEqual(config.get("config_schema_version"), 2)
            self.assertEqual(config.get_transport_security(), "compatible")

    def test_origin_rejects_api_path_credentials_query_and_fragment(self):
        invalid_origins = (
            "https://textile-monitor.internal/api",
            "https://user:pass@textile-monitor.internal",
            "https://textile-monitor.internal?test=1",
            "https://textile-monitor.internal?",
            "https://textile-monitor.internal/#fragment",
            "https://textile-monitor.internal#",
            r"https://textile-monitor.internal\api",
        )
        for origin in invalid_origins:
            with self.subTest(origin=origin):
                with self.assertRaises(ConfigValidationError):
                    validate_config(
                        {
                            "config_schema_version": 2,
                            "server_url": origin,
                            "transport_security": "required",
                            "tls_ca_bundle": "certs/root.pem",
                        }
                    )

    def test_atomic_save_keeps_previous_config_as_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            config = Config(str(path))
            self.assertTrue(config.update({"device_name": "before"}))
            previous = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(config.update({"device_name": "after"}))

            self.assertEqual(config.get_device_name(), "after")
            self.assertEqual(
                json.loads(
                    path.with_name("config.json.bak").read_text(encoding="utf-8")
                ),
                previous,
            )
            self.assertFalse(list(path.parent.glob(".config.json.*.tmp")))

    def test_invalid_hot_reload_candidate_keeps_active_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            config = Config(str(path))
            self.assertTrue(config.update({"device_name": "active"}))
            active = config.get_all()
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(ConfigValidationError):
                config.load_candidate()

            self.assertEqual(config.get_all(), active)


if __name__ == "__main__":
    unittest.main()
