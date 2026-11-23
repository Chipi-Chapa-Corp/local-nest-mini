"""One progressively encoded audio stream per Cast reply."""

import subprocess
import threading
import time


class CastStream:
    def __init__(self, url, task_id, store, player):
        self.url, self.task_id, self.store, self.player = url, task_id, store, player
        self.data = bytearray()
        self.changed = threading.Condition()
        self.done = False
        self.closed = False
        self.encoder = None
        self.reader = None
        self.playback = None
        self.play_result = None
        self.play_error = None

    def append(self, pcm):
        if not pcm:
            return
        self.store.check(self.task_id)
        if self.encoder is None:
            self.encoder = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-probesize", "32", "-analyzeduration", "0",
                 "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
                 "-c:a", "libmp3lame", "-b:a", "64k", "-write_xing", "0",
                 "-id3v2_version", "0", "-flush_packets", "1", "-f", "mp3", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            self.store.register_process(self.task_id, self.encoder)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
        self.encoder.stdin.write(pcm)
        self.encoder.stdin.flush()

    def _read(self):
        try:
            while block := self.encoder.stdout.read1(8192):
                with self.changed:
                    self.data.extend(block)
                    self.changed.notify_all()
                if self.playback is None:
                    self.store.start(self.task_id, "output", transport="cast-live")
                    self.playback = threading.Thread(target=self._play, daemon=True)
                    self.playback.start()
        finally:
            with self.changed:
                self.done = True
                self.changed.notify_all()

    def _play(self):
        try:
            result = self.player.play(self.url, self.task_id, live=True)
            if not result.get("played"):
                raise RuntimeError(result.get("reason", "Cast stream did not start"))
            self.play_result = result
            self.store.start(self.task_id, "output", transport="cast-live",
                             cast_started_at=time.time())
        except Exception as exc:
            self.play_error = exc

    def read(self):
        offset = 0
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.done or len(self.data) > offset
                                      or self.store.cancelled(self.task_id), timeout=.25)
                if self.store.cancelled(self.task_id):
                    return
                if len(self.data) == offset:
                    if self.done:
                        return
                    continue
                block = bytes(self.data[offset:offset + 8192])
            offset += len(block)
            yield block

    def close(self):
        if self.closed:
            return self.play_result
        self.closed = True
        if self.encoder is None:
            with self.changed:
                self.done = True
                self.changed.notify_all()
            return None
        proc = self.encoder
        try:
            proc.stdin.close()
            if proc.wait(timeout=10) and not self.store.cancelled(self.task_id):
                raise RuntimeError("Live MP3 encoding failed")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            self.reader.join()
            proc.stdout.close()
            self.store.unregister_process(self.task_id, proc)
        if self.playback:
            self.playback.join(timeout=30)
            if self.playback.is_alive():
                raise RuntimeError("Live Cast launch timed out")
        if self.play_error is not None:
            raise self.play_error
        return self.play_result
