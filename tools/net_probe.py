"""Measure how fast Microsoft's speech service really delivers audio.

Prints, per text: connection kind, time to first byte, time to last byte,
audio seconds, delivery speed (x realtime), and the worst stall gap between
packets. Run from the repo root:  python tools/net_probe.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniaudio  # noqa: E402

from readaloud.engine import Voice  # noqa: E402

SAMPLE_RATE = 24000

TEXTS = {
    "first-160": "The quick brown fox jumps over the lazy dog and keeps on going for a while.",
    "mid-400": ("Please select some text in any application and click the round button to hear it "
                "read out loud. Click the button again to stop the reading straight away. " * 3),
    "big-1500": ("This is a longer passage used to measure how quickly the service sends audio for "
                 "a full chunk of text. It is long enough that playing it in real time would take "
                 "over a minute, so we can see whether the service is ahead of the player. " * 12),
}


async def one(voice, label, text, warm):
    marks = []
    t0 = time.time()

    def emit(b):
        marks.append((time.time() - t0, b))

    conn, kind = await voice.take()
    if not warm:
        await conn.close()
        conn, kind = await voice.open(), "cold"
    try:
        await voice._synth_on(conn, text, "+0%", emit)
        err = ""
    except Exception as e:
        err = repr(e)[:50]
    finally:
        try:
            await conn.close()
        except Exception:
            pass
    end = time.time() - t0
    mp3 = b"".join(b for _, b in marks)
    frames = miniaudio.decode(mp3, nchannels=1, sample_rate=SAMPLE_RATE).num_frames if mp3 else 0
    audio = frames / SAMPLE_RATE
    gaps = [b[0] - a[0] for a, b in zip(marks, marks[1:])]
    first = marks[0][0] if marks else float("nan")
    print(f"{label:10} {kind:6} {len(text):>5} chars {len(mp3):>7} B  first {first * 1000:>6.0f}ms  "
          f"last {end * 1000:>6.0f}ms  audio {audio:>6.1f}s  speed {audio / max(end, 0.001):>5.1f}x  "
          f"maxgap {max(gaps, default=0) * 1000:>6.0f}ms  {err}")
    return dict(audio=audio, end=end, first=first, gaps=gaps)


async def main():
    voice = Voice(False)
    print("=== warm connection (what the app uses on the first chunk) ===")
    for label, text in TEXTS.items():
        await one(voice, label, text, warm=True)
    print("=== cold connection (what later chunks get) ===")
    for label, text in TEXTS.items():
        await one(voice, label, text, warm=False)


if __name__ == "__main__":
    asyncio.run(main())
