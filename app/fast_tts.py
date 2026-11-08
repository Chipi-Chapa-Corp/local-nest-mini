"""Cancellable client for a resident Silero/Pocket worker in its own environment."""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


ASSET_FILES = ("silero-v5_6_ru.pt", "pocket.yaml", "pocket-model.safetensors",
               "pocket-tokenizer.model", "pocket-alba.safetensors")
MODELS = {"ru": "Silero v5.6 / Xenia", "en": "Pocket TTS 2M / Alba"}


class FastTTS:
    def __init__(self, python, assets, *, threads=2, worker=None):
        self.python, self.assets = Path(python), Path(assets)
        self.threads = threads
        self.worker = Path(worker or Path(__file__).with_name("fast_tts_worker.py"))
        self.lock = threading.Lock()
        self.proc = None
        self.responses = None

    def available(self):
        return self.python.is_file() and all((self.assets / name).is_file() for name in ASSET_FILES)

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe:
                pipe.close()

    def close(self):
        with self.lock:
            self._stop()

    def _receive(self, check, timeout):
        deadline = time.monotonic() + timeout
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Speech model timed out")
            try:
                line = self.responses.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("Speech model worker exited")
            return json.loads(line)

    def _start(self, check):
        if self.proc is not None and self.proc.poll() is None:
            return
        self._stop()
        if not self.available():
            raise RuntimeError("Speech models missing; run bash host/setup-fast-tts.sh")
        channel = self.responses = queue.Queue()
        self.proc = subprocess.Popen(
            [str(self.python), "-I", str(self.worker), "--assets", str(self.assets),
             "--threads", str(self.threads)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "HF_HUB_OFFLINE": "1"})

        def read(pipe):
            try:
                for line in pipe:
                    channel.put(line)
            finally:
                channel.put(None)

        threading.Thread(target=read, args=(self.proc.stdout,), daemon=True).start()
        if not self._receive(check, 60).get("ready"):
            raise RuntimeError("Speech model worker did not become ready")

    def _acquire(self, check):
        while not self.lock.acquire(timeout=.05):
            check()

    def warm(self, check=lambda: None):
        self._acquire(check)
        try:
            check()
            self._start(check)
        except BaseException:
            self._stop()
            raise
        finally:
            self.lock.release()

    def synthesize(self, text, lang, output, *, check=lambda: None, timeout=120):
        self._acquire(check)
        try:
            check()
            self._start(check)
            self.proc.stdin.write(json.dumps({"text": text, "lang": lang, "output": str(output)}) + "\n")
            self.proc.stdin.flush()
            result = self._receive(check, timeout)
            check()
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Speech generation failed"))
            if not Path(output).is_file():
                raise RuntimeError("Speech worker returned no audio file")
            return result
        except BaseException:
            self._stop()
            Path(output).unlink(missing_ok=True)
            raise
        finally:
            self.lock.release()
