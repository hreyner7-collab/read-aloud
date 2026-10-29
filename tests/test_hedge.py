"""The fast-path hedge must never cancel the attempt that is winning."""
import asyncio

import readaloud.engine as E


class FakeConn:
    def __init__(self, name):
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True


def test_hedge_failure_does_not_cancel_streaming_winner(monkeypatch):
    async def run():
        monkeypatch.setattr(E, "HEDGE_AFTER", 0.01)
        voice = E.Voice()
        warm, hedge = FakeConn("warm"), FakeConn("hedge")
        hedge_open = asyncio.Event()

        async def take():
            return warm, "warm"

        async def open_():
            hedge_open.set()
            return hedge

        async def synth_on(conn, _voice, _text, _rate, emit):
            if conn is warm:
                await hedge_open.wait()
                emit(b"warm-one")
                await asyncio.sleep(0.05)     # still streaming when hedge dies
                emit(b"warm-two")
            else:
                await asyncio.sleep(0.01)
                raise ConnectionError("hedge failed")

        voice.take = take
        voice.open = open_
        voice._synth_on = synth_on
        voice.give_back = lambda _conn: None
        voice.refill = lambda: None
        got = []
        await asyncio.wait_for(voice.synth("one atom", "+0%", got.append), 2)
        await asyncio.sleep(0)                # let the loser's close() run
        return got, warm.closed, hedge.closed

    got, warm_closed, hedge_closed = asyncio.run(run())
    assert got == [b"warm-one", b"warm-two"]
    assert hedge_closed
    assert not warm_closed


def test_audio_requires_turn_end_before_success():
    import types
    import pytest

    async def run(complete):
        voice = E.Voice()
        queue = asyncio.Queue()

        async def send(_data):
            pass

        conn = types.SimpleNamespace(ws=types.SimpleNamespace(send_str=send), q=queue)
        headers = b"X-RequestId:test\r\nPath:audio\r\nContent-Type:audio/mpeg\r\n"
        packet = len(headers).to_bytes(2, "big") + headers + b"partial-mp3"
        queue.put_nowait(types.SimpleNamespace(type=voice.aiohttp.WSMsgType.BINARY, data=packet))
        if complete:
            queue.put_nowait(types.SimpleNamespace(type=voice.aiohttp.WSMsgType.TEXT,
                                                   data="Path:turn.end\r\n"))
        else:
            queue.put_nowait(None)
        received = []
        await voice._synth_on(conn, "en-GB-SoniaNeural", "Test text.", "+0%", received.append)
        return received

    assert asyncio.run(run(True)) == [b"partial-mp3"]
    with pytest.raises(E.Truncated):
        asyncio.run(run(False))


def test_disabling_warm_pool_during_open_closes_new_spare():
    async def run():
        voice = E.Voice()
        conn = FakeConn("late")

        async def open_():
            voice.set_keep_warm(False)
            return conn

        voice.open = open_
        await voice._refill()
        assert conn.closed
        assert voice.spares == []
    asyncio.run(run())
