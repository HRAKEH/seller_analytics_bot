"""Lossless encoding for large API snapshots, retaining legacy JSON text reads."""
from __future__ import annotations
import zlib

_MAGIC = b'SBJSON1\x00'
_MIN_BYTES = 16 * 1024

def pack_raw_json(text: str) -> str | bytes:
    if not isinstance(text, str):
        raise TypeError('Expected canonical JSON text')
    if len(text) < _MIN_BYTES // 4:
        return text
    raw = text.encode('utf-8')
    if len(raw) < _MIN_BYTES:
        return text
    compressed = _MAGIC + zlib.compress(raw, level=1)
    return compressed if len(compressed) < len(raw) * .9 else text

def unpack_raw_json(value: str | bytes) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes) or not value.startswith(_MAGIC):
        raise ValueError('Unknown API snapshot encoding')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(value[len(_MAGIC):])
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Incomplete or extra compressed API snapshot data')
        return raw.decode('utf-8')
    except (zlib.error, UnicodeError) as exc:
        raise ValueError('Damaged compressed API snapshot') from exc

def decoded_raw_row(row) -> dict:
    out = dict(row)
    out['payload_json'] = unpack_raw_json(out['payload_json'])
    return out
