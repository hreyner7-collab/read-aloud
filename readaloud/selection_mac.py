"""Get the currently selected text in whatever app is in front (macOS).

1. Ask the app directly through the Accessibility API (doesn't touch the clipboard).
2. If that doesn't work (some apps don't support it), press Cmd+C for the user,
   read the clipboard, then put their original clipboard back.

Both need the one-time Accessibility permission.
"""
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateSystemWide,
    kAXFocusedUIElementAttribute,
    kAXSelectedTextAttribute,
    kAXTrustedCheckOptionPrompt,
)

KEY_C = 8  # kVK_ANSI_C


def has_permission():
    return bool(AXIsProcessTrusted())


def ask_permission():
    """Shows macOS's 'allow Read Aloud to control your computer' prompt."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))


def _ax_selected_text():
    try:
        system = AXUIElementCreateSystemWide()
        err, focused = AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute, None)
        if err or focused is None:
            return None
        err, text = AXUIElementCopyAttributeValue(focused, kAXSelectedTextAttribute, None)
        if err or not text:
            return None
        text = str(text)
        return text if text.strip() else None
    except Exception:
        return None


def _save_clipboard(pb):
    saved = []
    for item in pb.pasteboardItems() or []:
        entry = {}
        for t in item.types():
            data = item.dataForType_(t)
            if data is not None:
                entry[t] = data
        saved.append(entry)
    return saved


def _restore_clipboard(pb, saved):
    pb.clearContents()
    items = []
    for entry in saved:
        item = NSPasteboardItem.alloc().init()
        for t, data in entry.items():
            item.setData_forType_(data, t)
        items.append(item)
    if items:
        pb.writeObjects_(items)


def _copy_selected_text():
    pb = NSPasteboard.generalPasteboard()
    saved = _save_clipboard(pb)
    before = pb.changeCount()

    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, KEY_C, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    deadline = time.time() + 0.6
    while pb.changeCount() == before and time.time() < deadline:
        time.sleep(0.015)
    if pb.changeCount() == before:
        return None  # nothing selected (Cmd+C did nothing)
    text = pb.stringForType_(NSPasteboardTypeString)
    _restore_clipboard(pb, saved)
    text = str(text) if text else None
    return text if text and text.strip() else None


def get_selected_text():
    """Returns the selected text, or None if nothing is selected."""
    return _ax_selected_text() or _copy_selected_text()
