"""Gather and measure candidate songs to recommend.

Stage 1 (gather): starting from the user's top artists ("seeds"), collect
  - Deezer related artists (main source, scene-aware) and, optionally later, artists two steps out
  - Deezer artist radio
  - MusicBrainz artist relationships (collaborators, members)
  - a few ReccoBeats sound-alike suggestions per sub-taste (minor source)
  - unheard songs by the seeds themselves ("known" mode)
Every candidate keeps its links (which of the user's artists point to it, how strongly), its
mode ("new"/"known") and hop (1 = close, 2 = two steps out), so the dashboard can filter later.
Songs already in the library are dropped (by ISRC and by title + artist).

Stage 2 (measure): official ReccoBeats values by ISRC for everyone; then for the best
pre-scored candidates in each mode: genres (MusicBrainz, batched), a Deezer clip for bass, a clip
estimate when official values are missing, and a Spotify link.

All outside requests go through net.py, fetched concurrently; results are then processed in the
same fixed order as before, so the candidate set doesn't depend on which request finished first.
Deezer responses are cached in cache/deezer_cache.json (and identical in-flight requests are
shared), so re-runs are fast. Clips go to cache/previews/.
"""
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

import deezer
import net
import progress as P
from artist_genres import genres_for, key, mb_get, resolve_isrcs
from enrich_audio import PREVIEWS, analyse_clip, bass_from_clip, fetch_clip, norm
from recommend import score_all, total
import spotify_links
from taste_profile import SOURCE_WEIGHT

from config import CACHE
RECCO = "https://api.reccobeats.com/v1"

N_SEEDS = 25            # top artists to branch out from
RELATED_PER_SEED = 15
RADIO_PER_SEED = 25
TOP_PER_NEW_ARTIST = 3
KNOWN_TOP = 15          # songs checked per seed for "unheard songs from artists you know"
FAR_FROM = 30           # strongest close artists to branch out from again
FAR_RELATED = 10
FAR_DECAY = 0.4         # two-steps-out links count this much of the path that led there
RECCO_PER_CLUSTER = 25
STAGE2_PER_BUCKET = 120  # candidates fully measured per mode (new-close, new-far, known)

LINK_WEIGHT = {"radio": 0.6, "musicbrainz": 0.8, "reccobeats": 0.3}


def load(name, default):
    p = CACHE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    """Atomic write: stages run side by side, so a reader must never see a half-written file."""
    tmp = CACHE / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(CACHE / name)


def dz(path, fresh=False, **params):
    """Deezer answer from the shared, expiring store (see deezer.py)."""
    return deezer.get(path, fresh=fresh, **params)


def save_dz():
    deezer.save()


def fan(fn, items, workers=16):
    """Fetch concurrently, hand back results in the original order (exceptions become None)."""
    items = list(items)
    res = net.pmap(lambda k: fn(items[k]), range(len(items)), workers=workers)
    return [None if isinstance(res[k], Exception) else res[k] for k in range(len(items))]


# ---------- library ----------

def library(enriched, features, isrc_map):
    isrcs = {f["isrc"] for f in features.values() if f.get("isrc")} | {v for v in isrc_map.values() if v}
    names = {(norm(t["name"]), key(t["artists"][0])) for t in enriched.values()}
    known = {key(a) for t in enriched.values() for a in t["artists"]}
    weights = defaultdict(float)
    by_artist = defaultdict(list)
    for i, t in enriched.items():
        weights[t["artists"][0]] += sum(SOURCE_WEIGHT.get(s, 0) for s in t["sources"])
        code = (features.get(i) or {}).get("isrc") or isrc_map.get(i)
        if code:
            by_artist[t["artists"][0]].append(code)
    tot = sum(weights.values())
    share = {a: v / tot for a, v in weights.items()}
    return isrcs, names, known, share, by_artist


