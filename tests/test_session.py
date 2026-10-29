"""Session behaviour: end states, losses, stop, cache and audio ordering."""
import asyncio
import time

from readaloud import session as S
from tests.fakes import PCM, FakeEngine, FakeLocal, PlayerFactory, fast_session

TEXT = ("One short sentence here. A second sentence follows it! "
        "And a third question? Finally a calm closing line.")


def _run(coro):
    return asyncio.run(coro)


async def _read(monkeypatch, text=TEXT, offline_only=False, consume=True,
                open_ok=True, local_ready=True):
    engine = FakeEngine(local=FakeLocal(ready=local_ready))
    session, factory, states = fast_session(
        monkeypatch, engine, text, offline_only=offline_only,
        consume=consume, open_ok=open_ok)
    await asyncio.wait_for(session.run(), 10)
    return session, engine, factory, states


def test_offline_only_missing_refuses_without_network(monkeypatch):
    session, engine, factory, states = _run(
        _read(monkeypatch, offline_only=True, local_ready=False))
    assert engine.voice.calls == []
    assert session.requests == 0
    assert factory.players == []
    assert states == [("offline_missing", "download")]
    ledger = engine.last_ledger
    assert ledger["end"] == "offline_missing"
    assert ledger["spoken"] == ledger["partial"] == ledger["skipped"] == 0


def test_offline_only_uses_local_voice_only(monkeypatch):
    session, engine, factory, states = _run(_read(monkeypatch, offline_only=True))
    ledger = engine.last_ledger
    assert engine.voice.calls == []
    assert ledger["end"] == "idle" and ledger["complete"]
    assert session.requests == len(session.atoms)
    assert session.engine.local.synth_calls == len(session.atoms)
    assert factory.total_played > 0


def test_zero_frame_failure_is_error_with_offsets(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=False))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        for atom in session.atoms:
            engine.voice.behaviour[atom.text] = "edge_fail"
        await asyncio.wait_for(session.run(), 10)
        return session, engine, factory, states

    session, engine, factory, states = _run(run())
    ledger = engine.last_ledger
    assert ledger["end"] == "error", ledger
    assert ledger["warning"] == ""
    assert ledger["skipped"] == ledger["atoms"]
    assert len(ledger["offsets"]) == ledger["atoms"]
    assert all(row["reason"] for row in ledger["offsets"])
    assert [row["start"] for row in ledger["offsets"]] == [
        row["start"] for row in session.ledger.rows]
    assert factory.total_played == 0
    assert states[-1][0] == "error" and states[-1][1]


