"""One user's analysis run as a resumable state machine (job kind 'user_pipeline').

The worker runs each step in a child process dedicated to that one job, with FREQUENCY_CACHE pointing at the
job's own work folder, so the single-user modules' in-memory state (candidates, deezer, progress) can never
mix two users. Shared facts go to PostgreSQL; the work folder only holds this run's progress display and
candidate pool, and is deleted when the run ends.

  import ─▶ measure_library ─▶ genres ─▶ profile ─▶ gather ─▶ measure_candidates ─▶ finalize
                  ▲ waits                                           ▲ waits
  "waits" = the stage enqueued shared measure_track jobs and gives its turn back (job.wait) until they're done,
  so no worker sits idle holding a lease while ReccoBeats paces analyses. Every stage is idempotent: a run that
  crashed or was killed resumes at the stage it was in and skips work already stored.
"""
import json
import time
from collections import defaultdict
from pathlib import Path

from .. import calibration, catalog, db, jobs, provider_cache, userdata
from ..sources import SourceError
from . import genres, library, measure

STAGES = ["import", "measure_library", "genres", "profile", "gather", "measure_candidates", "finalize"]
WAIT_S = 8
MIN_TRACKS = 20                                  # fewer measurable songs than this can't form sub-tastes
INTERACTIVE_PRIORITY, CANDIDATE_PRIORITY = 50, 60


