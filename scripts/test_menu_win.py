"""Drive the Windows right-click menu with real mouse input.

Usage:
    python scripts/test_menu_win.py rows  <pid> ["Speed"]
    python scripts/test_menu_win.py click <pid> "Speed > Faster"
    python scripts/test_menu_win.py close-dialog <pid>

Menu labels come from the app's own log, screen rects from its native HMENU,
so every click lands on the exact pixel a person would click.
"""
import ctypes
import ctypes.wintypes as w
import os
import sys
import time

u = ctypes.windll.user32
try:                        # physical pixels everywhere: menu rects are not virtualized
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        u.SetProcessDPIAware()
    except Exception:
        pass

u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u.SetWindowPos.restype = ctypes.c_bool
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u.WindowFromPoint.argtypes = [w.POINT]
u.WindowFromPoint.restype = w.HWND
u.GetAncestor.argtypes = [w.HWND, ctypes.c_uint]
u.GetAncestor.restype = w.HWND

MN_GETHMENU = 0x01E1
WM_CLOSE = 0x0010
LOG = os.path.join(os.environ.get("APPDATA", ""), "Read Aloud", "read-aloud.log")


def class_of(h):
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    return b.value


def title_of(h):
    b = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(h, b, 256)
    return b.value


def windows_of(pid, cls=None, visible=True):
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def cb(h, _l):
        p = w.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            if (cls is None or class_of(h) == cls) and (not visible or u.IsWindowVisible(h)):
                out.append(h)
        return True

    u.EnumWindows(cb, 0)
    return out


def rect(h):
    r = w.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def click(x, y, right=False):
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    u.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.06)
    u.mouse_event(up, 0, 0, 0, 0)


def hover(x, y):
    u.SetCursorPos(int(x), int(y))


def newest_menu(pid, known):
    found = [h for h in windows_of(pid, "#32768") if h not in known]
    return found[-1] if found else None


def wait_for_menu(pid, known, rows, timeout=2.0):
    """A #32768 owned by pid, new since known, with `rows` live rows."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for h in [x for x in windows_of(pid, "#32768") if x not in known]:
            rects = menu_rects(h)
            if rects is not None and len(rects) == rows:
                return h
        time.sleep(0.03)
    return None


def open_menu(pid, timeout=3.0):
    for h in windows_of(pid, "#32768"):
        u.PostMessageW(h, WM_CLOSE, 0, 0)
    tops = [h for h in windows_of(pid, "TkTopLevel") if title_of(h) == "Read Aloud"]
    if not tops:
        raise SystemExit("no Read Aloud window for pid %s" % pid)
    button = tops[0]
    # Other always-on-top windows can cover the button; raise it for the click.
    u.SetWindowPos(button, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    x0, y0, x1, y1 = rect(button)
    at = u.WindowFromPoint(w.POINT((x0 + x1) // 2, (y0 + y1) // 2))
    if u.GetAncestor(at, 2) != button:
        raise SystemExit("another window covers the button: %s %r"
                         % (class_of(at), title_of(at)))
    click((x0 + x1) // 2, (y0 + y1) // 2, right=True)
    main = logged_rows().get("main") or []
    menu = wait_for_menu(pid, set(), len(main), timeout)
    if menu is None:
        raise SystemExit("the menu did not open")
    return menu


def logged_rows():
    """{menu key: [label | '---', ...]} from the app's own diagnostics."""
    rows = {}
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "menu rows " not in line:
                    continue
                key, _, rest = line.split("menu rows ", 1)[1].partition(": ")
                rows[key.strip()] = [x.strip() for x in rest.split(" || ")]
    except OSError:
        pass
    return rows


def menu_rects(menu):
    """Native screen rects for each row, in Tk item order, or None if the
    window has no HMENU (Windows recycles menu windows, so a #32768 seen one
    moment can be a dead shell the next)."""
    hmenu = u.SendMessageW(menu, MN_GETHMENU, 0, 0)
    if not hmenu:
        return None
    out = []
    for i in range(u.GetMenuItemCount(hmenu)):
        r = w.RECT()
        u.GetMenuItemRect(menu, hmenu, i, ctypes.byref(r))
        out.append((r.left, r.top, r.right, r.bottom))
    return out


