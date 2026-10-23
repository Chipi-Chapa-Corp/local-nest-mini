"""Browser-recorded test turns using the same local STT, processor and TTS."""

import threading
import tempfile
from pathlib import Path

from .tasks import TaskCancelled


class TestPipeline:
    def __init__(self, store, *, transcribe, respond, synthesize, urls, player, data_dir):
        self.store = store
        self.transcribe = transcribe
        self.respond = respond
        self.synthesize = synthesize
        self.urls = urls
        self.player = player
        self.data_dir = data_dir
        self.lock = threading.RLock()

    def create(self):
        task = self.store.create(source="browser_test")
        self.store.stage(task["id"], "received", "waiting")
        return task["id"]

    def _task(self, task_id):
        task = self.store.get(task_id)
        if not task or task["meta"].get("source") != "browser_test":
            raise ValueError("Test pipeline not found")
        self.store.check(task_id)
        return task

    @staticmethod
    def _stage(task, name):
        return next((s for s in task["stages"] if s["name"] == name), {})

    def receive(self, task_id, audio):
        with self.lock:
            task = self._task(task_id)
            if self._stage(task, "received").get("status") != "waiting":
                raise ValueError("This pipeline already has a recording")
            if not audio:
                raise ValueError("A recording is required")
            self.store.start(task_id, "received", bytes=len(audio))
            threading.Thread(target=self._generate, args=(task_id, audio), daemon=True).start()

    def _generate(self, task_id, audio):
        stage = "received"
        try:
            with tempfile.NamedTemporaryFile(suffix=".audio", dir=self.data_dir) as temp:
                temp.write(audio)
                temp.flush()
                self.store.check(task_id)
                self.store.finish(task_id, stage, bytes=len(audio))
                stage = "transcribe"
                self.store.start(task_id, stage)
                text = self.transcribe(Path(temp.name), "auto", task_id)
            self.store.check(task_id)
            if not text.strip():
                raise ValueError("No speech detected. Create a new Test and try again.")
            self.store.finish(task_id, stage, text=text)
            stage = "process"
            self.store.start(task_id, stage)
            response = self.respond(text)
            lang = "ru" if any("\u0400" <= c <= "\u04ff" for c in text) else "en"
            self.store.check(task_id)
            self.store.finish(task_id, stage, response=response, lang=lang)
            stage = "tts"
            self.store.start(task_id, stage)
            audio_path = self.synthesize(response, lang, task_id)
            self.store.check(task_id)
            self.store.finish(task_id, stage, **self.urls(audio_path))
            self.store.stage(task_id, "output", "waiting")
        except TaskCancelled:
            self.store.stage(task_id, stage, "interrupted")
        except Exception as exc:
            self.store.fail(task_id, stage, str(exc))

    def audio(self, task_id):
        task = self._task(task_id)
        tts = self._stage(task, "tts")
        if tts.get("status") != "done":
            raise ValueError("Audio is not ready")
        path = Path(tts["detail"]["audio_path"])
        if not path.is_file():
            raise ValueError("Audio is no longer available")
        return path

    def output(self, task_id, target):
        with self.lock:
            task = self._task(task_id)
            self.audio(task_id)
            if self._stage(task, "output").get("status") == "loading":
                raise ValueError("Already casting to Nest")
            if target == "browser":
                self.store.finish(task_id, "output", transport="browser", last_error="")
            elif target == "cast":
                url = self._stage(task, "tts")["detail"]["audio_url"]
                self.store.start(task_id, "output", transport="cast", last_error="")
                threading.Thread(target=self._cast, args=(task_id, url), daemon=True).start()
            else:
                raise ValueError("Unknown playback target")

    def _cast(self, task_id, url):
        try:
            self.store.check(task_id)
            result = self.player.play(url, task_id)
            self.store.check(task_id)
            if not result.get("played"):
                raise RuntimeError(result.get("reason", "Cast did not start"))
            self.store.finish(task_id, "output", transport="cast", **result)
        except TaskCancelled:
            self.store.stage(task_id, "output", "interrupted")
        except Exception as exc:
            # Keep generated audio available for another Cast attempt or browser playback.
            self.store.stage(task_id, "output", "waiting", last_error=str(exc))
