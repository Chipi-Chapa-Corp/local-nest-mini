"""Local HTTP/2 gRPC endpoint for Nest S3Bidi voice requests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RequestReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)

from app.native import build_response, _frames, _fields, _field, _frame
from app import endpoint
from app.s3_audio import (extract_flac_segments, extract_query_audio, has_wake_audio,
                          is_noise_transcript, strip_wake_word)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
TEMPLATE = Path(os.environ.get("NATIVE_TEMPLATE", str(DATA_DIR / "native-template.bin")))
# Google's reply to a follow-on turn without speech (owned-device capture
# 63e8192c, frames 2 and 14-23): end-of-utterance, empty result, dialog end
# with "HorizontalPunt". It closes the turn quietly instead of an error.
NO_SPEECH_TEMPLATE = Path(os.environ.get("NATIVE_NOSPEECH_TEMPLATE",
                                         str(DATA_DIR / "native-nospeech.bin")))
ASSISTANT_URL = os.environ.get("ASSISTANT_URL", "http://127.0.0.1:8099")
ASSISTANT_LANG = os.environ.get("ASSISTANT_LANG", "auto")
S3_HOST = "assistant-s3-pa.googleapis.com"
S3_PORT = int(os.environ.get("S3_SERVER_PORT", "18444"))
SILENCE = float(os.environ.get("S3_SILENCE", "0.9"))
ENDPOINT_INTERVAL = 0.2
MAX_BODY = 8 * 1024 * 1024
MODEL_LOCK = threading.Lock()


def tls_context():
    """Use the same lab certificate chain as the existing proxy."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mitmproxy import certs

    store = certs.CertStore.from_store(
        Path(os.environ.get("PROXY_CONFIG", "proxy-config")), "mitmproxy", 2048)
    store.default_privatekey = certs.load_pem_private_key(certs.EMUDHRA_CHAIN_PEM, None)
    chain = [certs.Cert(c) for c in x509.load_pem_x509_certificates(certs.EMUDHRA_CHAIN_PEM)]
    chain.insert(0, chain.pop())
    store.default_ca = chain[0]
    entry = store.get_cert(S3_HOST, [x509.DNSName(S3_HOST)])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory(prefix="nest-s3-tls-") as temp:
        pem = Path(temp) / "server.pem"
        pem.touch(mode=0o600)
        pem.write_bytes(entry.privatekey.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()) + entry.cert.to_pem()
            + b"".join(cert.to_pem() for cert in chain))
        context.load_cert_chain(pem)
    context.set_alpn_protocols(["h2"])
    return context


def utterance_complete(body):
    try:
        return any((3, 0, 1) in _fields(frame) for frame in _frames(body))
    except ValueError:
        return False


def recognition_init(fields):
    """Mirror the initial recognition-control reply in the working capture."""
    locale = "en-CA"
    for field, wire, value in fields:
        if field == 293000 and wire == 2:
            for subfield, subwire, subvalue in _fields(value):
                if subfield == 2 and subwire == 2:
                    for key, kind, language in _fields(subvalue):
                        if key == 1 and kind == 2:
                            locale = language.decode("ascii", errors="replace")
    # Captured control extension: event { type: 0, time: 0, ...: 0 }, locales.
    message = _field(2, _field(1, 0) + _field(2, 0) + _field(3, 0))
    message += _field(3, locale.lower().encode()) + _field(4, locale.encode())
    return _frame(_field(1253625, message))


def recording_stop(elapsed_us):
    """End-of-utterance event (3); event 2 is end-of-audio, not a stop command.

    The published Assistant library's ProcessEndpointerEvent dispatches 1/3
    to onSpeechEnd and ignores 2. The working Nest capture sends 3, 1, then 2.
    See data/research/assistant-library/PROTOCOL.md for the static analysis.
    Live check 19:09:04: the Nest stopped uploading immediately after this.
    """
    event = _field(1, 3) + _field(2, elapsed_us) + _field(3, elapsed_us)
    return _frame(_field(1253625, _field(2, event)))


