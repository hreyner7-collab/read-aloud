"""PCM playback: one live device, streaming frames, rebuild after device loss.

The decoded frames of the atom being read are appended as they arrive, so sound
starts on the first MP3 packet.  A watchdog notices a dead device (no frame
consumed while audio is queued) and rebuilds it on this player's audio thread,
resuming 50 ms before the last played frame.  `finish()` is idempotent and is
the only way a read reports that playback ended.
"""
import array
import collections
import os
import queue
import sys
import threading
import time

SAMPLE_RATE = 24000
BUFFER_MS = 60
# PCM-only cache hits do not need the network/decoder jitter buffer.
FAST_BUFFER_MS = 12
RETAIN_SECONDS = 2.0
RESUME_BACK_MS = 50
STALL_SECONDS = 1.5
WATCH_INTERVAL = 0.25
SILENT_END_SECONDS = 0.3
OPEN_TIMEOUT = 3.0
MAX_OPEN_FAILURES = 2
AUDIO_BACKEND = os.environ.get("READ_ALOUD_AUDIO_BACKEND")  # e.g. "null"

_RETAIN_FRAMES = int(RETAIN_SECONDS * SAMPLE_RATE)
_RESUME_FRAMES = int(RESUME_BACK_MS * SAMPLE_RATE / 1000)

_LIVE_LOCK = threading.Lock()
_LIVE = set()          # players whose device is open and may emit sound


def live_count():
    with _LIVE_LOCK:
        return len(_LIVE)


def live_players():
    with _LIVE_LOCK:
        return list(_LIVE)


def _new_device(buffer_ms=BUFFER_MS):
    import miniaudio
    kwargs = dict(nchannels=1, sample_rate=SAMPLE_RATE, buffersize_msec=buffer_ms,
                  app_name="Read Aloud")
    if AUDIO_BACKEND:
        kwargs["backends"] = [getattr(miniaudio.Backend, AUDIO_BACKEND.upper())]
        return miniaudio.PlaybackDevice(**kwargs)
    if sys.platform == "win32":
        # WASAPI init costs ~0.6 s on some Realtek systems; DirectSound opens
        # the same device in ~15 ms.  Fall back rather than lose sound.
        try:
            return miniaudio.PlaybackDevice(
                backends=[miniaudio.Backend.DSOUND], **kwargs)
        except Exception:
            pass
    return miniaudio.PlaybackDevice(**kwargs)


def _silence(frames):
    return array.array("h", bytes(2 * frames))


