"""Live progress for the analysis run, written to cache/progress.json for the dashboard's loading screen.

Each analysis script calls these as it works. Running a script on its own still works (it just
updates the same file). Writes are throttled so tight loops don't hammer the disk.
"""
import json
import os
import threading
import time
from pathlib import Path

from config import CACHE
FILE = CACHE / "progress.json"

# Ten "tracks" on a two-sided record. weight = share of a first run's time, from the measured
# optimised run (genres overlaps the clip stages, so it barely adds to the total).
STAGES = [
    {"key": "login",       "side": "A1", "title": "Sign the guestbook",        "lines": ["Sign", "the guestbook"],   "weight": 1,
     "blurb": "Logging in to Spotify. A browser tab opens. Click Agree."},
    {"key": "pull",        "side": "A2", "title": "Pull your records",         "lines": ["Pulling", "your records"],  "weight": 1,
     "blurb": "Fetching your top, recent and saved songs from Spotify."},
    {"key": "notes",       "side": "A3", "title": "Read the liner notes",      "lines": ["Reading", "the liner notes"], "weight": 1,
     "blurb": "Looking up official measurements for each song."},
    {"key": "previews",    "side": "A4", "title": "Listen to the previews",    "lines": ["Listening", "to previews"], "weight": 14,
     "blurb": "Finding 30-second clips on Deezer and iTunes for songs with no official data."},
    {"key": "lowend",      "side": "A5", "title": "Measure the low end",       "lines": ["Measuring", "the low end"], "weight": 32,
     "blurb": "Measuring bass straight from each clip, and estimating the missing songs."},
    {"key": "calibrate",   "side": "B1", "title": "Tune the instruments",      "lines": ["Tuning", "the instruments"], "weight": 11,
     "blurb": "Checking clip estimates against official numbers and correcting the drift."},
    {"key": "genres",      "side": "B2", "title": "Ask the record-store clerks", "lines": ["Asking", "the clerks"],   "weight": 2,
     "blurb": "Getting genres for every artist from MusicBrainz. Slow on purpose: one question a second."},
    {"key": "fingerprint", "side": "B3", "title": "Press your fingerprint",    "lines": ["Pressing", "your fingerprint"], "weight": 1,
     "blurb": "Weighing everything you play and finding your worlds."},
    {"key": "crates",      "side": "B4", "title": "Dig the crates",            "lines": ["Digging", "the crates"],    "weight": 28,
     "blurb": "Following your artists to their scene-mates and measuring new songs."},
    {"key": "sleeves",     "side": "B5", "title": "Print the sleeves",         "lines": ["Printing", "the sleeves"],  "weight": 1,
     "blurb": "Fetching cover art and artist photos."},
]
KEYS = [s["key"] for s in STAGES]
MAX_COVERS, MAX_FACTS = 48, 8

_state = None
_last_write = 0.0
_lock = threading.RLock()          # stages can now run side by side, from several threads
_here = threading.local()          # which stage this thread is reporting for


def _blank():
    return {"status": "idle", "stage": None, "now": None, "error": None, "started": None, "updated": None,
            "pid": None, "covers": [], "facts": [],
            "stages": {k: {"state": "pending", "done": 0, "total": None, "label": None, "started": None, "ended": None}
                       for k in KEYS}}


def read():
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return _blank()


def _load():
    global _state
    if _state is None:
        _state = read()
        for k in KEYS:                                 # tolerate files from older stage lists
            _state["stages"].setdefault(k, _blank()["stages"][k])
    return _state


def _write(force=False):
    global _last_write
    now = time.time()
    if not force and now - _last_write < 0.4:
        return
    _last_write = now
    s = _load()
    s["updated"] = now
    CACHE.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, FILE)


def _mine():
    return getattr(_here, "stage", None) or _load().get("stage")


def begin_run():
    """Fresh progress for a new run (keeps nothing from the last one)."""
    global _state
    with _lock:
        _state = _blank()
        _state.update(status="running", started=time.time(), pid=os.getpid())
        _write(force=True)


def stage(key, total=None, label=None):
    """This thread now reports for `key`. Stages may overlap; each is finished explicitly."""
    with _lock:
        s = _load()
        st = s["stages"][key]
        if st["state"] != "active":
            st.update(state="active", started=time.time(), done=0)
        st.update(total=total, label=label)
        s["stage"] = key                               # the headline shows the latest stage to start
        _here.stage = key
        _write(force=True)


def tick(done, total=None, now=None, label=None):
    with _lock:
        s = _load()
        key = _mine()
        if not key:
            return
        st = s["stages"][key]
        st["done"] = done
        if total is not None:
            st["total"] = total
        if label is not None:
            st["label"] = label
        if now:
            s["now"] = now
        _write()


def finish(key):
    with _lock:
        s = _load()
        s["stages"][key].update(state="done", ended=time.time())
        if s["stages"][key]["total"]:
            s["stages"][key]["done"] = s["stages"][key]["total"]
        if s.get("stage") == key:                      # hand the headline to a stage still running
            running = [k for k in KEYS if s["stages"][k]["state"] == "active"]
            s["stage"] = running[-1] if running else key
        _write(force=True)


def cover(url, title="", artist="", tag=None):
    if not url:
        return
    with _lock:
        s = _load()
        if any(c["url"] == url for c in s["covers"][-MAX_COVERS:]):
            return
        s["covers"].append({"url": url, "title": title, "artist": artist, "tag": tag})
        s["covers"] = s["covers"][-MAX_COVERS:]
        _write()


def fact(text, key=None):
    """A live note for the margin. A keyed fact replaces the previous one with the same key."""
    with _lock:
        s = _load()
        if key:
            s["facts"] = [f for f in s["facts"] if f.get("key") != key]
        s["facts"].append({"text": text, "key": key, "stage": _mine(), "t": time.time()})
        s["facts"] = s["facts"][-MAX_FACTS:]
        _write(force=True)


def stage_times():
    with _lock:
        s = _load()
        return {k: round(v["ended"] - v["started"], 1) for k, v in s["stages"].items() if v["started"] and v["ended"]}


def end_run(error=None):
    with _lock:
        s = _load()
        if error:
            s.update(status="error", error=error)
        else:
            for k in KEYS:
                if s["stages"][k]["state"] != "done":
                    s["stages"][k].update(state="done", ended=time.time())
            s.update(status="done", stage=None, now=None)
        _write(force=True)
