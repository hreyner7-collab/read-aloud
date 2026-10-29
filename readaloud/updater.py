"""Self-update from the project's public download site.

The site publishes manifest.json listing one verified artifact per platform.
A check downloads the matching artifact, checks its size and SHA-256 against
the manifest, and stages it.  The app installs a staged update when it isn't
speaking, or at the next launch if the daily background check found it while
the app was closed:

* Windows: the running .exe is renamed to .old (Windows allows that), the new
  one takes its name, and it's started. Pinned taskbar icons keep working.
* Mac: the new "Read Aloud.app" replaces the old one and is relaunched.

The new copy waits for the old one to quit before it starts (--after-update).

A per-user daily task runs `--update-check`: it checks and stages only, so the
UI never appears on its own.  Setup is idempotent (one named task / one
LaunchAgent), least privilege, and never downloads anything that the manifest
does not describe with an https same-site URL, an exact size and a full hash.
"""
import contextlib
import dataclasses
import datetime
import getpass
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.sax.saxutils

from .engine import data_dir, log
from .version import VERSION

MANIFEST_URL = os.environ.get(
    "READ_ALOUD_UPDATE_MANIFEST",
    "https://read-aloud-kvtn6ncvzmt.qoder.website/manifest.json")
CHECK_EVERY = 6 * 3600
DAILY_HOUR = 12
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 220 * 1024 * 1024
TASK_NAME = "Read Aloud Update"
LAUNCH_AGENT = "com.jreyneruk.readaloud.dailycheck"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


class UpdateError(Exception):
    pass


class NoBuildError(UpdateError):
    """The manifest is valid but publishes no build for this platform."""


@dataclasses.dataclass
class Artifact:
    version: str
    name: str
    url: str
    sha256: str
    size: int
    platform: str = ""
    path: str = ""

    def meta(self):
        return {"version": self.version, "name": self.name, "url": self.url,
                "sha256": self.sha256, "size": self.size,
                "platform": self.platform}


def parse_version(v):
    try:
        text = str(v).strip().lstrip("v")
        if not text or len(text.split(".")) > 4:
            return None
        parts = tuple(int(x) for x in text.split("."))
        if any(x < 0 for x in parts) or len(parts) < 2:
            return None
        return parts
    except (ValueError, AttributeError):
        return None


def artifact_key(system=None, machine=None):
    """Which manifest entry this computer needs."""
    system = str(system or sys.platform).lower()
    machine = str(machine or platform.machine()).lower()
    if system.startswith("win"):
        return "windows"
    if system.startswith("darwin") or system.startswith("mac"):
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        if machine in ("x86_64", "amd64"):
            return "macos-x86_64"
    return None


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


# ------------------------------------------------------------------ manifest

