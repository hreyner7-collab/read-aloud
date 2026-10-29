"""AudioPlayer: streaming, finish, stop, rebuild and one-live-device rules."""
import array
import threading
import time

import pytest

from readaloud import player as P

PCM = array.array("h", [1000] * 2400)          # 0.1 s at 24 kHz
LONG = array.array("h", [2000] * 4800)         # 0.2 s


class FakeDevice:
    def __init__(self):
        self.gen = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self, gen):
        self.gen = gen
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def pull(self, frames=480):
        return self.gen.send(frames)


class Factory:
    def __init__(self, fail_next=0):
        self.devices = []
        self.fail_next = fail_next

    def __call__(self):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("no device")
        dev = FakeDevice()
        self.devices.append(dev)
        return dev


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(P, "STALL_SECONDS", 0.05)
    monkeypatch.setattr(P, "SILENT_END_SECONDS", 0.03)
    monkeypatch.setattr(P, "WATCH_INTERVAL", 0.005)


@pytest.fixture
def made():
    players = []
    yield players
    for p in players:
        try:
            p.stop()
        except Exception:
            pass
        assert P.live_count() == 0


def _player(made, factory, on_first=None, on_finished=None, fast_start=False):
    p = P.AudioPlayer(on_first or (lambda: None), on_finished or (lambda r: None),
                      device_factory=factory, fast_start=fast_start)
    made.append(p)
    return p


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_normal_player_starts_device_on_open(fast, made):
    factory = Factory()
    p = _player(made, factory)
    assert p.open()
    assert factory.devices[0].started       # streaming: ready before audio


def test_fast_start_waits_for_the_first_frames(fast, made):
    factory = Factory()
    sounds = []
    p = _player(made, factory, lambda: sounds.append(1), fast_start=True)
    assert p.open()
    dev = factory.devices[0]
    assert dev.started is False             # no timer running while silent
    p.add(("a", 1), PCM)
    assert wait_for(lambda: dev.started)
    assert list(dev.pull(2400)) == list(PCM)
    assert sounds == [1]


def test_callback_honours_each_requested_buffer_size(fast, made):
    factory = Factory()
    p = _player(made, factory)
    assert p.open()
    p.add(("a", 1), LONG)
    for frames in (480, 240, 960, 120):
        assert len(factory.devices[0].pull(frames)) == frames
    assert p.frames_played == 1800


def test_final_samples_drain_before_finished(fast, made):
    factory, reasons = Factory(), []
    p = _player(made, factory, on_finished=reasons.append)
    assert p.open()
    p.add(("a", 1), PCM)
    p.end()
    assert list(factory.devices[0].pull(2400)) == list(PCM)
    assert reasons == []
    p._check()
    assert reasons == []
    assert wait_for(lambda: reasons == ["finished"])


def test_deferred_start_and_close_share_the_audio_thread(fast, made):
    entered, release = threading.Event(), threading.Event()
    events = []

    class Device(FakeDevice):
        def start(self, gen):
            events.append(("start", threading.get_ident()))
            entered.set()
            assert release.wait(2)
            assert not self.closed
            super().start(gen)

        def close(self):
            events.append(("close", threading.get_ident()))
            super().close()

    dev = Device()
    p = _player(made, lambda: dev, fast_start=True)
    assert p.open()
    p.add(("a", 1), PCM)
    assert entered.wait(1)
    stopper = threading.Thread(target=p.stop)
    stopper.start()
    try:
        assert not dev.closed
    finally:
        release.set()
        stopper.join(2)
    assert not stopper.is_alive()
    assert [event[0] for event in events] == ["start", "close"]
    assert events[0][1] == events[1][1] == p._audio.ident


def test_windows_device_prefers_directsound(monkeypatch):
    import sys
    import types

    created = []

    def PlaybackDevice(**kw):
        created.append(kw)
        return object()

    fake = types.SimpleNamespace(
        Backend=types.SimpleNamespace(DSOUND="dsound", WASAPI="wasapi"),
        PlaybackDevice=PlaybackDevice)
    monkeypatch.setitem(sys.modules, "miniaudio", fake)
    monkeypatch.setattr(P, "AUDIO_BACKEND", None)
    monkeypatch.setattr(sys, "platform", "win32")

    P._new_device()
    assert created[0]["backends"] == ["dsound"]
    assert created[0]["buffersize_msec"] == P.BUFFER_MS


def test_windows_device_falls_back_when_directsound_fails(monkeypatch):
    import sys
    import types

    calls = []

    def PlaybackDevice(**kw):
        calls.append(kw)
        if "backends" in kw:
            raise RuntimeError("no dsound device")
        return object()

    fake = types.SimpleNamespace(
        Backend=types.SimpleNamespace(DSOUND="dsound", WASAPI="wasapi"),
        PlaybackDevice=PlaybackDevice)
    monkeypatch.setitem(sys.modules, "miniaudio", fake)
    monkeypatch.setattr(P, "AUDIO_BACKEND", None)
    monkeypatch.setattr(sys, "platform", "win32")

    P._new_device(12)
    assert [c.get("backends") for c in calls] == [["dsound"], None]
    assert calls[1]["buffersize_msec"] == 12


def test_first_sound_as_soon_as_frames_arrive(fast, made):
    factory = Factory()
    sounds, finished = [], []
    p = _player(made, factory, lambda: sounds.append(1), finished.append)
    assert p.open()
    assert P.live_count() == 1
    p.add(("a", 1), PCM)
    out = factory.devices[0].pull(2400)
    assert len(out) == 2400 and list(out) == list(PCM)
    assert sounds == [1]
    assert finished == []
    assert p.played("a") == 2400


