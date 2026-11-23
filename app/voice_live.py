"""Azure Voice Live input, Sol tools, and language-specific streaming output."""

import base64
import json
import os
import time
from urllib.parse import urlencode, urlsplit

from . import processor, web_tools
from .sentences import LineBuffer

TOOL_LABELS = {
    "web_search": {"en": "Searching the web.", "ru": "Ищу в интернете."},
    "read_web_page": {"en": "Checking sources.", "ru": "Проверяю источники."},
}
AZURE_VOICE = "en-US-AvaMultilingualNeural"


def enabled():
    return os.environ.get("NEST_VOICE_LIVE", "0").lower() in {"1", "true", "yes"}


def endpoint_url():
    host = urlsplit(os.environ.get("LLM_BASE_URL", "")).hostname or ""
    if not host.endswith(".openai.azure.com"):
        raise RuntimeError("Voice Live requires an Azure OpenAI LLM_BASE_URL")
    foundry = host.removesuffix(".openai.azure.com") + ".services.ai.azure.com"
    query = urlencode({"api-version": "2026-04-10", "model": os.environ["LLM_MODEL"]})
    return f"wss://{foundry}/voice-live/realtime?{query}"


class VoiceLive:
    def __init__(self, lang, check):
        import websocket

        self.lang, self.check = lang, check
        self.timeout = websocket.WebSocketTimeoutException
        self.tools = web_tools.TOOL_DEFINITIONS if web_tools.available() else []
        self.ws = websocket.create_connection(
            endpoint_url(), header={"api-key": os.environ["LLM_API_KEY"]},
            timeout=5, suppress_origin=True,
        )
        try:
            self.ws.settimeout(.25)
            settings = {
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16",
                "input_audio_sampling_rate": 16000,
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "azure-speech",
                    "language": {"en": "en-US", "ru": "ru-RU"}.get(lang, "en-US,ru-RU"),
                },
                "turn_detection": None,
                "voice": {"type": "azure-standard", "name": AZURE_VOICE},
                "instructions": processor.SYSTEM_PROMPT + (processor.TOOL_INSTRUCTIONS if self.tools else ""),
            }
            if effort := os.environ.get("LLM_REASONING_EFFORT"):
                settings["reasoning_effort"] = effort
            if self.tools:
                settings["tools"] = [{"type": "function", **item["function"]} for item in self.tools]
            self.send("session.update", session=settings)
            for event in self.events(10):
                if event["type"] == "session.updated":
                    break
        except BaseException:
            self.close()
            raise

    def close(self):
        self.ws.close(timeout=.2)

    def send(self, kind, **payload):
        self.check()
        self.ws.send(json.dumps({"type": kind, **payload}, ensure_ascii=False))

    def events(self, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                data = self.ws.recv()
            except self.timeout:
                continue
            if not data:
                raise RuntimeError("Azure Voice Live disconnected")
            event = json.loads(data)
            if event["type"] == "error":
                code = event.get("error", {}).get("code") or "unknown"
                raise RuntimeError(f"Azure Voice Live error ({code})")
            yield event
        raise RuntimeError("Azure Voice Live timed out")

    def append(self, pcm):
        self.send("input_audio_buffer.append", audio=base64.b64encode(pcm).decode("ascii"))

    def commit(self, history):
        for message in history:
            role = message["role"]
            content = {"type": "text" if role == "assistant" else "input_text",
                       "text": message["content"]}
            self.send("conversation.item.create", item={"type": "message", "role": role,
                                                        "content": [content]})
        self.send("input_audio_buffer.commit")
        for event in self.events(15):
            match event["type"]:
                case "conversation.item.input_audio_transcription.completed":
                    return event.get("transcript", "").strip()
                case "conversation.item.input_audio_transcription.failed":
                    raise RuntimeError("Azure Voice Live transcription failed")

    def respond(self, lang, *, on_audio, on_sentence, on_tool):
        tools_left = processor.MAX_TOOL_CALLS if self.tools else 0
        announced = False
        while True:
            self.send("response.create", response={
                "modalities": ["text"] if lang == "ru" else ["text", "audio"],
                "tool_choice": "auto" if tools_left else "none",
            })
            words = []
            sentences = LineBuffer(on_sentence) if lang == "ru" else None
            for event in self.events():
                kind = event["type"]
                if kind in {"response.audio.delta", "response.output_audio.delta"}:
                    on_audio(base64.b64decode(event["delta"]))
                elif kind in {"response.text.delta", "response.output_text.delta",
                              "response.audio_transcript.delta", "response.output_audio_transcript.delta"}:
                    if (sentences is None) != ("audio_transcript" in kind):
                        continue
                    text = event["delta"]
                    words.append(text)
                    if sentences:
                        sentences.feed(text)
                elif kind == "response.done":
                    response = event["response"]
                    if response["status"] != "completed":
                        raise RuntimeError(f"Azure Voice Live response {response['status']}")
                    break
            calls = [item for item in response.get("output", []) if item["type"] == "function_call"]
            if not calls:
                if sentences:
                    sentences.finish()
                return "".join(words).strip()
            if not tools_left:
                raise RuntimeError("Azure Voice Live requested tools after reaching the tool limit")
            for call in calls:
                self.check()
                if tools_left:
                    tools_left -= 1
                    name = call["name"]
                    if name in TOOL_LABELS and not announced:
                        on_tool(name)
                        announced = True
                    result = web_tools.execute(name, call.get("arguments", ""))
                else:
                    result = "The tool limit is reached. Give your final answer."
                self.send("conversation.item.create", item={
                    "type": "function_call_output", "call_id": call["call_id"], "output": result,
                })
