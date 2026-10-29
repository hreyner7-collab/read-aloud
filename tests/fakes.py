"""Deterministic fakes for the session/player tests (no device, no network)."""
import array
import asyncio
import collections
import concurrent.futures
import threading
import time

from readaloud import session as S
from readaloud.cache import AudioCache

PCM = array.array("h", [1200] * 2400)      # 0.1 s


class FakeDecoder:
    """Treats each fed byte as 10 PCM frames; enough for ledger accounting."""

    active = 0
    max_active = 0
    fail_all = False
    _lock = threading.Lock()

    def __init__(self, token, emit):
        self.token = token
        self.emit = emit
        self.cancelled = False
        self.error = "decode failed" if FakeDecoder.fail_all else None
        self.done = threading.Event()
        with FakeDecoder._lock:
            FakeDecoder.active += 1
            FakeDecoder.max_active = max(FakeDecoder.max_active, FakeDecoder.active)

    def feed(self, data):
        if self.cancelled:
            return
        if data == b"garbage":
            self.error = "decode failed"
            return
        self.emit(array.array("h", [1000] * (len(data) * 10)))

    def end(self):
        self.done.set()
        with FakeDecoder._lock:
            FakeDecoder.active -= 1

    def cancel(self):
        if not self.cancelled:
            self.cancelled = True
            self.end()


class FakePlayer:
    """An audio device that can play instantly or only when the read ends."""

    def __init__(self, factory, on_first_sound, on_finished, fast_start=False):
        self.factory = factory
        self.on_first_sound = on_first_sound
        self.on_finished = on_finished
        self.fast_start = fast_start
        self.q = collections.deque()
        self._played = collections.Counter()
        self.frames_played = 0
        self.stopped = False
        self.finished = False
        self.ended = False
        self.opened = False
        self.open_count = 0

    def open(self, timeout=3.0):
        self.open_count += 1
        if not self.factory.open_ok:
            return False
        self.opened = True
        with self.factory.lock:
            self.factory.live += 1
            self.factory.max_live = max(self.factory.max_live, self.factory.live)
        return True

    def add(self, token, pcm):
        if self.stopped or self.finished or token in self.factory.dropped:
            return
        self.factory.added.append(token)
        if self.factory.consume:
            self._play(token, pcm)
        else:
            self.q.append((token, pcm))

    def drop(self, token):
        self.factory.dropped.add(token)
        self.q = collections.deque((t, p) for t, p in self.q if t != token)

    def _play(self, token, pcm):
        self.frames_played += len(pcm)
        self._played[token[0]] += len(pcm)
        self.factory.total_played += len(pcm)
        if not self.factory.first_sound:
            self.factory.first_sound = True
            self.on_first_sound()

    def played(self, atom_index):
        return self._played.get(atom_index, 0)

    def queued_seconds(self):
        return sum(len(p) for _t, p in self.q) / 24000

    def end(self):
        self.ended = True
        while self.q:
            token, pcm = self.q.popleft()
            self._play(token, pcm)
        self._finish("finished")

    def stop(self, timeout=3.0):
        if self.finished:
            self.stopped = True
            return True
        self.stopped = True
        self.q.clear()
        self._finish("stopped")
        return True

    def lose_device(self):
        self.q.clear()
        self._finish("device lost")

    def _finish(self, reason):
        if self.finished:
            return
        self.finished = True
        if self.opened:
            with self.factory.lock:
                self.factory.live -= 1
        self.on_finished(reason)


class PlayerFactory:
    def __init__(self, consume=True, open_ok=True):
        self.consume = consume
        self.open_ok = open_ok
        self.lock = threading.Lock()
        self.live = 0
        self.max_live = 0
        self.dropped = set()
        self.added = []
        self.total_played = 0
        self.first_sound = False
        self.players = []

    def __call__(self, on_first_sound, on_finished, fast_start=False):
        p = FakePlayer(self, on_first_sound, on_finished, fast_start)
        self.players.append(p)
        return p


