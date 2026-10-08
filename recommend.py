"""Score candidate songs against the taste profile and build a recommendation list.

Fast (no network): reads cache/candidates.json (from candidates.py), cache/profile.json,
cache/artist_genres.json, cache/calibration_model.json. The dashboard calls rank() live,
so options can be flipped instantly:
  include_known  - also suggest unheard songs by artists already in the library
  include_far    - also suggest artists two steps out (related to related artists)

Each song gets three 0-100 parts:
  sound      - closeness to the nearest sub-taste (energy, bass, tempo feel, mood...)
  scene      - overlap between its artist's genres and the user's genre mix
  connection - how strongly the user's own artists point to it, weighted by how much they're played
"""
import json
import math
from collections import defaultdict
from pathlib import Path

from artist_genres import key
from taste_profile import correct_estimate, load, score_track

from config import CACHE
WEIGHTS = {"sound": 0.5, "scene": 0.25, "connection": 0.25}
LINK_WORDS = {"related": "related to", "radio": "on the radio for", "musicbrainz": "has worked with",
              "own": "", "reccobeats": "sounds like your"}


def genre_vector(pairs):
    tot = sum(v for _, v in pairs) or 1
    return {g: v / tot for g, v in pairs}


def cosine(a, b):
    dot = sum(a[k] * b.get(k, 0) for k in a)
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def scene_score(artists, artist_genres, mine):
    votes = defaultdict(float)
    for a in artists:
        g = artist_genres.get(key(a)) or {}
        tot = sum(g.values())
        for name, c in g.items():
            votes[name] += c / tot
    if not votes:
        return None
    return round(100 * cosine(genre_vector(votes.items()), mine), 1)


def sound_score(c, profile, calibration):
    f = c.get("features")
    if not f:
        return None, None
    if c.get("feature_source") != "official":
        f = correct_estimate(c["id"], f, calibration)
    bass = (c.get("bass") or {}).get("low_end_share")
    s = score_track(f, bass, profile)
    return s["score"], s["cluster"]


def connection_raw(c):
    return sum(l["strength"] for l in c.get("links", []))


def score_all(cands, profile=None, artist_genres=None, calibration=None):
    profile = profile or load("profile.json", {})
    artist_genres = artist_genres if artist_genres is not None else load("artist_genres.json", {})
    calibration = calibration if calibration is not None else load("calibration_model.json", {})
    mine = genre_vector(profile["overall"]["top_genres"])
    # Case-insensitive lookup: Deezer says "SLAYR" where MusicBrainz says "slayr".
    by_key = {}
    for name, g in artist_genres.items():
        if g or key(name) not in by_key:
            by_key[key(name)] = g

    raws = sorted(connection_raw(c) for c in cands.values())
    def pct(v):                                        # percentile among candidates, 0-100
        lo = sum(r < v for r in raws)
        eq = sum(r == v for r in raws)
        return round(100 * (lo + 0.5 * eq) / max(len(raws), 1), 1)

    scored = []
    for c in cands.values():
        snd, cluster = sound_score(c, profile, calibration)
        scene = scene_score(c.get("artists") or [c["artist"]], by_key, mine)
        inferred = False
        if scene is None:
            # Untagged (often small underground) artist: borrow genres from the library artists
            # that point to it, since related/radio links are scene-mates. Labelled as inferred.
            vias = [l["via"] for l in c.get("links", []) if l["type"] in ("related", "radio", "musicbrainz")]
            scene = scene_score(vias, by_key, mine)
            inferred = scene is not None
        parts = {"sound": snd, "scene": scene, "connection": pct(connection_raw(c))}
        scored.append({**c, "parts": parts, "cluster": cluster, "scene_inferred": inferred})
    return scored


def total(parts, weights):
    have = {k: v for k, v in parts.items() if v is not None and weights.get(k, 0) > 0}
    wsum = sum(weights[k] for k in have)
    return round(sum(weights[k] * v for k, v in have.items()) / wsum, 1) if wsum else 0.0


