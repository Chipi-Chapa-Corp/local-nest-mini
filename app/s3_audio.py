"""Extract FLAC utterances from captured Nest S3 request bodies."""

import re

from .native import _fields

WAKE = re.compile(r"^\s*(?:(?:hey|ok|okay)\s+google)[\s,.!?:;-]*", re.IGNORECASE)
MAX_SEGMENT = 4 * 1024 * 1024


def _audio_segments(body: bytes):
    """Reassemble only audio field 1 of query/wake protobuf extensions.

    Control messages can occur between any two chunks. Never search their
    bytes (or compressed audio) for apparent protobuf tags. An incomplete final
    gRPC frame is ignored until more network data arrives.
    """
    segments = []
    active = {}
    offset = 0
    while offset + 5 <= len(body):
        size = int.from_bytes(body[offset + 1:offset + 5], 'big')
        end = offset + 5 + size
        if end > len(body):
            break
        if body[offset] != 0:
            raise ValueError('compressed S3 request frame is unsupported')
        for extension, wire, message in _fields(body[offset + 5:end]):
            if wire != 2 or extension not in (293101, 62724734):
                continue
            for field, kind, chunk in _fields(message):
                if field != 1 or kind != 2:
                    continue
                if chunk.startswith(b'fLaC'):
                    segment = bytearray()
                    segments.append((extension, segment))
                    active[extension] = segment
                segment = active.get(extension)
                if segment is not None:
                    if len(segment) + len(chunk) > MAX_SEGMENT:
                        raise ValueError('S3 audio segment exceeds size limit')
                    segment.extend(chunk)
        offset = end
    return [(kind, bytes(audio)) for kind, audio in segments]


def extract_flac_segments(body: bytes):
    return [audio for kind, audio in _audio_segments(body)]


def extract_query_audio(body: bytes):
    queries = [audio for kind, audio in _audio_segments(body) if kind == 293101]
    return queries[-1] if queries else b''


def has_wake_audio(body: bytes) -> bool:
    """Wake-word turns carry hotword audio; continued-conversation turns don't."""
    return any(kind == 62724734 for kind, audio in _audio_segments(body))


def strip_wake_word(text: str) -> str:
    return WAKE.sub("", (text or "").strip()).strip()


# Whisper output for silence or the Nest's listening chime. A follow-on turn
# opens the mic without the user asking, so answering these would loop.
HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "bye", "продолжение следует", "спасибо", "спасибо за внимание",
}
LETTER = re.compile(r"[A-Za-zЀ-ӿ]")


def is_noise_transcript(text: str) -> bool:
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return not LETTER.search(text or "") or " ".join(words) in HALLUCINATIONS
