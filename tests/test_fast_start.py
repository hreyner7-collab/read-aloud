"""Fast-start path: RAM PCM hits skip decode and open a small-buffer device."""
import array
import asyncio

from readaloud.cache import AudioCache
from tests.fakes import FakeEngine, fast_session

TEXT = ("One short sentence here. A second sentence follows it! "
        "And a third question? Finally a calm closing line.")


def _run(coro):
    return asyncio.run(coro)


def test_pcm_put_keeps_compressed_bytes_on_disk(tmp_path):
    cache = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    frames = [array.array("h", [1, 2, 3])]
    cache.put("k", "mp3", b"mp3bytes", pcm=frames)

    assert cache.pcm_cached("k")
    assert cache.get("k") == ("pcm", frames)

    cache.clear_ram()
    assert not cache.pcm_cached("k")
    assert cache.has("k")
    assert cache.get("k") == ("mp3", b"mp3bytes")


def test_second_read_uses_fast_start_and_no_network(monkeypatch):
    async def run():
        engine = FakeEngine()
        first, first_factory, _ = fast_session(monkeypatch, engine, TEXT)
        await asyncio.wait_for(first.run(), 10)
        second, second_factory, _ = fast_session(monkeypatch, engine, TEXT)
        await asyncio.wait_for(second.run(), 10)
        return first, first_factory, second, second_factory

    first, first_factory, second, second_factory = _run(run())
    assert first_factory.players[0].fast_start is False
    assert first.requests == len(first.atoms)
    assert second_factory.players[0].fast_start is True
    assert second.requests == 0
    assert second.engine.last_ledger["complete"]


def test_offline_only_does_not_reuse_a_cloud_voice(monkeypatch):
    async def run():
        engine = FakeEngine()
        first, _, _ = fast_session(monkeypatch, engine, TEXT)
        await first.run()
        network_calls = len(engine.voice.calls)
        offline, factory, _ = fast_session(monkeypatch, engine, TEXT, offline_only=True)
        assert not offline._all_cached()
        await offline.run()
        assert len(engine.voice.calls) == network_calls
        assert engine.local.synth_calls == len(offline.atoms)
        assert not factory.players[0].fast_start
        again, factory2, _ = fast_session(monkeypatch, engine, TEXT, offline_only=True)
        await again.run()
        assert factory2.players[0].fast_start
        assert again.requests == 0
    _run(run())


def test_non_object_disk_header_is_a_cache_miss(tmp_path):
    from readaloud.cache import MAGIC
    for header in (b"[]", b"null", b"42", b'"text"'):
        cache = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
        path = tmp_path / "a1-key.bin"
        path.write_bytes(MAGIC + header + b"\nbytes")
        assert cache.get("key") is None
        assert not path.exists()
