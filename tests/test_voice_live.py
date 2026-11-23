import base64
import shutil
import threading
import unittest
from unittest.mock import Mock, patch

from app.cast_stream import CastStream
from app.tasks import TaskStore
from app.voice_live import VoiceLive


class VoiceLiveTests(unittest.TestCase):
    def test_tool_status_precedes_execution_and_russian_uses_text(self):
        voice = VoiceLive.__new__(VoiceLive)
        voice.tools = [object()]
        voice.send = Mock()
        voice.check = Mock()
        tool = {"type": "function_call", "name": "web_search", "call_id": "id",
                "arguments": '{"query":"example"}'}
        voice.events = Mock(side_effect=[
            [{"type": "response.done", "response": {"status": "completed", "output": [tool]}}],
            [{"type": "response.text.delta", "delta": "Готово.\n"},
             {"type": "response.done", "response": {"status": "completed", "output": []}}],
        ])
        order = []
        speak = Mock()
        audio = Mock()
        with patch("app.voice_live.web_tools.execute",
                   side_effect=lambda *args: order.append("execute") or "result"):
            reply = voice.respond("ru", on_audio=audio, on_sentence=speak,
                                  on_tool=lambda name: order.append(name))
        self.assertEqual(order, ["web_search", "execute"])
        self.assertEqual(reply, "Готово.")
        speak.assert_called_once_with("Готово.")
        audio.assert_not_called()
        self.assertEqual(voice.send.call_args_list[0].kwargs["response"]["modalities"], ["text"])

    def test_english_uses_azure_audio_and_transcript(self):
        voice = VoiceLive.__new__(VoiceLive)
        voice.tools = []
        voice.send = Mock()
        voice.check = Mock()
        voice.events = lambda: iter([
            {"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x01").decode()},
            {"type": "response.audio_transcript.delta", "delta": "Hello."},
            {"type": "response.done", "response": {"status": "completed", "output": []}},
        ])
        out, speak = Mock(), Mock()
        reply = voice.respond("en", on_audio=out, on_sentence=speak, on_tool=Mock())
        self.assertEqual(reply, "Hello.")
        out.assert_called_once_with(b"\x00\x01")
        speak.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg"), "requires ffmpeg")
class CastStreamTests(unittest.TestCase):
    def test_cast_starts_before_encoder_finishes_and_loads_only_once(self):
        store = TaskStore()
        task = store.create()["id"]
        started = threading.Event()
        player = Mock()
        player.play.side_effect = lambda *a, **kw: started.set() or {"played": True}
        stream = CastStream("http://localhost/audio.mp3", task, store, player)
        try:
            stream.append(bytes(24000))
            self.assertTrue(started.wait(3))
            self.assertFalse(stream.done)
            self.assertTrue(next(stream.read()))
            stream.append(bytes(24000))
        finally:
            stream.close()
        player.play.assert_called_once_with(stream.url, task, live=True)
        self.assertEqual(b"".join(stream.read()), bytes(stream.data))