def deezer_artist(name, isrcs):
    """Exact: find the artist on one of their own tracks by ISRC; else exact-name search."""
    for code in isrcs[:3]:
        t = dz(f"track/isrc:{code}")
        for c in ([t.get("artist")] + t.get("contributors", [])) if t else []:
            if c and key(c["name"]) == key(name):
                return {"id": c["id"], "name": c["name"]}
    hits = [a for a in (dz("search/artist", q=name) or {}).get("data", []) if key(a["name"]) == key(name)]
    return max(hits, key=lambda a: a.get("nb_fan", 0)) if hits else None


# ---------- stage 1: gather ----------

def gather(enriched, features, isrc_map, profile):
    lib_isrcs, lib_names, known, share, by_artist = library(enriched, features, isrc_map)
    seeds = sorted(share, key=lambda a: -share[a])[:N_SEEDS]
    artist_links = defaultdict(list)   # deezer artist id -> [{via, type, strength}]
    artist_info = {}                    # deezer artist id -> name
    cand = {}                           # candidate id -> record
    track_links = defaultdict(list)     # candidate id -> extra track-level links

    def add_track(t, mode, hop, link=None):
        if not t or not t.get("id"):
            return
        a = t.get("artist") or {}
        if (norm(t.get("title", "")), key(a.get("name", ""))) in lib_names:
            return
        cid = f"dz{t['id']}"
        rec = cand.setdefault(cid, {"id": cid, "deezer_id": t["id"], "title": t.get("title", ""),
                                    "artist": a.get("name", ""), "artist_dz": a.get("id"),
                                    "mode": mode, "hop": hop, "deezer_rank": t.get("rank")})
        rec["hop"] = min(rec["hop"], hop)
        if mode == "new":
            P.cover((t.get("album") or {}).get("cover_big"), t.get("title", ""), a.get("name", ""), tag="new find")
        if link and link not in track_links[cid]:
            track_links[cid].append(link)

    print(f"[seeds] {len(seeds)} artists: {', '.join(seeds[:10])}...")
    P.stage("crates", label="artists followed")
    P.fact(f"Starting from your top {len(seeds)}: {', '.join(seeds[:4])}…")

    # MusicBrainz relationships run in the background (their own 1/s limit) while Deezer works.
    mbids = load("artist_mbids.json", {})
    rel_cache = load("mb_relations.json", {})       # artist relationships barely change: keep them

    def relations():
        out = {}
        for s in seeds:
            if s not in mbids:
                continue
            if mbids[s] in rel_cache:
                net.cache_hit("mb_relations")
            else:
                net.cache_hit("mb_relations", False)
                rel_cache[mbids[s]] = mb_get(f"artist/{mbids[s]}", {"inc": "artist-rels"}) or {}
            out[s] = rel_cache[mbids[s]]
        save("mb_relations.json", rel_cache)
        return out
    mb_pool = ThreadPoolExecutor(max_workers=1)
    mb_future = mb_pool.submit(relations)

    found = fan(lambda s: deezer_artist(s, by_artist.get(s, [])), seeds)
    seed_dz = {}
    for s, d in zip(seeds, found):
        if d:
            seed_dz[s] = d
            artist_info[d["id"]] = d["name"]
    print(f"  found {len(seed_dz)}/{len(seeds)} on Deezer")

    # Close: related artists + radio + the seeds' own unheard songs (fetched together, applied in order)
    pages = fan(lambda s: (dz(f"artist/{seed_dz[s]['id']}/related", limit=RELATED_PER_SEED),
                           dz(f"artist/{seed_dz[s]['id']}/radio", limit=RADIO_PER_SEED),
                           dz(f"artist/{seed_dz[s]['id']}/top", limit=KNOWN_TOP)), list(seed_dz))
    for n_seed, (s, got) in enumerate(zip(seed_dz, pages), 1):
        P.tick(n_seed, len(seed_dz), now=f"flipping through the {s} section")
        related, radio, top = got or (None, None, None)
        rel = (related or {}).get("data", [])
        if n_seed <= 3 and rel:
            P.fact(f"Next to {s} in the racks: {', '.join(a['name'] for a in rel[:3])}", key=f"near-{n_seed}")
        for r, a in enumerate(rel):
            artist_info[a["id"]] = a["name"]
            artist_links[a["id"]].append({"via": s, "type": "related",
                                          "strength": share[s] * (1 - r / (2 * RELATED_PER_SEED))})
        for t in (radio or {}).get("data", []):
            a = t.get("artist") or {}
            if key(a.get("name", "")) == key(s):
                continue                               # the seed's own songs come from top tracks
            artist_info[a.get("id")] = a.get("name")
            link = {"via": s, "type": "radio", "strength": share[s] * LINK_WEIGHT["radio"]}
            add_track(t, "known" if key(a.get("name", "")) in known else "new", 1, link)
        for t in (top or {}).get("data", []):
            add_track(t, "known", 1, {"via": s, "type": "own", "strength": share[s]})

    # Close: MusicBrainz relationships (collaborations, group members)
    rels = mb_future.result()
    mb_pool.shutdown()
    pairs = [(s, (rel.get("artist") or {}).get("name")) for s in seeds for rel in rels.get(s, {}).get("relations", [])]
    pairs = [(s, o) for s, o in pairs if o and key(o) != key(s)]
    searched = fan(lambda o: dz("search/artist", q=o), sorted({o for _, o in pairs}))
    by_name = dict(zip(sorted({o for _, o in pairs}), searched))
    for s, other in pairs:
        hits = [a for a in (by_name.get(other) or {}).get("data", []) if key(a["name"]) == key(other)]
        if hits:
            a = max(hits, key=lambda a: a.get("nb_fan", 0))
            artist_info[a["id"]] = a["name"]
            artist_links[a["id"]].append({"via": s, "type": "musicbrainz",
                                          "strength": share[s] * LINK_WEIGHT["musicbrainz"]})
    close_artists = set(artist_links)

    # Two steps out: related artists of the strongest close artists
    strongest = sorted((a for a in close_artists if key(artist_info.get(a, "")) not in known),
                       key=lambda a: -sum(l["strength"] for l in artist_links[a]))[:FAR_FROM]
    far_pages = fan(lambda y: dz(f"artist/{y}/related", limit=FAR_RELATED), strongest)
    for y, page in zip(strongest, far_pages):
        sy = sum(l["strength"] for l in artist_links[y])
        via = max(artist_links[y], key=lambda l: l["strength"])["via"]
        for r, a in enumerate((page or {}).get("data", [])):
            if a["id"] in close_artists:
                continue
            artist_info[a["id"]] = a["name"]
            artist_links[a["id"]].append({"via": via, "type": "related", "through": artist_info[y],
                                          "strength": sy * FAR_DECAY * (1 - r / (2 * FAR_RELATED))})

    # Songs for every linked artist that isn't already in the library
    print(f"[artists] {len(close_artists)} close, {len(artist_links) - len(close_artists)} two steps out")
    seed_keys = {key(s) for s in seeds}
    targets = [a_id for a_id in artist_links
               if key(artist_info.get(a_id, "")) not in known and key(artist_info.get(a_id, "")) not in seed_keys]
    tops = fan(lambda a_id: dz(f"artist/{a_id}/top", limit=TOP_PER_NEW_ARTIST), targets)
    for a_id, page in zip(targets, tops):
        hop = 1 if a_id in close_artists else 2
        for t in (page or {}).get("data", []):
            add_track(t, "new", hop)

    # Minor source: ReccoBeats sound-alikes, seeded per sub-taste with its most-played official songs
    # (needs the fingerprint, so a prefetch run without a profile skips it)
    recco_cache = load("recco_suggestions.json", {})   # random per call, like radio: kept for a day

    def recco(cl):
        ids = sorted((i for i, v in profile["track_clusters"].items()
                      if v["cluster"] == cl["id"] and enriched.get(i, {}).get("feature_source") == "official"),
                     key=lambda i: -profile["track_clusters"][i]["weight"])[:5]
        k = ",".join(ids)
        rec = recco_cache.get(k)
        if rec and time.time() - rec["t"] < 86400:
            net.cache_hit("recco_suggestions")
            return rec["v"]
        net.cache_hit("recco_suggestions", False)
        r = net.get("reccobeats", f"{RECCO}/track/recommendation", params={"size": RECCO_PER_CLUSTER, "seeds": k})
        v = r.json() if r.ok else {}
        if r.ok:
            recco_cache[k] = {"t": time.time(), "v": v}
        return v
    suggestions = fan(recco, profile["clusters"], workers=4) if profile else []
    if profile:
        save("recco_suggestions.json", recco_cache)
    picks = [(cl, t) for cl, r in zip(profile["clusters"] if profile else [], suggestions)
             for t in (r or {}).get("content", []) if t.get("isrc") and t["isrc"] not in lib_isrcs]
    looked = fan(lambda ct: dz(f"track/isrc:{ct[1]['isrc']}"), picks)
    for (cl, _), d in zip(picks, looked):
        if d:
            a = (d.get("artist") or {}).get("name", "")
            add_track(d, "known" if key(a) in known else "new", 1,
                      {"via": f"sub-taste {cl['id'] + 1}", "type": "reccobeats",
                       "strength": LINK_WEIGHT["reccobeats"] * min(share.values())})

    # Attach links, fetch details (ISRC, all credited artists), drop library songs and duplicates
    print(f"[details] {len(cand)} candidate songs")
    P.fact(f"Pulled {len(cand)} songs from {len(artist_links)} artists to audition", key="pulled")
    P.tick(0, len(cand), label="songs checked")
    order = list(cand)
    details = net.pmap(lambda cid: dz(f"track/{cand[cid]['deezer_id']}"), order, workers=16,
                       on_result=lambda cid, _, n: n % 25 == 0 and P.tick(n, now=f"checking {cand[cid]['title']} — {cand[cid]['artist']} isn't already yours"))
    out, seen = {}, {}
    for cid in order:
        c, d = cand[cid], details[cid]
        if isinstance(d, Exception) or not d or not d.get("isrc") or d["isrc"] in lib_isrcs:
            continue
        c.update({"isrc": d["isrc"], "release_date": d.get("release_date"),
                  "album": (d.get("album") or {}).get("title"), "duration": d.get("duration"),
                  "has_preview": bool(d.get("preview")),
                  "artists": [x["name"] for x in d.get("contributors", [])] or [c["artist"]]})
        c["links"] = artist_links.get(c["artist_dz"], []) + track_links.get(cid, [])
        dup = (norm(c["title"]), key(c["artist"]))
        if dup in seen and (out[seen[dup]].get("deezer_rank") or 0) >= (c.get("deezer_rank") or 0):
            out[seen[dup]]["links"] += [l for l in c["links"] if l not in out[seen[dup]]["links"]]
            continue
        if dup in seen:
            c["links"] += [l for l in out[seen[dup]]["links"] if l not in c["links"]]
            del out[seen[dup]]
        seen[dup] = cid
        out[cid] = c
    save_dz()
    return out


