"""Server-side end-of-utterance detection for live Nest S3 voice turns.

The Nest does not end a query by itself: in every owned-device Google capture
the server sends the end-of-utterance event (1253625.2.1 = 3) shortly after
speech stops, and only then does the client stop recording and send its
end-of-audio frame. Google closed a follow-on turn with no speech after 5 s.
Without that event the Nest keeps listening until a long device fallback.

This is a deliberately simple energy endpointer over the decoded query audio.
The noise floor is estimated per turn, because the microphone gain differs a
lot between wake-word turns (~-40 dBFS) and follow-on turns (~-58 dBFS).
"""

from __future__ import annotations

import array
import math
import os
import subprocess

RATE = 16000
FRAME = RATE // 10  # 100 ms

SILENCE = float(os.environ.get("S3_ENDPOINT_SILENCE", "1.8"))
NO_SPEECH = float(os.environ.get("S3_NO_SPEECH", "5.0"))
MAX_UTTERANCE = float(os.environ.get("S3_MAX_UTTERANCE", "12.0"))
MARGIN_DB = float(os.environ.get("S3_SPEECH_MARGIN_DB", "12.0"))
MIN_SPEECH = 0.2
MIN_AUDIO = 0.5

SPEECH_END = "speech_end"
NO_SPEECH_DETECTED = "no_speech"
MAX_LENGTH = "max_length"


def decode_pcm(flac: bytes) -> bytes:
    """Decode a (possibly still growing) FLAC stream to mono 16 kHz s16le."""
    if not flac:
        return b""
    result = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-threads", "1", "-filter_threads", "1", "-i", "pipe:0", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "pipe:1"],
        input=flac, capture_output=True, timeout=10)
    return result.stdout[:len(result.stdout) // 2 * 2]


def frame_levels(pcm: bytes) -> list[float]:
    samples = array.array("h")
    samples.frombytes(pcm)
    levels = []
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        chunk = samples[start:start + FRAME]
        rms = math.sqrt(sum(x * x for x in chunk) / FRAME) / 32768
        levels.append(20 * math.log10(max(rms, 1e-6)))
    return levels


def decide(levels: list[float], silence=None, no_speech=None, max_utterance=None,
           margin_db=None):
    """Return (decision or None, end time in seconds of audio)."""
    silence = SILENCE if silence is None else silence
    no_speech = NO_SPEECH if no_speech is None else no_speech
    max_utterance = MAX_UTTERANCE if max_utterance is None else max_utterance
    margin_db = MARGIN_DB if margin_db is None else margin_db
    step = FRAME / RATE
    duration = len(levels) * step
    if duration < MIN_AUDIO:
        return None, duration
    floor = sorted(levels)[len(levels) // 10]
    speech = [level > floor + margin_db for level in levels]
    need = max(1, round(MIN_SPEECH / step))
    started = None
    run = 0
    for index, active in enumerate(speech):
        run = run + 1 if active else 0
        if run >= need:
            started = index
            break
    if started is None:
        if duration >= no_speech:
            return NO_SPEECH_DETECTED, duration
        return None, duration
    last = max(index for index, active in enumerate(speech) if active)
    speech_end = (last + 1) * step
    if duration - speech_end >= silence:
        return SPEECH_END, speech_end
    if duration >= max_utterance:
        return MAX_LENGTH, duration
    return None, duration
