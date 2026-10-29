"""pytest setup: temp data dir, no real audio, no network.

Must run before `readaloud` is imported: paths.LOG is fixed at import time.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="readaloud-tests-")
os.environ["READ_ALOUD_DATA_DIR"] = _TMP
os.environ["READ_ALOUD_AUDIO_BACKEND"] = "null"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
