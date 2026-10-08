"""Build the weighted taste fingerprint and split it into sub-tastes.

Reads cache/enriched.json (+ cache/artist_genres.json if present).
Writes cache/profile.json: overall trait distributions, top artists/genres, sub-taste clusters,
the scaling/centroids needed to score new songs, and each track's cluster.
"""
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

import progress as P

from config import CACHE

# How much each listening signal counts. Recent favourites count most, but saved and
# long-term songs still contribute; a song in several lists adds up.
SOURCE_WEIGHT = {"top_short_term": 3.0, "top_medium_term": 2.0, "top_long_term": 1.5,
                 "recent": 1.5, "saved": 1.0}

CLUSTER_FEATURES = ["energy", "danceability", "valence", "acousticness", "speechiness",
                    "instrumentalness", "loudness", "tempo_feel", "low_end_share"]
REPORT_FEATURES = ["tempo", "tempo_feel", "energy", "danceability", "valence", "acousticness",
                   "speechiness", "instrumentalness", "loudness", "low_end_share"]

# Plain-language words for "above / below your average" on each trait.
TRAIT_WORDS = {
    "energy": ("high-energy", "laid-back"),
    "danceability": ("danceable", "less danceable"),
    "valence": ("upbeat", "dark"),
    "acousticness": ("acoustic", "produced"),
    "speechiness": ("rap-heavy", "melodic"),
    "instrumentalness": ("instrumental", "vocal"),
    "loudness": ("loud", "quieter"),
    "tempo_feel": ("fast", "slow"),
    "low_end_share": ("bass-heavy", "light on bass"),
}
K_RANGE = (3, 4)
MIN_CLUSTER_SHARE = 0.05   # a "sub-taste" smaller than this is just a few outliers
N_INIT = int(__import__("os").environ.get("FREQUENCY_N_INIT", 200))   # restarts per grouping: enough that a near-tie isn't settled by luck
Z_CAP = 2.5                # stop one extreme trait (e.g. rare instrumentals) dominating the grouping


def load(name, default):
    p = CACHE / name
    return json.loads(p.read_text()) if p.exists() else default


def tempo_feel(bpm):
    """Fold half/double-time readings (70 vs 140) into one 75-150 range, so the
    same groove isn't split in two by how the tempo detector happened to count it."""
    if not bpm or bpm <= 0:
        return None
    while bpm < 75:
        bpm *= 2
    while bpm >= 150:
        bpm /= 2
    return bpm


def wquantile(x, w, q):
    order = np.argsort(x)
    x, w = np.asarray(x)[order], np.asarray(w)[order]
    c = np.cumsum(w) - 0.5 * w
    return float(np.interp(q * w.sum(), c, x))


def merge_duplicates(enriched):
    """The same song released twice (single + album) has two Spotify IDs. Merge them so it isn't
    double-counted: keep the copy with the best data, and combine the lists it appears in."""
    groups = defaultdict(list)
    for i, t in enriched.items():
        groups[(t["name"].lower().strip(), t["artists"][0])].append(i)
    out = {}
    for ids in groups.values():
        ids.sort(key=lambda i: (enriched[i]["feature_source"] != "official", enriched[i]["features"] is None))
        t = dict(enriched[ids[0]])
        t["sources"] = sorted({s for i in ids for s in enriched[i]["sources"]})
        t["bass"] = t["bass"] or next((enriched[i]["bass"] for i in ids if enriched[i]["bass"]), None)
        out[ids[0]] = t
    return out


def correct_estimate(i, f, calibration):
    """Apply the per-source correction from calibrate.py to a clip-estimated song."""
    src = "deezer" if (CACHE / "previews" / f"{i}.mp3").exists() else "itunes"
    f = dict(f)
    for trait, c in calibration.get(src, {}).items():
        if f.get(trait) is not None:
            v = c["a"] + c["b"] * f[trait]
            f[trait] = v if trait == "loudness" else min(1.0, max(0.0, v))
    return f


def build_rows(enriched):
    calibration = load("calibration_model.json", {})
    rows = []
    for i, t in enriched.items():
        f = t["features"]
        if not f:
            continue
        if t["feature_source"] != "official":
            f = correct_estimate(i, f, calibration)
        row = {k: f.get(k) for k in ["tempo", "energy", "danceability", "valence", "acousticness",
                                     "speechiness", "instrumentalness", "loudness", "liveness"]}
        row["tempo_feel"] = tempo_feel(f.get("tempo"))
        row["low_end_share"] = (t["bass"] or {}).get("low_end_share")
        rows.append({"id": i, "name": t["name"], "artists": t["artists"],
                     "weight": sum(SOURCE_WEIGHT.get(s, 0) for s in t["sources"]),
                     "estimated": t["feature_source"] != "official", **row})
    return rows


