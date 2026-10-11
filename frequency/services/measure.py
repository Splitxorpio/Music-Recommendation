"""Measuring tracks, shared by every user: official numbers, preview clips, bass, clip analysis.

  official(track_ids)       ReccoBeats lookups (40 ids per request, ~1/8 analysis unit each), answers cached
                            for everyone: found 30 days, not found 1 day (estimates upgrade when ReccoBeats adds songs)
  request_measurements()    enqueues one global 'measure_track' job per track still missing something. The
                            idempotency key is the track, so two users needing the same song share one job
                            (and one ReccoBeats analysis); each user's run records that it waits on it.
  run_measure_track(job)    the job: find a clip (Deezer by id/ISRC, else an exact-enough iTunes match),
                            store it, measure bass, analyse it if there are no official numbers, apply the
                            active calibration, then delete the clip (FREQUENCY_KEEP_CLIPS=1 keeps it).
                            Idempotent: re-running a finished track does nothing.

Single-process helpers are reused from enrich_audio (clip download, bass, analysis call, iTunes matching).
"""
import hashlib
import time
from pathlib import Path

from .. import calibration, catalog, db, jobs, provider_cache, settings

RECCO = "https://api.reccobeats.com/v1"
OFFICIAL_FOUND_S, OFFICIAL_MISSING_S = 30 * 86400, 86400
NO_CLIP_MEMO_S = 7 * 86400
FEATURE_KEYS = ["acousticness", "danceability", "energy", "instrumentalness", "liveness", "loudness",
                "speechiness", "valence", "tempo", "key", "mode"]


def _lookup_key(ids: dict) -> str | None:
    if ids.get("spotify"):
        return f"sp:{ids['spotify']}"
    if ids.get("isrc"):
        return f"isrc:{ids['isrc']}"
    return None


def official(track_ids, progress=lambda n, total: None) -> dict[int, dict]:
    """Official ReccoBeats values for the tracks that have them ({track_id: values}); stores new ones."""
    import net
    track_ids = list(track_ids)
    with db.system() as cur:
        ids = catalog.identifiers(cur, track_ids)
        have = catalog.current_measurements(cur, track_ids)
    keys = {t: _lookup_key(ids.get(t, {})) for t in track_ids}
    answers, todo = {}, []
    for t, k in keys.items():
        if not k:
            continue
        rec = provider_cache.get("reccobeats_features", k)
        if rec and time.time() - rec["t"] < (OFFICIAL_FOUND_S if rec["v"] else OFFICIAL_MISSING_S):
            answers[k] = rec["v"]
        else:
            todo.append(k)
    todo = sorted(set(todo))
    for n in range(0, len(todo), 40):
        chunk = todo[n:n + 40]
        r = net.get("reccobeats", f"{RECCO}/audio-features", params={"ids": ",".join(k.split(":", 1)[1] for k in chunk)})
        if r.ok:
            got = {}
            for f in r.json().get("content", []):
                sid = (f.get("href") or "").rstrip("/").split("/")[-1]
                if sid:
                    got[f"sp:{sid}"] = f
                if f.get("isrc"):
                    got[f"isrc:{f['isrc']}"] = f
            for k in chunk:
                answers[k] = got.get(k)
                provider_cache.put("reccobeats_features", k, got.get(k),
                                   OFFICIAL_FOUND_S if k in got else OFFICIAL_MISSING_S, 200 if k in got else 404)
        progress(min(n + 40, len(todo)), len(todo))
    out = {}
    with db.system() as cur:
        for t, k in keys.items():
            f = answers.get(k) if k else None
            if not f:
                if "official" in have.get(t, {}):
                    out[t] = have[t]["official"]["values"]
                continue
            values = {x: f[x] for x in FEATURE_KEYS if f.get(x) is not None}
            cur_m = have.get(t, {}).get("official")
            if not cur_m or cur_m["values"] != values:
                catalog.record_measurement(cur, t, "official", "reccobeats", values, confidence="official",
                                           provenance={"reccobeats_id": f.get("id"), "href": f.get("href")})
            sid = (f.get("href") or "").rstrip("/").split("/")[-1]
            for prov, ext in (("reccobeats", f.get("id")), ("spotify", sid if "open.spotify.com/track/" in (f.get("href") or "") else None)):
                if ext:
                    cur.execute("INSERT INTO track_identifiers (provider, external_id, track_id, source) "
                                "VALUES (%s, %s, %s, 'reccobeats_features') ON CONFLICT DO NOTHING", (prov, ext, t))
            out[t] = values
    return out


def missing(track_ids) -> list[int]:
    """Tracks that still lack bass, or lack both official numbers and a clip estimate, and haven't recently
    been found to have no clip anywhere."""
    track_ids = list(track_ids)
    with db.system() as cur:
        m = catalog.current_measurements(cur, track_ids)
    out = []
    for t in track_ids:
        have = m.get(t, {})
        need_est = "official" not in have and "clip_estimate" not in have
        if (need_est or "bass" not in have) and not provider_cache.fresh("measure_outcome", str(t), NO_CLIP_MEMO_S):
            out.append(t)
    return out


def request_measurements(track_ids, for_job: int | None, priority: int = 100, tenant=None) -> dict:
    """Enqueue shared measure jobs for the tracks that need them. Returns counts. `tenant` is the user the work
    is for, so the queue's round-robin keeps one user's big library from starving another's small one."""
    need = missing(track_ids)
    created = shared = 0
    with db.system() as cur:
        for t in need:
            _, new = jobs.enqueue(cur, "measure_track", f"measure:{t}", {"track_id": t}, priority=priority,
                                  max_attempts=4, depends_for=for_job, tenant=tenant)
            created += new
            shared += not new
    return {"needed": len(need), "enqueued": created, "already_queued": shared}


