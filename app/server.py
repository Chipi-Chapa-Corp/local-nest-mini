#!/usr/bin/env python3
"""Small HTTP service for local transcription, LLM replies, and TTS."""

import json
import math
import os
import re
import subprocess
import threading
import tempfile
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .monitor import start_monitor
from .omnivoice_tts import MODEL as OMNIVOICE_MODEL_NAME
from .player import PLAYER
from .processor import respond
from .tasks import TASKS, TaskCancelled
from .test_pipeline import TestPipeline

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TTS_DIR = Path(os.environ.get("TTS_DIR", str(DATA_DIR / "tts")))
TTS_DIR.mkdir(parents=True, exist_ok=True)

STT_MODEL = Path(os.environ.get("STT_MODEL", "/app/models/stt/futo.bin"))
WHISPER_BIN = Path(os.environ.get("WHISPER_BIN", "/usr/local/bin/whisper-cli"))
PROJECT_DIR = Path(__file__).resolve().parent.parent
TTS_BACKEND = os.environ.get("TTS_BACKEND", "omnivoice").lower()
OMNIVOICE_PYTHON = Path(os.environ.get("OMNIVOICE_PYTHON", str(PROJECT_DIR / "models/omnivoice-env/bin/python")))
OMNIVOICE_DIR = Path(os.environ.get("OMNIVOICE_DIR", str(PROJECT_DIR / "models/omnivoice")))
OMNIVOICE_REFERENCE = Path(os.environ.get("OMNIVOICE_REFERENCE", str(PROJECT_DIR / "models/omnivoice-voices/laylo-en.wav")))
OMNIVOICE_THREADS = int(os.environ.get("OMNIVOICE_THREADS", "6"))
# CPU fallback steps match the GPU setting by default (closest equivalent setup).
OMNIVOICE_STEPS_EN = int(os.environ.get("OMNIVOICE_STEPS_EN", "24"))
OMNIVOICE_STEPS_RU = int(os.environ.get("OMNIVOICE_STEPS_RU", "24"))
# GPU path: omnivoice.cpp on Vulkan, used per reply when enough VRAM is free.
OMNIVOICE_GPU = os.environ.get("OMNIVOICE_GPU", "auto").lower()  # auto | off
OMNIVOICE_GPU_BIN = Path(os.environ.get(
    "OMNIVOICE_GPU_BIN", str(PROJECT_DIR / "models/omnivoice-cpp-src/build-vulkan/omnivoice-tts")))
OMNIVOICE_GGUF_DIR = Path(os.environ.get("OMNIVOICE_GGUF_DIR", str(PROJECT_DIR / "models/omnivoice-cpp-gguf")))
OMNIVOICE_GPU_MODEL = Path(os.environ.get("OMNIVOICE_GPU_MODEL", str(OMNIVOICE_GGUF_DIR / "omnivoice-base-Q8_0.gguf")))
OMNIVOICE_GPU_CODEC = Path(os.environ.get(
    "OMNIVOICE_GPU_CODEC", str(OMNIVOICE_GGUF_DIR / "omnivoice-tokenizer-F32.gguf")))
OMNIVOICE_GPU_DEVICE = os.environ.get("OMNIVOICE_GPU_DEVICE", "Vulkan0")
OMNIVOICE_GPU_STEPS = int(os.environ.get("OMNIVOICE_GPU_STEPS", "24"))
# Q8_0 peaked at 1.3-1.6 GiB of VRAM in benchmarks; require more than 1.7 GiB free.
OMNIVOICE_GPU_MIN_FREE_MIB = float(os.environ.get("OMNIVOICE_GPU_MIN_FREE_MIB", "1740"))  # 1.7 GiB
DRM_ROOT = Path("/sys/class/drm")
OMNIVOICE_LOCK = threading.Lock()
PC_IP = os.environ.get("PC_IP", "127.0.0.1")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://{PC_IP}/tts").rstrip("/")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))
STT_THREADS = int(os.environ.get("STT_THREADS", "6"))
# ffmpeg defaults to ~30 threads on this CPU; the unit caps tasks, and these
# clips are tiny. One decoder/filter thread keeps each call at ~6 threads.
FFMPEG_THREADS = ("-threads", "1", "-filter_threads", "1")
MAX_BODY = int(os.environ.get("MAX_BODY", str(32 * 1024 * 1024)))
PLAYER_ENABLED = os.environ.get("PLAYER_ENABLED", "0").lower() in {"1", "true", "yes"}
OUTPUT_MODE = os.environ.get("OUTPUT_MODE", "cast").lower()
NATIVE_BITRATE = os.environ.get("NATIVE_BITRATE", "32k")
NATIVE_TEMPLATE = Path(os.environ.get("NATIVE_TEMPLATE", str(DATA_DIR / "native-template.bin")))
MONITOR_HOST = os.environ.get("MONITOR_HOST", "127.0.0.1")
MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "53412"))
MONITOR_ENABLED = os.environ.get("MONITOR_ENABLED", "1").lower() in {"1", "true", "yes"}
LOG_PATH = DATA_DIR / "logs" / "proxy-events.jsonl"


