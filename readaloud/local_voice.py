"""Offline backup voice: Kokoro-82M, running on the computer itself.

Used automatically when Microsoft's voices can't be reached (no internet, or
Microsoft changed something), or always if the user picks "Offline voice only".

The voice model (177 MB) isn't inside the app; it's downloaded once, checked
against a fingerprint, and kept in the app's data folder.  One background
supervisor owns repair: it re-checks the file after a failure, re-downloads
only bytes that don't match the fingerprint, and never deletes a model that
hashes correctly just because a particular load failed.
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
REPAIR_THROTTLE = 60.0
SUPERVISOR_IDLE = 3600.0


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
        self._dl_lock = threading.Lock()
        self._supervisor = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._next_wake = 0.0
        self.download_state = "missing"   # missing / downloading / ready / failed / load_failed
        self.progress = 0.0
        self.last_error = ""
        self.preload_future = None
        self._load_failures = 0
        if self._present():
            self.download_state = "ready"

    # ---- model file
    def _present(self):
        try:
            p = model_path()
            return os.path.exists(p) and os.path.getsize(p) == MODEL_BYTES
        except OSError:
            return False

    @property
    def ready(self):
        if self.download_state == "ready" and not self._present():
            self.demote("model file missing or wrong size")
        return self.download_state == "ready"

    def demote(self, why):
        """Stop claiming to be ready the moment the model isn't usable."""
        if self.download_state in ("ready", "downloading"):
            log(f"offline voice unavailable: {why}"[:160])
        self.kokoro = None
        self.last_error = why
        self.download_state = "load_failed" if self._present() else "missing"
        self.request_repair(why)

    def request_repair(self, why="", force=False):
        """Wake the repair supervisor; throttled so one failure is one check."""
        if why:
            self.last_error = why
        now = time.monotonic()
        if not force and now < self._next_wake:
            return
        self._next_wake = now + REPAIR_THROTTLE
        self._wake.set()

    def verify(self):
        """Full fingerprint check; quarantines bytes that don't match."""
        if self._model_ok():
            return True
        if os.path.exists(model_path()):
            self._quarantine("checksum mismatch")
        self.demote("model file is corrupt")
        return False

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

    def _quarantine(self, why):
        p = model_path()
        if not os.path.exists(p):
            return
        try:
            os.replace(p, p + ".bad")
            log(f"offline voice model quarantined ({why})")
        except OSError:
            try:
                os.remove(p)
            except OSError:
                pass

    def status_text(self):
        return {
            "ready": "Offline voice: ready",
            "downloading": f"Offline voice: downloading {self.progress * 100:.0f}%",
            "failed": "Offline voice: download failed (will retry automatically)",
            "load_failed": "Offline voice: needs repair (will retry automatically)",
            "missing": "Offline voice: not downloaded (menu > Voice > Download)",
        }.get(self.download_state, "Offline voice: not downloaded yet")

    # ---- repair supervisor
    def download_in_background(self, delay=5, retry_every=600):
        """One long-lived repair thread: download, verify, reload, wait."""
        if self._supervisor is not None:
            return
        self._supervisor = threading.Thread(
            target=self._repair_loop, args=(delay, retry_every),
            daemon=True, name="kokoro-repair")
        self._supervisor.start()

    def _repair_loop(self, delay, retry_every):
        if self._stop.wait(delay):
            return
        while not self._stop.is_set():
            wait = SUPERVISOR_IDLE
            try:
                if self.ready:
                    self.verify()
                if not self.ready:
                    wait = retry_every
                    if self.repair_once():
                        wait = SUPERVISOR_IDLE
            except Exception as e:
                wait = retry_every
                log(f"offline repair: {type(e).__name__}: {e}"[:200])
            self._wake.wait(wait)
            self._wake.clear()

    def repair_once(self):
        """Verify the model, repair mismatched bytes, and make sure it loads."""
        if self.download_state == "ready" and self.kokoro is not None:
            return True
        p = model_path()
        if not os.path.exists(p):
            return self._download()
        try:
            size = os.path.getsize(p)
        except OSError:
            return self._download()
        if size != MODEL_BYTES:
            self._quarantine("wrong size")
            return self._download()
        if not self._model_ok():
            self._quarantine("checksum mismatch")
            return self._download()
        # These are the exact published bytes: never delete them for a load
        # error, and keep trying to start them on later repair passes.
        self.download_state = "ready"
        self.last_error = ""
        try:
            self.load()
            return True
        except Exception as e:
            self.download_state = "load_failed"
            self.last_error = f"offline voice couldn't start: {type(e).__name__}"
            log((f"offline voice repair: {self.last_error}: {e}")[:200])
            return False

    def retry_download(self):
        """Visible manual retry from the menu; never blocks the caller."""
        if self.ready:
            return True
        self.download_state = "downloading"
        self.progress = 0.0
        self.request_repair("manual retry", force=True)
        threading.Thread(target=self._repair_now, daemon=True,
                         name="kokoro-download-now").start()
        return False

    def _repair_now(self):
        try:
            self.repair_once()
        except Exception as e:
            log(f"offline repair: {type(e).__name__}: {e}"[:200])
        finally:
            if not self.ready and self.download_state == "downloading":
                self.download_state = "failed"

    def download(self):
        """Blocking download (used by the self-test). Waits for one already running."""
        return self._download()

    def _download(self):
        # One download at a time; a second caller waits for the first and reuses it
        with self._dl_lock:
            if self._model_ok():
                self.download_state = "ready"
                self.last_error = ""
                return True
            return self._download_locked()

    def _download_locked(self):
        self.download_state = "downloading"
        part = model_path() + f".{os.getpid()}.part"
        try:
            os.makedirs(os.path.dirname(model_path()), exist_ok=True)
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
                    self.progress = min(1.0, got / MODEL_BYTES)
            if got != MODEL_BYTES or h.hexdigest() != MODEL_SHA256:
                raise ValueError(f"download didn't match (got {got} bytes)")
            os.replace(part, model_path())
            self.download_state = "ready"
            self.last_error = ""
            try:
                os.remove(model_path() + ".bad")
            except OSError:
                pass
            log("offline voice downloaded")
            return True
        except Exception as e:
            self.download_state = "failed"
            log(f"offline voice download failed: {e!r}")
            try:
                os.remove(part)
            except OSError:
                pass
            return False

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
            try:
                import espeakng_loader  # noqa: F401  (bundled speech-sounds library)
                from kokoro_onnx import Kokoro
                t = time.time()
                self.kokoro = Kokoro(model_path(), _asset("kokoro-voices.npz"))
            except Exception as e:
                self._load_failures += 1
                why = f"load failed: {type(e).__name__}"
                log((f"offline voice {why}: {e}"[:200]))
                self.kokoro = None
                if self._model_ok():
                    # The bytes are right; the failure is environmental or
                    # transient. Keep the model and retry, never delete it.
                    self.download_state = "load_failed"
                    self.last_error = why
                else:
                    self._quarantine("checksum mismatch")
                    self.download_state = "missing"
                    self.last_error = why
                self.request_repair(why)
                raise
            self._load_failures = 0
            log(f"offline voice loaded in {time.time() - t:.1f}s")

    def preload(self, pool=None):
        """Load the model while the first packet is on its way (never blocks)."""
        if not self.ready or self.kokoro is not None:
            return self.preload_future
        if self.preload_future is not None and not self.preload_future.done():
            return self.preload_future
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(1, thread_name_prefix="offline-preload")
        self.preload_future = pool.submit(self._preload)
        return self.preload_future

    def _preload(self):
        try:
            self.load()
        except Exception as e:
            log(f"offline preload: {type(e).__name__}: {e}"[:200])

    def synth(self, text, edge_voice, rate):
        """Returns 16-bit mono PCM at 24 kHz as a Python array."""
        import array
        import numpy as np
        try:
            self.load()
        except Exception:
            self.request_repair(self.last_error or "offline voice unavailable")
            raise
        voice, lang = VOICE_MAP.get(edge_voice, ("bf_emma", "en-gb"))
        with self.lock:
            samples, sr = self.kokoro.create(text, voice=voice, speed=rate_to_speed(rate),
                                             lang=lang)
        if sr != 24000:
            raise RuntimeError(f"unexpected sample rate {sr}")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        return array.array("h", pcm.tobytes())