def find_index(labels, wanted):
    exact = [i for i, l in enumerate(labels) if l == wanted]
    loose = [i for i, l in enumerate(labels) if wanted.lower() in l.lower()]
    hits = exact or loose
    if not hits:
        raise SystemExit("no row %r in %r" % (wanted, labels))
    return hits[0]


def navigate(pid, path, known=None):
    """Open the menu and walk 'path' of cascade labels."""
    if known is None:
        menu = open_menu(pid)
        known = {menu}
    else:
        menu = max(known)
    time.sleep(0.2)
    key = "main"
    for part in path:
        labels = logged_rows().get(key)
        if not labels:
            raise SystemExit("the app did not log rows for %r" % key)
        index = find_index(labels, part)
        key = labels[index]
        sub_labels = logged_rows().get(key)
        if not sub_labels:
            raise SystemExit("the app did not log rows for the %r submenu" % key)
        x0, y0, x1, y1 = menu_rects(menu)[index]
        hover((x0 + x1) // 2, (y0 + y1) // 2)
        want = len(sub_labels)
        sub = wait_for_menu(pid, known, want)
        if sub is None:
            click((x0 + x1) // 2, (y0 + y1) // 2)
            sub = wait_for_menu(pid, known, want)
        if sub is None:
            raise SystemExit("submenu %r did not open" % part)
        known.add(sub)
        menu = sub
        time.sleep(0.25)
    return menu, known


def dismiss(pid):
    """Close menus and dialogs left over from a failed attempt: a stuck
    popup swallows every later click and looks like a broken command."""
    for cls in ("#32768", "#32770"):
        for h in windows_of(pid, cls):
            u.PostMessageW(h, WM_CLOSE, 0, 0)
    time.sleep(0.25)


def _leaf_labels(wanted):
    main = logged_rows()
    labels = main.get("main") or []
    for part in wanted[:-1]:
        index = find_index(labels, part)
        labels = main.get(labels[index]) or []
    return labels


def click_path(pid, path):
    wanted = [p.strip() for p in path.split(">") if p.strip()]
    if not wanted:
        raise SystemExit("empty path")
    labels = _leaf_labels(wanted)
    last = None
    for _attempt in range(3):
        try:
            menu, _known = navigate(pid, wanted[:-1])
            index = find_index(labels, wanted[-1])
            rects = menu_rects(menu)
            if rects is None or len(rects) != len(labels):
                # Tk recycles the submenu window between the hover and the
                # click; walking again from the top costs nothing.
                raise LookupError("menu window died")
            x0, y0, x1, y1 = rects[index]
            click((x0 + x1) // 2, (y0 + y1) // 2)
            print("clicked %r at %s" % (labels[index], ((x0 + x1) // 2, (y0 + y1) // 2)))
            return
        except (SystemExit, LookupError) as exc:
            last = exc
            dismiss(pid)
    raise SystemExit("would not click %r: %s" % (path, last))


def report(pid, path=()):
    menu, _known = navigate(pid, list(path))
    labels = logged_rows().get("main" if not path else path[-1], [])
    rects = menu_rects(menu)
    print("menu window", rect(menu), "rows logged", len(labels), "native", len(rects))
    for i, r in enumerate(rects):
        label = labels[i] if i < len(labels) else "?"
        print("%2d  %-36s %s" % (i, label.replace("\u2026", "..."), r))


def close_dialog(pid):
    deadline = time.time() + 3.0
    dlg = None
    while time.time() < deadline:
        found = windows_of(pid, "#32770")
        if found:
            dlg = found[0]
            break
        time.sleep(0.05)
    if dlg is None:
        raise SystemExit("no dialog appeared")
    ok = u.GetDlgItem(dlg, 1)
    x0, y0, x1, y1 = rect(ok)
    click((x0 + x1) // 2, (y0 + y1) // 2)
    print("closed dialog %r" % title_of(dlg))


def main():
    cmd = sys.argv[1]
    pid = int(sys.argv[2])
    if cmd == "rows":
        path = [p.strip() for p in sys.argv[3].split(">")] if len(sys.argv) > 3 else []
        report(pid, path)
    elif cmd == "click":
        click_path(pid, sys.argv[3])
    elif cmd == "close-dialog":
        close_dialog(pid)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
