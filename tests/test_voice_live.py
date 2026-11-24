import base64
import shutil
import threading
import unittest
from unittest.mock import Mock, patch

from app import processor
from app.cast_stream import CastStream
from app.tasks import TaskStore
from app.voice_live import VoiceLive


class VoiceLiveTests(unittest.TestCase):
    def test_tool_status_precedes_execution_and_russian_streams_harper(self):
        voice = VoiceLive.__new__(VoiceLive)
        voice.tools = [object()]
        voice.send = Mock()
        voice.check = Mock()
        tool = {"type": "function_call", "name": "web_search", "call_id": "id",
                "arguments": '{"query":"example"}'}
        voice.events = Mock(side_effect=[
            [{"type": "response.done", "response": {"status": "completed", "output": [tool]}}],
            [{"type": "response.audio.delta", "delta": "AAE="},
             {"type": "response.audio_transcript.delta", "delta": "Откройте GitHub."},
             {"type": "response.done", "response": {"status": "completed", "output": []}}],
        ])
        order = []
        audio = Mock()
        with patch("app.voice_live.web_tools.execute",
                   side_effect=lambda *args: order.append("execute") or "result"):
            reply = voice.respond("ru", on_audio=audio, on_tool=lambda name: order.append(name))
        self.assertEqual(order, ["web_search", "execute"])
        self.assertEqual(reply, "Откройте GitHub.")
        audio.assert_called_once_with(b"\x00\x01")
        requests = [call.kwargs["response"] for call in voice.send.call_args_list
                    if call.args[0] == "response.create"]
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["modalities"], ["text", "audio"])
            self.assertEqual(request["voice"], {
                "type": "azure-standard", "name": "en-US-Harper:MAI-Voice-2.1-Flash",
                "style": "narrator",
            })
            self.assertIn("original spelling and script", request["instructions"])
            self.assertIn(processor.TOOL_INSTRUCTIONS, request["instructions"])
            self.assertNotIn(processor.LOCAL_SPEECH_RULES, request["instructions"])

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
        out = Mock()
        reply = voice.respond("en", on_audio=out, on_tool=Mock())
        self.assertEqual(reply, "Hello.")
        out.assert_called_once_with(b"\x00\x01")
        self.assertEqual(voice.send.call_args.kwargs["response"],
                         {"modalities": ["text", "audio"], "tool_choice": "none"})

    def test_voice_usage_is_split_by_stage(self):
        voice = VoiceLive.__new__(VoiceLive)
        voice.tools = []
        voice.send = Mock()
        voice.events = lambda: iter([
            {"type": "response.audio_transcript.delta", "delta": "Hello."},
            {"type": "response.audio.delta", "delta": base64.b64encode(bytes(48000)).decode()},
            {"type": "response.done", "response": {"status": "completed", "usage": {
                "output_tokens": 28,
                "input_token_details": {"text_tokens": 100, "audio_tokens": 35,
                                        "cached_tokens_details": {"text_tokens": 20, "audio_tokens": 5}},
                "output_token_details": {"text_tokens": 5, "reasoning_tokens": 3, "audio_tokens": 20},
            }}},
        ])
        with patch.dict("os.environ", {"LLM_MODEL": "gpt-6.1-sol"}):
            voice.respond("en", on_audio=Mock(), on_tool=Mock())
        stt, llm, tts = (voice.metrics[name] for name in ("transcribe", "process", "tts"))
        self.assertEqual(stt["usage"], {"input_tokens": 35, "cached_tokens": 5, "output_tokens": 0})
        self.assertEqual(llm["usage"], {"input_tokens": 100, "cached_tokens": 20, "output_tokens": 8})
        self.assertEqual(tts["audio_seconds"], 1)
        self.assertAlmostEqual(tts["cost_usd"], 20 * 31 / 1_000_000)
        self.assertTrue(all(stage["cost_estimated"] for stage in (stt, llm, tts)))


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
