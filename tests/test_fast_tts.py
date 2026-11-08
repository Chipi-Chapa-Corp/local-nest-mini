"""Exercise resident-worker reuse, cancellation and recovery with a private fixture."""

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from app.fast_tts import ASSET_FILES, FastTTS
from app.tasks import TaskCancelled


WORKER = '''
import json, sys, time
from pathlib import Path
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    job = json.loads(line)
    output = Path(job["output"])
    output.write_bytes(b"partial")
    if job["text"] == "die": sys.exit(3)
    if job["text"] == "wait": time.sleep(30)
    output.write_bytes(b"RIFF" + bytes(64))
    print(json.dumps({"ok": True, "engine": job["lang"]}), flush=True)
'''


class ResidentTTSTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ASSET_FILES:
            (self.root / name).touch()
        worker = self.root / "fixture.py"
        worker.write_text(WORKER)
        self.client = FastTTS(sys.executable, self.root, worker=worker)
        self.addCleanup(self.client.close)

    def test_two_languages_share_one_loaded_process(self):
        self.client.warm()
        pid = self.client.proc.pid
        for lang in ("ru", "en", "ru"):
            result = self.client.synthesize("Speech.", lang, self.root / f"{lang}.wav")
            self.assertEqual(result["engine"], lang)
            self.assertEqual(self.client.proc.pid, pid)

    def test_worker_exit_removes_partial_audio_and_next_request_recovers(self):
        output = self.root / "speech.wav"
        with self.assertRaisesRegex(RuntimeError, "worker exited"):
            self.client.synthesize("die", "ru", output)
        self.assertFalse(output.exists())
        self.assertIsNone(self.client.proc)
        self.client.synthesize("Recovered.", "en", output)
        self.assertTrue(output.is_file())

    def test_generation_timeout_terminates_worker_and_removes_partial_audio(self):
        output = self.root / "speech.wav"
        with self.assertRaises(TimeoutError):
            self.client.synthesize("wait", "en", output, timeout=.1)
        self.assertFalse(output.exists())
        self.assertIsNone(self.client.proc)

    def test_cancel_active_generation_then_reuse_client(self):
        output = self.root / "speech.wav"
        cancelled = threading.Event()
        errors = []

        def check():
            if cancelled.is_set():
                raise TaskCancelled("interrupted")

        def run():
            try:
                self.client.synthesize("wait", "en", output, check=check)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(output.exists(), "fixture did not start generation")
        finally:
            cancelled.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], TaskCancelled)
        self.assertFalse(output.exists())
        self.client.synthesize("Recovered.", "ru", output)

    def test_cancelling_a_waiter_keeps_the_loaded_worker(self):
        self.client.warm()
        pid = self.client.proc.pid
        calls = []

        def check():
            calls.append(True)
            raise TaskCancelled("waiting request cancelled")

        with self.client.lock:
            with self.assertRaises(TaskCancelled):
                self.client.synthesize("Waiting.", "en", self.root / "waiting.wav", check=check)
        self.assertTrue(calls)
        self.assertEqual(self.client.proc.pid, pid)
        self.assertIsNone(self.client.proc.poll())


if __name__ == "__main__":
    unittest.main()
