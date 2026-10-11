"""The dashboard's data for one user, from PostgreSQL, through the same presenter the single-user page uses
(so payloads have exactly the shapes frontend/src/types/api.ts expects). Kept in a small per-process cache
keyed by the user's current profile and run, so dial changes re-rank in milliseconds."""
import re
import threading
from collections import OrderedDict

from .. import catalog, db, userdata
from . import genres
from .pipeline import _attach_measurements, build_enriched

_cache: "OrderedDict[str, tuple]" = OrderedDict()
_lock = threading.Lock()
MAX_USERS = 64


def _medium(url):
    """Deezer covers come in sizes in the URL: derive the 250px one the grid uses."""
    return re.sub(r"/\d+x\d+-", "/250x250-", url) if url and "dzcdn.net" in url else None


def load(user_id):
    """presenter.Data for the user's latest completed run, or None if they have none yet."""
    import presenter
    with db.as_user(user_id) as cur:
        prof = userdata.current_profile(cur)
        run = userdata.latest_run(cur)
        if not prof or not run:
            return None
        stamp = f"{prof['id']}:{run['id']}"
        with _lock:
            hit = _cache.get(str(user_id))
            if hit and hit[0] == stamp:
                _cache.move_to_end(str(user_id))
                return hit[1]
        results = userdata.run_results(cur, run["id"])
    cands = {c["id"]: {**c, "track_id": tid} for tid, c in results.items()}
    _attach_measurements(cands)                        # newer measurements / calibration since the run show up
    enriched, _, _, _ = build_enriched(user_id)
    tids = [t["track_id"] for t in enriched.values()] + [c["track_id"] for c in cands.values()]
    with db.system() as cur:
        cur.execute("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s)", (tids,))
        artist_ids = [r["artist_id"] for r in cur.fetchall()]
        g = genres.by_name(cur, artist_ids)
        cur.execute("SELECT id, cover_url FROM tracks WHERE id = ANY(%s) AND cover_url IS NOT NULL",
                    ([c["track_id"] for c in cands.values()],))
        cand_covers = {r["id"]: r["cover_url"] for r in cur.fetchall()}
        cur.execute("SELECT name, picture_url FROM artists WHERE id = ANY(%s) AND picture_url IS NOT NULL", (artist_ids,))
        photos = {r["name"]: r["picture_url"] for r in cur.fetchall()}
    art = {"tracks": {}, "artists": photos}
    for k, t in enriched.items():
        if t.get("cover"):
            art["tracks"][k] = {"cover": t["cover"], "cover_m": _medium(t["cover"])}
    for c in cands.values():
        url = cand_covers.get(c["track_id"])
        if url:
            art["tracks"][c["id"]] = {"cover": url, "cover_m": _medium(url), "album": c.get("album")}
    data = presenter.Data(prof["profile"], art, cands, enriched, g, calibration={})
    with _lock:
        _cache[str(user_id)] = (stamp, data)
        _cache.move_to_end(str(user_id))
        while len(_cache) > MAX_USERS:
            _cache.popitem(last=False)
    return data


def forget(user_id):
    with _lock:
        _cache.pop(str(user_id), None)