class AudioPlayer:
    """Plays int16 mono PCM at 24 kHz.  All device work happens on `_audio`."""

    def __init__(self, on_first_sound=None, on_finished=None, device_factory=None,
                 fast_start=False):
        self.on_first_sound = on_first_sound or (lambda: None)
        self.on_finished = on_finished or (lambda reason: None)
        buffer_ms = FAST_BUFFER_MS if fast_start else BUFFER_MS
        self._buffer_seconds = buffer_ms / 1000.0
        self._drain_seconds = self._buffer_seconds
        self._factory = device_factory or (lambda: _new_device(buffer_ms))
        self._defer_start = bool(fast_start)
        self._lock = threading.RLock()
        self._cmdq = queue.Queue()
        self._q = collections.deque()          # (token, array, count_frames)
        self._retain = collections.deque()     # arrays of frames already played
        self._retain_frames = 0
        self._queued_frames = 0
        self._frames_played = 0
        self._played = {}                      # token -> frames actually heard
        self._dropped = set()                  # tokens whose audio was discarded
        self._ended = False
        self._stopped = False
        self._finished = False
        self._closed = False
        self._first_sound = False
        self._last_progress = time.monotonic()
        self._device = None
        self._gen = None
        self._device_started = False
        self._failures = 0
        self._rebuilding = False
        self._rebuild_pending = False
        self._empty_since = None
        self._monitor_stop = threading.Event()
        self._audio = threading.Thread(target=self._audio_loop, name="audio",
                                       daemon=True)
        self._audio.start()
        self._monitor = threading.Thread(target=self._monitor_loop, name="audio-monitor",
                                         daemon=True)
        self._monitor.start()

    # ------------------------------------------------------------ lifecycle
    def open(self, timeout=OPEN_TIMEOUT):
        with self._lock:
            if self._device is not None:
                return True
        return self._command("open", timeout)

    def _command(self, name, timeout=OPEN_TIMEOUT):
        ev, box = threading.Event(), {}
        self._cmdq.put((name, ev, box))
        if not ev.wait(timeout):
            return False
        return box.get("ok", False)

    def stop(self, timeout=OPEN_TIMEOUT):
        """Stop emitting now and close the device; safe to call repeatedly."""
        with self._lock:
            self._stopped = True
            self._q.clear()
            self._queued_frames = 0
        if not self._closed:
            self._command("close", timeout)
        self._monitor_stop.set()
        reason = "stopped"
        self._finish(reason)
        return self._closed

    def end(self):
        """The producer has appended everything; finish after the queue drains."""
        with self._lock:
            self._ended = True
            if not self._q:
                self._empty_since = time.monotonic()

    def finish(self, reason="finished"):
        self._finish(reason)

    def _finish(self, reason):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._stopped = reason != "finished"
            if reason != "finished":
                self._q.clear()
                self._queued_frames = 0
        try:
            self.on_finished(reason)
        except Exception:
            pass

    # ------------------------------------------------------------ feeding
    def add(self, token, pcm):
        if not pcm:
            return
        deferred = False
        with self._lock:
            if self._stopped or self._finished or token in self._dropped:
                return
            self._q.append((token, pcm, True))
            self._queued_frames += len(pcm)
            deferred = (self._defer_start and not self._device_started
                        and self._device is not None)
        if deferred:
            self._cmdq.put(("start", threading.Event(), {}))

    def drop(self, token):
        """Throw away frames of `token` that were never heard (a retry)."""
        with self._lock:
            self._dropped.add(token)
            kept = collections.deque()
            removed = 0
            for item in self._q:
                if item[0] == token:
                    removed += len(item[1])
                else:
                    kept.append(item)
            self._q = kept
            self._queued_frames -= removed
            return removed

    def played(self, atom_index):
        with self._lock:
            return sum(n for (i, _attempt), n in self._played.items() if i == atom_index)

    def played_attempt(self, token):
        with self._lock:
            return self._played.get(token, 0)

    def queued_seconds(self):
        with self._lock:
            return self._queued_frames / SAMPLE_RATE

    @property
    def frames_played(self):
        with self._lock:
            return self._frames_played

    @property
    def finished(self):
        return self._finished

    @property
    def stopped(self):
        return self._stopped

    # ------------------------------------------------------------ audio thread
    def _audio_loop(self):
        while True:
            name, ev, box = self._cmdq.get()
            if name == "close":
                self._close_device()
                self._closed = True
                box["ok"] = True
                ev.set()
                return
            if name == "open":
                box["ok"] = self._open_device()
                ev.set()
            elif name == "rebuild":
                self._rebuild_pending = False
                self._rebuild()
                box["ok"] = True
                ev.set()
            elif name == "start":
                self._start_device()
                ev.set()

    def _open_device(self):
        dev = None
        try:
            dev = self._factory()
            gen = self._callback()
            next(gen)
        except Exception:
            try:
                if dev is not None:
                    dev.stop()
                    dev.close()
            except Exception:
                pass
            self._device = None
            with _LIVE_LOCK:
                _LIVE.discard(self)
            return False
        self._device, self._gen = dev, gen
        self._device_started = False
        self._last_progress = time.monotonic()
        with _LIVE_LOCK:
            _LIVE.add(self)
        if not self._rebuilding and (not self._defer_start or self._q):
            self._start_device()
        return not self._finished and not self._stopped

    def _start_device(self):
        """Fast start: begin the device only once frames are queued."""
        with self._lock:
            if (self._device_started or self._device is None
                    or self._stopped or self._closed or self._finished):
                return
            dev, gen = self._device, self._gen
            self._device_started = True
        try:
            dev.start(gen)
        except Exception:
            self._close_device()
            self._finish("device lost")

    def _close_device(self):
        dev, self._device = self._device, None
        self._device_started = False
        if dev is not None:
            for op in (dev.stop, dev.close):
                try:
                    op()
                except Exception:
                    pass
        self._gen = None
        with _LIVE_LOCK:
            _LIVE.discard(self)

    def _resume_position(self):
        """Last <=50 ms of already played audio, oldest first."""
        with self._lock:
            chunks, total = [], 0
            for a in reversed(self._retain):
                if total >= _RESUME_FRAMES:
                    break
                chunks.append(a)
                total += len(a)
            if not chunks:
                return None
            chunks.reverse()
            out = array.array("h")
            for a in chunks:
                out.extend(a)
            if len(out) > _RESUME_FRAMES:
                out = out[-_RESUME_FRAMES:]
            return out

    def _rebuild(self):
        self._rebuilding = True
        try:
            resume = self._resume_position()
            for _ in range(2):
                self._close_device()
                if self._open_device():
                    self._failures = 0
                    if resume is not None:
                        with self._lock:
                            if not self._stopped:
                                self._q.appendleft((("resume", 0), resume, False))
                    self._start_device()
                    return
                self._failures += 1
                if self._failures >= MAX_OPEN_FAILURES:
                    break
            with self._lock:
                self._stopped = True
                self._q.clear()
                self._queued_frames = 0
            self._close_device()
            self._finish("device lost")
        finally:
            self._rebuilding = False

    def _monitor_loop(self):
        while not self._monitor_stop.wait(WATCH_INTERVAL):
            self._check()

    def _check(self):
        if self._closed or self._finished or self._stopped:
            return
        now = time.monotonic()
        with self._lock:
            stalled = (self._device is not None and self._q
                       and not self._rebuilding and not self._rebuild_pending
                       and now - self._last_progress > STALL_SECONDS)
            silent_end = (self._ended and not self._q and self._empty_since
                          and now - self._empty_since > max(
                              SILENT_END_SECONDS, self._drain_seconds))
        if stalled:
            self._rebuild_pending = True
            self._cmdq.put(("rebuild", threading.Event(), {}))
            self._last_progress = now
        elif silent_end:
            self._finish("finished")

    # ------------------------------------------------------------ callback
    def _callback(self):
        need = yield b""
        while True:
            need = yield self._fill(need)

    def _fill(self, need):
        with self._lock:
            if self._stopped:
                return _silence(need)
            out = array.array("h")
            played = 0
            while len(out) < need and self._q:
                token, arr, counts = self._q[0]
                take = min(len(arr), need - len(out))
                part = arr[:take]
                out.extend(part)
                if take == len(arr):
                    self._q.popleft()
                else:
                    self._q[0] = (token, arr[take:], counts)
                if counts:
                    played += take
                    self._queued_frames -= take
                    self._frames_played += take
                    self._played[token] = self._played.get(token, 0) + take
                    self._retain.append(part)
                    self._retain_frames += take
                    while self._retain_frames > _RETAIN_FRAMES and self._retain:
                        old = self._retain.popleft()
                        self._retain_frames -= len(old)
            if played:
                self._last_progress = time.monotonic()
                self._empty_since = None
                self._drain_seconds = need / SAMPLE_RATE + self._buffer_seconds
            first = played and not self._first_sound
            if first:
                self._first_sound = True
            empty_end = self._ended and not self._q and not self._finished
            if empty_end:
                self._empty_since = self._empty_since or time.monotonic()
        if first:
            try:
                self.on_first_sound()
            except Exception:
                pass
        if len(out) < need:
            out.extend(_silence(need - len(out)))
        return out


# Old name, kept for the discovery harnesses.
StreamPlayer = AudioPlayer
