#!/usr/bin/env python3
"""Small HTTP service for local transcription, LLM replies, and TTS."""

import json
import math
import os
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
from .kitten_tts import REVISION as KITTEN_REVISION
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
POCKET_BIN = Path(os.environ.get("POCKET_BIN", "/usr/local/bin/pocket-tts"))
PROJECT_DIR = Path(__file__).resolve().parent.parent
TTS_BACKEND = os.environ.get("TTS_BACKEND", "omnivoice").lower()
OMNIVOICE_PYTHON = Path(os.environ.get("OMNIVOICE_PYTHON", str(PROJECT_DIR / "models/omnivoice-env/bin/python")))
OMNIVOICE_DIR = Path(os.environ.get("OMNIVOICE_DIR", str(PROJECT_DIR / "models/omnivoice")))
OMNIVOICE_REFERENCE = Path(os.environ.get("OMNIVOICE_REFERENCE", str(PROJECT_DIR / "models/omnivoice-voices/laylo-en.wav")))
OMNIVOICE_THREADS = int(os.environ.get("OMNIVOICE_THREADS", "6"))
OMNIVOICE_STEPS_EN = int(os.environ.get("OMNIVOICE_STEPS_EN", "16"))
OMNIVOICE_STEPS_RU = int(os.environ.get("OMNIVOICE_STEPS_RU", "32"))
OMNIVOICE_LOCK = threading.Lock()
KITTEN_PYTHON = Path(os.environ.get("KITTEN_PYTHON", str(PROJECT_DIR / "models/kitten-env/bin/python")))
KITTEN_CACHE = Path(os.environ.get("KITTEN_CACHE", str(PROJECT_DIR / "models/kitten-tts-2")))
KITTEN_MINI_PYTHON = Path(os.environ.get("KITTEN_MINI_PYTHON", str(PROJECT_DIR / "models/kitten-mini-env/bin/python")))
KITTEN_MINI_DIR = Path(os.environ.get("KITTEN_MINI_DIR", str(PROJECT_DIR / "models/kitten-tts-mini-0.8")))
KITTEN_DECODER = os.environ.get("KITTEN_DECODER", "default")
KITTEN_THREADS = int(os.environ.get("KITTEN_THREADS", "6"))
KITTEN_DECODER_THREADS = int(os.environ.get("KITTEN_DECODER_THREADS", "2"))
KITTEN_VOICE_EN = os.environ.get("KITTEN_VOICE_EN", "Bruno")
KITTEN_VOICE_RU = os.environ.get("KITTEN_VOICE_RU", "Russian")
# Keep simultaneous requests from loading several copies inside the service's memory cap.
KITTEN_LOCK = threading.Lock()
RU_CONFIG = Path(os.environ.get("RU_CONFIG", "/app/models/ru/config.yaml"))
RU_VOICE = Path(os.environ.get("RU_VOICE", "/app/models/voices/ru.safetensors"))
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
            timeout=180,
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


def _kitten_ready():
    if TTS_BACKEND == "kitten-mini":
        return KITTEN_MINI_PYTHON.is_file() and all((KITTEN_MINI_DIR / name).is_file() for name in (
            "config.json", "kitten_tts_mini_v0_8.onnx", "voices.npz",
        ))
    assets = KITTEN_CACHE / "KittenML/kitten-tts-2" / KITTEN_REVISION
    return KITTEN_PYTHON.is_file() and all((assets / name).is_file() for name in (
        "config.json", "cpp/model-tq2_1.gguf", f"cpp/{KITTEN_DECODER}/decoder.pt",
        f"cpp/{KITTEN_DECODER}/voices.json",
    ))


def _omnivoice_ready():
    return OMNIVOICE_PYTHON.is_file() and OMNIVOICE_REFERENCE.is_file() and all(
        (OMNIVOICE_DIR / name).is_file() for name in (
            "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
            "audio_tokenizer/config.json", "audio_tokenizer/model.safetensors",
            "audio_tokenizer/preprocessor_config.json"))


def _synthesize_omnivoice(text, lang, output, task_id):
    if not _omnivoice_ready():
        raise RuntimeError("OmniVoice environment, model or voice reference missing; run host/setup-omnivoice.sh")
    if task_id:
        TASKS.start(task_id, "tts", engine="OmniVoice CPU")
    while not OMNIVOICE_LOCK.acquire(timeout=.2):
        TASKS.check(task_id)
    try:
        TASKS.check(task_id)
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