# ---------- stage 2: measure ----------

OFFICIAL_FOUND_DAYS, OFFICIAL_MISSING_DAYS = 30, 1


def official_features(cands):
    """Official ReccoBeats values by ISRC, 40 per request. Answers are remembered: found ones for 30 days,
    'not found' for 1 day (ReccoBeats keeps adding songs, so an estimate should upgrade when it can)."""
    store = load("official_store.json", {})
    now = time.time()
    def fresh(code):
        rec = store.get(code)
        return rec and now - rec["t"] < (OFFICIAL_FOUND_DAYS if rec["v"] else OFFICIAL_MISSING_DAYS) * 86400
    pending = [c for c in cands.values() if "features" not in c]
    codes = sorted({c["isrc"] for c in pending if not fresh(c["isrc"])})
    for c in pending:
        net.cache_hit("official_store", fresh(c["isrc"]))
    chunks = [tuple(codes[i:i + 40]) for i in range(0, len(codes), 40)]

    def batch(chunk):
        r = net.get("reccobeats", f"{RECCO}/audio-features", params={"ids": ",".join(chunk)})
        return r.json().get("content", []) if r.ok else None
    for chunk, res in zip(chunks, fan(batch, chunks, workers=4)):
        if res is None:
            continue                                   # failed batch: ask again next time
        got = {f["isrc"]: f for f in res if f.get("isrc")}
        for code in chunk:
            store[code] = {"t": now, "v": got.get(code)}
    save("official_store.json", store)
    for c in pending:
        f = (store.get(c["isrc"]) or {}).get("v")
        c["features"], c["feature_source"] = (f, "official") if f else (None, "no_data")
    print(f"[official] {sum(c['feature_source'] == 'official' for c in cands.values())}/{len(cands)} have official values "
          f"({len(codes)} looked up, {len(pending) - len(codes)} remembered)")