def parse_manifest(payload, key, base_url=MANIFEST_URL):
    """Validate the site manifest strictly and return this machine's artifact."""
    if not isinstance(payload, dict):
        raise UpdateError("manifest is not an object")
    version = str(payload.get("version") or "").strip()
    if parse_version(version) is None:
        raise UpdateError("manifest version is not a release number")
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        entry = artifacts.get(key)
    elif artifacts is None and isinstance(payload.get("path"), str):
        # Sites published before 1.0.13 describe one build at the top level,
        # and that build is the Windows one.
        entry = payload if key == "windows" else None
    else:
        entry = None
    if not isinstance(entry, dict):
        raise NoBuildError(f"manifest has no {key} build")

    base = urllib.parse.urlsplit(str(base_url))
    if base.scheme != "https" or not base.netloc or "@" in base.netloc:
        raise UpdateError("update manifest is not a plain https URL")
    path = str(entry.get("path") or "")
    url = urllib.parse.urljoin(str(base_url), path)
    split = urllib.parse.urlsplit(url)
    if split.scheme != "https" or split.netloc != base.netloc:
        raise UpdateError("artifact is not on the update site")

    name = str(entry.get("name") or "")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,90}", name)
            or name.endswith((".part", ".tmp"))):
        raise UpdateError("artifact name is not a plain file name")
    extension = ".exe" if key == "windows" else ".zip"
    if not name.lower().endswith(extension):
        raise UpdateError("artifact has the wrong file type for this platform")
    digest = str(entry.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("artifact has no full SHA-256")
    size = entry.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        raise UpdateError("artifact size is missing")
    if not 0 < size <= MAX_ARTIFACT_BYTES:
        raise UpdateError("artifact size is out of range")
    return Artifact(version=version.lstrip("v"), name=name, url=url,
                    sha256=digest, size=size,
                    platform=str(entry.get("platform") or "")[:80])


# ------------------------------------------------------------------ files

def staging_dir():
    return os.path.join(data_dir(), "update")


def _meta_path():
    return os.path.join(staging_dir(), "staged.json")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_matches(path, digest, size):
    try:
        return (os.path.isfile(path) and os.path.getsize(path) == size
                and sha256_file(path) == digest)
    except OSError:
        return False


def write_staged_meta(artifact):
    folder = staging_dir()
    os.makedirs(folder, exist_ok=True)
    tmp = _meta_path() + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(artifact.meta(), f, indent=1)
    os.replace(tmp, _meta_path())
    try:
        with open(os.path.join(folder, "last-check"), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def _read_staged_meta():
    try:
        with open(_meta_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def forget_staged(remove_file=True):
    meta = _read_staged_meta()
    if meta and remove_file and isinstance(meta.get("name"), str):
        try:
            os.remove(os.path.join(staging_dir(), os.path.basename(meta["name"])))
        except OSError:
            pass
    for path in (_meta_path(),):
        try:
            os.remove(path)
        except OSError:
            pass


def load_staged(current=None, verify=True):
    """Return a staged, newer artifact; drop anything incomplete or stale."""
    current = VERSION if current is None else current
    data = _read_staged_meta()
    if data is None:
        return None
    try:
        key = artifact_key()
        art = parse_manifest({"version": data["version"], "artifacts": {key: {
            "name": data["name"], "path": data["url"], "sha256": data["sha256"],
            "size_bytes": data["size"], "platform": data.get("platform", ""),
        }}}, key)
    except (KeyError, TypeError, ValueError, UpdateError):
        forget_staged(remove_file=False)
        return None
    newest, mine = parse_version(art.version), parse_version(current)
    if newest is None or mine is None or newest <= mine:
        forget_staged()
        return None
    path = os.path.join(staging_dir(), art.name)
    try:
        right_size = os.path.getsize(path) == art.size
    except OSError:
        right_size = False
    if not right_size or (verify and not file_matches(path, art.sha256, art.size)):
        forget_staged()
        log("update: discarded an incomplete staged download")
        return None
    art.path = path
    return art


@contextlib.contextmanager
def file_lock(path):
    """Non-blocking cross-process lock; yields True only to the holder."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    got = False
    try:
        fh.seek(0)
        if IS_WIN:
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                got = True
            except OSError:
                got = False
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                got = False
        yield got
    finally:
        if got:
            try:
                fh.seek(0)
                if IS_WIN:
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _SameSiteRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if after.scheme != "https" or after.netloc != before.netloc:
            raise UpdateError("update redirect left the https download site")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url, timeout=60):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise UpdateError("updates require https")
    req = urllib.request.Request(url, headers={"User-Agent": f"ReadAloud/{VERSION}"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()), _SameSiteRedirect())
    return opener.open(req, timeout=timeout)


def download_artifact(artifact, folder=None, opener=None, on_progress=None):
    """Stream, size-check, hash-check and atomically stage one artifact."""
    folder = folder or staging_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, artifact.name)
    if file_matches(path, artifact.sha256, artifact.size):
        return path
    opener = opener or _get
    part = os.path.join(
        folder, f".{artifact.name}.{os.getpid()}.{time.time_ns()}.part")
    h = hashlib.sha256()
    got = 0
    try:
        with opener(artifact.url) as response, open(part, "wb") as f:
            while True:
                block = response.read(1 << 18)
                if not block:
                    break
                got += len(block)
                if got > artifact.size:
                    raise UpdateError("download is larger than the manifest says")
                f.write(block)
                h.update(block)
                if on_progress is not None and got % (1 << 22) < (1 << 18):
                    on_progress(min(1.0, got / artifact.size))
        if got != artifact.size:
            raise UpdateError(f"download stopped at {got} of {artifact.size} bytes")
        if h.hexdigest() != artifact.sha256:
            raise UpdateError("download didn't match its checksum")
        os.replace(part, path)
        if on_progress is not None:
            on_progress(1.0)
        return path
    finally:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


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


def cleanup_after_update(target=None, folder=None):
    """Remove the previous copy and stale downloads, never a staged update.

    The old copy is kept until a new copy actually runs: if the target is
    missing (an interrupted swap), the old copy is put back instead.
    """
    target = install_target() if target is None else target
    folder = folder or staging_dir()
    if target and IS_WIN:
        old = target + ".old"
        if not os.path.exists(target) and os.path.exists(old):
            try:
                os.replace(old, target)
                log("update: restored the previous app after an interrupted swap")
            except OSError as e:
                log(f"update: couldn't restore the previous app: {e!r}")
        elif os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass  # Windows may still hold it open; next time
    if target and IS_MAC:
        old = os.path.join(os.path.dirname(target), ".Read Aloud.app.old")
        if not os.path.exists(target) and os.path.isdir(old):
            try:
                os.rename(old, target)
            except OSError:
                pass
        elif os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
    try:
        now = time.time()
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if name.endswith(".part") and now - os.path.getmtime(p) > 3600:
                os.remove(p)
    except OSError:
        pass


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


# ------------------------------------------------------------------ daily task

def _next_noon():
    now = datetime.datetime.now()
    start = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if start <= now:
        start += datetime.timedelta(days=1)
    return start


def _task_user():
    domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    if domain and user:
        return f"{domain}\\{user}"
    try:
        return getpass.getuser()
    except Exception:
        return ""


def daily_task_xml(exe, user=None, start=None):
    """Task Scheduler definition: one daily, least-privilege, no-duplicate run."""
    esc = xml.sax.saxutils.escape
    start = start or _next_noon().strftime("%Y-%m-%dT%H:%M:%S")
    user = user if user is not None else _task_user()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Checks for Read Aloud updates once a day.</Description>
    <URI>\\{esc(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{esc(start)}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{esc(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{esc(exe)}</Command>
      <Arguments>--update-check</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _run_hidden(args, **kwargs):
    if IS_WIN:
        kwargs.setdefault("creationflags", 0x08000000)   # CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def _launch_agent_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_AGENT}.plist")


def install_daily_task(exe=None, run=None, user=None):
    """Create/refresh the one daily update check. Safe to call repeatedly."""
    exe = exe or install_target()
    if not exe or not (IS_WIN or IS_MAC):
        return False
    run = run or _run_hidden
    try:
        if IS_WIN:
            folder = staging_dir()
            os.makedirs(folder, exist_ok=True)
            xml_path = os.path.join(folder, "daily-task.xml")
            with open(xml_path, "w", encoding="utf-16") as f:
                f.write(daily_task_xml(exe, user=user))
            result = run(["schtasks", "/Create", "/TN", TASK_NAME,
                          "/XML", xml_path, "/F"],
                         capture_output=True, text=True)
            ok = getattr(result, "returncode", 1) == 0
            if not ok:
                log(f"update: daily task setup failed ({getattr(result, 'stderr', '')[:120]})")
            return ok
        path = _launch_agent_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        executable = (os.path.join(exe, "Contents", "MacOS", "Read Aloud")
                      if exe.endswith(".app") else exe)
        payload = {
            "Label": LAUNCH_AGENT,
            "ProgramArguments": [executable, "--update-check"],
            "StartCalendarInterval": {"Hour": DAILY_HOUR, "Minute": 0},
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "RunAtLoad": False,
        }
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            plistlib.dump(payload, f)
        os.replace(tmp, path)
        run(["/bin/launchctl", "unload", path], capture_output=True)
        result = run(["/bin/launchctl", "load", path], capture_output=True)
        ok = getattr(result, "returncode", 1) == 0
        if not ok:
            log("update: daily update check could not be loaded into launchd")
        return ok
    except Exception as e:
        log(f"update: daily task setup failed: {type(e).__name__}: {e}")
        return False


def remove_daily_task(exe=None, run=None):
    """Remove the daily check; a development copy never touches a real one."""
    exe = exe or install_target()
    if not exe or not (IS_WIN or IS_MAC):
        return False
    run = run or _run_hidden
    try:
        if IS_WIN:
            result = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                         capture_output=True, text=True)
            return getattr(result, "returncode", 1) == 0
        path = _launch_agent_path()
        run(["/bin/launchctl", "unload", path], capture_output=True)
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    except Exception as e:
        log(f"update: couldn't remove the daily task: {type(e).__name__}")
        return False


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
        self.artifact = None
        self.progress = 0.0
        self.auto = True              # mirrors the "Update automatically" setting
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._poke = threading.Event()
        if (parse_version(VERSION) is None or install_target() is None
                or artifact_key() is None):
            self.state = "unsupported"   # development copy, not a released app
        else:
            pending = load_staged(verify=False)
            if pending is not None:
                # Staged by an earlier check (or the daily task); verify off
                # the UI thread before offering to install it.
                self.latest = pending.version
                self.artifact = pending

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
            "error": f"{v}: {self.error or 'update check failed'}",
            "not_available": f"{v} (no update published for this device)",
            "unsupported": f"Version {VERSION}",
        }.get(self.state, v)

    # ---- background loop
    def start_background(self, first_delay=20):
        if not self.supported or (self._thread is not None and self._thread.is_alive()):
            return

        def loop():
            if self._stop.wait(first_delay):
                return
            self.resume()
            while not self._stop.is_set():
                if self.auto:
                    self.check()
                self._poke.wait(CHECK_EVERY)
                self._poke.clear()
        self._thread = threading.Thread(target=loop, daemon=True, name="updater")
        self._thread.start()

    def wake(self):
        """Ask the background loop to check now (e.g. auto-update turned on)."""
        self._poke.set()

    def resume(self):
        """Verify a staged download left by an earlier check or the daily task."""
        if not self.supported:
            return
        with self._lock:
            if self.state in ("ready", "downloading", "installing"):
                return
            art = load_staged(verify=True)
            if art is None:
                return
            self._stage(art)

    def _stage(self, art):
        self.staged = art.path
        self.artifact = art
        self.latest = art.version
        self.progress = 1.0
        self.state = "ready"
        self.error = ""
        try:
            self.on_change()
        except Exception:
            pass
        try:
            self.on_ready()
        except Exception:
            pass

    def check_now(self):
        if self.supported:
            threading.Thread(target=self.check, daemon=True).start()

    def check(self):
        if not self.supported or not self._lock.acquire(blocking=False):
            return
        try:
            if self.state in ("ready", "downloading", "installing"):
                return
            with file_lock(os.path.join(staging_dir(), "check.lock")) as mine:
                if not mine:
                    return              # the app or the daily task is checking
                self._set("checking")
                with _get(MANIFEST_URL) as response:
                    raw = response.read(MAX_MANIFEST_BYTES + 1)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise UpdateError("manifest is too large")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise UpdateError("manifest is not valid JSON")
                art = parse_manifest(payload, artifact_key())
                newest = parse_version(art.version)
                if newest <= parse_version(VERSION):
                    self.latest = art.version
                    self._set("up_to_date")
                    return
                self.latest = art.version
                self.progress = 0.0
                self._set("downloading")
                art.path = download_artifact(art, opener=_get,
                                             on_progress=self._progress)
                write_staged_meta(art)
            log(f"update {art.version} downloaded and verified")
            self._stage(art)
        except NoBuildError:
            # The feed is fine; it publishes no build for this platform. Mac
            # builds are manual downloads until the feed hosts them too.
            self._set("not_available")
        except Exception as e:
            log(f"update check failed: {e!r}")
            self._set("error", str(e)[:120])
        finally:
            self._lock.release()

    def _progress(self, fraction):
        self.progress = fraction
        try:
            self.on_change()
        except Exception:
            pass

    def install(self, quit_app):
        """Swap in the staged version and quit. Call when the app is idle."""
        if self.state == "installing" or not self.staged:
            return False
        art = self.artifact
        if (art is None or not self.staged
                or not file_matches(self.staged, art.sha256, art.size)):
            forget_staged()
            self.staged = None
            self.artifact = None
            self._set("error", "staged update was damaged; check again")
            return False
        self._set("installing")
        try:
            swap_in(self.staged, install_target())
        except Exception as e:
            log(f"update install failed: {e!r}")
            self.staged = None
            self.artifact = None
            self._set("error", f"couldn't install: {e}")
            return False
        log(f"installed update {self.latest}; restarting")
        quit_app()
        return True

    # ---- daily task
    def sync_daily_task(self):
        if not self.supported:
            return False
        return install_daily_task() if self.auto else remove_daily_task()


def run_daily_check():
    """Headless entry point for the daily task: check and stage, never show UI."""
    up = Updater()
    if not up.supported:
        return 0
    up.resume()
    if up.state != "ready":
        up.check()
    if up.state == "ready":
        log(f"daily update check: {up.latest} staged for the next launch")
        return 0
    if up.state == "up_to_date":
        log("daily update check: up to date")
        return 0
    log(f"daily update check: {up.state}")
    return 1
