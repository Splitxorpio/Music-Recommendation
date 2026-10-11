"""Versioned calibration of clip-based estimates.

A clip estimate (ReccoBeats analysing a 30 s Deezer/iTunes preview) is biased in known ways: clips read
acousticness too high and loudness too low. A calibration is a per-source, per-trait straight-line
correction (official ~= a + b * clip), learnt from tracks measured both ways (calibration_pairs).

Lifecycle:  candidate ──validate()──▶ validated ──activate()──▶ active ──(next activation)──▶ retired
                        └──────────▶ rejected                     ▲── rollback() re-activates the last retired one

Rules that make it safe for other people's libraries:
  * Held-out validation by track: every track is permanently in 'train' or 'holdout' (a hash of its id),
    so a version is judged on tracks it never saw. A trait's correction is kept only if it beats the raw
    value under cross-validation on train AND on the holdout, with at least MIN_PAIRS training pairs.
  * No extrapolation: each correction records the range of raw clip values it was trained on. A value
    outside it is left raw and the measurement is flagged low_confidence.
  * Traits whose clip readings correlate weakly with official values (or whose best line is nearly flat) are
    never "corrected": a flat line would lower average error by giving every song the average, erasing the trait.
    They're left raw and flagged 'rough'.
  * Training data provenance: pairs come from the operator's own library, public sample tracks, or users
    who opted in (users.calibration_consent). A user's pairs are deleted if they withdraw or delete their
    account. Nothing is pooled silently.
  * Raw estimates are always stored; changing the active version re-calibrates stored estimates without
    any provider calls (recalibrate_stored()).
"""
import hashlib
from datetime import datetime, timezone

import numpy as np

from . import db

TRAITS = ["acousticness", "danceability", "energy", "instrumentalness", "liveness", "loudness", "speechiness", "valence"]
SOURCES = ("deezer", "itunes")
MIN_PAIRS = 25             # training pairs per source/trait before a correction may be learnt
HOLDOUT_SHARE = 0.25
MIN_GAIN = 0.05            # correction must cut mean absolute error by 5%+ (cross-validated and on holdout)
ROUGH_CORRELATION = 0.7   # below this, clip readings are too loose to calibrate: left raw and flagged 'rough'
SLOPE_RANGE = (0.5, 2.0)  # a correction rescales a reading; a flatter line would mostly replace it with the average
RANGE_TOLERANCE = {"loudness": 1.0}   # dB slack at the ends of the trained range; 0.02 for 0-1 traits


def split_of(track_id: int) -> str:
    h = int(hashlib.sha256(f"calibration-split:{track_id}".encode()).hexdigest()[:8], 16)
    return "holdout" if h / 0xFFFFFFFF < HOLDOUT_SHARE else "train"


def _cv_errors(x, y, folds=5):
    idx = np.arange(len(x))
    raw, fit = [], []
    for f in range(folds):
        test = idx[f::folds]
        train = np.setdiff1d(idx, test)
        b, a = np.polyfit(x[train], y[train], 1)
        raw += list(np.abs(x[test] - y[test]))
        fit += list(np.abs(a + b * x[test] - y[test]))
    return float(np.mean(raw)), float(np.mean(fit))


