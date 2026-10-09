"""Run the whole analysis, start to finish, reporting progress for the dashboard's loading screen.

  python pipeline.py            resume / first run (every step skips work already in cache/)
  python pipeline.py --refresh  re-pull your Spotify listening and dig fresh candidates

Order (stages that use different services overlap):

  pull + official notes ──┬── clips + bass (Deezer, iTunes, ReccoBeats) ── calibration (reused) ───────┐
                          ├── genres (MusicBrainz) ── seed relationships (MusicBrainz) ─────────────────┼── fingerprint ── new songs ── artwork
                          └── dig ahead: new-song Deezer answers + official lookups into cache ─────────┘

Writes cache/metrics.json: per-stage times, and per-service request counts, retries, rate-limit hits,
latency percentiles and cache hit rates.
The dashboard starts this in the background; it's fine to run it by hand too.
"""
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import net
import progress as P
from config import CACHE
from spotify_auth import RateLimited


def awake_clock():
    """Seconds of awake time: on macOS CLOCK_UPTIME_RAW stops while the machine sleeps."""
    return time.clock_gettime(time.CLOCK_UPTIME_RAW) if hasattr(time, "CLOCK_UPTIME_RAW") else time.monotonic()


def report(run):
    import deezer
    wall, awake = time.time() - run["started"], awake_clock() - run["awake0"]
    m = net.metrics()
    up = m["services"].get("reccobeats_upload", {})
    meta = {
        "run_id": run["id"], "mode": run["mode"], "cache_at_start": run["cache_at_start"],
        "wall_s": round(wall, 1), "awake_s": round(awake, 1), "asleep_s": round(max(0, wall - awake), 1),
        "stages_s": P.stage_times(),
        "stages_skipped": [k for k in ("login", "pull", "notes") if run["mode"].startswith("resume") and run["cache_at_start"] != "empty"],
        "analyses": {"sent": up.get("requests", 0), "succeeded": up.get("ok", 0), "rejected_429": up.get("rate_limited", 0),
                     "calibration_reused": m["caches"].get("calibration_reused", {}).get("hits", 0),
                     "new_song_measurements_reused": m["caches"].get("song_measurements", {}).get("hits", 0)},
        "deezer_store": deezer.freshness(),
        "analysis_lane": {**net.lane_gaps("reccobeats_upload"),
                          "first_start_after_s": round(net.lane_gaps("reccobeats_upload").get("first_start", run["started"]) - run["started"], 1)},
        "status": P.read().get("status"),
    }
    out = net.write_metrics(meta)
    meta["complete"] = not inventory()                # False if any library song is still missing its analysis
    net.write_metrics(meta)
    print(f"\n== Run {run['id']} ({run['mode']}, cache at start: {run['cache_at_start']}): "
          f"{wall / 60:.1f} min wall, {awake / 60:.1f} min awake ==")
    for k, v in meta["stages_s"].items():
        print(f"  {k:12} {v:7.1f} s{'  (skipped: already done)' if k in meta['stages_skipped'] else ''}")
    print(f"  analyses: {meta['analyses']}")
    lane = meta["analysis_lane"]
    if lane.get("starts"):
        print(f"  analysis lane: first analysis {lane['first_start_after_s']} s in, busy span {lane['span_s']} s, "
              f"idle gaps (start s, length s): {lane['idle_gaps']}")
    print(f"  {'service':18} {'requests':>8} {'ok':>6} {'retries':>7} {'429s':>5} {'p50 ms':>7} {'p95 ms':>7}  outcomes")
    for k, v in sorted(out["services"].items(), key=lambda x: -x[1]["requests"]):
        print(f"  {k:18} {v['requests']:8} {v['ok']:6} {v['retries']:7} {v['rate_limited']:5} {v['p50_ms'] or 0:7} {v['p95_ms'] or 0:7}  {v['outcomes']}")
        for op, o in sorted(v["ops"].items(), key=lambda x: -x[1]["requests"]):
            print(f"      {op:32} {o['requests']:6}  {o['status']}")
    for k, v in out["caches"].items():
        print(f"  cache {k:30} {v['hits']} hits / {v['misses']} misses ({v['hit_rate']:.0%})")
    print(f"  deezer store at end: {meta['deezer_store']}")


