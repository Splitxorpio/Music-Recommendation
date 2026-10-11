"""Collecting calibration pairs: songs with official numbers whose preview clip is also analysed, so the clip
bias can be measured. Costs one ReccoBeats analysis per pair, so it only runs when the operator asks
(python -m frequency.cli calibration collect).

Where songs may come from (recorded on every pair as its origin):
  public_sample    songs found while browsing Deezer's public catalogue for candidates (no one's library)
  consented_user   songs in the libraries of users who opted in (users.calibration_consent); the pair remembers
                   the contributor, so withdrawing consent or deleting the account removes it
"""
from .. import calibration, catalog, db, jobs
from . import measure

ORIGINS = ("public_sample", "consented_user")


def eligible(n: int, origins=ORIGINS) -> list[dict]:
    out = []
    if "public_sample" in origins:
        with db.system() as cur:
            cur.execute("""SELECT DISTINCT ti.track_id FROM track_identifiers ti
                           JOIN track_measurements m ON m.track_id = ti.track_id AND m.kind = 'official' AND m.superseded_at IS NULL
                           WHERE ti.source = 'deezer_candidates'
                             AND NOT EXISTS (SELECT 1 FROM calibration_pairs p WHERE p.track_id = ti.track_id)
                           ORDER BY ti.track_id LIMIT %s""", (n,))
            out += [{"track_id": r["track_id"], "origin": "public_sample", "contributed_by": None} for r in cur.fetchall()]
    if "consented_user" in origins:
        with db.for_calibration() as cur:              # sees consenting users' libraries only (policy calibration_read)
            cur.execute("""SELECT DISTINCT ON (ul.track_id) ul.track_id, ul.user_id FROM user_library ul
                           JOIN track_measurements m ON m.track_id = ul.track_id AND m.kind = 'official' AND m.superseded_at IS NULL
                           WHERE NOT EXISTS (SELECT 1 FROM calibration_pairs p WHERE p.track_id = ul.track_id)
                           ORDER BY ul.track_id, ul.user_id LIMIT %s""", (n,))
            out += [{"track_id": r["track_id"], "origin": "consented_user", "contributed_by": str(r["user_id"])} for r in cur.fetchall()]
    return out


def request(n: int = 80, origins=ORIGINS) -> dict:
    picks = eligible(n, origins)
    created = 0
    with db.system() as cur:
        for p in picks:
            _, new = jobs.enqueue(cur, "calibration_pair", f"calpair:{p['track_id']}", p, priority=150, max_attempts=3)
            created += new
    return {"eligible": len(picks), "enqueued": created,
            "by_origin": {o: sum(1 for p in picks if p["origin"] == o) for o in origins}}


def run_calibration_pair(job) -> dict:
    import enrich_audio
    import net
    p = job["payload"]
    t = p["track_id"]
    with db.system() as cur:
        off = catalog.current_measurements(cur, [t]).get(t, {}).get("official")
    if not off:
        return {"outcome": "no_official_numbers"}
    clip = measure.find_clip(t)
    if not clip:
        return {"outcome": "no_clip"}
    path, src = clip
    raw = enrich_audio.analyse_clip(path)
    if not raw:
        raise RuntimeError("ReccoBeats couldn't analyse the clip")
    net.analysed("calibration", t)
    with db.system() as cur:
        cur.execute("""INSERT INTO calibration_pairs (track_id, clip_source, official, clip, origin, contributed_by)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (track_id, clip_source) DO NOTHING""",
                    (t, src, db.jsonb({k: off["values"].get(k) for k in calibration.TRAITS}),
                     db.jsonb({k: raw.get(k) for k in calibration.TRAITS}), p["origin"], p.get("contributed_by")))
    measure.evict_clip(t, path)
    return {"outcome": "paired", "clip_source": src}
