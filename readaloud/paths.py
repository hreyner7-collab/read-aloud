"""Where the app keeps its data and how it logs (no text is ever logged)."""
import os
import sys
import time


def data_dir():
    override = os.environ.get("READ_ALOUD_DATA_DIR")   # tests only
    if override:
        d = override
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Read Aloud")
    elif os.name == "nt":
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Read Aloud")
    else:
        d = os.path.join(os.path.expanduser("~"), ".config", "read-aloud")
    os.makedirs(d, exist_ok=True)
    return d


LOG = os.path.join(data_dir(), "read-aloud.log")


def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)[:300] + "\n")
    except OSError:
        pass
