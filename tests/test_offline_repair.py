"""The offline voice repairs itself without ever deleting a valid model."""
import hashlib
import sys
import time
import types

from readaloud import local_voice as LV

GOOD = b"the published model"
SHA = hashlib.sha256(GOOD).hexdigest()


def make(tmp_path, monkeypatch, body=GOOD, sha=SHA, expected=len(GOOD)):
    path = tmp_path / "kokoro.onnx"
    path.write_bytes(body)
    monkeypatch.setattr(LV, "model_path", lambda: str(path))
    monkeypatch.setattr(LV, "MODEL_BYTES", expected)
    monkeypatch.setattr(LV, "MODEL_SHA256", sha)
    return LV.LocalVoice(), path


def stop(voice):
    voice._stop.set()
    voice._wake.set()
    if voice._supervisor is not None:
        voice._supervisor.join(1.0)


def broken_kokoro(monkeypatch):
    module = types.ModuleType("kokoro_onnx")

    class Kokoro:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("onnxruntime is missing")

    module.Kokoro = Kokoro
    monkeypatch.setitem(sys.modules, "espeakng_loader", types.ModuleType("espeakng_loader"))
    monkeypatch.setitem(sys.modules, "kokoro_onnx", module)


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_wrong_size_is_quarantined_and_downloaded_again(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch, body=b"bad", expected=len(GOOD))
    downloads = []

    def download():
        downloads.append(1)
        path.write_bytes(GOOD)
        voice.download_state = "ready"
        return True

    monkeypatch.setattr(voice, "_download_locked", download)
    assert voice.repair_once() is True
    assert downloads == [1]
    assert (tmp_path / "kokoro.onnx.bad").read_bytes() == b"bad"
    assert voice.ready is True


def test_a_bad_fingerprint_is_quarantined_and_downloaded_again(tmp_path, monkeypatch):
    body = b"x" * len(GOOD)
    voice, path = make(tmp_path, monkeypatch, body=body)
    downloads = []

    def download():
        downloads.append(1)
        path.write_bytes(GOOD)
        voice.download_state = "ready"
        return True

    monkeypatch.setattr(voice, "_download_locked", download)
    assert voice.repair_once() is True
    assert downloads == [1]
    assert (tmp_path / "kokoro.onnx.bad").read_bytes() == body


def test_a_valid_model_that_fails_to_load_is_kept(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch)
    broken_kokoro(monkeypatch)
    try:
        voice.load()
    except RuntimeError:
        pass
    else:
        raise AssertionError("the fake Kokoro should have failed")
    assert path.read_bytes() == GOOD
    assert voice.download_state == "load_failed"
    assert not (tmp_path / "kokoro.onnx.bad").exists()
    assert "repair" in voice.status_text().lower()


def test_a_corrupt_model_is_quarantined_on_a_load_failure(tmp_path, monkeypatch):
    body = b"y" * len(GOOD)
    voice, path = make(tmp_path, monkeypatch, body=body)
    broken_kokoro(monkeypatch)
    try:
        voice.load()
    except RuntimeError:
        pass
    assert not path.exists()
    assert (tmp_path / "kokoro.onnx.bad").read_bytes() == body
    assert voice.download_state == "missing"


def test_the_supervisor_keeps_running_and_repairs_later_damage(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch)
    downloads = []

    def download():
        downloads.append(1)
        path.write_bytes(GOOD)
        voice.download_state = "ready"
        return True

    monkeypatch.setattr(voice, "_download_locked", download)
    voice.download_in_background(delay=0.01, retry_every=0.05)
    assert voice.ready is True                       # the starting file is valid
    path.write_bytes(b"damaged later")
    voice.demote("model file missing or wrong size")
    assert wait_until(lambda: downloads and voice.ready), voice.download_state
    assert voice._supervisor.is_alive()               # not the old one-shot loop
    stop(voice)


def test_manual_retry_does_not_block_and_reports_downloading(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch, body=b"bad", expected=len(GOOD))
    seen = []

    def download():
        seen.append(1)
        path.write_bytes(GOOD)
        voice.download_state = "ready"
        return True

    monkeypatch.setattr(voice, "_download_locked", download)
    assert voice.retry_download() is False
    assert wait_until(lambda: voice.ready)
    assert seen == [1]


def test_supervisor_detects_same_size_corruption_without_load_failure(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch, body=b"x" * len(GOOD))
    downloads = []

    def download():
        downloads.append(1)
        path.write_bytes(GOOD)
        voice.download_state = "ready"
        return True

    monkeypatch.setattr(voice, "_download_locked", download)
    assert voice.ready
    voice.download_in_background(delay=0, retry_every=0.05)
    try:
        assert wait_until(lambda: downloads and voice.ready)
        assert path.read_bytes() == GOOD
    finally:
        stop(voice)


def test_healthy_model_is_verified_without_loading_or_downloading(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch)
    verified = []
    verify = voice.verify

    def verify_once():
        verified.append(1)
        return verify()

    monkeypatch.setattr(voice, "verify", verify_once)
    voice.download_in_background(delay=0, retry_every=0.05)
    try:
        assert wait_until(lambda: verified)
        assert voice.ready and voice.kokoro is None
        assert path.read_bytes() == GOOD
    finally:
        stop(voice)


def test_failed_repair_keeps_valid_bytes_and_demotes(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch)
    broken_kokoro(monkeypatch)
    assert voice.repair_once() is False
    assert not voice.ready
    assert voice.download_state == "load_failed"
    assert path.read_bytes() == GOOD
    assert not (tmp_path / "kokoro.onnx.bad").exists()


def test_missing_model_download_failure_does_not_report_ready(tmp_path, monkeypatch):
    voice, path = make(tmp_path, monkeypatch)
    path.unlink()

    def fail_download():
        voice.download_state = "failed"
        return False

    monkeypatch.setattr(voice, "_download_locked", fail_download)
    assert voice.repair_once() is False
    assert not voice.ready
    assert "retry" in voice.status_text()
