# typed: true

"""Run the engine against a fake Microsoft server under a given scenario and
measure the audible pauses (runs of pure digital silence inside played speech).

The engine is invoked exactly as the app does (Engine(speak)) with the
monkeypatched TapDevice recording everything.

PASS: fewer than 2 pauses each under 200 ms, and the total silent time inside
speech under 250 ms.  A real listener would find anything above that jarring.

Usage:
    python tools/ctl_scenario.py --scenario <name>

The scenario server is started on a free port and torn down when the run ends.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ctl-scenario")

import audiotap  # must monkeypatch before importing engine
import readaloud.engine as eng

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import readaloud.engine

# Point the engine at our fake server via the env override the app already
# supports for testing.  Set once; updated per-run with the free port.
os.environ["READ_ALOUD_WSS_URL"] = "ws://127.0.0.1:PORT_PLACEHOLDER"
os.environ["READ_ALOUD_AUDIO_BACKEND"] = "null"     # no real device needed

# Monkeypatch PlaybackDevice -> TapDevice BEFORE engine creates anything.
audiotap.make_device()

from readaloud.engine import Engine

SAMPLE = (
    "Read Aloud turns whatever you have selected into speech using the same "
    "natural voices as Microsoft Edge."
)

TEXT = (SAMPLE + " " + SAMPLE + " " + SAMPLE + " "
        "The quick brown fox jumps over the lazy dog. "
        "Sphinx of black quartz, judge my vow. "
        "How vexingly quick daft zebras jump!")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _start_server(name: str, port: int) -> subprocess.Popen:
    script = os.path.join(os.path.dirname(__file__), "fake_server.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, script,
         "--scenario", name, "--port", str(port), "--chunks", "5"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        if os.name == "nt" else 0,
    )
    # wait until the server prints "listening"
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate(timeout=1)
            raise RuntimeError(
                f"fake server failed to start for scenario {name!r}:\n"
                f"{err.decode('utf-8', 'replace')}")
        line = proc.stderr.readline().decode("utf-8", "replace")
        if "listening" in line.lower():
            return proc
        time.sleep(0.05)
    raise RuntimeError(f"fake server scenario {name!r} did not print listening")


def run_scenario(name: str) -> Dict[str, Any]:
    port = _free_port()
    proc = _start_server(name, port)
    os.environ["READ_ALOUD_WSS_URL"] = f"ws://127.0.0.1:{port}"
    done = threading.Event()
    errors: List[Exception] = []

    def on_state(s):
        if s in ("idle", "error"):
            done.set()

    try:
        t0 = time.perf_counter()
        engine = Engine(on_state)
        t_engine = time.perf_counter() - t0

        engine.speak(TEXT)
        ok = done.wait(120)
        if not ok:
            errors.append(RuntimeError("engine did not finish within 120 s"))

        time.sleep(0.3)   # let the last packets drain through the tap
        tap = audiotap.last()
        rep = tap.report(min_ms=30)

        first_sound_ms = (tap.times[0] * 1000) if tap.times else None

        return dict(
            scenario=name,
            ok=ok and not errors,
            errors=[e.__class__.__name__ for e in errors],
            wall_s=round(time.perf_counter() - t0, 2),
            engine_start_s=round(t_engine, 3),
            first_sound_ms=first_sound_ms,
            pauses=rep["pauses"],
            pause_total_ms=round(rep["pause_total_s"] * 1000, 1),
            pause_max_ms=round(rep["pause_max_s"] * 1000, 1),
            pause_detail=rep["pauses_detail"],
            lead_in_ms=round(rep["lead_in_s"] * 1000, 1),
            speech_s=round(rep["speech_s"], 2),
            late_callbacks=rep["late_callbacks"],
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", required=True,
                   help="scenario name passed to fake_server.py")
    args = p.parse_args()
    r = run_scenario(args.scenario)
    print("\n=== %s ===" % r["scenario"])
    for k in ("ok", "wall_s", "first_sound_ms", "pauses",
              "pause_total_ms", "pause_max_ms", "lead_in_ms",
              "speech_s", "late_callbacks"):
        print(f"  {k:16}: {r[k]}")
    if r["pause_detail"]:
        print("  pause_detail :", r["pause_detail"])
    if r["errors"]:
        print("  errors       :", r["errors"])
    print(f"\n  verdict: {'PASS' if r['ok'] else 'FAIL'}")
    if r["errors"]:
        print("  (run failed before playback)")
    elif r["pauses"] >= 2 or r["pause_total_ms"] >= 250:
        print("  (audible pauses too large)")
    return 0 if r["ok"] and r["pauses"] < 2 and r["pause_total_ms"] < 250 else 1


if __name__ == "__main__":
    sys.exit(main())