def reason(c, profile):
    bits = []
    links = sorted(c.get("links", []), key=lambda l: -l["strength"])
    vias = []
    for l in links:
        if l["type"] in ("related", "radio", "musicbrainz", "own") and l["via"] not in vias:
            vias.append(l["via"])
    if c.get("mode") == "known":
        bits.append(f"New to you from {c['artist']}")
    elif vias:
        bits.append("Because you play " + " and ".join(vias[:2]) + (" (2 steps out)" if c.get("hop") == 2 else ""))
    elif any(l["type"] == "reccobeats" for l in links):
        bits.append("Sound-alike suggestion")
    if c.get("cluster") is not None:
        bits.append(f"fits your \"{profile['clusters'][c['cluster']]['name']}\" side")
    f = c.get("features") or {}
    b = (c.get("bass") or {}).get("low_end_share")
    detail = []
    if f.get("tempo"):
        detail.append(f"{f['tempo']:.0f} BPM")
    if b is not None:
        detail.append("heavy bass" if b >= 0.8 else "light bass" if b < 0.65 else "medium bass")
    if detail:
        bits.append(", ".join(detail))
    return " · ".join(bits)


def rank(scored, profile=None, include_known=False, include_far=False, weights=None,
         bpm=None, bass=None, per_artist=2, n=40, spread=True):
    """Filter, score and pick a varied list. bpm/bass are (low, high) ranges or None."""
    profile = profile or load("profile.json", {})
    weights = weights or WEIGHTS
    pool = []
    for c in scored:
        if not c.get("measured"):
            continue                                   # only fully measured candidates are eligible
        if c.get("mode") == "known" and not include_known:
            continue
        if c.get("hop") == 2 and not include_far:
            continue
        if c["parts"]["sound"] is None and weights.get("sound", 0) > 0:
            continue                                   # can't judge how it sounds
        tempo = (c.get("features") or {}).get("tempo")
        low_end = (c.get("bass") or {}).get("low_end_share")
        if bpm and (tempo is None or not bpm[0] <= tempo <= bpm[1]):
            continue
        if bass and (low_end is None or not bass[0] <= low_end <= bass[1]):
            continue
        pool.append({**c, "score": total(c["parts"], weights)})
    pool.sort(key=lambda c: -c["score"])

    picked, per = [], defaultdict(int)
    def take(c):
        if per[c["artist"]] >= per_artist or c in picked:
            return False
        per[c["artist"]] += 1
        picked.append(c)
        return True

    if spread:                                         # mirror how the user's listening splits
        for cl in profile["clusters"]:
            quota = round(n * cl["weight_share"])
            for c in (c for c in pool if c["cluster"] == cl["id"]):
                if quota <= 0:
                    break
                quota -= take(c)
    for c in pool:                                     # top up with the best remaining
        if len(picked) >= n:
            break
        take(c)
    picked.sort(key=lambda c: -c["score"])
    for c in picked[:n]:
        c["reason"] = reason(c, profile)
    return picked[:n]


def main():
    cands = load("candidates.json", {})
    profile = load("profile.json", {})
    scored = score_all(cands, profile)
    (CACHE / "recommendations.json").write_text(json.dumps(scored, indent=1))
    measured = [c for c in scored if c["parts"]["sound"] is not None]
    print(f"{len(scored)} candidates scored, {len(measured)} with sound data")
    for label, kw in [("New artists, close to your scenes (default)", {}),
                      ("+ unheard songs from artists you know", {"include_known": True}),
                      ("+ two steps out", {"include_far": True})]:
        recs = rank(scored, profile, n=15, **kw)
        print(f"\n== {label} ==")
        for c in recs:
            p = c["parts"]
            fmt = lambda v: "?" if v is None else f"{v:.0f}"
            scene = fmt(p["scene"]) + ("~" if c.get("scene_inferred") else "")
            print(f"  {c['score']:5.1f}  {c['title'][:34]:34} - {c['artist'][:18]:18} "
                  f"[sound {fmt(p['sound'])} scene {scene} conn {fmt(p['connection'])}]")
            print(f"         {c['reason']}")


if __name__ == "__main__":
    main()
