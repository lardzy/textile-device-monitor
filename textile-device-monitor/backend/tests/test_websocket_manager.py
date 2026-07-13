from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
import unittest

if importlib.util.find_spec("fastapi") is None:
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.WebSocket = object
    encoders_stub = types.ModuleType("fastapi.encoders")
    encoders_stub.jsonable_encoder = lambda value: value
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.encoders"] = encoders_stub

from app.websocket import manager as manager_module
from app.websocket.manager import ConnectionManager


class _FakeWebSocket:
    def __init__(self, *, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.messages = []

    async def send_json(self, payload):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append(payload)


class WebSocketManagerTests(unittest.TestCase):
    def setUp(self):
        self._original_timeout = manager_module.SEND_TIMEOUT_SECONDS
        manager_module.SEND_TIMEOUT_SECONDS = 0.01

    def tearDown(self):
        manager_module.SEND_TIMEOUT_SECONDS = self._original_timeout

    def test_slow_connection_does_not_block_broadcast(self):
        manager = ConnectionManager()
        fast = _FakeWebSocket()
        slow = _FakeWebSocket(delay=0.2)
        manager.active_connections = [fast, slow]

        started_at = time.monotonic()
        asyncio.run(manager.broadcast({"type": "device_status_update"}))
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertEqual(len(fast.messages), 1)
        self.assertIn(fast, manager.active_connections)
        self.assertNotIn(slow, manager.active_connections)

    def test_failed_connection_is_removed_and_others_receive_message(self):
        manager = ConnectionManager()
        fast = _FakeWebSocket()
        failed = _FakeWebSocket(fail=True)
        manager.active_connections = [failed, fast]

        asyncio.run(manager.broadcast({"type": "device_status_update"}))

        self.assertEqual(len(fast.messages), 1)
        self.assertIn(fast, manager.active_connections)
        self.assertNotIn(failed, manager.active_connections)


if __name__ == "__main__":
    unittest.main()
