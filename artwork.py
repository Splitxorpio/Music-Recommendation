"""Album covers and artist photos for the dashboard, from Deezer (cached; no images downloaded here).

Covers: every measured candidate, each sub-taste's example songs, and the most-played library songs.
Photos: top artists overall and per sub-taste.
Writes cache/artwork.json: {"tracks": {id: {"cover", "cover_m", "album"}}, "artists": {name: url}}.
"""
import json
from pathlib import Path

import progress as P
from candidates import deezer_artist, dz, fan, library, save, save_dz
from taste_profile import load

from config import CACHE
LIBRARY_TOP = 60


def cover(track):
    alb = (track or {}).get("album") or {}
    if not alb.get("cover_xl"):
        return None
    return {"cover": alb["cover_xl"], "cover_m": alb.get("cover_medium"), "album": alb.get("title")}


def main():
    enriched, features = load("enriched.json", {}), load("features.json", {})
    isrc_map, profile = load("isrc.json", {}), load("profile.json", {})
    cands = load("candidates.json", {})
    art = load("artwork.json", {"tracks": {}, "artists": {}})
    measured = [c for c in cands.values() if c.get("measured")]
    P.stage("sleeves", total=len(measured), label="sleeves printed")

    todo = [c for c in measured if c["id"] not in art["tracks"]]
    for c, d in zip(todo, fan(lambda c: dz(f"track/{c['deezer_id']}"), todo)):
        got = cover(d)
        if got:
            art["tracks"][c["id"]] = got
    P.tick(len(measured), now="printed the sleeves for your new songs")

    weights = profile["track_clusters"]
    lib_ids = sorted(weights, key=lambda i: -weights[i]["weight"])[:LIBRARY_TOP]
    lib_ids += [i for cl in profile["clusters"] for i in cl.get("example_ids", [])]
    lib_todo = [i for i in dict.fromkeys(lib_ids) if i not in art["tracks"]
                and ((features.get(i) or {}).get("isrc") or isrc_map.get(i))]
    code = lambda i: (features.get(i) or {}).get("isrc") or isrc_map.get(i)
    for i, d in zip(lib_todo, fan(lambda i: dz(f"track/isrc:{code(i)}"), lib_todo)):
        got = cover(d)
        if got:
            art["tracks"][i] = got

    _, _, _, _, by_artist = library(enriched, features, isrc_map)
    names = [a for a, _ in profile["overall"]["top_artists"]]
    names += [a for cl in profile["clusters"] for a, _ in cl["top_artists"][:6]]
    people = [n for n in dict.fromkeys(names) if n not in art["artists"]]
    def photo(name):
        d = deezer_artist(name, by_artist.get(name, []))
        full = dz(f"artist/{d['id']}") if d else None
        return (full or {}).get("picture_xl")
    for name, url in zip(people, fan(photo, people)):
        if url:
            art["artists"][name] = url

    save("artwork.json", art)
    save_dz()
    P.finish("sleeves")
    print(f"{len(art['tracks'])} covers, {len(art['artists'])} artist photos")


if __name__ == "__main__":
    main()