def candidate_genres(chosen, mbids, genres):
    """Genres for the chosen candidates' artists, batched (see artist_genres)."""
    need = [c for c in chosen if c["artist"] not in genres]
    if not need:
        return
    missing = resolve_isrcs([([c["artist"]], c["isrc"]) for c in need], mbids)
    known = {c["artist"]: mbids[c["artist"]] for c in need if c["artist"] in mbids}
    genres_for(known, genres)
    for name in missing:
        genres.setdefault(name, {})


def preview_url(c):
    """The preview link: the stored one if it hasn't expired yet, otherwise a fresh one."""
    return (deezer.track_with_preview(f"track/{c['deezer_id']}") or {}).get("preview")


_measured = None          # song id -> {"bass", "est"}: survives a re-dig, so nothing is measured twice
_mlock = threading.Lock()


def measure_clip(c):
    """Returns (bass, estimated features) for a candidate; downloads the clip only if it isn't on disk,
    and never re-measures a song measured on an earlier run."""
    with _mlock:
        prior = (_measured or {}).get(c["id"])
    if prior and (prior.get("est") or c["feature_source"] != "no_data"):
        net.cache_hit("song_measurements")
        return prior.get("bass"), prior.get("est")
    net.cache_hit("song_measurements", False)
    path = PREVIEWS / f"{c['id']}.mp3"
    if not (path.exists() and path.stat().st_size > 0):
        url = preview_url(c)
        if not url:
            return None, None
        fetch_clip(path, url, "deezer_cdn")
    else:
        net.cache_hit("clips")
    bass = c.get("bass") or bass_from_clip(path)
    est = analyse_clip(path) if c["feature_source"] == "no_data" else None
    if est:
        net.analysed("new_songs", c["id"])
    with _mlock:
        _measured[c["id"]] = {"bass": bass, "est": est}
    return bass, est


