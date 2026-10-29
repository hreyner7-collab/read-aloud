"""The engine half of 'Offline voice only': one thread-safe switch, one preload."""
import types

import pytest

from readaloud import engine as E


class Loop:
    def __init__(self, closed=False):
        self.calls = 0
        self.closed = closed

    def call_soon_threadsafe(self, fn):
        self.calls += 1
        if self.closed:
            raise RuntimeError("event loop is closed")
        fn()


class Voice:
    def __init__(self):
        self.calls = []

    def set_keep_warm(self, on):
        self.calls.append(("keep_warm", on))

    def refill(self):
        self.calls.append(("refill",))


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(E, "OFFLINE_ONLY", False)
    e = E.Engine.__new__(E.Engine)
    e.loop = Loop()
    e.voice = Voice()
    e.preloads = []
    e.local_pool = "pool"
    e.local = types.SimpleNamespace(preload=e.preloads.append)
    return e


def test_enabling_offline_only_stops_warm_cloud_sockets(engine):
    engine.set_offline_only(True)
    assert E.OFFLINE_ONLY is True
    assert engine.loop.calls == 1                  # ran on the engine's own loop
    assert engine.voice.calls == [("keep_warm", False)]
    assert engine.preloads == ["pool"]             # model starts loading now


def test_turning_it_off_brings_the_online_pool_back(engine):
    engine.set_offline_only(True)
    engine.set_offline_only(False)
    assert E.OFFLINE_ONLY is False
    assert engine.voice.calls[-1] == ("refill",)


def test_a_closed_engine_loop_cannot_break_the_toggle(engine):
    engine.loop = Loop(closed=True)
    engine.set_offline_only(True)                  # must not raise
    assert E.OFFLINE_ONLY is True
