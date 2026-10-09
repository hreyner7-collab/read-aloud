"""Is the silence in the source audio, or added by the player?

Fetches a text from Microsoft in one piece, decodes it, and lists runs of
digital silence (>= 60 ms) with their positions. Compare with the pause list
from tools/real_run.py: if they match, those pauses are the voice pausing, not
the app stalling.

Usage:  python tools/source_check.py [chars]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import array  # noqa: E402

import miniaudio  # noqa: E402

from readaloud.engine import Voice, split_chunks  # noqa: E402

SAMPLE = (
    "Read Aloud turns whatever you have selected into speech using the same natural voices as "
    "Microsoft Edge. You select a sentence in a document, a paragraph in an email, or a whole "
    "chapter of a book, and the button reads it back to you in a clear voice. "
)


async def fetch(text):
    voice = Voice(False)
    out = []

    async def go(t):
        got = []

        async def synth():
            await voice.synth(t, "+0%", got.append, {})

        await synth()
        out.append(b"".join(got))

    # one request per chunk, exactly like the app does
    for chunk in split_chunks(text):
        await go(chunk)
    await voice.session.close()
    return out


def zero_runs(pcm, min_ms=60, sr=24000):
    runs, start = [], None
    for i, s in enumerate(pcm):
        if s == 0:
            if start is None:
                start = i
        elif start is not None:
            if (i - start) * 1000 / sr >= min_ms:
                runs.append((start / sr, (i - start) / sr))
            start = None
    return runs


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    text = (SAMPLE * 12).strip()[:want]
    parts = asyncio.run(fetch(text))
    total = 0.0
    print(f"{len(parts)} chunks")
    for i, mp3 in enumerate(parts):
        pcm = array.array("h", miniaudio.decode(mp3, nchannels=1, sample_rate=24000).samples)
        runs = zero_runs(pcm)
        secs = len(pcm) / 24000
        print(f"chunk {i}: {secs:5.1f}s audio, {len(runs)} silent runs (>=60ms): "
              f"{[(round(a, 2), round(b, 2)) for a, b in runs]}")
        total += secs
    print(f"total audio {total:.1f}s")


if __name__ == "__main__":
    main()