def stage2(cands, profile):
    official_features(cands)
    save("candidates.json", cands)

    # Pre-score with what we know so far, then fully measure the best of each mode.
    scored = {c["id"]: c for c in score_all(cands, profile)}
    def pre(c):
        p = dict(scored[c["id"]]["parts"])
        if p["sound"] is None:
            p["sound"] = 50                           # unknown yet: don't rule it out
        return total(p, {"sound": 0.5, "connection": 0.5})
    buckets = defaultdict(list)
    for c in cands.values():
        buckets["known" if c["mode"] == "known" else f"new-hop{c['hop']}"].append(c)
    chosen = []
    for b, items in buckets.items():
        items.sort(key=lambda c: -pre(c))
        chosen += items[:STAGE2_PER_BUCKET]
        print(f"  {b}: {len(items)} candidates, measuring top {min(len(items), STAGE2_PER_BUCKET)}")

    global _measured
    _measured = load("song_measurements.json", {})
    mbids, genres = load("artist_mbids.json", {}), load("artist_genres.json", {})
    print(f"[measure] {len(chosen)} candidates")
    P.tick(0, len(chosen), label="new songs auditioned")

    # Genres (MusicBrainz) in the background while clips are fetched and measured (Deezer, ReccoBeats).
    with ThreadPoolExecutor(max_workers=1) as bg:
        genre_job = bg.submit(candidate_genres, chosen, mbids, genres)
        by_id = {c["id"]: c for c in chosen}

        def done(cid, res, n):
            c = by_id[cid]
            P.tick(n, now=f"auditioned {c['title']} — {c['artist']}")
            if isinstance(res, Exception):
                print(f"  skipped {c['title']} - {c['artist']}: {res}")
                return
            bass, est = res
            if bass:
                c["bass"] = bass
            if est:
                c["features"], c["feature_source"] = est, "estimated_from_preview"
                with _mlock:
                    save("song_measurements.json", dict(_measured))   # kept the moment it arrives
            c["measured"] = True
            if n % 50 == 0:
                save("candidates.json", cands)

        with_clip = [c["id"] for c in chosen if c.get("has_preview")]
        for c in chosen:
            if not c.get("has_preview"):
                c["measured"] = True
        net.pmap(lambda cid: measure_clip(by_id[cid]), with_clip, workers=12, on_result=done)
        genre_job.result()

    # Spotify links: from ReccoBeats' data, or remembered from an earlier lookup. No Spotify requests here:
    # the dashboard resolves exact links lazily for the songs it actually shows (see spotify_links.py).
    for c in chosen:
        href = (c.get("features") or {}).get("href") or ""
        if "open.spotify.com/track/" in href:
            c["spotify_url"], c["spotify_id"], c["spotify_exact"] = href, href.rstrip("/").split("/")[-1], True
            continue
        url, _ = spotify_links.known(c["isrc"])
        c["spotify_exact"] = bool(url)
        c["spotify_url"] = url or spotify_links.search_link(c["title"], c["artist"])

    save("candidates.json", cands)
    save("artist_mbids.json", mbids)
    save("artist_genres.json", genres)
    with _mlock:
        save("song_measurements.json", dict(_measured))
    save_dz()


