"""Floating Read Aloud button for Windows (tkinter + a little Win32).

A small round button that stays on top, never takes focus (so the app you're
reading in keeps its selection), and can be dragged anywhere. Right-click for
voice, speed, start-with-Windows and quit.
"""
import ctypes
import os
import queue
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from . import settings as st
from .controller import Controller

IS_WIN = os.name == "nt"
KEY = "#ff00fe"  # transparent colour around the round button

COLOURS = {
    "idle": "#232323", "grabbing": "#232323", "loading": "#232323",
    "speaking": "#0a84ff", "empty": "#ff9f0a", "permission": "#ff9f0a",
    "error": "#ff453a",
}
# Segoe MDL2 Assets / Segoe Fluent Icons glyphs (built into Windows 10 and 11)
GLYPHS = {"idle": "", "speaking": "", "empty": "",
          "permission": "", "error": ""}
HELP = ("Select text in any app, then click the round button to hear it read aloud. "
        "Click again to stop.\n\n"
        "Drag the button wherever you like. Right-click it for voices, speed, "
        "and to start it automatically with Windows.")


def _asset(name):
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", name)


def _dpi_aware():
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _single_instance():
    if not IS_WIN:
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "ReadAloud.SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


class WinApp:
    def __init__(self, selftest=None, selection=None):
        if not _single_instance():
            raise SystemExit(0)
        _dpi_aware()
        self.selftest = selftest
        if selection is None:
            if IS_WIN:
                from . import selection_win as selection
            else:  # only for testing the Windows UI on Linux
                import types
                selection = types.SimpleNamespace(
                    __name__="selection_stub", has_permission=lambda: True,
                    ask_permission=lambda: True, get_selected_text=lambda: None)
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.title("Read Aloud")
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.size = int(52 * self.scale)
        self.settings = st.load()
        self.controller = Controller(self.q.put, selection, self.settings)
        self._build_window()
        self.controller.view = self
        self._build_menu()
        self._spin_angle = 0
        self._spinning = False
        self.show("idle")
        self.root.after(30, self._drain)

    # ---- used by Controller
    def show(self, state):
        c, s = self.canvas, self.size
        c.delete("all")
        pad = int(4 * self.scale)
        c.create_oval(pad, pad, s - pad, s - pad, fill=COLOURS.get(state, "#232323"),
                      outline="#4a4a4a" if state in ("idle", "grabbing", "loading") else "")
        self._spinning = state in ("grabbing", "loading")
        if self._spinning:
            self._spin()
        else:
            c.create_text(s / 2, s / 2, text=GLYPHS.get(state, GLYPHS["idle"]),
                          fill="white", font=(self.icon_font, int(15 * self.scale) * -1))

    def later(self, seconds, fn):
        self.root.after(int(seconds * 1000), fn)

    def _spin(self):
        if not self._spinning:
            return
        c, s = self.canvas, self.size
        c.delete("spin")
        r = int(9 * self.scale)
        c.create_arc(s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r, start=self._spin_angle,
                     extent=270, style=tk.ARC, outline="white",
                     width=max(2, int(2 * self.scale)), tags="spin")
        self._spin_angle = (self._spin_angle - 20) % 360
        self.root.after(40, self._spin)

    def _drain(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(30, self._drain)

    # ---- window
    def _build_window(self):
        r, s = self.root, self.size
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=KEY)
        if IS_WIN:
            r.attributes("-transparentcolor", KEY)
        x, y = self.settings.get("x"), self.settings.get("y")
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        if x is None or y is None or not (0 <= x < sw - 10 and 0 <= y < sh - 10):
            x, y = sw - s - int(24 * self.scale), sh // 2
        r.geometry(f"{s}x{s}+{int(x)}+{int(y)}")
        self.canvas = tk.Canvas(r, width=s, height=s, bg=KEY, highlightthickness=0,
                                cursor="hand2")
        self.canvas.pack()
        fams = set(tkfont.families(r))
        self.icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else "Segoe MDL2 Assets"
        c = self.canvas
        c.bind("<ButtonPress-1>", self._down)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._up)
        c.bind("<ButtonPress-3>", self._menu)
        r.update_idletasks()
        # Closing from the taskbar (right-click > Close window) quits the app
        r.protocol("WM_DELETE_WINDOW", self.quit)
        icon = _asset("icon.ico")
        if IS_WIN and os.path.exists(icon):
            r.iconbitmap(default=icon)
        self.hwnd = None
        if IS_WIN:
            # Never take focus from the app you're reading in, but DO show a
            # taskbar button so it can be closed like any other app.
            u = ctypes.windll.user32
            hwnd = u.GetParent(r.winfo_id()) or r.winfo_id()
            GWL_EXSTYLE, NOACTIVATE, TOOLWINDOW, APPWINDOW, TOPMOST = (
                -20, 0x08000000, 0x80, 0x40000, 0x8)
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                             (style | NOACTIVATE | APPWINDOW | TOPMOST) & ~TOOLWINDOW)
            self.hwnd = hwnd
            r.withdraw()             # re-show so the taskbar picks up the new style
            r.after(50, r.deiconify)
            r.after(1000, self._keep_visible)

    def _keep_visible(self):
        """Clicking the taskbar button could minimise a borderless window with no
        way back - so if that happens, put the button straight back."""
        u = ctypes.windll.user32
        if self.hwnd and u.IsIconic(self.hwnd):
            u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        self.root.after(500, self._keep_visible)

    def quit_app(self):
        self.root.destroy()

    def update_status_changed(self):
        pass  # the menu is rebuilt each time it opens

    def quit(self):
        try:
            self.controller.engine.stop()
        finally:
            self.root.destroy()

    def _down(self, e):
        self._start = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._dragged = False

    def _drag(self, e):
        sx, sy, wx, wy = self._start
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._dragged = True
        if self._dragged:
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _up(self, e):
        if self._dragged:
            self.controller.moved(self.root.winfo_x(), self.root.winfo_y())
        else:
            self.controller.click()

    # ---- right-click menu
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        self.voice_var = tk.StringVar(value=self.settings["voice"])
        self.rate_var = tk.StringVar(value=self.settings["rate"])
        self.login_var = tk.BooleanVar(value=st.login_item_enabled())
        voices = tk.Menu(m, tearoff=0)
        for vid, label in st.VOICES:
            voices.add_radiobutton(label=label, value=vid, variable=self.voice_var,
                                   command=lambda: self.controller.set_voice(self.voice_var.get()))
        speeds = tk.Menu(m, tearoff=0)
        for rate, label in st.SPEEDS:
            speeds.add_radiobutton(label=label, value=rate, variable=self.rate_var,
                                   command=lambda: self.controller.set_rate(self.rate_var.get()))
        self.offline_var = tk.BooleanVar(value=bool(self.settings.get("offline_only")))
        voices.add_separator()
        voices.add_checkbutton(label="Offline voice only (works without internet)",
                               variable=self.offline_var,
                               command=lambda: self.controller.set_offline_only(self.offline_var.get()))
        voices.add_command(label=self.controller.offline_status(), state="disabled")
        m.add_cascade(label="Voice", menu=voices)
        m.add_cascade(label="Speed", menu=speeds)
        m.add_separator()
        if st.login_item_supported():
            m.add_checkbutton(label="Start with Windows", variable=self.login_var,
                              command=lambda: st.set_login_item(self.login_var.get()))
        m.add_command(label="How to use", command=lambda: self.welcome(force=True))
        m.add_separator()
        up = self.controller.updater
        m.add_command(label=up.status_text(), state="disabled")
        if up.supported:
            m.add_command(label="Install update now" if up.state == "ready" else "Check for updates",
                          command=self.controller.check_for_updates)
            self.auto_update_var = tk.BooleanVar(value=bool(self.settings.get("auto_update", True)))
            m.add_checkbutton(label="Update automatically", variable=self.auto_update_var,
                              command=lambda: self.controller.set_auto_update(self.auto_update_var.get()))
        m.add_separator()
        m.add_command(label="Quit Read Aloud", command=self.quit)
        self.menu = m

    def _menu(self, e):
        self._build_menu()  # so the offline-voice status is current
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def welcome(self, force=False):
        if self.settings.get("welcomed") and not force:
            return
        self.settings["welcomed"] = True
        st.save(self.settings)
        messagebox.showinfo("Read Aloud", HELP, parent=self.root)

    def run(self):
        if self.selftest:
            from .selftest import run_selftest
            run_selftest(self.controller, self.selftest,
                         lambda: self.q.put(self.root.destroy))
        else:
            self.root.after(600, self.welcome)
        self.root.mainloop()
