"""Used by the automatic builds: launch the real app, fetch real speech, report.

Proves the packaged app starts, the button window is created, the bundled
libraries load, Microsoft's voice service answers, and the MP3 decodes.
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
            result["ok"] = len(audio) > 1000 and decoded.num_frames > 0
        except Exception:
            result["error"] = traceback.format_exc()
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        quit_app()

    threading.Thread(target=work, daemon=True).start()
