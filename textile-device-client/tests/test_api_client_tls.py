from __future__ import annotations

from pathlib import Path
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, patch

if importlib.util.find_spec("requests") is None:
    requests_stub = types.ModuleType("requests")
    requests_stub.__spec__ = importlib.util.spec_from_loader("requests", loader=None)

    class _StubSession:
        def __init__(self):
            self.headers = {}
            self.trust_env = True
            self.verify = True

    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(
        SSLError=type("SSLError", (Exception,), {}),
        Timeout=type("Timeout", (Exception,), {}),
        ConnectionError=type("ConnectionError", (Exception,), {}),
        JSONDecodeError=ValueError,
    )
    sys.modules["requests"] = requests_stub

import requests


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from modules.api_client import ApiClient
from modules.transport_security import TransportSecurityError


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._payload


class ApiClientTLSTests(unittest.TestCase):
    def test_required_transport_rejects_http(self):
        with self.assertRaises(TransportSecurityError):
            ApiClient(
                "http://textile-monitor.internal",
                _Logger(),
                transport_security="required",
                tls_ca_bundle="root.pem",
            )

    def test_https_uses_pinned_ca_and_disables_environment_proxies(self):
        session = Mock()
        session.headers = {}
        ca_path = Path("/trusted/inspection-root-ca.pem")
        with (
            patch("modules.api_client.requests.Session", return_value=session),
            patch("modules.api_client.validate_ca_bundle", return_value=ca_path),
        ):
            client = ApiClient(
                "https://textile-monitor.internal/",
                _Logger(),
                transport_security="required",
                tls_ca_bundle=str(ca_path),
            )

        self.assertEqual(client.base_url, "https://textile-monitor.internal")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.verify, str(ca_path))

    def test_health_uses_shared_session_ready_endpoint_and_no_redirects(self):
        logger = _Logger()
        client = ApiClient("http://server.local", logger)
        client.session.request = Mock(return_value=_Response())

        self.assertTrue(client.health_check())

        client.session.request.assert_called_once_with(
            "GET",
            "http://server.local/health/ready",
            json=None,
            timeout=client.timeout,
            allow_redirects=False,
        )

    def test_redirect_is_refused_instead_of_downgrading(self):
        logger = _Logger()
        client = ApiClient("http://server.local", logger)
        client.session.request = Mock(return_value=_Response(status_code=302))

        self.assertFalse(client.health_check())
        self.assertEqual(
            client.last_request_info["error"],
            "redirect_refused",
        )

    def test_ssl_error_is_diagnosed_and_not_retried(self):
        logger = _Logger()
        client = ApiClient("http://server.local", logger)
        client.session.request = Mock(
            side_effect=requests.exceptions.SSLError(
                "certificate verify failed: hostname mismatch"
            )
        )

        self.assertFalse(client.health_check())

        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(
            client.last_request_info["error"],
            "tls_hostname_mismatch",
        )
        self.assertIn("主机名", client.last_tls_error_message)

    def test_https_missing_ca_is_rejected_before_network_request(self):
        with self.assertRaisesRegex(TransportSecurityError, "CA"):
            ApiClient(
                "https://textile-monitor.internal",
                _Logger(),
                transport_security="required",
                tls_ca_bundle="",
            )


if __name__ == "__main__":
    unittest.main()
