import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, StreamEnded, TrailersReceived

from app import s3_server
from app.native import _field, _frame, _frames, _fields


class S3TransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [
            patch.object(s3_server, "DATA_DIR", Path(self.temp.name)),
            patch.object(s3_server, "SILENCE", 0.03),
            patch.object(s3_server, "log"),
            patch.object(s3_server, "_finish_output"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.server = s3_server.Server()
        self.listener = await asyncio.start_server(self.server.handle, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self.client = H2Connection(H2Configuration(client_side=True))
        self.client.initiate_connection()
        self.client.send_headers(1, [
            (":method", "POST"), (":scheme", "https"),
            (":authority", s3_server.S3_HOST),
            (":path", "/speech.s3.S3StubbyClientConnectorService/S3Bidi"),
            ("content-type", "application/grpc"),
        ])
        self.writer.write(self.client.data_to_send())

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.listener.close()
        await self.listener.wait_closed()
        await asyncio.sleep(0.01)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    async def read_response(self):
        body = bytearray()
        trailers = {}
        while True:
            data = await asyncio.wait_for(self.reader.read(65536), 2)
            self.assertTrue(data, "server closed before gRPC trailers")
            ended = False
            for event in self.client.receive_data(data):
                if isinstance(event, DataReceived):
                    body.extend(event.data)
                    self.client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, TrailersReceived):
                    trailers = dict(event.headers)
                elif isinstance(event, StreamEnded):
                    ended = True
            self.writer.write(self.client.data_to_send())
            await self.writer.drain()
            if ended:
                return bytes(body), trailers

    async def test_bidi_response_exceeds_window_without_request_end(self):
        # An initial settings pause must not consume the voice request.
        self.client.send_data(1, _frame(_field(4, 0)))
        self.writer.write(self.client.data_to_send())
        await asyncio.sleep(0.08)
        audio = b"fLaC" + bytes(16)
        request = _frame(_field(293101, _field(1, audio))) + _frame(_field(3, 1))
        expected = bytes(range(256)) * 700
        with patch.object(s3_server, "_run_query", return_value=("test", expected, {})) as query:
            self.client.send_data(1, request, end_stream=False)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
            self.assertEqual(body, expected)
            self.assertEqual(trailers[b"grpc-status"], b"0")
            query.assert_called_once()
            self.assertEqual(query.call_args.args[0], audio)

    async def test_settings_only_request_gets_valid_empty_grpc_response(self):
        self.client.send_data(1, _frame(_field(4, 0)), end_stream=True)
        self.writer.write(self.client.data_to_send())
        body, trailers = await self.read_response()
        self.assertEqual(body, b"")
        self.assertEqual(trailers[b"grpc-status"], b"0")

    async def test_settings_reply_is_served_from_local_capture(self):
        request = _frame(_field(1, b"assistant-settings")) + _frame(_field(3, 1))
        reply = _frame(_field(119658398, _field(2, b""))) + _frame(_field(1, 1))
        captures = Path(self.temp.name) / "streams"
        captures.mkdir()
        (captures / "known-req.bin").write_bytes(request)
        (captures / "known-resp.bin").write_bytes(reply)
        with patch.object(s3_server, "_run_query") as query:
            self.client.send_data(1, request, end_stream=True)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertEqual(body, reply)
        self.assertEqual(trailers[b"grpc-status"], b"0")
        query.assert_not_called()

    async def test_model_failure_returns_grpc_error(self):
        request = _frame(_field(293101, _field(1, b"fLaC" + bytes(16))))
        with patch.object(s3_server, "_run_query", side_effect=RuntimeError("model failed")):
            self.client.send_data(1, request, end_stream=True)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertEqual(body, b"")
        self.assertEqual(trailers[b"grpc-status"], b"13")

    async def test_client_waits_for_s3_heartbeat_before_audio(self):
        initial = _field(1, b"assistant") + _field(29735924, _field(1, 1000))
        self.client.send_data(1, _frame(initial))
        self.writer.write(self.client.data_to_send())
        received = bytearray()
        while not any(f == 29737259 for frame in _frames(received) for f, w, v in _fields(frame)):
            data = await asyncio.wait_for(self.reader.read(65536), 2)
            for event in self.client.receive_data(data):
                if isinstance(event, DataReceived):
                    received.extend(event.data)
                    self.client.acknowledge_received_data(event.flow_controlled_length, 1)
            self.writer.write(self.client.data_to_send())
        fields = [field for frame in _frames(received) for field in _fields(frame)]
        self.assertTrue(any(f == 1253625 for f, w, v in fields))
        heartbeat = next(v for f, w, v in fields if f == 29737259 and w == 2)
        self.assertEqual(_fields(heartbeat)[0], (1, 0, 0))
        audio = b"fLaC" + bytes(16)
        request = _frame(_field(293101, _field(1, audio))) + _frame(_field(3, 1))
        with patch.object(s3_server, "_run_query", return_value=("test", b"reply", {})):
            self.client.send_data(1, request)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertEqual(body, b"reply")
        self.assertEqual(trailers[b"grpc-status"], b"0")
        captures = list(Path(self.temp.name).glob("streams/*-req.bin"))
        self.assertEqual(captures[0].read_bytes(), _frame(initial) + request)

    def stop_events(self, body):
        events = []
        body = bytes(body)
        if body.endswith(b"native-reply"):
            body = body[:-len(b"native-reply")]
        for frame in _frames(body):
            for field, wire, value in _fields(frame):
                if field == 1253625 and wire == 2:
                    for subfield, subwire, control in _fields(value):
                        if subfield == 2 and subwire == 2:
                            events.append(_fields(control)[0][2])
        return events

    async def test_endpoint_stops_recording_and_replies_while_upload_is_open(self):
        audio = _frame(_field(293101, _field(1, b"fLaC" + bytes(16))))
        # The no-new-data fallback must not be what ends this turn.
        with patch.object(s3_server, "SILENCE", 30), \
             patch.object(s3_server.endpoint, "decode_pcm", return_value=b""), \
             patch.object(s3_server.endpoint, "decide",
                          return_value=(s3_server.endpoint.SPEECH_END, 1.5)), \
             patch.object(s3_server, "_run_query", return_value=("test", b"native-reply", {})) as query:
            # The device keeps its upload open, as the live Nest does.
            self.client.send_data(1, _frame(_field(1, b"assistant")) + audio)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertEqual(self.stop_events(body)[-1], 3)
        self.assertTrue(body.endswith(b"native-reply"))
        self.assertLess(body.rindex(_field(1, 3)), body.rindex(b"native-reply"))
        self.assertEqual(trailers[b"grpc-status"], b"0")
        query.assert_called_once()
        self.assertTrue(query.call_args.args[-1], "turn without hotword audio is a follow-on")

    async def test_follow_on_without_speech_closes_quietly(self):
        template = Path(self.temp.name) / "nospeech.bin"
        template.write_bytes(_frame(_field(124211619, _field(1, 1))))
        audio = _frame(_field(293101, _field(1, b"fLaC" + bytes(16))))
        with patch.object(s3_server, "NO_SPEECH_TEMPLATE", template), \
             patch.object(s3_server, "SILENCE", 30), \
             patch.object(s3_server.endpoint, "decode_pcm", return_value=b""), \
             patch.object(s3_server.endpoint, "decide",
                          return_value=(s3_server.endpoint.NO_SPEECH_DETECTED, 5.0)), \
             patch.object(s3_server, "_run_query") as query:
            self.client.send_data(1, _frame(_field(1, b"assistant")) + audio)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertTrue(body.endswith(template.read_bytes()))
        self.assertEqual(trailers[b"grpc-status"], b"0")
        query.assert_not_called()

    async def test_empty_transcript_closes_quietly_instead_of_empty_reply(self):
        template = Path(self.temp.name) / "nospeech.bin"
        template.write_bytes(_frame(_field(124211619, _field(1, 1))))
        request = (_frame(_field(1, b"assistant"))
                   + _frame(_field(62724734, _field(1, b"fLaCwake")))
                   + _frame(_field(293101, _field(1, b"fLaC" + bytes(16)))) + _frame(_field(3, 1)))
        with patch.object(s3_server, "NO_SPEECH_TEMPLATE", template), \
             patch.object(s3_server, "_run_query", return_value=None) as query:
            self.client.send_data(1, request)
            self.writer.write(self.client.data_to_send())
            body, trailers = await self.read_response()
        self.assertTrue(body.endswith(template.read_bytes()))
        self.assertEqual(trailers[b"grpc-status"], b"0")
        self.assertFalse(query.call_args.args[-1], "hotword turn is not a follow-on")


if __name__ == "__main__":
    unittest.main()
