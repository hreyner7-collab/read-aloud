"""Daily health check: can the app still get speech from Microsoft?

Uses the app's own connection code (readaloud/engine.py) with whichever
edge-tts version is installed, and prints a JSON result. The workflow runs it
twice: with the edge-tts version the current release was built with, and with
the newest edge-tts on PyPI.
"""
import asyncio
import importlib.metadata
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from readaloud.engine import Voice
    result = {"edge_tts": importlib.metadata.version("edge-tts")}
    text = "This is the Read Aloud daily health check."
    voice = Voice(keep_warm=False)

    # 1. the app's fast route (what users hit first)
    got, t = [], time.time()
    try:
        await asyncio.wait_for(voice.synth(text, "+0%", got.append, {}), 40)
        result["fast_ok"] = sum(map(len, got)) > 1000
        result["fast_ms"] = round((time.time() - t) * 1000)
    except Exception as e:
        result["fast_ok"], result["fast_error"] = False, repr(e)[:400]

    # 2. the standard edge-tts route (the app's backup)
    got = []
    try:
        await asyncio.wait_for(voice.synth_fallback(text, "+0%", got.append), 40)
        result["std_ok"] = sum(map(len, got)) > 1000
    except Exception as e:
        result["std_ok"], result["std_error"] = False, repr(e)[:400]

    if voice.session:
        await voice.session.close()
    result["ok"] = bool(result.get("fast_ok") and result.get("std_ok"))
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
