# typed: true

"""Produces deterministic MP3 audio for a given chunk of text.

One MP3 segment per chunk, pre-encoded with lameenc at 48kbps CBR mono.
The fake server sends these bytes as the "audio" stream the app expects.
We pick a modest loud passage so the playback tap's silence analysis stays
honest (no silent padding in the source).

Usage only through fake_server.py; do not call directly from the app.
"""

from __future__ import annotations

import os
from typing import List, Optional

try:  # optional dependency for the MP3 archiving step
    import lameenc, numpy as np
except Exception as e:
    lameenc = None
    _import_err = e

_SAMPLE_RATE = 24000
_BITRATE_KBPS = 48
_DURATION_S = 6.0   # realistic chunk length: a 400-1500 char sentence group
_MAX_BYTES_PER_CHUNK = int(_SAMPLE_RATE * _DURATION_S * _BITRATE_KBPS / 8) + 32

_CACHED: dict = {}   # text_hash -> bytes


def _build_mp3(text: str) -> bytes:
    """Create one loud 6s MP3 segment for the chunk."""
    assert lameenc is not None, _import_err
    n = int(_SAMPLE_RATE * _DURATION_S)
    t = np.arange(n) / _SAMPLE_RATE
    # a clean mix that decodes to a pure non-silent tone at full scale;
    # lameenc.encode() expects int16 interleaved samples, not floats
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.5
           + np.sin(2 * np.pi * 880 * t) * 0.25
           + np.sin(2 * np.pi * 1320 * t) * 0.125)
    pcm16 = (pcm * 32767 * 0.9).astype(np.int16).tobytes()
    enc = lameenc.Encoder()
    enc.set_bit_rate(_BITRATE_KBPS)
    enc.set_channels(1)
    enc.set_in_sample_rate(_SAMPLE_RATE)
    raw = enc.encode(pcm16) + enc.flush()
    return bytes(raw[:_MAX_BYTES_PER_CHUNK])


def chunk_mp3(text: str) -> bytes:
    h = hash(text)
    if h not in _CACHED:
        _CACHED[h] = _build_mp3(text)
    return _CACHED[h]


def mp3_for_chunks(chunks: List[str], cache_dir: Optional[str] = None) -> List[bytes]:
    """One MP3 segment per chunk; cache on disk if cache_dir given."""
    out: List[bytes] = []
    for c in chunks:
        out.append(chunk_mp3(c))
    return out
