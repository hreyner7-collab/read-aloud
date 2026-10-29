"""A complete read gets its own playback window after a slow start."""
import asyncio
import threading
import time

from readaloud import session as S
from tests.fakes import FakeDecoder, FakeEngine, PlayerFactory


class SlowEndPlayerFactory(PlayerFactory):
    """Playback starts normally, but the device needs 1.2 s to report its end."""

    def __call__(self, on_first_sound, on_finished, fast_start=False):
        player = super().__call__(on_first_sound, on_finished, fast_start)

        def end():
            player.ended = True

            def finish_later():
                time.sleep(1.2)
                player._finish("finished")

            threading.Thread(target=finish_later, daemon=True).start()

        player.end = end
        player.queued_seconds = lambda: 5.0
        return player


def test_complete_read_is_not_a_timeout_when_playback_outlives_the_deadline(monkeypatch):
    monkeypatch.setattr(S, "Decoder", FakeDecoder)
    monkeypatch.setattr(S, "DEADLINE_MIN_SECONDS", 0.5)
    monkeypatch.setattr(S, "DEADLINE_SCALE", 0.0)

    async def run():
        engine = FakeEngine()          # binds its loop to the running one
        states = []
        factory = SlowEndPlayerFactory(consume=True)
        session = S.Session(
            engine, "One sentence is enough here.", 1, "en-GB-SoniaNeural",
            "+0%", False, lambda s, d="", g=1: states.append((s, d)),
            player_factory=factory)
        await asyncio.wait_for(session.run(), 10)
        return engine, factory, session

    engine, factory, session = asyncio.run(run())
    assert not session.timed_out
    assert session.end_state == "idle"
    assert engine.last_ledger["spoken"] == engine.last_ledger["atoms"]
    assert factory.total_played > 0
