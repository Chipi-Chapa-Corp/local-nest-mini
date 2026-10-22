import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded, TrailersReceived

from app import s3_relay, s3_server
from app.native import AUDIO_FIELD, _field, _frame


class StreamingRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [
            patch.object(s3_relay, "DATA_DIR", Path(self.temp.name)),
            patch.object(s3_server, "DATA_DIR", Path(self.temp.name)),
            patch.object(s3_relay, "log"), patch.object(s3_server, "log"),
            patch.object(s3_relay, "_finish_output"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.google_request = bytearray()
        self.control = _frame(_field(29737259, _field(1, 0)))
        self.google_tail = _frame(_field(AUDIO_FIELD, _field(1, b"OggS_google_audio"))) + _frame(_field(1, 1))
        self.settings = False
        self.fixture_tasks = []
        self.upstream = await asyncio.start_server(self.google, "127.0.0.1", 0)
        upstream_port = self.upstream.sockets[0].getsockname()[1]
        self.relay = s3_relay.RelayServer("127.0.0.1", upstream_port, upstream_tls=False)
        self.listener = await asyncio.start_server(self.relay.handle, "127.0.0.1", 0)
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.listener.sockets[0].getsockname()[1])
        self.client = H2Connection(H2Configuration(client_side=True))
        self.client.initiate_connection()
        self.client.send_headers(1, [
            (":method", "POST"), (":scheme", "https"),
            (":authority", s3_relay.S3_HOST),
            (":path", "/speech.s3.S3StubbyClientConnectorService/S3Bidi"),
            ("content-type", "application/grpc"),
        ])
        self.writer.write(self.client.data_to_send())

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.listener.close()
        self.upstream.close()
        await self.listener.wait_closed()
        await self.upstream.wait_closed()
        await asyncio.sleep(0.02)
        for task in self.fixture_tasks:
            task.cancel()
        await asyncio.gather(*self.fixture_tasks, return_exceptions=True)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    async def google(self, reader, writer):
        peer = s3_relay.Peer(reader, writer, client_side=False)
        peer.conn.initiate_connection()
        await peer.flush()
        sent_control = False

        async def handle(event):
            nonlocal sent_control
            if isinstance(event, RequestReceived):
                await peer.headers(1, [(":status", "200"), ("content-type", "application/grpc")])
            elif isinstance(event, DataReceived):
                self.google_request.extend(event.data)
                if not sent_control:
                    await peer.data(1, self.control)
                    sent_control = True
            elif isinstance(event, StreamEnded):
                if not self.settings:
                    # Split inside both a gRPC header and its audio payload.
                    for start, end in [(0, 2), (2, 11), (11, len(self.google_tail))]:
                        await peer.data(1, self.google_tail[start:end])
                await peer.headers(1, [("grpc-status", "0")], end_stream=True)
        task = asyncio.create_task(peer.read(handle))
        self.fixture_tasks.append(task)
        try:
            await task
        finally:
            writer.close()
            await writer.wait_closed()

    async def read_events(self):
        data = await asyncio.wait_for(self.reader.read(65536), 3)
        self.assertTrue(data)
        events = self.client.receive_data(data)
        for event in events:
            if isinstance(event, DataReceived):
                self.client.acknowledge_received_data(event.flow_controlled_length, 1)
        self.writer.write(self.client.data_to_send())
        return events

    async def test_preserves_interactive_capture_and_replaces_only_speech(self):
        initial = _frame(_field(1, b"assistant"))
        self.client.send_data(1, initial)
        self.writer.write(self.client.data_to_send())
        body = bytearray()
        while not body:
            for event in await self.read_events():
                if isinstance(event, DataReceived):
                    body.extend(event.data)
        # The microphone upload starts only after Google's live control reply.
        self.assertEqual(body, self.control)
        audio = b"fLaC" + bytes(20)
        utterance = _frame(_field(293101, _field(1, audio))) + _frame(_field(3, 1))
        replacement = bytes(range(256)) * 700  # larger than an HTTP/2 window
        with patch.object(s3_relay, "synthesize_query", return_value=(b"local_ogg", {"response": "local"})) as synth, \
             patch.object(s3_relay, "build_response", return_value=self.control + replacement) as build:
            # Deliberately split a gRPC header across incoming HTTP/2 frames.
            self.client.send_data(1, utterance[:3])
            self.client.send_data(1, utterance[3:], end_stream=True)
            self.writer.write(self.client.data_to_send())
            ended = False
            while not ended:
                for event in await self.read_events():
                    if isinstance(event, DataReceived):
                        body.extend(event.data)
                    elif isinstance(event, TrailersReceived):
                        self.assertEqual(dict(event.headers)[b"grpc-status"], b"0")
                    elif isinstance(event, StreamEnded):
                        ended = True
            self.assertEqual(body, self.control + replacement)
            self.assertEqual(self.google_request, initial + utterance)
            self.assertEqual(synth.call_args.args[0], audio)
            build.assert_called_once_with(self.control + self.google_tail, b"local_ogg")
        captures = list(Path(self.temp.name).glob("streams/*-req.bin"))
        self.assertEqual(captures[0].read_bytes(), initial + utterance)

    async def test_settings_stream_passes_through_without_model(self):
        self.settings = True
        initial = _frame(_field(1, b"assistant-settings")) + _frame(_field(3, 1))
        with patch.object(s3_relay, "synthesize_query") as synth:
            self.client.send_data(1, initial, end_stream=True)
            self.writer.write(self.client.data_to_send())
            body = bytearray()
            ended = False
            while not ended:
                for event in await self.read_events():
                    if isinstance(event, DataReceived):
                        body.extend(event.data)
                    elif isinstance(event, StreamEnded):
                        ended = True
            synth.assert_not_called()
        self.assertEqual(body, self.control)
        self.assertEqual(self.google_request, initial)

    async def test_failed_local_processing_reports_error_and_suppresses_google_audio(self):
        request = (_frame(_field(1, b"assistant"))
                   + _frame(_field(293101, _field(1, b"fLaC" + bytes(20))))
                   + _frame(_field(3, 1)))
        with patch.object(s3_relay, "synthesize_query", side_effect=RuntimeError("test failure")):
            self.client.send_data(1, request, end_stream=True)
            self.writer.write(self.client.data_to_send())
            body = bytearray()
            ended = False
            status = None
            while not ended:
                for event in await self.read_events():
                    if isinstance(event, DataReceived):
                        body.extend(event.data)
                    elif isinstance(event, TrailersReceived):
                        status = dict(event.headers)[b"grpc-status"]
                    elif isinstance(event, StreamEnded):
                        ended = True
        self.assertEqual(status, b"13")
        self.assertEqual(body, self.control)


if __name__ == "__main__":
    unittest.main()
