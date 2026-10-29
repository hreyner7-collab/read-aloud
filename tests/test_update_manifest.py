"""Only a fully described, verified build from the download site can arrive."""
import hashlib
import json
import sys
import types

import pytest

from readaloud import updater as U

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows .exe install target")

BASE = "https://read-aloud-kvtn6ncvzmt.qoder.website/manifest.json"
GOOD = b"pretend this is a new build"
SHA = hashlib.sha256(GOOD).hexdigest()


def entry(**overrides):
    data = {"name": "ReadAloud-1.0.13.exe", "platform": "Windows 11 64-bit",
            "path": "/files/ReadAloud-1.0.13.exe", "size_bytes": len(GOOD),
            "sha256": SHA}
    data.update(overrides)
    return data


def manifest(**overrides):
    data = {
        "version": "1.0.13",
        "artifacts": {
            "windows": entry(),
            "macos-arm64": entry(name="ReadAloud-Mac.zip",
                                 path="/files/ReadAloud-Mac.zip"),
            "macos-x86_64": entry(name="ReadAloud-Mac-Intel.zip",
                                  path="/files/ReadAloud-Mac-Intel.zip"),
        },
    }
    data.update(overrides)
    return data


class Response:
    def __init__(self, body, chunk=5):
        self.body, self.chunk = body, chunk

    def read(self, n):
        out, self.body = self.body[:min(n, self.chunk)], self.body[min(n, self.chunk):]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Runner:
    """Records Task Scheduler / launchctl calls like the real tools would."""

    def __init__(self, fail=False):
        self.calls = []
        self.tasks = {}
        self.fail = fail

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        code = 1 if self.fail else 0
        if args[1] == "/Create":
            self.tasks[args[3]] = list(args)
        elif args[1] == "/Delete":
            self.tasks.pop(args[3], None)
        return types.SimpleNamespace(returncode=code, stdout="", stderr="no")


def staged(tmp_path, monkeypatch, version="1.0.13", body=GOOD, name="ReadAloud-1.0.13.exe"):
    monkeypatch.setattr(U, "data_dir", lambda: str(tmp_path))
    folder = tmp_path / "update"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(body)
    art = U.Artifact(version=version, name=name,
                     url=BASE.replace("manifest.json", "files/" + name),
                     sha256=hashlib.sha256(body).hexdigest(), size=len(body),
                     platform="Windows 11", path=str(folder / name))
    U.write_staged_meta(art)
    return art


# ---------------------------------------------------------------- manifest

def test_versions_and_platforms_are_parsed_strictly():
    assert U.parse_version("v1.0.13") == (1, 0, 13)
    for bad in ("dev", "", "1", "1.x", "1.0.0.0.1"):
        assert U.parse_version(bad) is None
    assert U.artifact_key("win32", "AMD64") == "windows"
    assert U.artifact_key("darwin", "arm64") == "macos-arm64"
    assert U.artifact_key("darwin", "x86_64") == "macos-x86_64"
    assert U.artifact_key("linux", "x86_64") is None


def test_a_valid_manifest_names_one_verified_artifact_per_platform():
    art = U.parse_manifest(manifest(), "windows")
    assert art.version == "1.0.13"
    assert art.url == "https://read-aloud-kvtn6ncvzmt.qoder.website/files/ReadAloud-1.0.13.exe"
    assert art.size == len(GOOD) and art.sha256 == SHA
    arm = U.parse_manifest(manifest(), "macos-arm64")
    assert arm.name == "ReadAloud-Mac.zip"
    intel = U.parse_manifest(manifest(), "macos-x86_64")
    assert intel.name == "ReadAloud-Mac-Intel.zip"


def test_the_older_single_build_manifest_is_still_understood():
    flat = {"version": "1.0.12",
            **entry(name="ReadAloud-1.0.12.exe", path="/files/ReadAloud-1.0.12.exe")}
    art = U.parse_manifest(flat, "windows")
    assert art.version == "1.0.12" and art.size == len(GOOD)
    assert art.url == "https://read-aloud-kvtn6ncvzmt.qoder.website/files/ReadAloud-1.0.12.exe"
    with pytest.raises(U.UpdateError):
        U.parse_manifest(flat, "macos-arm64")


