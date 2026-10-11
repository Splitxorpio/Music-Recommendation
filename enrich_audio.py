"""Fill gaps in ReccoBeats coverage using 30s preview clips, and measure bass from the audio.

Clip sources, in order: Deezer looked up by ISRC (exact match), then iTunes search (fuzzy match).
Runs concurrently through net.py: each clip is downloaded the moment its link is found (Deezer links
expire, so they're never looked up twice), and download, bass and ReccoBeats analysis for different
songs overlap, each service held to its own limits.

Reads cache/tracks.json + cache/features.json (from check_coverage.py).
Writes:
  cache/isrc.json               spotify_id -> ISRC (from Spotify, one track at a time)
  cache/deezer_matches.json     spotify_id -> {isrc, deezer title/artist} (preview URLs expire, so not stored)
  cache/itunes_matches.json     spotify_id -> {preview_url, itunes title/artist, score}
  cache/near_misses.json        weak matches for manual review (not used)
  cache/estimated_features.json spotify_id -> features analysed from the preview clip
  cache/bass.json               spotify_id -> bass measures (every matched track)
  cache/enriched.json           final merged table, each track tagged with its source
Resumable: every step skips work already in the cache files.
"""
import json
import os
import re
import subprocess
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import librosa
import numpy as np

import deezer
import net
import progress as P

from config import CACHE
PREVIEWS = CACHE / "previews"
PREVIEWS.mkdir(parents=True, exist_ok=True)
ANALYSE = "https://api.reccobeats.com/v1/analysis/audio-features"
ITUNES = "https://itunes.apple.com/search"
DEEZER_ISRC = "https://api.deezer.com/track/isrc:{}"
MATCH_MIN, NEAR_MIN = 0.85, 0.60


def load(name, default):
    p = CACHE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    """Atomic write: stages run side by side, so a reader must never see a half-written file."""
    tmp = CACHE / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, indent=1))
    if FAULT == "mid_save" and name == "estimated_features.json" and len(obj) >= 20:
        print("[fault] exiting mid-save: new file written, not yet swapped in", flush=True)
        os._exit(137)
    tmp.replace(CACHE / name)