class FakeLocal:
    def __init__(self, ready=True, fail=False):
        self.ready = ready
        self.fail = fail
        self.synth_calls = 0
        self.repairs = []

    def request_repair(self, why="", force=False):
        self.repairs.append(why)

    def synth(self, text, voice, rate):
        self.synth_calls += 1
        if self.fail:
            raise RuntimeError("offline load failed")
        return PCM


class FakeVoice:
    """Behaviour per atom text: ok / fail / trunc / stall / edge_fail."""

    def __init__(self):
        self.behaviour = {}
        self.calls = []
        self.first_packet_pause = 0.0

    async def synth(self, text, rate, emit, stats=None, voice=None):
        self.calls.append(("fast", text))
        if self.first_packet_pause:
            await asyncio.sleep(self.first_packet_pause)
        b = self.behaviour.get(text, "ok")
        if b == "stall":
            await asyncio.sleep(30)
        if b in ("fail", "edge_fail"):
            raise ConnectionError("simulated 403")
        if b == "trunc":
            emit(b"partial-audio")
            raise ConnectionError("simulated reset mid-atom")
        if b in ("bad_decode", "all_bad"):
            emit(b"garbage")
            return
        emit(b"packet-one")
        emit(b"packet-two")

    async def synth_fallback(self, text, rate, emit, voice=None):
        self.calls.append(("edge", text))
        b = self.behaviour.get(text, "ok")
        if b in ("edge_fail", "stall"):
            raise ConnectionError("simulated edge failure")
        if b == "trunc":
            emit(b"partial-audio")
            raise ConnectionError("simulated edge reset")
        if b in ("bad_decode", "all_bad"):
            emit(b"garbage")
            return
        emit(b"edge-audio")


class FakeEngine:
    def __init__(self, local=None, voice=None):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
        self.gen = 1
        self.voice = voice or FakeVoice()
        self.local = local if local is not None else FakeLocal(True)
        self.local_pool = concurrent.futures.ThreadPoolExecutor(2)
        self.cache = AudioCache(max_ram=4 * 1024 * 1024)
        self.last_ledger = None
        self._fast_failures = 0
        self._circuit_until = 0.0
        self._edge_failures = 0
        self._edge_until = 0.0
        self._offline_future = None

    def begin_read(self):
        pass

    def note_fast_failure(self):
        self._fast_failures += 1
        if self._fast_failures >= 3:
            self._circuit_until = time.time() + 60

    def note_fast_success(self):
        self._fast_failures = 0

    def fast_circuit_open(self):
        return time.time() < self._circuit_until

    def note_edge_failure(self):
        self._edge_failures += 1
        if self._edge_failures >= 2:
            self._edge_until = time.time() + 60

    def note_edge_success(self):
        self._edge_failures = 0

    def edge_circuit_open(self):
        return time.time() < self._edge_until

    def offline_voice_id(self, voice):
        return "kokoro:" + voice

    def offline_future(self):
        return None


def fast_session(monkeypatch, engine, text, offline_only=False, consume=True,
                 on_state=None, open_ok=True):
    monkeypatch.setattr(S, "Decoder", FakeDecoder)
    monkeypatch.setattr(S, "ATOM_MIN_SECONDS", 0.05)
    monkeypatch.setattr(S, "ATOM_TIMEOUT_SCALE", 0.01)
    monkeypatch.setattr(S, "DEADLINE_MIN_SECONDS", 2.0)
    monkeypatch.setattr(S, "DEADLINE_SCALE", 0.01)
    factory = PlayerFactory(consume, open_ok=open_ok)
    states = []

    def _state(state, detail="", gen=1):
        states.append((state, detail))

    session = S.Session(engine, text, 1, "en-GB-SoniaNeural", "+0%", offline_only,
                        on_state or _state, player_factory=factory)
    return session, factory, states
