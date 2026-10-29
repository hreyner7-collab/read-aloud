"""Floating Read Aloud button for Windows (tkinter + a little Win32).

A small round button that stays on top, never takes focus (so the app you're
reading in keeps its selection), and can be dragged anywhere. Right-click for
voice, speed, audio cache, start-with-Windows and quit.
"""
import ctypes
import os
import queue
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from . import settings as st
from . import menu_text as mt
from .controller import Controller

IS_WIN = os.name == "nt"
KEY = "#ff00fe"  # transparent colour around the round button

COLOURS = {
    "idle": "#232323", "grabbing": "#232323", "loading": "#232323",
    "speaking": "#0a84ff", "empty": "#ff9f0a", "permission": "#ff9f0a",
    "offline_missing": "#ff9f0a", "error": "#ff453a",
}
# Segoe MDL2 Assets / Segoe Fluent Icons glyphs (built into Windows 10 and 11)
GLYPHS = {"idle": "\ue767", "speaking": "\ue71a", "empty": "\ue7ba",
          "permission": "\ue72e", "error": "\ue7ba"}
GLYPHS["offline_missing"] = GLYPHS["empty"]
TIP_IDLE = "Select text anywhere, then click to read it aloud"
TIPS = {
    "speaking": "Click to stop",
    "empty": "Select some text first",
    "permission": "Allow Read Aloud in Settings > Privacy & security > Accessibility",
    "error": "Couldn't read aloud — open the menu for why, or Try again",
    "offline_missing": "Offline voice not downloaded — menu › Voice › Download offline voice",
}
HELP = ("Select text in any app, then click the round button to hear it read aloud. "
        "Click again to stop.\n\n"
        "Drag the button wherever you like. Right-click it for voices, speed, "
        "and to start it automatically with Windows.\n\n"
        "If part of a sentence could not be read, a small orange dot stays on the "
        "button. The right-click menu shows the reason and has Try again.\n\n"
        "Offline voice only works without internet once its model has been "
        "downloaded (menu > Voice > Download offline voice).\n\n"
        "Read Aloud keeps one copy in your user folder. Double-clicking a newer "
        "download updates that copy in place, so you can delete the file you "
        "downloaded afterwards.")


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
    if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return True
    # Someone double-clicking a second copy must be told why nothing appeared.
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Read Aloud is already running. Look for the round button at the "
            "right of your screen - right-click it to quit, then start this one.",
            "Read Aloud", 0x40)          # MB_ICONINFORMATION
    except Exception:
        pass
    return False


