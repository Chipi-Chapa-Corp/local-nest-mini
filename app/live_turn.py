"""Run a microphone turn and expose its response as one live Cast stream."""

import threading
from pathlib import Path
import tempfile
import time
import wave

from . import processor
from .cast_stream import CastStream
from .s3_audio import strip_wake_word
from .tasks import TaskCancelled
from .voice_live import TOOL_LABELS, VoiceLive


class LiveTurns:
    def __init__(self, store, player, base_url, transcribe, speak_pcm):
        self.store, self.player = store, player
        self.base_url, self.transcribe, self.speak_pcm = base_url, transcribe, speak_pcm
        self.status_audio = {}
        self.lock = threading.Lock()
        self.current = None

    def warm(self):
        for name, labels in TOOL_LABELS.items():
            for lang, text in labels.items():
                self.status_audio[name, lang] = self.speak_pcm(text, lang, None)

    def get(self, task_id):
        with self.lock:
            if self.current is not None and self.current.task_id == task_id:
                return self.current
        return None

    def run(self, task_id, chunks, lang, received):
        url = f"{self.base_url}/live/{task_id}.mp3"
        stream = CastStream(url, task_id, self.store, self.player)
        with self.lock:
            previous = self.current
            self.current = stream
        if previous is not None:
            if previous.task_id != task_id:
                prior = self.store.get(previous.task_id)
                if prior and prior["status"] == "loading":
                    self.store.cancel(previous.task_id)
            threading.Thread(target=self.player.stop, args=(previous.url,), daemon=True).start()

        check = lambda: self.store.check(task_id)
        stage = "received"
        self.store.create(task_id, source="nest", transport="cast-live")
        self.store.start(task_id, stage)
        voice = None
        try:
            pcm = bytearray()
            try:
                voice = VoiceLive(lang, check)
            except TaskCancelled:
                raise
            except Exception as exc:
                self.store.start(task_id, stage, voice_live_error=type(exc).__name__)
            for chunk in chunks:
                check()
                pcm.extend(chunk)
                if voice:
                    try:
                        voice.append(chunk)
                    except TaskCancelled:
                        raise
                    except Exception:
                        voice.close()
                        voice = None
            received()
            self.store.finish(task_id, stage, bytes=len(pcm))
            if not pcm:
                self.store.skip(task_id, "output", "empty audio")
                return

            stage = "transcribe"
            self.store.start(task_id, stage, engine="Azure Speech" if voice else "Whisper")
            answer_started = False
            local_audio_bytes = 0
            local_started = local_first = local_done = None
            local_peaks = {}

            def append_answer(audio):
                nonlocal answer_started
                if audio:
                    if not answer_started and voice:
                        engine = "Azure / Harper Narrator" if lang == "ru" else "Azure / Ava"
                        self.store.start(task_id, "tts", engine=engine)
                    answer_started = True
                    stream.append(audio)

            def speak(text):
                nonlocal local_audio_bytes, local_started, local_first, local_done
                if local_started is None:
                    local_started = time.monotonic()
                self.store.start(task_id, "tts", engine="Silero / Xenia" if lang == "ru" else "Pocket / Alba")
                audio = self.speak_pcm(text, lang, task_id)
                local_audio_bytes += len(audio)
                local_done = time.monotonic()
                if local_first is None:
                    local_first = local_done
                metrics = self.store.detail(task_id, "tts").get("metrics", {})
                for key in ("peak_rss_mib", "peak_vram_mib"):
                    if key in metrics:
                        local_peaks[key] = max(local_peaks.get(key, 0), metrics[key])
                append_answer(audio)

            def announce(name):
                labels = TOOL_LABELS[name]
                self.store.start(task_id, "process", tool=labels[lang])
                clip = self.status_audio.get((name, lang))
                stream.append(clip if clip else self.speak_pcm(labels[lang], lang, task_id))

            if voice:
                try:
                    with processor._conversation_lock:
                        query = strip_wake_word(voice.commit(
                            list(processor._history)[-(processor.HISTORY_LIMIT - 1):]))
                        if not query:
                            self.store.finish(task_id, stage, text="")
                            self.store.skip(task_id, "output", "empty transcript")
                            return
                        lang = reply_language(lang, query)
                        input_seconds = len(pcm) / 32000
                        self.store.finish(task_id, stage, text=query, lang=lang,
                                          metrics={**voice.metrics["transcribe"], "audio_seconds": input_seconds})
                        stage = "process"
                        self.store.start(task_id, stage, text=query, engine="Azure Voice Live")
                        reply = voice.respond(lang, on_audio=append_answer, on_tool=announce)
                        if not answer_started:
                            raise RuntimeError("Azure Voice Live returned no audio")
                        check()
                        self.store.update_detail(task_id, "transcribe", metrics={
                            **voice.metrics["transcribe"], "audio_seconds": input_seconds,
                        })
                        if reply:
                            processor._history.extend(({"role": "user", "content": query},
                                                       {"role": "assistant", "content": reply}))
                except TaskCancelled:
                    raise
                except Exception as exc:
                    if answer_started:
                        raise
                    self.store.start(task_id, stage, voice_live_error=str(exc))
                    voice.close()
                    voice = None

            if voice is None:
                stage = "transcribe"
                self.store.start(task_id, stage, engine="Whisper")
                with tempfile.TemporaryDirectory(prefix="nest-voice-") as directory:
                    wav = Path(directory) / "input.wav"
                    with wave.open(str(wav), "wb") as audio:
                        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        audio.writeframes(pcm)
                    query = strip_wake_word(self.transcribe(wav, lang, task_id))
                self.store.finish(task_id, stage, text=query)
                if not query:
                    self.store.skip(task_id, "output", "empty transcript")
                    return
                lang = reply_language(lang, query)
                stage = "process"
                self.store.start(task_id, stage, text=query, engine="Responses fallback")
                reply = processor.respond(query, check=check, on_sentence=speak, task_id=task_id)
            if voice:
                engine = "Azure / Harper Narrator" if lang == "ru" else "Azure / Ava"
                self.store.finish(task_id, "process", response=reply, lang=lang,
                                  metrics=voice.metrics["process"])
                self.store.finish(task_id, "tts", engine=engine, metrics=voice.metrics["tts"])
            else:
                engine = "Silero / Xenia" if lang == "ru" else "Pocket / Alba"
                self.store.finish(task_id, "process", response=reply, lang=lang)
                metrics = {"local": True, **local_peaks, "audio_seconds": local_audio_bytes / 48000}
                if local_started is not None:
                    metrics.update(first_chunk_seconds=local_first - local_started,
                                   total_seconds=local_done - local_started)
                self.store.finish(task_id, "tts", engine=engine, metrics=metrics)
            stage = "output"
            result = stream.close()
            check()
            if not result:
                raise RuntimeError("No Cast playback started")
            self.store.finish(task_id, stage, transport="cast-live", **result)
        except (EOFError, TaskCancelled):
            self.store.cancel(task_id)
        except Exception as exc:
            self.store.fail(task_id, stage, f"{type(exc).__name__}: {exc}")
        finally:
            if voice:
                voice.close()
            if not stream.closed:
                try:
                    stream.close()
                except Exception:
                    pass


def reply_language(lang, text):
    if lang in {"en", "ru"}:
        return lang
    return "ru" if any("\u0400" <= char <= "\u04ff" for char in text) else "en"
