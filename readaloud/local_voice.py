"""Offline backup voice: Kokoro-82M, running on the computer itself.

Used automatically when Microsoft's voices can't be reached (no internet, or
Microsoft changed something), or always if the user picks "Offline voice only".

The voice model (177 MB) isn't inside the app; it's downloaded once in the
background the first time the app runs, checked against a fingerprint, and kept
in the app's data folder.
"""
import hashlib
import os
import re
import sys
import threading
import time
import types
import urllib.request

from .engine import data_dir, log

MODEL_NAME = "kokoro-v1.0.fp16.onnx"
MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
             "model-files-v1.0/kokoro-v1.0.fp16.onnx")
MODEL_SHA256 = "c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204"
MODEL_BYTES = 177464787

# Microsoft voice -> closest Kokoro voice (Kokoro has no Australian voice)
VOICE_MAP = {
    "en-GB-SoniaNeural": ("bf_emma", "en-gb"),
    "en-GB-RyanNeural": ("bm_george", "en-gb"),
    "en-GB-LibbyNeural": ("bf_isabella", "en-gb"),
    "en-US-AriaNeural": ("af_heart", "en-us"),
    "en-US-GuyNeural": ("am_michael", "en-us"),
    "en-AU-NatashaNeural": ("bf_alice", "en-gb"),
}
FIRST_PIECE = 50    # characters; small so the first sound comes quickly
MAX_PIECE = 260


def _asset(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", name)


def model_path():
    return os.path.join(data_dir(), MODEL_NAME)


def rate_to_speed(rate):
    try:
        return max(0.5, min(2.0, 1 + int(rate.strip("%")) / 100))
    except ValueError:
        return 1.0


def split_pieces(text):
    """Short first piece, then sentence-sized pieces."""
    pieces = []
    for para in text.split("\n\n"):
        para = " ".join(para.split())
        for s in re.split(r"(?<=[.!?:;])\s+", para):
            while len(s) > MAX_PIECE:  # break very long sentences at a comma/space
                cut = max(s.rfind(", ", 0, MAX_PIECE), s.rfind(" ", 0, MAX_PIECE))
                cut = cut if cut > 40 else MAX_PIECE
                pieces.append(s[:cut + 1].strip())
                s = s[cut + 1:].strip()
            if s:
                pieces.append(s)
    if pieces and len(pieces[0]) > FIRST_PIECE:
        first = pieces[0]
        cut = max(first.rfind(", ", 0, FIRST_PIECE), first.rfind(" ", 0, FIRST_PIECE))
        if cut > 15:
            pieces[0:1] = [first[:cut + 1].strip(), first[cut + 1:].strip()]
    return [p for p in pieces if p]


class LocalVoice:
    def __init__(self):
        self.kokoro = None
        self.lock = threading.Lock()
        self.download_state = "missing"   # missing / downloading / ready / failed
        self.progress = 0.0
        if self._model_ok(quick=True):
            self.download_state = "ready"

    # ---- model download
    def _model_ok(self, quick=False):
        p = model_path()
        if not os.path.exists(p) or os.path.getsize(p) != MODEL_BYTES:
            return False
        if quick:
            return True
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest() == MODEL_SHA256

    @property
    def ready(self):
        return self.download_state == "ready"

    def status_text(self):
        return {
            "ready": "Offline voice: ready",
            "downloading": f"Offline voice: downloading {self.progress * 100:.0f}%",
            "failed": "Offline voice: download failed (will retry)",
        }.get(self.download_state, "Offline voice: not downloaded yet")

    def download_in_background(self, delay=5):
        if self.ready or self.download_state == "downloading":
            return
        threading.Thread(target=self._download, args=(delay,), daemon=True,
                         name="kokoro-download").start()

    def download(self):
        """Blocking download (used by the self-test)."""
        self._download(0)
        return self.ready

    def _download(self, delay):
        time.sleep(delay)
        if self.ready:
            return
        self.download_state = "downloading"
        part = model_path() + ".part"
        try:
            import ssl
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "ReadAloud"})
            h = hashlib.sha256()
            got = 0
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r, open(part, "wb") as f:
                while True:
                    block = r.read(1 << 18)
                    if not block:
                        break
                    f.write(block)
                    h.update(block)
                    got += len(block)
                    self.progress = got / MODEL_BYTES
            if got != MODEL_BYTES or h.hexdigest() != MODEL_SHA256:
                raise ValueError(f"download didn't match (got {got} bytes)")
            os.replace(part, model_path())
            self.download_state = "ready"
            log("offline voice downloaded")
        except Exception as e:
            self.download_state = "failed"
            log(f"offline voice download failed: {e!r}")
            try:
                os.remove(part)
            except OSError:
                pass

    # ---- speech
    def load(self):
        with self.lock:
            if self.kokoro is not None:
                return
            if not self.ready:
                raise RuntimeError("offline voice not downloaded yet")
            # phonemizer imports 'segments' (+ ~100 MB of dependencies) for a
            # mode we never use; a placeholder keeps it out of the app.
            if "segments" not in sys.modules:
                stub = types.ModuleType("segments")
                stub.Profile = stub.Tokenizer = type("Unused", (), {})
                stub.__version__ = "0.0.0"
                sys.modules["segments"] = stub
            import espeakng_loader  # noqa: F401  (bundled speech-sounds library)
            from kokoro_onnx import Kokoro
            t = time.time()
            self.kokoro = Kokoro(model_path(), _asset("kokoro-voices.npz"))
            log(f"offline voice loaded in {time.time() - t:.1f}s")

    def synth(self, text, edge_voice, rate):
        """Returns 16-bit mono PCM at 24 kHz as a Python array."""
        import array
        import numpy as np
        self.load()
        voice, lang = VOICE_MAP.get(edge_voice, ("bf_emma", "en-gb"))
        with self.lock:
            samples, sr = self.kokoro.create(text, voice=voice, speed=rate_to_speed(rate),
                                             lang=lang)
        if sr != 24000:
            raise RuntimeError(f"unexpected sample rate {sr}")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        return array.array("h", pcm.tobytes())
