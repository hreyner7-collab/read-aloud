"""Read Aloud - a floating button that reads selected text aloud.

Usage:  Read Aloud [--selftest RESULT.json]
Internal: --after-update PID   (started by the old copy after a self-update)
          --test-update-swap STAGED MARKER   (CI: test the update swap)
"""
import json
import os
import sys


def _arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return None


def main():
    from readaloud import updater

    # A self-update just started us: let the old copy finish quitting first
    old_pid = _arg("--after-update")
    if old_pid:
        updater.wait_for_exit(int(old_pid))

    # CI test of the update swap, no UI
    marker = _arg("--test-marker")
    if marker:
        updater.cleanup_after_update()
        with open(marker, "w") as f:
            json.dump({"pid": os.getpid(), "exe": sys.executable,
                       "waited_for": old_pid, "version": updater.VERSION}, f)
        return
    swap = _arg("--test-update-swap")
    if swap:
        updater.swap_in(swap, updater.install_target(),
                        relaunch_args=("--test-marker", sys.argv[sys.argv.index(swap) + 1]))
        return

    updater.cleanup_after_update()
    selftest = _arg("--selftest")
    if sys.platform == "darwin":
        from readaloud.ui_mac import MacApp
        MacApp(selftest=selftest).run()
    else:
        from readaloud.ui_win import WinApp
        WinApp(selftest=selftest).run()


if __name__ == "__main__":
    main()
