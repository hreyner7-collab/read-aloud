"""One installed copy on Windows: install in place, hand off, never pile up."""
import os
import sys

import pytest

from readaloud import install


@pytest.fixture
def win(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WIN", True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("READ_ALOUD_NO_INSTALL", raising=False)
    spawned, shortcuts = [], []
    monkeypatch.setattr(install, "_spawn", spawned.append)
    monkeypatch.setattr(install, "_shortcut",
                        lambda target, folder: shortcuts.append(target))
    monkeypatch.setattr(install, "already_running", lambda: False)
    folder = tmp_path / "installed"
    return folder, spawned, shortcuts


def download(tmp_path, data=b"a newer build"):
    path = tmp_path / "ReadAloud-Windows.exe"
    path.write_bytes(data)
    return path


def installed_copy(folder, version, data=b"x" * (install.TOO_SMALL + 1)):
    os.makedirs(folder, exist_ok=True)
    target = folder / install.EXE_NAME
    target.write_bytes(data)
    install._write_side_car(str(folder), version)
    return target


def test_first_download_installs_once_and_hands_off(win, tmp_path):
    folder, spawned, shortcuts = win
    source = download(tmp_path)

    assert install.adopt(source, folder=str(folder)) == "handoff"
    target = folder / install.EXE_NAME
    assert target.read_bytes() == b"a newer build"
    assert install.installed_version(str(folder)) == install.VERSION
    assert spawned == [str(target)]
    assert shortcuts == [str(target)]
    assert sorted(os.listdir(folder)) == sorted([install.EXE_NAME, install.SIDE_CAR])


def test_current_install_hands_off_instead_of_copying_again(win, tmp_path):
    folder, spawned, shortcuts = win
    target = installed_copy(folder, install.VERSION, b"already here" + b"x" * install.TOO_SMALL)
    source = download(tmp_path)

    assert install.adopt(source, folder=str(folder)) == "handoff"
    assert target.read_bytes().startswith(b"already here")
    assert spawned == [str(target)]
    assert shortcuts == []          # no reinstall, no duplicate shortcut


def test_a_second_launch_while_running_says_so(win, tmp_path, monkeypatch):
    folder, spawned, _ = win
    installed_copy(folder, install.VERSION)
    monkeypatch.setattr(install, "already_running", lambda: True)

    assert install.adopt(download(tmp_path), folder=str(folder)) == "already-running"
    assert spawned == []


def test_second_launch_after_an_update_while_running(win, tmp_path, monkeypatch):
    folder, spawned, _ = win
    installed_copy(folder, install.VERSION)
    (folder / (install.EXE_NAME + ".old")).write_bytes(b"the copy in use")
    monkeypatch.setattr(install, "already_running", lambda: True)
    monkeypatch.setattr(install, "VERSION", "1.0.99")

    assert install.adopt(download(tmp_path), folder=str(folder)) == "updated-while-running"
    assert spawned == []
    assert install.installed_version(str(folder)) == "1.0.99"
    # the file the old copy is running from is kept, not deleted underneath it
    assert (folder / (install.EXE_NAME + ".old")).exists()


def test_newer_download_replaces_the_installed_copy(win, tmp_path, monkeypatch):
    folder, spawned, shortcuts = win
    target = installed_copy(folder, "1.0.1")
    monkeypatch.setattr(install, "VERSION", "1.0.2")
    source = download(tmp_path, b"1.0.2 build")

    assert install.adopt(source, folder=str(folder)) == "handoff"
    assert target.read_bytes() == b"1.0.2 build"
    assert install.installed_version(str(folder)) == "1.0.2"
    assert spawned == [str(target)]
    assert shortcuts == [str(target)]


def test_launching_the_installed_copy_repairs_a_stale_sidecar(win):
    folder, spawned, _ = win
    target = installed_copy(folder, "1.0.1")

    assert install.adopt(str(target), folder=str(folder)) == "already-installed"
    assert install.installed_version(str(folder)) == install.VERSION
    assert spawned == []             # the updater already replaced this file


def test_older_download_hands_off_to_the_newer_install(win, tmp_path, monkeypatch):
    folder, spawned, shortcuts = win
    target = installed_copy(folder, "1.0.9")
    monkeypatch.setattr(install, "VERSION", "1.0.2")
    source = download(tmp_path, b"an old download")

    assert install.adopt(source, folder=str(folder)) == "handoff"
    assert target.read_bytes().startswith(b"x" * (install.TOO_SMALL + 1))
    assert spawned == [str(target)]
    assert shortcuts == []


def test_failure_keeps_the_download_working(win, tmp_path):
    _, spawned, _ = win
    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder")

    assert install.adopt(download(tmp_path), folder=str(blocked)) == "failed"
    assert spawned == []


def test_environment_switch_disables_installing(win, tmp_path, monkeypatch):
    folder, spawned, _ = win
    monkeypatch.setenv("READ_ALOUD_NO_INSTALL", "1")

    assert install.adopt(download(tmp_path), folder=str(folder)) == "disabled"
    assert not (folder / install.EXE_NAME).exists()
    assert spawned == []


def test_development_copy_never_installs(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WIN", True)
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert install.adopt(tmp_path / "python.exe", folder=str(tmp_path / "app")) == "not-windows"


def test_install_dir_lives_under_local_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert install.install_dir() == os.path.join(str(tmp_path), "Programs", "Read Aloud")
    assert install.installed_exe().endswith(install.EXE_NAME)


def test_version_comparison():
    assert install.newer("1.0.14", "1.0.13")
    assert not install.newer("1.0.13", "1.0.13")
    assert not install.newer("1.0.12", "1.0.13")
    assert install.newer("1.0.14", "")        # unknown installed version -> replace
    assert not install.newer("dev", "1.0.13")  # a checkout never outranks a release
