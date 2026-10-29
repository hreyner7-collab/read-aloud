"""One installed copy on Windows.

A downloaded ReadAloud-*.exe is a one-time installer: the first time it runs it
copies itself to %LOCALAPPDATA%\\Programs\\Read Aloud\\ReadAloud.exe, creates a
Start-menu shortcut, and hands off to that copy.  Running a newer download
replaces the installed copy in place, and the self-updater always swaps that
one file, so copies never pile up in Downloads.

Everything here is a no-op for a development checkout, the CI self-tests and
the headless update check.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys

from .engine import log
from .version import VERSION

IS_WIN = os.name == "nt"
EXE_NAME = "ReadAloud.exe"
SIDE_CAR = "install.json"
MUTEX = "ReadAloud.SingleInstance"
TOO_SMALL = 1_000_000          # a real build is ~50 MB; a stub isn't a build
ALREADY_RUNNING = (
    "Read Aloud is already running. Look for the round button at the right of "
    "your screen - right-click it to quit, then start this one.")
UPDATED_WHILE_RUNNING = (
    "Read Aloud has been updated. Quit the copy that is still running "
    "(right-click its button, then Quit Read Aloud), then start Read Aloud "
    "from the Start menu.")
COULD_NOT_INSTALL = (
    "Read Aloud couldn't set up its permanent copy, so this download will keep "
    "working from where it is.")


def install_dir():
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "Programs", "Read Aloud")


def installed_exe(folder=None):
    return os.path.join(folder or install_dir(), EXE_NAME)


def installed_version(folder=None):
    try:
        with open(os.path.join(folder or install_dir(), SIDE_CAR),
                  encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("version") or "") if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def newer(mine, theirs):
    """True when `mine` is a strictly later release than `theirs`."""
    from .updater import parse_version
    a, b = parse_version(mine), parse_version(theirs)
    return a is not None and (b is None or a > b)


def _write_side_car(folder, version):
    tmp = os.path.join(folder, SIDE_CAR + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": version, "exe": EXE_NAME}, f, indent=1)
    os.replace(tmp, os.path.join(folder, SIDE_CAR))


def _old_name(target):
    """Where the previous .exe goes.  A copy that was already updated while
    running is the old file now, and Windows won't let it be overwritten."""
    old = target + ".old"
    try:
        os.remove(old)
    except OSError:
        pass
    if os.path.exists(old):
        old = target + f".old{os.getpid()}"
    return old


def _clean_old(folder, keep):
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(EXE_NAME + ".old") and name != os.path.basename(keep):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass                     # still running; a later launch removes it


def install_from(source, version=VERSION, folder=None):
    """Copy `source` over the installed copy and record its version."""
    folder = folder or install_dir()
    os.makedirs(folder, exist_ok=True)
    target = installed_exe(folder)
    tmp = target + f".{os.getpid()}.new"
    old = _old_name(target)
    try:
        shutil.copy2(source, tmp)
        if os.path.exists(target):
            try:
                os.replace(target, old)  # renaming a running .exe is allowed
            except OSError:
                pass
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    _write_side_car(folder, version)
    _clean_old(folder, old)
    return target


def _shortcut(target, folder):
    """Best-effort Start-menu entry so the installed copy can be started."""
    try:
        programs = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                                "Start Menu", "Programs")
        path = os.path.join(programs, "Read Aloud.lnk")
        if os.path.exists(path):
            return
        quote = lambda p: "'" + p.replace("'", "''") + "'"
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({quote(path)});"
                  f"$s.TargetPath={quote(target)};"
                  f"$s.WorkingDirectory={quote(folder)};"
                  f"$s.IconLocation={quote(target)};$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        script], creationflags=0x08000000, timeout=20,
                       capture_output=True)
    except Exception as e:
        log(f"install: no Start-menu shortcut: {type(e).__name__}")


def already_running():
    """True when another Read Aloud holds the single-instance mutex."""
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenMutexW(0x00100000, False, MUTEX)   # SYNCHRONIZE
        if handle:
            k32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def _spawn(exe):
    subprocess.Popen([exe], close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # detached, new group


def _message(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Read Aloud", 0x40)
    except Exception:
        pass


def adopt(source=None, folder=None, spawn=None, note=None):
    """Install/hand off.  Returns one of:
    disabled, not-windows, already-installed, handoff, already-running,
    updated-while-running, failed.
    """
    if not IS_WIN or not getattr(sys, "frozen", False):
        return "not-windows"
    if os.environ.get("READ_ALOUD_NO_INSTALL"):
        return "disabled"
    note = note or _message
    source = os.path.abspath(source or sys.executable)
    folder = folder or install_dir()
    target = installed_exe(folder)
    if os.path.normcase(source) == os.path.normcase(os.path.abspath(target)):
        if installed_version(folder) != VERSION:
            # The self-updater swaps the .exe without touching the sidecar.
            try:
                _write_side_car(folder, VERSION)
            except OSError:
                pass
        return "already-installed"

    running = already_running()
    installed_ok = (os.path.isfile(target)
                    and os.path.getsize(target) > TOO_SMALL
                    and not newer(VERSION, installed_version(folder)))
    if installed_ok:
        _clean_old(folder, "")           # last update's leftover, if any
        if running:
            return "already-running"
        (spawn or _spawn)(target)
        return "handoff"
    try:
        install_from(source, VERSION, folder)
        _shortcut(target, folder)
    except Exception as e:
        log(f"install: could not install the permanent copy: {e!r}")
        return "failed"
    log(f"installed {VERSION} to {target}")
    if running:
        return "updated-while-running"
    (spawn or _spawn)(target)
    return "handoff"


def explain(outcome):
    """Say what happened when there is no window to carry the message."""
    if outcome == "already-running":
        _message(ALREADY_RUNNING)
    elif outcome == "updated-while-running":
        _message(UPDATED_WHILE_RUNNING)
    elif outcome == "failed":
        _message(COULD_NOT_INSTALL)
