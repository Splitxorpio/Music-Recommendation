"""Run the whole analysis, start to finish, reporting progress for the dashboard's loading screen.

  python pipeline.py            resume / first run (every step skips work already in cache/)
  python pipeline.py --refresh  re-pull your Spotify listening and dig fresh candidates

Order (stages that use different services overlap):

  pull + official notes ──┬── clips + bass (Deezer, iTunes, ReccoBeats) ── calibration (ReccoBeats) ──────┐
                          └── genres (MusicBrainz) ── dig ahead: new-song Deezer requests into cache ──────┴── fingerprint ── new songs ── artwork

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


def report(wall, mode):
    m = net.write_metrics({"wall_s": round(wall, 1), "mode": mode, "stages_s": P.stage_times()})
    print(f"\n== Run report ({mode}): {wall / 60:.1f} min wall clock ==")
    for k, v in m["stages_s"].items():
        print(f"  {k:12} {v:7.1f} s")
    print(f"  {'service':12} {'requests':>8} {'retries':>7} {'429s':>5} {'errors':>6} {'p50 ms':>7} {'p95 ms':>7}")
    for k, v in sorted(m["services"].items(), key=lambda x: -x[1]["requests"]):
        print(f"  {k:12} {v['requests']:8} {v['retries']:7} {v['rate_limited']:5} {v['errors']:6} {v['p50_ms'] or 0:7} {v['p95_ms'] or 0:7}")
    for k, v in m["caches"].items():
        print(f"  cache {k:16} {v['hits']} hits / {v['misses']} misses ({v['hit_rate']:.0%})")


def audio_then_calibration():
    import enrich_audio
    enrich_audio.main()                      # A4 previews, A5 low end
    import calibrate
    calibrate.main()                         # B1 tune the instruments


def genres_then_dig_ahead():
    import artist_genres
    artist_genres.main()                     # B2 ask the clerks
    import candidates
    candidates.prefetch()                    # B4's Deezer requests, while ReccoBeats is the bottleneck


def main():
    refresh = "--refresh" in sys.argv
    started = time.time()
    if sys.platform == "darwin":
        # Keep the Mac from idle-sleeping until this run ends (a closed lid on battery still sleeps).
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    if refresh:
        for name in ("candidates.json", "profile.json"):
            (CACHE / name).unlink(missing_ok=True)
    P.begin_run()
    mode = "refresh" if refresh else "resume/first run"
    try:
        if refresh or not ((CACHE / "tracks.json").exists() and (CACHE / "features.json").exists()):
            import check_coverage
            check_coverage.main()            # A1 login, A2 pull, A3 liner notes
        else:                                # resuming: your songs are already on the shelf
            for k in ("login", "pull", "notes"):
                P.stage(k)
                P.finish(k)
            P.fact("Picking up where we left off. Your records are already pulled.")

        with ThreadPoolExecutor(max_workers=2) as side_by_side:
            jobs = [side_by_side.submit(audio_then_calibration), side_by_side.submit(genres_then_dig_ahead)]
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
        report(time.time() - started, mode)
        sys.exit(1)
    except Exception as ex:
        traceback.print_exc()
        stage = P.read().get("stage") or "the run"
        P.end_run(error=f"Stopped during {stage}: {type(ex).__name__}: {str(ex)[:200]}")
        report(time.time() - started, mode)
        sys.exit(1)
    P.end_run()
    report(time.time() - started, mode)


if __name__ == "__main__":
    main()