def test_partial_loss_warns_and_keeps_offsets(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        victim = 0                      # only the head atom can be mid-playback
        engine.voice.behaviour[session.atoms[victim].text] = "trunc"
        await asyncio.wait_for(session.run(), 10)
        return session, engine, factory, states, victim

    session, engine, factory, states, victim = _run(run())
    ledger = engine.last_ledger
    assert ledger["end"] == "idle"
    assert ledger["warning"] == "Part of the last read was skipped."
    assert ledger["partial"] == 1 and ledger["skipped"] == 0
    assert ledger["offsets"] == [{
        "status": S.PARTIAL,
        "start": session.atoms[victim].start,
        "end": session.atoms[victim].end,
        "reason": ledger["offsets"][0]["reason"],
    }]
    assert ledger["offsets"][0]["reason"]
    # the half-heard atom is never replayed
    assert factory.players[-1]._played[victim] == len(b"partial-audio") * 10
    assert factory.players[-1]._played[victim + 1] > 0


def test_stop_mid_read_marks_cancelled_without_end_state(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        engine.voice.first_packet_pause = 0.12
        task = asyncio.create_task(session.run())
        while not any(r["status"] == S.SPOKEN for r in session.ledger.rows):
            assert not task.done(), "read finished before it could be stopped"
            await asyncio.sleep(0.005)
        engine.gen = 2                     # controller bumps the generation
        before = list(states)
        started = time.monotonic()
        session.player.stop()               # and closes the device
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        elapsed = time.monotonic() - started
        return session, engine, states, before, elapsed

    session, engine, states, before, elapsed = _run(run())
    assert elapsed < 0.6, f"stop took {elapsed:.2f}s"
    assert states == before, f"state emitted after stop: {states[len(before):]}"
    ledger = engine.last_ledger
    assert ledger["end"] == "cancelled"
    assert ledger["cancelled"] > 0
    assert ledger["partial"] == ledger["skipped"] == 0


def test_cancelled_atom_is_marked_and_read_continues(monkeypatch):
    """A synth task cancelled mid-read must be recorded, not skipped past."""
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        orig = S.Session._synth_atom_body

        async def body(self, i):
            if i == 0:
                self._new_sink(0).write_pcm(PCM)
                raise asyncio.CancelledError()
            return await orig(self, i)

        monkeypatch.setattr(S.Session, "_synth_atom_body", body)
        await asyncio.wait_for(session.run(), 10)
        return session, engine, factory, states

    session, engine, factory, states = _run(run())
    ledger = engine.last_ledger
    assert ledger["end"] == "idle", ledger
    assert ledger["warning"] == "Part of the last read was skipped."
    assert ledger["partial"] == 1
    assert ledger["spoken"] == ledger["atoms"] - 1
    assert ledger["cancelled"] == 0          # never silently dropped
    assert ledger["offsets"][0]["reason"]
    assert factory.players[-1].played(1) > 0
    assert states[-1] == ("idle", "Part of the last read was skipped.")


def test_device_lost_is_error(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        engine.voice.first_packet_pause = 0.05
        task = asyncio.create_task(session.run())
        while not any(r["status"] == S.SPOKEN for r in session.ledger.rows):
            assert not task.done(), "read finished before the device was lost"
            await asyncio.sleep(0.005)
        session.player.lose_device()
        await asyncio.wait_for(task, 5)
        return session, engine, states

    session, engine, states = _run(run())
    assert engine.last_ledger["end"] == "error"
    assert states[-1] == ("error", "audio device lost")


def test_player_open_failure_is_error(monkeypatch):
    session, engine, factory, states = _run(_read(monkeypatch, open_ok=False))
    assert states == [("loading", ""), ("error", "audio device unavailable")]
    assert engine.last_ledger["end"] == "error"
    assert engine.voice.calls == []
    assert factory.live == 0


def test_cache_makes_repeat_free(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session1, factory1, _states1 = fast_session(monkeypatch, engine, TEXT)
        await asyncio.wait_for(session1.run(), 10)
        calls = len(engine.voice.calls)
        requests = session1.requests
        session2, _factory2, _states2 = fast_session(monkeypatch, engine, TEXT)
        await asyncio.wait_for(session2.run(), 10)
        return session1, session2, engine, calls, requests

    session1, session2, engine, calls, requests = _run(run())
    assert requests == len(session1.atoms)
    assert len(engine.voice.calls) == calls
    assert session2.requests == 0
    assert engine.last_ledger["complete"]
    assert engine.cache.hits >= len(session1.atoms)


def test_audio_order_matches_ledger(monkeypatch):
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        session, factory, states = fast_session(monkeypatch, engine, TEXT,
                                                consume=False)
        engine.voice.behaviour[session.atoms[0].text] = "fail"
        await asyncio.wait_for(session.run(), 10)
        return session, engine, factory

    session, engine, factory = _run(run())
    order = [token[0] for token in factory.added]
    assert order == sorted(order), f"audio out of order: {order}"
    assert order[:2] == [0, 1]
    assert engine.last_ledger["complete"]


def test_lookahead_decode_failure_is_never_marked_spoken(monkeypatch):
    """A corrupt atom whose audio arrives one atom early must be a loss, not
    silently recorded as SPOKEN (the old late-decode path hid it)."""
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=False))
        session, factory, states = fast_session(monkeypatch, engine, TEXT)
        bad = len(session.atoms) - 1      # synthesized while atom 0 is playing
        engine.voice.behaviour[session.atoms[bad].text] = "all_bad"
        await asyncio.wait_for(session.run(), 10)
        return session, engine, factory, bad

    session, engine, factory, bad = _run(run())
    row = session.ledger.rows[bad]
    assert row["status"] == S.SKIPPED and row["reason"], row
    ledger = engine.last_ledger
    assert any(o["start"] == row["start"] and o["status"] == S.SKIPPED
               for o in ledger["offsets"])
    assert ledger["spoken"] == ledger["atoms"] - 1
    assert ledger["end"] == "idle" and ledger["warning"]


def test_empty_text_is_idle(monkeypatch):
    session, engine, factory, states = _run(
        _read(monkeypatch, text="   \n \t "))
    assert states == [("idle", "")]
    assert engine.last_ledger["atoms"] == 0
    assert engine.last_ledger["end"] == "idle"
    assert factory.players == []


def test_ledger_is_tagged_with_its_own_generation(monkeypatch):
    """The terminal state callback fires before the ledger is written, so a
    build check that waits on 'idle' reads the previous read's numbers.  The
    tag is what lets it tell its own read apart."""
    session, engine, factory, states = _run(_read(monkeypatch))
    assert engine.last_ledger["gen"] == session.gen == 1

    engine.gen = 2
    again = S.Session(engine, "Just one sentence.", 2, "en-GB-SoniaNeural",
                      "+0%", False, lambda *a: None,
                      player_factory=PlayerFactory(consume=True))
    _run(asyncio.wait_for(again.run(), 10))
    assert engine.last_ledger["gen"] == 2
