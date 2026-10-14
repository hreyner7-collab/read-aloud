"""Used by the automatic builds: launch the real app and test it end to end.

1. Microsoft voice: fetch real speech through the app's fast connection and decode it.
2. Offline voice: download the Kokoro model, check its fingerprint, make speech with it.
3. Fallback: pretend Microsoft is unreachable and check a read still plays,
   using the offline voice automatically.
"""
import asyncio
import json
import threading
import time
import traceback


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
                                got.append, {}), eng.loop)
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

            # 3. Microsoft "down" -> automatic switch to the offline voice
            states = []
            original = eng.on_state

            def spy(s):
                states.append(s)
                original(s)
            eng.on_state = spy
            engmod.WSS_OVERRIDE = "ws://127.0.0.1:9/ws"   # nothing listens here

            async def microsoft_down(*args, **kwargs):
                raise ConnectionError("simulated outage")
            eng.voice.synth_fallback = microsoft_down     # block the backup route too
            for c in list(eng.voice.spares):
                eng.voice.spares.remove(c)
            eng.edge_down_since = 0.0
            eng.speak("This sentence should be read by the offline voice.")
            deadline = time.time() + 60
            while time.time() < deadline and not (states and states[-1] in ("idle", "error")
                                                  and "loading" in states):
                time.sleep(0.2)
            eng.on_state = original
            result["fallback_states"] = states
            fallback_ok = "speaking" in states and states[-1] == "idle"

            result["online_ok"], result["offline_ok"], result["fallback_ok"] = (
                online_ok, offline_ok, fallback_ok)
            result["ok"] = online_ok and offline_ok and fallback_ok
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