def inventory():
    """Per-song record of why each song did or didn't need a ReccoBeats analysis (cache/analysis_inventory.json)."""
    import json as _j
    load = lambda n, d: _j.loads((CACHE / n).read_text()) if (CACHE / n).exists() else d
    tracks, features, est = load("tracks.json", {}), load("features.json", {}), load("estimated_features.json", {})
    cands, measured = load("candidates.json", {}), load("song_measurements.json", {})
    now = {k: set(v) for k, v in net.ANALYSED.items()}
    clips = {p.stem for p in (CACHE / "previews").glob("*") if p.suffix in (".mp3", ".m4a")}
    lib = {}
    for i in tracks:
        lib[i] = ("official numbers exist, no analysis needed" if i in features else
                  "analysed this run (no official numbers)" if i in now.get("library", ()) else
                  "analysed on an earlier run, reused" if i in est else
                  "ANALYSIS FAILED (clip on disk; retried next run)" if i in clips else
                  "no clip anywhere, cannot be analysed")
    new = {}
    for cid, c in cands.items():
        if not c.get("measured"):
            new[cid] = "not chosen for measuring this run"
        elif c.get("feature_source") == "official":
            new[cid] = "official numbers exist, no analysis needed"
        elif cid in now.get("new_songs", ()):
            new[cid] = "analysed this run (chosen, no official numbers)"
        elif (measured.get(cid) or {}).get("est"):
            new[cid] = "analysed on an earlier run, reused"
        else:
            new[cid] = "no preview clip, cannot be analysed"
    summary = {"library": {}, "new_songs": {}}
    for part, d in (("library", lib), ("new_songs", new)):
        for why in d.values():
            summary[part][why] = summary[part].get(why, 0) + 1
    (CACHE / "analysis_inventory.json").write_text(_j.dumps({"summary": summary, "library": lib, "new_songs": new}, indent=1))
    print(f"  analysis inventory: {summary}")
    failed = summary["library"].get("ANALYSIS FAILED (clip on disk; retried next run)", 0)
    if failed:
        print(f"  !! {failed} library songs are missing their analysis: this run is INCOMPLETE")
        P.fact(f"{failed} songs couldn't be measured this time; the next run will retry them.", key="incomplete")
    return failed


def audio_then_calibration():
    import enrich_audio
    enrich_audio.main()                      # A4 previews, A5 low end
    import calibrate
    calibrate.main()                         # B1 tune the instruments


def genres_then_relations():
    import artist_genres
    artist_genres.main()                     # B2 ask the clerks (MusicBrainz)
    import candidates
    candidates.warm_relations()              # B4's MusicBrainz relationships, as soon as identities exist
    candidates.prefetch(use_relations=True)  # ...and the Deezer/official work for the collaborators they add


def dig_ahead():
    import candidates
    candidates.prefetch()                    # B4's Deezer requests, from the start (Deezer is on the critical path)


def main():
    refresh = "--refresh" in sys.argv
    if "--recalibrate" in sys.argv:
        os.environ["FREQUENCY_RECALIBRATE"] = "1"
    started = time.time()
    run = {"id": time.strftime("%Y%m%d-%H%M%S"), "started": started, "awake0": awake_clock(),
           "cache_at_start": "empty" if not (CACHE / "tracks.json").exists() else
                             ("has candidates" if (CACHE / "candidates.json").exists() else "has library")}
    if sys.platform == "darwin":
        # Keep the Mac from idle-sleeping until this run ends (a closed lid on battery still sleeps).
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    if refresh:
        for name in ("candidates.json", "profile.json"):
            (CACHE / name).unlink(missing_ok=True)
    P.begin_run()
    mode = "refresh" if refresh else "resume/first run"
    run["mode"] = mode
    try:
        if refresh or not ((CACHE / "tracks.json").exists() and (CACHE / "features.json").exists()):
            import check_coverage
            check_coverage.main()            # A1 login, A2 pull, A3 liner notes
        else:                                # resuming: your songs are already on the shelf
            for k in ("login", "pull", "notes"):
                P.stage(k)
                P.finish(k)
            P.fact("Picking up where we left off. Your records are already pulled.")

        with ThreadPoolExecutor(max_workers=3) as side_by_side:
            jobs = [side_by_side.submit(audio_then_calibration), side_by_side.submit(genres_then_relations),
                    side_by_side.submit(dig_ahead)]
            for j in jobs:
                j.result()                   # re-raises anything that went wrong in either branch

        import taste_profile
        taste_profile.main()                 # B3 press the fingerprint
        import candidates
        candidates.main()                    # B4 dig the crates
        import artwork
        artwork.main()                       # B5 print the sleeves
    except RateLimited as ex:
        traceback.print_exc()
        P.end_run(error=f"{ex} Everything so far is saved. Resume after that and it carries on from here.")
        report(run)
        sys.exit(1)
    except Exception as ex:
        traceback.print_exc()
        stage = P.read().get("stage") or "the run"
        P.end_run(error=f"Stopped during {stage}: {type(ex).__name__}: {str(ex)[:200]}")
        report(run)
        sys.exit(1)
    P.end_run()
    report(run)


if __name__ == "__main__":
    main()
