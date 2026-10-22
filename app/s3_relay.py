"""Stream Nest S3 traffic unchanged upstream; replace returned speech locally.

The capture path mirrors the original nest-audit addon: append each request
chunk, then forward those exact bytes. Google still handles S3 setup/control.
Only speech output is held for replacement; preceding control frames remain
live in both directions.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import time

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated, DataReceived, RequestReceived, ResponseReceived,
    StreamEnded, StreamReset, TrailersReceived, WindowUpdated,
)

from app.native import AUDIO_FIELD, _fields, build_response
from app.s3_audio import extract_flac_segments, extract_query_audio
from app.s3_server import (
    DATA_DIR, MAX_BODY, S3_HOST, S3_PORT, Stream, _finish_output,
    log, synthesize_query, tls_context, utterance_complete,
)


class Peer:
    def __init__(self, reader, writer, *, client_side):
        self.reader = reader
        self.writer = writer
        self.conn = H2Connection(H2Configuration(
            client_side=client_side, header_encoding="utf-8",
            validate_inbound_headers=False, validate_outbound_headers=False))
        self.window = asyncio.Event()
        self.closed = False

    async def flush(self):
        data = self.conn.data_to_send()
        if data:
            self.writer.write(data)
            await self.writer.drain()

    async def data(self, stream_id, data):
        offset = 0
        while offset < len(data):
            if self.closed:
                raise ConnectionError("HTTP/2 peer closed")
            available = self.conn.local_flow_control_window(stream_id)
            if available <= 0:
                self.window.clear()
                await self.window.wait()
                continue
            size = min(available, self.conn.max_outbound_frame_size, len(data) - offset)
            self.conn.send_data(stream_id, data[offset:offset + size])
            offset += size
            await self.flush()

    async def headers(self, stream_id, headers, *, end_stream=False):
        self.conn.send_headers(stream_id, headers, end_stream=end_stream)
        await self.flush()

    async def read(self, handler):
        try:
            while data := await self.reader.read(65536):
                for event in self.conn.receive_data(data):
                    if isinstance(event, DataReceived):
                        self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, WindowUpdated):
                        self.window.set()
                    elif isinstance(event, ConnectionTerminated):
                        return
                    await handler(event)
                await self.flush()
        finally:
            self.closed = True
            self.window.set()


class RelayStream(Stream):
    def __init__(self, stream_id, headers):
        super().__init__(stream_id, headers, time.time())
        self.requests = asyncio.Queue()
        self.responses = asyncio.Queue()
        self.request_done = asyncio.Event()
        self.response = bytearray()
        self.response_offset = 0
        self.forwarded = bytearray()
        self.held = False
        self.local_job = None
        self.local_error = None
        self.response_trailers = []
        self.finished = False
        self.workers = []


class RelayServer:
    def __init__(self, upstream_host=S3_HOST, upstream_port=443, *, upstream_tls=True):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_tls = upstream_tls

    async def handle(self, reader, writer):
        tasks = set()
        streams = {}
        changed = asyncio.Event()
        downstream = Peer(reader, writer, client_side=False)
        upstream = None

        def spawn(coro):
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(lambda task: changed.set())
            return task

        try:
            context = ssl.create_default_context() if self.upstream_tls else None
            if context:
                context.set_alpn_protocols(["h2"])
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(
                self.upstream_host, self.upstream_port, ssl=context,
                server_hostname=S3_HOST if context else None), 15)
            if context and up_writer.get_extra_info("ssl_object").selected_alpn_protocol() != "h2":
                up_writer.close()
                raise ConnectionError("S3 upstream did not negotiate HTTP/2")
            upstream = Peer(up_reader, up_writer, client_side=True)
            for peer in (downstream, upstream):
                peer.conn.initiate_connection()
                await peer.flush()

            def start_local(state):
                if state.local_job:
                    return
                segments = extract_flac_segments(bytes(state.body))
                log("s3_segments", flow=state.flow_id, bytes=len(state.body),
                    count=len(segments), sizes=[len(x) for x in segments])
                if segments:
                    async def run():
                        try:
                            return await asyncio.to_thread(synthesize_query,
                                extract_query_audio(bytes(state.body)), state.flow_id, len(state.body),
                                [len(x) for x in segments], state.started)
                        except Exception as exc:
                            state.local_error = f"{type(exc).__name__}: {exc}"
                            log("s3_query_error", flow=state.flow_id, error=state.local_error)
                            return None
                    state.local_job = spawn(run())

            async def from_nest(event):
                if isinstance(event, RequestReceived):
                    state = RelayStream(event.stream_id, dict(event.headers))
                    state.capture.parent.mkdir(parents=True, exist_ok=True)
                    streams[event.stream_id] = state
                    log("s3_request", flow=state.flow_id, stream=event.stream_id,
                        path=state.headers.get(":path", ""), transport="relay")
                    await upstream.headers(event.stream_id, event.headers)
                    state.workers = [spawn(self.forward_request(state, upstream)),
                                     spawn(self.forward_response(state, downstream))]
                elif isinstance(event, DataReceived):
                    state = streams[event.stream_id]
                    if len(state.body) + len(event.data) > MAX_BODY:
                        raise ValueError("S3 request exceeds capture limit")
                    state.receive(event.data)
                    state.requests.put_nowait(("data", event.data))
                    if utterance_complete(state.body):
                        start_local(state)
                elif isinstance(event, TrailersReceived):
                    streams[event.stream_id].requests.put_nowait(("trailers", event.headers))
                elif isinstance(event, StreamEnded):
                    state = streams[event.stream_id]
                    start_local(state)
                    state.request_done.set()
                    state.requests.put_nowait(("end", None))
                    log("s3_upload_complete", flow=state.flow_id, bytes=len(state.body),
                        frames=state.frame_count, service=state.service)
                elif isinstance(event, StreamReset):
                    state = streams.get(event.stream_id)
                    if state:
                        state.finished = True
                        state.request_done.set()
                        for task in state.workers + ([state.local_job] if state.local_job else []):
                            task.cancel()
                        log("s3_reset", flow=state.flow_id, code=event.error_code,
                            bytes=len(state.body), frames=state.frame_count, service=state.service)
                    upstream.conn.reset_stream(event.stream_id, error_code=event.error_code)
                    await upstream.flush()

            async def from_google(event):
                state = streams.get(getattr(event, "stream_id", None))
                if not state or state.finished:
                    return
                if isinstance(event, ResponseReceived):
                    state.responses.put_nowait(("headers", event.headers))
                elif isinstance(event, DataReceived):
                    if len(state.response) + len(event.data) > MAX_BODY:
                        raise ValueError("S3 response exceeds capture limit")
                    state.response.extend(event.data)
                    path = DATA_DIR / "streams" / f"{state.flow_id}-resp.bin"
                    with path.open("ab") as out:
                        out.write(event.data)
                    state.responses.put_nowait(("data", event.data))
                elif isinstance(event, TrailersReceived):
                    state.responses.put_nowait(("trailers", event.headers))
                elif isinstance(event, StreamEnded):
                    state.responses.put_nowait(("end", None))
                elif isinstance(event, StreamReset):
                    downstream.conn.reset_stream(event.stream_id, error_code=event.error_code)
                    await downstream.flush()
                    state.finished = True
                    state.request_done.set()
                    for task in state.workers + ([state.local_job] if state.local_job else []):
                        task.cancel()

            readers = [spawn(downstream.read(from_nest)), spawn(upstream.read(from_google))]
            while True:
                await changed.wait()
                changed.clear()
                done = [task for task in tasks if task.done()]
                for task in done:
                    tasks.discard(task)
                    if not task.cancelled():
                        task.result()
                    if task in readers:
                        return
                for stream_id, state in list(streams.items()):
                    if state.finished and state.request_done.is_set():
                        streams.pop(stream_id, None)
        except Exception as exc:
            log("s3_connection_error", error=f"{type(exc).__name__}: {exc}", transport="relay")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for peer in (downstream, upstream):
                if peer:
                    peer.writer.close()
                    try:
                        await peer.writer.wait_closed()
                    except Exception:
                        pass

    async def forward_request(self, state, upstream):
        trailers_sent = False
        while True:
            kind, data = await state.requests.get()
            if kind == "data":
                await upstream.data(state.stream_id, data)
            elif kind == "trailers":
                await upstream.headers(state.stream_id, data, end_stream=True)
                trailers_sent = True
            elif kind == "end":
                if not trailers_sent:
                    upstream.conn.end_stream(state.stream_id)
                    await upstream.flush()
                return

    async def forward_response(self, state, downstream):
        while True:
            kind, data = await state.responses.get()
            if kind == "headers":
                # Output size can change. All other setup/control headers survive.
                headers = [(k, v) for k, v in data if k not in {"content-length", "alt-svc"}]
                headers.append(("alt-svc", "clear"))
                await downstream.headers(state.stream_id, headers)
            elif kind == "data":
                # Google may split a gRPC message across arbitrary HTTP/2 frames.
                while state.response_offset + 5 <= len(state.response):
                    offset = state.response_offset
                    size = int.from_bytes(state.response[offset + 1:offset + 5], "big")
                    end = offset + 5 + size
                    if end > len(state.response):
                        break
                    frame = bytes(state.response[offset:end])
                    fields = _fields(frame[5:])
                    if any(field == AUDIO_FIELD for field, wire, value in fields):
                        state.held = True
                    if not state.held:
                        await downstream.data(state.stream_id, frame)
                        state.forwarded.extend(frame)
                    state.response_offset = end
            elif kind == "trailers":
                state.response_trailers = data
            elif kind == "end":
                if state.held:
                    await self.finish_native(state, downstream)
                else:
                    # Settings, enrollment, and other control streams are unchanged.
                    remaining = bytes(state.response[state.response_offset:])
                    if remaining:
                        await downstream.data(state.stream_id, remaining)
                    if state.response_trailers:
                        await downstream.headers(state.stream_id, state.response_trailers, end_stream=True)
                    else:
                        downstream.conn.end_stream(state.stream_id)
                        await downstream.flush()
                    if state.local_job:
                        await asyncio.shield(state.local_job)
                        error = state.local_error or "upstream response contained no speech audio to replace"
                        log("s3_no_upstream_audio", flow=state.flow_id, error=error)
                        await asyncio.to_thread(_finish_output, state.flow_id, 0, error)
                state.finished = True
                return

    async def finish_native(self, state, downstream):
        try:
            await asyncio.wait_for(state.request_done.wait(), 30)
            if not state.local_job:
                raise RuntimeError("upstream speech response has no captured microphone audio")
            prepared = await asyncio.wait_for(asyncio.shield(state.local_job), 180)
            if not prepared:
                raise RuntimeError(state.local_error or "no local transcript after removing wake word")
            ogg, answer = prepared
            # Use this live conversation's control metadata, not another turn's.
            native = build_response(bytes(state.response), ogg)
            if not native.startswith(state.forwarded):
                raise RuntimeError("native replacement would change already forwarded S3 control frames")
            tail = native[len(state.forwarded):]
            await downstream.data(state.stream_id, tail)
            await downstream.headers(state.stream_id, [("grpc-status", "0")], end_stream=True)
            log("s3_response", flow=state.flow_id, bytes=len(native),
                replaced_bytes=len(tail), text=answer.get("response", ""), transport="relay")
            await asyncio.to_thread(_finish_output, state.flow_id, len(native))
        except Exception as exc:
            log("s3_query_error", flow=state.flow_id, error=f"{type(exc).__name__}: {exc}")
            await downstream.headers(state.stream_id, [("grpc-status", "13")], end_stream=True)
            await asyncio.to_thread(_finish_output, state.flow_id, 0, f"{type(exc).__name__}: {exc}")


async def main():
    if os.environ.get("ALLOW_GOOGLE_UPSTREAM", "0") != "1":
        raise SystemExit("Google upstream is disabled; use the local app.s3_server endpoint")
    server = RelayServer()
    listener = await asyncio.start_server(server.handle, "127.0.0.1", S3_PORT, ssl=tls_context())
    log("s3_server_started", port=S3_PORT, transport="relay", upstream=S3_HOST)
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
