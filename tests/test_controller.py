"""Controller: one read per double-click, stale events, stop, warnings."""
import types

import pytest

from readaloud import controller as C


class Clock:
    def __init__(self):
        self.now = 1000.0          # the real app never clicks at t=0

    def monotonic(self):
        return self.now


class FakeEngine:
    def __init__(self, on_state, settings=None):
        self.on_state = on_state
        self.settings = settings or {}
        self.gen = 0
        self.speaks = []
        self.stops = 0
        self.offline_calls = []
        self.local = None
        self.cache = None

    def speak(self, text):
        self.gen += 1
        self.speaks.append(text)
        self.on_state("loading", "", self.gen)

    def stop(self):
        self.gen += 1
        self.stops += 1

    def set_offline_only(self, on):
        self.offline_calls.append(on)


class FakeUpdater:
    def __init__(self, on_change=None, on_ready=None):
        self.auto = True
        self.state = "idle"
        self.supported = True
        self.started = 0

    def start_background(self):
        self.started += 1

    def install(self, quit_app):
        pass

    def check_now(self):
        pass

    def status_text(self):
        return "Updates: idle"


class FakeSelection:
    def __init__(self):
        self.has_permission_value = True
        self.text = "Selected words."
        self.asked = 0
        self.calls = 0

    def has_permission(self):
        return self.has_permission_value

    def ask_permission(self):
        self.asked += 1

    def get_selected_text(self):
        self.calls += 1
        return self.text


class FakeView:
    def __init__(self):
        self.shown = []
        self.later_calls = []
        self.notes = []

    def show(self, state, detail=""):
        self.shown.append((state, detail))

    def note(self, text):
        self.notes.append(text)

    def later(self, seconds, fn):
        self.later_calls.append((seconds, fn))

    def update_status_changed(self):
        pass

    def quit_app(self):
        pass


class ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


@pytest.fixture
def made(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(C, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(C, "threading", types.SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(C, "Engine", FakeEngine)
    monkeypatch.setattr(C, "Updater", FakeUpdater, raising=False)
    import readaloud.updater as updater
    monkeypatch.setattr(updater, "Updater", FakeUpdater)
    monkeypatch.setattr(C, "selftest_mode", lambda: False)
    sel = FakeSelection()
    settings = {"voice": "en-GB-SoniaNeural", "rate": "+0%", "offline_only": False,
                "auto_update": False, "x": 0, "y": 0}
    posts = []
    c = C.Controller(lambda fn: fn(), sel, settings)
    c.view = FakeView()
    return c, clock, sel, posts


def test_double_click_reads_once(made):
    c, clock, sel, _ = made
    for _ in range(100):
        c._set("idle")
        clock.now += 1.0
        c.click()
        clock.now += 0.1
        c.click()
    assert len(c.engine.speaks) == 100
    assert sel.calls == 100
    assert c.state == "loading"


def test_second_click_while_busy_stops(made):
    c, clock, sel, _ = made
    c.click()
    assert c.state == "loading"
    clock.now += 1.0
    c.click()
    assert c.engine.stops == 1
    assert c.state == "idle"


def test_click_while_grabbing_is_ignored(made):
    c, clock, sel, _ = made
    c._set("grabbing")
    clock.now += 1.0
    before = c.engine.stops
    token = c._token
    c.click()
    assert c.state == "grabbing"
    assert c.engine.stops == before
    assert c._token == token
    assert sel.calls == 0


def test_empty_selection_flashes_then_idle(made):
    c, clock, sel, _ = made
    sel.text = None
    c.click()
    assert c.state == "empty"
    assert c.view.later_calls[-1][0] == 1.6
    c.view.later_calls[-1][1]()
    assert c.state == "idle"


def test_no_permission_asks_and_does_not_read(made):
    c, clock, sel, _ = made
    sel.has_permission_value = False
    c.click()
    assert c.state == "permission"
    assert sel.asked == 1
    assert c.engine.speaks == []


def test_stale_generation_cannot_change_the_state(made):
    c, clock, sel, _ = made
    c._set("speaking")
    c.engine.gen = 7
    c._engine_state("error", "old failure", 6)
    assert c.state == "speaking"
    assert c.last_error == ""
    c._engine_state("error", "current failure", 7)
    assert c.state == "error"
    assert c.last_error == "current failure"


def test_warning_survives_idle_and_clears_on_next_read(made):
    c, clock, sel, _ = made
    c._set("idle", "Part of the last read was skipped.")
    assert c.warning == "Part of the last read was skipped."
    assert c.view.shown[-1] == ("idle", "Part of the last read was skipped.")
    c._set("loading")
    assert c.warning == ""
    c._set("idle")
    assert c.warning == ""


def test_retry_uses_the_last_selection(made):
    c, clock, sel, _ = made
    c.click()
    clock.now += 1.0
    c.click()                       # stop
    c.retry()
    assert c.engine.speaks == ["Selected words.", "Selected words."]


def test_retry_while_busy_does_nothing(made):
    c, clock, sel, _ = made
    c.click()
    c.retry()
    assert len(c.engine.speaks) == 1


def test_selection_error_is_an_empty_read(made, monkeypatch):
    c, clock, sel, _ = made

    def boom():
        raise RuntimeError("clipboard busy")

    sel.get_selected_text = boom
    c.click()
    assert c.state == "empty"


def test_download_offline_without_engine_local_is_safe(made):
    c, clock, sel, _ = made
    c.engine.local = None
    c.download_offline()             # must not raise
    assert c.offline_status().startswith("Offline voice")


def test_offline_only_switch_applies_and_confirms(made, monkeypatch):
    from readaloud import settings as st
    c, clock, sel, _ = made
    monkeypatch.setattr(C.eng, "OFFLINE_ONLY", False)
    monkeypatch.setattr(st, "save", lambda s: None)
    c.engine.local = types.SimpleNamespace(ready=True)
    c.set_offline_only(True)
    assert c.settings["offline_only"] is True
    assert C.eng.OFFLINE_ONLY is True
    assert c.engine.offline_calls == [True]
    assert c.view.notes[-1].startswith("Offline voice only")
    c.set_offline_only(False)
    assert c.settings["offline_only"] is False
    assert C.eng.OFFLINE_ONLY is False
    assert c.engine.offline_calls == [True, False]
    assert c.view.notes[-1].startswith("Online voice")


def test_offline_only_without_the_model_shows_what_to_do(made, monkeypatch):
    from readaloud import settings as st
    c, clock, sel, _ = made
    monkeypatch.setattr(C.eng, "OFFLINE_ONLY", False)
    monkeypatch.setattr(st, "save", lambda s: None)
    c.engine.local = types.SimpleNamespace(ready=False)
    c.set_offline_only(True)
    assert C.eng.OFFLINE_ONLY is True
    assert c.state == "offline_missing"
    assert c.view.later_calls[-1][0] == 2.5
    c.view.later_calls[-1][1]()
    assert c.state == "idle"


def test_changing_the_voice_mode_stops_a_read_in_the_old_mode(made, monkeypatch):
    from readaloud import settings as st
    c, clock, sel, _ = made
    monkeypatch.setattr(C.eng, "OFFLINE_ONLY", False)
    monkeypatch.setattr(st, "save", lambda s: None)
    c.engine.local = types.SimpleNamespace(ready=True)
    c._set("speaking")
    c.set_offline_only(True)
    assert c.engine.stops == 1
    assert c.state == "idle"
