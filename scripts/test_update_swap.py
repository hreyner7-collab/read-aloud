"""CI test: the self-update swap really works on this OS.

Copies the freshly built app into a temp folder, tells that copy to "update"
itself to the build artifact (--test-update-swap), and checks the new copy was
put in the same place, started, and waited for the old one to quit.

usage: python scripts/test_update_swap.py <built app: ReadAloud.exe or Read Aloud.app> <update file: .exe or .zip>
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def fail(msg):
    print(f"::error::update swap test: {msg}")
    sys.exit(1)


def main(app, update_file):
    work = tempfile.mkdtemp(prefix="swaptest-")
    marker = os.path.join(work, "marker.json")
    update_file = os.path.abspath(update_file)
    if os.name == "nt":
        target = os.path.join(work, "ReadAloud.exe")
        shutil.copy2(app, target)
        cmd = [target]
    else:
        target = os.path.join(work, "Read Aloud.app")
        subprocess.run(["ditto", app, target], check=True)
        cmd = [os.path.join(target, "Contents", "MacOS", "Read Aloud")]
    before = os.path.getmtime(target)
    subprocess.run(cmd + ["--test-update-swap", update_file, marker], timeout=120)
    for _ in range(120):
        if os.path.exists(marker):
            break
        time.sleep(0.5)
    else:
        fail("the updated copy never started (no marker written)")
    time.sleep(1)
    info = json.load(open(marker))
    print(json.dumps(info, indent=2))
    exe = os.path.normcase(os.path.realpath(info["exe"]))
    if not exe.startswith(os.path.normcase(os.path.realpath(work))):
        fail(f"new copy ran from the wrong place: {info['exe']}")
    if not info.get("waited_for"):
        fail("new copy didn't wait for the old one to quit")
    if not os.path.exists(target):
        fail("app missing after the update")
    print(f"PASS: update swapped in place and relaunched "
          f"(target modified: {os.path.getmtime(target) != before})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