def _stop_process(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(cmd, *, timeout, stdin=None, task_id=None):
    TASKS.check(task_id)
    argv = [str(part) for part in cmd]
    if not task_id:
        return subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    TASKS.register_process(task_id, proc)
    try:
        if stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()
        deadline = time.monotonic() + timeout
        while True:
            TASKS.check(task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(proc)
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        TASKS.check(task_id)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except BaseException:
        _stop_process(proc)
        raise
    finally:
        TASKS.unregister_process(task_id, proc)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _audio_duration(path: Path, task_id: str | None = None) -> float:
    result = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        timeout=15,
        task_id=task_id,
    )
    try:
        return float(result.stdout.decode().strip())
    except ValueError:
        return 0.0


def _prepare_audio(source: Path, destination: Path, task_id: str | None = None):
    result = _run(
        ["ffmpeg", "-v", "error", "-y", *FFMPEG_THREADS, "-i", source, "-ar", "16000",
         "-ac", "1", "-f", "wav", destination],
        timeout=60,
        task_id=task_id,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "audio conversion failed")


def transcribe_audio(source: Path, lang: str = "auto", task_id: str | None = None) -> str:
    if not STT_MODEL.is_file():
        raise RuntimeError(f"STT model missing: {STT_MODEL}")
    with tempfile.TemporaryDirectory(prefix="stt-") as temp:
        temp = Path(temp)
        wav = temp / "input.wav"
        _prepare_audio(source, wav, task_id)
        duration = _audio_duration(wav, task_id)
        audio_ctx = min(1500, max(64, math.ceil(duration * 50) + 64)) if duration else 1500
        lang = (lang or "auto").lower()
        if lang not in {"auto", "en", "ru"}:
            raise ValueError("lang must be auto, en, or ru")
        result = _run(
            [WHISPER_BIN, "-m", STT_MODEL, "-l", lang, "-t", STT_THREADS,
             "-nt", "-np", "--audio-ctx", str(audio_ctx), "-f", wav],
            timeout=300,
            task_id=task_id,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or "whisper failed")
        lines = []
        for line in result.stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith(("whisper_", "system_info:", "main:")):
                lines.append(line)
        text = " ".join(lines).strip()
        return text or ""


def _has_cyrillic(text):
    return any("\u0400" <= char <= "\u04ff" for char in text)


def _omnivoice_ready():
    return OMNIVOICE_PYTHON.is_file() and OMNIVOICE_REFERENCE.is_file() and all(
        (OMNIVOICE_DIR / name).is_file() for name in (
            "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
            "audio_tokenizer/config.json", "audio_tokenizer/model.safetensors",
            "audio_tokenizer/preprocessor_config.json"))


def _omnivoice_gpu_ready():
    return (OMNIVOICE_GPU != "off" and os.access(OMNIVOICE_GPU_BIN, os.X_OK)
            and OMNIVOICE_GPU_MODEL.is_file() and OMNIVOICE_GPU_CODEC.is_file()
            and OMNIVOICE_REFERENCE.is_file())


def _gpu_free_vram_mib(root=None):
    """Free VRAM of the GPU with the most VRAM (the discrete card), from amdgpu sysfs.

    Two small file reads (~50 µs). None when no card reports VRAM.
    """
    best = None
    for card in Path(root or DRM_ROOT).glob("card*"):
        if not card.name[4:].isdigit():
            continue  # connector entries such as card1-DP-1
        try:
            total = int((card / "device/mem_info_vram_total").read_text())
            used = int((card / "device/mem_info_vram_used").read_text())
        except (OSError, ValueError):
            continue
        if best is None or total > best[0]:
            best = (total, used)
    return None if best is None else (best[0] - best[1]) / 1048576


# OmniVoice's non-verbal tags, plus any other bracketed ASCII markup an LLM may emit.
SOUND_TAG = re.compile(r"\s*\[[A-Za-z][A-Za-z0-9_-]*\]")


def _strip_sound_tags(text):
    text = SOUND_TAG.sub(" ", text)
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _synthesize_omnivoice_gpu(text, lang, output, task_id, free_mib):
    """Run omnivoice.cpp on Vulkan; return its metrics or raise."""
    from .omnivoice_tts import REFERENCE_TEXT
    reference_rvq = OMNIVOICE_REFERENCE.with_suffix(".rvq")
    with tempfile.TemporaryDirectory(prefix="omnivoice-gpu-") as temp:
        ref_text = Path(temp) / "reference.txt"
        ref_text.write_text(REFERENCE_TEXT)
        reference = (["--ref-rvq", reference_rvq] if reference_rvq.is_file()
                     else ["--ref-wav", OMNIVOICE_REFERENCE])
        started = time.monotonic()
        result = _run([
            OMNIVOICE_GPU_BIN, "--device", OMNIVOICE_GPU_DEVICE,
            "--model", OMNIVOICE_GPU_MODEL, "--codec", OMNIVOICE_GPU_CODEC,
            "--lang", lang, *reference, "--ref-text", ref_text,
            "--seed", "1234", "--steps", OMNIVOICE_GPU_STEPS, "-o", output,
        ], timeout=120, stdin=text.encode(), task_id=task_id)
        wall = time.monotonic() - started
    log = (result.stdout + result.stderr).decode(errors="replace")
    if result.returncode or not output.is_file() or output.stat().st_size <= 44:
        raise RuntimeError(log.strip()[-2000:] or "OmniVoice GPU generation failed")
    synth = re.search(r"\[Perf\] Total ([\d.]+) ms", log)
    audio = re.findall(r"Post-proc: .*\(([\d.]+)s at", log)
    generation = float(synth.group(1)) / 1000 if synth else wall
    audio_seconds = sum(float(value) for value in audio)
    return dict(backend="omnivoice-vulkan", engine="omnivoice.cpp / Vulkan", device=OMNIVOICE_GPU_DEVICE,
                precision="Q8_0 transformer / F32 codec", language=lang, steps=OMNIVOICE_GPU_STEPS,
                load_seconds=round(wall - generation, 3), generation_seconds=round(generation, 3),
                audio_seconds=round(audio_seconds, 3),
                rtf=round(generation / audio_seconds, 3) if audio_seconds else None,
                vram_free_mib=round(free_mib))


def _synthesize_omnivoice(text, lang, output, task_id):
    if not _omnivoice_ready():
        raise RuntimeError("OmniVoice environment, model or voice reference missing; run host/setup-omnivoice.sh")
    text = _strip_sound_tags(text)
    if not text:
        raise ValueError("text must contain speech, not only sound tags")
    if task_id:
        TASKS.start(task_id, "tts", engine="OmniVoice")
    while not OMNIVOICE_LOCK.acquire(timeout=.2):
        TASKS.check(task_id)
    try:
        TASKS.check(task_id)
        # Probe VRAM after the lock, right before running: a game may have started meanwhile.
        gpu_ready = _omnivoice_gpu_ready()
        free = _gpu_free_vram_mib() if gpu_ready else None
        if not gpu_ready:
            cpu_reason = "GPU disabled or not set up"
        elif free is None:
            cpu_reason = "no VRAM information"
        else:
            cpu_reason = f"{free:.0f} MiB VRAM free, need more than {OMNIVOICE_GPU_MIN_FREE_MIB:.0f}"
        if free is not None and free > OMNIVOICE_GPU_MIN_FREE_MIB:
            if task_id:
                TASKS.start(task_id, "tts", engine="OmniVoice GPU (Vulkan)")
            try:
                report = _synthesize_omnivoice_gpu(text, lang, output, task_id, free)
                if task_id:
                    TASKS.start(task_id, "tts", metrics=report)
                return output
            except TaskCancelled:
                raise
            except Exception as exc:
                output.unlink(missing_ok=True)
                cpu_reason = f"GPU failed: {str(exc).strip().splitlines()[-1] if str(exc).strip() else type(exc).__name__}"
                print(json.dumps({"kind": "omnivoice_gpu_failed", "error": str(exc)[-2000:]}), flush=True)
        if task_id:
            TASKS.start(task_id, "tts", engine="OmniVoice CPU", cpu_reason=cpu_reason)
        result = _run([
            OMNIVOICE_PYTHON, Path(__file__).with_name("omnivoice_tts.py"),
            "--model-dir", OMNIVOICE_DIR, "--reference", OMNIVOICE_REFERENCE,
            "--text", text, "--output", output, "--lang", lang,
            "--threads", OMNIVOICE_THREADS,
            "--steps", OMNIVOICE_STEPS_RU if lang == "ru" else OMNIVOICE_STEPS_EN,
        ], timeout=600, task_id=task_id)
        if result.returncode or not output.is_file():
            detail = result.stderr.decode(errors="replace").strip()[-4000:]
            raise RuntimeError(detail or "OmniVoice generation failed")
        report = json.loads(result.stdout.decode().strip().splitlines()[-1])
        if task_id:
            TASKS.start(task_id, "tts", metrics=report)
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        OMNIVOICE_LOCK.release()


def synthesize(text: str, lang: str = "en", task_id: str | None = None) -> Path:
    text = text.strip()
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > 2000:
        raise ValueError("text is too long (max 2000 characters)")
    lang = (lang or "en").lower()
    if lang == "auto":
        lang = "ru" if _has_cyrillic(text) else "en"
    if lang not in {"en", "ru"}:
        raise ValueError("lang must be en or ru")
    output = TTS_DIR / f"reply-{uuid.uuid4().hex}.wav"
    if TTS_BACKEND != "omnivoice":
        raise RuntimeError(f"Unknown TTS_BACKEND: {TTS_BACKEND}")
    _synthesize_omnivoice(text, lang, output, task_id)
    output.chmod(0o644)
    return output


def tts_urls(path: Path):
    return {
        "audio_path": str(path),
        "audio_url": f"{PUBLIC_BASE_URL}/{path.name}",
        "audio_local_url": f"http://127.0.0.1:{LISTEN_PORT}/tts/{path.name}",
    }


def transcode_ogg(source: Path, task_id: str | None = None) -> Path:
    output = source.with_suffix(".ogg")
    result = _run(
        ["ffmpeg", "-v", "error", "-y", *FFMPEG_THREADS, "-i", source, "-c:a", "libopus",
         "-b:a", NATIVE_BITRATE, "-ar", "48000", "-ac", "1", "-f", "ogg", output],
        timeout=60,
        task_id=task_id,
    )
    if result.returncode or not output.is_file():
        output.unlink(missing_ok=True)
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "Opus conversion failed")
    output.chmod(0o644)
    return output