class WinApp:
    def __init__(self, selftest=None, selection=None):
        # --selftest is only ever passed by the build check, and it has to be
        # able to run beside the copy already on the screen.
        if selftest is None and not _single_instance():
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
        self._start = None
        self._dragged = False
        self._tip = None
        self._tip_text = ""
        self._spin_angle = 0
        self._spinning = False
        self._state = "idle"
        self._detail = ""
        self._hover = False
        self._pressed = False
        self.canvas = None
        self.menu = None
        self._build_window()
        self.controller.view = self
        self._build_menu()
        self.show("idle", "")
        self.root.after(30, self._drain)
        self.controller.start()

    # ---- used by Controller
    def show(self, state, detail=""):
        self._state, self._detail = state, detail
        self._draw()
        warning = self.controller.warning if state == "idle" else ""
        self._tip_text = detail or warning or TIPS.get(state, "") or TIP_IDLE
        self._tip_hide()

    def note(self, text):
        """Show a brief confirmation beside the button after a menu choice."""
        self._tip_hide()
        self._tip_text = text
        self._tip_show()
        self.root.after(2200, self._tip_hide)

    def _draw(self):
        c, s = self.canvas, self.size
        if c is None:
            return
        c.delete("all")
        pad = int(4 * self.scale) + (1 if self._pressed else 0)
        fill = COLOURS.get(self._state, COLOURS["idle"])
        if self._hover and self._state in ("idle", "grabbing", "loading"):
            fill = "#2f2f2f"
        # Two soft edges read as depth on the transparent window; neither
        # changes the 44 px button footprint.
        c.create_oval(pad - 1, pad - 1, s - pad + 1, s - pad + 1,
                      fill="", outline="#101010", width=max(2, int(2 * self.scale)))
        c.create_oval(pad, pad, s - pad, s - pad, fill=fill,
                      outline="#8a8a8a" if self._hover else "#4a4a4a",
                      width=max(1, int(self.scale)))
        if self._state in ("idle", "grabbing", "loading") and not self._spinning:
            c.create_arc(pad + 3, pad + 3, s - pad - 3, s - pad - 3,
                         start=35, extent=110, style=tk.ARC,
                         outline="#3d3d3d", width=max(1, int(self.scale)))
        self._spinning = self._state in ("grabbing", "loading")
        if self._spinning:
            self._spin()
        else:
            c.delete("spin")
            c.create_text(s / 2, s / 2, text=GLYPHS.get(self._state, GLYPHS["idle"]),
                          fill="white", font=(self.icon_font, int(18 * self.scale) * -1))
            if self._state == "idle" and (self.controller.warning or self._detail):
                d = int(5 * self.scale)
                x, y = s - d - int(3 * self.scale), d + int(4 * self.scale)
                c.create_oval(x - d, y - d, x + d, y + d, fill="#ff9f0a",
                              outline=fill, width=max(1, int(self.scale)),
                              tags="warning")

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

    # ---- tooltip: warnings and errors stay visible, non-modal
    def _enter(self, _event=None):
        self._hover = True
        self._draw()
        self._tip_show()

    def _leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._draw()
        self._tip_hide()

    def _tip_show(self, _event=None):
        self._tip_hide()
        if not self._tip_text:
            return
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        try:
            t.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(t, text=self._tip_text, bg="#2b2b2b", fg="white", padx=8, pady=4,
                 wraplength=int(260 * self.scale), justify="left").pack()
        x = self.root.winfo_x() - int(250 * self.scale)
        y = self.root.winfo_y() + self.size + 4
        t.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._tip = t

    def _tip_hide(self, _event=None):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

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
        menu_family = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
        self.menu_font = tkfont.Font(root=r, family=menu_family, size=10)
        c = self.canvas
        c.bind("<ButtonPress-1>", self._down)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._up)
        c.bind("<ButtonPress-3>", self._menu)
        c.bind("<Enter>", self._enter)
        c.bind("<Leave>", self._leave)
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
            r.after(150, self._keep_visible)

    def _keep_visible(self):
        """Clicking the taskbar button could minimise a borderless window with no
        way back, so put the button straight back - and re-assert always-on-top,
        which the withdraw/deiconify above drops."""
        u = ctypes.windll.user32
        if self.hwnd:
            if u.IsIconic(self.hwnd):
                u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            u.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0013)  # TOPMOST, no move/size/activate
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
        self._pressed = True
        self._draw()

    def _drag(self, e):
        if self._start is None:
            return
        sx, sy, wx, wy = self._start
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._dragged = True
        if self._dragged:
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _up(self, e):
        if self._start is None:
            return                     # release without a matching press
        self._start = None
        self._pressed = False
        self._draw()
        if self._dragged:
            self.controller.moved(self.root.winfo_x(), self.root.winfo_y())
        else:
            self.controller.click()

    # ---- right-click menu
    def _build_menu(self):
        if self.menu is not None:
            try:
                self.menu.destroy()    # one menu lives at a time (no Tk leak)
            except tk.TclError:
                pass
            self.menu = None
        controller = self.controller

        def make(parent):
            return tk.Menu(parent, tearoff=0, font=self.menu_font,
                           activebackground="#0a84ff", activeforeground="white")

        m = make(self.root)
        status = controller.last_error or controller.warning
        if status:
            m.add_command(label=mt.status_line(status), state="disabled")
            m.add_command(label="Try again", command=controller.retry)
            m.add_separator()

        self.voice_var = tk.StringVar(value=self.settings["voice"])
        self.rate_var = tk.StringVar(value=self.settings["rate"])
        self.login_var = tk.BooleanVar(value=st.login_item_enabled())
        voices = make(m)
        for vid, label in st.VOICES:
            voices.add_radiobutton(label=label, value=vid, variable=self.voice_var,
                                   command=lambda: controller.set_voice(self.voice_var.get()))
        self.offline_var = tk.BooleanVar(value=bool(self.settings.get("offline_only")))
        voices.add_separator()
        voices.add_command(label=mt.offline_status(controller), state="disabled")
        local = controller.engine.local
        if local is not None and not local.ready:
            voices.add_command(label=mt.DOWNLOAD_LABEL,
                               command=controller.download_offline)
        voices.add_separator()
        # The mode switch is the last row: a disabled status line sitting under
        # it made people click the wrong thing and think the switch did nothing.
        voices.add_checkbutton(
            label=mt.OFFLINE_LABEL, variable=self.offline_var,
            command=lambda: controller.set_offline_only(
                not bool(self.settings.get("offline_only"))))
        m.add_cascade(label=mt.voice_title(self.settings), menu=voices)

        speeds = make(m)
        for rate, label in st.SPEEDS:
            speeds.add_radiobutton(label=label, value=rate, variable=self.rate_var,
                                   command=lambda: controller.set_rate(self.rate_var.get()))
        m.add_cascade(label=mt.speed_title(self.settings), menu=speeds)

        cache = make(m)
        cache.add_command(label=mt.CACHE_NOTE, state="disabled")
        self.disk_cache_var = tk.BooleanVar(value=bool(self.settings.get("disk_cache")))
        cache.add_checkbutton(label=mt.DISK_LABEL, variable=self.disk_cache_var,
                              command=lambda: controller.set_disk_cache(
                                  self.disk_cache_var.get()))
        cache.add_command(label="Clear audio cache", command=controller.clear_cache)
        m.add_cascade(label="Audio cache", menu=cache)

        m.add_separator()
        if st.login_item_supported():
            m.add_checkbutton(label="Start with Windows", variable=self.login_var,
                              command=lambda: st.set_login_item(self.login_var.get()))
        m.add_command(label=mt.HOW_TO_LABEL, command=lambda: self.welcome(force=True))

        up = controller.updater
        updates = make(m)
        updates.add_command(label=mt.update_status(up), state="disabled")
        if up.supported:
            updates.add_command(label=mt.update_action(up),
                                command=controller.check_for_updates)
            self.auto_update_var = tk.BooleanVar(
                value=bool(self.settings.get("auto_update", True)))
            updates.add_checkbutton(label="Update automatically", variable=self.auto_update_var,
                                    command=lambda: controller.set_auto_update(
                                        self.auto_update_var.get()))
        m.add_cascade(label="Updates", menu=updates)

        m.add_separator()
        m.add_command(label="Quit Read Aloud", command=self.quit)
        self.menu = m

    def _menu(self, e):
        self._tip_hide()
        self._build_menu()  # so statuses are current
        m = self.menu
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            try:
                m.grab_release()
            except tk.TclError:
                pass
            try:
                m.destroy()
            except tk.TclError:
                pass
            if self.menu is m:
                self.menu = None

    def welcome(self, force=False):
        if self.settings.get("welcomed") and not force:
            return
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        messagebox.showinfo("Read Aloud", HELP, parent=self.root)
        if not self.settings.get("welcomed"):
            self.settings["welcomed"] = True   # only after it was dismissed
            st.save(self.settings)

    def run(self):
        if self.selftest:
            from .selftest import run_selftest
            run_selftest(self.controller, self.selftest,
                         lambda: self.q.put(self.root.destroy))
        else:
            self.root.after(600, self.welcome)
        self.root.mainloop()