@pytest.mark.parametrize("change", [
    {"version": "dev"},
    {"version": None},
    {"artifacts": None},
    {"artifacts": {"macos-arm64": entry()}},
])
def test_manifest_without_a_usable_version_or_build_is_rejected(change):
    with pytest.raises(U.UpdateError):
        U.parse_manifest(manifest(**change), "windows")


@pytest.mark.parametrize("bad", [
    {"path": "https://evil.example/ReadAloud.exe"},
    {"path": "//evil.example/ReadAloud.exe"},
    {"path": "http://read-aloud-kvtn6ncvzmt.qoder.website/x.exe"},
    {"sha256": "abc"},
    {"sha256": None},
    {"size_bytes": True},
    {"size_bytes": 0},
    {"size_bytes": U.MAX_ARTIFACT_BYTES + 1},
    {"name": "../escape.exe"},
    {"name": "file.exe.part"},
    {"name": ""},
])
def test_manifest_rejects_an_untrusted_or_unverifiable_artifact(bad):
    payload = manifest()
    payload["artifacts"]["windows"].update(bad)
    with pytest.raises(U.UpdateError):
        U.parse_manifest(payload, "windows")


def test_a_non_https_manifest_is_rejected():
    with pytest.raises(U.UpdateError):
        U.parse_manifest(manifest(), "windows", base_url="http://example.com/m.json")


@pytest.mark.parametrize("url", ["http://read-aloud-kvtn6ncvzmt.qoder.website/new",
                                  "https://other.example/new"])
def test_update_redirect_cannot_leave_the_https_site(url):
    from urllib.request import Request
    with pytest.raises(U.UpdateError):
        U._SameSiteRedirect().redirect_request(Request(BASE), None, 302, "", {}, url)


def test_same_site_https_redirect_is_allowed():
    from urllib.request import Request
    url = BASE.replace("manifest.json", "files/current.exe")
    request = U._SameSiteRedirect().redirect_request(Request(BASE), None, 302, "", {}, url)
    assert request.full_url == url


@pytest.mark.parametrize("name", ["../outside.exe", "C:/outside.exe", "new.zip"])
def test_staged_metadata_cannot_escape_or_change_platform(tmp_path, monkeypatch, name):
    art = staged(tmp_path, monkeypatch)
    meta = art.meta()
    meta["name"] = name
    (tmp_path / "update" / "staged.json").write_text(json.dumps(meta))
    assert U.load_staged(current="1.0.12") is None
    assert (tmp_path / "update" / art.name).read_bytes() == GOOD


def test_a_legacy_feed_publishes_only_the_windows_build():
    payload = {"version": "1.0.13", "path": "/files/ReadAloud-1.0.13.exe",
               "name": "ReadAloud-1.0.13.exe", "sha256": SHA,
               "size_bytes": len(GOOD)}
    assert U.parse_manifest(payload, "windows").name == "ReadAloud-1.0.13.exe"
    with pytest.raises(U.NoBuildError):
        U.parse_manifest(payload, "macos-arm64")


