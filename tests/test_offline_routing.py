import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mitmproxy.test import tflow

with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DATA_DIR": directory}):
    import nest_relay


class OfflineRoutingTest(unittest.TestCase):
    def setUp(self):
        self.patchers = [patch.object(nest_relay, "ALLOW_GOOGLE_UPSTREAM", False),
                         patch.object(nest_relay, "log"),
                         patch.object(nest_relay, "OUTPUT_MODE", "native"),
                         patch.object(nest_relay, "OFFLINE_BOOTSTRAP_ENABLED", False),
                         patch.object(nest_relay, "ASSISTANT_ENABLED", True)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def hello(self, host):
        server = SimpleNamespace(address=None, sni=None)
        data = SimpleNamespace(client_hello=SimpleNamespace(sni=host),
                               context=SimpleNamespace(server=server), ignore_connection=False)
        nest_relay.NestRelay().tls_clienthello(data)
        return data

    def test_assistant_routes_only_to_local_endpoint(self):
        data = self.hello("assistant-s3-pa.googleapis.com")
        self.assertEqual(data.context.server.address, ("127.0.0.1", 18444))
        self.assertTrue(data.ignore_connection)

    def test_media_does_not_bypass_tls_proxy(self):
        data = self.hello("play.googleapis.com")
        self.assertFalse(data.ignore_connection)
        self.assertEqual(data.context.server.address, ("blocked.invalid", 443))

    def request(self, host, path):
        flow = tflow.tflow()
        flow.client_conn.tls = True
        flow.client_conn.sni = host
        flow.request.host = host
        flow.request.path = path
        nest_relay.NestRelay().requestheaders(flow)
        return flow

    def test_google_requests_get_local_failure(self):
        for host, path in [("play.googleapis.com", "/media"),
                           ("clients3.google.com", "/cast/cloudcast/device/register"),
                           ("clients4.google.com", "/uma/v2")]:
            with self.subTest(host=host):
                flow = self.request(host, path)
                self.assertEqual(flow.response.status_code, 503)
                self.assertIn(b"Google upstream disabled", flow.response.content)

    def test_connectivity_probe_is_answered_locally(self):
        for host in ("connectivitycheck.gstatic.com", "clients1.google.com"):
            flow = self.request(host, "/generate_204")
            self.assertEqual(flow.response.status_code, 204)
            self.assertEqual(flow.response.content, b"")

    def test_bootstrap_is_local_and_uses_nested_credentials(self):
        from app.bootstrap import token_response
        path = "/cast/orchestration/googleoauthtoken"
        self.assertEqual(self.request("clients3.google.com", path).response.status_code, 503)
        body = token_response(['123456789012345678901'])
        with patch.object(nest_relay, "OFFLINE_BOOTSTRAP_ENABLED", True), \
                patch.object(nest_relay, "load_token_response", return_value=body):
            flow = self.request("clients3.google.com", path)
            self.assertEqual(flow.response.status_code, 200)
            self.assertEqual(flow.response.headers["content-type"], "application/x-protobuf")
            self.assertEqual(flow.response.content, body)
            # Enabling local bootstrap never enables a cloud destination.
            self.assertEqual(self.hello("clients3.google.com").context.server.address,
                             ("blocked.invalid", 443))

    def test_missing_local_accounts_fail_closed(self):
        with patch.object(nest_relay, 'OFFLINE_BOOTSTRAP_ENABLED', True), \
                patch.object(nest_relay, 'load_token_response', side_effect=FileNotFoundError):
            flow = self.request('clients3.google.com', '/cast/orchestration/googleoauthtoken')
            self.assertEqual(flow.response.status_code, 503)

    def test_reverse_proxy_preserves_original_sni_for_local_routing(self):
        flow = tflow.tflow()
        flow.client_conn.tls = True
        flow.client_conn.sni = "connectivitycheck.gstatic.com"
        flow.request.host = "blocked.invalid"
        flow.request.path = "/generate_204"
        nest_relay.NestRelay().requestheaders(flow)
        self.assertEqual(flow.response.status_code, 204)
        self.assertEqual(flow.request.host, "connectivitycheck.gstatic.com")


if __name__ == "__main__":
    unittest.main()
