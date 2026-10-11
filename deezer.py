"""One shared store for Deezer API answers, used by every stage (clips, new songs, artwork).

Each answer is kept with the time it was fetched and reused only while it is fresh for its kind
(measured Oct 8-9 2026: related-artist lists 25/25 unchanged after a day; artist top-15 lists 1/25
unchanged after a day; artist radio random on every call):

  track/{id}, track/isrc:*      30 days   song facts (preview links inside expire; checked separately)
  artist/{id}                   30 days   artist facts and photo
  artist/{id}/related, search   7 days
  artist/{id}/top, /radio       1 day
  "not found" answers           1 day     (may appear later)

Identical requests already in flight are shared (net.once), so concurrent stages never ask twice.
Answers from the old cache format (no timestamp) count as fetched when that file was last written.
"""
import json
import re
import threading
import time

import net
from config import CACHE

FILE = CACHE / "deezer_cache.json"
DAY = 86400
_lock = threading.Lock()
_store = None
_legacy_time = 0.0
_last_save = time.time()
SAVE_EVERY_S = 20            # answers are written out regularly, so a killed run keeps what it fetched
_backend = None              # optional shared store (frequency.provider_cache.DeezerBackend); None = the JSON file


def install_backend(backend):
    """Keep answers in a shared store (get(key) -> {"t", "v"} | None, put(key, value, ttl_s)) instead of the file."""
    global _backend
    _backend = backend


def ttl(key, value):
    if value is None:
        return DAY
    if re.match(r"artist/\d+/(top|radio)", key):
        return DAY
    if re.match(r"artist/\d+/related", key) or key.startswith("search/"):
        return 7 * DAY
    return 30 * DAY


def _load():
    global _store, _legacy_time
    if _store is None:
        try:
            raw = json.loads(FILE.read_text())
            _legacy_time = FILE.stat().st_mtime
        except Exception:
            raw = {}
        _store = {k: v if isinstance(v, dict) and set(v) == {"t", "v"} else {"t": _legacy_time, "v": v}
                  for k, v in raw.items()}
    return _store


def save():
    if _backend:
        return                                        # the shared store is written answer by answer
    with _lock:
        snapshot = dict(_load())
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot))
    tmp.replace(FILE)


def key_of(path, params):
    return path + "?" + "&".join(f"{a}={b}" for a, b in sorted(params.items()))


def get(path, fresh=False, **params):
    """A Deezer answer from the store if still fresh for its kind, otherwise from Deezer.
    fresh=True always asks Deezer (and refreshes the store)."""
    k = key_of(path, params)
    if not fresh:
        if _backend:
            rec = _backend.get(k)
        else:
            with _lock:
                rec = _load().get(k)
        if rec and time.time() - rec["t"] < ttl(k, rec["v"]):
            net.cache_hit("deezer_fresh_in_store")
            return rec["v"]
        net.cache_hit("deezer_fresh_in_store", False)
        if rec:
            net.cache_hit("deezer_expired_refetched")
    global _last_save
    v = net.once(("deezer", k), lambda: net.deezer_json(path, **params))
    if _backend:
        _backend.put(k, v, ttl(k, v))
        return v
    with _lock:
        _load()[k] = {"t": time.time(), "v": v}
        due = time.time() - _last_save > SAVE_EVERY_S
        if due:
            _last_save = time.time()
    if due:
        save()
    return v


def preview_live(url, margin=120):
    """True if a Deezer preview link hasn't expired (they carry exp=<unix time>)."""
    m = re.search(r"exp=(\d+)", url or "")
    return bool(url and m and int(m.group(1)) > time.time() + margin)


def track_with_preview(path):
    """A track answer whose preview link is usable right now: the stored one if its link is still
    live, otherwise a fresh answer from Deezer."""
    d = get(path)
    if d and d.get("preview") and not preview_live(d["preview"]):
        d = get(path, fresh=True)
    return d


def freshness():
    """Counts of stored answers by state, for the run report."""
    now = time.time()
    if _backend:
        return {"shared_store": True}
    with _lock:
        store = dict(_load())
    out = {"fresh": 0, "stale": 0, "legacy_untimed": 0}
    for k, rec in store.items():
        if rec["t"] == _legacy_time and _legacy_time:
            out["legacy_untimed"] += 1
        out["fresh" if now - rec["t"] < ttl(k, rec["v"]) else "stale"] += 1
    return out
