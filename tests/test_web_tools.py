"""Verify Keenable's wire protocol against a local fixture, never a live provider."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import web_tools


class FakeKeenable(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        self.server.requests.append({
            "method": self.command, "path": self.path,
            "key": self.headers.get("X-API-Key"),
            "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
        })
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):
        pass


class WebToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeKeenable)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        cls.thread.join()

    def setUp(self):
        self.api.requests = []
        self.api.status = 200
        self.api.body = b'{}'
        for p in (patch.dict(os.environ, {"KEENABLE_API_KEY": "test-key"}),
                  patch.object(web_tools, "API_URL", f"http://127.0.0.1:{self.api.server_port}/v1")):
            p.start()
            self.addCleanup(p.stop)

    def test_search_posts_query_and_returns_sources(self):
        self.api.body = json.dumps({"results": [
            {"title": "Source", "url": "https://example.com", "description": "Evidence", "acquired_at": 123},
            None,
        ]}).encode()
        result = json.loads(web_tools.execute("web_search", '{"query":" Привет "}'))
        self.assertEqual(result, [{"title": "Source", "url": "https://example.com", "description": "Evidence"}])
        request = self.api.requests[0]
        self.assertEqual((request["method"], request["path"], request["key"]),
                         ("POST", "/v1/search", "test-key"))
        self.assertEqual(json.loads(request["body"]), {"query": "Привет"})

    def test_page_read_uses_keenable_fetch_with_encoded_url(self):
        self.api.body = json.dumps({"content": "Verified page content"}).encode()
        url = "https://example.com/path?a=1&b=two words"
        self.assertEqual(web_tools.execute("read_web_page", json.dumps({"url": url})),
                         "Verified page content")
        request = self.api.requests[0]
        parsed = urlsplit(request["path"])
        self.assertEqual((request["method"], parsed.path, request["key"]), ("GET", "/v1/fetch", "test-key"))
        self.assertEqual(parse_qs(parsed.query), {"url": [url]})

    def test_invalid_arguments_and_unknown_tools_do_not_call_provider(self):
        for name, args in (("web_search", "{"), ("web_search", "null"),
                           ("web_search", '{"query":""}'), ("web_search", '{"query":2}'),
                           ("web_search", '{"query":"hello","other":true}'),
                           ("read_web_page", '{"url":"file:///tmp/private"}'),
                           ("unknown", '{}')):
            with self.subTest(name=name, args=args):
                self.assertIn("error", json.loads(web_tools.execute(name, args)))
        self.assertEqual(self.api.requests, [])

    def test_provider_failures_return_errors_without_private_body(self):
        self.api.status = 401
        self.api.body = b'private provider detail test-key'
        output = web_tools.execute("web_search", '{"query":"hello"}')
        self.assertIn("HTTP 401", output)
        self.assertNotIn("private", output)
        self.assertNotIn("test-key", output)
        self.api.status = 200
        for body in (b'not json', b'null', b'{}'):
            self.api.body = body
            self.assertIn("error", json.loads(web_tools.execute("web_search", '{"query":"hello"}')))
        with patch.object(web_tools, "urlopen", side_effect=TimeoutError):
            self.assertIn("timed out", web_tools.execute("web_search", '{"query":"hello"}'))

    def test_tool_output_is_bounded_with_valid_unicode(self):
        self.api.body = json.dumps({"content": "Я" * 20000}).encode()
        output = web_tools.execute("read_web_page", '{"url":"https://example.com"}')
        self.assertTrue(output.endswith("[Tool result truncated.]"))
        self.assertLess(len(output.encode()), 16100)
        self.assertNotIn("\ufffd", output)

    def test_key_is_optional_and_missing_key_prevents_network_request(self):
        with patch.dict(os.environ, {"KEENABLE_API_KEY": ""}):
            self.assertFalse(web_tools.available())
            self.assertIn("not configured", web_tools.execute("web_search", '{"query":"hello"}'))
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
