"""Platform-independent button logic: click -> grab selection -> speak / stop."""
import threading
import time

from . import engine as eng
from .engine import Engine, log

# Button states: idle, grabbing, loading, speaking, empty, error, permission,
# offline_missing
BUSY = ("grabbing", "loading", "speaking")
FLASH = ("empty", "permission")     # shown briefly, then back to idle
CLICK_DEBOUNCE = 0.35


def selftest_mode():
    import sys
    return "--selftest" in sys.argv


class Controller:
    def __init__(self, post, selection, settings):
        """post(fn) runs fn on the UI thread; selection is the platform module."""
        self.post = post
        self.sel = selection
        self.settings = settings
        self.view = None
        self.state = "idle"
        self._token = 0
        self._last_click = 0.0
        self._last_text = ""
        self.last_error = ""
        self.warning = ""
        eng.VOICE = settings["voice"]
        eng.RATE = settings["rate"]
        eng.OFFLINE_ONLY = bool(settings.get("offline_only"))
        self.engine = Engine(self._engine_state, settings=settings)
        from .updater import Updater
        self.updater = Updater(on_change=self._update_changed,
                               on_ready=lambda: self.post(self._install_when_idle))
        self.updater.auto = bool(settings.get("auto_update", True))

    def start(self):
        """Called once the window exists: start updates and the daily task."""
        if selftest_mode():
            return
        self.updater.start_background()
        if self.view is None:
            self.updater.sync_daily_task()
            return
        self.view.later(2.0, self.updater.sync_daily_task)
        if self.updater.state == "ready" and self.settings.get("auto_update", True):
            self.view.later(1.2, self._install_when_idle)

    # ---- self-update
    def _update_changed(self):
        if self.view and hasattr(self.view, "update_status_changed"):
            self.post(self.view.update_status_changed)

    def _install_when_idle(self):
        """Install a downloaded update, but never in the middle of a read."""
        if not self.settings.get("auto_update", True):
            return  # user installs it from the menu
        if self.state in BUSY:
            self.view.later(5, self._install_when_idle)
            return
        self.install_update()

    def install_update(self):
        if self.state in BUSY:
            self.engine.stop()
        self.updater.install(self.view.quit_app)

    def check_for_updates(self):
        if self.updater.state == "ready" or self.updater.staged:
            self.install_update()
        else:
            self.updater.check_now()

    def set_auto_update(self, on):
        self.settings["auto_update"] = self.updater.auto = bool(on)
        self._save()
        self.updater.sync_daily_task()
        if on:
            self.updater.wake()
            if self.updater.state == "ready":
                self._install_when_idle()

    # called from engine threads
    def _engine_state(self, s, detail="", gen=0):
        if gen != self.engine.gen:
            return  # late event from a read the user already stopped
        self.post(lambda: self._set(s, detail))

    def _set(self, s, detail=""):
        if s == "idle" and self.state in FLASH:
            return  # let the flash finish
        if s == "error":
            self.last_error = detail or "couldn't read aloud"
        if s == "idle":
            self.warning = detail if detail else ""
        if s in ("loading", "speaking"):
            self.warning = ""
        self.state = s
        if self.view:
            self.view.show(s, detail)
        if s in FLASH:
            tok = self._token

            def back():
                if self.state == s and self._token == tok:
                    self.state = "idle"
                    self.view.show("idle", self.warning)
            self.view.later(1.6, back)

    def click(self):
        now = time.monotonic()
        if now - self._last_click < CLICK_DEBOUNCE:
            return  # a double-click reads once
        self._last_click = now
        if self.state == "grabbing":
            return  # a click while the selection is being grabbed
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
            log(f"selection error: {type(e).__name__}: {e}")
            text = None
        if tok != self._token:
            return  # user clicked again (stop) meanwhile
        if not text:
            self.post(lambda: self._set("empty"))
            return
        self._speak(text)

    def _speak(self, text):
        self._last_text = text
        self.last_error = ""
        self.engine.speak(text)

    def retry(self):
        """Menu action after an error: try the same text again."""
        if self.state in BUSY:
            return
        if self._last_text:
            self._speak(self._last_text)

    # menu actions
    def set_voice(self, voice):
        eng.VOICE = self.settings["voice"] = voice
        self._save()

    def set_rate(self, rate):
        eng.RATE = self.settings["rate"] = rate
        self._save()

    def set_offline_only(self, on):
        on = bool(on)
        eng.OFFLINE_ONLY = self.settings["offline_only"] = on
        self.engine.set_offline_only(on)
        self._save()
        log("voice mode: offline only" if on else
            "voice mode: online, with the offline voice as backup")
        local = self.engine.local
        missing = local is not None and not local.ready
        if self.state in BUSY:
            # The read in progress began in the other mode; stop it so the next
            # click uses the voice the user just chose.
            self.engine.stop()
            self._set("idle")
        if on and missing:
            self._set("offline_missing")     # orange: download it, then click
            if self.view:
                self.view.later(2.5, self._offline_missing_done)
        elif self.view:
            self.view.note("Offline voice only - click the button to read" if on
                           else "Online voice (offline voice as backup)")

    def _offline_missing_done(self):
        if self.state == "offline_missing":
            self._set("idle")

    def download_offline(self):
        local = self.engine.local
        if local is None:
            return  # still starting; the menu will offer it again
        local.retry_download()
        self._update_changed()

    def set_disk_cache(self, on):
        self.settings["disk_cache"] = bool(on)
        cache = self.engine.cache
        if cache is not None:
            cache.set_disk(on)
            if not on:
                cache.clear_disk()
        self._save()

    def clear_cache(self):
        cache = self.engine.cache
        if cache is not None:
            cache.clear_ram()
            cache.clear_disk()

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
