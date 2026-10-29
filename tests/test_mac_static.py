"""Static checks for the code CI cannot exercise on this machine (Mac/CI wiring)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_bundle_version_comes_from_version_py():
    spec = read("ReadAloud.spec")
    assert 'readaloud", "version.py"' in spec
    assert 'CFBundleShortVersionString": VERSION' in spec
    assert 'CFBundleVersion": VERSION' in spec
    assert '1.0.0' not in spec


def test_mac_single_instance_has_an_updater_bypass():
    main = read("main.py")
    assert "fcntl.flock" in main
    assert 'sys.platform == "darwin" and not old_pid' in main
    assert 'old_pid = _arg("--after-update")' in main
    assert "return                      # another Read Aloud is already running" in main


def test_mac_welcome_is_only_marked_after_dismissal():
    src = read("readaloud/ui_mac.py")
    welcome = src[src.index("    def welcome("):src.index("    def run(")]
    assert welcome.index("a.runModal()") < welcome.index('self.settings["welcomed"] = True')
    assert "setLevel_(NSStatusWindowLevel)" in welcome


def test_windows_welcome_is_only_marked_after_dismissal():
    src = read("readaloud/ui_win.py")
    welcome = src[src.index("    def welcome("):src.index("    def run(")]
    assert welcome.index("messagebox.showinfo") < welcome.index(
        'self.settings["welcomed"] = True')


def test_ci_runs_unit_tests_compileall_and_selftest():
    ci = read(".github/workflows/build.yml")
    assert "pytest==8.3.4" in ci
    assert "python -m pytest -q" in ci
    assert "python -m compileall" in ci
    assert "python scripts/check_selftest.py" in ci
    for runner in ("windows-latest", "macos-latest", "macos-15-intel"):
        assert runner in ci


def test_ci_checks_plist_version_and_second_launch():
    ci = read(".github/workflows/build.yml")
    assert "CFBundleShortVersionString" in ci
    assert "--after-update 999999" in ci
    assert "second launch is still running" in ci


def test_check_selftest_rejects_partial_and_skipped():
    src = read("scripts/check_selftest.py")
    assert 'led.get("partial")' in src
    assert 'led.get("skipped")' in src
    assert 'led.get("spoken") != led.get("atoms")' in src
    assert "fallback_network_calls" in src
    assert "offline fallback made no network attempts" in src


def test_selftest_fallback_clears_the_cache_and_uses_new_text():
    src = read("readaloud/selftest.py")
    assert "FALLBACK_TEXT" in src
    assert "eng.cache.clear_ram()" in src
    assert "read(FALLBACK_TEXT)" in src


def test_selftest_asserts_every_atom_was_spoken():
    src = read("readaloud/selftest.py")
    assert 'led.get("spoken") == led.get("atoms", -1)' in src
    assert "_covered(led3)" in src
