"""Exercise browser Test turns through the monitor HTTP API without real devices."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import Mock

from app.monitor import start_monitor
from app.tasks import TaskStore
from app.test_pipeline import TestPipeline


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "reply.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 800)
        self.store = TaskStore()
        self.transcribe = Mock(return_value="Hello")
        self.synthesize = Mock(return_value=self.audio)
        self.player = Mock()
        self.player.play.return_value = {"played": True}
        self.tests = TestPipeline(
            self.store, transcribe=self.transcribe,
            respond=lambda text: "I have heard: " + text,
            synthesize=self.synthesize, urls=lambda path: {
                "audio_path": str(path), "audio_url": "http://local/tts/reply.wav"},
            player=self.player, data_dir=self.root,
        )
        self.server = start_monitor(self.store, host="127.0.0.1", port=0,
                                    log_path=self.root / "events", test_pipeline=self.tests, max_body=1000)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, method=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def create(self):
        status, body = self.request("/api/tests", b"")
        self.assertEqual(status, 201)
        return json.loads(body)["task_id"]

    def wait_for(self, task_id, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = self.store.get(task_id)
            if task["status"] == status:
                return task
            time.sleep(.01)
        self.fail(f"Pipeline did not reach {status}: {task}")

    def ready(self, text="Hello"):
        self.transcribe.return_value = text
        task_id = self.create()
        self.assertEqual(self.request(f"/api/tests/{task_id}/recording", b"recorded audio")[0], 202)
        self.wait_for(task_id, "waiting")
        return task_id

    def test_record_generate_choose_output(self):
        task_id = self.ready()
        task = self.store.get(task_id)
        self.assertEqual([s["name"] for s in task["stages"]],
                         ["received", "transcribe", "process", "tts", "output"])
        self.assertEqual(task["stages"][2]["detail"]["response"], "I have heard: Hello")
        self.player.play.assert_not_called()
        self.assertEqual(self.request(f"/api/tests/{task_id}/audio"), (200, self.audio.read_bytes()))
        self.assertEqual(self.request(f"/api/tests/{task_id}/browser", b"")[0], 202)
        self.assertEqual(self.store.get(task_id)["status"], "done")
        self.assertEqual(self.request(f"/api/tests/{task_id}/cast", b"")[0], 202)
        self.wait_for(task_id, "done")
        self.player.play.assert_called_once_with("http://local/tts/reply.wav", task_id)

    def test_russian_input_selects_russian_tts(self):
        self.ready("Привет")
        self.assertEqual(self.synthesize.call_args.args[1], "ru")

    def test_cast_failure_keeps_browser_playback_available(self):
        task_id = self.ready()
        self.player.play.side_effect = RuntimeError("Nest unavailable")
        self.request(f"/api/tests/{task_id}/cast", b"")
        task = self.wait_for(task_id, "waiting")
        self.assertIn("Nest unavailable", task["stages"][-1]["detail"]["last_error"])
        self.assertIsNone(task["error"])
        self.assertEqual(self.request(f"/api/tests/{task_id}/audio")[0], 200)
        self.assertEqual(self.request(f"/api/tests/{task_id}/browser", b"")[0], 202)

    def test_empty_speech_fails_without_tts(self):
        self.transcribe.return_value = ""
        task_id = self.create()
        self.request(f"/api/tests/{task_id}/recording", b"silence")
        task = self.wait_for(task_id, "error")
        self.assertIn("No speech detected", task["error"]["message"])
        self.synthesize.assert_not_called()

    def test_interrupt_and_duplicate_upload(self):
        gate = threading.Event()
        entered = threading.Event()
        def transcribe(*args):
            entered.set()
            gate.wait(2)
            return "Hello"
        self.transcribe.side_effect = transcribe
        task_id = self.create()
        self.request(f"/api/tests/{task_id}/recording", b"audio")
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.request(f"/api/tests/{task_id}/recording", b"audio")[0], 400)
        self.request(f"/api/tasks/{task_id}/interrupt", b"")
        gate.set()
        self.wait_for(task_id, "interrupted")
        self.assertEqual(self.request(f"/api/tests/{task_id}/cast", b"")[0], 400)
        self.synthesize.assert_not_called()

    def test_interrupt_while_waiting_for_recording(self):
        task_id = self.create()
        self.request(f"/api/tasks/{task_id}/interrupt", b"")
        task = self.store.get(task_id)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["stages"][0]["status"], "interrupted")
        self.assertEqual(self.request(f"/api/tests/{task_id}/recording", b"audio")[0], 400)

    def test_input_bounds_and_missing_audio(self):
        task_id = self.create()
        self.assertEqual(self.request(f"/api/tests/{task_id}/recording", b"")[0], 400)
        self.assertEqual(self.request(f"/api/tests/{task_id}/recording", b"x"*1001)[0], 413)
        self.assertEqual(self.request(f"/api/tests/{task_id}/audio")[0], 404)
        nest = self.store.create(source="nest")
        self.assertEqual(self.request(f"/api/tests/{nest['id']}/recording", b"audio")[0], 400)
        self.assertEqual(self.request("/api/tests/missing/audio")[0], 404)

    def test_page_and_local_icons(self):
        self.assertEqual(self.request("/")[0], 200)
        status, sprite = self.request("/icons.svg")
        self.assertEqual(status, 200)
        self.assertIn(b'<symbol id="mic"', sprite)


if __name__ == "__main__":
    unittest.main()