def norm(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)            # (feat. x), (Remastered), [Explicit]
    s = re.sub(r" - .*$", "", s)                      # " - Remastered 2011"
    s = re.sub(r"\b(feat|ft|with)\b.*$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def itunes_search(params):
    """iTunes signals throttling with 403 as well as 429; treat both as 'slow down'."""
    for attempt in range(5):
        r = net.get("itunes", ITUNES, params=params)
        if r.status_code in (403, 429):
            net.hold_off("itunes", 10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("iTunes kept throttling")


def best_match(track):
    title, artists = norm(track["name"]), [norm(a) for a in track["artists"]]
    term = f'{track["artists"][0]} {re.sub(r"\(.*?\)", "", track["name"])}'
    results = itunes_search({"term": term, "entity": "song", "limit": 8}).get("results", [])
    best = None
    for c in results:
        if not c.get("previewUrl"):
            continue
        t = SequenceMatcher(None, title, norm(c["trackName"])).ratio()
        ca = norm(c["artistName"])
        a = 1.0 if any(x and (x in ca or ca in x) for x in artists) else 0.0
        score = t * (1.0 if a else 0.5)               # wrong artist halves the score
        if not best or score > best["score"]:
            best = {"score": round(score, 3), "preview_url": c["previewUrl"],
                    "itunes_title": c["trackName"], "itunes_artist": c["artistName"],
                    "art": (c.get("artworkUrl100") or "").replace("100x100bb", "600x600bb") or None}
    return best


def step_isrc(tracks, todo):
    """Recording codes (ISRCs) from data we already hold: the Spotify pull itself (full track objects
    carry external_ids.isrc), ReccoBeats' official data, and earlier runs. A per-track Spotify lookup
    would return the very same track object, so it is never made (634 of them on Oct 7 2026 earned a
    24 h block on that endpoint). Songs with no code at all fall back to the iTunes name search."""
    isrc = load("isrc.json", {})
    for i in todo:
        if not isrc.get(i):
            code = tracks[i].get("isrc") or (load_features_cached().get(i) or {}).get("isrc")
            if code:
                isrc[i] = code
    missing = [i for i in todo if not isrc.get(i)]
    if missing:
        print(f"[isrc] {len(missing)} songs have no recording code anywhere; they'll use the iTunes search")
    save("isrc.json", isrc)
    return isrc


_features = None
def load_features_cached():
    global _features
    if _features is None:
        _features = load("features.json", {})
    return _features


def deezer_lookup(code):
    """Deezer's answer for a recording code, from the shared store when its preview link is still live
    (the same answers the new-songs and artwork steps reuse)."""
    r = deezer.track_with_preview(f"track/isrc:{code}")
    return r if r and r.get("preview") else None


def clip_path(i, match):
    return PREVIEWS / f"{i}.{'mp3' if match.get('source') == 'deezer' else 'm4a'}"


def fetch_clip(path, url, service):
    """Download a clip unless it's already on disk (clips are immutable, keyed by track id)."""
    if path.exists() and path.stat().st_size > 0:
        net.cache_hit("clips")
        return path
    net.cache_hit("clips", False)
    r = net.get(service, url)
    r.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(path)
    return path


def step_deezer(tracks, itunes):
    """Exact ISRC match on Deezer for every track that doesn't already have a clip source.
    The preview link in the answer is used straight away to download the clip."""
    dz = load("deezer_matches.json", {})
    todo = [i for i in tracks if i not in itunes and i not in dz]
    for i, m in {**itunes, **dz}.items():             # resumed run: earlier matches can start measuring now
        if i in tracks:
            measure_early(i, m, load_features_cached())
    isrc = step_isrc(tracks, todo)
    todo = [i for i in todo if isrc.get(i)]
    print(f"[deezer] {len(todo)} tracks to look up by ISRC")

    # Lookups and downloads use separate workers, so a slow download never holds up the next lookup.
    downloads = ThreadPoolExecutor(max_workers=8)
    pending = []

    def work(i):
        r = deezer_lookup(isrc[i])
        if not r:
            return None
        m = {"source": "deezer", "isrc": isrc[i], "deezer_title": r.get("title"),
             "deezer_artist": r.get("artist", {}).get("name")}
        return m, (r.get("album") or {}).get("cover_big"), r["preview"]

    features = load_features_cached()

    def fetch(i, m, url):
        try:
            fetch_clip(clip_path(i, m), url, "deezer_cdn")
            measure_early(i, m, features)
        except Exception as ex:                       # the measure step retries with a fresh link
            print(f"  clip download failed for {tracks[i]['name']}: {ex}")

    def done(i, res, n):
        P.tick(n, len(todo), now=f"found {tracks[i]['name']} — {tracks[i]['artists'][0]} on Deezer", label="checked on Deezer")
        if isinstance(res, Exception):
            print(f"  deezer error for {tracks[i]['name']}: {res}")
        elif res:
            dz[i] = res[0]
            P.cover(res[1], tracks[i]["name"], tracks[i]["artists"][0])
            pending.append(downloads.submit(fetch, i, res[0], res[2]))
        if n % 100 == 0:
            save("deezer_matches.json", dz)

    net.pmap(work, todo, workers=10, on_result=done)
    for f in pending:
        f.result()
    downloads.shutdown()
    save("deezer_matches.json", dz)
    return dz


def step_match(tracks):
    matches, near = load("itunes_matches.json", {}), load("near_misses.json", {})
    todo = [i for i in tracks if i not in matches and i not in near]
    print(f"[match] {len(todo)} tracks to search ({len(matches)} already matched)")
    def work(i):
        b = best_match(tracks[i])
        if b and b["score"] >= MATCH_MIN:
            try:
                fetch_clip(clip_path(i, b), b["preview_url"], "itunes_cdn")
                measure_early(i, b, load_features_cached())
            except Exception as ex:
                print(f"  clip download failed for {tracks[i]['name']}: {ex}")
        return b

    def done(i, b, n):
        P.tick(n, len(todo), now=f"searched iTunes for {tracks[i]['name']} — {tracks[i]['artists'][0]}", label="searched on iTunes")
        if isinstance(b, Exception):
            print(f"  itunes error for {tracks[i]['name']}: {b}")
        elif b and b["score"] >= MATCH_MIN:
            matches[i] = b
            P.cover(b.get("art"), tracks[i]["name"], tracks[i]["artists"][0])
        elif b and b["score"] >= NEAR_MIN:
            near[i] = {"spotify": f'{tracks[i]["name"]} - {", ".join(tracks[i]["artists"])}', **b}
        else:
            near[i] = None                             # searched, nothing usable
        save("itunes_matches.json", matches)          # every search kept as it happens (iTunes allows ~20/min)
        save("near_misses.json", near)

    net.pmap(work, todo, workers=3, on_result=done)   # iTunes' own limiter paces the searches
    save("itunes_matches.json", matches)
    save("near_misses.json", near)
    return matches


def download(i, match):
    """Make sure the clip is on disk. Deezer links expire, so a missing Deezer clip gets a fresh one."""
    path = clip_path(i, match)
    if path.exists() and path.stat().st_size > 0:
        net.cache_hit("clips")
        return path
    if match.get("source") == "deezer":
        fresh = deezer_lookup(match["isrc"])
        if not fresh:
            raise RuntimeError("Deezer no longer offers this preview")
        return fetch_clip(path, fresh["preview"], "deezer_cdn")
    return fetch_clip(path, match["preview_url"], "itunes_cdn")


def to_wav(clip):
    wav = clip.with_suffix(".wav")
    if not wav.exists():
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", str(clip), str(wav)], check=True)
    return wav


def bass_from_clip(clip):
    """Bass straight from the clip. MP3 decodes directly; M4A (iTunes) still needs a WAV copy."""
    if clip.suffix == ".mp3":
        return bass_measures(clip)
    wav = to_wav(clip)
    try:
        return bass_measures(wav)
    finally:
        wav.unlink(missing_ok=True)


def analyse_clip(clip):
    """ReccoBeats measures the clip (upload + analysis, ~2 s). None if it couldn't."""
    mime = "audio/mpeg" if clip.suffix == ".mp3" else "audio/mp4"
    r = net.post("reccobeats_upload", ANALYSE, files={"audioFile": (clip.name, clip.read_bytes(), mime)})
    return r.json() if r.status_code == 200 else None


def bass_measures(wav):
    """Share of spectral energy in bass bands, plus how loud the bass is in dB."""
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    S = np.abs(librosa.stft(y, n_fft=4096, hop_length=2048)) ** 2
    f = librosa.fft_frequencies(sr=sr, n_fft=4096)
    total = S.sum() + 1e-12
    band = lambda lo, hi: float(S[(f >= lo) & (f < hi)].sum())
    sub, bass = band(20, 60), band(60, 250)
    return {
        "sub_bass_share": round(float(sub / total), 4),        # 20-60 Hz
        "bass_share": round(float(bass / total), 4),           # 60-250 Hz
        "low_end_share": round(float((sub + bass) / total), 4),
        "low_end_db": round(float(10 * np.log10(sub + bass + 1e-12) - 10 * np.log10(total)), 2),
    }


def step_audio(tracks, features, matches):
    est, bass = load_estimates_cached(), load_bass_cached()      # the same dicts early results go into
    todo = [i for i in matches if i not in bass or (i not in features and i not in est)]
    print(f"[audio] {len(todo)} clips to process")
    P.stage("lowend", total=len(todo), label="clips measured")
    loudest = max(bass.values(), key=lambda b: b["low_end_share"], default=None)

    def work(i):
        early = EARLY.pop(i, None)                    # already measured while clips were still arriving
        if early is not None:
            return early.result()
        return measure_one(i, matches[i], i not in bass, i not in features and i not in est)

    def done(i, res, n):
        nonlocal loudest
        P.tick(n, now=f"listened to {tracks[i]['name']} — {tracks[i]['artists'][0]}")
        if isinstance(res, Exception):
            print(f"  skipped {tracks[i]['name']}: {res}")
            return
        b, e = res
        keep(i, b, e)
        if b:
            if not loudest or b["low_end_share"] > loudest["low_end_share"]:
                loudest = b
                P.fact(f"Heaviest bass so far: {tracks[i]['name']} by {tracks[i]['artists'][0]}", key="heaviest")
        if n % 50 == 0:
            print(f"  processed {n}/{len(todo)}")

    net.pmap(work, todo, workers=12, on_result=done)
    save("estimated_features.json", est)
    save("bass.json", bass)
    return est, bass


# Clips are measured the moment they land, not after every clip has been found: ReccoBeats is the
# slowest service (15 analyses per 30 s), so it should start on the first clip, not wait ~2 minutes.
EARLY = {}
_early_pool = ThreadPoolExecutor(max_workers=6)


# Test-only fault injection (FREQUENCY_FAULT): "after_analysis:N" exits right after the Nth analysis comes
# back but before it is saved; "mid_save" exits halfway through writing a cache file. Never set in normal use.
FAULT = os.environ.get("FREQUENCY_FAULT", "")
_fault_count = 0
_fault_lock = threading.Lock()


def _maybe_fault_after_analysis():
    global _fault_count
    if FAULT.startswith("after_analysis:"):
        with _fault_lock:
            _fault_count += 1
            if _fault_count >= int(FAULT.split(":")[1]):
                print(f"[fault] exiting after analysis #{_fault_count}, before saving it", flush=True)
                os._exit(137)


def measure_one(i, match, need_bass, need_estimate):
    clip = download(i, match)
    b = bass_from_clip(clip) if need_bass else None
    e = analyse_clip(clip) if need_estimate else None
    if e:
        net.analysed("library", i)
        _maybe_fault_after_analysis()
    return b, e


_plock = threading.Lock()


def keep(i, b, e):
    """Persist one song's results the moment they exist, so a crash never costs an analysis twice."""
    with _plock:
        if b:
            load_bass_cached()[i] = b
            save("bass.json", load_bass_cached())
        if e:
            load_estimates_cached()[i] = e
            save("estimated_features.json", load_estimates_cached())


def measure_early(i, match, features):
    """Called as soon as a clip is on disk; results are saved as soon as they arrive."""
    est, bass = load_estimates_cached(), load_bass_cached()
    with _plock:
        need_bass, need_est = i not in bass, i not in features and i not in est
    if need_bass or need_est:
        fut = _early_pool.submit(measure_one, i, match, need_bass, need_est)
        fut.add_done_callback(lambda f, i=i: f.exception() is None and keep(i, *f.result()))
        EARLY[i] = fut


_est_cache = _bass_cache = None
def load_estimates_cached():
    global _est_cache
    if _est_cache is None:
        _est_cache = load("estimated_features.json", {})
    return _est_cache


def load_bass_cached():
    global _bass_cache
    if _bass_cache is None:
        _bass_cache = load("bass.json", {})
    return _bass_cache


def step_merge(tracks, features, est, bass):
    out, counts = {}, {"official": 0, "estimated_from_preview": 0, "no_data": 0}
    for i, t in tracks.items():
        if i in features:
            src, feat = "official", features[i]
        elif i in est:
            src, feat = "estimated_from_preview", est[i]
        else:
            src, feat = "no_data", None
        counts[src] += 1
        out[i] = {"name": t["name"], "artists": t["artists"], "sources": t["sources"],
                  "feature_source": src, "features": feat, "bass": bass.get(i)}
    save("enriched.json", out)
    n = len(tracks)
    print("\n== Result ==")
    for k, v in counts.items():
        print(f"  {k}: {v} ({100 * v / n:.1f}%)")
    nb = sum(i in bass for i in tracks)
    print(f"  with measured bass: {nb} ({100 * nb / n:.1f}%)")
    missing = {}
    for i, t in tracks.items():
        if i not in features and i not in est:
            missing[t["artists"][0]] = missing.get(t["artists"][0], 0) + 1
    print("  still missing, by artist:", sorted(missing.items(), key=lambda x: -x[1])[:8])


def main():
    tracks, features = load("tracks.json", {}), load("features.json", {})
    P.stage("previews")
    itunes = load("itunes_matches.json", {})
    deezer = step_deezer(tracks, itunes)
    # Saved matches can include songs that have since left the library (they're kept in case they return);
    # every later step works only on songs in the current library.
    deezer = {i: m for i, m in deezer.items() if i in tracks}
    rescued = {}
    for i in deezer:
        if i not in features:
            rescued[tracks[i]["artists"][0]] = rescued.get(tracks[i]["artists"][0], 0) + 1
    if rescued:
        a, n = max(rescued.items(), key=lambda x: x[1])
        P.fact(f"Deezer had {n} {a} songs nobody else had", key="rescued")
    itunes = step_match({i: t for i, t in tracks.items() if i not in deezer})
    matches = {i: m for i, m in {**itunes, **deezer}.items() if i in tracks}
    P.fact(f"Found 30-second clips for {len(matches)} of {len(tracks)} songs")
    P.finish("previews")
    est, bass = step_audio(tracks, features, matches)
    step_merge(tracks, features, est, bass)
    covered = sum(1 for i in tracks if i in features or i in est)
    P.fact(f"Traits for {100 * covered / max(len(tracks), 1):.1f}% of your songs now. {len(est)} worked out by ear.")
    P.finish("lowend")


if __name__ == "__main__":
    main()
