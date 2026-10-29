"""Read Aloud - a floating button that reads selected text aloud.

Usage:  Read Aloud [--selftest RESULT.json]
Internal: --after-update PID   (started by the old copy after a self-update)
          --update-check       (daily task: check and stage, no UI)
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


def _mac_single_instance():
    """One button per Mac.  The self-updater relaunch passes --after-update,
    which skips this lock while the old copy is still quitting."""
    import fcntl
    from readaloud.paths import data_dir
    f = open(os.path.join(data_dir(), "instance.lock"), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def main():
    from readaloud import updater

    # Headless daily check: no UI, no single-instance lock, no speech engine.
    # It stages only; the app installs the update when idle or at next launch.
    if "--update-check" in sys.argv:
        raise SystemExit(updater.run_daily_check())

    # A self-update just started us: let the old copy finish quitting first
    old_pid = _arg("--after-update")
    if old_pid:
        updater.wait_for_exit(int(old_pid))

    # Windows keeps one permanent copy.  A downloaded .exe installs itself and
    # hands off to it; a newer download replaces it in place, so Downloads no
    # longer fills up with one file per version.
    internal = any(f in sys.argv for f in
                   ("--selftest", "--test-update-swap", "--test-marker"))
    if sys.platform == "win32" and not internal:
        updater.cleanup_after_update()
        from readaloud import install
        outcome = install.adopt()
        if outcome in ("handoff", "already-running", "updated-while-running", "failed"):
            install.explain(outcome)
            return
        if outcome != "disabled":
            from readaloud import settings as st
            st.ensure_login_path()

    _mac_lock = None
    if sys.platform == "darwin" and not old_pid:
        _mac_lock = _mac_single_instance()
        if _mac_lock is None:
            return                      # another Read Aloud is already running

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
