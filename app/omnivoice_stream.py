"""Read standalone sentence clips from OmniVoice's native --stream-by-line mode."""

import os
from pathlib import Path
import selectors
import struct
import subprocess
import tempfile
import threading
import time
import uuid

from .tasks import TaskCancelled


class NativeStreamError(RuntimeError):
    def __init__(self, message, pending):
        super().__init__(message)
        self.pending = pending


def stream_lines(command, next_line, *, store, task_id, output_dir, timeout=120):
    """Keep one model process for the reply, repairing live WAV sizes per clip.

    Native output uses an identical 44-byte live WAV header at every line.
    Match the complete header, not a bare RIFF sequence in PCM sample data.
    A clip completes at the next header, or EOF for the last line.
    """
    pending = []
    lock = threading.Lock()
    stop = threading.Event()
    feed_errors = []
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(list(map(str, command)), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log)
        store.register_process(task_id, proc)

        def feed():
            try:
                while not stop.is_set():
                    line = next_line(stop.is_set)
                    if line is None:
                        break
                    with lock:
                        pending.append(line)
                    proc.stdin.write((line.replace("\n", " ").replace("\r", " ") + "\n").encode())
                    proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            except Exception as exc:
                feed_errors.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        buffer = bytearray()
        header = None

        def clip(data):
            if len(data) <= 44 or (len(data) - 44) % 2:
                raise RuntimeError("OmniVoice returned an empty or incomplete audio line")
            data = bytearray(data)
            struct.pack_into("<I", data, 4, len(data) - 8)
            struct.pack_into("<I", data, 40, len(data) - 44)
            with lock:
                if not pending:
                    raise RuntimeError("OmniVoice returned an unexpected audio line")
                text = pending.pop(0)
            path = Path(output_dir) / f"reply-{uuid.uuid4().hex}.wav"
            path.write_bytes(data)
            path.chmod(0o644)
            return text, path

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while True:
                    store.check(task_id)
                    if stop.is_set():
                        raise TaskCancelled("stream stopped")
                    if feed_errors:
                        raise feed_errors[0]
                    if time.monotonic() > deadline:
                        raise RuntimeError("OmniVoice line streaming timed out")
                    if not selector.select(.1):
                        continue
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    deadline = time.monotonic() + timeout
                    buffer.extend(data)
                    if header is None and len(buffer) >= 44:
                        header = bytes(buffer[:44])
                        if (header[:4] != b"RIFF" or header[8:16] != b"WAVEfmt "
                                or header[36:40] != b"data"
                                or struct.unpack_from("<HHI", header, 20) != (1, 1, 24000)
                                or struct.unpack_from("<H", header, 34)[0] != 16):
                            raise RuntimeError("Unexpected OmniVoice streaming WAV format")
                    while header is not None:
                        boundary = buffer.find(header, 44)
                        if boundary < 0:
                            break
                        part = bytes(buffer[:boundary])
                        del buffer[:boundary]
                        yield clip(part)
                    if len(buffer) > 64 * 1024 * 1024:
                        raise RuntimeError("OmniVoice audio line is too large")
            if proc.wait(timeout=5):
                raise RuntimeError("OmniVoice native line streaming failed")
            if len(buffer) > 44:
                yield clip(buffer)
            with lock:
                if pending:
                    raise RuntimeError("OmniVoice did not synthesize every line")
        except TaskCancelled:
            raise
        except Exception as exc:
            stop.set()
            if proc.poll() is None:
                store._stop(proc)
            feeder.join(1)
            with lock:
                raise NativeStreamError(str(exc), list(pending)) from exc
        finally:
            stop.set()
            if proc.poll() is None:
                store._stop(proc)
            feeder.join(1)
            store.unregister_process(task_id, proc)
            proc.stdout.close()