def fit(pairs: list[dict]) -> tuple[dict, dict, dict]:
    """pairs: [{track_id, clip_source, official: {trait: v}, clip: {trait: v}}].
    Returns (model, supported_ranges, validation report)."""
    model, ranges, report = {}, {}, {}
    for src in SOURCES:
        rows = [p for p in pairs if p["clip_source"] == src]
        train = [p for p in rows if split_of(p["track_id"]) == "train"]
        hold = [p for p in rows if split_of(p["track_id"]) == "holdout"]
        for t in TRAITS:
            tr = [(p["clip"][t], p["official"][t]) for p in train if p["clip"].get(t) is not None and p["official"].get(t) is not None]
            ho = [(p["clip"][t], p["official"][t]) for p in hold if p["clip"].get(t) is not None and p["official"].get(t) is not None]
            r = {"n_train": len(tr), "n_holdout": len(ho), "applied": False}
            report.setdefault(src, {})[t] = r
            if len(tr) < MIN_PAIRS:
                r["why"] = f"only {len(tr)} training pairs (need {MIN_PAIRS})"
                continue
            x, y = np.array([a for a, _ in tr], float), np.array([b for _, b in tr], float)
            if np.std(x) < 1e-6:
                r["why"] = "no variation in clip values"
                continue
            r["correlation"] = round(float(np.corrcoef(x, y)[0, 1]), 3)
            r["bias"] = round(float(np.mean(x - y)), 4)
            cv_raw, cv_fit = _cv_errors(x, y)
            r.update(cv_error_raw=round(cv_raw, 4), cv_error_corrected=round(cv_fit, 4))
            b, a = np.polyfit(x, y, 1)
            if ho:
                hx, hy = np.array([a_ for a_, _ in ho], float), np.array([b_ for _, b_ in ho], float)
                h_raw, h_fit = float(np.mean(np.abs(hx - hy))), float(np.mean(np.abs(a + b * hx - hy)))
                r.update(holdout_error_raw=round(h_raw, 4), holdout_error_corrected=round(h_fit, 4))
            else:
                h_raw = h_fit = None
            cv_ok = cv_fit < cv_raw * (1 - MIN_GAIN)
            ho_ok = h_raw is not None and len(ho) >= 5 and h_fit < h_raw * (1 - MIN_GAIN)
            shape_ok = r["correlation"] >= ROUGH_CORRELATION and SLOPE_RANGE[0] <= b <= SLOPE_RANGE[1]
            r["slope"] = round(float(b), 3)
            if r["correlation"] < ROUGH_CORRELATION:
                r["rough"] = True
            if cv_ok and ho_ok and shape_ok:
                model.setdefault(src, {})[t] = {"a": round(float(a), 5), "b": round(float(b), 5)}
                ranges.setdefault(src, {})[t] = [round(float(x.min()), 4), round(float(x.max()), 4)]
                r["applied"] = True
            else:
                r["why"] = ("clip readings too loosely related to official values to calibrate" if not shape_ok else
                            "not better under cross-validation" if not cv_ok else
                            "too few held-out tracks to confirm" if h_raw is None or len(ho) < 5 else
                            "not better on held-out tracks")
    return model, ranges, report


