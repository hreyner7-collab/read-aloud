"""C2 / G6: clicking while the engine is still starting must wait, read, or
say so — never crash on the half-built engine.

The real `_run` opens a network connection, so these tests replace only the
startup body: the thread, the event loop, `speak()`, `_start_when_ready()` and
`_start()` are the shipped ones.  The timeouts are patched down so the suite
stays fast; the code paths are the same.
"""
import asyncio
import time

from readaloud import engine as E
from readaloud.engine import Engine


def wait_until(xs, predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(xs):
            return True
        time.sleep(0.01)
    return False


def build(monkeypatch, mode, delay, start_timeout=2.0, patch_start=True):
    """An Engine whose thread becomes ready `delay` seconds after it starts.

    mode: "ok" builds a usable engine, "failed" has a start error,
    "waiting" stays not-ready for the whole delay.
    """
    monkeypatch.setattr(E, "READY_TIMEOUT", 0.2)
    monkeypatch.setattr(E, "START_TIMEOUT", start_timeout)
    states, started = [], []

    def fake_run(self):
        asyncio.set_event_loop(self.loop)
        time.sleep(delay)
        if mode == "ok":
            self.voice, self.local = object(), object()
        elif mode == "failed":
            self._start_error = "PortAudioMissing: no audio device"
        if mode != "waiting":
            self.ready.set()
        self.loop.run_forever()

    monkeypatch.setattr(Engine, "_run", fake_run)
    if patch_start:
        monkeypatch.setattr(Engine, "_start",
                            lambda self, text, voice, rate, offline_only, gen:
                            started.append(text))
    t = time.monotonic()
    engine = Engine(lambda s, d="", g=0: states.append((s, d)),
                    settings={"disk_cache": False})
    return engine, states, started, time.monotonic() - t


def has(states, name):
    return any(s == name for s, _ in states)


def test_init_returns_before_a_slow_start_finishes(monkeypatch):
    """A long start must not hold the UI thread: __init__ gives up after 2 s."""
    engine, states, started, init_s = build(monkeypatch, "ok", 1.2)
    assert init_s < 0.6, init_s
    assert not engine.ready.is_set()          # still starting, and that is known
    engine.stop()


def test_click_during_the_wait_reads_once_the_engine_is_ready(monkeypatch):
    engine, states, started, _ = build(monkeypatch, "ok", 0.6)
    time.sleep(0.2)                            # click 0.2 s in, as G6 asks
    engine.speak("Click before ready.")
    assert wait_until(states, lambda xs: has(xs, "loading")), states
    assert wait_until(started, bool), started
    assert started == ["Click before ready."], started
    assert not has(states, "error"), states
    engine.stop()


def test_a_give_up_wait_is_an_error_not_a_hang(monkeypatch):
    engine, states, started, _ = build(monkeypatch, "waiting", 1.0,
                                       start_timeout=0.3)
    engine.speak("Never started.")
    assert wait_until(states, lambda xs: has(xs, "error")), states
    assert states[-1] == ("error", "still starting"), states
    assert started == []
    engine.stop()


def test_a_failed_start_reports_error_without_reading(monkeypatch):
    """The real `_start` runs here: a start error must not reach a Session."""
    engine, states, started, _ = build(monkeypatch, "failed", 0.05,
                                       patch_start=False)
    assert engine.ready.wait(2)
    engine.speak("Broken audio device.")
    assert wait_until(states, lambda xs: has(xs, "error")), states
    assert states[-1] == ("error", "still starting"), states
    assert started == []
    assert engine.session is None
    engine.stop()


def test_a_stale_generation_never_starts_a_read(monkeypatch):
    """Stop during the wait: the queued `_start` arrives with an old gen."""
    engine, states, started, _ = build(monkeypatch, "ok", 0.05,
                                       patch_start=False)
    assert engine.ready.wait(2)
    stale = engine.gen
    engine.gen += 1                            # what stop()/a new click does
    engine._start("Too late.", "gb", 1.0, False, stale)
    assert engine.session is None
    assert states == []
    engine.stop()
