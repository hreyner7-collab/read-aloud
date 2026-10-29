"""CI test: a downloaded .exe installs once, hand off, and updates in place.

usage: python scripts/test_install.py dist\\ReadAloud.exe

Runs the real build with a throw-away LOCALAPPDATA and data folder, checks
that it copies itself to <LOCALAPPDATA>\\Programs\\Read Aloud\\ReadAloud.exe,
starts that copy (not itself), and that launching the same download again
reuses the installed copy instead of making a second one.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def wait_for(path, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(path):
            return True
        time.sleep(0.2)
    return False


def expected_version():
    version_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "readaloud", "version.py")
    with open(version_py, encoding="utf-8") as f:
        return re.search(r'VERSION = "([^"]+)"', f.read()).group(1)


def process_image(pid):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    h = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        return buf.value if k32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(h)


def wait_for_window():
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    end = time.time() + 90
    while time.time() < end:
        hwnd = u.FindWindowW(None, "Read Aloud")
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return None


def close_window(hwnd):
    u = ctypes.windll.user32
    u.PostMessageW(hwnd, 0x0010, 0, 0)            # WM_CLOSE
    end = time.time() + 30
    while time.time() < end:
        if not u.FindWindowW(None, "Read Aloud"):
            return True
        time.sleep(0.3)
    return False


def launch(download, env):
    p = subprocess.Popen([download], env=env)
    try:
        code = p.wait(120)
    except subprocess.TimeoutExpired:
        p.kill()
        fail("the downloaded app did not hand off and exit")
    if code != 0:
        fail(f"the downloaded app exited with {code}")


def running_from(where):
    hwnd = wait_for_window()
    if not hwnd:
        fail(f"the installed app never showed its button (expected {where})")
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    image = process_image(pid.value)
    return hwnd, image


def main(download):
    if os.name != "nt":
        print("skipped: Windows only")
        return
    download = os.path.abspath(download)
    version = expected_version()
    tmp = tempfile.mkdtemp(prefix="ra-install-")
    local = os.path.join(tmp, "local")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    with open(os.path.join(data, "settings.json"), "w") as f:
        json.dump({"welcomed": True}, f)          # no welcome box in the way

    env = dict(os.environ,
               LOCALAPPDATA=local,
               READ_ALOUD_DATA_DIR=data,
               READ_ALOUD_AUDIO_BACKEND="null")
    env.pop("READ_ALOUD_NO_INSTALL", None)

    folder = os.path.join(local, "Programs", "Read Aloud")
    target = os.path.join(folder, "ReadAloud.exe")
    side_car = os.path.join(folder, "install.json")

    launch(download, env)
    if not wait_for(target):
        fail("the download never installed itself")
    if not wait_for(side_car, 10):
        fail("the install did not record its version")
    with open(side_car, encoding="utf-8") as f:
        recorded = json.load(f).get("version")
    if recorded != version:
        fail(f"install.json says {recorded!r}, expected {version!r}")

    hwnd, image = running_from(target)
    print(f"first launch runs {image}")
    if os.path.normcase(image) != os.path.normcase(target):
        close_window(hwnd)
        fail("the app kept running from Downloads instead of the installed copy")
    if not close_window(hwnd):
        fail("the installed app did not close like a normal app")

    before = os.stat(target).st_mtime_ns
    launch(download, env)
    time.sleep(1.0)
    hwnd, image = running_from(target)
    after = os.stat(target).st_mtime_ns
    exes = [n for n in os.listdir(folder) if n.lower().endswith(".exe")]
    close_window(hwnd)
    print(f"second launch runs {image}; copies in the folder: {exes}")
    if after != before:
        fail("a second launch copied the app again instead of reusing it")
    if len(exes) != 1:
        fail(f"the install folder gained another copy: {exes}")
    if os.path.normcase(image) != os.path.normcase(target):
        fail("a second launch did not use the installed copy")

    print(f"PASS: installed {version} once at {target}, second launch reused it")


if __name__ == "__main__":
    main(sys.argv[1])
