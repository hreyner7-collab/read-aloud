"""The two right-click menus share one short, honest wording set."""
from pathlib import Path

from readaloud import menu_text as mt
from readaloud import settings as st

ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


class Updater:
    supported = True
    auto = True
    staged = None

    def __init__(self, state="idle", text="Up to date"):
        self.state = state
        self._text = text

    def status_text(self):
        return self._text


class Controller:
    def offline_status(self):
        return "offline voice ready"


def test_voice_and_speed_titles_show_the_current_choice():
    voice_id, voice_label = st.VOICES[1]
    rate_id, rate_label = st.SPEEDS[-1]
    settings = {"voice": voice_id, "rate": rate_id}
    assert mt.voice_title(settings) == "Voice · " + voice_label
    assert mt.speed_title(settings) == "Speed · " + rate_label


def test_status_is_one_line_and_bounded():
    line = mt.status_line("part of the last read was skipped. " * 8)
    assert "\n" not in line and len(line) <= 74 and line.endswith("…")
    assert mt.status_line("  two   spaces ") == "two spaces"


def test_update_rows_say_what_the_click_will_do():
    assert mt.update_action(Updater("ready")) == "Install update"
    assert mt.update_action(Updater("idle")) == "Check now"
    assert "daily" in mt.update_status(Updater()).lower()
    manual = Updater("idle")
    manual.auto = False
    assert "daily" not in mt.update_status(manual).lower()


def test_offline_status_starts_with_a_capital():
    assert mt.offline_status(Controller()) == "Offline voice ready"


def test_voice_title_shows_when_offline_only_is_on():
    voice_id, voice_label = st.VOICES[0]
    assert mt.voice_title({"voice": voice_id}) == "Voice · " + voice_label
    assert mt.voice_title({"voice": voice_id, "offline_only": True}) == (
        "Voice · " + voice_label + " · offline only")


def test_windows_offline_rows_put_the_switch_last():
    src = source("readaloud/ui_win.py")
    menu = src[src.index("self.offline_var = tk.BooleanVar"):
               src.index("m.add_cascade(label=mt.voice_title")]
    assert menu.index("mt.offline_status(controller)") < menu.index("mt.OFFLINE_LABEL")
    assert menu.index("mt.DOWNLOAD_LABEL") < menu.index("mt.OFFLINE_LABEL")
    assert 'not bool(self.settings.get("offline_only"))' in menu


def test_mac_menu_selectors_match_their_python_methods():
    src = source("readaloud/ui_mac.py")
    for selector in ("retry:", "clearCache:", "toggleDiskCache:", "toggleAutoUpdate:"):
        assert f'"{selector}"' in src
    for broken in ("retry_", "clearCache_:", "toggleDiskCache_:", "toggleAutoUpdate_:"):
        assert f'"{broken}"' not in src


def test_both_menus_use_compact_groups_and_keep_the_privacy_note():
    for path in ("readaloud/ui_win.py", "readaloud/ui_mac.py"):
        src = source(path)
        assert "menu_text" in src
        assert "Updates" in src and "Audio cache" in src
        assert "mt.CACHE_NOTE" in src
        assert "Keeps audio only" not in src
        assert "Offline voice only (works without internet)" not in src


def test_windows_button_has_hover_and_idle_tooltip():
    src = source("readaloud/ui_win.py")
    assert 'bind("<Enter>", self._enter)' in src
    assert 'bind("<Leave>", self._leave)' in src
    assert "TIP_IDLE" in src
    assert "self._hover" in src and "self._pressed" in src


def test_mac_button_has_hover_tracking():
    src = source("readaloud/ui_mac.py")
    assert "NSTrackingMouseEnteredAndExited" in src
    assert "mouseEntered_" in src and "mouseExited_" in src
    assert "self._hover" in src and "self._pressed" in src
