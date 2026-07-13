from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.config import Config


class ConfigReloadTests(unittest.TestCase):
    def test_load_updates_the_active_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps({"server_url": "http://before.example"}),
                encoding="utf-8",
            )
            config = Config(str(config_path))

            config_path.write_text(
                json.dumps({"server_url": "http://after.example"}),
                encoding="utf-8",
            )
            loaded = config.load()

            self.assertEqual(loaded["server_url"], "http://after.example")
            self.assertEqual(config.get_server_url(), "http://after.example")


if __name__ == "__main__":
    unittest.main()