def test_a_feed_without_this_platform_is_not_a_failed_check(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(U, "VERSION", "1.0.12")
    monkeypatch.setattr(U, "install_target", lambda: str(tmp_path / "Read Aloud.app"))
    monkeypatch.setattr(U, "artifact_key", lambda: "macos-arm64")
    payload = json.dumps({"version": "1.0.13", "path": "/files/ReadAloud-1.0.13.exe",
                          "name": "ReadAloud-1.0.13.exe", "sha256": SHA,
                          "size_bytes": len(GOOD)}).encode()
    monkeypatch.setattr(U, "_get", lambda url, timeout=60: Response(payload, chunk=1 << 20))
    up = U.Updater()
    up.check()
    assert up.state == "not_available"
    assert "no update published" in up.status_text()


# ---------------------------------------------------------------- download

def test_a_download_is_size_and_hash_checked_before_it_is_staged(tmp_path):
    art = U.parse_manifest(manifest(), "windows")
    seen = []
    path = U.download_artifact(art, folder=str(tmp_path),
                               opener=lambda url: Response(GOOD),
                               on_progress=seen.append)
    assert (tmp_path / art.name).read_bytes() == GOOD
    assert seen[-1] == 1.0
    assert not list(tmp_path.glob("*.part"))
    assert path == str(tmp_path / art.name)


def test_a_truncated_checksum_or_oversized_download_is_never_staged(tmp_path):
    art = U.parse_manifest(manifest(), "windows")
    for body in (GOOD[:-2], GOOD[:-1] + b"X", GOOD + b"more"):
        with pytest.raises(U.UpdateError):
            U.download_artifact(art, folder=str(tmp_path),
                                opener=lambda url, b=body: Response(b, chunk=len(b)))
        assert not (tmp_path / art.name).exists()
    assert not list(tmp_path.glob("*.part"))


def test_a_complete_staged_file_is_reused_without_another_download(tmp_path):
    art = U.parse_manifest(manifest(), "windows")
    (tmp_path / art.name).write_bytes(GOOD)

    def no_network(url):
        raise AssertionError("should not download again")

    path = U.download_artifact(art, folder=str(tmp_path), opener=no_network)
    assert path == str(tmp_path / art.name)


# ---------------------------------------------------------------- staging

@windows_only
def test_a_staged_newer_build_is_found_and_verified(tmp_path, monkeypatch):
    art = staged(tmp_path, monkeypatch)
    found = U.load_staged(current="1.0.12")
    assert found is not None and found.path == art.path


@windows_only
def test_an_old_or_damaged_staged_build_is_discarded(tmp_path, monkeypatch):
    art = staged(tmp_path, monkeypatch, version="1.0.12")
    assert U.load_staged(current="1.0.12") is None
    assert not (tmp_path / "update" / art.name).exists()

    art = staged(tmp_path, monkeypatch)
    (tmp_path / "update" / art.name).write_bytes(GOOD[:-1] + b"X")
    assert U.load_staged(current="1.0.12") is None
    assert not (tmp_path / "update" / art.name).exists()


def test_the_updater_resumes_a_daily_task_download_off_the_ui_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "VERSION", "1.0.12")
    monkeypatch.setattr(U, "install_target", lambda: str(tmp_path / "ReadAloud.exe"))
    monkeypatch.setattr(U, "artifact_key", lambda: "windows")
    staged(tmp_path, monkeypatch)
    ready = []
    up = U.Updater(on_ready=lambda: ready.append(1))
    assert up.state == "idle"            # nothing is promised before the hash
    up.resume()
    assert up.state == "ready" and up.staged and ready == [1]
    assert up.artifact.version == "1.0.13"


def test_a_check_stages_only_the_newer_verified_build(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(U, "VERSION", "1.0.12")
    monkeypatch.setattr(U, "install_target", lambda: str(tmp_path / "ReadAloud.exe"))
    monkeypatch.setattr(U, "artifact_key", lambda: "windows")
    payload = json.dumps(manifest()).encode()

    def fake_get(url, timeout=60):
        return Response(payload if url == U.MANIFEST_URL else GOOD, chunk=1 << 20)

    monkeypatch.setattr(U, "_get", fake_get)
    up = U.Updater()
    up.check()
    assert up.state == "ready" and up.latest == "1.0.13"
    path = tmp_path / "update" / "ReadAloud-1.0.13.exe"
    assert path.read_bytes() == GOOD
    assert json.loads((tmp_path / "update" / "staged.json").read_text())["sha256"] == SHA


def test_install_refuses_a_staged_file_that_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "VERSION", "1.0.12")
    monkeypatch.setattr(U, "install_target", lambda: str(tmp_path / "ReadAloud.exe"))
    monkeypatch.setattr(U, "artifact_key", lambda: "windows")
    art = staged(tmp_path, monkeypatch)
    up = U.Updater()
    up.resume()
    called = []
    monkeypatch.setattr(U, "swap_in", lambda *a, **k: called.append(a))
    (tmp_path / "update" / art.name).write_bytes(b"tampered")
    assert up.install(lambda: None) is False
    assert called == []
    assert up.state == "error"


