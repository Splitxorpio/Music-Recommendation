"""Genres for every artist in the library, from MusicBrainz (Spotify no longer returns genres).

Each artist is resolved exactly through their tracks' ISRCs (ISRC -> recording -> credited artists
with MBIDs), so same-name artists can't be confused; exact-name search is the last resort.

Batched: MusicBrainz's search answers ~20 ISRCs, or 100 artists' tags, per request (it allows ~1
request/second, so request count is what matters). A MusicBrainz genre is a tag on its curated
genre list, so filtering batched tags by that list gives the same genres as per-artist lookups
(verified on this library: 222/222 identical).

Reads cache/tracks.json, cache/features.json, cache/isrc.json.
Writes cache/artist_mbids.json (name -> mbid) and cache/artist_genres.json (name -> {genre: count}).
Resumable.
"""
import json
import re
import time

import net
import progress as P

from config import CACHE
MB = "https://musicbrainz.org/ws/2"
ISRC_BATCH, ARTIST_BATCH = 20, 100


def load(name, default):
    p = CACHE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    """Atomic write: stages run side by side, so a reader must never see a half-written file."""
    tmp = CACHE / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(CACHE / name)


def key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def mb_get(path, params):
    r = net.get("musicbrainz", f"{MB}/{path}", params={**params, "fmt": "json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _credit_match(names, credits, mbids):
    for credit in credits:
        a = credit["artist"]
        for name in names:
            if name not in mbids and key(name) in (key(a["name"]), key(credit.get("name", ""))):
                mbids[name] = a["id"]


def resolve_isrcs(items, mbids, report=True):
    """items: [(artist names, isrc)]. Resolves names -> MBIDs in batches; returns names still unknown."""
    todo = [(names, code) for names, code in items if code and any(n not in mbids for n in names)]
    codes = sorted({c for _, c in todo})
    for k in range(0, len(codes), ISRC_BATCH):
        chunk = codes[k:k + ISRC_BATCH]
        data = mb_get("recording", {"query": "isrc:(" + " OR ".join(chunk) + ")", "limit": 100}) or {}
        by_code = {}
        for rec in data.get("recordings", []):
            for c in rec.get("isrcs", []):
                by_code.setdefault(c, []).append(rec.get("artist-credit", []))
        for names, code in todo:
            for credits in by_code.get(code, [])[:3]:
                _credit_match(names, credits, mbids)
        if report:
            P.tick(len(mbids), now=f"asking who made {min(k + ISRC_BATCH, len(codes))} of {len(codes)} recordings")
    return {n for names, _ in todo for n in names if n not in mbids}


def resolve_mbids(tracks, isrcs):
    """name -> mbid for every artist in `tracks`."""
    mbids = load("artist_mbids.json", {})
    tried = set(load("isrc_tried.json", []))
    wanted = {a for t in tracks.values() for a in t["artists"]}
    print(f"[resolve] {len(wanted - set(mbids))} of {len(wanted)} artists unresolved")
    P.stage("genres", total=len(wanted), label="artists identified")
    missing = resolve_isrcs([(t["artists"], isrcs.get(i)) for i, t in tracks.items()], mbids)
    save("artist_mbids.json", mbids)

    # Search index misses: look those few ISRCs up one by one (one per still-missing artist).
    for name in sorted(missing):
        for i, t in tracks.items():
            code = isrcs.get(i)
            if name in mbids or name not in t["artists"] or not code or code in tried:
                continue
            tried.add(code)
            for rec in (mb_get(f"isrc/{code}", {"inc": "artist-credits"}) or {}).get("recordings", [])[:3]:
                _credit_match(t["artists"], rec.get("artist-credit", []), mbids)
            break
    save("isrc_tried.json", sorted(tried))

    # Last resort: exact-name search, only accepting a perfect, unambiguous match. Names MusicBrainz
    # doesn't know are remembered for a week, so re-digs don't ask again.
    unknown = load("artist_unknown.json", {})
    todo = []
    for name in sorted(wanted - set(mbids)):
        if time.time() - unknown.get(name, 0) < 7 * 86400:
            net.cache_hit("mb_unknown_artists")
        else:
            todo.append(name)
    for name, mbid in search_names(todo).items():
        if mbid:
            mbids[name] = mbid
        else:
            unknown[name] = time.time()
    save("artist_mbids.json", mbids)
    save("artist_unknown.json", unknown)
    print(f"[resolve] {len(mbids)}/{len(wanted)} artists resolved")
    return mbids


NAME_BATCH = 20


def _lucene(name):
    return '"' + re.sub(r'([\\"])', r"\\\1", name) + '"'


def search_names(names):
    """Exact-name search, NAME_BATCH names per request. A name resolves only when exactly one returned
    artist matches it exactly (normalised), so ambiguous names stay unresolved. {name: mbid or None}."""
    out = {}
    for k in range(0, len(names), NAME_BATCH):
        chunk = names[k:k + NAME_BATCH]
        data = mb_get("artist", {"query": "artist:(" + " OR ".join(_lucene(n) for n in chunk) + ")", "limit": 100}) or {}
        found = data.get("artists", [])
        for n in chunk:
            hits = {a["id"] for a in found if key(a["name"]) == key(n)}
            out[n] = hits.pop() if len(hits) == 1 else None
    return out


_vocab = None
VOCAB_DAYS = 30


def genre_vocabulary():
    """MusicBrainz's curated genre list (~2,200 names). Kept for VOCAB_DAYS; it changes slowly."""
    global _vocab
    if _vocab is None:
        rec = load("mb_genre_vocabulary.json", None)
        if rec and time.time() - rec["t"] < VOCAB_DAYS * 86400:
            net.cache_hit("mb_genre_vocabulary")
            _vocab = set(rec["v"])
        else:
            r = net.get("musicbrainz", f"{MB}/genre/all", params={"fmt": "txt"})
            r.raise_for_status()
            _vocab = {g.strip() for g in r.text.splitlines() if g.strip()}
            save("mb_genre_vocabulary.json", {"t": time.time(), "v": sorted(_vocab)})
    return _vocab


def genres_for(name_to_mbid, genres, report=True):
    """Fill genres[name] for every name not already known, 100 artists per request."""
    todo = [n for n in name_to_mbid if n not in genres]
    if not todo:
        net.cache_hit("genres", True)
        return genres
    vocab = genre_vocabulary()
    for k in range(0, len(todo), ARTIST_BATCH):
        chunk = todo[k:k + ARTIST_BATCH]
        data = mb_get("artist", {"query": " OR ".join(f"arid:{name_to_mbid[n]}" for n in chunk), "limit": 100}) or {}
        tags = {a["id"]: a.get("tags", []) for a in data.get("artists", [])}
        for n in chunk:
            t = tags.get(name_to_mbid[n], [])
            g = {x["name"]: x["count"] for x in t if x["name"] in vocab and x.get("count", 0) > 0}
            genres[n] = g or {x["name"]: x["count"] for x in t if x.get("count", 0) > 0}   # tags as a fallback
            if genres[n]:
                top = [x for x, _ in sorted(genres[n].items(), key=lambda x: -x[1])[:3]]
                P.fact(f"{n}: {' / '.join(top)}", key="clerk")
        if report:
            P.tick(min(k + ARTIST_BATCH, len(todo)), len(todo), label="artists described")
    return genres


def fetch_genres(mbids):
    genres = load("artist_genres.json", {})
    print(f"[genres] {sum(1 for n in mbids if n not in genres)} artists to fetch")
    genres_for(mbids, genres)
    save("artist_genres.json", genres)
    return genres


def main():
    tracks = load("tracks.json", {})                   # (not enriched.json, so this can run alongside the clip stage)
    isrcs = {i: f["isrc"] for i, f in load("features.json", {}).items() if f.get("isrc")}
    isrcs.update({i: c for i, c in load("isrc.json", {}).items() if c})
    mbids = resolve_mbids(tracks, isrcs)
    genres = fetch_genres(mbids)
    with_g = sum(1 for g in genres.values() if g)
    print(f"\n{with_g}/{len({a for t in tracks.values() for a in t['artists']})} artists have genres")
    P.fact(f"The clerks knew {with_g} of your artists")
    P.finish("genres")


if __name__ == "__main__":
    main()
