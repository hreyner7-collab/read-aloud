"""Windows clipboard safety: byte-identical restore, no Ctrl+C without a snapshot."""
import ctypes
import sys

import pytest

from readaloud import selection_win as sw

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows clipboard and wide-character API")


class FakeWin32:
    """Just enough user32/kernel32 to exercise the clipboard contract."""

    def __init__(self):
        self.blocks = {}
        self.next_handle = 0x1000
        self.formats = {}
        self.seq = 1
        self.open_ok = True
        self.open_calls = 0
        self.empty_calls = 0
        self.sent = []
        self.registered = {}
        self.registered_next = 0xC001
        self.selected_text = None

    # ---- user32
    def OpenClipboard(self, hwnd):
        self.open_calls += 1
        return 1 if self.open_ok else 0

    def CloseClipboard(self):
        return 1

    def EmptyClipboard(self):
        self.empty_calls += 1
        self.formats.clear()
        return 1

    def GetClipboardSequenceNumber(self):
        return self.seq

    def EnumClipboardFormats(self, prev):
        for fmt in sorted(self.formats):
            if fmt > prev:
                return fmt
        return 0

    def GetClipboardData(self, fmt):
        data = self.formats.get(fmt)
        if data is None:
            return 0
        return self._store(data)

    def _store(self, data):
        h = self.next_handle
        self.next_handle += 1
        self.blocks[h] = ctypes.create_string_buffer(data, max(1, len(data)))
        return h

    def SetClipboardData(self, fmt, handle):
        self.formats[fmt] = ctypes.string_at(self.GlobalLock(handle),
                                             self.GlobalSize(handle))
        self.GlobalFree(handle)
        return 1

    def RegisterClipboardFormatW(self, name):
        if name not in self.registered:
            self.registered[name] = self.registered_next
            self.registered_next += 1
        return self.registered[name]

    def GetClipboardFormatNameW(self, fmt, buf, size):
        names = {v: k for k, v in self.registered.items()}
        name = names.get(fmt, "Unknown Format")
        buf.value = name
        return len(name)

    def IsClipboardFormatAvailable(self, fmt):
        return 1 if fmt in self.formats else 0

    def GetAsyncKeyState(self, vk):
        return 0

    def SendInput(self, n, arr, size):
        keys = [arr[i].u.ki.wVk for i in range(n)]
        self.sent.append(keys)
        if sw.VK_C in keys and self.selected_text is not None:
            self.formats = {
                sw.CF_UNICODETEXT:
                    self.selected_text.encode("utf-16-le") + b"\x00\x00"
            }
            self.seq += 1

    # ---- kernel32
    def GlobalAlloc(self, flags, size):
        return self._store(b"\x00" * size)

    def GlobalLock(self, handle):
        return ctypes.addressof(self.blocks[handle]) if handle in self.blocks else 0

    def GlobalUnlock(self, handle):
        return 1

    def GlobalSize(self, handle):
        return len(self.blocks.get(handle, b""))

    def GlobalFree(self, handle):
        self.blocks.pop(handle, None)
        return 0


@pytest.fixture
def clip(monkeypatch):
    fake = FakeWin32()
    monkeypatch.setattr(sw, "user32", fake)
    monkeypatch.setattr(sw, "kernel32", fake)
    return fake


def _set_formats(clip, text="original clipboard text"):
    html = clip.RegisterClipboardFormatW("HTML Format")
    rtf = clip.RegisterClipboardFormatW("Rich Text Format")
    clip.formats = {
        sw.CF_UNICODETEXT: text.encode("utf-16-le") + b"\x00\x00",
        html: b"<html><body>hello</body></html>",
        rtf: b"{\\rtf1 hello}",
        sw.CF_DIB: b"\x28\x00\x00\x00fake-dib",
        sw.CF_HDROP: b"\x00\x00fake-files",
        sw.CF_BITMAP: b"fake-bitmap",
    }
    return dict(clip.formats), html, rtf


def test_selection_restores_every_safe_format_byte_for_byte(clip, monkeypatch):
    before, html, rtf = _set_formats(clip)
    clip.selected_text = "the words the user selected"
    logs = []
    monkeypatch.setattr(sw, "log", logs.append)

    assert sw.get_selected_text() == "the words the user selected"
    assert clip.sent and sw.VK_C in clip.sent[0]
    for fmt in (sw.CF_UNICODETEXT, html, rtf, sw.CF_DIB, sw.CF_HDROP):
        assert clip.formats[fmt] == before[fmt], fmt
    assert sw.CF_BITMAP not in clip.formats       # GDI handle: skipped, logged
    assert any("CF_BITMAP" in line for line in logs)


def test_snapshot_failure_never_touches_the_clipboard(clip):
    clip.open_ok = False
    clip.selected_text = "selected"
    assert sw.get_selected_text() is None
    assert clip.sent == []
    assert clip.empty_calls == 0


def test_empty_selection_restores_and_returns_none(clip, monkeypatch):
    before, _html, _rtf = _set_formats(clip)
    clip.selected_text = None

    class AdvancingTime:
        t = 0.0

        def time(self):
            self.t += 0.05
            return self.t

        def sleep(self, _s):
            pass

    monkeypatch.setattr(sw, "time", AdvancingTime())
    assert sw.get_selected_text() is None
    assert clip.empty_calls == 1
    assert clip.formats[sw.CF_UNICODETEXT] == before[sw.CF_UNICODETEXT]


def test_exception_after_ctrl_c_still_restores(clip, monkeypatch):
    before, _html, _rtf = _set_formats(clip)
    clip.selected_text = "selected words"

    def boom():
        raise RuntimeError("clipboard reader failed")

    monkeypatch.setattr(sw, "_read_text", boom)
    with pytest.raises(RuntimeError):
        sw.get_selected_text()
    assert clip.formats[sw.CF_UNICODETEXT] == before[sw.CF_UNICODETEXT]
    assert clip.formats[sw.CF_DIB] == before[sw.CF_DIB]


def test_held_modifier_keys_are_released(clip, monkeypatch):
    _set_formats(clip)
    clip.selected_text = "selected"
    monkeypatch.setattr(clip, "GetAsyncKeyState", lambda vk: 0x8000)
    sw.get_selected_text()
    keys = clip.sent[0]
    assert sw.VK_SHIFT in keys and sw.VK_LWIN in keys


def test_restore_does_nothing_when_snapshot_is_none(clip):
    assert sw._restore(None) is False
    assert clip.empty_calls == 0
    assert clip.open_calls == 0
