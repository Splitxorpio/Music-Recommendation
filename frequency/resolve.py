"""Turn source sightings into canonical catalog tracks, using as few outside requests as possible.

  1. Sightings that carry an ISRC (the Spotify pull) are resolved directly. No requests.
  2. Spotify ids we've seen before resolve from track_identifiers. No requests.
  3. Other Spotify ids (history uploads): ReccoBeats /v1/track, 40 ids per request (~1/8 of an analysis
     unit each), gives the ISRC and credited artists. Answers are cached for everyone in provider_cache.
  4. Name-only sightings (Last.fm, basic history, ids ReccoBeats lacks): Deezer search; a result is accepted
     only when the normalised title and lead artist both match exactly, and is stored as a 'fuzzy' match.
     Unmatched songs are still kept (name only) but can't be measured; they're counted in the import stats.
"""
import time

from . import catalog, db, provider_cache
from .sources import Sighting

RECCO = "https://api.reccobeats.com/v1"
RECCO_TRACK_FOUND_S, RECCO_TRACK_MISSING_S = 30 * 86400, 86400


def _recco_tracks(spotify_ids: list[str]) -> dict[str, dict]:
    """{spotify_id: ReccoBeats track} for ids ReccoBeats knows (cached; misses remembered a day)."""
    import net
    out, todo = {}, []
    for sid in spotify_ids:
        rec = provider_cache.get("reccobeats_track", sid)
        if rec and time.time() - rec["t"] < (RECCO_TRACK_FOUND_S if rec["v"] else RECCO_TRACK_MISSING_S):
            if rec["v"]:
                out[sid] = rec["v"]
        else:
            todo.append(sid)
    for k in range(0, len(todo), 40):
        chunk = todo[k:k + 40]
        r = net.get("reccobeats", f"{RECCO}/track", params={"ids": ",".join(chunk)})
        if not r.ok:
            continue                                         # asked again next time
        got = {}
        for t in r.json().get("content", []):
            sid = (t.get("href") or "").rstrip("/").split("/")[-1]
            if sid:
                got[sid] = t
        for sid in chunk:
            provider_cache.put("reccobeats_track", sid, got.get(sid),
                               RECCO_TRACK_FOUND_S if sid in got else RECCO_TRACK_MISSING_S, 200 if sid in got else 404)
        out.update(got)
    return out


def deezer_match(title: str, artist: str) -> dict | None:
    """Exact-match Deezer search: the result must agree on title and lead artist (normalised)."""
    import deezer
    res = deezer.get("search", q=f"{artist} {title}", limit=10) or {}
    tk, ak = catalog.title_key(title), catalog.key(artist)
    hits = [d for d in res.get("data", []) if catalog.title_key(d.get("title", "")) == tk
            and catalog.key((d.get("artist") or {}).get("name", "")) == ak]
    return max(hits, key=lambda d: d.get("rank") or 0) if hits else None


def resolve(sightings: list[Sighting], source: str, progress=lambda n, total: None) -> tuple[dict, dict]:
    """Returns ({sighting.key(): track_id}, stats). Writes only to global tables."""
    out, stats = {}, {"direct": 0, "known_id": 0, "reccobeats": 0, "deezer_search": 0, "name_only": 0}
    unique = {}
    for s in sightings:
        unique.setdefault(s.key(), s)
    pending = []
    with db.system() as cur:
        for k, s in unique.items():
            sid = s.ids.get("spotify")
            if s.isrc:
                out[k] = catalog.resolve_track(cur, s.title, s.artists, ids=s.ids, isrc=s.isrc, duration_ms=s.duration_ms,
                                               source=source)
                _cover(cur, out[k], s.cover_url)
                stats["direct"] += 1
            elif sid and (known := catalog.track_by_identifier(cur, "spotify", sid)):
                out[k] = known[0]
                stats["known_id"] += 1
            else:
                pending.append((k, s))
    progress(len(out), len(unique))

    recco = _recco_tracks([s.ids["spotify"] for _, s in pending if s.ids.get("spotify")])
    still = []
    with db.system() as cur:
        for k, s in pending:
            t = recco.get(s.ids.get("spotify"))
            if t and t.get("isrc"):
                artists = [{"name": a["name"], "ids": {"spotify": (a.get("href") or "").rstrip("/").split("/")[-1] or None,
                                                       "reccobeats": a.get("id")}} for a in t.get("artists", [])]
                out[k] = catalog.resolve_track(cur, t.get("trackTitle") or s.title, artists or s.artists,
                                               ids={**s.ids, "reccobeats": t.get("id")}, isrc=t["isrc"],
                                               duration_ms=t.get("durationMs"), source=f"{source}+reccobeats")
                stats["reccobeats"] += 1
            else:
                still.append((k, s))
    progress(len(out), len(unique))

    for n, (k, s) in enumerate(still, 1):
        lead = s.artists[0]["name"] if isinstance(s.artists[0], dict) else s.artists[0]
        d = deezer_match(s.title, lead)
        with db.system() as cur:
            if d:
                out[k] = catalog.resolve_track(cur, d["title"], [{"name": d["artist"]["name"], "ids": {"deezer": d["artist"].get("id")}}]
                                               if not isinstance(s.artists[0], dict) or not s.artists[0].get("mbid") else s.artists,
                                               ids={**s.ids, "deezer": d["id"]}, isrc=d.get("isrc"),
                                               duration_ms=(d.get("duration") or 0) * 1000 or None,
                                               source=f"{source}+deezer_search", match="fuzzy")
                _cover(cur, out[k], (d.get("album") or {}).get("cover_xl") or (d.get("album") or {}).get("cover_big"))
                stats["deezer_search"] += 1
            else:
                out[k] = catalog.resolve_track(cur, s.title, s.artists, ids=s.ids, source=source, match="fuzzy")
                stats["name_only"] += 1
        if n % 25 == 0:
            progress(len(out), len(unique))
    progress(len(out), len(unique))
    return out, stats


def _cover(cur, track_id, url):
    if url:
        cur.execute("UPDATE tracks SET cover_url = coalesce(cover_url, %s) WHERE id = %s", (url, track_id))
