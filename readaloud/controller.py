"""Platform-independent button logic: click -> grab selection -> speak / stop."""
import threading

from . import engine as eng
from .engine import Engine, log

# Button states: idle, grabbing, loading, speaking, empty, error, permission
BUSY = ("grabbing", "loading", "speaking")
FLASH = ("empty", "error", "permission")  # shown briefly, then back to idle


class Controller:
    def __init__(self, post, selection, settings):
        """post(fn) runs fn on the UI thread; selection is the platform module."""
        self.post = post
        self.sel = selection
        self.settings = settings
        self.view = None
        self.state = "idle"
        self._token = 0
        eng.VOICE = settings["voice"]
        eng.RATE = settings["rate"]
        eng.OFFLINE_ONLY = bool(settings.get("offline_only"))
        self.engine = Engine(self._engine_state)

    # called from engine threads
    def _engine_state(self, s):
        self.post(lambda: self._set(s))

    def _set(self, s):
        if s == "idle" and self.state in FLASH:
            return  # let the flash finish
        self.state = s
        if self.view:
            self.view.show(s)
        if s in FLASH:
            tok = self._token

            def back():
                if self.state == s and self._token == tok:
                    self.state = "idle"
                    self.view.show("idle")
            self.view.later(1.6, back)

    def click(self):
        self._token += 1
        if self.state in BUSY:
            self.engine.stop()
            self._set("idle")
            return
        if not self.sel.has_permission():
            self.sel.ask_permission()
            self._set("permission")
            return
        self._set("grabbing")
        tok = self._token
        threading.Thread(target=self._grab, args=(tok,), daemon=True).start()

    def _grab(self, tok):
        try:
            text = self.sel.get_selected_text()
        except Exception as e:
            log(f"selection error: {e!r}")
            text = None
        if tok != self._token:
            return  # user clicked again (stop) meanwhile
        if not text:
            self.post(lambda: self._set("empty"))
            return
        self.engine.speak(text)

    # menu actions
    def set_voice(self, voice):
        eng.VOICE = self.settings["voice"] = voice
        self._save()

    def set_rate(self, rate):
        eng.RATE = self.settings["rate"] = rate
        self._save()

    def set_offline_only(self, on):
        eng.OFFLINE_ONLY = self.settings["offline_only"] = bool(on)
        if on:
            self.engine.preload_offline()
        self._save()

    def offline_status(self):
        try:
            return self.engine.local.status_text()
        except Exception:
            return "Offline voice: unavailable"

    def moved(self, x, y):
        self.settings["x"], self.settings["y"] = x, y
        self._save()

    def _save(self):
        from . import settings as st
        st.save(self.settings)
