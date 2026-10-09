"""Records exactly what the app would play, without needing a sound card.

TapDevice stands in for miniaudio.PlaybackDevice: it pulls samples from the
engine's generator in real time, just like a real output device, and keeps
everything it was handed. Analysis then finds audible pauses (runs of silence
in the middle of speech), which is what the user hears as "it stops and then
continues".
"""
import array
import threading
import time

SAMPLE_RATE = 24000


class TapDevice:
    """Drop-in replacement for miniaudio.PlaybackDevice that records audio."""

    def __init__(self, nchannels=1, sample_rate=SAMPLE_RATE, buffersize_msec=60, **kw):
        self.nchannels = nchannels
        self.sample_rate = sample_rate
        self.period = buffersize_msec / 1000.0
        self.frames_per_cb = max(1, int(sample_rate * self.period))
        self.played = []          # array('h') per callback
        self.times = []           # callback start, seconds since device start
        self.late = []            # how late the generator answered
        self.gap_report = None
        self._stop = threading.Event()
        self._thread = None
        self._t0 = None

    # --- miniaudio.PlaybackDevice surface used by engine.py
    def start(self, gen):
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, args=(gen,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    # --- the real-time pull loop
    def _loop(self, gen):
        deadline = self._t0
        while not self._stop.is_set():
            now = time.perf_counter()
            if now < deadline:
                time.sleep(min(deadline - now, 0.02))
                continue
            try:
                data = gen.send(self.frames_per_cb)
            except StopIteration:
                break
            self.times.append(now - self._t0)
            self.late.append(now - deadline)
            if not isinstance(data, array.array):
                data = array.array("h", data)
            self.played.append(data)
            deadline += self.period

    # --- analysis
    def samples(self):
        out = array.array("h")
        for chunk in self.played:
            out.extend(chunk)
        return out

    def silence_runs(self, min_ms=60):
        """Runs of pure silence, as (start_frame, frames, seconds)."""
        pcm = self.samples()
        runs, start = [], None
        for i, s in enumerate(pcm):
            if s == 0:
                if start is None:
                    start = i
            elif start is not None:
                if (i - start) * 1000 / self.sample_rate >= min_ms:
                    runs.append((start, i - start, (i - start) / self.sample_rate))
                start = None
        if start is not None:
            runs.append((start, len(pcm) - start, (len(pcm) - start) / self.sample_rate))
        return runs

    def report(self, min_ms=60):
        pcm = self.samples()
        runs = self.silence_runs(min_ms)
        speech = [i for i, s in enumerate(pcm) if s != 0]
        first, last = (speech[0], speech[-1]) if speech else (0, 0)
        # pauses that sit between speech (audible dropouts), ignoring the lead-in
        mid = [r for r in runs if r[0] > first and r[0] + r[1] <= last and r[1] < len(pcm) - 1]
        mid = [r for r in mid if r[0] > first]
        return dict(
            total_s=len(pcm) / self.sample_rate,
            lead_in_s=first / self.sample_rate,
            speech_s=(last - first) / self.sample_rate,
            pauses=len(mid),
            pause_total_s=sum(r[2] for r in mid),
            pause_max_s=max([r[2] for r in mid], default=0.0),
            pauses_detail=[(round(r[0] / self.sample_rate, 2), round(r[2], 2)) for r in mid],
            late_callbacks=len([x for x in self.late if x > self.period]),
            max_late_ms=round(max(self.late, default=0) * 1000),
        )


def install(monkeypatch_miniaudio_module=None):
    """Patch miniaudio.PlaybackDevice so engine.py uses TapDevice. Returns the class."""
    import miniaudio
    import readaloud.engine as eng

    class RecordingDevice(TapDevice):
        pass

    miniaudio.PlaybackDevice = RecordingDevice
    eng._tap = None
    return RecordingDevice


INSTANCES = []


def make_device(**kw):
    """Create a TapDevice and remember it for later interrogation."""
    import miniaudio
    if not hasattr(miniaudio, "_real_playback"):
        miniaudio._real_playback = miniaudio.PlaybackDevice

    def factory(*a, **k):
        kw2 = dict(k)
        kw2.update(kw)
        dev = TapDevice(**kw2)
        INSTANCES.append(dev)
        return dev

    miniaudio.PlaybackDevice = factory
    return factory


def last():
    return INSTANCES[-1] if INSTANCES else None
