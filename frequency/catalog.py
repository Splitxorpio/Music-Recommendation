"""The global catalog: canonical tracks and artists, their external identifiers, and measurements.

Identity rules
  * A provider id (Spotify, Deezer, ReccoBeats, iTunes, ...) names exactly one canonical track.
  * An ISRC is strong evidence but not unique: labels reuse ISRCs across releases and occasionally
    mis-assign them. An ISRC only merges two sightings when the normalised title and lead artist agree
    too; otherwise a separate track is created and the clash is written to identifier_conflicts.
  * Resolution of one recording is serialised with a transaction-scoped advisory lock, so two workers
    seeing the same new song at the same moment create one track, not two.
"""
import re
import unicodedata

from .db import jsonb

TRACK_PROVIDERS = ("spotify", "deezer", "reccobeats", "itunes", "musicbrainz", "lastfm")


def key(s: str | None) -> str:
    """Matching key: accents folded, lowercase alphanumerics only ("Beyoncé (Live)" -> "beyoncelive")."""
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower()) or (s or "").strip().lower()


def title_key(title: str) -> str:
    """Title key that ignores feature credits and remaster/version suffixes in brackets or after ' - '."""
    t = re.sub(r"\s*[\(\[][^\)\]]*(feat|with|remaster|version|edit|mono|stereo)[^\)\]]*[\)\]]", "", title or "", flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|version|edit|mix\b|live).*$", "", t, flags=re.I)
    return key(t)


def _lock(cur, k: str):
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (k,))


# ─── artists ───

