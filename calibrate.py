"""Measure how clip-based estimates differ from official ReccoBeats values, and fit a correction.

Takes songs that have BOTH official values and a downloaded preview clip, runs the clip through
the same analysis used for estimated songs, and compares. Separate corrections per clip source
(iTunes M4A vs Deezer MP3), since they may cut different parts of the song.

A correction (official ~= a + b * clip) is only kept for a trait if it beats the raw estimate
under cross-validation; otherwise that trait is left as-is.

Reads cache/enriched.json, cache/features.json, cache/previews/.
Writes cache/calibration_pairs.json (raw comparisons) and cache/calibration_model.json.
"""
import json
import random
import time
from pathlib import Path

import numpy as np
import requests

import net
import progress as P

from config import CACHE
PREVIEWS = CACHE / "previews"
ANALYSE = "https://api.reccobeats.com/v1/analysis/audio-features"
PER_SOURCE = 40
TRAITS = ["acousticness", "danceability", "energy", "instrumentalness", "liveness",
          "loudness", "speechiness", "valence"]     # tempo excluded: its errors are half/double-time jumps, not a drift
SOURCES = {"itunes": ("m4a", "audio/mp4"), "deezer": ("mp3", "audio/mpeg")}
MIN_GAIN = 0.05                                     # keep a correction only if it cuts error by 5%+


def load(name, default):
    p = CACHE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    """Atomic write: stages run side by side, so a reader must never see a half-written file."""
    tmp = CACHE / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(CACHE / name)


def analyse(clip, mime):
    r = net.post("reccobeats_upload", ANALYSE, files={"audioFile": (clip.name, clip.read_bytes(), mime)})
    return r.json() if r.status_code == 200 else None


def collect_pairs(enriched, official):
    pairs = load("calibration_pairs.json", {})
    rng = random.Random(42)
    P.stage("calibrate", total=PER_SOURCE * len(SOURCES), label="songs double-checked")
    jobs = {}
    for src, (ext, mime) in SOURCES.items():
        pool = sorted(i for i, v in enriched.items()
                      if v["feature_source"] == "official" and (PREVIEWS / f"{i}.{ext}").exists())
        rng.shuffle(pool)
        have = [i for i in pool if pairs.get(i, {}).get("source") == src]
        todo = [i for i in pool if i not in pairs][: max(0, PER_SOURCE - len(have))]
        print(f"[{src}] {len(have)} already compared, analysing {len(todo)} more")
        jobs.update({i: (src, ext, mime) for i in todo})

    def done(i, clip, n):
        P.tick(len(pairs), now=f"compared our ears with the official notes on {enriched[i]['name']}")
        if isinstance(clip, Exception):
            print(f"  skipped {enriched[i]['name']}: {clip}")
        elif clip:
            pairs[i] = {"source": jobs[i][0], "name": enriched[i]["name"],
                        "official": {t: official[i][t] for t in TRAITS},
                        "clip": {t: clip[t] for t in TRAITS}}

    # both clip sources at once; ReccoBeats' own limit in net.py keeps it to 4 in flight
    net.pmap(lambda i: analyse(PREVIEWS / f"{i}.{jobs[i][1]}", jobs[i][2]), jobs, workers=8, on_result=done)
    save("calibration_pairs.json", pairs)
    return pairs


def cv_errors(x, y, folds=5):
    """Mean absolute error of raw clip values vs a fitted line, both judged on held-out songs."""
    idx = np.arange(len(x))
    raw, fit = [], []
    for f in range(folds):
        test = idx[f::folds]
        train = np.setdiff1d(idx, test)
        b, a = np.polyfit(x[train], y[train], 1)
        raw += list(np.abs(x[test] - y[test]))
        fit += list(np.abs(a + b * x[test] - y[test]))
    return float(np.mean(raw)), float(np.mean(fit))


def fit_model(pairs):
    model = {}
    print(f"\n{'source':7} {'trait':17} {'n':>3} {'bias':>7} {'match':>6} {'err raw':>8} {'err fixed':>9}  decision")
    for src in SOURCES:
        rows = [p for p in pairs.values() if p["source"] == src]
        model[src] = {}
        for t in TRAITS:
            x = np.array([p["clip"][t] for p in rows], dtype=float)
            y = np.array([p["official"][t] for p in rows], dtype=float)
            if len(x) < 15 or np.std(x) < 1e-6:
                continue
            bias = float(np.mean(x - y))                 # + means clips read too high
            corr = float(np.corrcoef(x, y)[0, 1])
            raw_err, fit_err = cv_errors(x, y)
            keep = fit_err < raw_err * (1 - MIN_GAIN)
            b, a = np.polyfit(x, y, 1)
            if keep:
                model[src][t] = {"a": round(float(a), 5), "b": round(float(b), 5)}
            model.setdefault("_report", {}).setdefault(src, {})[t] = {
                "n": len(x), "bias": round(bias, 4), "correlation": round(corr, 3),
                "error_raw": round(raw_err, 4), "error_corrected": round(fit_err, 4), "applied": keep}
            print(f"{src:7} {t:17} {len(x):3} {bias:+7.3f} {corr:6.2f} {raw_err:8.3f} {fit_err:9.3f}  "
                  f"{'correct' if keep else 'leave as is'}")
    save("calibration_model.json", model)
    rep = model.get("_report", {})
    worst = max(((src, t, r) for src, d in rep.items() for t, r in d.items() if r["applied"]),
                key=lambda x: abs(x[2]["bias"]) / (1 if x[1] != "loudness" else 10), default=None)
    if worst:
        src, t, r = worst
        word = "too high" if r["bias"] > 0 else "too low"
        P.fact(f"{src.title()} clips read {t} {word} by {abs(r['bias']):.2f}. Corrected.", key="calibration")
    P.finish("calibrate")
    return model


def main():
    enriched = load("enriched.json", {})
    official = load("features.json", {})
    pairs = collect_pairs(enriched, official)
    fit_model(pairs)


if __name__ == "__main__":
    main()
