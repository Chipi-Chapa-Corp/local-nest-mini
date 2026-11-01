"""Produce sentence audio ahead of playback; hand off native speech to Cast."""

import queue
import math
import threading
import time

from .tasks import TaskCancelled

END = object()
STOP = object()


class ReplyStream:
    def __init__(self, owner, task_id, text, lang):
        self.owner, self.task_id, self.text, self.lang = owner, task_id, text, lang
        self.lines = queue.Queue(maxsize=16)
        self.audio = queue.Queue(maxsize=4)
        self.ready = threading.Event()
        self.delivered = threading.Event()
        self.stopped = threading.Event()
        self.first = None
        self.error = None
        self.delivered_at = None
        self.native_bytes = 0

    def check(self):
        self.owner.store.check(self.task_id)
        if self.stopped.is_set():
            raise TaskCancelled("stream stopped")

    def put(self, target, value):
        while True:
            self.check()
            try:
                target.put(value, timeout=.1)
                return
            except queue.Full:
                pass

    def take(self, target, extra_stop=None):
        while True:
            self.check()
            if extra_stop and extra_stop():
                return STOP
            try:
                return target.get(timeout=.1)
            except queue.Empty:
                pass

    def fail(self, stage, exc):
        if self.error is not None:
            return
        self.error = exc
        if not isinstance(exc, TaskCancelled):
            self.owner.store.fail(self.task_id, stage, str(exc))
            self.owner.store.cancel(self.task_id)
        self.stopped.set()
        self.ready.set()

    def start(self):
        for target in (self.generate, self.synthesize, self.play):
            threading.Thread(target=target, daemon=True).start()

    def generate(self):
        try:
            self.owner.store.start(self.task_id, "process", text=self.text, streaming=True)
            reply = self.owner.respond(self.text, on_sentence=lambda line: self.put(self.lines, line),
                                       check=self.check)
            self.check()
            self.owner.store.finish(self.task_id, "process", response=reply, streaming=True)
            self.put(self.lines, END)
        except Exception as exc:
            self.fail("process", exc)

    def synthesize(self):
        try:
            count = 0
            input_ended = False
            self.owner.store.start(self.task_id, "tts", streaming=True)

            def next_line(extra_stop=None):
                nonlocal input_ended
                if input_ended:
                    return None
                line = self.take(self.lines, extra_stop)
                if line is END:
                    input_ended = True
                return None if line is END or line is STOP else line

            for text, path in self.owner.synthesize(next_line, self.lang, self.task_id):
                self.check()
                count += 1
                part = {"response": text, "lang": self.lang, "sentence": count,
                        "audio_seconds": self.owner.duration(path, self.task_id),
                        **self.owner.urls(path)}
                if not math.isfinite(part["audio_seconds"]) or part["audio_seconds"] <= 0:
                    raise RuntimeError("Could not determine sentence audio duration")
                if count == 1:
                    part["audio_ogg_path"] = str(self.owner.transcode(path, self.task_id))
                    self.first = {"input": self.text, "task_id": self.task_id, "streaming": True, **part}
                    self.owner.store.start(self.task_id, "output", transport="native+cast", streaming=True)
                    self.ready.set()
                else:
                    self.put(self.audio, part)
                self.owner.store.start(self.task_id, "tts", sentences=count, streaming=True)
            if not count:
                raise RuntimeError("No speech was generated")
            self.owner.store.finish(self.task_id, "tts", sentences=count, streaming=True)
            self.put(self.audio, END)
        except Exception as exc:
            self.fail("tts", exc)

    def first_reply(self):
        deadline = time.monotonic() + 300
        while not self.ready.wait(.1):
            self.check()
            if time.monotonic() > deadline:
                raise RuntimeError("First sentence generation timed out")
        if self.error:
            raise self.error
        self.check()
        return self.first

    def native_delivered(self, size):
        if not self.delivered.is_set():
            self.native_bytes = size
            self.delivered_at = time.monotonic()
            self.delivered.set()

    def play(self):
        try:
            deadline = time.monotonic() + 300
            while not self.delivered.wait(.1):
                self.check()
                if time.monotonic() > deadline:
                    raise RuntimeError("Native sentence was not delivered")
            # Delivery confirms transport, not audible completion. Allow the
            # first clip's duration plus a short device playback guard.
            start_cast = self.delivered_at + self.first["audio_seconds"] + self.owner.guard
            while time.monotonic() < start_cast:
                self.check()
                self.stopped.wait(min(.1, start_cast - time.monotonic()))
            count = 0
            while True:
                part = self.take(self.audio)
                if part is END:
                    break
                result = self.owner.player.play(part["audio_url"], self.task_id,
                                                wait=True, duration=part["audio_seconds"])
                if not result.get("played"):
                    raise RuntimeError(result.get("reason", "Cast playback failed"))
                count += 1
                self.owner.store.start(self.task_id, "output", transport="native+cast",
                                       native_sentences=1, cast_sentences=count)
            self.owner.store.finish(self.task_id, "output", transport="native+cast",
                                    bytes=self.native_bytes, native_sentences=1, cast_sentences=count)
        except Exception as exc:
            self.fail("output", exc)
        finally:
            self.owner.finished(self)


class ReplyStreams:
    def __init__(self, store, *, respond, synthesize, transcode, duration, urls, player, guard=.3):
        self.store, self.respond, self.synthesize = store, respond, synthesize
        self.transcode, self.duration, self.urls = transcode, duration, urls
        self.player, self.guard = player, guard
        self.lock = threading.Lock()
        self.jobs = {}

    def start(self, task_id, text, lang):
        if not text.strip():
            raise ValueError("input must not be empty")
        if lang not in {"en", "ru", "auto"}:
            raise ValueError("lang must be auto, en, or ru")
        with self.lock:
            if task_id in self.jobs:
                raise ValueError("A streaming reply already exists for this task")
            # A new user turn supersedes queued speech from the previous turn.
            for old in self.jobs.values():
                self.store.cancel(old.task_id)
                old.stopped.set()
            job = ReplyStream(self, task_id, text, lang)
            self.jobs[task_id] = job
        job.start()
        try:
            return job.first_reply()
        except Exception as exc:
            job.fail("process", exc)
            raise

    def deliver(self, task_id, size, error=None):
        with self.lock:
            job = self.jobs.get(task_id)
        if job is None:
            return False
        if error:
            job.fail("output", RuntimeError(error))
        else:
            job.native_delivered(size)
        return True

    def finished(self, job):
        with self.lock:
            if self.jobs.get(job.task_id) is job:
                del self.jobs[job.task_id]