def upsert_artist(cur, name: str, mbid: str | None = None, ids: dict | None = None) -> int:
    ids = {p: v for p, v in (ids or {}).items() if v}
    _lock(cur, "artist:" + (mbid or next((f"{p}:{v}" for p, v in sorted(ids.items())), "name:" + key(name))))
    artist_id = None
    for p, v in sorted(ids.items()):
        cur.execute("SELECT artist_id FROM artist_identifiers WHERE provider = %s AND external_id = %s", (p, str(v)))
        r = cur.fetchone()
        if r:
            artist_id = r["artist_id"]
            break
    if artist_id is None and mbid:
        cur.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,))
        r = cur.fetchone()
        artist_id = r and r["id"]
    if artist_id is None and not ids and not mbid:
        # Name only (e.g. a scrobble). Reuse an existing artist only if the name is unambiguous.
        cur.execute("SELECT id FROM artists WHERE name_key = %s LIMIT 2", (key(name),))
        rows = cur.fetchall()
        if len(rows) == 1:
            artist_id = rows[0]["id"]
    if artist_id is None:
        cur.execute("INSERT INTO artists (name, name_key, mbid) VALUES (%s, %s, %s) RETURNING id", (name, key(name), mbid))
        artist_id = cur.fetchone()["id"]
    elif mbid:
        cur.execute("UPDATE artists SET mbid = %s, updated_at = now() WHERE id = %s AND mbid IS NULL", (mbid, artist_id))
    for p, v in ids.items():
        cur.execute("INSERT INTO artist_identifiers (provider, external_id, artist_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (p, str(v), artist_id))
    return artist_id


def set_artist_genres(cur, artist_id: int, source: str, genres: dict[str, float]):
    """Replace one source's genres for an artist."""
    cur.execute("DELETE FROM artist_genres WHERE artist_id = %s AND source = %s", (artist_id, source))
    for g, w in genres.items():
        if w and w > 0:
            cur.execute("INSERT INTO artist_genres (artist_id, genre, weight, source) VALUES (%s, %s, %s, %s)",
                        (artist_id, g, float(w), source))


def artist_genres(cur, artist_ids) -> dict[int, dict[str, float]]:
    cur.execute("SELECT artist_id, genre, weight, source FROM artist_genres WHERE artist_id = ANY(%s)", (list(artist_ids),))
    out: dict[int, dict] = {}
    best: dict[int, str] = {}
    rank = {"musicbrainz": 0, "lastfm": 1, "musicbrainz_tag": 2, "deezer": 3, "legacy_cache": 4}
    rows = cur.fetchall()
    for r in rows:                      # one source per artist: the most trustworthy one it has
        if r["artist_id"] not in best or rank[r["source"]] < rank[best[r["artist_id"]]]:
            best[r["artist_id"]] = r["source"]
    for r in rows:
        if best[r["artist_id"]] == r["source"]:
            out.setdefault(r["artist_id"], {})[r["genre"]] = r["weight"]
    return out


# ─── tracks ───

def _conflict(cur, provider, external_id, track_ids, detail):
    cur.execute("""INSERT INTO identifier_conflicts (provider, external_id, track_ids, detail)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (provider, external_id) WHERE resolved_at IS NULL
                   DO UPDATE SET track_ids = (SELECT array_agg(DISTINCT x ORDER BY x)
                                              FROM unnest(identifier_conflicts.track_ids || EXCLUDED.track_ids) x),
                                 detail = identifier_conflicts.detail || EXCLUDED.detail""",
                (provider, external_id, sorted(set(track_ids)), jsonb(detail)))


def _lead_artist_key(cur, track_id):
    cur.execute("SELECT a.name_key FROM track_artists ta JOIN artists a ON a.id = ta.artist_id "
                "WHERE ta.track_id = %s ORDER BY ta.position LIMIT 1", (track_id,))
    r = cur.fetchone()
    return r and r["name_key"]


def resolve_track(cur, title: str, artists: list, *, ids: dict | None = None, isrc: str | None = None,
                  duration_ms: int | None = None, source: str, match: str = "exact") -> int:
    """Find or create the canonical track for one sighting of a recording and attach its identifiers.

    artists: list of names, or of dicts {name, mbid?, ids?: {provider: id}}. Returns the track id."""
    ids = {p: str(v) for p, v in (ids or {}).items() if v}
    unknown = set(ids) - set(TRACK_PROVIDERS)
    if unknown:
        raise ValueError(f"unknown track providers: {unknown}")
    isrc = (isrc or "").strip().upper() or None
    artists = [a if isinstance(a, dict) else {"name": a} for a in artists if a]
    tkey, akey = title_key(title), key(artists[0]["name"]) if artists else ""
    _lock(cur, "track:" + (f"isrc:{isrc}" if isrc else next((f"{p}:{v}" for p, v in sorted(ids.items())),
                                                             f"name:{akey}:{tkey}")))

    found = {}
    for p, v in ids.items():
        cur.execute("SELECT track_id FROM track_identifiers WHERE provider = %s AND external_id = %s", (p, v))
        r = cur.fetchone()
        if r:
            found[p] = r["track_id"]
    track_id = min(found.values()) if found else None
    if len(set(found.values())) > 1:
        for p, t in found.items():
            if t != track_id:
                _conflict(cur, p, ids[p], [t, track_id], {"why": "provider ids of one sighting point at different tracks",
                                                         "source": source})

    isrc_clash = None
    if isrc:
        cur.execute("SELECT track_id FROM track_identifiers WHERE provider = 'isrc' AND external_id = %s ORDER BY track_id", (isrc,))
        same_isrc = [r["track_id"] for r in cur.fetchall()]
        if track_id is None and same_isrc:
            agree = []
            for t in same_isrc:
                cur.execute("SELECT title_key FROM tracks WHERE id = %s", (t,))
                if cur.fetchone()["title_key"] == tkey and (not akey or _lead_artist_key(cur, t) == akey):
                    agree.append(t)
            if agree:
                track_id = agree[0]
            if len(agree) != len(same_isrc):
                isrc_clash = same_isrc

    if track_id is None and not ids and not isrc and artists:
        # Name-only sighting (scrobble, history upload): reuse a track with the same title and lead artist.
        cur.execute("""SELECT t.id FROM tracks t JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
                       JOIN artists a ON a.id = ta.artist_id
                       WHERE t.title_key = %s AND a.name_key = %s AND t.merged_into IS NULL ORDER BY t.id LIMIT 1""",
                    (tkey, akey))
        r = cur.fetchone()
        track_id = r and r["id"]

    created = track_id is None
    if created:
        cur.execute("INSERT INTO tracks (title, title_key, duration_ms) VALUES (%s, %s, %s) RETURNING id",
                    (title, tkey, duration_ms))
        track_id = cur.fetchone()["id"]
        for pos, a in enumerate(artists):
            aid = upsert_artist(cur, a["name"], a.get("mbid"), a.get("ids"))
            cur.execute("INSERT INTO track_artists (track_id, position, artist_id) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING", (track_id, pos, aid))
    elif duration_ms:
        cur.execute("UPDATE tracks SET duration_ms = coalesce(duration_ms, %s) WHERE id = %s", (duration_ms, track_id))

    for p, v in ids.items():
        if p in found and found[p] != track_id:
            continue                                   # recorded as a conflict above; don't move it
        cur.execute("INSERT INTO track_identifiers (provider, external_id, track_id, match, source) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (p, v, track_id, match, source))
    if isrc:
        cur.execute("INSERT INTO track_identifiers (provider, external_id, track_id, match, source) "
                    "VALUES ('isrc', %s, %s, %s, %s) ON CONFLICT DO NOTHING", (isrc, track_id, match, source))
    if isrc_clash:
        _conflict(cur, "isrc", isrc, isrc_clash + [track_id],
                  {"why": "same ISRC, different title or lead artist", "title": title,
                   "artist": artists[0]["name"] if artists else None, "source": source})
    return track_id


def track_by_identifier(cur, provider: str, external_id: str) -> list[int]:
    cur.execute("SELECT track_id FROM track_identifiers WHERE provider = %s AND external_id = %s ORDER BY track_id",
                (provider, str(external_id)))
    return [r["track_id"] for r in cur.fetchall()]


def identifiers(cur, track_ids) -> dict[int, dict[str, str]]:
    cur.execute("SELECT track_id, provider, external_id FROM track_identifiers WHERE track_id = ANY(%s) "
                "ORDER BY created_at", (list(track_ids),))
    out: dict[int, dict] = {}
    for r in cur.fetchall():
        out.setdefault(r["track_id"], {}).setdefault(r["provider"], r["external_id"])
    return out


def track_info(cur, track_ids) -> dict[int, dict]:
    cur.execute("""SELECT t.id, t.title, array_agg(a.name ORDER BY ta.position) AS artists,
                          array_agg(a.id ORDER BY ta.position) AS artist_ids
                   FROM tracks t LEFT JOIN track_artists ta ON ta.track_id = t.id LEFT JOIN artists a ON a.id = ta.artist_id
                   WHERE t.id = ANY(%s) GROUP BY t.id""", (list(track_ids),))
    return {r["id"]: {"title": r["title"], "artists": [a for a in r["artists"] if a],
                      "artist_ids": [a for a in r["artist_ids"] if a]} for r in cur.fetchall()}


# ─── measurements ───

def record_measurement(cur, track_id: int, kind: str, provider: str, values: dict, *, confidence: str,
                       clip_source: str | None = None, calibrated: dict | None = None,
                       calibration_version_id: int | None = None, flags=(), provenance: dict | None = None) -> int:
    """Store a measurement; the previous current one of the same kind is kept as history (superseded)."""
    cur.execute("UPDATE track_measurements SET superseded_at = now() "
                "WHERE track_id = %s AND kind = %s AND superseded_at IS NULL", (track_id, kind))
    cur.execute("""INSERT INTO track_measurements (track_id, kind, provider, clip_source, values, calibrated,
                       calibration_version_id, confidence, flags, provenance)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (track_id, kind, provider, clip_source, jsonb(values), jsonb(calibrated) if calibrated is not None else None,
                 calibration_version_id, confidence, list(flags), jsonb(provenance or {})))
    return cur.fetchone()["id"]


def current_measurements(cur, track_ids) -> dict[int, dict[str, dict]]:
    """{track_id: {kind: row}} for the current (not superseded) measurements."""
    cur.execute("SELECT * FROM track_measurements WHERE track_id = ANY(%s) AND superseded_at IS NULL", (list(track_ids),))
    out: dict[int, dict] = {}
    for r in cur.fetchall():
        out.setdefault(r["track_id"], {})[r["kind"]] = r
    return out


def features_for(m: dict[str, dict]) -> tuple[dict | None, str]:
    """The trait values to use for a track and where they came from: official numbers if any, else the
    calibrated clip estimate, else the raw clip estimate (flagged unvalidated)."""
    if "official" in m:
        return m["official"]["values"], "official"
    est = m.get("clip_estimate")
    if est:
        if est["calibrated"] is not None:
            return est["calibrated"], est["confidence"]
        return est["values"], "unvalidated"
    return None, "no_data"
