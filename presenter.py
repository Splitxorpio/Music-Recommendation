"""What the dashboard page shows: the profile payload and live recommendations, built from one library's data.

Shared by the single-user dashboard (dashboard.py, data from cache/) and the multi-user API (frequency/api, data
from PostgreSQL). Pure: no files, no network. Side effects are passed in (on_shown: a pick with only a search link
was shown; link_known: an exact Spotify link already known for an ISRC).
"""
from collections import defaultdict
from datetime import date

import numpy as np

from artist_genres import key
from recommend import WEIGHTS, rank, score_all
from taste_profile import SOURCE_WEIGHT, load, merge_duplicates


def bass_word(b):
    if b is None:
        return None
    return "heavy" if b >= 0.8 else "light" if b < 0.65 else "medium"


class Data:
    @classmethod
    def from_cache(cls, **kw):
        """The single-user pipeline's files in cache/."""
        return cls(load("profile.json", {}), load("artwork.json", {"tracks": {}, "artists": {}}), load("candidates.json", {}),
                   load("enriched.json", {}), load("artist_genres.json", {}), **kw)

    def __init__(self, profile, art, cands, enriched, genres, on_shown=None, link_known=None, calibration=None):
        self.profile = profile
        self.art = art
        self.cands = cands
        self.enriched = merge_duplicates(enriched)
        self.on_shown = on_shown or (lambda isrc: None)
        self.genres = {}
        for name, g in genres.items():
            if g or key(name) not in self.genres:
                self.genres[key(name)] = g
        self.scored = score_all(self.cands, self.profile, genres, calibration)
        for c in self.scored:                          # exact links found by earlier lazy lookups
            if link_known and not c.get("spotify_exact") and c.get("isrc"):
                url = link_known(c["isrc"])
                if url:
                    c["spotify_url"], c["spotify_exact"] = url, True
        measured = [c for c in self.scored if c.get("measured")]
        tempos = [c["features"]["tempo"] for c in measured if (c.get("features") or {}).get("tempo")]
        lows = [c["bass"]["low_end_share"] for c in measured if c.get("bass")]
        self.ranges = {"bpm": [int(min(tempos)), int(max(tempos)) + 1] if tempos else [60, 200],
                       "bass": [round(min(lows), 2), round(max(lows), 2)] if lows else [0.0, 1.0]}
        self.profile_payload = self.build_profile()

    def cover(self, i):
        return self.art["tracks"].get(i) or {}

    def build_profile(self):
        p = self.profile
        clusters = []
        for cl in p["clusters"]:
            examples = []
            for i in cl.get("example_ids", []):
                t = self.enriched.get(i) or {}
                examples.append({"id": i, "title": t.get("name"), "artists": t.get("artists", []),
                                 "cover": self.cover(i).get("cover"), "cover_m": self.cover(i).get("cover_m"),
                                 "estimated": t.get("feature_source") != "official",
                                 "spotify_url": t.get("spotify_url") or f"https://open.spotify.com/track/{i}"})
            clusters.append({
                "id": cl["id"], "label": cl["name"], "share": cl["weight_share"], "tracks": cl["tracks"],
                "estimated_share": cl["estimated_share"], "traits": cl["traits"],
                "artists": [{"name": a, "share": s, "photo": self.art["artists"].get(a)} for a, s in cl["top_artists"][:6]],
                "genres": cl["top_genres"][:8], "examples": examples})

        # Tempo landscape per sub-taste: weighted, smoothed distribution of raw BPM.
        grid = np.linspace(60, 200, 71)
        land = defaultdict(lambda: np.zeros_like(grid))
        for i, tc in p["track_clusters"].items():
            f = (self.enriched.get(i) or {}).get("features") or {}
            if f.get("tempo"):
                land[tc["cluster"]] += tc["weight"] * np.exp(-0.5 * ((grid - f["tempo"]) / 4.0) ** 2)
        peak = max((v.max() for v in land.values()), default=0) or 1
        landscapes = [{"cluster": c, "points": (land[c] / peak).round(4).tolist()} for c in sorted(land)]

        # Genre constellation: top genres, linked when the same library artists carry both.
        top = [g for g, _ in p["overall"]["top_genres"]]
        share = defaultdict(float)
        for t in self.enriched.values():
            share[t["artists"][0]] += sum(SOURCE_WEIGHT.get(s, 0) for s in t["sources"])
        links = defaultdict(float)
        for a, w in share.items():
            g = [x for x in top if x in (self.genres.get(key(a)) or {})]
            for x in range(len(g)):
                for y in range(x + 1, len(g)):
                    links[(g[x], g[y])] += w
        mx = max(links.values()) if links else 1
        edges = [{"a": a, "b": b, "w": round(v / mx, 3)} for (a, b), v in sorted(links.items(), key=lambda x: -x[1])[:22]]

        lib_covers = []
        for i in sorted(p["track_clusters"], key=lambda i: -p["track_clusters"][i]["weight"]):
            c = self.cover(i)
            if c.get("cover"):
                lib_covers.append({"cover": c["cover"], "cover_m": c.get("cover_m"),
                                   "title": self.enriched[i]["name"], "artist": self.enriched[i]["artists"][0]})
            if len(lib_covers) == 24:
                break

        return {
            "meta": {"date": date.today().isoformat(), "songs": p["coverage"]["with_traits"]},
            "coverage": p["coverage"],
            "traits": p["overall"]["traits"],
            "genres": p["overall"]["top_genres"],
            "artists": [{"name": a, "share": s, "photo": self.art["artists"].get(a)} for a, s in p["overall"]["top_artists"]],
            "clusters": clusters,
            "landscapes": landscapes,
            "landscape_bpm": [60, 200],
            "genre_links": edges,
            "library_covers": lib_covers,
            "ranges": self.ranges,
            "default_weights": WEIGHTS,
            "separation": p.get("separation_score"),
        }

    def recommendations(self, q):
        def num(name, default):
            try:
                return float(q.get(name, [default])[0])
            except ValueError:
                return default
        def rng(name):
            v = q.get(name, [""])[0]
            try:
                lo, hi = (float(x) for x in v.split(","))
                return (lo, hi)
            except ValueError:
                return None
        weights = {"sound": num("ws", WEIGHTS["sound"]), "scene": num("wc", WEIGHTS["scene"]),
                   "connection": num("wn", WEIGHTS["connection"])}
        if sum(weights.values()) <= 0:
            weights = WEIGHTS
        picks = rank(self.scored, self.profile,
                     include_known=q.get("known", ["0"])[0] == "1",
                     include_far=q.get("far", ["0"])[0] == "1",
                     weights=weights, bpm=rng("bpm"), bass=rng("bass"), n=int(num("n", 24)))
        out = []
        for c in picks:
            if not c.get("spotify_exact") and "/search/" in (c.get("spotify_url") or ""):
                self.on_shown(c.get("isrc"))           # exact link fetched in the background for next time
            art = self.cover(c["id"])
            g = self.genres.get(key(c["artist"])) or {}
            b = (c.get("bass") or {}).get("low_end_share")
            out.append({
                "id": c["id"], "title": c["title"], "artist": c["artist"], "artists": c.get("artists", []),
                "album": art.get("album") or c.get("album"), "cover": art.get("cover"), "cover_m": art.get("cover_m"),
                "score": c["score"], "parts": c["parts"], "cluster": c["cluster"], "reason": c["reason"],
                "tempo": round((c.get("features") or {}).get("tempo") or 0) or None,
                "bass": b, "bass_word": bass_word(b),
                "estimated": c.get("feature_source") != "official", "scene_inferred": c.get("scene_inferred", False),
                "mode": c.get("mode"), "hop": c.get("hop"), "release": c.get("release_date"),
                "genres": [x for x, _ in sorted(g.items(), key=lambda x: -x[1])[:3]],
                "spotify_url": c.get("spotify_url")})
        return {"count": len(out), "items": out}


