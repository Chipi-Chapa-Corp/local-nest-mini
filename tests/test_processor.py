"""Exercise the LLM wire protocol and reply-to-playback path without providers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from app import processor
from app.tasks import TaskCancelled, TaskStore


class FakeAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
        })
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):
        pass


class ProcessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
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
        self.reply = "Привет! Как дела?"
        self.api.body = json.dumps({"choices": [{"message": {"content": self.reply}}]}).encode()
        env = patch.dict(os.environ, {
            "LLM_BASE_URL": f"http://127.0.0.1:{self.api.server_port}/v1/",
            "LLM_MODEL": "local-model",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_prompt_unicode_markup_and_optional_fields(self):
        self.assertEqual(processor.respond("  Привет  "), self.reply)
        request = self.api.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertIsNone(request["authorization"])
        self.assertEqual(request["body"], {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": processor.SYSTEM_PROMPT},
                {"role": "user", "content": "Привет"},
            ],
        })

    def test_authentication_and_reasoning_when_configured(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key", "LLM_REASONING_EFFORT": "low"}):
            processor.respond("Hello")
        self.assertEqual(self.api.requests[0]["authorization"], "Bearer test-key")
        self.assertEqual(self.api.requests[0]["body"]["reasoning_effort"], "low")

    def test_prompt_asks_for_plain_speech_without_sound_tags(self):
        for backend in ("omnivoice",):
            with self.subTest(backend=backend), patch.dict(os.environ, {"TTS_BACKEND": backend}):
                processor.respond("Hello")
                prompt = self.api.requests[-1]["body"]["messages"][0]["content"]
                self.assertEqual(prompt, processor.SYSTEM_PROMPT)
                self.assertNotRegex(prompt, r"\[[A-Za-z-]+\]")  # no sound tags offered
                for legacy in ("<sound>", "(((", "# Voice", "# Sounds"):
                    self.assertNotIn(legacy, prompt)

    def test_empty_input_and_missing_configuration_do_not_call_api(self):
        with self.assertRaises(ValueError):
            processor.respond("  ")
        for name in ("LLM_BASE_URL", "LLM_MODEL"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                self.assertEqual(processor.respond("Hello"),
                                 "Не получилось сгенерировать ответ. Диктую ошибку: "
                                 "LLM_BASE_URL and LLM_MODEL must be configured")
        self.assertEqual(self.api.requests, [])

    def test_invalid_base_url(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "file:///tmp/llm"}):
            self.assertIn("Диктую ошибку: LLM_BASE_URL must be an HTTP(S) API base URL",
                          processor.respond("Hello"))

    def test_api_failure_does_not_expose_provider_body(self):
        self.api.status = 401
        self.api.body = b"private provider error"
        self.assertEqual(processor.respond("Hello"),
                         "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned HTTP 401")

    def test_invalid_and_empty_responses_become_spoken_errors(self):
        for body in (b"not json", b"{}", b"null", b'{"choices": []}',
                     b'{"choices": [{"message": {"content": null}}]}',
                     b'{"choices": [{"message": {"content": "  "}}]}'):
            with self.subTest(body=body):
                self.api.body = body
                self.assertTrue(processor.respond("Hello").startswith(
                    "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned"))

    def test_timeout_becomes_a_spoken_error(self):
        with patch.object(processor, "urlopen", side_effect=TimeoutError):
            self.assertEqual(processor.respond("Hello"),
                             "Не получилось сгенерировать ответ. Диктую ошибку: "
                             "LLM API request failed or timed out")

    def test_cancellation_is_not_spoken(self):
        with patch.object(processor, "urlopen", side_effect=TaskCancelled("task interrupted")):
            with self.assertRaises(TaskCancelled):
                processor.respond("Hello")

    def test_api_reply_reaches_tts_and_playback(self):
        self.assert_reply_reaches_playback(self.reply)

    def test_api_failure_reaches_tts_and_playback(self):
        self.api.status = 503
        self.api.body = b"private provider error"
        reply = "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned HTTP 503"
        for path in ("/answer", "/turn"):
            with self.subTest(path=path):
                self.assert_reply_reaches_playback(reply, path)

    def assert_reply_reaches_playback(self, reply, path="/answer"):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"DATA_DIR": temp}):
                from app import server

            class QuietHandler(server.Handler):
                def log_message(self, *args):
                    pass

            audio = Path(temp) / "reply.wav"
            audio.write_bytes(b"RIFF")
            player = Mock()
            player.play.return_value = {"played": True}
            store = TaskStore()
            with patch.object(server, "TASKS", store), \
                 patch.object(server, "DATA_DIR", Path(temp)), \
                 patch.object(server, "OUTPUT_MODE", "cast"), \
                 patch.object(server, "transcribe_audio", return_value="Привет"), \
                 patch.object(server, "synthesize", return_value=audio) as synthesize, \
                 patch.object(server, "PLAYER", player):
                http = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                try:
                    req = Request(f"http://127.0.0.1:{http.server_port}{path}?play=1",
                                  data=(b"recorded audio" if path == "/turn" else
                                        json.dumps({"text": "Привет", "play": True}).encode()),
                                  headers={"Content-Type": "application/json"})
                    with urlopen(req, timeout=3) as response:
                        result = json.load(response)
                finally:
                    http.shutdown()
                    http.server_close()
                    thread.join()
            self.assertEqual(result["response"], reply)
            synthesize.assert_called_once_with(reply, "auto", result["task_id"])
            player.play.assert_called_once_with(result["audio_url"], result["task_id"])
            self.assertEqual(store.get(result["task_id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