def test_install_swaps_only_the_verified_staged_file(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "VERSION", "1.0.12")
    target = str(tmp_path / "ReadAloud.exe")
    (tmp_path / "ReadAloud.exe").write_bytes(b"old")
    monkeypatch.setattr(U, "install_target", lambda: target)
    monkeypatch.setattr(U, "artifact_key", lambda: "windows")
    staged(tmp_path, monkeypatch)
    up = U.Updater()
    up.resume()
    called = []
    monkeypatch.setattr(U, "swap_in", lambda *a, **k: called.append(a))
    quit_called = []
    assert up.install(lambda: quit_called.append(1)) is True
    assert called == [(up.staged, target)]
    assert quit_called == [1]


# ---------------------------------------------------------------- leftovers

@windows_only
def test_cleanup_keeps_the_staged_update_and_restores_a_missing_app(tmp_path, monkeypatch):
    folder = tmp_path / "update"
    folder.mkdir()
    keep = folder / "ReadAloud-1.0.13.exe"
    keep.write_bytes(GOOD)
    stale = folder / ".ReadAloud.exe.123.1.part"
    stale.write_bytes(b"half")
    import os
    import time
    old = time.time() - 7200
    os.utime(stale, (old, old))
    target = tmp_path / "ReadAloud.exe"
    (tmp_path / "ReadAloud.exe.old").write_bytes(b"previous")
    U.cleanup_after_update(target=str(target), folder=str(folder))
    assert not stale.exists()
    assert keep.read_bytes() == GOOD           # a staged update is not a leftover
    assert target.read_bytes() == b"previous"  # interrupted swap is repaired


# ---------------------------------------------------------------- daily task

def test_windows_daily_task_is_one_least_privilege_update_check():
    xml = U.daily_task_xml(r"C:\Program Files\Read & Co\ReadAloud.exe",
                           user=r"PC\alice", start="2026-10-29T12:00:00")
    assert xml.count("<CalendarTrigger>") == 1
    assert "<Arguments>--update-check</Arguments>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<ExecutionTimeLimit>PT30M</ExecutionTimeLimit>" in xml
    assert "Read &amp; Co" in xml


def test_windows_setup_repeatedly_leaves_exactly_one_named_task(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "IS_WIN", True)
    monkeypatch.setattr(U, "IS_MAC", False)
    monkeypatch.setattr(U, "data_dir", lambda: str(tmp_path))
    runner = Runner()
    assert U.install_daily_task(exe=r"C:\Apps\ReadAloud.exe", run=runner,
                                user=r"PC\alice") is True
    assert U.install_daily_task(exe=r"C:\Apps\ReadAloud.exe", run=runner,
                                user=r"PC\alice") is True
    assert len(runner.tasks) == 1
    assert all("/F" in call for call in runner.calls if call[1] == "/Create")
    assert U.remove_daily_task(exe=r"C:\Apps\ReadAloud.exe", run=runner) is True
    assert runner.tasks == {}


def test_a_development_copy_never_touches_a_real_daily_task(monkeypatch):
    monkeypatch.setattr(U, "install_target", lambda: None)
    runner = Runner()
    assert U.install_daily_task(run=runner) is False
    assert U.remove_daily_task(run=runner) is False
    assert runner.calls == []


def test_mac_daily_task_is_one_launch_agent_with_the_same_label(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "IS_WIN", False)
    monkeypatch.setattr(U, "IS_MAC", True)
    monkeypatch.setattr(U, "install_target", lambda: "/Applications/Read Aloud.app")
    plist = tmp_path / "com.jreyneruk.readaloud.dailycheck.plist"
    monkeypatch.setattr(U, "_launch_agent_path", lambda: str(plist))
    runner = Runner()
    for _ in range(2):
        assert U.install_daily_task(run=runner) is True
    import plistlib
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == U.LAUNCH_AGENT
    assert data["ProgramArguments"][0].replace("\\", "/") == (
        "/Applications/Read Aloud.app/Contents/MacOS/Read Aloud")
    assert data["ProgramArguments"][1] == "--update-check"
    assert data["StartCalendarInterval"] == {"Hour": U.DAILY_HOUR, "Minute": 0}
    assert not any(call[1] == "/Create" for call in runner.calls)
    assert U.remove_daily_task(exe="/Applications/Read Aloud.app/Contents/MacOS/Read Aloud",
                               run=runner) is True
    assert not plist.exists()