def control_reply(service):
    """Use an existing owned-device control capture, without any cloud call."""
    if service not in {"assistant-settings", "enrollment-fetcher", "assistant-voiceless"}:
        return b""
    for path in sorted((DATA_DIR / "streams").glob("*-req.bin")):
        response = path.with_name(path.name.replace("-req.bin", "-resp.bin"))
        if not response.is_file() or response.stat().st_size > 65536:
            continue
        try:
            with path.open("rb") as source:
                header = source.read(5)
                if len(header) != 5:
                    continue
                size = int.from_bytes(header[1:], "big")
                if size > 65536:
                    continue
                fields = _fields(source.read(size))
            if (1, 2, service.encode()) in fields:
                return response.read_bytes()
        except (OSError, ValueError):
            continue
    return _frame(_field(1, 1))


def log(kind, **fields):
    line = json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), kind=kind, **fields),
                      ensure_ascii=False)
    try:
        (DATA_DIR / "logs" / "proxy-events.jsonl").parent.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "logs" / "proxy-events.jsonl").open("a") as out:
            out.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


class TLSHandshakeLog(logging.Handler):
    """Expose pre-HTTP TLS failures, which asyncio otherwise suppresses."""
    def emit(self, record):
        if record.exc_info and "SSL handshake" in record.getMessage():
            exc = record.exc_info[1]
            log("s3_tls_error", error=f"{type(exc).__name__}: {exc}")


def _has_cyrillic(text):
    return any("\u0400" <= char <= "\u04ff" for char in text)


def _reply_lang(text):
    if ASSISTANT_LANG != "auto":
        return ASSISTANT_LANG
    return "ru" if _has_cyrillic(text) else "en"


