"""Artist identities and genres from MusicBrainz, stored once for everyone (artists.mbid, artist_genres).

Reuses artist_genres.py's batched lookups (20 ISRCs or 100 artists per request at MusicBrainz's 1/s), so a
new user whose artists are already known costs no MusicBrainz requests at all. Artists MusicBrainz doesn't
know are remembered for a week, ones whose genres were fetched for 30 days.
"""
from .. import catalog, db, provider_cache

UNKNOWN_MEMO_S = 7 * 86400
GENRES_FRESH_S = 30 * 86400


def ensure(artist_ids) -> dict:
    import artist_genres as AG
    artist_ids = sorted(set(artist_ids))
    if not artist_ids:
        return {"resolved": 0, "described": 0}
    with db.system() as cur:
        cur.execute("SELECT id, name, mbid::text AS mbid FROM artists WHERE id = ANY(%s)", (artist_ids,))
        arts = {r["id"]: r for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT artist_id FROM artist_genres WHERE artist_id = ANY(%s) AND source <> 'legacy_cache' "
                    "AND fetched_at > now() - %s * interval '1 second'", (artist_ids, GENRES_FRESH_S))
        described = {r["artist_id"] for r in cur.fetchall()}
        described |= {a for a in artist_ids if provider_cache.fresh("mb_genres_none", str(a), GENRES_FRESH_S)}
        need_id = [a for a in arts.values() if not a["mbid"]
                   and not provider_cache.fresh("mb_artist_unknown", str(a["id"]), UNKNOWN_MEMO_S)]
        items = []
        if need_id:
            cur.execute("""SELECT ta.track_id, ti.external_id AS isrc, array_agg(a.name ORDER BY ta2.position) AS names
                           FROM track_artists ta
                           JOIN track_identifiers ti ON ti.track_id = ta.track_id AND ti.provider = 'isrc'
                           JOIN track_artists ta2 ON ta2.track_id = ta.track_id JOIN artists a ON a.id = ta2.artist_id
                           WHERE ta.artist_id = ANY(%s) GROUP BY ta.track_id, ti.external_id""",
                        ([a["id"] for a in need_id],))
            items = [(r["names"], r["isrc"]) for r in cur.fetchall()]

    stats = {"resolved": 0, "described": 0, "unknown": 0}
    if need_id:
        by_name = {a["name"]: a for a in need_id}
        mbids = {a["name"]: a["mbid"] for a in arts.values() if a["mbid"]}
        missing = AG.resolve_isrcs(items, mbids, report=False)
        missing = sorted((set(missing) | {n for n in by_name if n not in mbids}) & set(by_name))
        for name, mbid in AG.search_names(missing).items():
            if mbid:
                mbids[name] = mbid
        with db.system() as cur:
            for name, a in by_name.items():
                mbid = mbids.get(name)
                if not mbid:
                    provider_cache.put("mb_artist_unknown", str(a["id"]), True, UNKNOWN_MEMO_S)
                    stats["unknown"] += 1
                    continue
                cur.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,))
                other = cur.fetchone()
                if other and other["id"] != a["id"]:
                    continue                              # another row already holds this identity (duplicate name row)
                cur.execute("UPDATE artists SET mbid = %s, updated_at = now() WHERE id = %s", (mbid, a["id"]))
                a["mbid"] = mbid
                stats["resolved"] += 1

    todo = {a["name"]: a["mbid"] for a in arts.values() if a["mbid"] and a["id"] not in described}
    if todo:
        genres = AG.genres_for(todo, {}, report=False)
        with db.system() as cur:
            for a in arts.values():
                if a["name"] in todo:
                    catalog.set_artist_genres(cur, a["id"], "musicbrainz", genres.get(a["name"]) or {})
                    if not genres.get(a["name"]):           # no genres: remember we asked, so we don't ask again soon
                        provider_cache.put("mb_genres_none", str(a["id"]), True, GENRES_FRESH_S)
                    stats["described"] += 1
    return stats


def by_name(cur, artist_ids) -> dict[str, dict[str, float]]:
    """{artist name: {genre: weight}} in the shape the scoring code expects (blank markers dropped)."""
    cur.execute("SELECT id, name FROM artists WHERE id = ANY(%s)", (list(artist_ids),))
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    out = {}
    for aid, g in catalog.artist_genres(cur, names).items():
        if g or names[aid] not in out:
            out[names[aid]] = g
    for aid, n in names.items():
        out.setdefault(n, {})
    return out
