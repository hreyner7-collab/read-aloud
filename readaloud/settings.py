"""Small JSON settings file, plus 'open at login' for each platform."""
import json
import os
import sys

from .engine import data_dir, log

PATH = os.path.join(data_dir(), "settings.json")

VOICES = [
    ("en-GB-SoniaNeural", "Sonia (British, female)"),
    ("en-GB-RyanNeural", "Ryan (British, male)"),
    ("en-GB-LibbyNeural", "Libby (British, female)"),
    ("en-US-AriaNeural", "Aria (American, female)"),
    ("en-US-GuyNeural", "Guy (American, male)"),
    ("en-AU-NatashaNeural", "Natasha (Australian, female)"),
]
SPEEDS = [
    ("-15%", "Slower"),
    ("+0%", "Normal"),
    ("+15%", "Faster"),
    ("+30%", "Fastest"),
]

DEFAULTS = {"voice": "en-GB-SoniaNeural", "rate": "+0%", "x": None, "y": None,
            "offline_only": False, "auto_update": True}


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(s):
    try:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except OSError:
        pass


# ---------------------------------------------------------------- open at login

def _mac_service():
    from ServiceManagement import SMAppService  # macOS 13+
    return SMAppService.mainAppService()


def login_item_supported():
    if sys.platform == "darwin":
        try:
            _mac_service()
            return getattr(sys, "frozen", False)  # only works for the real .app
        except Exception:
            return False
    return os.name == "nt" and getattr(sys, "frozen", False)


def login_item_enabled():
    try:
        if sys.platform == "darwin":
            return _mac_service().status() == 1  # SMAppServiceStatusEnabled
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "Read Aloud")
                return True
    except Exception:
        return False
    return False


def ensure_login_path():
    """Keep 'Start with Windows' pointing at this copy when it moves.

    The registered path is the downloaded .exe until the permanent copy runs
    once; after that it must follow the installed .exe or Windows would start
    an old download at login.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    want = f'"{sys.executable}"'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                current, _ = winreg.QueryValueEx(k, "Read Aloud")
            except OSError:
                return False
            if current != want:
                winreg.SetValueEx(k, "Read Aloud", 0, winreg.REG_SZ, want)
                log("login item: pointed at the installed copy")
            return True
    except Exception as e:
        log(f"login item: {e!r}")
        return False


def set_login_item(on):
    try:
        if sys.platform == "darwin":
            svc = _mac_service()
            if on:
                svc.registerAndReturnError_(None)
            else:
                svc.unregisterAndReturnError_(None)
        elif os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, "Read Aloud", 0, winreg.REG_SZ, f'"{sys.executable}"')
                else:
                    winreg.DeleteValue(k, "Read Aloud")
    except Exception as e:
        log(f"login item: {e!r}")