def _synthesize_kitten(text, lang, output, task_id):
    if not _kitten_ready():
        setup = "host/setup-kitten.sh mini" if TTS_BACKEND == "kitten-mini" else "host/setup-kitten.sh"
        raise RuntimeError(f"KittenTTS environment or cached model missing; run {setup}")
    if task_id:
        TASKS.start(task_id, "tts", engine="Kitten TTS Mini 80M" if TTS_BACKEND == "kitten-mini" else "Kitten TTS 2")
    while not KITTEN_LOCK.acquire(timeout=.2):
        TASKS.check(task_id)
    try:
        TASKS.check(task_id)
        result = _run([
            KITTEN_MINI_PYTHON if TTS_BACKEND == "kitten-mini" else KITTEN_PYTHON,
            Path(__file__).with_name("kitten_tts.py"),
            "--family", "mini" if TTS_BACKEND == "kitten-mini" else "2",
            "--cache-dir", KITTEN_MINI_DIR if TTS_BACKEND == "kitten-mini" else KITTEN_CACHE,
            "--text", text, "--output", output,
            "--voice", KITTEN_VOICE_RU if lang == "ru" else KITTEN_VOICE_EN,
            "--lang", lang,
            "--decoder", KITTEN_DECODER, "--threads", str(KITTEN_THREADS),
            "--decoder-threads", str(KITTEN_DECODER_THREADS),
        ], timeout=180, task_id=task_id)
        if result.returncode or not output.is_file():
            detail = result.stderr.decode(errors="replace").strip()[-4000:]
            raise RuntimeError(detail or "KittenTTS generation failed")
        report = json.loads(result.stdout.decode().strip().splitlines()[-1])
        if task_id:
            TASKS.start(task_id, "tts", metrics=report)
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        KITTEN_LOCK.release()


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
    if TTS_BACKEND == "omnivoice":
        _synthesize_omnivoice(text, lang, output, task_id)
        output.chmod(0o644)
        return output
    if TTS_BACKEND == "kitten" or (TTS_BACKEND == "kitten-mini" and lang == "en"):
        _synthesize_kitten(text, lang, output, task_id)
        output.chmod(0o644)
        return output
    if TTS_BACKEND not in {"pocket", "kitten-mini"}:
        raise RuntimeError(f"Unknown TTS_BACKEND: {TTS_BACKEND}")
    if task_id and TTS_BACKEND == "kitten-mini":
        TASKS.start(task_id, "tts", engine="Pocket TTS")
    if lang == "en":
        cmd = [POCKET_BIN, "generate", "--language", "english_7m_2026-10",
               "--text", text, "--output-path", output, "-q"]
    elif lang == "ru":
        if not RU_CONFIG.is_file() or not RU_VOICE.is_file():
            raise RuntimeError("Russian TTS model or voice state is missing")
        cmd = [POCKET_BIN, "generate", "--config", RU_CONFIG, "--voice", RU_VOICE,
               "--max-tokens", "100", "--quantize", "--text", text,
               "--output-path", output, "-q"]
    result = _run(cmd, timeout=180, task_id=task_id)
    if result.returncode or not output.is_file():
        output.unlink(missing_ok=True)
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "TTS generation failed")
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
                "tts": _omnivoice_ready() if TTS_BACKEND == "omnivoice" else _kitten_ready() if TTS_BACKEND in {"kitten", "kitten-mini"} else POCKET_BIN.is_file(),
                "tts_backend": TTS_BACKEND,
                "tts_backend_supported": TTS_BACKEND in {"omnivoice", "pocket", "kitten", "kitten-mini"},
                "russian_tts": _omnivoice_ready() if TTS_BACKEND == "omnivoice" else _kitten_ready() if TTS_BACKEND == "kitten" else POCKET_BIN.is_file() and RU_CONFIG.is_file() and RU_VOICE.is_file(),
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
                "tts_en": {"omnivoice": OMNIVOICE_MODEL_NAME, "kitten": "KittenML/kitten-tts-2", "kitten-mini": "KittenML/kitten-tts-mini-0.8"}.get(TTS_BACKEND, "english_7m_2026-10"),
                "tts_ru": OMNIVOICE_MODEL_NAME if TTS_BACKEND == "omnivoice" else "KittenML/kitten-tts-2" if TTS_BACKEND == "kitten" else str(RU_CONFIG),
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
