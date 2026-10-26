"""Self-update from GitHub Releases.

Checks the latest release when the app opens and every few hours. If there's
a newer version it downloads it in the background, checks it against the
release's SHA256SUMS.txt, and swaps it in the next time the app isn't speaking:

* Windows: the running .exe is renamed to .old (Windows allows that), the new
  one takes its name, and it's started. Pinned taskbar icons keep working.
* Mac: the new "Read Aloud.app" replaces the old one and is relaunched.

The new copy waits for the old one to quit before it starts (--after-update).
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from .engine import data_dir, log
from .version import VERSION

REPO = "jreyneruk-dev/read-aloud"
API = os.environ.get("READ_ALOUD_UPDATE_API",
                     f"https://api.github.com/repos/{REPO}/releases/latest")
CHECK_EVERY = 6 * 3600
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def parse_version(v):
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except (ValueError, AttributeError):
        return None


def asset_name():
    if IS_WIN:
        return "ReadAloud-Windows.exe"
    if IS_MAC:
        return "ReadAloud-Mac.zip" if platform.machine() == "arm64" else "ReadAloud-Mac-Intel.zip"
    return None


def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ReadAloud/{VERSION}", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def install_target():
    """What gets replaced: the .exe on Windows, the .app bundle on Mac."""
    if not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(sys.executable)
    if IS_WIN:
        return exe
    if IS_MAC:
        app = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        return app if app.endswith(".app") else None
    return None


# ------------------------------------------------------------------ swapping

def wait_for_exit(pid, timeout=20):
    """Used by the freshly started copy: wait until the old copy has quit."""
    deadline = time.time() + timeout
    if IS_WIN:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if h:
            ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000))
            ctypes.windll.kernel32.CloseHandle(h)
        return
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def cleanup_after_update():
    """Delete what a previous update left behind (old copy, downloads)."""
    target = install_target()
    leftovers = []
    if target and IS_WIN:
        leftovers.append(target + ".old")
    if target and IS_MAC:
        leftovers.append(os.path.join(os.path.dirname(target), ".Read Aloud.app.old"))
    leftovers.append(os.path.join(data_dir(), "update"))
    for p in leftovers:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        except OSError:
            pass  # e.g. Windows still has the old .exe open; next time


def swap_in(staged, target, relaunch_args=()):
    """Replace target with the staged download and start the new copy.
    Returns normally on success (the caller should then quit)."""
    pid = str(os.getpid())
    if IS_WIN:
        old = target + ".old"
        try:
            os.remove(old)
        except OSError:
            pass
        os.replace(target, old)              # renaming a running .exe is allowed
        try:
            shutil.copy2(staged, target)
        except Exception:
            os.replace(old, target)          # put the original back
            raise
        flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([target, "--after-update", pid, *relaunch_args],
                         creationflags=flags, close_fds=True)
        return
    if IS_MAC:
        parent = os.path.dirname(target)
        work = tempfile.mkdtemp(prefix=".readaloud-update-", dir=parent)
        try:
            subprocess.run(["/usr/bin/ditto", "-x", "-k", staged, work], check=True)
            new_app = os.path.join(work, "Read Aloud.app")
            if not os.path.isdir(new_app):
                raise RuntimeError("update zip didn't contain Read Aloud.app")
            old = os.path.join(parent, ".Read Aloud.app.old")
            shutil.rmtree(old, ignore_errors=True)
            os.rename(target, old)
            try:
                os.rename(new_app, target)
            except Exception:
                os.rename(old, target)
                raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        subprocess.Popen(["/usr/bin/open", "-n", target, "--args",
                          "--after-update", pid, *relaunch_args])
        return
    raise RuntimeError("self-update not supported on this system")


# ------------------------------------------------------------------ checking

class Updater:
    """state: idle / checking / up_to_date / downloading / ready / installing /
    error / unsupported"""

    def __init__(self, on_change=lambda: None, on_ready=lambda: None):
        self.on_change = on_change
        self.on_ready = on_ready
        self.state = "idle"
        self.latest = None
        self.error = ""
        self.staged = None
        self.progress = 0.0
        self.auto = True              # mirrors the "Update automatically" setting
        self._lock = threading.Lock()
        if parse_version(VERSION) is None or install_target() is None or asset_name() is None:
            self.state = "unsupported"   # development copy, not a released app

    @property
    def supported(self):
        return self.state != "unsupported"

    def _set(self, state, error=""):
        self.state, self.error = state, error
        try:
            self.on_change()
        except Exception:
            pass

    def status_text(self):
        v = f"Version {VERSION}"
        return {
            "checking": f"{v}: checking for updates…",
            "up_to_date": f"{v} (up to date)",
            "downloading": f"{v}: downloading {self.latest} ({self.progress * 100:.0f}%)",
            "ready": f"{v}: {self.latest} ready, installing when idle" if self.auto
                     else f"{v}: update {self.latest} available",
            "installing": f"Installing {self.latest}…",
            "error": f"{v}: update check failed",
            "unsupported": f"Version {VERSION}",
        }.get(self.state, v)

    def start_background(self, first_delay=20):
        if not self.supported:
            return

        def loop():
            time.sleep(first_delay)
            while True:
                self.check()
                time.sleep(CHECK_EVERY)
        threading.Thread(target=loop, daemon=True, name="updater").start()

    def check_now(self):
        if self.supported:
            threading.Thread(target=self.check, daemon=True).start()

    def check(self):
        if not self.supported or not self._lock.acquire(blocking=False):
            return
        try:
            if self.state in ("ready", "installing"):
                return
            self._set("checking")
            with _get(API) as r:
                rel = json.load(r)
            tag = rel.get("tag_name", "")
            newest, mine = parse_version(tag), parse_version(VERSION)
            if newest is None or newest <= mine:
                self.latest = tag.lstrip("v")
                self._set("up_to_date")
                return
            self.latest = tag.lstrip("v")
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            name = asset_name()
            if name not in assets or "SHA256SUMS.txt" not in assets:
                raise RuntimeError(f"release {tag} is missing {name} or SHA256SUMS.txt")
            with _get(assets["SHA256SUMS.txt"]) as r:
                sums = dict(reversed(line.split(None, 1))
                            for line in r.read().decode().splitlines() if line.strip())
            expected = sums.get(name) or sums.get("*" + name)
            if not expected:
                raise RuntimeError(f"no checksum for {name}")
            self.staged = self._download(assets[name], name, expected.strip().lower())
            log(f"update {self.latest} downloaded and verified")
            self._set("ready")
            self.on_ready()
        except Exception as e:
            log(f"update check failed: {e!r}")
            self._set("error", str(e))
        finally:
            self._lock.release()

    def _download(self, url, name, expected):
        folder = os.path.join(data_dir(), "update")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        part = path + ".part"
        self._set("downloading")
        h = hashlib.sha256()
        with _get(url, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                block = r.read(1 << 18)
                if not block:
                    break
                f.write(block)
                h.update(block)
                got += len(block)
                if total:
                    self.progress = got / total
                    if got % (1 << 22) < (1 << 18):
                        self.on_change()
        if h.hexdigest() != expected:
            os.remove(part)
            raise RuntimeError("downloaded update didn't match its checksum")
        os.replace(part, path)
        return path

    def install(self, quit_app):
        """Swap in the downloaded version and quit. Call when the app is idle."""
        if self.state != "ready" or not self.staged:
            return False
        self._set("installing")
        try:
            swap_in(self.staged, install_target())
        except Exception as e:
            log(f"update install failed: {e!r}")
            self._set("error", f"couldn't install: {e}")
            return False
        log(f"installed update {self.latest}; restarting")
        quit_app()
        return True