def _wants_play(data, query):
    value = data.get("play", query.get("play", ["0"])[0])
    return str(value).lower() in {"1", "true", "yes"}


def _task_id(query, data=None):
    raw = (data or {}).get("task_id") or query.get("task_id", [""])[0]
    raw = str(raw or "").strip()
    return raw[:200] or None


def _ensure_task(task_id, **meta):
    task_id = task_id or uuid.uuid4().hex
    if not TASKS.exists(task_id):
        TASKS.create(task_id, **meta)
        TASKS.finish(task_id, "received", **meta)
    return task_id


def _fail(task_id, stage, exc):
    TASKS.fail(task_id, stage, f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())


def _output(task_id, result, data, query):
    if OUTPUT_MODE == "native":
        TASKS.start(task_id, "output", transport="native")
        return
    if not _wants_play(data, query):
        TASKS.skip(task_id, "output", reason="play=false")
        return
    TASKS.start(task_id, "output", transport="cast")
    try:
        player = PLAYER.play(result["audio_url"], task_id)
    except TaskCancelled as exc:
        TASKS.stage(task_id, "output", "interrupted", error=str(exc))
        raise
    except Exception as exc:
        _fail(task_id, "output", exc)
        raise
    result["player"] = player
    TASKS.finish(task_id, "output", **player)


