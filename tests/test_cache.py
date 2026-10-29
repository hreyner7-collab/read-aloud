"""AudioCache: privacy, LRU, corruption, disk opt-in and never failing a read."""
import array
import os

import pytest

from readaloud.cache import AudioCache, cache_key

TEXT = "A secret sentence that must never appear on disk."
MP3 = b"\xff\xfb" + bytes(range(256)) * 8
PCM = [array.array("h", [1000] * 2400)]


def test_default_cache_is_memory_only(tmp_path):
    c = AudioCache(disk_dir=str(tmp_path))
    assert c.disk_enabled is False
    key = cache_key("voice", "+0%", TEXT)
    c.put(key, "mp3", MP3)
    assert c.get(key) == ("mp3", MP3)
    assert os.listdir(tmp_path) == []


def test_key_is_a_hash_and_hides_the_text():
    key = cache_key("en-GB-SoniaNeural", "+0%", TEXT)
    assert TEXT not in key
    assert len(key) == 32
    assert key != cache_key("en-GB-SoniaNeural", "+10%", TEXT)


def test_disk_round_trip_after_restart_and_no_text_on_disk(tmp_path):
    key = cache_key("voice", "+0%", TEXT)
    c1 = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    c1.put(key, "mp3", MP3)
    c1.put(cache_key("voice", "+0%", "PCM sentence"), "pcm", PCM)
    names = os.listdir(tmp_path)
    assert names and all(n.startswith("a1-") for n in names)
    blob = b"".join((tmp_path / n).read_bytes() for n in names)
    assert TEXT.encode() not in blob
    assert b"PCM sentence" not in blob

    c2 = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    assert c2.get(key) == ("mp3", MP3)
    assert c2.get(cache_key("voice", "+0%", "PCM sentence"))[0] == "pcm"
    assert c2.get(key)[1] == MP3


def test_lru_evicts_the_oldest_and_counts_bytes(tmp_path):
    c = AudioCache(max_ram=10)
    c.put("a", "mp3", b"123456")
    c.put("b", "mp3", b"123456")
    assert c.get("a") is None
    assert c.get("b") == ("mp3", b"123456")
    assert c.ram_bytes == 6
    c.clear_ram()
    assert c.ram_bytes == 0 and c.get("b") is None


def test_corrupt_disk_file_is_deleted(tmp_path):
    key = cache_key("voice", "+0%", TEXT)
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    c.put(key, "mp3", MP3)
    path = tmp_path / [n for n in os.listdir(tmp_path) if n.startswith("a1-")][0]
    path.write_bytes(b"not a cache file")
    fresh = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    assert fresh.get(key) is None
    assert not path.exists()


def test_header_key_mismatch_is_deleted(tmp_path):
    key = cache_key("voice", "+0%", TEXT)
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    c.put(key, "mp3", MP3)
    path = tmp_path / [n for n in os.listdir(tmp_path) if n.startswith("a1-")][0]
    data = path.read_bytes().replace(key.encode(), b"0" * 32)
    path.write_bytes(data)
    fresh = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    assert fresh.get(key) is None
    assert not path.exists()


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    key = cache_key("voice", "+0%", TEXT)
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    c.put(key, "mp3", MP3)
    original = next(tmp_path / n for n in os.listdir(tmp_path))

    def no_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", no_replace)
    c.put(key, "mp3", b"new bytes")          # RAM updated, disk write fails
    assert original.exists()
    assert original.read_bytes().endswith(MP3)
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_clear_disk_only_removes_cache_files(tmp_path):
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    c.put("a", "mp3", MP3)
    other = tmp_path / "keep-me.txt"
    other.write_text("user file")
    assert c.clear_disk() == 1
    assert other.exists()
    assert c.clear_disk() == 0


def test_disk_failure_never_breaks_put(tmp_path, monkeypatch):
    key = cache_key("voice", "+0%", TEXT)
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)

    def boom(*_a, **_k):
        raise OSError("no space")

    monkeypatch.setattr(c, "_write", boom)
    c.put(key, "mp3", MP3)                    # must not raise
    assert c.get(key) == ("mp3", MP3)


def test_disk_trim_keeps_under_the_cap(tmp_path):
    c = AudioCache(disk_dir=str(tmp_path), disk_enabled=True, max_disk=1000)
    for i in range(10):
        c.put(f"k{i}", "mp3", b"x" * 400)
    assert c.disk_bytes() <= 1000
    fresh = AudioCache(disk_dir=str(tmp_path), disk_enabled=True)
    assert fresh.get("k9") == ("mp3", b"x" * 400)


def test_disk_dir_that_cannot_be_created_disables_disk(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = AudioCache(disk_dir=str(blocker), disk_enabled=True)
    assert c.disk_enabled is False
    c.put("k", "mp3", MP3)                    # RAM still works
    assert c.get("k") == ("mp3", MP3)
