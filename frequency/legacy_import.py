"""Import the single-user pipeline's cache (cache/*.json, models/calibration.json) into PostgreSQL.

    python -m frequency.legacy_import --email you@example.com [--cache path/to/cache]

What it does, and deliberately does not do:
  * Library songs become canonical tracks (Spotify id + ISRC), assigned to the given user's library.
  * Official ReccoBeats values are stored as 'official' measurements flagged 'legacy_import'.
  * Clip estimates are stored RAW with confidence 'unvalidated' and no calibrated values. They are never
    treated as validated: the active calibration (once one is validated) is applied when they're used.
  * models/calibration.json becomes a 'candidate' calibration version, not an active one: it was fit on
    one person's library and its raw pairs weren't kept, so it can't be re-validated (see calibration.py).
  * Running it twice is safe (identifiers and measurements are matched, not duplicated).
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, db, userdata
from .settings import ROOT

SIGNAL_WEIGHT = {"top_short_term": 3.0, "top_medium_term": 2.0, "top_long_term": 1.5, "recent": 1.5, "saved": 1.0}
TRAITS = ["acousticness", "danceability", "energy", "instrumentalness", "liveness", "loudness",
          "speechiness", "valence", "tempo", "key", "mode"]


def _load(cache: Path, name, default):
    p = cache / name
    return json.loads(p.read_text()) if p.exists() else default


def _traits(f):
    return {k: f[k] for k in TRAITS if f.get(k) is not None}


def import_calibration(cur, cache: Path, model_path: Path) -> int | None:
    if not model_path.exists():
        return None
    rec = json.loads(model_path.read_text())
    version = "legacy-" + datetime.fromtimestamp(rec["created"], timezone.utc).strftime("%Y%m%d-%H%M%S")
    cur.execute("SELECT id FROM calibration_versions WHERE version = %s", (version,))
    r = cur.fetchone()
    if r:
        return r["id"]
    model = {k: v for k, v in rec["model"].items() if not k.startswith("_")}
    cur.execute("""INSERT INTO calibration_versions (version, status, model, training_data, notes)
                   VALUES (%s, 'candidate', %s, %s, %s) RETURNING id""",
                (version, db.jsonb(model),
                 db.jsonb({"origin": "legacy", "pairs": rec.get("pairs"), "by_source": rec.get("by_source"),
                           "report": rec["model"].get("_report"), "consent": "operator's own library"}),
                 "Imported from models/calibration.json. Fit on a single library; raw pairs were not kept, so it "
                 "cannot be validated on held-out tracks. Not active."))
    return cur.fetchone()["id"]


def run(email: str, cache: Path, model_path: Path, verbose=True) -> dict:
    tracks = _load(cache, "tracks.json", {})
    features = _load(cache, "features.json", {})
    isrcs = {i: f["isrc"] for i, f in features.items() if f.get("isrc")}
    isrcs.update({i: c for i, c in _load(cache, "isrc.json", {}).items() if c})
    isrcs.update({i: t["isrc"] for i, t in tracks.items() if t.get("isrc")})
    estimates = _load(cache, "estimated_features.json", {})
    bass = _load(cache, "bass.json", {})
    deezer_m = _load(cache, "deezer_matches.json", {})
    itunes_m = _load(cache, "itunes_matches.json", {})
    mbids = _load(cache, "artist_mbids.json", {})
    genres = _load(cache, "artist_genres.json", {})
    pairs = _load(cache, "calibration_pairs.json", {})
    stats = {"tracks": 0, "official": 0, "estimates": 0, "bass": 0, "artists_with_genres": 0, "calibration_pairs": 0}

    with db.system() as cur:
        cur.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (email,))
        u = cur.fetchone()
        if not u:
            raise SystemExit(f"No user {email}. Create the account first (python -m frequency.cli create-user).")
        user_id = u["id"]
        cal_id = import_calibration(cur, cache, model_path)
        sid_to_track = {}
        for sid, t in tracks.items():
            artists = [{"name": a, "mbid": mbids.get(a)} for a in t["artists"]]
            tid = catalog.resolve_track(cur, t["name"], artists, ids={"spotify": sid}, isrc=isrcs.get(sid),
                                        source="legacy_cache", match="exact")
            sid_to_track[sid] = tid
            stats["tracks"] += 1
            have = catalog.current_measurements(cur, [tid]).get(tid, {})
            if sid in features and "official" not in have:
                catalog.record_measurement(cur, tid, "official", "reccobeats", _traits(features[sid]),
                                           confidence="official", flags=["legacy_import"],
                                           provenance={"from": "cache/features.json"})
                stats["official"] += 1
            if sid in estimates and "clip_estimate" not in have:
                src = "deezer" if sid in deezer_m and deezer_m[sid] else "itunes" if itunes_m.get(sid) else None
                catalog.record_measurement(cur, tid, "clip_estimate", "reccobeats_analysis", _traits(estimates[sid]),
                                           clip_source=src, confidence="unvalidated", flags=["legacy_import"],
                                           provenance={"from": "cache/estimated_features.json"})
                stats["estimates"] += 1
            if bass.get(sid) and "bass" not in have:
                catalog.record_measurement(cur, tid, "bass", "librosa", bass[sid], confidence="estimated",
                                           clip_source="deezer" if deezer_m.get(sid) else "itunes" if itunes_m.get(sid) else None,
                                           flags=["legacy_import"], provenance={"from": "cache/bass.json"})
                stats["bass"] += 1
        for name, g in genres.items():
            if g:
                aid = catalog.upsert_artist(cur, name, mbids.get(name))
                catalog.set_artist_genres(cur, aid, "legacy_cache", g)
                stats["artists_with_genres"] += 1
        for sid, p in pairs.items():
            if sid in sid_to_track:
                cur.execute("""INSERT INTO calibration_pairs (track_id, clip_source, official, clip, origin)
                               VALUES (%s, %s, %s, %s, 'legacy') ON CONFLICT DO NOTHING""",
                            (sid_to_track[sid], p["source"], db.jsonb(p["official"]), db.jsonb(p["clip"])))
                stats["calibration_pairs"] += cur.rowcount

    signals: dict[str, list] = {}
    for sid, t in tracks.items():
        for s in t.get("sources", []):
            signals.setdefault(s, []).append({"track_id": sid_to_track[sid], "weight": SIGNAL_WEIGHT.get(s, 1.0)})
    with db.as_user(user_id) as cur:
        imp = userdata.start_import(cur, user_id, "legacy_cache")
        userdata.replace_signals(cur, user_id, imp, signals)
        userdata.finish_import(cur, imp, "completed", stats)
        userdata.audit(cur, user_id, "legacy_import", {**stats, "calibration_version_id": cal_id})
    stats["calibration_version_id"] = cal_id
    if verbose:
        print(f"[legacy import] {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--email", required=True)
    ap.add_argument("--cache", type=Path, default=ROOT / "cache")
    ap.add_argument("--calibration", type=Path, default=ROOT / "models" / "calibration.json")
    a = ap.parse_args()
    run(a.email, a.cache, a.calibration)


if __name__ == "__main__":
    main()