class Handler(BaseHTTPRequestHandler):
    server_version = "local-nest-mini/1"

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            raise ValueError("invalid content-length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body is too large")
        return self.rfile.read(length)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            checks = {
                "stt_model": STT_MODEL.is_file(),
                "whisper": WHISPER_BIN.is_file(),
                "tts": _omnivoice_ready(),
                "tts_backend": TTS_BACKEND,
                # Informational (a string, so it never fails the check): CPU covers an absent GPU.
                "tts_gpu": "ready" if _omnivoice_gpu_ready() else "unavailable",
                "tts_backend_supported": TTS_BACKEND == "omnivoice",
                "russian_tts": _omnivoice_ready(),
                "tts_dir": TTS_DIR.is_dir(),
                "player": OUTPUT_MODE != "cast" or (not PLAYER_ENABLED) or PLAYER.available(),
                "output_mode": OUTPUT_MODE,
                "native_template": OUTPUT_MODE != "native" or NATIVE_TEMPLATE.is_file(),
            }
            self._json(200 if all(checks.values()) else 503,
                       {"ok": all(checks.values()), "checks": checks})
            return
        if path == "/models":
            self._json(200, {
                "stt": str(STT_MODEL),
                "tts_backend": TTS_BACKEND,
                "tts_en": OMNIVOICE_MODEL_NAME,
                "tts_ru": OMNIVOICE_MODEL_NAME,
            })
            return
        if path == "/tasks":
            self._json(200, {"tasks": TASKS.snapshot()})
            return
        if path.startswith("/tts/"):
            name = Path(path[5:]).name
            audio = TTS_DIR / name
            if name != path[5:] or not audio.is_file() or audio.suffix.lower() != ".wav":
                self._json(404, {"error": "not found"})
                return
            body = audio.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "audio/wav")
            self.send_header("access-control-allow-origin", "*")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._json(200, {
            "service": "local-nest-mini processor",
            "endpoints": ["/health", "/models", "/process", "/transcribe", "/turn", "/speak", "/answer", "/task/start", "/task/output", "/tasks"],
        })

    def do_POST(self):
        task_id = None
        stage = "request"
        try:
            query = self._query()
            path = urlparse(self.path).path
            raw = self._body()
            data = json.loads(raw or b"{}") if path not in {"/transcribe", "/turn"} else {}
            task_id = _task_id(query, data)
            if path == "/task/cancel":
                if not task_id:
                    raise ValueError("task_id is required")
                # Remember cancellation even if the S3 worker is still waiting
                # for its model lock and has not sent /task/start yet.
                if not TASKS.exists(task_id):
                    TASKS.create(task_id, source="nest")
                TASKS.cancel(task_id)
                self._json(200, {"task_id": task_id, "interrupted": True})
                return
            if path == "/task/start":
                meta = dict(data)
                meta.pop("task_id", None)
                created = meta.pop("received", None)
                task_id = _ensure_task(task_id, created=created, source="nest", **meta)
                TASKS.finish(task_id, "received", **meta)
                TASKS.check(task_id)
                self._json(200, {"task_id": task_id})
                return
            if path == "/task/output":
                task_id = _ensure_task(task_id, source="nest")
                meta = dict(data)
                meta.pop("task_id", None)
                error = meta.pop("error", None)
                if error:
                    TASKS.fail(task_id, "output", str(error), **meta)
                else:
                    TASKS.finish(task_id, "output", **meta)
                self._json(200, {"task_id": task_id})
                return
            if path == "/process":
                task_id = _ensure_task(task_id, source="http")
                text = str(data.get("input", "")).strip()
                stage = "process"
                TASKS.start(task_id, stage)
                response = respond(text)
                TASKS.check(task_id)
                TASKS.finish(task_id, stage, input=text, response=response)
                self._json(200, {"input": text, "response": response, "task_id": task_id})
                return
            if path == "/speak":
                task_id = _ensure_task(task_id, source="http")
                text = str(data.get("text", "")).strip()
                lang = str(data.get("lang", query.get("lang", ["en"])[0]))
                stage = "tts"
                TASKS.start(task_id, stage, text=text, lang=lang)
                audio = synthesize(text, lang, task_id)
                result = {"text": text, "lang": lang, **tts_urls(audio), "task_id": task_id}
                if OUTPUT_MODE == "native":
                    result["audio_ogg_path"] = str(transcode_ogg(audio, task_id))
                TASKS.finish(task_id, stage, audio_url=result["audio_url"])
                _output(task_id, result, data, query)
                self._json(200, result)
                return
            if path == "/answer":
                task_id = _ensure_task(task_id, source="http")
                text = str(data.get("text", "")).strip()
                lang = str(data.get("lang", "auto")).lower()
                stage = "process"
                TASKS.start(task_id, stage, text=text, lang=lang)
                reply = respond(text)
                TASKS.check(task_id)
                TASKS.finish(task_id, stage, response=reply)
                stage = "tts"
                TASKS.start(task_id, stage, text=reply, lang=lang)
                audio = synthesize(reply, lang, task_id)
                result = {"input": text, "response": reply, "lang": lang, **tts_urls(audio), "task_id": task_id}
                if OUTPUT_MODE == "native":
                    result["audio_ogg_path"] = str(transcode_ogg(audio, task_id))
                TASKS.finish(task_id, stage, audio_url=result["audio_url"])
                _output(task_id, result, data, query)
                self._json(200, result)
                return
            if path in {"/transcribe", "/turn"}:
                if not raw:
                    raise ValueError("audio body is required")
                task_id = _ensure_task(task_id, source="http")
                lang = str(query.get("lang", ["auto"])[0])
                stage = "transcribe"
                TASKS.start(task_id, stage, lang=lang, bytes=len(raw))
                with tempfile.NamedTemporaryFile(prefix="input-", suffix=".audio",
                                                 dir=DATA_DIR, delete=False) as temp:
                    temp.write(raw)
                    source = Path(temp.name)
                try:
                    text = transcribe_audio(source, lang, task_id)
                finally:
                    source.unlink(missing_ok=True)
                TASKS.finish(task_id, stage, text=text, lang=lang)
                if path == "/transcribe":
                    self._json(200, {"text": text, "lang": lang, "task_id": task_id})
                    return
                stage = "process"
                TASKS.start(task_id, stage, text=text)
                reply = respond(text)
                TASKS.check(task_id)
                TASKS.finish(task_id, stage, response=reply)
                result = {"input": text, "response": reply, "lang": lang, "task_id": task_id}
                if query.get("speak", ["1"])[0] not in {"0", "false", "no"}:
                    stage = "tts"
                    TASKS.start(task_id, stage, text=reply, lang=lang)
                    audio = synthesize(reply, lang, task_id)
                    result.update(tts_urls(audio))
                    if OUTPUT_MODE == "native":
                        result["audio_ogg_path"] = str(transcode_ogg(audio, task_id))
                    TASKS.finish(task_id, stage, audio_url=result["audio_url"])
                    _output(task_id, result, data, query)
                else:
                    TASKS.skip(task_id, "tts", reason="speak=false")
                    TASKS.skip(task_id, "output", reason="speak=false")
                self._json(200, result)
                return
            self._json(404, {"error": "not found"})
        except TaskCancelled as exc:
            if task_id:
                TASKS.stage(task_id, stage, "interrupted", error=str(exc))
            self._json(409, {"error": "interrupted", "task_id": task_id})
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if task_id:
                _fail(task_id, stage, exc)
            self._json(400 if isinstance(exc, ValueError) else 500,
                       {"error": f"{type(exc).__name__}: {exc}", "task_id": task_id})
        except Exception as exc:
            if task_id:
                _fail(task_id, stage, exc)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}", "task_id": task_id})

    def log_message(self, fmt, *args):
        print(json.dumps({"time": self.log_date_time_string(), "client": self.client_address[0],
                          "message": fmt % args}), flush=True)


def main():
    if MONITOR_ENABLED:
        tests = TestPipeline(TASKS, transcribe=transcribe_audio, respond=respond,
                             synthesize=synthesize, urls=tts_urls, player=PLAYER, data_dir=DATA_DIR)
        monitor = start_monitor(TASKS, host=MONITOR_HOST, port=MONITOR_PORT,
                                log_path=LOG_PATH, test_pipeline=tests, max_body=MAX_BODY)
        print(f"monitor listening on {MONITOR_HOST}:{monitor.server_port}", flush=True)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    if OUTPUT_MODE == "cast" and PLAYER_ENABLED and PLAYER.available():
        threading.Thread(target=PLAYER.warm, name="cast-warm", daemon=True).start()
    print(f"processor listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
