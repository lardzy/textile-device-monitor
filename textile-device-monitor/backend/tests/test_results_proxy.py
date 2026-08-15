from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.api.area import _read_area_config
from app.api.results import _get_client_base_url
from app.database import SessionLocal, engine
from app.models import Base, Device, DeviceStatus


class ResultsProxyConnectionTests(unittest.TestCase):
    """代理设备客户端的接口不得在发起 HTTP 调用时仍占用数据库连接。"""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _create_device(self, *, client_base_url=None) -> int:
        device = Device(
            device_code="dev-1",
            name="设备-1",
            model="model",
            location="lab",
            status=DeviceStatus.IDLE,
            client_base_url=client_base_url,
        )
        self.db.add(device)
        self.db.commit()
        self.db.refresh(device)
        device_id = device.id
        self.db.close()
        return device_id

    def test_base_url_returned_without_holding_connection(self):
        device_id = self._create_device(client_base_url="http://192.168.1.10:9100/")

        base_url = _get_client_base_url(device_id)

        self.assertEqual(base_url, "http://192.168.1.10:9100")
        self.assertEqual(engine.pool.checkedout(), 0)

    def test_missing_device_raises_404_without_holding_connection(self):
        self.db.close()

        with self.assertRaises(HTTPException) as context:
            _get_client_base_url(999)

        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(engine.pool.checkedout(), 0)

    def test_missing_base_url_raises_404(self):
        device_id = self._create_device(client_base_url=None)

        with self.assertRaises(HTTPException) as context:
            _get_client_base_url(device_id)

        self.assertEqual(context.exception.status_code, 404)

    def test_blank_base_url_raises_404(self):
        device_id = self._create_device(client_base_url="   ")

        with self.assertRaises(HTTPException) as context:
            _get_client_base_url(device_id)

        self.assertEqual(context.exception.status_code, 404)

    def test_read_area_config_releases_transaction(self):
        config = _read_area_config(self.db)

        self.assertIn("root_path", config)
        self.assertFalse(self.db.in_transaction())


if __name__ == "__main__":
    unittest.main()