def _assistant(path, body, content_type, task_id=None):
    if task_id:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}task_id={urllib.parse.quote(task_id)}"
    request = urllib.request.Request(
        ASSISTANT_URL + path,
        data=body,
        headers={"content-type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read(16384) or b"{}").get("error", str(exc))
        raise RuntimeError(f"local {path.split('?')[0]}: {detail}") from exc


def _run_query(audio, task_id, byte_count, segment_sizes, received, follow_on=False):
    # A disconnected client can cancel the awaiting coroutine, but a worker
    # thread continues running. Keep serialization inside the worker as well.
    with MODEL_LOCK:
        return _process_query(audio, task_id, byte_count, segment_sizes, received, follow_on)


def _process_query(audio, task_id, byte_count, segment_sizes, received, follow_on=False):
    prepared = _prepare_query(audio, task_id, byte_count, segment_sizes, received, follow_on)
    if not prepared:
        return None
    ogg, result = prepared
    native = build_response(TEMPLATE.read_bytes(), ogg)
    log("native_built", task_id=task_id, bytes=len(native))
    return task_id, native, result


def synthesize_query(audio, task_id, byte_count, segment_sizes, received):
    with MODEL_LOCK:
        return _prepare_query(audio, task_id, byte_count, segment_sizes, received)


def _prepare_query(audio, task_id, byte_count, segment_sizes, received, follow_on=False):
    start = {
        "task_id": task_id,
        "flow": task_id,
        "bytes": byte_count,
        "segments": segment_sizes,
        "received": received,
    }
    _assistant("/task/start", json.dumps(start).encode(), "application/json")
    text = _assistant(f"/transcribe?lang={ASSISTANT_LANG}", audio,
                      "application/octet-stream", task_id).get("text", "")
    query = strip_wake_word(text)
    if not query:
        log("local_wake_only", text=text, task_id=task_id)
        _finish_output(task_id, 0, result="no speech")
        return None
    if follow_on and is_noise_transcript(query):
        log("local_follow_on_noise", text=text, task_id=task_id)
        _finish_output(task_id, 0, result="no speech")
        return None
    result = _assistant("/answer", json.dumps({
        "text": query,
        "lang": _reply_lang(query),
        "play": False,
        "task_id": task_id,
    }).encode(), "application/json", task_id)
    ogg = Path(result["audio_ogg_path"]).read_bytes()
    return ogg, result


def _finish_output(task_id, size, error=None, **extra):
    payload = {"task_id": task_id, "transport": "native", "bytes": size, **extra}
    if error:
        payload["error"] = error
    try:
        _assistant("/task/output", json.dumps(payload).encode(), "application/json", task_id)
    except Exception as exc:
        log("task_output_error", task_id=task_id, error=f"{type(exc).__name__}: {exc}")


def _cancel_query(task_id):
    try:
        _assistant("/task/cancel", json.dumps({"task_id": task_id}).encode(),
                   "application/json")
    except Exception as exc:
        log("task_cancel_error", task_id=task_id, error=f"{type(exc).__name__}: {exc}")


class Stream:
    def __init__(self, stream_id, headers, started):
        self.stream_id = stream_id
        self.headers = headers
        self.started = started
        self.body = bytearray()
        self.task = None
        self.done = False
        self.closed = False
        self.query_started = False
        self.delivered = False
        self.response_sent = False
        self.debounce_task = None
        self.flow_id = uuid.uuid4().hex
        self.frame_offset = 0
        self.frame_count = 0
        self.service = None
        self.heartbeat_task = None
        self.recognition_initialized = False
        self.endpoint_task = None
        self.capture = DATA_DIR / "streams" / f"{self.flow_id}-req.bin"

    def receive(self, data):
        self.body.extend(data)
        self.capture.parent.mkdir(parents=True, exist_ok=True)
        with self.capture.open("ab") as out:
            os.fchmod(out.fileno(), 0o600)
            if os.geteuid() == 0:
                owner = DATA_DIR.stat()
                os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
            out.write(data)
        frames = []
        while self.frame_offset + 5 <= len(self.body):
            size = int.from_bytes(self.body[self.frame_offset + 1:self.frame_offset + 5], "big")
            end = self.frame_offset + 5 + size
            if end > len(self.body):
                break
            payload = bytes(self.body[self.frame_offset + 5:end])
            fields = _fields(payload)
            frames.append(fields)
            self.frame_offset = end
            self.frame_count += 1
            for field, wire, value in fields:
                if field == 1 and wire == 2:
                    self.service = value.decode("utf-8", errors="replace")[:80]
            if self.frame_count <= 3 or self.frame_count % 25 == 0:
                log("s3_frame", flow=self.flow_id, frame=self.frame_count,
                    service=self.service, bytes=len(self.body),
                    fields=[field for field, wire, value in fields])
        return frames


class Server:
    def __init__(self):
        self.connections = set()
        self.model_lock = asyncio.Lock()

    async def handle(self, reader, writer):
        tls = writer.get_extra_info("ssl_object")
        if tls:
            log("s3_tls_established", alpn=tls.selected_alpn_protocol(),
                version=tls.version())
        conn = H2Connection(H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        streams = {}
        window = asyncio.Event()
        self.connections.add(writer)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                events = conn.receive_data(data)
                for event in events:
                    if isinstance(event, RequestReceived):
                        headers = dict(event.headers)
                        streams[event.stream_id] = Stream(event.stream_id, headers,
                                                          time.time())
                        state = streams[event.stream_id]
                        log("s3_request", stream=event.stream_id, flow=state.flow_id,
                            path=headers.get(":path", ""))
                        conn.send_headers(event.stream_id, [
                            (":status", "200"), ("content-type", "application/grpc"),
                            ("alt-svc", "clear")])
                        state.response_sent = True
                    elif isinstance(event, DataReceived):
                        state = streams.get(event.stream_id)
                        if not state:
                            continue
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        if state.done:
                            continue
                        if len(state.body) + len(event.data) > MAX_BODY:
                            state.done = True
                            self._trailers(conn, writer, state.stream_id, 8, "request too large")
                            continue
                        frames = state.receive(event.data)
                        if not state.endpoint_task and state.service == "assistant":
                            if any(field == 293101 for fields in frames for field, wire, value in fields):
                                state.endpoint_task = asyncio.create_task(
                                    self.endpoint(conn, writer, state, window))
                        if not state.recognition_initialized:
                            for fields in frames:
                                if (1, 2, b"assistant") in fields:
                                    await self._send_data(conn, writer, state.stream_id,
                                                          recognition_init(fields), window)
                                    state.recognition_initialized = True
                                    log("s3_recognition_init", flow=state.flow_id)
                                    break
                        if not state.heartbeat_task:
                            for fields in frames:
                                request = next((value for field, wire, value in fields
                                                if field == 29735924 and wire == 2), None)
                                if request is not None:
                                    interval_ms = next((value for field, wire, value in _fields(request)
                                                        if field == 1 and wire == 0), 1000)
                                    state.heartbeat_task = asyncio.create_task(
                                        self.heartbeats(conn, writer, state, window,
                                                        max(0.25, min(interval_ms / 1000, 10))))
                                    break
                        if state.debounce_task:
                            state.debounce_task.cancel()
                        if utterance_complete(state.body):
                            state.task = asyncio.create_task(self.respond(conn, writer, state, window))
                        else:
                            state.debounce_task = asyncio.create_task(self.debounce(conn, writer, state, window))
                    elif isinstance(event, StreamEnded):
                        state = streams.get(event.stream_id)
                        if not state:
                            continue
                        if state.debounce_task:
                            state.debounce_task.cancel()
                        if not state.task:
                            state.task = asyncio.create_task(self.respond(conn, writer, state, window))
                    elif isinstance(event, WindowUpdated):
                        window.set()
                    elif isinstance(event, StreamReset):
                        state = streams.pop(event.stream_id, None)
                        if state:
                            for task in (state.task, state.debounce_task, state.heartbeat_task, state.endpoint_task):
                                if task:
                                    task.cancel()
                            log("s3_reset", flow=state.flow_id, code=event.error_code,
                                bytes=len(state.body), frames=state.frame_count, service=state.service)
                    elif isinstance(event, ConnectionTerminated):
                        return
                out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
        except Exception as exc:
            log("s3_connection_error", error=f"{type(exc).__name__}: {exc}")
        finally:
            for state in streams.values():
                for task in (state.task, state.debounce_task, state.heartbeat_task, state.endpoint_task):
                    if task:
                        task.cancel()
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def endpoint(self, conn, writer, state, window):
        """Tell the Nest to stop recording once the speaker has finished.

        Google does this server-side; the Nest otherwise keeps the mic open.
        """
        try:
            while not state.done:
                await asyncio.sleep(ENDPOINT_INTERVAL)
                audio = extract_query_audio(bytes(state.body))
                levels = endpoint.frame_levels(await asyncio.to_thread(endpoint.decode_pcm, audio))
                decision, at = endpoint.decide(levels)
                if not decision or state.done:
                    continue
                await self._send_data(conn, writer, state.stream_id,
                                      recording_stop(int(at * 1_000_000)), window)
                log("s3_endpoint", flow=state.flow_id, decision=decision,
                    speech_end=round(at, 2), audio=round(len(levels) / 10, 1),
                    bytes=len(state.body))
                if not state.task:
                    state.task = asyncio.create_task(self.respond(
                        conn, writer, state, window,
                        no_speech=decision == endpoint.NO_SPEECH_DETECTED))
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log("s3_endpoint_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")

    async def heartbeats(self, conn, writer, state, window, interval):
        """Honor the interval requested in the captured S3 heartbeat extension."""
        try:
            # The stream remains alive while STT/TTS is running, even after
            # recording has stopped. The heartbeat is not a recording event.
            while not state.closed:
                payload = _field(5, 0) + _field(29737259,
                    _field(1, 0) + _field(2, int(time.time() * 1000)))
                await self._send_data(conn, writer, state.stream_id, _frame(payload), window)
                log("s3_heartbeat", flow=state.flow_id)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log("s3_heartbeat_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")

    async def debounce(self, conn, writer, state, window):
        try:
            await asyncio.sleep(SILENCE)
        except asyncio.CancelledError:
            return
        if not state.done and not state.task and extract_flac_segments(bytes(state.body)):
            state.task = asyncio.create_task(self.respond(conn, writer, state, window))

    async def quiet_close(self, conn, writer, state, window, reason):
        """End a voice turn without speech the way Google does, not as an error."""
        try:
            reply = NO_SPEECH_TEMPLATE.read_bytes()
            await self._send_data(conn, writer, state.stream_id, reply, window)
        except Exception as exc:
            reply = b""
            log("s3_quiet_close_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")
        log("s3_quiet_close", flow=state.flow_id, reason=reason, bytes=len(reply))
        self._trailers(conn, writer, state.stream_id, 0, "")
        state.delivered = True

    async def respond(self, conn, writer, state, window, no_speech=False):
        if state.done:
            return
        state.done = True
        try:
            await self._respond(conn, writer, state, window, no_speech)
        except asyncio.CancelledError:
            if state.query_started and not state.delivered:
                await asyncio.to_thread(_cancel_query, state.flow_id)
            raise
        finally:
            state.closed = True
            if state.heartbeat_task:
                state.heartbeat_task.cancel()

    async def _respond(self, conn, writer, state, window, no_speech):
        body = bytes(state.body)
        if no_speech:
            await self.quiet_close(conn, writer, state, window, "no speech")
            return
        segments = extract_flac_segments(body)
        log("s3_segments", flow=state.flow_id, bytes=len(body), count=len(segments),
            sizes=[len(x) for x in segments])
        if not segments:
            log("s3_no_audio", flow=state.flow_id)
            reply = control_reply(state.service)
            if reply:
                await self._send_data(conn, writer, state.stream_id, reply, window)
                log("s3_control_reply", flow=state.flow_id, service=state.service, bytes=len(reply))
            self._trailers(conn, writer, state.stream_id, 0, "")
            return
        audio = extract_query_audio(body)
        if not audio:
            log('s3_no_query_audio', flow=state.flow_id)
            await self.quiet_close(conn, writer, state, window, "no query audio")
            return
        capture = DATA_DIR / "streams" / f"{state.flow_id}-req.bin"
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_bytes(body)
        try:
            async with self.model_lock:
                state.query_started = True
                result = await asyncio.to_thread(
                    _run_query, audio, state.flow_id, len(body),
                    [len(x) for x in segments], state.started, not has_wake_audio(body),
                )
        except Exception as exc:
            log("s3_query_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")
            self._trailers(conn, writer, state.stream_id, 13,
                           f"{type(exc).__name__}: {exc}")
            await asyncio.to_thread(_finish_output, state.flow_id, 0, f"{type(exc).__name__}: {exc}")
            return
        if not result:
            await self.quiet_close(conn, writer, state, window, "empty transcript")
            return
        task_id, native, answer = result
        try:
            await self._send_data(conn, writer, state.stream_id, native, window)
            conn.send_headers(state.stream_id, [("grpc-status", "0")], end_stream=True)
            writer.write(conn.data_to_send())
            await writer.drain()
            state.delivered = True
            log("s3_response", flow=task_id, bytes=len(native),
                text=answer.get("response", ""), audio_url=answer.get("audio_url", ""))
            await asyncio.to_thread(_finish_output, task_id, len(native))
        except Exception as exc:
            log("s3_send_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")
            await asyncio.to_thread(_finish_output, state.flow_id, 0, f"{type(exc).__name__}: {exc}")

    async def _send_data(self, conn, writer, stream_id, data, window):
        offset = 0
        while offset < len(data):
            available = conn.local_flow_control_window(stream_id)
            if available <= 0:
                window.clear()
                await window.wait()
                continue
            size = min(available, conn.max_outbound_frame_size, len(data) - offset)
            conn.send_data(stream_id, data[offset:offset + size], end_stream=False)
            offset += size
            writer.write(conn.data_to_send())
            await writer.drain()

    @staticmethod
    def _trailers(conn, writer, stream_id, status, message):
        headers = [("grpc-status", str(status))]
        if message:
            headers.append(("grpc-message", message.replace("\n", " ")))
        try:
            conn.send_headers(stream_id, headers, end_stream=True)
            writer.write(conn.data_to_send())
        except Exception:
            pass


async def main():
    if not TEMPLATE.is_file():
        raise SystemExit(f"missing native template: {TEMPLATE}")
    context = tls_context()
    asyncio.get_running_loop().set_debug(True)
    tls_logger = logging.getLogger("asyncio")
    tls_logger.setLevel(logging.DEBUG)
    tls_logger.addHandler(TLSHandshakeLog())
    server = Server()
    srv = await asyncio.start_server(server.handle, "127.0.0.1", S3_PORT, ssl=context)
    log("s3_server_started", port=S3_PORT, template=str(TEMPLATE))
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