def prefetch():
    """Warm the Deezer cache with everything gather() will ask for, using only the song list.
    The pipeline runs this while clip analysis is waiting on ReccoBeats' slow lane; the real
    gather() later reads the same requests from cache, so its result is unchanged."""
    if (CACHE / "candidates.json").exists():
        return
    tracks, features, isrc_map = load("tracks.json", {}), load("features.json", {}), load("isrc.json", {})
    P.stage("crates", label="artists followed")
    ahead = gather(tracks, features, isrc_map, profile=None)
    official_features(ahead)                          # their official-number lookups too (remembered by ISRC)
    print(f"[prefetch] Deezer answers and official lookups ready for {len(ahead)} candidates")


def main():
    enriched, features = load("enriched.json", {}), load("features.json", {})
    isrc_map, profile = load("isrc.json", {}), load("profile.json", {})
    P.stage("crates", label="artists followed")
    cands = load("candidates.json", None)
    if cands is None:
        cands = gather(enriched, features, isrc_map, profile)
        save("candidates.json", cands)
    stage2(cands, profile)
    m = [c for c in cands.values() if c.get("measured")]
    P.fact(f"{len(m)} new songs auditioned and ready to rank", key="pulled")
    P.finish("crates")
    print(f"\n{len(cands)} candidates, {len(m)} fully measured "
          f"({sum(c['feature_source'] == 'official' for c in m)} official, "
          f"{sum(c['feature_source'] == 'estimated_from_preview' for c in m)} estimated, "
          f"{sum(bool(c.get('bass')) for c in m)} with bass)")


if __name__ == "__main__":
    main()