def genre_mix(rows, idx, artist_genres):
    """Weighted genre shares: each track's weight is split across its artists, and each
    artist's share is split across their genres by vote count."""
    g = defaultdict(float)
    for r in (rows[k] for k in idx):
        arts = [a for a in r["artists"] if artist_genres.get(a)]
        for a in arts:
            votes = artist_genres[a]
            tot = sum(votes.values())
            for name, c in votes.items():
                g[name] += r["weight"] / len(arts) * c / tot
    total = sum(g.values()) or 1
    return [(k, round(v / total, 3)) for k, v in sorted(g.items(), key=lambda x: -x[1])[:12]]


def artist_mix(rows, idx):
    a = defaultdict(float)
    for r in (rows[k] for k in idx):
        a[r["artists"][0]] += r["weight"]
    total = sum(a.values()) or 1
    return [(k, round(v / total, 3)) for k, v in sorted(a.items(), key=lambda x: -x[1])[:10]]


def describe(z):
    """Name a cluster from the 3 traits where it differs most from the overall average."""
    top = sorted(range(len(z)), key=lambda j: -abs(z[j]))[:3]
    return ", ".join(TRAIT_WORDS[CLUSTER_FEATURES[j]][0 if z[j] > 0 else 1] for j in top)


def stats(rows, idx):
    out = {}
    w = np.array([rows[k]["weight"] for k in idx])
    for f in REPORT_FEATURES:
        x = np.array([rows[k][f] for k in idx], dtype=float)
        ok = ~np.isnan(x)
        if ok.sum():
            out[f] = {"mean": round(float(np.average(x[ok], weights=w[ok])), 3),
                      "p10": round(wquantile(x[ok], w[ok], 0.1), 3),
                      "p50": round(wquantile(x[ok], w[ok], 0.5), 3),
                      "p90": round(wquantile(x[ok], w[ok], 0.9), 3)}
    return out


def score_track(features, low_end_share, profile):
    """0-100 sound match of a song against the closest sub-taste, plus which one.
    (Recommendation ranking will add genre and artist-connection signals on top.)"""
    vals = dict(features, tempo_feel=tempo_feel(features.get("tempo")), low_end_share=low_end_share)
    mean, std = np.array(profile["scaler"]["mean"]), np.array(profile["scaler"]["std"])
    x = np.array([vals.get(f) if vals.get(f) is not None else m
                  for f, m in zip(CLUSTER_FEATURES, mean)], dtype=float)
    z = np.clip((x - mean) / std, -Z_CAP, Z_CAP)
    best = None
    for c in profile["clusters"]:
        d = float(np.linalg.norm(z - np.array(c["centroid_z"])))
        s = 100 * np.exp(-0.5 * (d / c["typical_distance"]) ** 2)
        if not best or s > best[0]:
            best = (round(float(s), 1), c["id"])
    return {"score": best[0], "cluster": best[1]}


