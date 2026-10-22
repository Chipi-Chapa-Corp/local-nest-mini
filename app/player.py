"""Play generated WAV files on the selected Cast device."""

import os
import threading
import time

from .tasks import TASKS, TaskCancelled

CAST_NAME = os.environ.get("CAST_NAME", "Office speaker")
ENABLED = os.environ.get("PLAYER_ENABLED", "0").lower() in {"1", "true", "yes"}
TIMEOUT = float(os.environ.get("CAST_TIMEOUT", "10"))
DISCOVERY_TIMEOUT = float(os.environ.get("CAST_DISCOVERY_TIMEOUT", "3"))
CONNECT_TIMEOUT = float(os.environ.get("CAST_CONNECT_TIMEOUT", "3"))


class _Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._cast = None
        self._browser = None

    def available(self):
        try:
            import pychromecast  # noqa: F401
        except ImportError:
            return False
        return True

    def _connect(self):
        if self._cast is not None:
            return self._cast
        import pychromecast

        casts, browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT)
        cast = next((item for item in casts if item.name == CAST_NAME), None)
        if cast is None:
            pychromecast.discovery.stop_discovery(browser)
            raise RuntimeError(f"Cast device not found: {CAST_NAME}")
        cast.wait(timeout=CONNECT_TIMEOUT)
        self._cast = cast
        self._browser = browser
        return cast

    def warm(self):
        if not ENABLED:
            return
        try:
            with self._lock:
                self._connect()
        except Exception:
            pass

    def play(self, url: str, task_id: str | None = None):
        if not ENABLED:
            return {"played": False, "reason": "player disabled"}
        with self._lock:
            TASKS.check(task_id)
            try:
                return self._play_locked(url, task_id)
            except TaskCancelled:
                self.close()
                raise
            except Exception:
                self.close()
                TASKS.check(task_id)
                return self._play_locked(url, task_id)

    def _play_locked(self, url: str, task_id: str | None = None):
        cast = self._connect()
        media = cast.media_controller
        media.play_media(url, "audio/wav")
        seen_active = False
        state = "UNKNOWN"
        for _ in range(int(TIMEOUT / 0.25)):
            time.sleep(0.25)
            if TASKS.cancelled(task_id):
                media.stop()
                raise TaskCancelled("task interrupted")
            state = media.status.player_state
            if state in {"PLAYING", "BUFFERING"}:
                seen_active = True
                break
            if state == "IDLE" and seen_active:
                break
        content_id = media.status.content_id or ""
        if state in {"PLAYING", "BUFFERING"}:
            return {"played": True, "state": state, "url": url}
        if state == "IDLE" and content_id == url:
            return {"played": True, "state": state, "url": url}
        raise RuntimeError(f"Cast did not start playback: {state}")

    def close(self):
        if self._browser is not None:
            try:
                import pychromecast

                pychromecast.discovery.stop_discovery(self._browser)
            finally:
                self._browser = None
                self._cast = None


PLAYER = _Player()
