"""Get the currently selected text in whatever app is in front (Windows).

The floating button never takes focus, so the app you were reading in stays
in front. We press Ctrl+C for you, read the clipboard, then put your original
clipboard text back.
"""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x11, 0x10, 0x12, 0x5B, 0x5C
VK_C = 0x43

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


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]


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


def _write_text(text):
    if not _open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        if text is None:
            return
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, size)
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        user32.CloseClipboard()


def get_selected_text():
    """Returns the selected text, or None if nothing is selected."""
    saved = _read_text()
    before = user32.GetClipboardSequenceNumber()

    # Release any modifier the user might still be holding, then Ctrl+C
    held = [vk for vk in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)
            if user32.GetAsyncKeyState(vk) & 0x8000]
    _send(*[_key(vk, up=True) for vk in held],
          _key(VK_CONTROL), _key(VK_C), _key(VK_C, up=True), _key(VK_CONTROL, up=True))

    deadline = time.time() + 0.6
    while user32.GetClipboardSequenceNumber() == before and time.time() < deadline:
        time.sleep(0.015)
    if user32.GetClipboardSequenceNumber() == before:
        return None  # nothing selected
    time.sleep(0.02)  # let the app finish writing all formats
    text = _read_text()
    _write_text(saved)  # put the user's clipboard back
    return text if text and text.strip() else None