def main():
    P.stage("fingerprint")
    raw = load("enriched.json", {})
    enriched = merge_duplicates(raw)
    print(f"  merged {len(raw) - len(enriched)} duplicate releases")
    artist_genres = load("artist_genres.json", {})
    rows = build_rows(enriched)
    w = np.array([r["weight"] for r in rows])
    X = np.array([[r[f] if r[f] is not None else np.nan for f in CLUSTER_FEATURES] for r in rows], dtype=float)

    # Fill the few missing values (e.g. no bass clip) with the weighted median.
    for j in range(X.shape[1]):
        miss = np.isnan(X[:, j])
        if miss.any():
            X[miss, j] = wquantile(X[~miss, j], w[~miss], 0.5)

    mean = np.average(X, axis=0, weights=w)
    std = np.sqrt(np.average((X - mean) ** 2, axis=0, weights=w)) + 1e-9
    Z = np.clip((X - mean) / std, -Z_CAP, Z_CAP)

    best = None
    for k in range(K_RANGE[0], K_RANGE[1] + 1):
        km = KMeans(n_clusters=k, n_init=N_INIT, random_state=0).fit(Z, sample_weight=w)
        sil = silhouette_score(Z, km.labels_)
        smallest = min(w[km.labels_ == c].sum() for c in range(k)) / w.sum()
        print(f"  k={k}: separation score {sil:.3f}, smallest group {smallest:.0%}")
        if smallest < MIN_CLUSTER_SHARE:
            continue
        if not best or sil > best[1]:
            best = (km, sil)
    if not best:                                       # nothing passed the size rule
        km = KMeans(n_clusters=K_RANGE[0], n_init=N_INIT, random_state=0).fit(Z, sample_weight=w)
        best = (km, silhouette_score(Z, km.labels_))
    km, sil = best
    labels = km.labels_

    clusters = []
    order = np.argsort([-w[labels == c].sum() for c in range(km.n_clusters)])
    for new_id, c in enumerate(order):
        idx = [k for k in range(len(rows)) if labels[k] == c]
        cz = km.cluster_centers_[c]
        dists = np.linalg.norm(Z[idx] - cz, axis=1)
        reps, seen = [], set()
        for j in np.argsort(dists - 0.05 * w[idx]):     # same song can appear under two releases
            r = rows[idx[j]]
            if (r["name"].lower(), r["artists"][0]) not in seen:
                seen.add((r["name"].lower(), r["artists"][0]))
                reps.append(r)
            if len(reps) == 6:
                break
        clusters.append({
            "id": new_id,
            "name": describe(cz),
            "tracks": len(idx),
            "weight_share": round(float(w[idx].sum() / w.sum()), 3),
            "estimated_share": round(sum(rows[k]["estimated"] for k in idx) / len(idx), 3),
            "centroid_z": [round(float(v), 4) for v in cz],
            "typical_distance": round(float(np.median(dists)), 4),
            "traits": stats(rows, idx),
            "top_artists": artist_mix(rows, idx),
            "top_genres": genre_mix(rows, idx, artist_genres),
            "examples": [f'{r["name"]} - {", ".join(r["artists"])}' for r in reps],
            "example_ids": [r["id"] for r in reps],
        })
    remap = {int(c): n for n, c in enumerate(order)}

    all_idx = list(range(len(rows)))
    profile = {
        "source_weights": SOURCE_WEIGHT,
        "coverage": {"tracks": len(enriched), "with_traits": len(rows),
                     "estimated": sum(r["estimated"] for r in rows),
                     "with_bass": sum(r["low_end_share"] is not None for r in rows),
                     "artists_with_genres": sum(1 for g in artist_genres.values() if g)},
        "overall": {"traits": stats(rows, all_idx), "top_artists": artist_mix(rows, all_idx),
                    "top_genres": genre_mix(rows, all_idx, artist_genres)},
        "cluster_features": CLUSTER_FEATURES,
        "scaler": {"mean": mean.round(5).tolist(), "std": std.round(5).tolist()},
        "separation_score": round(float(sil), 3),
        "clusters": clusters,
        "track_clusters": {r["id"]: {"cluster": remap[int(labels[k])], "weight": r["weight"]}
                           for k, r in enumerate(rows)},
    }
    (CACHE / "profile.json").write_text(json.dumps(profile, indent=1))
    P.fact(f"You have {len(clusters)} worlds: " + "; ".join(c["name"] for c in clusters))
    a, sh = profile["overall"]["top_artists"][0]
    P.fact(f"{a} is {sh:.0%} of everything you play", key="top-artist")
    P.finish("fingerprint")

    o = profile["overall"]["traits"]
    print(f"\n== Your overall fingerprint ({len(rows)} songs) ==")
    print(f"  tempo feel ~{o['tempo_feel']['p50']:.0f} BPM (most songs {o['tempo_feel']['p10']:.0f}-{o['tempo_feel']['p90']:.0f})")
    for f in ["energy", "danceability", "valence", "acousticness", "speechiness", "low_end_share"]:
        print(f"  {f}: {o[f]['mean']:.2f}  (range {o[f]['p10']:.2f}-{o[f]['p90']:.2f})")
    print("  top genres:", ", ".join(f"{g} {s:.0%}" for g, s in profile["overall"]["top_genres"][:8]) or "(none yet)")
    print("  top artists:", ", ".join(f"{a} {s:.0%}" for a, s in profile["overall"]["top_artists"][:8]))
    for c in clusters:
        t = c["traits"]
        print(f"\n== Sub-taste {c['id'] + 1}: {c['name']} — {c['weight_share']:.0%} of your listening, "
              f"{c['tracks']} songs ({c['estimated_share']:.0%} estimated) ==")
        print(f"  tempo feel ~{t['tempo_feel']['p50']:.0f} | energy {t['energy']['mean']:.2f} | mood {t['valence']['mean']:.2f} "
              f"| bass {t['low_end_share']['mean']:.2f} | rap-ness {t['speechiness']['mean']:.2f}")
        print("  artists:", ", ".join(a for a, _ in c["top_artists"][:6]))
        print("  genres: ", ", ".join(g for g, _ in c["top_genres"][:6]) or "(none yet)")
        print("  e.g.    ", " | ".join(c["examples"][:4]))


if __name__ == "__main__":
    main()
