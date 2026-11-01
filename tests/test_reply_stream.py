"""Streaming boundaries, native line output, and ordered native-to-Cast handoff."""

import os
import json
from http.server import ThreadingHTTPServer
import sys
import tempfile
import threading
import time
import unittest
import wave
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import Mock, patch

from app.omnivoice_stream import stream_lines, NativeStreamError
from app.reply_stream import ReplyStreams
from app.sentences import LineBuffer
from app.tasks import TaskStore, TaskCancelled


class LineTests(unittest.TestCase):
    def test_newlines_are_incremental_and_punctuation_is_fallback(self):
        lines = []
        buffer = LineBuffer(lines.append)
        for text in ("Hello.", "\n\nТемпература три", " градуса!\r", "\n", "Value 3.", "14 is fine. Why?!", " Last part"):
            buffer.feed(text)
        self.assertEqual(lines, ["Hello.", "Температура три градуса!"])
        buffer.finish()
        self.assertEqual(lines, ["Hello.", "Температура три градуса!", "Value 3.14 is fine.", "Why?!", "Last part"])


class ReplyStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore()
        self.player = Mock()
        self.player.play.return_value = {"played": True}
        self.jobs = []
        self.addCleanup(self.cleanup_jobs)

    def cleanup_jobs(self):
        for manager in self.jobs:
            for job in list(manager.jobs.values()):
                self.store.cancel(job.task_id)
                job.stopped.set()
            self.wait(lambda: not manager.jobs)

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Streaming worker did not finish")

    def manager(self, respond, synthesize=None):
        def clips(next_line, lang, task_id):
            while True:
                line = next_line()
                if line is None:
                    return
                path = self.root / f"{len(list(self.root.iterdir()))}.wav"
                path.write_bytes(b"RIFF")
                yield line, path
        manager = ReplyStreams(self.store, respond=respond, synthesize=synthesize or clips,
                               transcode=lambda path, task_id: path.with_suffix('.ogg'),
                               duration=lambda path, task_id: .05,
                               urls=lambda path: {"audio_url": str(path), "audio_path": str(path)},
                               player=self.player, guard=.01)
        self.jobs.append(manager)
        return manager

    def test_first_sentence_returns_early_then_casts_without_overlap(self):
        release_text, release_cast, casting = threading.Event(), threading.Event(), threading.Event()
        def respond(text, *, on_sentence, check):
            on_sentence("First.")
            while not release_text.wait(.01):
                check()
            on_sentence("Second!")
            on_sentence("Third?")
            return "First.\nSecond!\nThird?"
        starts = []
        def play(url, task_id, **kwargs):
            starts.append(time.monotonic())
            if len(starts) == 1:
                casting.set()
                while not release_cast.wait(.01):
                    self.store.check(task_id)
            return {"played": True}
        self.player.play.side_effect = play
        manager = self.manager(respond)
        task = self.store.create(source="nest")
        first = manager.start(task['id'], "hello", "en")
        self.assertEqual(first['response'], "First.")
        self.assertEqual(self.store.get(task['id'])['stages'][0]['status'], 'loading')
        self.player.play.assert_not_called()
        acknowledged = time.monotonic()
        manager.deliver(task['id'], 123)
        release_text.set()
        self.assertTrue(casting.wait(2))
        self.assertGreaterEqual(starts[0] - acknowledged, .05)
        self.assertEqual(self.player.play.call_count, 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'loading')
        release_cast.set()
        self.wait(lambda: not manager.jobs)
        self.assertEqual(self.player.play.call_count, 2)
        for call in self.player.play.call_args_list:
            self.assertEqual(call.kwargs, {"wait": True, "duration": .05})
            self.assertNotEqual(call.args[0], first['audio_url'])
        task = self.store.get(task['id'])
        self.assertEqual(task['status'], 'done')
        stages = {s['name']: s for s in task['stages']}
        self.assertEqual(stages['process']['detail']['response'], "First.\nSecond!\nThird?")
        self.assertEqual(stages['output']['detail']['cast_sentences'], 2)

    def test_interruption_during_cast_drops_remaining_sentences(self):
        casting = threading.Event()
        def respond(text, *, on_sentence, check):
            for line in ("First.", "Second.", "Third."):
                on_sentence(line)
            return "First. Second. Third."
        def play(url, task_id, **kwargs):
            casting.set()
            while True:
                self.store.check(task_id)
                time.sleep(.01)
        self.player.play.side_effect = play
        manager = self.manager(respond)
        task = self.store.create()
        manager.start(task['id'], "hello", "en")
        manager.deliver(task['id'], 10)
        self.assertTrue(casting.wait(2))
        self.store.cancel(task['id'])
        self.wait(lambda: not manager.jobs)
        self.assertEqual(self.player.play.call_count, 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'interrupted')

    def test_native_delivery_failure_stops_cast(self):
        def respond(text, *, on_sentence, check):
            on_sentence("First.")
            on_sentence("Second.")
            return "First. Second."
        manager = self.manager(respond)
        task = self.store.create()
        manager.start(task['id'], "hello", "en")
        manager.deliver(task['id'], 0, "connection lost")
        self.wait(lambda: not manager.jobs)
        self.player.play.assert_not_called()
        self.assertEqual(self.store.get(task['id'])['error']['message'], 'connection lost')

    def test_synthesis_failure_is_reported_before_http_reply(self):
        def respond(text, *, on_sentence, check):
            on_sentence("First.")
            return "First."
        def clips(*args):
            raise RuntimeError("synthesis failed")
            yield
        manager = self.manager(respond, clips)
        task = self.store.create()
        with self.assertRaisesRegex(RuntimeError, "synthesis failed"):
            manager.start(task['id'], "hello", "en")
        self.wait(lambda: not manager.jobs)
        self.assertEqual(self.store.get(task['id'])['status'], 'error')

    def test_http_returns_first_clip_and_native_ack_starts_continuation(self):
        with patch.dict(os.environ, {"DATA_DIR": str(self.root)}):
            from app import server
        gate = threading.Event()
        def respond(text, *, on_sentence, check):
            on_sentence("First.")
            while not gate.wait(.01):
                check()
            on_sentence("Second.")
            return "First.\nSecond."
        manager = self.manager(respond)
        class QuietHandler(server.Handler):
            def log_message(self, *args):
                pass
        with patch.object(server, 'TASKS', self.store), \
             patch.object(server, 'STREAMS', manager), \
             patch.object(server, 'OUTPUT_MODE', 'native'), \
             patch.object(server, 'streaming_enabled', return_value=True):
            http = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            def post(path, data):
                request = Request(f'http://127.0.0.1:{http.server_port}{path}',
                                  data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=2) as response:
                    return json.load(response)
            try:
                first = post('/answer', {'text': 'hello', 'lang': 'en'})
                self.assertTrue(first['streaming'])
                self.assertEqual(first['response'], 'First.')
                self.assertFalse(gate.is_set())
                self.player.play.assert_not_called()
                ack = post('/task/output', {'task_id': first['task_id'], 'bytes': 123})
                self.assertTrue(ack['streaming'])
                self.assertEqual(self.store.get(first['task_id'])['status'], 'loading')
                gate.set()
                self.wait(lambda: not manager.jobs)
                self.assertEqual(self.player.play.call_count, 1)
                self.assertEqual(self.store.get(first['task_id'])['status'], 'done')
            finally:
                gate.set()
                http.shutdown()
                http.server_close()
                worker.join()


class NativeLineStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore()
        self.task = self.store.create()

    def test_native_line_audio_is_available_before_text_eof(self):
        # A fixture imitates native live-size WAV headers, including a bare
        # RIFF in PCM, to verify framing without loading a model or device.
        script = r'''
import sys,struct
h=struct.pack('<4sI4s4sIHHIIHH4sI',b'RIFF',0x7fffffff,b'WAVE',b'fmt ',16,1,1,24000,48000,2,16,b'data',0x7fffffff)
for line in sys.stdin:
 sys.stdout.buffer.write(h+b'RIFF'+b'\0\0'*100)
 sys.stdout.buffer.flush()
'''
        gate = threading.Event()
        sent = []
        def next_line(stop=None):
            if len(sent) < 2:
                line = ["First.", "Second!"][len(sent)]
                sent.append(line)
                return line
            while not gate.wait(.01):
                if stop and stop():
                    return None
            return None
        generator = stream_lines([sys.executable, '-u', '-c', script], next_line,
                                 store=self.store, task_id=self.task['id'], output_dir=self.root)
        try:
            first, path = next(generator)
            self.assertEqual(first, 'First.')
            self.assertFalse(gate.is_set())
            with wave.open(str(path)) as audio:
                self.assertEqual(audio.getframerate(), 24000)
                self.assertEqual(audio.readframes(2), b'RIFF')
            gate.set()
            remaining = list(generator)
            self.assertEqual([text for text, path in remaining], ['Second!'])
        finally:
            gate.set()
            generator.close()

    def test_gpu_stream_failure_retains_only_unsynthesized_lines_for_fallback(self):
        with patch.dict(os.environ, {"DATA_DIR": str(self.root)}):
            from app import server
        source = iter(['First.', 'Second.', 'Third.', None])
        path = self.root / 'first.wav'
        path.write_bytes(b'RIFF')
        def native(command, next_line, **kwargs):
            self.assertIn('--stream-by-line', command)
            self.assertEqual(next_line(), 'First.')
            self.assertEqual(next_line(), 'Second.')
            yield 'First.', path
            raise NativeStreamError('device lost', ['Second.'])
        with patch.object(server, 'TASKS', self.store), \
             patch.object(server, '_omnivoice_gpu_ready', return_value=True), \
             patch.object(server, '_gpu_free_vram_mib', return_value=3000), \
             patch.object(server, '_native_line_streaming', return_value=True), \
             patch.object(server, 'stream_lines', side_effect=native), \
             patch.object(server, 'synthesize', return_value=path) as synthesize:
            result = list(server.synthesize_lines(lambda stop=None: next(source), 'en', self.task['id']))
        self.assertEqual([text for text, path in result], ['First.', 'Second.', 'Third.'])
        self.assertEqual([c.args[0] for c in synthesize.call_args_list], ['Second.', 'Third.'])
        self.assertTrue(all(c.kwargs['cpu_only_reason'] == 'device lost' for c in synthesize.call_args_list))


if __name__ == '__main__':
    unittest.main()
