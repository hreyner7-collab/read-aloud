"""Run one real read (real Microsoft service, recorded output) and report pauses.

Usage:  python tools/real_run.py [chars]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiotap  # noqa: E402

audiotap.make_device()

from readaloud.engine import Engine, log, LOG  # noqa: E402

SAMPLE = (
    "Read Aloud turns whatever you have selected into speech using the same natural voices as "
    "Microsoft Edge. You select a sentence in a document, a paragraph in an email, or a whole "
    "chapter of a book, and the button reads it back to you in a clear voice. "
)

TEXT = (SAMPLE * 8).strip()


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    text = TEXT[:want]
    states = []
    done = threading.Event()

    def on_state(s):
        states.append((time.time(), s))
        print(f"  state: {s}")
        if s in ("idle", "error"):
            done.set()

    engine = Engine(on_state)
    t0 = time.time()
    engine.speak(text)
    if not done.wait(300):
        print("timed out")
    time.sleep(0.3)
    tap = audiotap.last()
    rep = tap.report()
    print("\n--- playback report ---")
    for k, v in rep.items():
        print(f"{k}: {v}")
    print(f"wall clock: {time.time() - t0:.1f}s")
    print("--- engine log tail ---")
    try:
        with open(LOG, encoding="utf-8") as f:
            for line in f.readlines()[-6:]:
                print(line.rstrip())
    except OSError as e:
        print(e)


if __name__ == "__main__":
    main()
