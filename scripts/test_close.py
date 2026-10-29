"""CI test: the app can be closed like a normal app.

Windows: the window must have a taskbar button, and "Close window" from the
taskbar (a WM_CLOSE message) must quit the app.
Mac: the app must be in the Dock, and "Quit" from the Dock (a quit Apple Event,
sent here via NSRunningApplication.terminate) must quit it.

usage: python scripts/test_close.py <path to ReadAloud.exe or Read Aloud.app>
"""
import json
import os
import subprocess
import sys
import time


def seed_settings(folder):
    """Skip the first-run welcome box so nothing modal is open during the test."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "settings.json"), "w") as f:
        json.dump({"welcomed": True}, f)


def fail(msg):
    print(f"::error::{msg}")  # shows up as a GitHub annotation
    sys.exit(1)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def app_log():
    p = os.path.expanduser("~/Library/Application Support/Read Aloud/read-aloud.log")
    try:
        return open(p).read()[-600:].replace("\n", " / ")
    except OSError:
        return "(no log)"


def test_windows(exe):
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    u.GetWindowLongW.restype = ctypes.c_long
    seed_settings(os.path.join(os.environ["APPDATA"], "Read Aloud"))
    env = dict(os.environ, READ_ALOUD_NO_INSTALL="1")  # run the built copy itself
    p = subprocess.Popen([exe], env=env)
    hwnd = None
    for _ in range(40):
        time.sleep(0.5)
        hwnd = u.FindWindowW(None, "Read Aloud")
        if hwnd:
            break
    if not hwnd:
        p.kill()
        fail("app window never appeared")
    time.sleep(1.5)  # let the taskbar style be applied
    ex = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
    print(f"window found, extended style 0x{ex:08x}")
    if not ex & 0x40000:
        fail("no taskbar button (WS_EX_APPWINDOW missing)")
    if ex & 0x80:
        fail("still a tool window (hidden from taskbar)")
    if not ex & 0x08000000:
        fail("window can steal focus (WS_EX_NOACTIVATE missing)")
    u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, what taskbar 'Close window' sends
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()
        fail("app did not quit after Close window")
    print("PASS: taskbar button present and Close window quits the app")


def test_mac(app):
    from AppKit import NSRunningApplication
    seed_settings(os.path.expanduser("~/Library/Application Support/Read Aloud"))
    subprocess.run(["open", app], check=True)
    ra = None
    for _ in range(60):
        time.sleep(0.5)
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.readaloud.button")
        if apps:
            ra = apps[0]
            break
    if ra is None:
        fail(f"app never started. App log: {app_log()}")
    time.sleep(4)
    pid = ra.processIdentifier()
    # re-query so the policy isn't a stale cached value
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    policy = ra.activationPolicy()  # 0 = regular (in Dock), 1 = accessory, 2 = hidden
    print(f"app running, pid {pid}, activation policy {policy}")
    if policy != 0:
        os.kill(pid, 9)
        fail(f"app is not shown in the Dock (activation policy {policy})")
    sent = ra.terminate()  # same quit request as Dock > Quit
    print(f"quit request sent: {sent}")
    for _ in range(30):
        time.sleep(0.5)
        if not alive(pid):
            print("PASS: in the Dock and Quit closes the app")
            return
    os.kill(pid, 9)
    fail(f"app did not quit after Quit (request accepted={sent}). App log: {app_log()}")


if __name__ == "__main__":
    target = sys.argv[1]
    test_windows(target) if os.name == "nt" else test_mac(target)