def create_candidate(cur, version: str | None = None, notes: str | None = None) -> int:
    """Fit a new candidate version from every permitted pair currently stored."""
    cur.execute("SELECT track_id, clip_source, official, clip, origin FROM calibration_pairs")
    pairs = cur.fetchall()
    model, ranges, report = fit(pairs)
    origins: dict[str, int] = {}
    for p in pairs:
        origins[p["origin"]] = origins.get(p["origin"], 0) + 1
    version = version or "cal-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cur.execute("""INSERT INTO calibration_versions (version, status, model, supported_ranges, validation, training_data, notes)
                   VALUES (%s, 'candidate', %s, %s, %s, %s, %s) RETURNING id""",
                (version, db.jsonb(model), db.jsonb(ranges), db.jsonb(report),
                 db.jsonb({"pairs": len(pairs), "by_origin": origins,
                           "by_source": {s: sum(1 for p in pairs if p["clip_source"] == s) for s in SOURCES},
                           "holdout_share": HOLDOUT_SHARE, "min_pairs": MIN_PAIRS}), notes))
    return cur.fetchone()["id"]


def validate(cur, version_id: int) -> str:
    """candidate -> validated if it has held-out evidence for at least one correction and its held-out error
    is no worse than raw on any corrected trait; otherwise rejected. Legacy imports without a holdout report
    are always rejected."""
    cur.execute("SELECT * FROM calibration_versions WHERE id = %s FOR UPDATE", (version_id,))
    v = cur.fetchone()
    if v["status"] != "candidate":
        raise ValueError(f"version {v['version']} is {v['status']}, not a candidate")
    rep = v["validation"] or {}
    applied = [(s, t, r) for s, d in rep.items() for t, r in d.items() if r.get("applied")]
    ok = bool(applied) and all(r.get("holdout_error_corrected") is not None and
                               r["holdout_error_corrected"] <= r["holdout_error_raw"] for _, _, r in applied)
    status = "validated" if ok else "rejected"
    cur.execute("UPDATE calibration_versions SET status = %s, validated_at = CASE WHEN %s THEN now() END WHERE id = %s",
                (status, ok, version_id))
    return status


def activate(cur, version_id: int):
    cur.execute("SELECT status FROM calibration_versions WHERE id = %s FOR UPDATE", (version_id,))
    st = cur.fetchone()["status"]
    if st not in ("validated", "retired"):
        raise ValueError(f"only a validated (or previously active) version can be activated, not a {st} one")
    cur.execute("UPDATE calibration_versions SET status = 'retired', retired_at = now() WHERE status = 'active'")
    cur.execute("UPDATE calibration_versions SET status = 'active', activated_at = now(), retired_at = NULL WHERE id = %s",
                (version_id,))
    recalibrate_stored(cur)


def rollback(cur) -> int | None:
    """Re-activate the most recently retired version (the one active before the current)."""
    cur.execute("SELECT id FROM calibration_versions WHERE status = 'retired' AND validated_at IS NOT NULL "
                "ORDER BY retired_at DESC LIMIT 1")
    r = cur.fetchone()
    if not r:
        return None
    activate(cur, r["id"])
    return r["id"]


def active(cur) -> dict | None:
    cur.execute("SELECT id, version, model, supported_ranges, validation FROM calibration_versions WHERE status = 'active'")
    return cur.fetchone()


def apply(values: dict, clip_source: str | None, cal: dict | None) -> tuple[dict | None, list[str], str]:
    """(calibrated values or None, flags, confidence) for one raw clip estimate."""
    if not cal:
        return None, ["no_active_calibration"], "unvalidated"
    if clip_source not in SOURCES:
        return None, ["unknown_clip_source"], "unvalidated"
    out, flags = dict(values), []
    corr = (cal["model"] or {}).get(clip_source, {})
    ranges = (cal["supported_ranges"] or {}).get(clip_source, {})
    for t, c in corr.items():
        v = values.get(t)
        if v is None:
            continue
        lo, hi = ranges.get(t, (None, None))
        tol = RANGE_TOLERANCE.get(t, 0.02)
        if lo is None or v < lo - tol or v > hi + tol:
            flags.append(f"out_of_range:{t}")            # no extrapolation: keep the raw reading
            continue
        y = c["a"] + c["b"] * v
        out[t] = y if t == "loudness" else min(1.0, max(0.0, y))
    for t, r in ((cal.get("validation") or {}).get(clip_source) or {}).items():
        if r.get("rough"):
            flags.append(f"rough:{t}")
    confidence = "low_confidence" if any(f.startswith("out_of_range") for f in flags) else "estimated"
    return out, flags, confidence


def recalibrate_stored(cur) -> int:
    """Re-apply the active version to every current clip estimate (raw values are kept, so no provider calls)."""
    cal = active(cur)
    cur.execute("SELECT id, values, clip_source, flags FROM track_measurements WHERE kind = 'clip_estimate' AND superseded_at IS NULL")
    rows = cur.fetchall()
    for r in rows:
        calibrated, flags, conf = apply(r["values"], r["clip_source"], cal)
        keep = [f for f in r["flags"] if not f.startswith(("out_of_range:", "rough:", "no_active", "unknown_clip"))]
        cur.execute("UPDATE track_measurements SET calibrated = %s, calibration_version_id = %s, confidence = %s, flags = %s "
                    "WHERE id = %s", (db.jsonb(calibrated) if calibrated is not None else None, cal and cal["id"], conf,
                                      keep + flags, r["id"]))
    return len(rows)