def test_finish_is_idempotent(fast, made):
    factory = Factory()
    reasons = []
    p = _player(made, factory, on_finished=reasons.append)
    assert p.open()
    p.add(("a", 1), PCM)
    factory.devices[0].pull(2400)
    p.end()
    p.finish()
    p.finish()
    assert reasons == ["finished"]
    p.stop()
    assert P.live_count() == 0
    assert factory.devices[0].closed


def test_silent_tail_finishes_the_read(fast, made):
    factory = Factory()
    reasons = []
    p = _player(made, factory, on_finished=reasons.append)
    assert p.open()
    p.add(("a", 1), PCM)
    factory.devices[0].pull(2400)
    start = time.monotonic()
    p.end()
    assert wait_for(lambda: reasons == ["finished"], 1.0)
    assert time.monotonic() - start < 0.5


def test_stop_closes_now_and_ignores_late_audio(fast, made):
    factory = Factory()
    reasons = []
    p = _player(made, factory, on_finished=reasons.append)
    assert p.open()
    p.add(("a", 1), PCM)
    factory.devices[0].pull(480)
    assert p.stop() is True
    assert reasons == ["stopped"]
    assert P.live_count() == 0
    assert factory.devices[0].closed
    p.add(("a", 1), PCM)
    assert p.queued_seconds() == 0
    p.stop()
    assert reasons == ["stopped"]


def test_first_sound_callback_exception_does_not_kill_audio(fast, made):
    factory = Factory()

    def boom():
        raise RuntimeError("callback")

    p = _player(made, factory, boom)
    assert p.open()
    p.add(("a", 1), PCM)
    out = factory.devices[0].pull(480)
    assert len(out) == 480
    assert p.frames_played == 480


def test_open_failure_reports_false_and_leaves_no_device(fast, made):
    factory = Factory(fail_next=1)
    p = _player(made, factory)
    assert p.open() is False
    assert P.live_count() == 0


def test_drop_removes_only_that_attempt(fast, made):
    factory = Factory()
    p = _player(made, factory)
    assert p.open()
    p.add(("a", 1), PCM)
    p.add(("a", 2), PCM)
    p.add(("a", 3), PCM)
    assert p.drop(("a", 2)) == 2400
    assert p.queued_seconds() == pytest.approx(0.2)
    out = factory.devices[0].pull(4800)
    assert len(out) == 4800
    assert p.played("a") == 4800
    assert p.played_attempt(("a", 1)) == 2400


def test_device_loss_rebuilds_and_repeats_at_most_50ms(fast, made):
    factory = Factory()
    reasons = []
    p = _player(made, factory, on_finished=reasons.append)
    assert p.open()
    dev1 = factory.devices[0]
    p.add(("a", 1), PCM)
    played = array.array("h", dev1.pull(480))
    assert len(played) == 480
    # stop consuming: the watchdog must see no progress while frames are queued
    assert wait_for(lambda: len(factory.devices) >= 2, 2.0)
    dev2 = factory.devices[1]
    assert wait_for(lambda: p._q and p._q[0][0] == ("resume", 0), 2.0)
    resume = dev2.pull(480)
    assert len(resume) <= P._RESUME_FRAMES
    assert list(resume) == list(played[-len(resume):])
    assert dev1.closed and dev2.started
    assert P.live_count() == 1
    assert reasons == []


def test_two_failed_rebuilds_end_with_device_lost(fast, made):
    factory = Factory()
    p = _player(made, factory)
    assert p.open()
    p.add(("a", 1), PCM)
    factory.devices[0].pull(480)
    factory.fail_next = 2          # both rebuild attempts fail to open
    assert wait_for(lambda: p.finished, 3.0)
    assert P.live_count() == 0
    p.stop()


def test_g5_hundred_device_losses_rebuild_one_at_a_time(fast, made):
    """CEO gate G5, at the scale the gate asks for.

    The `fast` fixture drops the stall threshold to 50 ms; in the shipped app
    STALL_SECONDS is 1.5 s, so a device that dies *silently* is rebuilt after
    that, while a driver that reports the loss goes through the error path
    immediately.  What is checked here is the invariant, not the wall clock:
    one live device, only the last 50 ms replayed, and exactly one end.
    """
    peak_live = 0
    worst_replay = 0
    worst_gap = 0.0
    for _ in range(100):
        factory = Factory()
        reasons = []
        p = _player(made, factory, on_finished=reasons.append)
        assert p.open()
        p.add(("a", 1), PCM)
        played = list(factory.devices[0].pull(480))
        stall = time.monotonic()               # the device stops taking frames
        assert wait_for(lambda: len(factory.devices) >= 2, 2.0), "no rebuild"
        worst_gap = max(worst_gap, time.monotonic() - stall)
        peak_live = max(peak_live, P.live_count())
        resume = list(factory.devices[1].pull(480))
        worst_replay = max(worst_replay, len(resume))
        assert resume == played[-len(resume):], "rebuilt device replayed new audio"
        p.add(("b", 2), PCM)
        for _ in range(50):                      # one block per pull until drained
            if not p._q:
                break
            factory.devices[1].pull(4800)
        p.end()
        assert wait_for(lambda: reasons == ["finished"], 2.0), reasons
        p.stop()
        assert P.live_count() == 0
    assert peak_live == 1
    assert worst_replay <= P._RESUME_FRAMES == int(24000 * 0.05)


def test_one_live_max_across_many_open_stop_cycles(fast, made):
    peak = 0
    for _ in range(50):
        factory = Factory()
        p = _player(made, factory)
        assert p.open()
        peak = max(peak, P.live_count())
        p.add(("a", 1), PCM)
        factory.devices[0].pull(240)
        p.stop()
        assert P.live_count() == 0
    assert peak == 1