class CacheDict(dict):
    """A dict backed by provider_cache, loaded key by key (what candidates.gather() reads and writes)."""

    def __init__(self, provider, ttl_s):
        super().__init__()
        self.provider, self.ttl, self.seen = provider, ttl_s, set()

    def _load(self, k):
        if k not in self.seen:
            self.seen.add(k)
            rec = provider_cache.fresh(self.provider, str(k), self.ttl)
            if rec:
                dict.__setitem__(self, k, rec["v"])

    def __contains__(self, k):
        self._load(k)
        return dict.__contains__(self, k)

    def __getitem__(self, k):
        self._load(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._load(k)
        return dict.get(self, k, default)

    def __setitem__(self, k, v):
        dict.__setitem__(self, k, v)
        self.seen.add(k)
        provider_cache.put(self.provider, str(k), v, self.ttl)


def build_enriched(user_id):
    """The user's library in the single-user 'enriched' shape, keyed by a provider id the recommendation code
    can use as a ReccoBeats seed (Spotify id, else ReccoBeats id, else t<track id>)."""
    with db.as_user(user_id) as cur:
        lib = userdata.library(cur)
    tids = list(lib)
    with db.system() as cur:
        info = catalog.track_info(cur, tids)
        m = catalog.current_measurements(cur, tids)
        ids = catalog.identifiers(cur, tids)
        cur.execute("SELECT id, cover_url FROM tracks WHERE id = ANY(%s)", (tids,))
        covers = {r["id"]: r["cover_url"] for r in cur.fetchall()}
    enriched, features, isrc_map, keys = {}, {}, {}, {}
    for t in tids:
        feats, conf = catalog.features_for(m.get(t, {}))
        i = ids.get(t, {})
        k = i.get("spotify") or i.get("reccobeats") or f"t{t}"
        keys[k] = t
        enriched[k] = {"name": info[t]["title"], "artists": info[t]["artists"] or ["?"], "sources": sorted(lib[t]),
                       "feature_source": "official" if conf == "official" else "estimated_from_preview" if feats else "no_data",
                       "features": feats, "confidence": conf, "bass": (m.get(t, {}).get("bass") or {}).get("values"),
                       "track_id": t, "cover": covers.get(t), "isrc": i.get("isrc"),
                       "spotify_url": f"https://open.spotify.com/track/{i['spotify']}" if i.get("spotify") else None}
        if i.get("isrc"):
            isrc_map[k] = i["isrc"]
            if conf == "official":
                features[k] = {"isrc": i["isrc"]}
    return enriched, features, isrc_map, keys


def _deps_live(job_id) -> tuple[int, dict]:
    with db.system() as cur:
        d = jobs.dependencies(cur, job_id)
    return sum(d.get(s, 0) for s in jobs.LIVE), d


def _library_artist_ids(user_id):
    with db.as_user(user_id) as cur:
        tids = list(userdata.library(cur))
    with db.system() as cur:
        cur.execute("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s)", (tids,))
        return [r["artist_id"] for r in cur.fetchall()]


def run(job, workdir: Path) -> dict:
    """Run stages until the run finishes or has to wait. Returns the outcome for the parent worker:
    {"action": "finish", "status", "result"} | {"action": "wait", "seconds"} | {"action": "fail", "error", "retryable"}."""
    import progress as P
    state = dict((job["progress"] or {}).get("pipeline") or {"stage": "import"})
    user_id, payload = job["user_id"], job["payload"]
    if not state.get("begun"):
        P.begin_run()
        state["begun"] = time.time()
    out = {"state": state}
    try:
        while state["stage"] != "done":
            step = globals()["stage_" + state["stage"]]
            res = step(job, user_id, payload, state, workdir, P)
            if res:                                    # wait
                return {**out, "action": "wait", "seconds": res}
            state["stage"] = STAGES[STAGES.index(state["stage"]) + 1] if state["stage"] != STAGES[-1] else "done"
        P.end_run()
        return {**out, "action": "finish", "status": state.get("final_status", "completed"), "result": state.get("result", {})}
    except SourceError as ex:
        if ex.retry_after:
            P.fact(str(ex), key="wait")
            return {**out, "action": "wait", "seconds": ex.retry_after}
        P.end_run(error=str(ex))
        return {**out, "action": "fail", "error": f"user: {ex}", "retryable": False}   # "user:" = safe to show as-is
    except Exception as ex:                          # noqa: BLE001 - reported on the job, retried with backoff
        if type(ex).__name__ in ("ServicePaused", "QuotaWait"):
            P.fact("A music service asked us to pause; picking up again shortly.", key="wait")
            return {**out, "action": "wait", "seconds": getattr(ex, "seconds", 60)}
        import traceback
        traceback.print_exc()
        return {**out, "action": "fail", "error": f"{type(ex).__name__} during {state['stage']}: {str(ex)[:300]}",
                "retryable": True}
    finally:
        P._write(force=True)


# ─── stages ───

def stage_import(job, user_id, payload, state, workdir, P):
    for k in ("login", "pull"):
        P.stage(k)
    P.finish("login")
    if payload.get("source"):
        stats = library.import_source(user_id, payload["source"], payload, progress=lambda m: P.tick(0, now=m))
        state["import"] = {k: v for k, v in stats.items() if k in ("songs", "resolution", "signals", "plays")}
        P.fact(f"{stats['songs']} of your songs are on the shelf", key="pulled")
    P.finish("pull")


def stage_measure_library(job, user_id, payload, state, workdir, P):
    with db.as_user(user_id) as cur:
        tids = list(userdata.library(cur))
    if not state.get("library_requested"):
        P.stage("notes", total=len(tids), label="songs looked up")
        off = measure.official(tids, progress=lambda n, t: P.tick(n, t))
        P.fact(f"Official notes for {len(off)} of {len(tids)} songs", key="notes")
        P.finish("notes")
        state["library_requested"] = measure.request_measurements(tids, for_job=job["id"], priority=INTERACTIVE_PRIORITY,
                                                                  tenant=user_id)
    live, d = _deps_live(job["id"])
    P.stage("previews")
    P.stage("lowend", total=sum(d.values()), label="songs measured")
    P.tick(d.get("completed", 0) + d.get("failed", 0), sum(d.values()) or None)
    if live:
        return WAIT_S
    P.finish("previews")
    P.finish("lowend")


def stage_genres(job, user_id, payload, state, workdir, P):
    P.stage("genres", label="artists described")
    state["genres"] = genres.ensure(_library_artist_ids(user_id))
    P.finish("genres")


def stage_profile(job, user_id, payload, state, workdir, P):
    import taste_profile
    P.stage("calibrate")
    with db.system() as cur:
        cal = calibration.active(cur)
    P.fact(f"Clip estimates corrected with calibration {cal['version']}" if cal else
           "No validated calibration yet: clip estimates are used as measured and labelled unvalidated.", key="calibration")
    P.finish("calibrate")
    P.stage("fingerprint")
    enriched, _, _, keys = build_enriched(user_id)
    usable = sum(1 for t in enriched.values() if t["features"])
    if usable < MIN_TRACKS:
        raise SourceError(f"Only {usable} of your songs could be measured; at least {MIN_TRACKS} are needed to find "
                          f"your sub-tastes. Add more listening (or another source) and try again.")
    with db.system() as cur:
        g = genres.by_name(cur, _library_artist_ids(user_id))
    profile = taste_profile.build_profile(enriched, g, calibration={})
    profile["keys"] = keys
    with db.as_user(user_id) as cur:
        state["profile_id"] = str(userdata.save_profile(cur, user_id, profile, cal and cal["id"]))
    P.fact(f"You have {len(profile['clusters'])} worlds: " + "; ".join(c["name"] for c in profile["clusters"]))
    P.finish("fingerprint")


def _pool_path(workdir):
    return workdir / "candidates.json"


def stage_gather(job, user_id, payload, state, workdir, P):
    import candidates
    enriched, features, isrc_map, _ = build_enriched(user_id)
    with db.as_user(user_id) as cur:
        profile = userdata.current_profile(cur)["profile"]
    with db.system() as cur:
        cur.execute("SELECT DISTINCT a.name, a.mbid::text AS mbid FROM artists a JOIN track_artists ta ON ta.artist_id = a.id "
                    "WHERE ta.track_id = ANY(%s) AND a.mbid IS NOT NULL", ([t["track_id"] for t in enriched.values()],))
        mbids = {r["name"]: r["mbid"] for r in cur.fetchall()}
    P.stage("crates", label="artists followed")
    cands = candidates.gather(enriched, features, isrc_map, profile, mbids=mbids,
                              rel_cache=CacheDict("mb_relations", 30 * 86400),
                              recco_cache=CacheDict("recco_suggestions", 86400))
    import deezer
    with db.system() as cur:
        for c in cands.values():
            artists = [{"name": c["artist"], "ids": {"deezer": c.get("artist_dz")}}] + \
                      [n for n in c.get("artists", []) if catalog.key(n) != catalog.key(c["artist"])]
            c["track_id"] = catalog.resolve_track(cur, c["title"], artists, ids={"deezer": c["deezer_id"]}, isrc=c["isrc"],
                                                  duration_ms=(c.get("duration") or 0) * 1000 or None, source="deezer_candidates")
            d = deezer.get(f"track/{c['deezer_id']}") or {}           # already in the shared store (gather asked)
            cover = (d.get("album") or {}).get("cover_xl") or (d.get("album") or {}).get("cover_big")
            if cover:
                cur.execute("UPDATE tracks SET cover_url = coalesce(cover_url, %s) WHERE id = %s", (cover, c["track_id"]))
    _pool_path(workdir).write_text(json.dumps(cands))
    state["gathered"] = len(cands)


def _attach_measurements(cands):
    tids = [c["track_id"] for c in cands.values()]
    with db.system() as cur:
        m = catalog.current_measurements(cur, tids)
    for c in cands.values():
        feats, conf = catalog.features_for(m.get(c["track_id"], {}))
        c["features"] = feats
        c["feature_source"] = "official" if conf == "official" else "estimated_from_preview" if feats else "no_data"
        c["confidence"] = conf
        b = (m.get(c["track_id"], {}).get("bass") or {}).get("values")
        if b:
            c["bass"] = b


def stage_measure_candidates(job, user_id, payload, state, workdir, P):
    import candidates
    from recommend import score_all, total
    cands = json.loads(_pool_path(workdir).read_text())
    if not state.get("candidates_requested"):
        measure.official([c["track_id"] for c in cands.values()], progress=lambda n, t: P.tick(n, t, label="official notes"))
        _attach_measurements(cands)
        with db.as_user(user_id) as cur:
            profile = userdata.current_profile(cur)["profile"]
        with db.system() as cur:
            cur.execute("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s)", ([c["track_id"] for c in cands.values()],))
            g = genres.by_name(cur, [r["artist_id"] for r in cur.fetchall()] + _library_artist_ids(user_id))
        scored = {c["id"]: c for c in score_all(cands, profile, g, calibration={})}

        def pre(c):                                   # same pre-score as candidates.stage2
            p = dict(scored[c["id"]]["parts"])
            if p["sound"] is None:
                p["sound"] = 50
            return total(p, {"sound": 0.5, "connection": 0.5})
        buckets = defaultdict(list)
        for c in cands.values():
            buckets["known" if c["mode"] == "known" else f"new-hop{c['hop']}"].append(c)
        for items in buckets.values():
            items.sort(key=lambda c: -pre(c))
            for c in items[:candidates.STAGE2_PER_BUCKET]:
                c["chosen"] = True
        chosen = [c for c in cands.values() if c.get("chosen")]
        _pool_path(workdir).write_text(json.dumps(cands))
        state["candidates_requested"] = measure.request_measurements(
            [c["track_id"] for c in chosen if c.get("has_preview")], for_job=job["id"], priority=CANDIDATE_PRIORITY,
            tenant=user_id)
        state["chosen"] = len(chosen)
    live, d = _deps_live(job["id"])
    P.tick(d.get("completed", 0) + d.get("failed", 0), sum(d.values()) or None, label="new songs auditioned")
    if live:
        return WAIT_S


def stage_finalize(job, user_id, payload, state, workdir, P):
    import spotify_links
    cands = json.loads(_pool_path(workdir).read_text())
    _attach_measurements(cands)
    chosen = {k: c for k, c in cands.items() if c.get("chosen")}
    with db.system() as cur:
        cur.execute("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s)", ([c["track_id"] for c in chosen.values()],))
        cand_artists = [r["artist_id"] for r in cur.fetchall()]
    genres.ensure(cand_artists)
    with db.system() as cur:
        ids = catalog.identifiers(cur, [c["track_id"] for c in cands.values()])
    for c in cands.values():
        c["measured"] = bool(c.get("chosen"))
        sid = ids.get(c["track_id"], {}).get("spotify")
        c["spotify_exact"] = bool(sid)
        c["spotify_url"] = f"https://open.spotify.com/track/{sid}" if sid else spotify_links.search_link(c["title"], c["artist"])
    _artist_photos(user_id)
    _, d = _deps_live(job["id"])
    failed = d.get("failed", 0)
    status = "partially_completed" if failed else "completed"
    with db.as_user(user_id) as cur:
        prof = userdata.current_profile(cur)
        run_id = userdata.create_run(cur, user_id, prof["id"], {"source": payload.get("source")})
        stats = {"candidates": len(cands), "measured": len(chosen), "measure_jobs": d, **{k: state.get(k) for k in ("import", "genres")}}
        userdata.save_results(cur, user_id, run_id, {c["track_id"]: c for c in cands.values()}, status, stats)
    state.update(final_status=status, result={"run_id": str(run_id), "candidates": len(cands), "measured": len(chosen),
                                              "failed_measurements": failed})
    P.finish("crates")
    P.stage("sleeves")
    P.finish("sleeves")


def _artist_photos(user_id):
    """Deezer photos for the artists the profile page shows (cached Deezer answers; stored on artists)."""
    import deezer
    with db.as_user(user_id) as cur:
        p = userdata.current_profile(cur)["profile"]
    names = {a for a, _ in p["overall"]["top_artists"]} | {a for c in p["clusters"] for a, _ in c["top_artists"][:6]}
    with db.system() as cur:
        cur.execute("SELECT id, name FROM artists WHERE name = ANY(%s) AND picture_url IS NULL", (sorted(names),))
        rows = cur.fetchall()
    for r in rows:
        hits = [a for a in (deezer.get("search/artist", q=r["name"]) or {}).get("data", [])
                if catalog.key(a.get("name", "")) == catalog.key(r["name"])]
        if hits:
            best = max(hits, key=lambda a: a.get("nb_fan", 0))
            with db.system() as cur:
                cur.execute("UPDATE artists SET picture_url = %s WHERE id = %s", (best.get("picture_big"), r["id"]))
