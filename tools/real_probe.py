"""Zoom in: why does playback go silent? Logs buffer depth, feed arrivals, stalls.

Usage:  python tools/real_probe.py [chars]
"""
import array
import os
import sys
import threading
import time  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiotap  # noqa: E402

audiotap.make_device()

from readaloud import engine as eng  # noqa: E402
from readaloud.engine import Engine  # noqa: E402

T0 = [time.perf_counter()]
EVENTS = []      # (t, kind, detail)
LOCK = threading.Lock()


def stamp():
    return time.perf_counter() - T0[0]


def note(kind, detail, t=None):
    with LOCK:
        EVENTS.append((stamp() if t is None else t, kind, detail))


class ProbePlayer(eng.StreamPlayer):
    """Same as the real player, but records buffer depth at every audio pull."""

    LAST = []
    FEEDS = [0]

    def __init__(self, *a, **kw):
        self.frames_out = 0
        self.frames_silent = 0
        self.bytes_in = 0
        self.depth_log = []
        super().__init__(*a, **kw)
        ProbePlayer.LAST.append(self)

    def feed(self, data):
        self.bytes_in += len(data)
        ProbePlayer.FEEDS[0] += 1
        if ProbePlayer.FEEDS[0] in (1, 2, 3, 500, 501, 1000, 1500, 2000, 2500, 3000, 4000, 5000):
            note("feed", f"#{ProbePlayer.FEEDS[0]} {len(data)}B src_q={len(self.src.q)}")
        super().feed(data)

    def end(self):
        note("feed_end", "all bytes handed over")
        super().end()

    def _decode(self):
        try:
            for frames in self.ma.stream_any(
                self._src, self.ma.FileFormat.MP3, nchannels=1,
                sample_rate=eng.SAMPLE_RATE, frames_to_read=480,
            ):
                if self.stopped:
                    return
                self.pcm.append(frames)
        except Exception as e:
            if not self.stopped:
                eng.log(f"decoder error: {e!r}")
        finally:
            self.decoded_all = True
            note("decoded_all", f"pcm={len(self.pcm)}")

    def _generator(self):
        need = yield b""
        pending = array.array("h")
        started = finished = False
        # Small prefill so the device opens on real audio, then a deep cushion
        # absorbs network hiccups between chunks for the rest of the read.
        while len(pending) < eng.MIN_PREFILL_FRAMES and not self.stopped:
            if self.pcm:
                pending.extend(self.pcm.popleft())
            elif self.decoded_all:
                break
            else:
                time.sleep(0.005)
        if pending and not started:
            started = True
            self.on_first_sound()
        while True:
            # Top the cushion up from decoded PCM whenever there is headroom.
            while len(pending) < eng.CUSHION_FRAMES and self.pcm:
                pending.extend(self.pcm.popleft())
            if len(pending) < need:
                if self.decoded_all and not self.pcm and not finished:
                    finished = True
                    self.on_finished()
                # Underflow: pad with silence so the device always gets a
                # full callback; playback resumes when the next chunk lands.
                pending.extend([0] * (need - len(pending)))
            out, pending = pending[:need], pending[need:]
            self.frames_out += need
            self.depth_log.append((stamp(), len(self.pcm), self.frames_out / eng.SAMPLE_RATE))
            need = yield out

    def stop(self):
        note("player_stop", f"played {self.frames_out / eng.SAMPLE_RATE:.1f}s, "
                            f"silent {self.frames_silent / eng.SAMPLE_RATE:.2f}s")
        super().stop()


eng.StreamPlayer = ProbePlayer

SAMPLE = (
    "Read Aloud turns whatever you have selected into speech using the same natural voices as "
    "Microsoft Edge. You select a sentence in a document, a paragraph in an email, or a whole "
    "chapter of a book, and the button reads it back to you in a clear voice. "
)


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    text = (SAMPLE * 12).strip()[:want]
    done = threading.Event()
    engine = Engine(lambda s: done.set() if s in ("idle", "error") else None)
    T0[0] = time.perf_counter()
    engine.speak(text)
    done.wait(300)
    time.sleep(0.2)
    tag = audiotap.last().report()
    print("\n--- timeline (seconds from click) ---")
    for t, kind, detail in EVENTS:
        print(f"{t:7.2f}  {kind:12} {detail}")
    print("\n--- stalls in the played audio ---")
    print(f"pauses: {tag['pauses']}  total {tag['pause_total_s']:.2f}s  max {tag['pause_max_s']:.2f}s")
    print(f"lead-in {tag['lead_in_s']:.2f}s  speech {tag['speech_s']:.1f}s")
    print(tag["pauses_detail"])
    for p in ProbePlayer.LAST:
        print(f"player: bytes_in={p.bytes_in}  played={p.frames_out / eng.SAMPLE_RATE:.1f}s  "
              f"silent={p.frames_silent / eng.SAMPLE_RATE:.2f}s  "
              f"pcm_left={len(p.pcm)}x480 frames")
    tap = audiotap.last()
    print(f"tap: callbacks={len(tap.times)}  played_items={len(tap.played)}  "
          f"last_t={tap.times[-1] if tap.times else -1:.2f}s  devices={len(audiotap.INSTANCES)}")


if __name__ == "__main__":
    main()