# ─── the measure_track job ───

def _clip_path(track_id: int, fmt: str) -> Path:
    root = settings.get().clip_store
    sub = root / f"{track_id % 256:02x}"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{track_id}.{fmt}"


def _store_asset(cur, track_id, provider, ref, match, fmt, path: Path):
    data = path.read_bytes()
    cur.execute("""INSERT INTO preview_assets (track_id, provider, provider_ref, match, format, storage_key, sha256, bytes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (track_id, provider) DO UPDATE SET provider_ref = EXCLUDED.provider_ref, match = EXCLUDED.match,
                       format = EXCLUDED.format, storage_key = EXCLUDED.storage_key, sha256 = EXCLUDED.sha256,
                       bytes = EXCLUDED.bytes, fetched_at = now()""",
                (track_id, provider, str(ref), match, fmt, str(path.relative_to(settings.get().clip_store)),
                 hashlib.sha256(data).hexdigest(), len(data)))


def find_clip(track_id: int) -> tuple[Path, str] | None:
    """(clip path, clip source) — reusing a stored clip, else Deezer by id or ISRC, else iTunes."""
    import enrich_audio
    import deezer
    with db.system() as cur:
        cur.execute("SELECT provider, storage_key FROM preview_assets WHERE track_id = %s AND storage_key IS NOT NULL "
                    "ORDER BY provider = 'deezer' DESC", (track_id,))
        for a in cur.fetchall():
            p = settings.get().clip_store / a["storage_key"]
            if p.exists() and p.stat().st_size > 0:
                return p, a["provider"]
        ids = catalog.identifiers(cur, [track_id]).get(track_id, {})
        info = catalog.track_info(cur, [track_id])[track_id]
    d, match = None, None
    if ids.get("deezer"):
        d, match = deezer.track_with_preview(f"track/{ids['deezer']}"), "isrc" if ids.get("isrc") else "fuzzy"
    if not (d and d.get("preview")) and ids.get("isrc"):
        d, match = deezer.track_with_preview(f"track/isrc:{ids['isrc']}"), "isrc"
    if d and d.get("preview"):
        path = enrich_audio.fetch_clip(_clip_path(track_id, "mp3"), d["preview"], "deezer_cdn")
        with db.system() as cur:
            _store_asset(cur, track_id, "deezer", d["id"], match, "mp3", path)
            cur.execute("INSERT INTO track_identifiers (provider, external_id, track_id, source) "
                        "VALUES ('deezer', %s, %s, 'deezer_isrc') ON CONFLICT DO NOTHING", (str(d["id"]), track_id))
            cover = (d.get("album") or {}).get("cover_xl") or (d.get("album") or {}).get("cover_big")
            if cover:
                cur.execute("UPDATE tracks SET cover_url = coalesce(cover_url, %s) WHERE id = %s", (cover, track_id))
        return path, "deezer"
    if not info["artists"]:
        return None
    best = enrich_audio.best_match({"name": info["title"], "artists": info["artists"]})
    if best and best["score"] >= enrich_audio.MATCH_MIN:   # same threshold as the single-user pipeline
        path = enrich_audio.fetch_clip(_clip_path(track_id, "m4a"), best["preview_url"], "itunes_cdn")
        with db.system() as cur:
            _store_asset(cur, track_id, "itunes", best["preview_url"].split("?")[0], "fuzzy", "m4a", path)
        return path, "itunes"
    return None


def run_measure_track(job) -> dict:
    import enrich_audio
    import net
    t = job["payload"]["track_id"]
    with db.system() as cur:
        have = catalog.current_measurements(cur, [t]).get(t, {})
    need_est = "official" not in have and "clip_estimate" not in have
    need_bass = "bass" not in have
    if not (need_est or need_bass):
        return {"outcome": "already_measured"}
    clip = find_clip(t)
    if not clip:
        provider_cache.put("measure_outcome", str(t), {"outcome": "no_clip"}, NO_CLIP_MEMO_S)
        return {"outcome": "no_clip"}
    path, src = clip
    out = {"outcome": "measured", "clip_source": src}
    if need_bass:
        bass = enrich_audio.bass_from_clip(path)
        with db.system() as cur:
            catalog.record_measurement(cur, t, "bass", "librosa", bass, confidence="estimated", clip_source=src,
                                       provenance={"clip": path.name, "method": "enrich_audio.bass_measures"})
        out["bass"] = True
    if need_est:
        raw = enrich_audio.analyse_clip(path)
        if not raw:
            raise RuntimeError("ReccoBeats couldn't analyse the clip")
        net.analysed("shared", t)
        values = {x: raw[x] for x in FEATURE_KEYS if raw.get(x) is not None}
        with db.system() as cur:
            cal = calibration.active(cur)
            calibrated, flags, conf = calibration.apply(values, src, cal)
            catalog.record_measurement(cur, t, "clip_estimate", "reccobeats_analysis", values, clip_source=src,
                                       calibrated=calibrated, calibration_version_id=cal and cal["id"],
                                       confidence=conf, flags=flags, provenance={"clip": path.name})
        out["estimate"] = conf
    evict_clip(t, path)
    return out


def evict_clip(track_id: int, path: Path):
    """Measured clips aren't needed again (calibration re-fetches the few it uses), so they don't pile up."""
    if settings.get().keep_clips:
        return
    path.unlink(missing_ok=True)
    with db.system() as cur:
        cur.execute("UPDATE preview_assets SET storage_key = NULL WHERE track_id = %s", (track_id,))
