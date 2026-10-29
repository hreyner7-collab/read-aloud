"""Used by the automatic builds: launch the real app and test it end to end.

1. Microsoft voice: fetch real speech through the app's fast connection and decode it.
2. Offline voice: download the Kokoro model, check its fingerprint, make speech with it.
3. A real read through the whole pipeline (atoms, ledger, streaming player).
4. Stop, then read again (no stale state, no stuck player).
5. Microsoft unreachable and edge-tts blocked: the read still plays offline, and
   every character is accounted for in the ledger.
"""
import asyncio
import json
import threading
import time
import traceback

READ_TEXT = ("The first sentence is read as soon as its audio arrives. "
             "The second sentence follows right after it. "
             "A third one checks that every character is accounted for. "
             "And the last sentence closes the self test.")
FALLBACK_TEXT = ("This passage has never been read before. The online voices "
                 "are unreachable now, so the offline voice has to read every "
                 "sentence and the ledger has to prove it.")


def _covered(led):
    total = led.get("atoms", 0)
    covered = (led.get("spoken", 0) + led.get("partial", 0)
               + led.get("skipped", 0) + led.get("cancelled", 0))
    chars = led.get("total_chars", 0)
    chars_covered = (led.get("spoken_chars", 0) + led.get("partial_chars", 0)
                     + led.get("skipped_chars", 0) + led.get("cancelled_chars", 0))
    return total > 0 and covered == total and chars_covered == chars


def _brief(led):
    keys = ("atoms", "spoken", "partial", "skipped", "cancelled", "total_chars",
            "spoken_chars", "total_chars", "end", "first_ms", "frames", "requests")
    return {k: led.get(k) for k in keys if k in led}


def run_selftest(controller, out_path, quit_app):
    def work():
        result = {"ok": False}
        try:
            time.sleep(2)  # let the UI settle and the warm connection open
            eng = controller.engine
            from . import engine as engmod

            # 1. Microsoft voice
            got = []
            t = time.time()
            fut = asyncio.run_coroutine_threadsafe(
                eng.voice.synth("Hello, this is the Read Aloud self test.", "+0%",
                                got.append, {}, voice=engmod.VOICE), eng.loop)
            fut.result(40)
            audio = b"".join(got)
            result["synth_seconds"] = round(time.time() - t, 2)
            result["mp3_bytes"] = len(audio)
            import miniaudio
            decoded = miniaudio.decode(audio, nchannels=1, sample_rate=24000)
            result["audio_seconds"] = round(decoded.num_frames / 24000, 2)
            result["selection_module"] = controller.sel.__name__
            online_ok = len(audio) > 1000 and decoded.num_frames > 0

            # 2. Offline voice
            t = time.time()
            downloaded = eng.local.download()
            result["offline_download_seconds"] = round(time.time() - t, 1)
            t = time.time()
            eng.local.load()
            result["offline_load_seconds"] = round(time.time() - t, 2)
            t = time.time()
            pcm = eng.local.synth("Hello, this is the offline voice speaking.",
                                  "en-GB-SoniaNeural", "+0%")
            took = time.time() - t
            secs = len(pcm) / 24000
            result["offline_audio_seconds"] = round(secs, 2)
            result["offline_speed_x_realtime"] = round(secs / took, 2)
            offline_ok = downloaded and secs > 1.0

            # 3. one real read through the new pipeline
            def read(text, timeout=120):
                states, original = [], eng.on_state

                def spy(state, detail="", gen=0):
                    states.append((state, detail))
                    original(state, detail, gen)
                eng.on_state = spy
                eng.speak(text)
                gen = eng.gen
                try:
                    # The ledger is written just after the final state callback,
                    # so waiting for the callback would report the previous read.
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        if (eng.last_ledger or {}).get("gen") == gen:
                            break
                        never_started = (eng.session is None
                                         or eng.session.gen != gen)
                        if never_started and states and states[-1][0] in (
                                "error", "offline_missing"):
                            break
                        time.sleep(0.05)
                finally:
                    eng.on_state = original
                led = eng.last_ledger or {}
                # Nothing from another read counts as evidence for this one.
                return states, (led if led.get("gen") == gen else {})

            states, led = read(READ_TEXT)
            result["read_states"] = states
            result["read"] = _brief(led)
            read_ok = led.get("end") == "idle" and led.get("spoken") == led.get("atoms", -1)
            result["read_ok"] = read_ok
            result["atoms_covered"] = _covered(led)

            # 4. stop mid-read, then read again
            eng.speak(READ_TEXT)
            deadline = time.time() + 30
            while time.time() < deadline:
                if eng.busy and eng.session and eng.session.first_ms is not None:
                    break
                time.sleep(0.05)
            t = time.time()
            eng.stop()
            result["stop_seconds"] = round(time.time() - t, 2)
            deadline = time.time() + 10
            while time.time() < deadline and eng.busy:
                time.sleep(0.05)
            stopped_ok = time.time() - t < 1.0
            result["stop_ok"] = stopped_ok
            _s2, led2 = read(READ_TEXT)
            result["restart"] = _brief(led2)
            restart_ok = led2.get("end") == "idle" and led2.get("spoken") == led2.get("atoms", -1)

            # 5. Microsoft unreachable and edge-tts blocked -> offline read.
            # Clear the repeat cache first, or this would prove nothing.
            eng.cache.clear_ram()
            eng.cache.clear_disk()
            original_synth, original_edge = eng.voice.synth, eng.voice.synth_fallback
            network_calls = []

            async def no_fast(*args, **kwargs):
                network_calls.append("fast")
                raise ConnectionError("simulated outage")

            async def no_edge(*args, **kwargs):
                network_calls.append("edge")
                raise ConnectionError("simulated outage")

            eng.voice.synth = no_fast
            eng.voice.synth_fallback = no_edge
            try:
                states3, led3 = read(FALLBACK_TEXT)
            finally:
                eng.voice.synth = original_synth
                eng.voice.synth_fallback = original_edge
            result["fallback_states"] = states3
            result["fallback"] = _brief(led3)
            result["fallback_network_calls"] = len(network_calls)
            fallback_ok = (led3.get("end") == "idle"
                           and led3.get("spoken") == led3.get("atoms", -1)
                           and len(network_calls) > 0)

            result["online_ok"], result["offline_ok"], result["fallback_ok"] = (
                online_ok, offline_ok, fallback_ok)
            result["ok"] = bool(
                online_ok and offline_ok and read_ok and result["atoms_covered"]
                and stopped_ok and restart_ok and fallback_ok
                and _covered(led3) and _covered(led2))
        except Exception:
            result["error"] = traceback.format_exc()
        try:
            from .engine import LOG
            result["log_tail"] = open(LOG, encoding="utf-8").read()[-1500:]
        except Exception:
            pass
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        quit_app()

    threading.Thread(target=work, daemon=True).start()
