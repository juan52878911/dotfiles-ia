"""Imported by every test module: the Nod emitter writes to a throw-away file and never POSTs, so a test run
does not touch the real ~/.local/state/nod/events.jsonl nor the collector on :8787 (subprocesses inherit it)."""
import atexit
import os
import shutil
import tempfile

if "DELEGA_TEST_NOD_DIR" not in os.environ:
    _dir = tempfile.mkdtemp(prefix="delega-nod-")
    atexit.register(shutil.rmtree, _dir, True)
    os.environ["DELEGA_TEST_NOD_DIR"] = _dir
    os.environ["NOD_EVENTS_FILE"] = os.path.join(_dir, "events.jsonl")
    os.environ["NOD_COLLECTOR_URL"] = ""
