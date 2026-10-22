"""Build a native Nest S3 response by replacing the audio in a captured template."""

from __future__ import annotations

import struct
from pathlib import Path

AUDIO_FIELD = 28599812


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated varint")


def _field(field: int, value) -> bytes:
    return _encode(field, 0 if isinstance(value, int) else 2, value)


def _encode(field: int, wire: int, value) -> bytes:
    key = _varint((field << 3) | wire)
    if wire == 0:
        return key + _varint(value)
    if wire == 2:
        return key + _varint(len(value)) + value
    if wire in (1, 5):
        return key + value
    raise ValueError(f"unsupported wire type {wire}")


def _encode_fields(fields: list[tuple[int, int, object]]) -> bytes:
    return b"".join(_encode(field, wire, value) for field, wire, value in fields)


def _fields(data: bytes) -> list[tuple[int, int, object]]:
    result = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field = key >> 3
        wire = key & 7
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            value = data[offset:offset + size]
            offset += size
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        result.append((field, wire, value))
    return result


def _frames(data: bytes) -> list[bytes]:
    result = []
    offset = 0
    while offset + 5 <= len(data):
        size = int.from_bytes(data[offset + 1:offset + 5], "big")
        end = offset + 5 + size
        if end > len(data):
            raise ValueError("truncated gRPC frame")
        result.append(data[offset + 5:end])
        offset = end
    if offset != len(data):
        raise ValueError("trailing bytes after gRPC frames")
    return result


def _frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def _audio_message(data: bytes) -> dict:
    result = {}
    for field, wire, value in _fields(data):
        if wire == 2:
            result[field] = value
    return result


def _rewrite_sequence(payload: bytes, sequence: int) -> bytes:
    out = []
    replaced = False
    for field, wire, value in _fields(payload):
        if field == 5 and wire == 0 and not replaced:
            out.append((field, wire, sequence))
            replaced = True
        else:
            out.append((field, wire, value))
    return _encode_fields(out)


def _rewrite_duration(payload: bytes, duration: float) -> bytes:
    out = []
    for field, wire, value in _fields(payload):
        if field != AUDIO_FIELD or wire != 2:
            out.append((field, wire, value))
            continue
        message = []
        for sub_field, sub_wire, sub_value in _fields(value):
            if sub_field != 3 or sub_wire != 2:
                message.append((sub_field, sub_wire, sub_value))
                continue
            meta = []
            for meta_field, meta_wire, meta_value in _fields(sub_value):
                if meta_field == 2 and meta_wire == 5:
                    meta_value = struct.pack("<f", duration)
                meta.append((meta_field, meta_wire, meta_value))
            message.append((sub_field, sub_wire, _encode_fields(meta)))
        out.append((field, wire, _encode_fields(message)))
    return _encode_fields(out)


def _ogg_duration(data: bytes) -> float:
    pages = _ogg_pages(data)
    granule = int.from_bytes(pages[-1][6:14], "little")
    if granule == 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Ogg stream has no completed duration")
    return granule / 48000


def _ogg_pages(data: bytes) -> list[bytes]:
    pages = []
    offset = 0
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS" or offset + 27 > len(data):
            raise ValueError("invalid Ogg stream")
        segments = data[offset + 26]
        table_end = offset + 27 + segments
        if table_end > len(data):
            raise ValueError("truncated Ogg page table")
        body = sum(data[offset + 27:table_end])
        end = table_end + body
        if end > len(data):
            raise ValueError("truncated Ogg page")
        pages.append(data[offset:end])
        offset = end
    return pages


def _chunks(pages: list[bytes]) -> list[bytes]:
    if len(pages) < 3:
        raise ValueError("Ogg stream needs OpusHead and OpusTags pages")
    chunks = [pages[0], pages[1]]
    rest = b"".join(pages[2:])
    for offset in range(0, len(rest), 1600):
        chunks.append(rest[offset:offset + 1600])
    return chunks


def build_response(template: bytes, ogg_audio: bytes) -> bytes:
    frames = _frames(template)
    start = None
    end = None
    for index, payload in enumerate(frames):
        try:
            fields = _fields(payload)
        except ValueError:
            continue
        audio = next((value for field, wire, value in fields
                      if field == AUDIO_FIELD and wire == 2), None)
        if not isinstance(audio, bytes):
            continue
        message = _audio_message(audio)
        if 1 not in message:
            continue
        chunk = message[1]
        if start is None and chunk.startswith(b"OggS"):
            start = index
        if start is not None:
            end = index
    if start is None or end is None:
        raise ValueError("native template has no audio frames")

    first_meta = [(field, wire, value) for field, wire, value in _fields(
        next(value for field, wire, value in _fields(frames[start])
             if field == AUDIO_FIELD and wire == 2)) if field not in {1}]
    first_seq = next(value for field, wire, value in _fields(frames[start])
                     if field == 5 and wire == 0)
    old_count = end - start + 1
    chunks = _chunks(_ogg_pages(ogg_audio))
    duration = _ogg_duration(ogg_audio)

    rebuilt = frames[:start]
    for index, chunk in enumerate(chunks):
        message = _field(1, chunk)
        if index == 0:
            for field, wire, value in first_meta:
                message += _encode(field, wire, value)
        payload = _field(5, first_seq + index) + _field(AUDIO_FIELD, message)
        rebuilt.append(payload)

    shift = len(chunks) - old_count
    for payload in frames[end + 1:]:
        fields = _fields(payload)
        if fields and fields[0][0] == 5 and fields[0][1] == 0:
            payload = _rewrite_sequence(payload, fields[0][2] + shift)
        rebuilt.append(_rewrite_duration(payload, duration))

    return b"".join(_frame(payload) for payload in rebuilt)


def load_template(path: Path) -> bytes:
    return path.read_bytes()
