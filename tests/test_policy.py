"""Circuits, rung order and offline-voice policy (no app UI needed)."""
import os
import time

from readaloud import local_voice as LV
from readaloud import session as S
from readaloud.engine import Engine
from tests.fakes import FakeEngine, FakeLocal


def _engine():
    e = object.__new__(Engine)
    e._fast_failures = 0
    e._circuit_until = 0.0
    e._edge_failures = 0
    e._edge_until = 0.0
    e.local = None
    return e


def test_fast_circuit_opens_after_three_and_success_resets_the_count():
    e = _engine()
    e.note_fast_failure()
    e.note_fast_failure()
    assert not e.fast_circuit_open()
    e.note_fast_failure()
    assert e.fast_circuit_open()
    e.note_fast_success()
    assert e._fast_failures == 0


def test_edge_circuit_opens_after_two():
    e = _engine()
    e.note_edge_failure()
    assert not e.edge_circuit_open()
    e.note_edge_failure()
    assert e.edge_circuit_open()
    e.note_edge_success()
    assert e._edge_failures == 0


def _rungs(engine, offline_only=False):
    s = object.__new__(S.Session)
    s.engine = engine
    s.offline_only = offline_only
    return s._rungs()


def test_rung_order_is_fast_edge_offline():
    assert _rungs(FakeEngine(local=FakeLocal(ready=True))) == [
        "fast", "edge", "offline"]


def test_fast_circuit_skips_fast_but_keeps_edge_and_offline():
    e = FakeEngine(local=FakeLocal(ready=True))
    e._circuit_until = time.time() + 60
    assert _rungs(e) == ["edge", "offline"]


def test_edge_circuit_is_skipped_when_the_offline_voice_is_ready():
    e = FakeEngine(local=FakeLocal(ready=True))
    e._edge_until = time.time() + 60
    assert _rungs(e) == ["fast", "offline"]


def test_edge_is_kept_when_the_offline_voice_is_missing():
    e = FakeEngine(local=FakeLocal(ready=False))
    e._edge_until = time.time() + 60
    assert _rungs(e) == ["fast", "edge"]


def test_offline_only_never_lists_a_network_rung():
    assert _rungs(FakeEngine(local=FakeLocal(ready=True)), offline_only=True) == [
        "offline"]


def test_no_rungs_still_returns_a_network_attempt():
    e = FakeEngine(local=FakeLocal(ready=False))
    e._circuit_until = time.time() + 60
    e._edge_until = time.time() + 60
    assert _rungs(e) == ["edge"]


def test_offline_voice_reports_missing_and_demotes_when_the_file_vanishes(
        tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    monkeypatch.setattr(LV, "model_path", lambda: str(model))
    monkeypatch.setattr(LV, "MODEL_BYTES", 4)

    lv = LV.LocalVoice()
    assert lv.ready is False
    assert lv.download_state == "missing"
    assert "not downloaded" in lv.status_text()

    model.write_bytes(b"abcd")
    lv = LV.LocalVoice()
    assert lv.download_state == "ready" and lv.ready is True
    assert "ready" in lv.status_text()

    os.remove(model)
    assert lv.ready is False
    assert lv.download_state == "missing"
    assert lv.last_error
    assert lv.kokoro is None


def test_offline_voice_verify_rejects_the_wrong_size(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"abc")
    monkeypatch.setattr(LV, "model_path", lambda: str(model))
    monkeypatch.setattr(LV, "MODEL_BYTES", 4)
    lv = LV.LocalVoice()
    lv.download_state = "ready"
    assert lv.verify() is False
    assert lv.ready is False


def test_offline_voice_accepts_an_existing_model_without_downloading(
        tmp_path, monkeypatch):
    import hashlib
    model = tmp_path / "model.onnx"
    model.write_bytes(b"abcd")
    monkeypatch.setattr(LV, "model_path", lambda: str(model))
    monkeypatch.setattr(LV, "MODEL_BYTES", 4)
    monkeypatch.setattr(LV, "MODEL_SHA256", hashlib.sha256(b"abcd").hexdigest())
    lv = LV.LocalVoice()
    assert lv.ready is True
    assert lv.retry_download() is True          # no thread, no network
    lv._download()                              # quick path, no network
    assert lv.ready is True
