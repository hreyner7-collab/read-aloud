"""Get the currently selected text in whatever app is in front (Windows).

The floating button never takes focus, so the app you were reading in stays in
front.  We snapshot the clipboard, press Ctrl+C for you, read the text, then put
the original clipboard back - every format we can copy safely, in a finally
block, so a crash or a slow app can never eat what you had copied.
"""
import ctypes
import os
import time
from ctypes import wintypes

from .paths import log

if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:                                   # lets the module be imported in CI tests
    user32 = kernel32 = None

CF_TEXT = 1
CF_BITMAP = 2
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17

GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x11, 0x10, 0x12, 0x5B, 0x5C
VK_C = 0x43

# HGLOBAL copies we can restore byte for byte.  CF_BITMAP/CF_PALETTE are GDI
# handles, not memory blocks, so they are skipped (and logged) rather than
# guessed at.  "HTML Format" and "Rich Text Format" are added at runtime.
SAFE_FORMATS = (CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_DIB, CF_DIBV5,
                CF_HDROP, CF_LOCALE)
SAFE_FORMAT_NAMES = ("HTML Format", "Rich Text Format")
FORMAT_LABELS = {
    CF_TEXT: "CF_TEXT", CF_BITMAP: "CF_BITMAP", CF_OEMTEXT: "CF_OEMTEXT",
    CF_DIB: "CF_DIB", CF_PALETTE: "CF_PALETTE", CF_UNICODETEXT: "CF_UNICODETEXT",
    CF_HDROP: "CF_HDROP", CF_LOCALE: "CF_LOCALE", CF_DIBV5: "CF_DIBV5",
}

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if os.name == "nt":
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT,
                                               wintypes.LPWSTR, ctypes.c_int]
    user32.GetClipboardFormatNameW.restype = ctypes.c_int
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def has_permission():
    return True


def ask_permission():
    return True


def _key(vk, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                        time=0, dwExtraInfo=0)
    return i


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _open_clipboard():
    for _ in range(20):  # another app may have it open for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _format_name(fmt):
    if fmt in FORMAT_LABELS:
        return FORMAT_LABELS[fmt]
    if fmt >= 0xC000:
        buf = ctypes.create_unicode_buffer(256)
        n = user32.GetClipboardFormatNameW(fmt, buf, 256)
        if n:
            return buf.value
    return f"fmt-{fmt:#06x}"


def _safe_formats():
    formats = list(SAFE_FORMATS)
    for name in SAFE_FORMAT_NAMES:
        fmt = user32.RegisterClipboardFormatW(name)
        if fmt:
            formats.append(fmt)
    return formats


def _snapshot():
    """Returns (entries, skipped_names, original_sequence) or None."""
    if not _open_clipboard():
        return None
    try:
        seq = user32.GetClipboardSequenceNumber()
        entries, skipped = [], []
        safe = _safe_formats()
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt not in safe:
                skipped.append(_format_name(fmt))
                continue
            h = user32.GetClipboardData(fmt)
            if not h:
                skipped.append(_format_name(fmt))
                continue
            size = kernel32.GlobalSize(h)
            if not size:
                skipped.append(_format_name(fmt))
                continue
            p = kernel32.GlobalLock(h)
            if not p:
                skipped.append(_format_name(fmt))
                continue
            try:
                data = ctypes.string_at(p, size)
            finally:
                kernel32.GlobalUnlock(h)
            entries.append((fmt, data))
        return entries, skipped, seq
    except OSError:
        return None
    finally:
        user32.CloseClipboard()


def _restore(entries):
    """Replace the clipboard with the snapshot; called from a finally block."""
    if entries is None:
        return False
    if not _open_clipboard():
        log("clipboard: could not restore (clipboard busy)")
        return False
    ok = True
    try:
        if not user32.EmptyClipboard():
            return False
        for fmt, data in entries:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                ok = False
                continue
            p = kernel32.GlobalLock(h)
            if not p:
                kernel32.GlobalFree(h)
                ok = False
                continue
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)      # clipboard kept ownership only on success
                ok = False
        return ok
    finally:
        user32.CloseClipboard()


def _read_text():
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def get_selected_text():
    """Returns the selected text, or None if nothing is selected.

    The clipboard always ends up as it started: if it cannot be snapshotted,
    we do not press Ctrl+C at all rather than destroy what the user copied.
    """
    snapshot = _snapshot()
    if snapshot is None:
        log("clipboard: not touched (could not snapshot)")
        return None
    entries, skipped, before = snapshot
    if skipped:
        log("clipboard: not restored (unsupported formats): "
            + ", ".join(sorted(set(skipped))[:8]))
    try:
        held = [vk for vk in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)
                if user32.GetAsyncKeyState(vk) & 0x8000]
        _send(*[_key(vk, up=True) for vk in held],
              _key(VK_CONTROL), _key(VK_C), _key(VK_C, up=True),
              _key(VK_CONTROL, up=True))

        deadline = time.time() + 0.6
        while (user32.GetClipboardSequenceNumber() == before
               and time.time() < deadline):
            time.sleep(0.015)
        if user32.GetClipboardSequenceNumber() == before:
            return None  # nothing selected
        time.sleep(0.02)  # let the app finish writing all formats
        text = _read_text()
        return text if text and text.strip() else None
    finally:
        _restore(entries)
